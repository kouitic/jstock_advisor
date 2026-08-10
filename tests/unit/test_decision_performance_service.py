"""services/decision_performance_service.pyのテスト(判定精度向上機能Phase A)。

コードレビュー対応: DecisionSnapshot専用のEvaluationResultは生成しないため、
joinはEvaluationResult.recommendation_id == DecisionSnapshot.recommendation_id
で行い、Phase A対象ホライズン(既定5/20/60/120/250営業日)のみへ絞り込む。

再レビュー対応: モデル上「1 Recommendation = 1 DecisionSnapshot」だが、不正な
データ投入等で同一recommendation_idに複数のDecisionSnapshotが存在した場合、
list順に依存して結果が変わらないこと・黙って最後の1件を採用しないことを検証する。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig, DecisionEvaluationConfig
from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
    build_decision_id,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DecisionType,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.services.decision_performance_service import (
    DECISION_PERFORMANCE_DUPLICATE_SNAPSHOT_EVENT,
    DECISION_PERFORMANCE_INVALID_COVERAGE_THRESHOLD_EVENT,
    DecisionPerformanceService,
    score_predicate,
)

# config_values_used内のキー名(decision_performance_service.pyの_CONFIG_VALUES_KEYと
# 同じマッピング。timingのみフィールドprefixと異なる"timing_score"を使う)。
_CONFIG_VALUES_KEY = {
    "historical_valuation": "historical_valuation",
    "timing": "timing_score",
    "earnings_surprise": "earnings_surprise",
    "earnings_trend": "earnings_trend",
    "market": "market_environment",
    "sector": "sector_environment",
    "environment": "environment",
}

_NOW = dt.datetime(2026, 8, 8, tzinfo=dt.UTC)


def _config(horizons_business_days: list[int] | None = None) -> AppConfig:
    base = load_config()
    return base.model_copy(
        update={
            "decision_evaluation": DecisionEvaluationConfig(
                horizons_business_days=horizons_business_days or [5, 20, 60, 120, 250]
            )
        }
    )


def _decision(
    recommendation_id: str,
    decision_type: DecisionType = DecisionType.BUY,
    existing_action: RecommendationType = RecommendationType.BUY,
) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=build_decision_id(recommendation_id),
        decision_type=decision_type,
        stock_code="2914",
        evaluated_at=_NOW,
        evaluation_date_jst=_NOW.date(),
        recommendation_id=recommendation_id,
        existing_action=existing_action,
        market_price=Decimal("1150"),
        rule_version="v1-mvp",
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
    )


def _evaluation(
    evaluation_id: str,
    recommendation_id: str,
    horizon_business_days: int | None = 5,
    horizon_calendar_days: int | None = None,
    price_return_pct: float = 3.0,
    max_gain_pct: float | None = 5.0,
    max_drawdown_pct: float | None = -2.0,
    label: EvaluationLabel = EvaluationLabel.SUCCESS,
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=evaluation_id,
        recommendation_id=recommendation_id,
        horizon_business_days=horizon_business_days,
        horizon_calendar_days=horizon_calendar_days,
        evaluated_at=_NOW,
        evaluation_date=_NOW.date(),
        price_at_evaluation=Decimal("1200"),
        price_return_pct=price_return_pct,
        max_gain_pct=max_gain_pct,
        max_drawdown_pct=max_drawdown_pct,
        evaluation_label=label,
        label_evidence="x",
    )


def _decision_with_score(
    recommendation_id: str,
    score_name: str,
    *,
    score: float | None = None,
    confidence: ConfidenceLevel | None = None,
    coverage: float | None = None,
    metrics: dict[str, object] | None = None,
    coverage_high_threshold: float | None = None,
    coverage_medium_threshold: float | None = None,
) -> DecisionSnapshot:
    """1スコア分のscore/confidence/coverage/metrics(+config_values_used内の
    coverage閾値)を設定したDecisionSnapshotを組み立てる。"""
    base = _decision(recommendation_id)
    update: dict[str, object] = {
        f"{score_name}_score": score,
        f"{score_name}_confidence": confidence,
        f"{score_name}_coverage": coverage,
        f"{score_name}_metrics": metrics or {},
    }
    if coverage_high_threshold is not None or coverage_medium_threshold is not None:
        update["config_values_used"] = {
            _CONFIG_VALUES_KEY[score_name]: {
                "coverage_high_threshold": coverage_high_threshold,
                "coverage_medium_threshold": coverage_medium_threshold,
            }
        }
    return base.model_copy(update=update)


def test_summarize_with_no_data_returns_empty_overall(tmp_path: Path) -> None:
    service = DecisionPerformanceService(
        evaluation_repository=EvaluationResultRepository(store_dir=tmp_path),
        decision_repository=DecisionSnapshotRepository(store_dir=tmp_path),
        config=_config(),
    )

    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 0
    assert summary.by_decision_type == []


def test_summarize_joins_via_recommendation_id_and_groups(tmp_path: Path) -> None:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)

    decision_repo.insert_if_absent(_decision("rec-1", DecisionType.BUY, RecommendationType.BUY))
    decision_repo.insert_if_absent(
        _decision("rec-2", DecisionType.PROFIT_TAKING, RecommendationType.SELL)
    )
    eval_repo.save(_evaluation("e1", "rec-1", horizon_business_days=5))
    eval_repo.save(_evaluation("e2", "rec-2", horizon_business_days=5))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )
    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 2
    assert {b.key for b in summary.by_decision_type} == {"BUY", "PROFIT_TAKING"}
    assert {b.key for b in summary.by_existing_action} == {"BUY", "SELL"}
    assert {b.key for b in summary.by_model_version} == {DECISION_SNAPSHOT_MODEL_VERSION}


def test_summarize_excludes_evaluation_with_missing_decision(tmp_path: Path) -> None:
    """decision_idに対応するDecisionSnapshotが存在しない(join欠損。Phase A導入前の
    Recommendation等)場合は推測補完せず、集計から除外する。"""
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    eval_repo.save(_evaluation("e1", "rec-without-decision", horizon_business_days=5))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo,
        decision_repository=DecisionSnapshotRepository(store_dir=tmp_path),
        config=_config(),
    )
    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 0


def test_summarize_only_includes_phase_a_horizons(tmp_path: Path) -> None:
    """1営業日の共通チェックポイント・7暦日評価(週次改善レビュー専用)は
    DecisionPerformanceへ混入しない(既定horizons_business_days=[5,20,60,120,250])。"""
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    decision_repo.insert_if_absent(_decision("rec-1"))

    eval_repo.save(_evaluation("e-1d", "rec-1", horizon_business_days=1))  # 対象外
    eval_repo.save(
        _evaluation("e-7cal", "rec-1", horizon_business_days=None, horizon_calendar_days=7)
    )  # 対象外(暦日評価)
    eval_repo.save(_evaluation("e-5d", "rec-1", horizon_business_days=5))  # 対象
    eval_repo.save(_evaluation("e-20d", "rec-1", horizon_business_days=20))  # 対象

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )
    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 2  # 5d/20dのみ


def test_summarize_filters_by_specific_horizon(tmp_path: Path) -> None:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    decision_repo.insert_if_absent(_decision("rec-1"))
    eval_repo.save(_evaluation("e1", "rec-1", horizon_business_days=5))
    eval_repo.save(_evaluation("e2", "rec-1", horizon_business_days=20))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )
    summary = service.summarize(horizon_business_days=5, now=_NOW)

    assert summary.overall.count == 1


def test_summarize_computes_median_mfe_mae(tmp_path: Path) -> None:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    decision_repo.insert_if_absent(_decision("rec-1"))
    decision_repo.insert_if_absent(_decision("rec-2"))
    eval_repo.save(
        _evaluation(
            "e1",
            "rec-1",
            horizon_business_days=5,
            price_return_pct=2.0,
            max_gain_pct=4.0,
            max_drawdown_pct=-1.0,
        )
    )
    eval_repo.save(
        _evaluation(
            "e2",
            "rec-2",
            horizon_business_days=5,
            price_return_pct=6.0,
            max_gain_pct=8.0,
            max_drawdown_pct=-3.0,
        )
    )

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )
    summary = service.summarize(now=_NOW)

    assert summary.median_price_return_pct == 4.0
    assert summary.avg_mfe_pct == 6.0
    assert summary.avg_mae_pct == -2.0


def test_summarize_excludes_recommendation_with_duplicate_decision_snapshots(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """モデル上「1 Recommendation = 1 DecisionSnapshot」のはずだが、不正なデータ
    投入等で同一recommendation_idに複数のDecisionSnapshotが存在した場合、黙って
    最後の1件を採用せず、対象recommendation_idごと集計から除外する。overall.countが
    既存EvaluationResult件数を超えて増えないことも合わせて確認する。"""
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)

    # 不正データ: 同一recommendation_id="rec-dup"に対しdecision_idの異なる
    # DecisionSnapshotを2件直接投入する(通常の生産コードでは発生しない想定外ケース)。
    dup_a = _decision("rec-dup").model_copy(update={"decision_id": "dup-a"})
    dup_b = _decision("rec-dup").model_copy(
        update={"decision_id": "dup-b", "market_price": Decimal("1999")}
    )
    decision_repo.insert_if_absent(dup_a)
    decision_repo.insert_if_absent(dup_b)
    decision_repo.insert_if_absent(_decision("rec-1"))

    eval_repo.save(_evaluation("e-dup", "rec-dup", horizon_business_days=5))
    eval_repo.save(_evaluation("e1", "rec-1", horizon_business_days=5))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )
    with caplog.at_level(logging.WARNING):
        summary = service.summarize(now=_NOW)

    assert summary.overall.count == 1  # rec-dupは除外され、rec-1のみ集計される
    assert any(
        DECISION_PERFORMANCE_DUPLICATE_SNAPSHOT_EVENT in record.getMessage()
        for record in caplog.records
    )


def test_summarize_duplicate_exclusion_is_order_independent(tmp_path: Path) -> None:
    """同一recommendation_idの重複DecisionSnapshotをどちらの順序で保存しても、
    集計結果(除外されること)が変わらない(list順で結果が変わる設計を禁止)。"""
    decision_repo_a = DecisionSnapshotRepository(store_dir=tmp_path / "a")
    decision_repo_b = DecisionSnapshotRepository(store_dir=tmp_path / "b")
    eval_repo_a = EvaluationResultRepository(store_dir=tmp_path / "a")
    eval_repo_b = EvaluationResultRepository(store_dir=tmp_path / "b")

    dup_a = _decision("rec-dup").model_copy(update={"decision_id": "dup-a"})
    dup_b = _decision("rec-dup").model_copy(update={"decision_id": "dup-b"})

    decision_repo_a.insert_if_absent(dup_a)
    decision_repo_a.insert_if_absent(dup_b)
    decision_repo_b.insert_if_absent(dup_b)
    decision_repo_b.insert_if_absent(dup_a)
    eval_repo_a.save(_evaluation("e-dup", "rec-dup", horizon_business_days=5))
    eval_repo_b.save(_evaluation("e-dup", "rec-dup", horizon_business_days=5))

    service_a = DecisionPerformanceService(
        evaluation_repository=eval_repo_a, decision_repository=decision_repo_a, config=_config()
    )
    service_b = DecisionPerformanceService(
        evaluation_repository=eval_repo_b, decision_repository=decision_repo_b, config=_config()
    )

    count_a = service_a.summarize(now=_NOW).overall.count
    count_b = service_b.summarize(now=_NOW).overall.count
    assert count_a == count_b == 0


# ===== DecisionPerformance分析強化: summarize_score_segments() =====


class _StaticDecisionRepo:
    """DecisionSnapshotRepositoryのJSON永続化(ラウンドトリップ)を経由しない
    in-memoryスタブ。json_store.pyの保存/再読込では、NaN/InfinityはNoneへ、
    bool/数値文字列は通常のfloatへPydanticにより自動的に丸められてしまうため、
    coverage自身が本当に壊れているケース(_is_valid_coverage()のテスト対象)を
    検証するには、この経路を迂回して生の値をそのまま_extract_coverage_tier()
    へ渡す必要がある。"""

    def __init__(self, decisions: list[DecisionSnapshot]) -> None:
        self._decisions = decisions

    def list_all(self) -> list[DecisionSnapshot]:
        return list(self._decisions)


class _StaticEvaluationRepo:
    def __init__(self, evaluations: list[EvaluationResult]) -> None:
        self._evaluations = evaluations

    def list_all(self) -> list[EvaluationResult]:
        return list(self._evaluations)


def _service_with(
    tmp_path: Path, decisions: list[DecisionSnapshot], evaluations: list[EvaluationResult]
) -> DecisionPerformanceService:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    for d in decisions:
        decision_repo.insert_if_absent(d)
    for e in evaluations:
        eval_repo.save(e)
    return DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )


@pytest.mark.parametrize(
    "score_name",
    [
        "historical_valuation",
        "timing",
        "earnings_surprise",
        "earnings_trend",
        "market",
        "sector",
        "environment",
    ],
)
def test_summarize_score_segments_groups_by_category(tmp_path: Path, score_name: str) -> None:
    decisions = [
        _decision_with_score("rec-cheap", score_name, metrics={"category": "CHEAP"}),
        _decision_with_score("rec-expensive", score_name, metrics={"category": "EXPENSIVE"}),
    ]
    evaluations = [
        _evaluation("e1", "rec-cheap", horizon_business_days=60, price_return_pct=8.0),
        _evaluation("e2", "rec-expensive", horizon_business_days=60, price_return_pct=-2.0),
    ]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments(score_name, horizon_business_days=60, now=_NOW)

    assert {s.bucket_key for s in result.by_category} == {"CHEAP", "EXPENSIVE"}
    cheap = next(s for s in result.by_category if s.bucket_key == "CHEAP")
    assert cheap.sample_count == 1
    assert cheap.average_return_pct == 8.0


def test_summarize_score_segments_accepts_category_not_in_current_enum(tmp_path: Path) -> None:
    """コードレビュー対応(第4回): 保存済みcategory文字列は現在のEnumで
    再検証しない。将来Enumのメンバー名が変わっても(または過去に一時的に
    存在した値でも)、過去の事実としてそのままbucketに使われることを確認する。"""
    decisions = [_decision_with_score("rec-1", "timing", metrics={"category": "SOME_FUTURE_LABEL"})]
    evaluations = [_evaluation("e1", "rec-1", horizon_business_days=60)]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    assert {s.bucket_key for s in result.by_category} == {"SOME_FUTURE_LABEL"}


def test_summarize_score_segments_excludes_missing_category(tmp_path: Path) -> None:
    decisions = [_decision_with_score("rec-1", "timing", metrics={})]
    evaluations = [_evaluation("e1", "rec-1", horizon_business_days=60)]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    assert result.by_category == []


def test_summarize_score_segments_groups_by_confidence(tmp_path: Path) -> None:
    decisions = [
        _decision_with_score("rec-high", "timing", confidence=ConfidenceLevel.HIGH),
        _decision_with_score("rec-low", "timing", confidence=ConfidenceLevel.LOW),
        _decision_with_score("rec-none", "timing", confidence=None),
    ]
    evaluations = [
        _evaluation("e1", "rec-high", horizon_business_days=60),
        _evaluation("e2", "rec-low", horizon_business_days=60),
        _evaluation("e3", "rec-none", horizon_business_days=60),
    ]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    assert {s.bucket_key for s in result.by_confidence} == {"HIGH", "LOW"}


def test_coverage_tier_low_is_not_conflated_with_missing(tmp_path: Path) -> None:
    """coverage=0.0(実際に計算された低coverage)は"LOW"へ正しく分類され、
    coverage=None(未計算)とは区別されて除外されないことを確認する。"""
    decisions = [
        _decision_with_score(
            "rec-zero",
            "timing",
            coverage=0.0,
            coverage_high_threshold=0.9,
            coverage_medium_threshold=0.5,
        ),
        _decision_with_score("rec-missing", "timing", coverage=None),
    ]
    evaluations = [
        _evaluation("e1", "rec-zero", horizon_business_days=60),
        _evaluation("e2", "rec-missing", horizon_business_days=60),
    ]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    assert {s.bucket_key for s in result.by_coverage_tier} == {"LOW"}
    assert result.by_coverage_tier[0].sample_count == 1


def test_coverage_tier_uses_point_in_time_thresholds_not_current_config(tmp_path: Path) -> None:
    """コードレビュー対応(最重要): 過去DecisionSnapshotをconfig_values_used
    に保存された当時のcoverage閾値で分類し、現在ロードされたAppConfigの
    閾値は一切参照しない。同一coverage値でも、保存済み閾値が異なれば
    異なるtierになることを直接確認する。"""
    decisions = [
        _decision_with_score(
            "rec-old-threshold",
            "timing",
            coverage=0.6,
            coverage_high_threshold=0.5,
            coverage_medium_threshold=0.3,  # 0.6は当時基準でHIGH
        ),
        _decision_with_score(
            "rec-new-threshold",
            "timing",
            coverage=0.6,
            coverage_high_threshold=0.9,
            coverage_medium_threshold=0.5,  # 0.6は当時基準でMEDIUM
        ),
    ]
    evaluations = [
        _evaluation("e1", "rec-old-threshold", horizon_business_days=60),
        _evaluation("e2", "rec-new-threshold", horizon_business_days=60),
    ]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    tiers = {s.bucket_key: s.sample_count for s in result.by_coverage_tier}
    assert tiers == {"HIGH": 1, "MEDIUM": 1}


@pytest.mark.parametrize(
    ("high", "medium"),
    [
        (None, 0.5),  # 欠損
        ("bad", 0.5),  # 型不正
        (0.5, 0.7),  # medium >= high
        (1.5, 0.5),  # high > 1
        (0.9, -0.1),  # medium < 0
    ],
)
def test_coverage_tier_excludes_invalid_threshold_and_logs_warning(
    tmp_path: Path, high: object, medium: object, caplog: pytest.LogCaptureFixture
) -> None:
    """異常なconfig_values_used(欠損/型不正/範囲外/medium>=high)は
    coverage_tier分析からのみ除外し、warningログを出す。レポート全体は
    落ちない。"""
    decision = _decision("rec-1").model_copy(
        update={
            "timing_score": 10.0,
            "timing_confidence": ConfidenceLevel.HIGH,
            "timing_coverage": 0.6,
            "timing_metrics": {"category": "TAILWIND", "model_version": "timing_v4"},
            "config_values_used": {
                "timing_score": {
                    "coverage_high_threshold": high,
                    "coverage_medium_threshold": medium,
                }
            },
        }
    )
    evaluations = [_evaluation("e1", "rec-1", horizon_business_days=60)]
    service = _service_with(tmp_path, [decision], evaluations)

    with caplog.at_level(logging.WARNING):
        result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    assert result.by_coverage_tier == []
    assert any(
        DECISION_PERFORMANCE_INVALID_COVERAGE_THRESHOLD_EVENT in r.getMessage()
        for r in caplog.records
    )
    # 同じ異常データでも他の分析軸(category/confidence/model_version)は継続する。
    assert {s.bucket_key for s in result.by_category} == {"TAILWIND"}
    assert {s.bucket_key for s in result.by_confidence} == {"HIGH"}
    assert {s.bucket_key for s in result.by_model_version} == {"timing_v4"}


@pytest.mark.parametrize(
    "broken_config_values",
    [
        "broken",  # 文字列(Mappingでない)
        None,  # キー自体が存在しない(config_values_used自体は空dict)
        ["not", "a", "mapping"],  # list
        123,  # int
    ],
)
def test_coverage_tier_excludes_when_config_values_used_entry_is_not_a_mapping(
    tmp_path: Path, broken_config_values: object, caplog: pytest.LogCaptureFixture
) -> None:
    """コードレビュー対応: config_values_used["timing_score"]自体がdict
    (Mapping)でない壊れたデータ(文字列・list・数値等)の場合、
    "broken".get(...)のようなAttributeErrorでレポート全体を落とさず、
    coverage_tier分析からのみ安全に除外する。他の分析軸は継続する。"""
    config_values_used = (
        {} if broken_config_values is None else {"timing_score": broken_config_values}
    )
    decision = _decision("rec-1").model_copy(
        update={
            "timing_confidence": ConfidenceLevel.HIGH,
            "timing_coverage": 0.6,
            "timing_metrics": {"category": "TAILWIND", "model_version": "timing_v4"},
            "config_values_used": config_values_used,
        }
    )
    evaluations = [_evaluation("e1", "rec-1", horizon_business_days=60)]
    service = _service_with(tmp_path, [decision], evaluations)

    with caplog.at_level(logging.WARNING):
        result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    assert result.by_coverage_tier == []
    assert any(
        DECISION_PERFORMANCE_INVALID_COVERAGE_THRESHOLD_EVENT in r.getMessage()
        for r in caplog.records
    )
    assert {s.bucket_key for s in result.by_category} == {"TAILWIND"}
    assert {s.bucket_key for s in result.by_confidence} == {"HIGH"}
    assert {s.bucket_key for s in result.by_model_version} == {"timing_v4"}


@pytest.mark.parametrize(
    "invalid_coverage",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.1,
        1.1,
        True,  # bool(int派生だが除外)
        "0.5",  # 文字列
    ],
)
def test_coverage_tier_excludes_invalid_coverage_value(
    invalid_coverage: object, caplog: pytest.LogCaptureFixture
) -> None:
    """コードレビュー対応: coverage自身がNaN/Infinity/範囲外/bool/非数値の
    場合、coverage_tier分析からのみ除外する。特にNaNは比較演算が常にFalseに
    なるため、検証しないと誤ってLOWへ分類されてしまう不具合を防ぐ。

    JSON永続化を経由するとNaN/Infinityは自動的にNoneへ、bool/数値文字列は
    通常のfloatへ丸められてしまい、この検証をすり抜けてしまうため、
    _StaticDecisionRepoでラウンドトリップを迂回し生の値を直接検証する。"""
    decision = _decision("rec-1").model_copy(
        update={
            "timing_confidence": ConfidenceLevel.HIGH,
            "timing_coverage": invalid_coverage,
            "timing_metrics": {"category": "TAILWIND", "model_version": "timing_v4"},
            "config_values_used": {
                "timing_score": {
                    "coverage_high_threshold": 0.9,
                    "coverage_medium_threshold": 0.5,
                }
            },
        }
    )
    evaluations = [_evaluation("e1", "rec-1", horizon_business_days=60)]
    service = DecisionPerformanceService(
        evaluation_repository=_StaticEvaluationRepo(evaluations),
        decision_repository=_StaticDecisionRepo([decision]),
        config=_config(),
    )

    with caplog.at_level(logging.WARNING):
        result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    assert result.by_coverage_tier == []
    assert any(
        DECISION_PERFORMANCE_INVALID_COVERAGE_THRESHOLD_EVENT in r.getMessage()
        for r in caplog.records
    )
    # 異常値をLOW/HIGHへ丸めない(誤ってLOWへ分類されていないことも直接確認)。
    assert "LOW" not in {s.bucket_key for s in result.by_coverage_tier}
    assert {s.bucket_key for s in result.by_category} == {"TAILWIND"}


def test_summarize_score_segments_groups_by_individual_model_version(tmp_path: Path) -> None:
    """DecisionSnapshot.model_version(Decision Enhancement Layer全体)ではなく、
    スコア個別のmodel_version(timing_v3 vs timing_v4)で分離できることを確認する。"""
    decisions = [
        _decision_with_score("rec-v3", "timing", metrics={"model_version": "timing_v3"}),
        _decision_with_score("rec-v4", "timing", metrics={"model_version": "timing_v4"}),
        _decision_with_score("rec-none", "timing", metrics={}),
    ]
    evaluations = [
        _evaluation("e1", "rec-v3", horizon_business_days=60),
        _evaluation("e2", "rec-v4", horizon_business_days=60),
        _evaluation("e3", "rec-none", horizon_business_days=60),
    ]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    assert {s.bucket_key for s in result.by_model_version} == {"timing_v3", "timing_v4"}


@pytest.mark.parametrize("score_name", ["market", "sector"])
def test_phase_d_coverage_tier_uses_own_config_values_key(tmp_path: Path, score_name: str) -> None:
    """Phase D(market/sector)のcoverage_tierも、config_values_used内の専用
    キー(market_environment/sector_environment)から当時の閾値を復元できる
    ことを確認する(STEP1のcoverage_tier分析がPhase Dでも実データで機能する)。"""
    decisions = [
        _decision_with_score(
            "rec-high",
            score_name,
            coverage=0.95,
            coverage_high_threshold=0.9,
            coverage_medium_threshold=0.5,
        ),
    ]
    evaluations = [_evaluation("e1", "rec-high", horizon_business_days=60)]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments(score_name, horizon_business_days=60, now=_NOW)

    assert {s.bucket_key for s in result.by_coverage_tier} == {"HIGH"}


def test_phase_d_sector_not_evaluated_excluded_from_all_dimensions(tmp_path: Path) -> None:
    """Sector NOT_EVALUATED/NOT_APPLICABLEレコード(sector_score=None)は、
    既存仕様どおりcategory/confidence/coverage_tier全ての分析軸から除外される
    (score is Noneのため対象外)。"""
    decisions = [
        _decision_with_score("rec-evaluated", "sector", score=10.0, coverage=0.9),
        _decision_with_score("rec-not-applicable", "sector", score=None, coverage=None),
    ]
    evaluations = [
        _evaluation("e1", "rec-evaluated", horizon_business_days=60),
        _evaluation("e2", "rec-not-applicable", horizon_business_days=60),
    ]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments("sector", horizon_business_days=60, now=_NOW)

    total_segment_samples = sum(s.sample_count for s in result.by_coverage_tier)
    assert total_segment_samples <= 1


def test_summarize_score_segments_rejects_invalid_score_name(tmp_path: Path) -> None:
    service = _service_with(tmp_path, [], [])
    with pytest.raises(ValueError, match="score_name"):
        service.summarize_score_segments("not_a_score", horizon_business_days=60, now=_NOW)  # type: ignore[arg-type]


def test_summarize_score_segments_rejects_non_phase_a_horizon(tmp_path: Path) -> None:
    service = _service_with(tmp_path, [], [])
    with pytest.raises(ValueError, match="horizon_business_days"):
        service.summarize_score_segments("timing", horizon_business_days=7, now=_NOW)


def test_summarize_score_segments_excludes_non_phase_a_evaluations(tmp_path: Path) -> None:
    """summarize()と同じhorizon許可リスト(1営業日・7暦日の除外)を
    summarize_score_segments()も共有していることを確認する。"""
    decisions = [_decision_with_score("rec-1", "timing", metrics={"category": "TAILWIND"})]
    evaluations = [
        _evaluation("e-1d", "rec-1", horizon_business_days=1),
        _evaluation("e-7cal", "rec-1", horizon_business_days=None, horizon_calendar_days=7),
        _evaluation("e-60d", "rec-1", horizon_business_days=60),
    ]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    assert result.by_category[0].sample_count == 1


def test_summarize_score_segments_excludes_join_and_duplicate_failures(tmp_path: Path) -> None:
    eval_repo_only = EvaluationResultRepository(store_dir=tmp_path)
    eval_repo_only.save(_evaluation("e-orphan", "rec-without-decision", horizon_business_days=60))
    service = DecisionPerformanceService(
        evaluation_repository=eval_repo_only,
        decision_repository=DecisionSnapshotRepository(store_dir=tmp_path),
        config=_config(),
    )
    result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)
    assert result.by_category == []
    assert result.by_confidence == []


def test_summarize_score_segments_median_and_excess_return(tmp_path: Path) -> None:
    decisions = [
        _decision_with_score("rec-1", "timing", metrics={"category": "TAILWIND"}),
        _decision_with_score("rec-2", "timing", metrics={"category": "TAILWIND"}),
    ]
    evaluations = [
        EvaluationResult(
            evaluation_id="e1",
            recommendation_id="rec-1",
            horizon_business_days=60,
            evaluated_at=_NOW,
            evaluation_date=_NOW.date(),
            price_at_evaluation=Decimal("1200"),
            price_return_pct=2.0,
            excess_return_pct=1.0,
            max_gain_pct=3.0,
            max_drawdown_pct=-1.0,
            evaluation_label=EvaluationLabel.SUCCESS,
            label_evidence="x",
        ),
        EvaluationResult(
            evaluation_id="e2",
            recommendation_id="rec-2",
            horizon_business_days=60,
            evaluated_at=_NOW,
            evaluation_date=_NOW.date(),
            price_at_evaluation=Decimal("1200"),
            price_return_pct=6.0,
            excess_return_pct=5.0,
            max_gain_pct=9.0,
            max_drawdown_pct=-3.0,
            evaluation_label=EvaluationLabel.SUCCESS,
            label_evidence="x",
        ),
    ]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.summarize_score_segments("timing", horizon_business_days=60, now=_NOW)

    segment = result.by_category[0]
    assert segment.median_return_pct == 4.0
    assert segment.median_excess_return_pct == 3.0
    assert segment.average_mfe_pct == 6.0
    assert segment.average_mae_pct == -2.0


# ===== DecisionPerformance分析強化: compare_segments() =====


def test_compare_segments_returns_two_groups_with_overlap_count(tmp_path: Path) -> None:
    decisions = [
        _decision_with_score("rec-good", "timing", score=40.0),
        _decision_with_score("rec-poor", "timing", score=-40.0),
    ]
    evaluations = [
        _evaluation("e1", "rec-good", horizon_business_days=60, price_return_pct=10.0),
        _evaluation("e2", "rec-poor", horizon_business_days=60, price_return_pct=-5.0),
    ]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.compare_segments(
        "good",
        score_predicate("timing", minimum=20),
        "poor",
        score_predicate("timing", maximum=-20),
        horizon_business_days=60,
        now=_NOW,
    )

    assert result.group_a.bucket_key == "good"
    assert result.group_a.sample_count == 1
    assert result.group_a.average_return_pct == 10.0
    assert result.group_b.bucket_key == "poor"
    assert result.group_b.sample_count == 1
    assert result.overlap_count == 0


def test_compare_segments_reports_overlap_when_ranges_overlap(tmp_path: Path) -> None:
    decisions = [_decision_with_score("rec-1", "timing", score=25.0)]
    evaluations = [_evaluation("e1", "rec-1", horizon_business_days=60)]
    service = _service_with(tmp_path, decisions, evaluations)

    result = service.compare_segments(
        "a",
        score_predicate("timing", minimum=20),
        "b",
        score_predicate("timing", maximum=30),
        horizon_business_days=60,
        now=_NOW,
    )

    assert result.overlap_count == 1


def test_compare_segments_rejects_non_phase_a_horizon(tmp_path: Path) -> None:
    service = _service_with(tmp_path, [], [])
    with pytest.raises(ValueError, match="horizon_business_days"):
        service.compare_segments(
            "a",
            score_predicate("timing", minimum=20),
            "b",
            score_predicate("timing", maximum=-20),
            horizon_business_days=999,
            now=_NOW,
        )


def test_score_predicate_boundaries_are_inclusive_and_excludes_none(tmp_path: Path) -> None:
    predicate = score_predicate("timing", minimum=10.0, maximum=20.0)
    at_min = _decision_with_score("rec-min", "timing", score=10.0)
    at_max = _decision_with_score("rec-max", "timing", score=20.0)
    below = _decision_with_score("rec-below", "timing", score=9.9)
    missing = _decision_with_score("rec-missing", "timing", score=None)

    assert predicate(at_min) is True
    assert predicate(at_max) is True
    assert predicate(below) is False
    assert predicate(missing) is False
