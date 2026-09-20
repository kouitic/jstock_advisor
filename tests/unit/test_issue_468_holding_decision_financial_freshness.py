"""Issue #468: 保有判断(F-10)への財務データの期間鮮度(#52 B3)の接続。

USER決定 U17 = OPTION_B_CONFIDENCE_CAP(#468 issuecomment-5746860554)と、Q1 = W2
(issuecomment-5746914054)の契約を固定する。

契約の核心:
  STALE  : confidenceに**HIGHを許可しない**(上限MEDIUM)。「1段階下げる」ではない。
           HIGH→MEDIUM / MEDIUM→MEDIUM / LOW→LOW / INSUFFICIENT_EVIDENCE→INSUFFICIENT_EVIDENCE。
           利用者向けの留意事項(既存の FINANCIAL_STALE_USER_WARNING)を追加する。
  FRESH  : 何も変えない。
  UNKNOWN: 何も変えない(STALE扱いにしない。留意事項も出さない)。
  不変    : score / component score / coverage / coverage gate / 通知判定(should_notify) /
           永続schema(HoldingDecisionResult)。STALEをcoverage不足・不評価へ変換しない
           (それを行うとOPTION_Cになり、決定に反する)。

層ごとの検証:
  A. combine_holding_decision(純関数): confidenceの上限と、それ以外の不変性(T1〜T5・T7〜T10)
  B. HoldingDecisionService.evaluate: 共通の鮮度判定の再利用・配線・監査・境界(T6・T11・T12・X)
  C. build_holding_decision_recommendation + LINE本文: 留意事項の格納と表示(W2)

★ 銘柄コードは実在しない0000系・架空値のみ。Productionへは一切アクセスしない。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    PriceRangeEvaluationState,
    RecentPeriodsSource,
)
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.holding_decision_score import (
    HoldingDecisionOutcome,
    combine_holding_decision,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.interfaces.types import FinancialSummary, QuarterlyFinancials
from jstock_advisor.services import line_notification_service as line_module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.financial_freshness_integration import (
    FINANCIAL_STALE_USER_WARNING,
    assess_financial_freshness,
)
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
)
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot

_CFG = load_config()
_RULES = _CFG.holding_decision
_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"

_STOCK_CODE = "0000"
_MOCK_STOCK_CODE = "2914"  # mock providerがデータを返す銘柄コード(評価の入力用)

# 期末2026-03-31 -> 期待される次の期末2026-06-30 -> 報告期限 2026-06-30 + 50日 = 2026-08-19。
# 期限当日はSTALE側に含める(domain契約)。期限前日はFRESH。
_LATEST_PERIOD_END = dt.date(2026, 3, 31)
_FRESH_NOW = dt.datetime(2026, 8, 18, 7, 0, tzinfo=dt.UTC)
_STALE_NOW = dt.datetime(2026, 8, 19, 7, 0, tzinfo=dt.UTC)

_HIGH = HoldingDecisionConfidenceLevel.HIGH
_MEDIUM = HoldingDecisionConfidenceLevel.MEDIUM
_LOW = HoldingDecisionConfidenceLevel.LOW
_INSUFFICIENT = HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE

_NO_GATE = HoldingDecisionHardGate(triggered=False)


# ---------------------------------------------------------------------------
# A. combine_holding_decision(純関数)
# ---------------------------------------------------------------------------

# confidenceはcoverageのみで決まる(全構成要素のcoverageを揃えると overall = その値)。
# 閾値は config から読む(high 0.79 / medium 0.67 / low 0.50。テスト内へ値を複製しない)。
_COVERAGE_FOR = {
    _HIGH: 1.0,
    _MEDIUM: (
        _RULES.confidence_thresholds.medium_minimum + _RULES.confidence_thresholds.high_minimum
    )
    / 2,
    _LOW: (_RULES.confidence_thresholds.low_minimum + _RULES.confidence_thresholds.medium_minimum)
    / 2,
    _INSUFFICIENT: _RULES.confidence_thresholds.low_minimum / 2,
}


def _combine(
    coverage: float,
    *,
    stale: bool | None,
    cq: float = 25.0,
    it: float = 25.0,
    rd: float = 50.0,
    risk_coverage: float | None = None,
) -> HoldingDecisionOutcome:
    """staleがNoneのときは引数を渡さない(従来の呼び出し = 既定値)。"""
    kwargs: dict[str, Any] = {}
    if stale is not None:
        kwargs["financial_stale"] = stale
    return combine_holding_decision(
        CompanyQualityScore(score=cq, coverage_ratio=coverage),
        InvestmentThesisScore(score=it, coverage_ratio=coverage),
        RiskDeductionScore(
            score=rd, coverage_ratio=coverage if risk_coverage is None else risk_coverage
        ),
        _NO_GATE,
        _RULES,
        **kwargs,
    )


def test_control_coverage_fixtures_reach_each_confidence_level() -> None:
    """基準の前提: 各coverageが狙ったconfidenceになる(これが崩れると以降の検証が空になる)。"""
    for level, coverage in _COVERAGE_FOR.items():
        assert _combine(coverage, stale=None).confidence == level


def test_t1_fresh_high_stays_high() -> None:
    """T1: FRESH + HIGH -> HIGH(financial_stale=False は従来と同じ)。"""
    assert _combine(_COVERAGE_FOR[_HIGH], stale=False).confidence == _HIGH


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        pytest.param(_HIGH, _MEDIUM, id="t2-high-becomes-medium"),
        pytest.param(_MEDIUM, _MEDIUM, id="t3-medium-stays-medium"),
        pytest.param(_LOW, _LOW, id="t4-low-stays-low"),
        pytest.param(_INSUFFICIENT, _INSUFFICIENT, id="t5-insufficient-stays-insufficient"),
    ],
)
def test_t2_to_t5_stale_caps_confidence_at_medium_and_never_lowers_further(
    existing: HoldingDecisionConfidenceLevel, expected: HoldingDecisionConfidenceLevel
) -> None:
    """T2〜T5: STALEはHIGHを許可しない(上限MEDIUM)。MEDIUM以下は変えない。

    「1段階下げる」実装だと、MEDIUM→LOW・LOW→INSUFFICIENTと下がってしまい、U17の契約
    (上限であって減点ではない)に反する。
    """
    result = _combine(_COVERAGE_FOR[existing], stale=True)

    assert result.confidence == expected


def test_default_argument_is_identical_to_not_stale() -> None:
    """financial_stale を渡さない従来の呼び出しは、False と完全に同じ結果を返す。"""
    for level, coverage in _COVERAGE_FOR.items():
        assert _combine(coverage, stale=None) == _combine(coverage, stale=False), level


# 通知される入力(should_notify=True)・されない入力・ハードゲート等を含む、代表的な入力の網羅。
_INPUT_GRID = [
    pytest.param(1.0, 25.0, 25.0, 50.0, id="high-notify"),
    pytest.param(1.0, 50.0, 50.0, 0.0, id="high-strong-hold-no-notify"),
    pytest.param(1.0, 0.0, 0.0, 100.0, id="high-min-score-notify"),
    pytest.param(_COVERAGE_FOR[_MEDIUM], 25.0, 25.0, 50.0, id="medium"),
    pytest.param(_COVERAGE_FOR[_LOW], 20.0, 20.0, 60.0, id="low-coverage-not-satisfied"),
    pytest.param(_COVERAGE_FOR[_INSUFFICIENT], 20.0, 20.0, 60.0, id="insufficient"),
]


@pytest.mark.parametrize(("coverage", "cq", "it", "rd"), _INPUT_GRID)
def test_t7_to_t10_stale_changes_nothing_but_confidence(
    coverage: float, cq: float, it: float, rd: float
) -> None:
    """T7〜T10: STALEの有無で、confidence以外の全項目が完全に一致する。

    score(base / final / display)・カテゴリ・component coverage・coverage_satisfied・
    coverage_gate_passed・score_threshold_met・should_notify・hard_gate を比較する。
    STALEを「coverage不足」「不評価」へ変換していないこと(=OPTION_Cでないこと)の固定でもある。
    """
    fresh = _combine(coverage, stale=False, cq=cq, it=it, rd=rd)
    stale = _combine(coverage, stale=True, cq=cq, it=it, rd=rd)

    fresh_fields = dataclasses.asdict(fresh)
    stale_fields = dataclasses.asdict(stale)
    assert fresh_fields.pop("confidence") is not None
    assert stale_fields.pop("confidence") is not None
    assert stale_fields == fresh_fields  # confidence 以外はすべて同一
    # 名指しの確認(上の一括比較が空振りしていないことの保証)。
    for name in (
        "base_score",
        "final_score",
        "display_value",
        "category",
        "coverage",
        "score_threshold_met",
        "coverage_satisfied",
        "coverage_gate_passed",
        "should_notify",
    ):
        assert getattr(stale, name) == getattr(fresh, name), name


def test_stale_high_with_notification_still_notifies() -> None:
    """通知される(should_notify=True)HIGHの入力で、STALEでも通知可否は変わらない。"""
    fresh = _combine(1.0, stale=False, cq=0.0, it=0.0, rd=100.0)
    stale = _combine(1.0, stale=True, cq=0.0, it=0.0, rd=100.0)

    assert fresh.should_notify is True
    assert fresh.confidence == _HIGH
    assert stale.should_notify is True
    assert stale.confidence == _MEDIUM


def test_x2_stale_composes_with_the_existing_risk_coverage_cap_without_double_lowering() -> None:
    """既存のcap(risk_deductionのcoverageが低いとHIGH→MEDIUM)と合成しても、LOWにならない。

    既存のcapでMEDIUMになった入力にSTALEを重ねても、MEDIUMのまま(二重に下がらない)。
    FRESHの既存capのみの結果は従来と同一。
    """
    below = _RULES.coverage_thresholds.risk_deduction_confidence_minimum - 0.01
    # overall >= high_minimum を保ちつつ、risk_deductionのcoverageだけを下げる。
    kwargs: dict[str, Any] = {"coverage": 1.0, "risk_coverage": below}
    existing_only = _combine(stale=False, **kwargs)
    with_stale = _combine(stale=True, **kwargs)

    assert existing_only.confidence == _MEDIUM  # 既存capが効いている(前提)
    assert with_stale.confidence == _MEDIUM
    assert with_stale.coverage == existing_only.coverage


# ---------------------------------------------------------------------------
# B. HoldingDecisionService.evaluate(共通の鮮度判定の再利用・配線・監査)
# ---------------------------------------------------------------------------

_SOURCE = DataSourceReference(provider="test-fixture", fetched_at=_STALE_NOW)


def _financial(
    *, quarterly: bool, fiscal_year_end_month: int | None = 3, stock_code: str = _MOCK_STOCK_CODE
) -> FinancialSummary:
    """FRESH / STALE / UNKNOWN を作り分ける財務データ(判定は評価時刻 now で変わる)。"""
    quarters = (
        [
            QuarterlyFinancials(stock_code=stock_code, quarter_end=q, source=_SOURCE.model_copy())
            for q in (dt.date(2025, 12, 31), _LATEST_PERIOD_END)
        ]
        if quarterly
        else []
    )
    return FinancialSummary(
        stock_code=stock_code,
        stock_name=None,
        fiscal_period_end=_LATEST_PERIOD_END,
        fiscal_year_end_month=fiscal_year_end_month,
        recent_quarters=quarters,
        recent_periods_source=(
            RecentPeriodsSource.QUARTERLY if quarterly else RecentPeriodsSource.UNAVAILABLE
        ),
        source=DataSourceReference(provider="test-fixture", fetched_at=_STALE_NOW),
    )


# (名前, 四半期データあり, 決算月, 評価時刻, STALEか)
_VERDICT_CASES = {
    "FRESH": (True, 3, _FRESH_NOW, False),
    "STALE": (True, 3, _STALE_NOW, True),
    "UNKNOWN": (False, None, _STALE_NOW, False),
}


def _cfg_where_high_is_reachable() -> AppConfig:
    """mock providerのデータでconfidenceがHIGHに到達する設定(閾値だけを下げた複製)。

    mockのcoverageは約0.70(MEDIUM)のため、HIGHの入力を得るためにテスト用に閾値を下げる。
    財務鮮度の判定・猶予日数には触れない。
    """
    rules = _RULES.model_copy(
        update={
            "confidence_thresholds": _RULES.confidence_thresholds.model_copy(
                update={"high_minimum": 0.5}
            ),
            "coverage_thresholds": _RULES.coverage_thresholds.model_copy(
                update={"risk_deduction_confidence_minimum": 0.0}
            ),
        }
    )
    return _CFG.model_copy(update={"holding_decision": rules})


_CFG_HIGH = _cfg_where_high_is_reachable()
_PROVIDERS = build_mock_provider_bundle(_STALE_NOW)


def _holding(stock_code: str = _MOCK_STOCK_CODE) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_STALE_NOW,
        updated_at=_STALE_NOW,
    )


def _service(
    store_dir: Path, config: AppConfig = _CFG_HIGH
) -> tuple[HoldingDecisionService, AuditLogRepository]:
    audit_repo = AuditLogRepository(store_dir)
    service = HoldingDecisionService(
        _PROVIDERS,
        config,
        investment_thesis_service=InvestmentThesisService(store_dir=store_dir),
        runtime_config_service=HoldingDecisionRuntimeConfigService(store_dir=store_dir),
        audit_service=AuditService(audit_repo),
    )
    return service, audit_repo


def _snapshot_with(
    quarterly: bool, fiscal_year_end_month: int | None, config: AppConfig = _CFG_HIGH
) -> StockSnapshot:
    """mock providerのsnapshotの財務内容(=採点の入力・coverage)はそのままに、財務鮮度の判定に
    使う項目(期間末・四半期・決算月)だけを差し替える。

    財務データ全体を差し替えるとcoverageが変わり、HIGHに到達しなくなる(採点の入力が変わる)。
    """
    snapshot, error = build_stock_snapshot(_PROVIDERS, _MOCK_STOCK_CODE, _STALE_NOW, config)
    assert snapshot is not None, error
    variant = _financial(quarterly=quarterly, fiscal_year_end_month=fiscal_year_end_month)
    financial = snapshot.financial.model_copy(
        update={
            "fiscal_period_end": variant.fiscal_period_end,
            "fiscal_year_end_month": variant.fiscal_year_end_month,
            "recent_quarters": variant.recent_quarters,
            "recent_periods_source": variant.recent_periods_source,
        }
    )
    return dataclasses.replace(snapshot, financial=financial)


def _evaluate(
    store_dir: Path, verdict: str, config: AppConfig = _CFG_HIGH
) -> tuple[HoldingDecisionResult, AuditLogRepository, StockSnapshot, dt.datetime]:
    quarterly, fy_month, now, _ = _VERDICT_CASES[verdict]
    # verdictごとに独立した保存先を使う(同じ保存先だと、2回目以降の評価は「初回評価」ではなくなり、
    # baseline比較の有無でcoverageが変わって、FRESH / STALEの比較が成り立たない)。
    isolated_dir = store_dir / f"evaluation-{verdict.lower()}"
    isolated_dir.mkdir(parents=True, exist_ok=True)
    service, audit_repo = _service(isolated_dir, config)
    snapshot = _snapshot_with(quarterly, fy_month, config)
    outcome = service.evaluate(
        _holding(), now, ExecutionPlanReason.NORMAL_SHADOW, snapshot=snapshot
    )
    assert outcome.result is not None
    return outcome.result, audit_repo, snapshot, now


def test_control_verdict_fixtures_produce_the_intended_verdicts() -> None:
    """基準の前提: 3つの財務データが、共通の判定でFRESH / STALE / UNKNOWNになる。"""
    for name, (quarterly, fy_month, now, is_stale) in _VERDICT_CASES.items():
        assessment = assess_financial_freshness(
            _snapshot_with(quarterly, fy_month).financial, now, _CFG
        )
        assert assessment.result.verdict.value == name
        assert assessment.is_stale is is_stale


def test_t1_t2_t6_service_confidence_per_verdict(store_dir: Path) -> None:
    """T1・T2・T6(service): FRESH / UNKNOWN はHIGHのまま、STALEだけがMEDIUMになる。"""
    results = {name: _evaluate(store_dir, name)[0] for name in _VERDICT_CASES}

    assert results["FRESH"].confidence == _HIGH
    assert results["UNKNOWN"].confidence == _HIGH  # UNKNOWNをSTALE扱いしない
    assert results["STALE"].confidence == _MEDIUM


def test_t7_to_t10_service_result_differs_only_in_confidence_between_fresh_and_stale(
    store_dir: Path,
) -> None:
    """T7〜T10(service): 同じ入力でFRESHとSTALEを切り替えても、confidence以外の結果が一致する。

    永続される HoldingDecisionResult の score・coverage・gate・通知可否・理由が同一であること
    (=persistence schemaに新しいfieldを足しておらず、値も変えていない)。
    """
    fresh, *_ = _evaluate(store_dir, "FRESH")
    stale, *_ = _evaluate(store_dir, "STALE")

    ignored = {"holding_decision_result_id", "evaluation_duration_ms", "evaluated_at"}
    fresh_dump = {k: v for k, v in fresh.model_dump().items() if k not in ignored}
    stale_dump = {k: v for k, v in stale.model_dump().items() if k not in ignored}
    assert fresh_dump.pop("confidence") == _HIGH
    assert stale_dump.pop("confidence") == _MEDIUM
    assert stale_dump == fresh_dump
    # 永続schemaに財務鮮度のfieldが追加されていない。
    assert not [
        name for name in HoldingDecisionResult.model_fields if "fresh" in name or "stale" in name
    ]


def test_x1_deadline_boundary_previous_day_is_fresh_and_deadline_day_is_stale(
    store_dir: Path,
) -> None:
    """T-X1(時刻境界 C-BS): 報告期限の前日=FRESH(HIGH維持)/ 当日=STALE(MEDIUM)。"""
    fresh, *_ = _evaluate(store_dir, "FRESH")
    stale, *_ = _evaluate(store_dir, "STALE")

    assert (_FRESH_NOW.date(), _STALE_NOW.date()) == (dt.date(2026, 8, 18), dt.date(2026, 8, 19))
    assert fresh.confidence == _HIGH
    assert stale.confidence == _MEDIUM


def test_x1_evaluation_date_is_resolved_in_jst(store_dir: Path) -> None:
    """T-X1: 前日23:00 UTC(= 期限当日 08:00 JST)は、JST基準で期限当日 = STALE。

    定期実行(08:00 JST)は前日のUTC暦日に当たる。評価日をUTC暦日で見ると期限前日と
    誤判定してHIGHが残る(#52 / #66 の日付境界)。共通のassessがJSTで解決していることの固定。
    """
    utc_evening = dt.datetime(2026, 8, 18, 23, 0, tzinfo=dt.UTC)  # = 2026-08-19 08:00 JST
    service, _ = _service(store_dir)
    snapshot = _snapshot_with(True, 3)

    outcome = service.evaluate(
        _holding(), utc_evening, ExecutionPlanReason.NORMAL_SHADOW, snapshot=snapshot
    )

    assert outcome.result is not None
    assert outcome.result.confidence == _MEDIUM


def test_x5_audit_records_the_shared_freshness_items_and_final_confidence(
    store_dir: Path,
) -> None:
    """T-X5: 監査へ、SELL・利確と同一の10項目と最終confidenceが記録される。"""
    expected_keys = set(
        assess_financial_freshness(_financial(quarterly=True), _STALE_NOW, _CFG).audit_values(_CFG)
    )
    assert len(expected_keys) == 10

    for name in _VERDICT_CASES:
        result, audit_repo, snapshot, now = _evaluate(store_dir, name)
        entries = [e for e in audit_repo.list_by_decision_type("holding_decision")]
        output = entries[-1].output_values
        shared = assess_financial_freshness(snapshot.financial, now, _CFG).audit_values(_CFG)

        assert expected_keys <= set(output), name
        assert {k: output[k] for k in expected_keys} == shared, name
        assert output["confidence"] == result.confidence.value, name
        assert output["financial_freshness_verdict"] == name
        assert output["financial_freshness_warning"] is (name == "STALE")
        assert output["financial_stale_high_confidence_disallowed"] is (name == "STALE")


def test_x7_data_error_path_does_not_evaluate_freshness(store_dir: Path) -> None:
    """T-X7: データが取得できない銘柄では、財務鮮度を評価せず従来と同じ結果(データエラー)を返す。"""
    service, audit_repo = _service(store_dir)

    outcome = service.evaluate(
        _holding("7203"), _STALE_NOW, ExecutionPlanReason.NORMAL_SHADOW
    )  # mock providerがデータを返さない銘柄

    assert outcome.result is None
    assert outcome.data_error is not None
    entries = list(audit_repo.list_by_decision_type("holding_decision"))
    assert not any("financial_freshness_verdict" in e.output_values for e in entries)


def test_t11_no_holding_decision_specific_reporting_lag_or_threshold_exists() -> None:
    """T11: 保有判断専用の猶予日数・閾値が存在しない(既存の共通configを使う)。

    (a) 猶予日数を変えると、保有判断の結果(cap)が既存のキーだけで変わる。
    (b) 保有判断のconfigとソースに、財務の猶予日数・報告ラグの独自定義が無い。
    """
    lag = _CFG.screening.data_quality.financial_reporting_lag_calendar_days
    # (a) 期限当日(50日後)がSTALEになる猶予を、さらに1日延ばすとFRESHになる(共通キーの効果)。
    financial = _financial(quarterly=True, fiscal_year_end_month=3)
    longer = _CFG.model_copy(
        update={
            "screening": _CFG.screening.model_copy(
                update={
                    "data_quality": _CFG.screening.data_quality.model_copy(
                        update={"financial_reporting_lag_calendar_days": lag + 1}
                    )
                }
            )
        }
    )
    assert assess_financial_freshness(financial, _STALE_NOW, _CFG).is_stale is True
    assert assess_financial_freshness(financial, _STALE_NOW, longer).is_stale is False

    # (b) 保有判断のconfigに、財務の報告ラグ・鮮度の日数を表すfieldが無い。
    holding_fields = set(type(_RULES).model_fields) | set(
        type(_CFG.holding_decision_ratio).model_fields
    )
    assert not [f for f in holding_fields if "reporting_lag" in f or "financial_stale" in f]
    # 保有判断側のソースが、猶予日数のキーを独自に定義していない(読むだけ)。
    for relative in (
        "services/holding_decision_service.py",
        "services/holding_decision_notification_builder.py",
        "domain/signals/holding_decision_score.py",
    ):
        text = (_SRC / relative).read_text(encoding="utf-8")
        assert not re.search(r"reporting_lag[_a-z]*\s*=", text), relative
        assert "financial_reporting_lag_calendar_days" not in text, relative


def _calls_and_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
        if isinstance(node, ast.Name | ast.Attribute):
            names.add(node.attr if isinstance(node, ast.Attribute) else node.id)
    return names


def test_t12_freshness_is_decided_only_by_the_existing_shared_functions() -> None:
    """T12: 新しいSTALE判定の実装が無く、既存の共通部品(assess)だけを使う。

    保有判断側のソースは、STALEの判定に FinancialFreshnessVerdict を直接比較せず、
    evaluate_financial_freshness も呼ばない(共通のassess_financial_freshnessのみ)。
    """
    for relative in (
        "services/holding_decision_service.py",
        "services/holding_decision_notification_builder.py",
    ):
        names = _calls_and_names(_SRC / relative)
        assert "assess_financial_freshness" in names, relative
        assert "evaluate_financial_freshness" not in names, relative
        assert "FinancialFreshnessVerdict" not in names, relative
        assert "STALE" not in names, relative
    # 純関数側は判定を持たず、真偽値(financial_stale)を受け取るだけ。
    score_names = _calls_and_names(_SRC / "domain/signals/holding_decision_score.py")
    assert "assess_financial_freshness" not in score_names
    assert "evaluate_financial_freshness" not in score_names


# ---------------------------------------------------------------------------
# C. builder + LINE本文(W2)
# ---------------------------------------------------------------------------

_NOTIFY_NOW = _STALE_NOW
_EXIT_RANGE = ExitPriceRangeResult(
    state=PriceRangeEvaluationState.NOT_EVALUATED,
    current_price=Decimal("1000"),
    evaluated_at=_NOTIFY_NOW,
    model_version="exit_price_range_v1",
)


def _decision_result(
    now: dt.datetime, confidence: HoldingDecisionConfidenceLevel = _HIGH
) -> HoldingDecisionResult:
    return HoldingDecisionResult(
        holding_decision_result_id="issue-468-test",
        holding_id=build_holding_id(DEFAULT_OWNER, _MOCK_STOCK_CODE),
        stock_code=_MOCK_STOCK_CODE,
        evaluated_at=now,
        company_quality=CompanyQualityScore(score=30.0, coverage_ratio=1.0),
        investment_thesis=InvestmentThesisScore(score=25.0, coverage_ratio=1.0),
        risk_deduction=RiskDeductionScore(score=10.0, coverage_ratio=1.0),
        base_score=45.0,
        hard_gate=HoldingDecisionHardGate(triggered=False),
        final_score=45.0,
        display_value=45,
        category=HoldingDecisionCategory.SELL_CONSIDERATION,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=confidence,
        should_notify=True,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
    )


def _recommendation(verdict: str) -> Recommendation:
    quarterly, fy_month, now, _ = _VERDICT_CASES[verdict]
    snapshot = _snapshot_with(quarterly, fy_month)
    return build_holding_decision_recommendation(
        _holding(),
        _decision_result(now),
        snapshot,
        "rule-v1",
        _CFG,
        _EXIT_RANGE,
        recommendation_id="rec-issue-468",  # 本文の通知IDを固定する(比較のため)
    )


def test_x3_warning_is_stored_in_key_risks_only_when_stale() -> None:
    """T-X3: 警告(既存の共通定数)は STALE のときだけ key_risks へ入る(FRESH・UNKNOWNは空)。

    cap判定(service)と警告(builder)は同じ共通関数・同じ入力(評価時刻)を使うため、
    「STALE ⇔ 警告あり」が全verdictで成り立つ。文言は新しく作らない。
    """
    for name, (_, _, _, is_stale) in _VERDICT_CASES.items():
        recommendation = _recommendation(name)
        expected = [FINANCIAL_STALE_USER_WARNING] if is_stale else []
        assert recommendation.key_risks == expected, name


def test_x3_warning_is_not_mixed_into_other_sections() -> None:
    """警告は reasons・counter_factors・holding_risks・次の判断条件へ混ぜない。"""
    recommendation = _recommendation("STALE")

    for field in ("reasons", "counter_factors", "holding_risks", "next_review_conditions"):
        assert FINANCIAL_STALE_USER_WARNING not in getattr(recommendation, field), field
    assert FINANCIAL_STALE_USER_WARNING not in (recommendation.recommended_action_summary or "")


def test_x4_recommendation_differs_only_in_key_risks_between_fresh_and_stale() -> None:
    """T-X4: STALE以外のRecommendationは変更前と同一。STALEとの差は key_risks のみ。"""
    fresh = _recommendation("FRESH")
    unknown = _recommendation("UNKNOWN")
    stale = _recommendation("STALE")

    ignored = {"recommendation_id", "recommended_at"}
    fresh_dump = {k: v for k, v in fresh.model_dump().items() if k not in ignored}
    unknown_dump = {k: v for k, v in unknown.model_dump().items() if k not in ignored}
    stale_dump = {k: v for k, v in stale.model_dump().items() if k not in ignored}
    assert unknown_dump == fresh_dump
    differing = {k for k in fresh_dump if stale_dump[k] != fresh_dump[k]}
    assert differing == {"key_risks"}


def _line_body(recommendation: Recommendation) -> str:
    return line_module._format_holding_decision_message(recommendation)


def test_x6_line_body_shows_the_section_only_when_stale() -> None:
    """T-X6(W2): STALEのとき「留意事項」節に既存の警告が1回だけ出る。他では節も見出しも出ない。"""
    stale_body = _line_body(_recommendation("STALE"))
    assert stale_body.count("留意事項：") == 1
    assert stale_body.count(FINANCIAL_STALE_USER_WARNING) == 1
    assert f"・{FINANCIAL_STALE_USER_WARNING}" in stale_body

    for name in ("FRESH", "UNKNOWN"):
        body = _line_body(_recommendation(name))
        assert "留意事項" not in body, name
        assert FINANCIAL_STALE_USER_WARNING not in body, name


def test_x6_line_body_of_non_stale_is_byte_for_byte_unchanged() -> None:
    """T-X6: STALEでない本文は1バイトも変わらない(key_risks を持たない本文と一致)。

    「空の節の禁止」の固定でもある: 見出しも空行も出さない。
    """
    for name in ("FRESH", "UNKNOWN"):
        recommendation = _recommendation(name)
        assert recommendation.key_risks == []
        body = _line_body(recommendation)
        # key_risks を持たない(=変更前と同じ)Recommendationの本文と一致する。
        legacy = _line_body(recommendation.model_copy(update={"key_risks": []}))
        assert body == legacy
        assert "\n\n\n" not in body  # 空の節が空行だけを残していない


def test_x6_section_position_is_right_before_the_confidence_line() -> None:
    """T-X6: 節は「次の判断条件」の直後・「判定の信頼度」の直前にある。"""
    body = _line_body(_recommendation("STALE"))
    lines = body.split("\n")
    section = lines.index("留意事項：")
    assert lines[section + 1] == f"・{FINANCIAL_STALE_USER_WARNING}"
    assert lines[section + 2] == ""
    assert lines[section + 3].startswith("判定の信頼度：")
    assert lines.index("次の判断条件：") < section


def test_x6_stale_body_differs_from_non_stale_body_only_by_the_section() -> None:
    """STALEの本文は、節(見出し・箇条書き・空行)を除けば、FRESHの本文と同一。"""
    fresh_body = _line_body(_recommendation("FRESH"))
    stale_body = _line_body(_recommendation("STALE"))

    section = f"留意事項：\n・{FINANCIAL_STALE_USER_WARNING}\n\n"
    assert section in stale_body
    assert stale_body.replace(section, "", 1) == fresh_body


# ---------------------------------------------------------------------------
# D. スコープ外の固定(BUY・SELL・利確・保有判断以外は変更しない)
# ---------------------------------------------------------------------------


def test_x8_other_notification_bodies_do_not_render_key_risks() -> None:
    """T-X8: 本Issueは保有判断のLINE本文だけを変える。他の本文関数は key_risks を参照しない。

    SELL・利確・BUYのLINE本文は変更しない(可視性は #474 で別途調査)。
    """
    tree = ast.parse((_SRC / "services/line_notification_service.py").read_text(encoding="utf-8"))
    referrers = []
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if any(isinstance(n, ast.Attribute) and n.attr == "key_risks" for n in ast.walk(function)):
            referrers.append(function.name)
    assert referrers == ["_format_holding_decision_message"]
