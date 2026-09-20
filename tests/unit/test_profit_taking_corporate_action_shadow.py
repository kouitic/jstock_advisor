"""Issue #456(#160 PR-2c): 保有の利確判定へ、企業行動のshadow facts(G4)を供給する。

## 確認する契約(USER決定 U7 / U8、MANAGER判断 D-a〜D-c)

- 企業行動の取得は**既存の1回のみ**(追加のprovider呼び出し = 0)。取得開始日は、shadow modeに依存せず
  `min(profit_protection_basis_date, lookback_start)`へ広げる(単一のコード経路)。
- ★ **既存Profit Protectionの観測窓は`effective_date >= basis_date`のまま**(取得範囲を広げても
  判定対象期間は広げない)。basis_dateより前にだけSPLITがあるケース(T3)が最重要ゲート。
- G4の評価は、shadow mode=SHADOWかつ保有のFULL_PROFIT_TAKEのときだけ、
  `isolated_shadow_computation`の保護下で行う。評価の失敗は本流を変えない(T7)。
  mode=OFFでは`check_split_consistency`を実行しない(T8)。
- shadow OFF / ON で、既存の業務結果(Recommendation・data_error・監査ログ)は完全一致する(T9)。

時刻は固定(`_NOW`)。実時計に依存しない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import CorporateActionType, RecommendationType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.signals.judgment_safety import CorporateActionFacts
from jstock_advisor.domain.signals.judgment_safety_shadow_config import (
    JudgmentSafetyShadowConfig,
    ShadowMode,
)
from jstock_advisor.interfaces.types import CorporateActionEvent
from jstock_advisor.services import profit_taking_service as service_module
from jstock_advisor.services.data_quality_service import (
    DataQualityIssue,
    DataQualityIssueSeverity,
    check_split_consistency,
)
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from tests.unit.test_profit_taking_service import (
    _CONFIG,
    _NOW,
    _TEST_FINANCIAL_SOURCE,
    _canned_result,
    _holding,
    _providers,
)

_SHADOW_ON = JudgmentSafetyShadowConfig(mode=ShadowMode.SHADOW)
_SHADOW_OFF = JudgmentSafetyShadowConfig(mode=ShadowMode.OFF)
_LOOKBACK_YEARS = _CONFIG.data_validation.split_consistency.lookback_years
_LOOKBACK_START = _NOW.date() - dt.timedelta(days=365 * _LOOKBACK_YEARS)
_BASIS_DATE = dt.date(2024, 1, 1)  # _holding(): last_purchase_date(last_sale_dateなし)
_BEFORE_BASIS_WITHIN_LOOKBACK = dt.date(2023, 10, 1)
_AFTER_BASIS = dt.date(2025, 6, 1)
_OUTSIDE_LOOKBACK = _LOOKBACK_START - dt.timedelta(days=30)
_KINDS = [
    "price_discontinuity_unexplained",
    "fair_value_divergence_resembles_split_ratio",
    "dividend_change_resembles_split_ratio",
    "purchase_price_basis_mismatch",
]


def _event(
    effective_date: dt.date,
    event_type: CorporateActionType = CorporateActionType.SPLIT,
) -> CorporateActionEvent:
    return CorporateActionEvent(
        stock_code="2914",
        event_type=event_type,
        announced_date=effective_date - dt.timedelta(days=30),
        effective_date=effective_date,
        ratio=Decimal("2") if event_type is CorporateActionType.SPLIT else Decimal("0.5"),
        source=_TEST_FINANCIAL_SOURCE,
    )


class _RecordingProvider:
    """実providerと同じく`since`より古いeventsを返さない。呼び出し(stock_code, since)を記録する。"""

    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events
        self.calls: list[tuple[str, dt.date]] = []

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        self.calls.append((stock_code, since))
        return [e for e in self._events if e.effective_date is None or e.effective_date >= since]


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> Any:
        self.records.append(kwargs)
        return SimpleNamespace(audit_id="audit-1")


def _make_service(
    monkeypatch: pytest.MonkeyPatch,
    provider: _RecordingProvider,
    shadow: JudgmentSafetyShadowConfig,
    recommendation_type: RecommendationType = RecommendationType.FULL_PROFIT_TAKE,
) -> tuple[ProfitTakingService, _RecordingAudit]:
    canned = _canned_result(recommendation_type)
    monkeypatch.setattr(service_module, "evaluate_profit_taking", lambda **kw: canned)
    providers = dataclasses.replace(
        _providers(None, dt.date(2026, 6, 30)), corporate_action=provider
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG, shadow_config=shadow)
    audit = _RecordingAudit()
    monkeypatch.setattr(service, "_audit", audit)
    return service, audit


def _capture_g4_inputs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """G4が`check_split_consistency`へ渡した引数を記録する(結果は実物のまま)。"""
    captured: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> list[DataQualityIssue]:
        captured.append(kwargs)
        return check_split_consistency(**kwargs)

    monkeypatch.setattr(service_module, "check_split_consistency", _spy)
    return captured


def _normalized(outcome: Any, audit: _RecordingAudit) -> dict[str, Any]:
    """変更前後で一致すべき既存の業務結果(毎回変わるIDを除く)。"""
    rec = outcome.recommendation.model_dump(mode="json") if outcome.recommendation else None
    if rec is not None:
        rec.pop("recommendation_id", None)
    records = [{k: v for k, v in r.items() if k not in {"audit_id"}} for r in audit.records]
    return {"recommendation": rec, "data_error": outcome.data_error, "audit": records}


# ============================================================================
# T1 / T2: 取得は1回・取得開始日は min(basis_date, lookback_start)(mode非依存)
# ============================================================================


@pytest.mark.parametrize("shadow", [_SHADOW_ON, _SHADOW_OFF], ids=["shadow_on", "shadow_off"])
def test_t1_the_provider_is_called_exactly_once_per_holding(
    monkeypatch: pytest.MonkeyPatch, shadow: JudgmentSafetyShadowConfig
) -> None:
    provider = _RecordingProvider([_event(_AFTER_BASIS)])
    service, _ = _make_service(monkeypatch, provider, shadow)

    service.analyze(_holding("2914"), _NOW)

    assert [c[0] for c in provider.calls] == ["2914"]  # 1銘柄あたり1回


@pytest.mark.parametrize("shadow", [_SHADOW_ON, _SHADOW_OFF], ids=["shadow_on", "shadow_off"])
def test_t2_since_is_the_earlier_of_basis_date_and_lookback_start_regardless_of_mode(
    monkeypatch: pytest.MonkeyPatch, shadow: JudgmentSafetyShadowConfig
) -> None:
    provider = _RecordingProvider([])
    service, _ = _make_service(monkeypatch, provider, shadow)

    service.analyze(_holding("2914"), _NOW)

    assert provider.calls == [("2914", min(_BASIS_DATE, _LOOKBACK_START))]
    assert _LOOKBACK_START < _BASIS_DATE  # この例では、lookback_startの方が古い(取得を広げる側)


def test_t2_a_basis_date_older_than_the_lookback_start_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保有基準日がlookback_startより古い場合は、基準日(従来の取得開始日)のまま(狭めない)。"""
    old = _holding("2914").model_copy(
        update={
            "first_purchase_date": dt.date(2020, 1, 1),
            "last_purchase_date": dt.date(2020, 1, 1),
        }
    )
    provider = _RecordingProvider([])
    service, _ = _make_service(monkeypatch, provider, _SHADOW_ON)

    service.analyze(old, _NOW)

    assert provider.calls == [("2914", dt.date(2020, 1, 1))]


@pytest.mark.parametrize("now_offset_days", [0, 1, 400])
@pytest.mark.parametrize("years", [1, 3, 5])
def test_u9_lookback_start_matches_check_split_consistency_boundary(
    monkeypatch: pytest.MonkeyPatch, now_offset_days: int, years: int
) -> None:
    """U9: 取得開始日の`lookback_start`は、`check_split_consistency`が内部で使う境界と同じ。

    検査は「lookback_start以降の分割」だけを既知の分割として扱う。境界当日は既知(issueなし)、
    前日は既知でない(適正価格乖離のissueが出る)ことで、式の一致を固定する。
    """
    cfg = _CONFIG.model_copy(
        update={
            "data_validation": _CONFIG.data_validation.model_copy(
                update={
                    "split_consistency": _CONFIG.data_validation.split_consistency.model_copy(
                        update={"lookback_years": years}
                    )
                }
            )
        }
    )
    now = _NOW + dt.timedelta(days=now_offset_days)
    service = ProfitTakingService(
        providers=_providers(None, dt.date(2026, 6, 30)), config=cfg, shadow_config=_SHADOW_ON
    )
    start = service._split_consistency_lookback_start(now)

    def _issues(event_date: dt.date) -> list[str]:
        return [
            i.check_name
            for i in check_split_consistency(
                stock_code="2914",
                current_price=Decimal("200"),
                bars_close_by_date=[],
                fair_value=Decimal("100"),  # 乖離2倍 = 典型的な分割比率
                actual_annual_dividend_per_share=None,
                previous_fiscal_year_dividend_per_share=None,
                corporate_action_events=[_event(event_date)],
                holding=None,
                now=now,
                config=cfg.data_validation.split_consistency,
            )
        ]

    assert _issues(start) == []  # 境界当日は既知の分割として扱われる
    assert _issues(start - dt.timedelta(days=1)) == ["fair_value_divergence_resembles_split_ratio"]


# ============================================================================
# T3〜T6: 既存Profit Protectionの観測窓は不変・G4には広い窓のeventsが渡る
# ============================================================================


def _protection_metrics(
    monkeypatch: pytest.MonkeyPatch, event: CorporateActionEvent
) -> tuple[Any, list[dict[str, Any]]]:
    """analyzeを通し、Profit Protectionの指標(実物)とG4への入力を取り出す。"""
    provider = _RecordingProvider([event])
    service, _ = _make_service(monkeypatch, provider, _SHADOW_ON)
    captured = _capture_g4_inputs(monkeypatch)
    holding = _holding("2914")
    providers = service._providers
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None
    events = service._fetch_corporate_action_events(holding, _NOW)
    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW, events=events)
    service.analyze(holding, _NOW)
    return metrics, captured


@pytest.mark.parametrize(
    "event_type", [CorporateActionType.SPLIT, CorporateActionType.REVERSE_SPLIT]
)
def test_t3_t5_an_event_before_basis_date_reaches_g4_but_not_the_existing_protection(
    monkeypatch: pytest.MonkeyPatch, event_type: CorporateActionType
) -> None:
    """★ 最重要ゲート: basis_dateより前(かつlookback内)のeventは、G4へ渡るが、既存判定は従来どおり
    「基準日以降の分割なし」(`ratio_adjustment_event_since_basis`はFalse = データ不足にならない)。
    """
    event = _event(_BEFORE_BASIS_WITHIN_LOOKBACK, event_type)

    metrics, captured = _protection_metrics(monkeypatch, event)

    assert metrics.insufficient_data_reason is None  # 既存判定は基準日より前のeventで妨げられない
    assert [e.effective_date for e in captured[-1]["corporate_action_events"]] == [
        _BEFORE_BASIS_WITHIN_LOOKBACK
    ]  # G4には広い窓のeventが渡る


@pytest.mark.parametrize(
    "event_type", [CorporateActionType.SPLIT, CorporateActionType.REVERSE_SPLIT]
)
def test_t4_t5_an_event_on_or_after_basis_date_is_detected_by_both(
    monkeypatch: pytest.MonkeyPatch, event_type: CorporateActionType
) -> None:
    event = _event(_AFTER_BASIS, event_type)

    metrics, captured = _protection_metrics(monkeypatch, event)

    assert metrics.insufficient_data_reason is not None  # 既存は従来どおり検知(データ不足)
    assert [e.effective_date for e in captured[-1]["corporate_action_events"]] == [_AFTER_BASIS]


def test_t3_the_boundary_day_itself_is_still_since_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """basis_date当日のeventは「基準日以降」(従来どおり)。前日は「基準日より前」。"""
    on_basis, _ = _protection_metrics(monkeypatch, _event(_BASIS_DATE))
    day_before, _ = _protection_metrics(monkeypatch, _event(_BASIS_DATE - dt.timedelta(days=1)))

    assert on_basis.insufficient_data_reason is not None
    assert day_before.insufficient_data_reason is None


def test_t6_g4_receives_the_events_within_the_lookback_and_the_callee_owns_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """渡す側はlookback内のeventsを渡し(=取得の`since`)、lookback外の切り捨ては渡す側で行わない。"""
    provider = _RecordingProvider(
        [
            _event(_OUTSIDE_LOOKBACK),
            _event(_BEFORE_BASIS_WITHIN_LOOKBACK),
            _event(_AFTER_BASIS),
        ]
    )
    service, _ = _make_service(monkeypatch, provider, _SHADOW_ON)
    captured = _capture_g4_inputs(monkeypatch)

    service.analyze(_holding("2914"), _NOW)

    dates = [e.effective_date for e in captured[-1]["corporate_action_events"]]
    assert dates == [
        _BEFORE_BASIS_WITHIN_LOOKBACK,
        _AFTER_BASIS,
    ]  # since(=lookback_start)で取得済み


# ============================================================================
# G4の写像・対象範囲
# ============================================================================


def _stub_issues(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    issues = [
        DataQualityIssue(
            check_name=n,
            severity=DataQualityIssueSeverity.BLOCKING,
            description="x",
            affected_fields=[],
            suppressed_values={},
        )
        for n in names
    ]
    monkeypatch.setattr(service_module, "check_split_consistency", lambda **kw: issues)


def test_g4_success_maps_the_check_names_the_inspection_actually_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _make_service(monkeypatch, _RecordingProvider([]), _SHADOW_ON)
    _stub_issues(monkeypatch, _KINDS)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.safety_facts is not None
    assert outcome.safety_facts.corporate_action == CorporateActionFacts(
        "EVALUATED",
        tuple(_KINDS),  # type: ignore[arg-type]
    )


def test_g4_no_issue_is_evaluated_with_nothing_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _make_service(monkeypatch, _RecordingProvider([]), _SHADOW_ON)
    _stub_issues(monkeypatch, [])

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.safety_facts is not None
    assert outcome.safety_facts.corporate_action == CorporateActionFacts("EVALUATED", ())


def test_d_a_an_unknown_check_name_is_computation_failed_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _make_service(monkeypatch, _RecordingProvider([]), _SHADOW_ON)
    _stub_issues(monkeypatch, [_KINDS[0], "a_future_check_added_later"])

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.safety_facts is not None
    assert outcome.safety_facts.corporate_action == CorporateActionFacts("COMPUTATION_FAILED")


@pytest.mark.parametrize(
    "rtype",
    [
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.REVIEW_AFTER_EARNINGS,
    ],
)
def test_d_c_only_full_profit_take_is_evaluated(
    monkeypatch: pytest.MonkeyPatch, rtype: RecommendationType
) -> None:
    service, _ = _make_service(monkeypatch, _RecordingProvider([]), _SHADOW_ON, rtype)
    monkeypatch.setattr(
        service_module,
        "check_split_consistency",
        lambda **kw: (_ for _ in ()).throw(AssertionError("G4 must not run for non-strong types")),
    )

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.recommendation is not None
    assert outcome.safety_facts is not None
    assert outcome.safety_facts.corporate_action is None


# ============================================================================
# T7: G4の失敗は本流を変えない
# ============================================================================


def _baseline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """shadow OFFでの既存の業務結果(比較の基準)。"""
    service, audit = _make_service(
        monkeypatch, _RecordingProvider([_event(_AFTER_BASIS)]), _SHADOW_OFF
    )
    return _normalized(service.analyze(_holding("2914"), _NOW), audit)


@pytest.mark.parametrize(
    "boom",
    [
        pytest.param(lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")), id="raises"),
        pytest.param(
            lambda **kw: [
                DataQualityIssue(
                    check_name="unknown_check",
                    severity=DataQualityIssueSeverity.BLOCKING,
                    description="x",
                    affected_fields=[],
                    suppressed_values={},
                )
            ],
            id="unknown_check_name",
        ),
    ],
)
def test_t7_a_failing_g4_never_changes_the_existing_business_result(
    monkeypatch: pytest.MonkeyPatch, boom: Callable[..., Any]
) -> None:
    expected = _baseline(monkeypatch)
    service, audit = _make_service(
        monkeypatch, _RecordingProvider([_event(_AFTER_BASIS)]), _SHADOW_ON
    )
    monkeypatch.setattr(service_module, "check_split_consistency", boom)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert _normalized(outcome, audit) == expected
    assert outcome.safety_facts is not None
    assert outcome.safety_facts.corporate_action == CorporateActionFacts("COMPUTATION_FAILED")


def test_t7_the_failure_handler_itself_cannot_raise() -> None:
    """`on_failure`は定数構築のみ(S-20は`on_failure`自身の例外を保護しない)。"""
    assert CorporateActionFacts("COMPUTATION_FAILED") == CorporateActionFacts(
        "COMPUTATION_FAILED", ()
    )


# ============================================================================
# T8: mode=OFFではG4を評価しない(既存の取得は従来どおり)
# ============================================================================


def test_t8_shadow_off_never_runs_the_inspection_and_still_fetches_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _RecordingProvider([_event(_AFTER_BASIS)])
    service, _ = _make_service(monkeypatch, provider, _SHADOW_OFF)
    monkeypatch.setattr(
        service_module,
        "check_split_consistency",
        lambda **kw: (_ for _ in ()).throw(AssertionError("G4 must not run when shadow is OFF")),
    )

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.recommendation is not None
    assert outcome.safety_facts is not None
    assert outcome.safety_facts.corporate_action is None
    assert len(provider.calls) == 1  # OFFにしても既存に必要な取得は止めない


def test_t8_the_default_shadow_mode_from_the_shipped_config_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """コンストラクタ既定(shadow_config未指定)は、出荷configのmode=OFFを読む。"""
    monkeypatch.setattr(
        service_module,
        "evaluate_profit_taking",
        lambda **kw: _canned_result(RecommendationType.FULL_PROFIT_TAKE),
    )
    service = ProfitTakingService(providers=_providers(None, dt.date(2026, 6, 30)), config=_CONFIG)

    assert service._shadow_config.enabled is False


# ============================================================================
# T9: golden(shadow OFF / ON で、既存の業務結果は完全一致。差分は corporate_action のみ)
# ============================================================================


@pytest.mark.parametrize("event_date", [_AFTER_BASIS, _BEFORE_BASIS_WITHIN_LOOKBACK, None])
def test_t9_existing_results_are_identical_with_and_without_shadow(
    monkeypatch: pytest.MonkeyPatch, event_date: dt.date | None
) -> None:
    events = [_event(event_date)] if event_date is not None else []
    off_service, off_audit = _make_service(monkeypatch, _RecordingProvider(events), _SHADOW_OFF)
    off = off_service.analyze(_holding("2914"), _NOW)
    on_service, on_audit = _make_service(monkeypatch, _RecordingProvider(events), _SHADOW_ON)
    on = on_service.analyze(_holding("2914"), _NOW)

    assert _normalized(on, on_audit) == _normalized(off, off_audit)
    assert off.safety_facts is not None
    assert on.safety_facts is not None
    assert off.safety_facts.corporate_action is None
    assert on.safety_facts.corporate_action is not None
    # 許可する差分は corporate_action のみ
    assert dataclasses.replace(on.safety_facts, corporate_action=None) == off.safety_facts


def test_the_recommendation_never_carries_the_shadow_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _make_service(monkeypatch, _RecordingProvider([]), _SHADOW_ON)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.recommendation is not None
    assert not hasattr(outcome.recommendation, "safety_facts")
    assert "corporate_action" not in outcome.recommendation.model_dump()


def test_holding_fixture_sanity(monkeypatch: pytest.MonkeyPatch) -> None:
    """前提の固定: _holdingの基準日は2024-01-01で、lookback_startはそれより古い。"""
    holding: Holding = _holding("2914")

    assert holding.last_purchase_date == _BASIS_DATE
    assert _LOOKBACK_START < _BASIS_DATE
    assert _OUTSIDE_LOOKBACK < _LOOKBACK_START <= _BEFORE_BASIS_WITHIN_LOOKBACK < _BASIS_DATE
