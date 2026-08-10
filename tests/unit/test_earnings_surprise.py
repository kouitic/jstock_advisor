"""domain/signals/earnings_surprise.pyのテスト(判定精度向上機能Phase C、
コードレビュー対応でv2/v3へ再設計: Dividend Revision除去・raw metrics追加・
EarningsDecisionRelevance統合)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.config.models import (
    EarningsSurpriseCategoryThresholds,
    EarningsSurpriseRulesConfig,
)
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    EarningsSurpriseCategory,
    EarningsSurpriseEvaluationState,
)
from jstock_advisor.domain.signals.earnings_surprise import (
    earnings_surprise_config_values,
    earnings_surprise_result_to_metrics,
    evaluate_earnings_surprise,
)
from jstock_advisor.interfaces.types import EarningsSurpriseRecord

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _config(**overrides: object) -> EarningsSurpriseRulesConfig:
    defaults: dict[str, object] = dict(
        model_version="test-fixture",
        analyst_consensus_weight=1.0,
        analyst_consensus_strong_negative_pct=-0.20,
        analyst_consensus_negative_pct=-0.05,
        analyst_consensus_positive_pct=0.05,
        analyst_consensus_strong_positive_pct=0.20,
        min_coverage_required=0.3,
        coverage_high_threshold=0.9,
        coverage_medium_threshold=0.5,
        category_thresholds=EarningsSurpriseCategoryThresholds(
            strong_positive=50.0, positive=15.0, negative=-15.0, strong_negative=-50.0
        ),
    )
    defaults.update(overrides)
    return EarningsSurpriseRulesConfig.model_validate(defaults)


_CONFIG = _config()

_TEST_SOURCE = DataSourceReference(provider="test-fixture", fetched_at=_NOW)
_PERIOD_END = dt.date(2026, 6, 30)


def _record(
    quarter_end: dt.date = _PERIOD_END,
    eps_actual: Decimal | None = Decimal("100"),
    eps_estimate: Decimal | None = Decimal("90"),
    surprise_pct: float | None = None,
    source: DataSourceReference = _TEST_SOURCE,
) -> EarningsSurpriseRecord:
    if surprise_pct is None and eps_actual is not None and eps_estimate is not None:
        surprise_pct = float((eps_actual - eps_estimate) / eps_estimate)
    return EarningsSurpriseRecord(
        stock_code="0000",
        quarter_end=quarter_end,
        eps_actual=eps_actual,
        eps_estimate=eps_estimate,
        surprise_pct=surprise_pct,
        source=source,
    )


def _evaluate(
    history: list[EarningsSurpriseRecord],
    resolved_period_end: dt.date | None = _PERIOD_END,
    release_confirmation_state: EarningsReleaseConfirmationState = (
        EarningsReleaseConfirmationState.NOT_APPLICABLE
    ),
    decision_relevance: EarningsDecisionRelevance = EarningsDecisionRelevance.NOT_RELEVANT,
    config: EarningsSurpriseRulesConfig | None = None,
):
    return evaluate_earnings_surprise(
        history,
        resolved_period_end,
        release_confirmation_state,
        decision_relevance,
        _NOW,
        config or _CONFIG,
    )


# ===== 正常系: Analyst Consensusのみで構成(v2) =====


def test_evaluated_with_positive_surprise() -> None:
    result = _evaluate([_record(surprise_pct=0.10)])
    assert result.state == EarningsSurpriseEvaluationState.EVALUATED
    assert result.analyst_consensus_component == 50.0
    assert result.coverage == 1.0
    assert result.confidence == ConfidenceLevel.HIGH
    assert result.score == 50.0


# ===== Analyst Consensus成分の段階評価 =====


@pytest.mark.parametrize(
    ("surprise_pct", "expected"),
    [
        (0.25, 100.0),
        (0.10, 50.0),
        (0.0, 0.0),
        (-0.10, -50.0),
        (-0.25, -100.0),
    ],
)
def test_analyst_consensus_component_bands(surprise_pct: float, expected: float) -> None:
    result = _evaluate([_record(surprise_pct=surprise_pct)])
    assert result.analyst_consensus_component == expected


# ===== resolved_period_endとの突合 =====


def test_analyst_component_unavailable_when_no_matching_quarter() -> None:
    result = _evaluate(
        [_record(quarter_end=dt.date(2026, 3, 31))],
        resolved_period_end=_PERIOD_END,
    )
    assert result.analyst_consensus_component is None
    assert "ANALYST_CONSENSUS_UNAVAILABLE" in result.reason_codes
    assert result.state == EarningsSurpriseEvaluationState.NOT_EVALUATED
    # コードレビュー対応(v2): 突合できなかった場合、matched_*系はNoneのまま。
    assert result.matched_quarter_end is None
    assert result.eps_actual is None
    assert result.eps_estimate is None
    assert result.surprise_pct is None


def test_analyst_component_unavailable_when_surprise_pct_missing() -> None:
    result = _evaluate(
        [_record(eps_actual=Decimal("50"), eps_estimate=None, surprise_pct=None)],
    )
    assert result.analyst_consensus_component is None
    assert "ANALYST_CONSENSUS_UNAVAILABLE" in result.reason_codes


# ===== NOT_APPLICABLE(決算反映未確認)ゲート(コードレビュー対応v3:
# EarningsDecisionRelevance統合) =====


@pytest.mark.parametrize(
    "state",
    [
        EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        EarningsReleaseConfirmationState.DELAYED,
    ],
)
def test_not_applicable_when_awaiting_earnings_confirmation_and_relevant(
    state: EarningsReleaseConfirmationState,
) -> None:
    """1. STALE_PAST_DATE + AWAITING_CONFIRMATION + RELEVANT → NOT_APPLICABLE
    2. STALE_PAST_DATE + DELAYED + RELEVANT → NOT_APPLICABLE
    (STALE_PAST_DATEはrelease_confirmation_stateがAWAITING/DELAYEDになる
    前提条件であり、この関数自体はearnings_date_statusを受け取らない)。"""
    result = _evaluate(
        [_record()],
        release_confirmation_state=state,
        decision_relevance=EarningsDecisionRelevance.RELEVANT,
    )
    assert result.state == EarningsSurpriseEvaluationState.NOT_APPLICABLE
    assert result.reason_codes == ("AWAITING_EARNINGS_CONFIRMATION",)
    assert result.score is None
    assert result.release_confirmation_state == state
    assert result.resolved_financial_period_end == _PERIOD_END
    assert result.earnings_decision_relevance == EarningsDecisionRelevance.RELEVANT
    # NOT_APPLICABLEでは突合自体を行わないためmatched_*はNoneのまま。
    assert result.matched_quarter_end is None


def test_not_applicable_when_unknown_relevance_still_continues() -> None:
    """3. STALE_PAST_DATE + DELAYED + UNKNOWN → NOT_APPLICABLEにならず評価継続
    (古すぎる決算予定日でProviderが更新されない場合、決算待ちだけを理由に
    Shadow計測を無期限停止しない)。"""
    result = _evaluate(
        [_record(surprise_pct=0.10)],
        release_confirmation_state=EarningsReleaseConfirmationState.DELAYED,
        decision_relevance=EarningsDecisionRelevance.UNKNOWN,
    )
    assert result.state == EarningsSurpriseEvaluationState.EVALUATED
    assert result.earnings_decision_relevance == EarningsDecisionRelevance.UNKNOWN
    # UNKNOWNは悪材料ではないため、通常どおりのスコアになる(減点されない)。
    assert result.score == 50.0


def test_not_applicable_when_not_relevant_still_continues() -> None:
    """4. STALE_PAST_DATE + AWAITING_CONFIRMATION + NOT_RELEVANT → 評価継続。"""
    result = _evaluate(
        [_record(surprise_pct=0.10)],
        release_confirmation_state=EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        decision_relevance=EarningsDecisionRelevance.NOT_RELEVANT,
    )
    assert result.state == EarningsSurpriseEvaluationState.EVALUATED
    assert result.earnings_decision_relevance == EarningsDecisionRelevance.NOT_RELEVANT


def test_evaluated_when_data_updated_even_if_relevant() -> None:
    """5. DATA_UPDATED → 通常評価(AWAITING_STATESに含まれないため
    decision_relevanceの値に関わらずNOT_APPLICABLEにならない)。"""
    result = _evaluate(
        [_record(surprise_pct=0.10)],
        release_confirmation_state=EarningsReleaseConfirmationState.DATA_UPDATED,
        decision_relevance=EarningsDecisionRelevance.RELEVANT,
    )
    assert result.state == EarningsSurpriseEvaluationState.EVALUATED


# ===== coverage/confidence/NOT_EVALUATEDゲート =====


def test_not_evaluated_when_analyst_consensus_unavailable() -> None:
    result = _evaluate([], resolved_period_end=None)
    assert result.state == EarningsSurpriseEvaluationState.NOT_EVALUATED
    assert result.coverage == 0.0
    assert result.score is None


def test_not_evaluated_even_with_matching_quarter_but_no_estimate() -> None:
    """コードレビュー対応(v2): 実績のみでコンセンサス予想が無い場合も
    analyst consensusは評価不能(NOT_EVALUATED)であり、他のデータで
    穴埋めしない。"""
    result = _evaluate(
        [_record(eps_actual=Decimal("100"), eps_estimate=None, surprise_pct=None)],
    )
    assert result.state == EarningsSurpriseEvaluationState.NOT_EVALUATED
    assert result.coverage == 0.0


# ===== カテゴリ分類 =====


def test_category_strong_positive_surprise() -> None:
    result = _evaluate([_record(surprise_pct=0.25)])
    assert result.category == EarningsSurpriseCategory.STRONG_POSITIVE_SURPRISE


def test_category_strong_negative_surprise() -> None:
    result = _evaluate([_record(surprise_pct=-0.25)])
    assert result.category == EarningsSurpriseCategory.STRONG_NEGATIVE_SURPRISE


def test_category_neutral() -> None:
    result = _evaluate([_record(surprise_pct=0.0)])
    assert result.category == EarningsSurpriseCategory.NEUTRAL


# ===== raw metrics監査情報(コードレビュー対応v2/v3) =====


def test_result_holds_raw_input_values_used_for_scoring() -> None:
    source = DataSourceReference(provider="yfinance", fetched_at=_NOW)
    result = _evaluate(
        [
            _record(
                eps_actual=Decimal("110"),
                eps_estimate=Decimal("100"),
                surprise_pct=0.10,
                source=source,
            )
        ]
    )
    assert result.matched_quarter_end == _PERIOD_END
    assert result.resolved_financial_period_end == _PERIOD_END
    assert result.eps_actual == Decimal("110")
    assert result.eps_estimate == Decimal("100")
    assert result.surprise_pct == pytest.approx(0.10)
    assert result.earnings_surprise_source_provider == "yfinance"
    assert result.earnings_surprise_source_fetched_at == _NOW
    assert result.release_confirmation_state == EarningsReleaseConfirmationState.NOT_APPLICABLE


def test_result_to_metrics_contains_raw_input_values() -> None:
    source = DataSourceReference(provider="yfinance", fetched_at=_NOW)
    result = _evaluate(
        [
            _record(
                eps_actual=Decimal("110"),
                eps_estimate=Decimal("100"),
                surprise_pct=0.10,
                source=source,
            )
        ],
        decision_relevance=EarningsDecisionRelevance.NOT_RELEVANT,
    )
    metrics = earnings_surprise_result_to_metrics(result)
    assert metrics["analyst_consensus_component"] == 50.0
    assert metrics["state"] == "EVALUATED"
    assert metrics["matched_quarter_end"] == _PERIOD_END.isoformat()
    assert metrics["resolved_financial_period_end"] == _PERIOD_END.isoformat()
    assert metrics["eps_actual"] == "110"
    assert metrics["eps_estimate"] == "100"
    assert metrics["surprise_pct"] == pytest.approx(0.10)
    assert metrics["earnings_surprise_source_provider"] == "yfinance"
    assert metrics["earnings_surprise_source_fetched_at"] == _NOW.isoformat()
    assert metrics["release_confirmation_state"] == "NOT_APPLICABLE"
    # 6. metricsへearnings_decision_relevanceが保存される(コードレビュー対応v3)。
    assert metrics["earnings_decision_relevance"] == "NOT_RELEVANT"
    # コードレビュー対応(v2): Dividend Revisionはmetricsに含まれない。
    assert "dividend_revision_component" not in metrics


def test_result_to_metrics_raw_values_none_when_not_matched() -> None:
    result = _evaluate([], resolved_period_end=None)
    metrics = earnings_surprise_result_to_metrics(result)
    assert metrics["matched_quarter_end"] is None
    assert metrics["eps_actual"] is None
    assert metrics["eps_estimate"] is None
    assert metrics["surprise_pct"] is None
    assert metrics["earnings_surprise_source_provider"] is None
    assert metrics["earnings_surprise_source_fetched_at"] is None


def test_config_values_include_category_thresholds() -> None:
    values = earnings_surprise_config_values(_CONFIG)
    assert values["category_thresholds"] == _CONFIG.category_thresholds.model_dump()
    # コードレビュー対応(v2): 削除済みdividend関連キーは含まれない。
    assert "dividend_revision_weight" not in values
    assert "dividend_increase_score" not in values


# ===== Config validation =====


def test_config_rejects_non_positive_weight() -> None:
    with pytest.raises(ValidationError):
        _config(analyst_consensus_weight=0.0)


def test_config_rejects_unordered_analyst_consensus_boundaries() -> None:
    with pytest.raises(ValidationError):
        _config(analyst_consensus_negative_pct=0.10, analyst_consensus_positive_pct=0.05)


def test_config_rejects_invalid_coverage_chain() -> None:
    with pytest.raises(ValidationError):
        _config(min_coverage_required=0.6, coverage_medium_threshold=0.5)


def test_config_category_thresholds_rejects_unordered() -> None:
    with pytest.raises(ValidationError):
        EarningsSurpriseCategoryThresholds(
            strong_positive=10.0, positive=15.0, negative=-15.0, strong_negative=-50.0
        )


def test_config_rejects_dividend_fields_no_longer_accepted() -> None:
    """コードレビュー対応(v2): Dividend Revision関連フィールドは
    EarningsSurpriseRulesConfigからもう受け付けない(strict設定のため
    未知のキーがあればエラーになる)。"""
    with pytest.raises(ValidationError):
        _config(dividend_revision_weight=0.5)
