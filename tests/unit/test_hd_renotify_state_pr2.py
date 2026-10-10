"""再通知の比較に使う『前回の状態』の記録(Issue #890 PR-2)のテスト。

固定するもの
  (1) 確認状態の分類(理由コード → GateConfirmation)。純粋・決定的・例外を出さない
  (2) builder が記録する hd_renotify_state(版 1。PR-1 の形式)の中身
  (3) 記録の失敗が通知の記録(Recommendation)の構築を止めない
  (4) 評価(HoldingDecisionService)が返す確認状態: 開示キーワード由来は、確認の段階から導く
  (5) 検証モード(SHADOW)では記録が作られない / 通常運用(ACTIVE)では作られる(handler 通し)
  (6) 旧形式の記録(キー無し)は『比べられない』になる

既存の出力が変わらないこと(characterization)は test_hd_renotify_state_characterization.py。
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import (
    ExecutionPlanReason,
    HoldingDecisionCategory,
    RecommendationType,
    RuntimeConfigMode,
)
from jstock_advisor.domain.entities.holding_decision import (
    HoldingDecisionHardGate,
    HoldingDecisionResult,
)
from jstock_advisor.domain.signals import holding_decision_gate_confirmation as gate_module
from jstock_advisor.domain.signals.holding_decision_gate_confirmation import (
    classify_gate_confirmations,
    disclosure_levels,
)
from jstock_advisor.domain.signals.holding_decision_renotification import (
    HD_RENOTIFY_STATE_KEY,
    EarningsDataFreshness,
    GateConfirmation,
    HdNotifyState,
    Reason,
    StateUnavailable,
    extract_hd_state,
)
from jstock_advisor.services import holding_decision_notification_builder as builder_module
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from tests.unit.test_holding_decision_service_audit_fields import (
    _CFG as _SERVICE_CFG,
)
from tests.unit.test_holding_decision_service_audit_fields import (
    _NOW as _SERVICE_NOW,
)
from tests.unit.test_holding_decision_service_audit_fields import (
    _PROVIDERS as _SERVICE_PROVIDERS,
)
from tests.unit.test_holding_decision_service_audit_fields import (
    _holding as _service_holding,
)
from tests.unit.test_holding_decision_service_audit_fields import (
    _service,
)
from tests.unit.test_holdings_watchlist_handler_integration import (
    _build_services,
    _notifying_holding_decision_result,
    _run,
)
from tests.unit.test_issue_67_recommendation_provenance_transfer import (
    _CONFIG,
    _NOT_EVALUATED_EXIT_PRICE_RANGE,
    _base_snapshot,
    _holding,
    _holding_decision_result,
    _register_fictional_stock,  # noqa: F401 - autouse fixture
)

_MATERIAL = "MATERIAL_EVENT_CONFIRMED"
_KEYWORD = "RISK_KEYWORD_DETECTED"


# ===========================================================================
# (1) 確認状態の分類
# ===========================================================================


@pytest.mark.parametrize(
    ("code", "rule", "level", "expected"),
    [
        ("BANKRUPTCY_FILING", "major_scandal", _MATERIAL, GateConfirmation.CONFIRMED),
        ("BANKRUPTCY_FILING", "major_scandal", _KEYWORD, GateConfirmation.KEYWORD_ONLY),
        ("DELISTING_OR_KANRI", "listing_maintenance_risk", _MATERIAL, GateConfirmation.CONFIRMED),
        ("DELISTING_OR_KANRI", "listing_maintenance_risk", _KEYWORD, GateConfirmation.KEYWORD_ONLY),
        ("ACCOUNTING_FRAUD", "accounting_problem", _MATERIAL, GateConfirmation.CONFIRMED),
        ("ACCOUNTING_FRAUD", "accounting_problem", _KEYWORD, GateConfirmation.KEYWORD_ONLY),
    ],
)
def test_disclosure_derived_codes_follow_the_confirmation_level(
    code: str, rule: str, level: str, expected: GateConfirmation
) -> None:
    assert classify_gate_confirmations([code], {rule: level}) == ((code, expected.value),)


@pytest.mark.parametrize("level", [None, "", "NONE", "SOMETHING_NEW", 123])
@pytest.mark.parametrize("code", ["BANKRUPTCY_FILING", "DELISTING_OR_KANRI", "ACCOUNTING_FRAUD"])
def test_unreadable_confirmation_level_falls_to_keyword_only(code: str, level: object) -> None:
    """キーワード一致だけで『確認済み』にしない(USER 指定): 段階が読めないときは数えない側。"""
    rule = gate_module.DISCLOSURE_RULE_BY_REASON_CODE[code]
    assert classify_gate_confirmations([code], {rule: level}) == (  # type: ignore[dict-item]
        (code, GateConfirmation.KEYWORD_ONLY.value),
    )
    assert classify_gate_confirmations([code], {}) == ((code, GateConfirmation.KEYWORD_ONLY.value),)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("DEBT_EXCESS", GateConfirmation.CONFIRMED),
        ("GOING_CONCERN_DOUBT", GateConfirmation.CONFIRMED),
        ("DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS", GateConfirmation.CONFIRMED),
        ("INVESTMENT_THESIS_COLLAPSE", GateConfirmation.BASELINE_CONFIRMED),
        ("A_CODE_NOBODY_KNOWS", GateConfirmation.UNVERIFIED),
    ],
)
def test_flag_baseline_and_unknown_codes(code: str, expected: GateConfirmation) -> None:
    assert classify_gate_confirmations([code], {}) == ((code, expected.value),)


def test_classification_is_sorted_deduplicated_and_deterministic() -> None:
    codes = ["INVESTMENT_THESIS_COLLAPSE", "DEBT_EXCESS", "DEBT_EXCESS", "BANKRUPTCY_FILING"]
    first = classify_gate_confirmations(codes, {"major_scandal": _MATERIAL})
    assert [code for code, _ in first] == [
        "BANKRUPTCY_FILING",
        "DEBT_EXCESS",
        "INVESTMENT_THESIS_COLLAPSE",
    ]
    assert classify_gate_confirmations(reversed(codes), {"major_scandal": _MATERIAL}) == first


def test_every_hard_gate_reason_code_is_classified() -> None:
    """hard gate が付けうる全ての理由コードに、明示の規則がある(UNVERIFIED に落ちない)。"""
    from jstock_advisor.domain.signals.holding_decision_hard_gate import _REASON_LABELS

    for code in _REASON_LABELS:
        ((_, confirmation),) = classify_gate_confirmations([code], {})
        assert confirmation != GateConfirmation.UNVERIFIED.value, code


def test_no_codes_means_no_confirmations() -> None:
    assert classify_gate_confirmations([], {"major_scandal": _MATERIAL}) == ()


class _Evaluation:
    def __init__(self, metric_name: str | None, current_value: object) -> None:
        self.metric_name = metric_name
        self.current_value = current_value


def test_disclosure_levels_reads_only_the_confirmation_level_metric() -> None:
    evaluations = {
        "major_scandal": _Evaluation("disclosure_risk_confirmation_level", _MATERIAL),
        "accounting_problem": _Evaluation("disclosure_risk_confirmation_level", _KEYWORD),
        "listing_maintenance_risk": _Evaluation("some_other_metric", _MATERIAL),
        "dividend_omission": _Evaluation("disclosure_risk_confirmation_level", _MATERIAL),
    }
    assert disclosure_levels(evaluations) == {
        "major_scandal": _MATERIAL,
        "accounting_problem": _KEYWORD,
    }
    assert disclosure_levels({}) == {}
    assert disclosure_levels(
        {"major_scandal": _Evaluation("disclosure_risk_confirmation_level", 1)}
    ) == {"major_scandal": None}


# ===========================================================================
# (2) builder が記録する hd_renotify_state
# ===========================================================================


def _recommendation(
    result: HoldingDecisionResult | None = None,
    gate_confirmations: tuple[tuple[str, str], ...] = (),
    snapshot: Any = None,
) -> Any:
    return build_holding_decision_recommendation(
        _holding(),
        result if result is not None else _holding_decision_result(),
        snapshot if snapshot is not None else _base_snapshot(),
        "rule-v1",
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
        recommendation_id="pr2-test",
        gate_confirmations=gate_confirmations,
    )


def _state_of(recommendation: Any) -> HdNotifyState:
    extracted = extract_hd_state(recommendation.config_values_used)
    assert isinstance(extracted, HdNotifyState), extracted
    return extracted


def _gate_result(codes: tuple[str, ...]) -> HoldingDecisionResult:
    return _holding_decision_result().model_copy(
        update={
            "hard_gate": HoldingDecisionHardGate(
                triggered=True, reason_codes=codes, score_cap=-30.0, adjustment_applied=True
            ),
            "final_score": -30.0,
        }
    )


def test_state_is_recorded_in_the_pr1_format_and_round_trips() -> None:
    recommendation = _recommendation()
    state = _state_of(recommendation)
    result = _holding_decision_result()
    assert state.scoring_model_version == str(_CONFIG.holding_decision.scoring_model_version)
    assert state.base_score == result.base_score
    assert state.final_score == result.final_score
    assert state.recommendation_type == RecommendationType.SELL_CONSIDERATION.value
    assert state.category == result.category.value
    assert state.decision_severity == 1
    assert state.gate_confirmations == frozenset()
    assert state.market_price is not None and state.market_price > 0
    assert state.earnings_freshness in set(EarningsDataFreshness)


@pytest.mark.parametrize(
    ("category", "expected_type", "expected_severity"),
    [
        (HoldingDecisionCategory.SELL_CONSIDERATION, RecommendationType.SELL_CONSIDERATION, 1),
        (
            HoldingDecisionCategory.STRONG_SELL_CONSIDERATION,
            RecommendationType.STRONG_SELL_CONSIDERATION,
            2,
        ),
    ],
)
def test_decision_severity_follows_the_existing_order_of_the_three_types(
    category: HoldingDecisionCategory, expected_type: RecommendationType, expected_severity: int
) -> None:
    state = _state_of(
        _recommendation(_holding_decision_result().model_copy(update={"category": category}))
    )
    assert state.recommendation_type == expected_type.value
    assert state.decision_severity == expected_severity


def test_urgent_review_is_the_heaviest_and_has_no_sell_reference() -> None:
    recommendation = _recommendation(_gate_result(("BANKRUPTCY_FILING",)))
    state = _state_of(recommendation)
    assert state.recommendation_type == RecommendationType.URGENT_HOLDING_REVIEW.value
    assert state.decision_severity == 3
    assert state.sell_reference is None  # 即時執行目安(現在値)は参照価格にしない


def test_sell_reference_uses_only_target_price_levels() -> None:
    """売却目安価格は、適正価格由来(basis = TARGET_PRICE)だけ。監視用の現在値は使わない。"""
    from jstock_advisor.domain.entities.enums import PriceFieldBasis

    snapshot = _base_snapshot()
    sell = _recommendation(snapshot=snapshot)
    state = _state_of(sell)
    prices = sell.sell_prices
    level = prices.stop_review_price
    if level is not None and level.basis is PriceFieldBasis.TARGET_PRICE:
        assert state.sell_reference is not None
        assert state.sell_reference.kind == "stop_review_price"
        assert state.sell_reference.price == float(level.price)
    else:
        assert state.sell_reference is None


def test_gate_confirmations_are_recorded_per_reason_code() -> None:
    result = _gate_result(("BANKRUPTCY_FILING", "DEBT_EXCESS"))
    recommendation = _recommendation(
        result,
        gate_confirmations=(("BANKRUPTCY_FILING", "KEYWORD_ONLY"), ("DEBT_EXCESS", "CONFIRMED")),
    )
    assert _state_of(recommendation).gate_confirmations == frozenset(
        {
            ("BANKRUPTCY_FILING", GateConfirmation.KEYWORD_ONLY),
            ("DEBT_EXCESS", GateConfirmation.CONFIRMED),
        }
    )


def test_missing_or_invalid_confirmations_become_unverified() -> None:
    """渡されなかった理由コード・未知の値は UNVERIFIED(数えない側)。余分な理由コードは無視。"""
    result = _gate_result(("BANKRUPTCY_FILING", "DEBT_EXCESS"))
    recommendation = _recommendation(
        result,
        gate_confirmations=(("DEBT_EXCESS", "NOT_A_STATE"), ("SOMETHING_ELSE", "CONFIRMED")),
    )
    assert _state_of(recommendation).gate_confirmations == frozenset(
        {
            ("BANKRUPTCY_FILING", GateConfirmation.UNVERIFIED),
            ("DEBT_EXCESS", GateConfirmation.UNVERIFIED),
        }
    )


def test_no_hard_gate_means_no_confirmations_even_if_some_are_passed() -> None:
    recommendation = _recommendation(gate_confirmations=(("DEBT_EXCESS", "CONFIRMED"),))
    assert _state_of(recommendation).gate_confirmations == frozenset()


def test_the_state_carries_values_only_no_identifiers() -> None:
    """銘柄コード・保有 ID・金額・株数・所有者を持たない(値だけ)。"""
    recommendation = _recommendation()
    stored = recommendation.config_values_used[HD_RENOTIFY_STATE_KEY]
    text = repr(stored)
    assert recommendation.stock_code not in text
    assert (recommendation.holding_id or "\0") not in text
    assert set(stored) == {
        "state_version",
        "scoring_model_version",
        "base_score",
        "final_score",
        "recommendation_type",
        "category",
        "decision_severity",
        "gate_confirmations",
        "earnings_key",
        "earnings_freshness",
        "sell_reference",
        "market_price",
    }


# ===========================================================================
# (2b) 売却目安価格・決算の反映状況(財務鮮度)の記録
# ===========================================================================


def _price(value: str, basis: Any) -> Any:
    from decimal import Decimal

    from jstock_advisor.domain.entities.common import PriceWithRationale

    return PriceWithRationale(price=Decimal(value), rationale="test", basis=basis)


def test_sell_reference_picks_the_target_price_field_of_each_type() -> None:
    from jstock_advisor.domain.entities.common import SellPriceLevels
    from jstock_advisor.domain.entities.enums import PriceFieldBasis

    target = PriceFieldBasis.TARGET_PRICE
    prices = SellPriceLevels(
        stop_review_price=_price("900", target),
        full_profit_consideration_price=_price("800", target),
        immediate_execution_price=_price("1000", target),
    )
    pick = builder_module._sell_reference
    sell = pick(RecommendationType.SELL_CONSIDERATION, prices)
    strong = pick(RecommendationType.STRONG_SELL_CONSIDERATION, prices)
    assert (sell.kind, sell.price) == ("stop_review_price", 900.0)
    assert (strong.kind, strong.price) == ("full_profit_consideration_price", 800.0)
    # 緊急確認は参照価格を持たない(即時執行目安は現在値で、目安価格ではない)
    assert pick(RecommendationType.URGENT_HOLDING_REVIEW, prices) is None


def test_sell_reference_ignores_monitoring_and_immediate_reference_prices() -> None:
    from jstock_advisor.domain.entities.common import SellPriceLevels
    from jstock_advisor.domain.entities.enums import PriceFieldBasis

    for basis in (
        PriceFieldBasis.MONITORING_ONLY_NOT_A_SELL_TARGET,
        PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE,
    ):
        prices = SellPriceLevels(
            stop_review_price=_price("900", basis),
            full_profit_consideration_price=_price("800", basis),
        )
        for recommendation_type in (
            RecommendationType.SELL_CONSIDERATION,
            RecommendationType.STRONG_SELL_CONSIDERATION,
        ):
            assert builder_module._sell_reference(recommendation_type, prices) is None, basis


def test_sell_reference_is_none_when_the_price_is_missing() -> None:
    from jstock_advisor.domain.entities.common import SellPriceLevels

    for recommendation_type in (
        RecommendationType.SELL_CONSIDERATION,
        RecommendationType.STRONG_SELL_CONSIDERATION,
    ):
        assert builder_module._sell_reference(recommendation_type, SellPriceLevels()) is None


@pytest.mark.parametrize("verdict", ["FRESH", "STALE", "UNKNOWN"])
def test_the_state_records_the_financial_freshness_and_the_latest_period_end(
    verdict: str,
) -> None:
    from jstock_advisor.domain.signals.earnings_window import resolve_latest_financial_period_end
    from tests.unit.test_issue_468_holding_decision_financial_freshness import (
        _VERDICT_CASES,
        _snapshot_with,
    )
    from tests.unit.test_issue_468_holding_decision_financial_freshness import (
        _recommendation as _freshness_recommendation,
    )

    quarterly, fy_month, now, _ = _VERDICT_CASES[verdict]
    state = _state_of(_freshness_recommendation(verdict))
    assert state.earnings_freshness is EarningsDataFreshness(verdict)
    # 決算の識別(財務期間末)は、評価時刻の日付(JST)で解決した最新の期間末と一致する
    from jstock_advisor.domain.jst import evaluation_date_jst

    expected = resolve_latest_financial_period_end(
        _snapshot_with(quarterly, fy_month).financial, evaluation_date_jst(now)
    ).period_end
    assert state.earnings_key == expected
    if verdict in {"FRESH", "STALE"}:
        assert state.earnings_key is not None


# ===========================================================================
# (3) 記録の失敗が通知の記録の構築を止めない
# ===========================================================================


def test_a_failure_while_building_the_state_does_not_break_the_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(builder_module, "_build_hd_renotify_state", _boom)
    recommendation = _recommendation()
    assert recommendation.recommendation_id == "pr2-test"
    stored = recommendation.config_values_used[HD_RENOTIFY_STATE_KEY]
    assert stored == {"computation_failed": True, "error_type": "RuntimeError"}
    # 比較側は『比べられない(STATE_MALFORMED)』として扱う
    assert extract_hd_state(recommendation.config_values_used) == StateUnavailable(
        Reason.STATE_MALFORMED
    )


def test_a_non_finite_score_is_recorded_as_failed_not_raised() -> None:
    result = _holding_decision_result().model_copy(update={"final_score": math.nan})
    recommendation = _recommendation(result)
    assert extract_hd_state(recommendation.config_values_used) == StateUnavailable(
        Reason.STATE_MALFORMED
    )
    assert recommendation.recommendation_type == RecommendationType.SELL_CONSIDERATION


def test_the_notification_content_does_not_depend_on_the_state() -> None:
    """記録の成否・確認状態の有無で、通知の内容(本文に使う項目)は変わらない。"""
    plain = _recommendation()
    with_confirmations = _recommendation(gate_confirmations=(("DEBT_EXCESS", "CONFIRMED"),))
    ignored = {"config_values_used"}
    assert {k: v for k, v in plain.model_dump().items() if k not in ignored} == {
        k: v for k, v in with_confirmations.model_dump().items() if k not in ignored
    }


# ===========================================================================
# (4) 評価が返す確認状態
# ===========================================================================


def _evaluate_with(store_dir: Path, risk: list[str], material: list[str]) -> Any:
    snapshot, error = build_stock_snapshot(_SERVICE_PROVIDERS, "2914", _SERVICE_NOW, _SERVICE_CFG)
    assert snapshot is not None, error
    snapshot = dataclasses.replace(
        snapshot,
        disclosure_risk_keywords_found=risk,
        material_event_keywords_found=material,
    )
    service: HoldingDecisionService = _service(store_dir)
    return service.evaluate(
        _service_holding(),
        _SERVICE_NOW,
        ExecutionPlanReason.NORMAL_ACTIVE,
        snapshot=snapshot,
    )


def test_evaluation_without_any_hard_gate_returns_no_confirmations(store_dir: Path) -> None:
    outcome = _evaluate_with(store_dir, [], [])
    assert outcome.result is not None
    assert outcome.result.hard_gate.triggered is False
    assert outcome.gate_confirmations == ()


def test_keyword_only_disclosure_is_keyword_only_not_confirmed(store_dir: Path) -> None:
    """#888 の事実: 危険な言葉だけで hard gate は発動するが、確認状態は KEYWORD_ONLY。"""
    outcome = _evaluate_with(store_dir, ["第三者委員会"], [])
    assert outcome.result is not None
    assert "BANKRUPTCY_FILING" in outcome.result.hard_gate.reason_codes
    assert dict(outcome.gate_confirmations)["BANKRUPTCY_FILING"] == "KEYWORD_ONLY"


def test_material_event_confirmation_is_confirmed(store_dir: Path) -> None:
    outcome = _evaluate_with(store_dir, ["第三者委員会"], ["決算訂正"])
    assert outcome.result is not None
    assert dict(outcome.gate_confirmations)["BANKRUPTCY_FILING"] == "CONFIRMED"


def test_accounting_problem_has_no_two_stage_so_it_is_keyword_only(store_dir: Path) -> None:
    """会計の問題は現行では 2 段階でなく、確認語があっても KEYWORD_ONLY(#889 の事実)。"""
    outcome = _evaluate_with(store_dir, ["不適切な会計処理"], ["決算訂正"])
    assert outcome.result is not None
    assert "ACCOUNTING_FRAUD" in outcome.result.hard_gate.reason_codes
    assert dict(outcome.gate_confirmations)["ACCOUNTING_FRAUD"] == "KEYWORD_ONLY"


def test_gate_confirmations_cover_exactly_the_triggered_reason_codes(store_dir: Path) -> None:
    outcome = _evaluate_with(store_dir, ["第三者委員会", "上場廃止基準"], ["決算訂正"])
    assert outcome.result is not None
    assert {code for code, _ in outcome.gate_confirmations} == set(
        outcome.result.hard_gate.reason_codes
    )


def test_the_evaluation_result_shape_is_unchanged(store_dir: Path) -> None:
    """保存される評価結果(HoldingDecisionResult)の項目は増えていない(付属情報は outcome 側)。"""
    outcome = _evaluate_with(store_dir, ["第三者委員会"], [])
    assert outcome.result is not None
    assert "gate_confirmations" not in type(outcome.result).model_fields
    assert not any("renotify" in name for name in type(outcome.result).model_fields)


# ===========================================================================
# (5) handler 通し: SHADOW では記録が作られず、ACTIVE では作られる
# ===========================================================================


def _recommendations(services: dict[str, Any]) -> list[Any]:
    return list(services["recommendation_repo"].list_all())


def _patch_evaluate(monkeypatch: pytest.MonkeyPatch, confirmations: tuple[tuple[str, str], ...]):
    from jstock_advisor.services.holding_decision_service import (
        HoldingDecisionEvaluationOutcome,
    )

    result = _notifying_holding_decision_result("2914")

    def _fake(self: Any, *args: Any, **kwargs: Any) -> Any:
        return HoldingDecisionEvaluationOutcome("2914", result, gate_confirmations=confirmations)

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _fake)


def test_shadow_mode_creates_no_recommendation_and_no_state(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = _build_services(store_dir, RuntimeConfigMode.SHADOW)
    _patch_evaluate(monkeypatch, (("BANKRUPTCY_FILING", "KEYWORD_ONLY"),))
    _run(services)
    recommendations = _recommendations(services)
    assert not any(HD_RENOTIFY_STATE_KEY in r.config_values_used for r in recommendations)
    # 保有判断の種類の Recommendation そのものが作られない(builder が呼ばれない)
    assert not any(
        r.recommendation_type
        in {
            RecommendationType.SELL_CONSIDERATION,
            RecommendationType.STRONG_SELL_CONSIDERATION,
            RecommendationType.URGENT_HOLDING_REVIEW,
        }
        for r in recommendations
    )


def test_active_mode_records_the_state_with_the_evaluation_confirmations(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = _build_services(store_dir, RuntimeConfigMode.ACTIVE)
    _patch_evaluate(monkeypatch, ())
    _run(services)
    holding_decision_recs = [
        r
        for r in _recommendations(services)
        if r.recommendation_type
        in {
            RecommendationType.SELL_CONSIDERATION,
            RecommendationType.STRONG_SELL_CONSIDERATION,
            RecommendationType.URGENT_HOLDING_REVIEW,
        }
    ]
    assert len(holding_decision_recs) == 1
    state = extract_hd_state(holding_decision_recs[0].config_values_used)
    assert isinstance(state, HdNotifyState)


def test_active_mode_passes_the_evaluation_confirmations_to_the_record(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = _build_services(store_dir, RuntimeConfigMode.ACTIVE)
    result = _notifying_holding_decision_result("2914")
    result = result.model_copy(
        update={
            "hard_gate": HoldingDecisionHardGate(
                triggered=True,
                reason_codes=("BANKRUPTCY_FILING",),
                score_cap=-30.0,
                adjustment_applied=True,
            ),
            "final_score": -30.0,
        }
    )
    from jstock_advisor.services.holding_decision_service import (
        HoldingDecisionEvaluationOutcome,
    )

    def _fake(self: Any, *args: Any, **kwargs: Any) -> Any:
        return HoldingDecisionEvaluationOutcome(
            "2914", result, gate_confirmations=(("BANKRUPTCY_FILING", "KEYWORD_ONLY"),)
        )

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _fake)
    _run(services)
    recs = [
        r
        for r in _recommendations(services)
        if r.recommendation_type == RecommendationType.URGENT_HOLDING_REVIEW
    ]
    assert len(recs) == 1
    state = extract_hd_state(recs[0].config_values_used)
    assert isinstance(state, HdNotifyState)
    assert state.gate_confirmations == frozenset(
        {("BANKRUPTCY_FILING", GateConfirmation.KEYWORD_ONLY)}
    )


def test_kill_switch_suppresses_the_line_message_but_the_record_is_still_made(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既存の挙動の固定: 緊急停止スイッチ(通知の停止)が入っていても、通常運用(ACTIVE)では
    Recommendation の生成・保存は続き(LINE 送信だけを止める)、前回の状態の記録も作られる。

    実行計画(mode_plan)は緊急停止スイッチの影響を受けない値で決める(handler の
    resolve_execution_plan(notification_enabled=True))ためで、PR-2 が変えたものではない。
    送信されない記録は『配信された通知』ではないので、PR-3 は通知ログ(送信実績)から前回を引く。
    """
    services = _build_services(store_dir, RuntimeConfigMode.ACTIVE, notification_enabled=False)
    _patch_evaluate(monkeypatch, ())
    result = _run(services)
    assert result.notified is False
    assert services["line_client"].sent_messages == []
    assert any(HD_RENOTIFY_STATE_KEY in r.config_values_used for r in _recommendations(services))


# ===========================================================================
# (6) 旧形式の記録は『比べられない』
# ===========================================================================


def test_a_legacy_recommendation_without_the_key_is_not_comparable() -> None:
    recommendation = _recommendation()
    legacy_values = {
        k: v for k, v in recommendation.config_values_used.items() if k != HD_RENOTIFY_STATE_KEY
    }
    assert extract_hd_state(legacy_values) == StateUnavailable(Reason.PREVIOUS_IS_LEGACY)


def test_no_previous_recommendation_means_no_previous_state() -> None:
    assert extract_hd_state(None) == StateUnavailable(Reason.NO_PREVIOUS_HD_STATE)
