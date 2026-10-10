"""保有判断の再通知条件(R1〜R5)の通知の判断への接続(Issue #890 PR-3)のテスト。

固定するもの
  (1) 比べられるとき(前回・今回の状態がある): 条件 1 つずつで『送る』、成立しなければ『送らない』、
      周期の再送(経過日数 ≧ N)は暫定の方針(KEEP)で残る
  (2) 価格による再送は R5(売却目安価格 5.0%)に置き換わり、共通の 3.0% は保有判断に効かない
  (3) 比べられないとき(前回が旧方式の記録・状態が無い・builder 失敗の印・今回の状態が無い)と、
      保有判断以外の種類は、従来の判断のまま(既存の挙動の全体は characterization で固定)
  (4) 新しい判断の失敗・設定の不正は、従来の判断へ倒れる(通知の判断を止めない)
  (5) 暫定の方針は 1 か所にあり、値が固定され、他から作られない
  (6) 成立した条件が取り出せる / 検証モード(SHADOW)では保有判断の通知の判断に到達しない

時間意味論: 経過日数は既存の JST 暦日差を使う。固定の日時リテラルのみ(wall clock は使わない)。
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import (
    NotificationStatus,
    RecommendationType,
    RuntimeConfigMode,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.holding_decision_renotification import (
    HD_RENOTIFY_STATE_KEY,
    DecisionChangeScope,
    EarningsDataFreshness,
    EarningsMode,
    GateConfirmation,
    HdNotifyState,
    KeywordOnlyHandling,
    PeriodicPolicy,
    ScoreBasis,
    SellPriceReference,
    SellReference,
    serialize_hd_state,
)
from jstock_advisor.services import line_notification_service as line_module
from jstock_advisor.services.hd_renotification_provisional_policy import (
    PROVISIONAL_RENOTIFICATION_POLICY,
)
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from tests.unit.test_hd_renotification_wiring_characterization import (
    _CONFIG,
    _NOW,
    _RESEND_AFTER_DAYS,
    make_recommendation,
    make_service,
)
from tests.unit.test_holdings_watchlist_handler_integration import (
    _build_services,
    _notifying_holding_decision_result,
    _run,
)

_SELL = RecommendationType.SELL_CONSIDERATION
_STRONG = RecommendationType.STRONG_SELL_CONSIDERATION
_URGENT = RecommendationType.URGENT_HOLDING_REVIEW
_Q1 = dt.date(2026, 3, 31)
_Q2 = dt.date(2026, 6, 30)
_PRICE = 1000.0

_CONFIRMED = GateConfirmation.CONFIRMED
_KEYWORD = GateConfirmation.KEYWORD_ONLY


def state(**overrides: Any) -> HdNotifyState:
    base: dict[str, Any] = {
        "scoring_model_version": "1",
        "base_score": -20.0,
        "final_score": -20.0,
        "recommendation_type": _SELL.value,
        "category": "WATCH",
        "decision_severity": 1,
        "gate_confirmations": frozenset(),
        "earnings_key": _Q1,
        "earnings_freshness": EarningsDataFreshness.FRESH,
        "sell_reference": SellReference("stop_review_price", _PRICE),
        "market_price": 1200.0,
    }
    base.update(overrides)
    return HdNotifyState(**base)


def hd_recommendation(
    recommendation_type: RecommendationType, hd_state: HdNotifyState | None, *, identifier: str
) -> Recommendation:
    values: dict[str, Any] = {}
    if hd_state is not None:
        values[HD_RENOTIFY_STATE_KEY] = serialize_hd_state(hd_state)
    return make_recommendation(
        recommendation_type,
        Decimal("1000"),
        recommendation_id=identifier,
        config_values_used=values,
    )


def decide(
    tmp_path: Path,
    *,
    current: HdNotifyState | None,
    previous: HdNotifyState | None,
    days: int = 1,
    recommendation_type: RecommendationType = _SELL,
    previous_type: RecommendationType | None = None,
) -> NotificationStatus:
    service = make_service(tmp_path, days)
    current_rec = hd_recommendation(
        recommendation_type, current, identifier="22222222-2222-4222-8222-222222222222"
    )
    previous_rec = hd_recommendation(
        previous_type or recommendation_type,
        previous,
        identifier="33333333-3333-4333-8333-333333333333",
    )
    return service._notification_status_for_send(current_rec, previous_rec, _NOW)


# ===========================================================================
# (1) 条件 1 つずつ・周期
# ===========================================================================


def test_unchanged_state_is_suppressed_before_the_periodic_resend(tmp_path: Path) -> None:
    assert (
        decide(tmp_path, current=state(), previous=state())
        is NotificationStatus.DUPLICATE_SUPPRESSED
    )


@pytest.mark.parametrize(
    ("label", "current_overrides"),
    [
        ("R1 スコアが 10 点悪化", {"base_score": -30.0}),
        ("R2 判定(category)が変わった", {"category": "AVOID"}),
        ("R3 新たに確認済みの hard gate", {"gate_confirmations": frozenset({("A", _CONFIRMED)})}),
        ("R4 決算を反映", {"earnings_key": _Q2}),
        (
            "R5 売却目安価格が 5% 動いた",
            {"sell_reference": SellReference("stop_review_price", 1050.0)},
        ),
    ],
)
def test_each_condition_alone_sends(
    tmp_path: Path, label: str, current_overrides: dict[str, Any]
) -> None:
    assert decide(tmp_path, current=state(**current_overrides), previous=state()) is (
        NotificationStatus.SENT
    ), label


@pytest.mark.parametrize(
    ("label", "current_overrides"),
    [
        ("R1 9.99 点の悪化", {"base_score": -29.99}),
        ("R3 キーワード一致のみ", {"gate_confirmations": frozenset({("A", _KEYWORD)})}),
        ("R4 同じ決算", {"earnings_key": _Q1}),
        (
            "R4 鮮度が STALE",
            {"earnings_key": _Q2, "earnings_freshness": EarningsDataFreshness.STALE},
        ),
        ("R5 4.9% の動き", {"sell_reference": SellReference("stop_review_price", 1049.0)}),
    ],
)
def test_conditions_just_below_the_boundary_do_not_send(
    tmp_path: Path, label: str, current_overrides: dict[str, Any]
) -> None:
    assert decide(tmp_path, current=state(**current_overrides), previous=state()) is (
        NotificationStatus.DUPLICATE_SUPPRESSED
    ), label


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (_RESEND_AFTER_DAYS - 1, NotificationStatus.DUPLICATE_SUPPRESSED),
        (_RESEND_AFTER_DAYS, NotificationStatus.SENT),
        (_RESEND_AFTER_DAYS + 1, NotificationStatus.SENT),
    ],
)
def test_periodic_resend_is_kept_by_the_provisional_policy(
    tmp_path: Path, days: int, expected: NotificationStatus
) -> None:
    """D-1(暫定: 残す)。条件が何も成立しなくても、経過日数が N 日に達したら再送する。"""
    assert decide(tmp_path, current=state(), previous=state(), days=days) is expected


def test_a_different_recommendation_type_still_sends_as_before(tmp_path: Path) -> None:
    """既存の『種別が違えば送る』は保有判断でも前に効く(状態の比較より先)。"""
    assert decide(
        tmp_path,
        current=state(recommendation_type=_STRONG.value),
        previous=state(),
        recommendation_type=_STRONG,
        previous_type=_SELL,
    ) is (NotificationStatus.SENT)


# ===========================================================================
# (2) 価格による再送は R5 に置き換わる
# ===========================================================================


def test_the_common_three_percent_rule_does_not_apply_to_holding_decision(tmp_path: Path) -> None:
    """代表価格(sell_prices)が 5% 動いても、売却目安価格(状態の sell_reference)が不変なら送らない。

    従来の判断(共通の 3.0%)なら送る入力(同じ入力で、状態が無ければ SENT になることも確認する)。
    """
    service = make_service(tmp_path, 1)
    stateful_current = make_recommendation(
        _SELL,
        Decimal("1050"),
        recommendation_id="22222222-2222-4222-8222-222222222222",
        config_values_used={HD_RENOTIFY_STATE_KEY: serialize_hd_state(state())},
    )
    stateful_previous = hd_recommendation(
        _SELL, state(), identifier="33333333-3333-4333-8333-333333333333"
    )
    assert service._notification_status_for_send(stateful_current, stateful_previous, _NOW) is (
        NotificationStatus.DUPLICATE_SUPPRESSED
    )
    legacy_current = make_recommendation(
        _SELL, Decimal("1050"), recommendation_id="22222222-2222-4222-8222-222222222222"
    )
    legacy_previous = hd_recommendation(
        _SELL, None, identifier="33333333-3333-4333-8333-333333333333"
    )
    assert service._notification_status_for_send(legacy_current, legacy_previous, _NOW) is (
        NotificationStatus.SENT
    )


def test_current_price_moves_alone_do_not_send_under_target_price_only(tmp_path: Path) -> None:
    """D-6(暫定: 適正価格由来のみ)。市場価格だけが動いても送らない。"""
    assert decide(tmp_path, current=state(market_price=2400.0), previous=state()) is (
        NotificationStatus.DUPLICATE_SUPPRESSED
    )


# ===========================================================================
# (3) 比べられないときは従来の判断のまま
# ===========================================================================


def test_previous_without_a_state_record_follows_the_existing_judgment(tmp_path: Path) -> None:
    """前回が旧方式の記録(状態が無い)なら、状態があれば R1 で送る入力でも、従来の判断のまま。"""
    existing = decide(tmp_path / "a", current=None, previous=None)
    status = decide(tmp_path / "b", current=state(base_score=-90.0), previous=None)
    assert status is existing
    assert status is not NotificationStatus.SENT


def test_current_without_a_state_record_follows_the_existing_judgment(tmp_path: Path) -> None:
    existing = decide(tmp_path / "a", current=None, previous=None)
    status = decide(tmp_path / "b", current=None, previous=state())
    assert status is existing
    assert status is not NotificationStatus.SENT


def test_the_decision_function_is_not_called_when_a_state_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """比べられない場合は、判定の関数を呼ばずに従来の判断へ進む(例外の捕捉に頼らない)。"""
    calls: list[int] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise AssertionError("must not be called")

    monkeypatch.setattr(line_module, "decide_hd_renotification", _spy)
    decide(tmp_path / "a", current=None, previous=state())
    decide(tmp_path / "b", current=state(), previous=None)
    decide(tmp_path / "c", current=None, previous=None)
    assert calls == []


def test_builder_failure_marker_is_treated_as_not_comparable(tmp_path: Path) -> None:
    service = make_service(tmp_path, 1)
    marker = {"computation_failed": True, "error_type": "RuntimeError"}
    current = make_recommendation(
        _SELL,
        Decimal("1000"),
        recommendation_id="22222222-2222-4222-8222-222222222222",
        config_values_used={HD_RENOTIFY_STATE_KEY: marker},
    )
    previous = hd_recommendation(_SELL, state(), identifier="33333333-3333-4333-8333-333333333333")
    assert service._notification_status_for_send(current, previous, _NOW) is not (
        NotificationStatus.SENT
    )


def test_non_holding_decision_types_ignore_a_state_record(tmp_path: Path) -> None:
    """保有判断の 3 種類以外は、状態の記録があっても従来の判断のまま(全体は golden で固定)。"""
    service = make_service(tmp_path, 1)

    def _status(with_state: bool) -> NotificationStatus:
        def values(hd_state: HdNotifyState) -> dict[str, Any]:
            return {HD_RENOTIFY_STATE_KEY: serialize_hd_state(hd_state)} if with_state else {}

        current = make_recommendation(
            RecommendationType.SELL,
            Decimal("1000"),
            recommendation_id="22222222-2222-4222-8222-222222222222",
            config_values_used=values(state(base_score=-90.0)),
        )
        previous = make_recommendation(
            RecommendationType.SELL,
            Decimal("1000"),
            recommendation_id="33333333-3333-4333-8333-333333333333",
            config_values_used=values(state()),
        )
        return service._notification_status_for_send(current, previous, _NOW)

    assert _status(with_state=True) is _status(with_state=False)
    assert _status(with_state=True) is not NotificationStatus.SENT


# ===========================================================================
# (4) 失敗・設定の不正は従来の判断へ倒れる
# ===========================================================================


def test_a_failure_in_the_new_judgment_falls_back_to_the_existing_judgment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(line_module, "decide_hd_renotification", _boom)
    with caplog.at_level("WARNING"):
        status = decide(tmp_path, current=state(base_score=-90.0), previous=state())
    assert status is not NotificationStatus.SENT  # 従来の判断(日数未経過・価格は不変 → 送らない)
    messages = [r.getMessage() for r in caplog.records if "hd renotification" in r.getMessage()]
    assert messages
    assert all("boom" not in m for m in messages)  # 例外の中身は出さない(型名だけ)
    assert any("RuntimeError" in m for m in messages)


def test_an_invalid_renotification_config_falls_back_to_the_existing_judgment(
    tmp_path: Path,
) -> None:
    rules = _CONFIG.holding_decision.renotification
    bad = rules.model_copy(update={"renotify_score_deterioration": 0.0})
    bad_config = _CONFIG.model_copy(
        update={
            "holding_decision": _CONFIG.holding_decision.model_copy(update={"renotification": bad})
        }
    )
    service = make_service(tmp_path, 1)
    service._config = bad_config
    current = hd_recommendation(
        _SELL, state(base_score=-90.0), identifier="22222222-2222-4222-8222-222222222222"
    )
    previous = hd_recommendation(_SELL, state(), identifier="33333333-3333-4333-8333-333333333333")
    assert service._notification_status_for_send(current, previous, _NOW) is not (
        NotificationStatus.SENT
    )


# ===========================================================================
# (5) 暫定の方針は 1 か所
# ===========================================================================

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"


def test_the_provisional_policy_values_are_pinned() -> None:
    """設計 rev1 の推奨値(案 A)。確定(D-1〜D-6)したら、この 1 か所とこのテストだけを変える。"""
    policy = PROVISIONAL_RENOTIFICATION_POLICY
    assert policy.periodic is PeriodicPolicy.KEEP
    assert policy.decision_change_scope is DecisionChangeScope.ANY_CHANGE
    assert policy.earnings_mode is EarningsMode.FIRST_EVALUATION_AFTER_EARNINGS
    assert policy.score_basis is ScoreBasis.BASE_SCORE
    assert policy.keyword_only is KeywordOnlyHandling.NOT_COUNTED
    assert policy.sell_price_reference is SellPriceReference.TARGET_PRICE_ONLY


def test_the_policy_is_built_in_one_place_and_used_only_by_the_notification_service() -> None:
    constructors = []
    users = []
    for path in _SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(_SRC_ROOT).as_posix()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "RenotificationPolicy"
            ):
                constructors.append(relative)
        if "PROVISIONAL_RENOTIFICATION_POLICY" in text and not relative.endswith(
            "hd_renotification_provisional_policy.py"
        ):
            users.append(relative)
    assert constructors == ["services/hd_renotification_provisional_policy.py"]
    assert users == ["services/line_notification_service.py"]


def test_the_provisional_policy_is_named_and_documented_as_provisional() -> None:
    module = Path(_SRC_ROOT / "services" / "hd_renotification_provisional_policy.py")
    text = module.read_text(encoding="utf-8")
    assert "暫定" in text
    assert "ACTIVE" in text and "D-1〜D-6" in text


# ===========================================================================
# (6) 成立した条件・SHADOW
# ===========================================================================


def test_met_conditions_are_exposed_for_comparable_states(tmp_path: Path) -> None:
    service = make_service(tmp_path, 1)
    current = hd_recommendation(
        _SELL,
        state(base_score=-40.0, earnings_key=_Q2),
        identifier="22222222-2222-4222-8222-222222222222",
    )
    previous = hd_recommendation(_SELL, state(), identifier="33333333-3333-4333-8333-333333333333")
    assert service._hd_renotify_conditions(current, previous, _NOW) == (
        "renotify_after_earnings",
        "renotify_score_deterioration",
    )


def test_no_conditions_are_exposed_when_not_comparable_or_not_applicable(tmp_path: Path) -> None:
    service = make_service(tmp_path, 1)
    legacy = hd_recommendation(_SELL, None, identifier="33333333-3333-4333-8333-333333333333")
    current = hd_recommendation(
        _SELL, state(base_score=-90.0), identifier="22222222-2222-4222-8222-222222222222"
    )
    assert service._hd_renotify_conditions(current, legacy, _NOW) == ()
    assert service._hd_renotify_conditions(current, None, _NOW) == ()
    sell = make_recommendation(
        RecommendationType.SELL,
        Decimal("1000"),
        recommendation_id="44444444-4444-4444-8444-444444444444",
    )
    assert service._hd_renotify_conditions(sell, legacy, _NOW) == ()


def test_the_outcome_field_defaults_to_empty() -> None:
    outcome = line_module.NotificationOutcome(status=NotificationStatus.SENT, sent=False)
    assert outcome.hd_renotify_conditions == ()


def test_shadow_mode_never_reaches_the_holding_decision_judgment(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """検証モード(SHADOW)では、保有判断の通知の経路が動かず、再通知の判断に到達しない。"""
    from jstock_advisor.services.holding_decision_service import HoldingDecisionEvaluationOutcome

    services = _build_services(store_dir, RuntimeConfigMode.SHADOW)
    result = _notifying_holding_decision_result("2914")

    def _fake(self: Any, *args: Any, **kwargs: Any) -> Any:
        return HoldingDecisionEvaluationOutcome("2914", result)

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _fake)
    calls: list[RecommendationType] = []
    original = line_module.LineNotificationService._notification_status_for_send

    def _spy(self: Any, recommendation: Recommendation, previous: Any, now: Any) -> Any:
        calls.append(recommendation.recommendation_type)
        return original(self, recommendation, previous, now)

    monkeypatch.setattr(line_module.LineNotificationService, "_notification_status_for_send", _spy)
    _run(services)
    assert not any(t in line_module.HOLDING_DECISION_RECOMMENDATION_TYPES for t in calls)
