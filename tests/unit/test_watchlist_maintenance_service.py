"""AUTO_SCREENING銘柄の自動メンテナンス(計画Part C)の単体テスト(§テストA-P、
開示リスク回帰a-c)。

`evaluate_maintenance_decision()`は純粋関数(DynamoDB書き込み・監査記録を
一切行わない)であるため、ここでは全てin-memoryのWatchlistItem/
MaintenanceScreeningSummaryを直接組み立てて呼び出す。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.config.models import AutoRemovalConfig
from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.services.watchlist_maintenance_service import (
    MaintenanceOutcome,
    MaintenanceScreeningSummary,
    evaluate_maintenance_decision,
)

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)

_CONFIG = AutoRemovalConfig(
    enabled=True,
    minimum_age_days=90,
    consecutive_not_qualified_required=3,
    minimum_not_qualified_span_days=28,
    stale_recheck_days=30,
    maximum_unconfirmed_days=180,
    readd_cooldown_days=30,
)


def _item(
    *,
    created_at: dt.datetime,
    consecutive_not_qualified_count: int = 0,
    removal_candidate_since: dt.datetime | None = None,
    last_qualified_at: dt.datetime | None = None,
    registration_source: WatchlistRegistrationSource = WatchlistRegistrationSource.AUTO_SCREENING,
) -> WatchlistItem:
    return WatchlistItem(
        stock_code="1111",
        stock_name="テスト銘柄",
        reason="自動追加",
        registration_source=registration_source,
        registration_policy="multi_style_monitoring",
        created_at=created_at,
        updated_at=created_at,
        consecutive_not_qualified_count=consecutive_not_qualified_count,
        removal_candidate_since=removal_candidate_since,
        last_qualified_at=last_qualified_at,
    )


def _summary(
    *,
    passed: bool,
    total_score: float = 50.0,
    matched_target_types: list[str] | None = None,
    hard_exclusion_reasons: list[str] | None = None,
) -> MaintenanceScreeningSummary:
    return MaintenanceScreeningSummary(
        passed=passed,
        total_score=total_score,
        matched_target_types=matched_target_types or [],
        hard_exclusion_reasons=hard_exclusion_reasons or [],
        policy_name="multi_style_monitoring",
    )


# --- テストA: MANUAL登録は対象外(collector層でのフィルタリング確認) --------------


def test_manual_registration_is_excluded_from_maintenance_targets(
    monkeypatch,
) -> None:
    """`_collect_maintenance_targets()`はAUTO_SCREENING銘柄のみを対象とし、
    MANUAL登録は何があっても含めないこと(計画Part C-1)。"""
    from jstock_advisor.lambda_handlers import watchlist_dispatcher_handler as dispatcher_module

    auto_item = _item(created_at=_NOW)
    manual_item = WatchlistItem(
        stock_code="2222",
        stock_name="手動銘柄",
        reason="手動登録",
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert manual_item.registration_source == WatchlistRegistrationSource.MANUAL

    class _FakeWatchlistRepository:
        def list_all(self) -> list[WatchlistItem]:
            return [auto_item, manual_item]

    monkeypatch.setattr(
        dispatcher_module, "WatchlistRepository", lambda: _FakeWatchlistRepository()
    )

    codes, extra_kwargs = dispatcher_module._collect_maintenance_targets({})

    assert codes == ["1111"]
    assert extra_kwargs == {}


# --- テストB/C: 最低継続期間(age)ゲート -----------------------------------------


def test_b_age_below_minimum_keeps_even_with_enough_consecutive_count() -> None:
    """age30日+3回連続非該当でも、minimum_age_days(90日)未満のため削除しない。"""
    item = _item(
        created_at=_NOW - dt.timedelta(days=30),
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=29),
    )
    summary = _summary(passed=False)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.KEEP


def test_b2_same_day_added_stock_is_not_removed_even_with_enough_consecutive_count() -> None:
    """平日毎日起動化(2026-08)対応: age=0日(当日追加)+3回連続非該当でも、
    minimum_age_days未満のため削除しないこと(テスト#11相当、日次実行で
    「3営業日」に短縮されても当日追加銘柄が即削除されないことの直接確認)。"""
    item = _item(
        created_at=_NOW,
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW,
    )
    summary = _summary(passed=False)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.KEEP


def test_c_age_and_count_and_span_all_satisfied_removes() -> None:
    """age120日+3回連続非該当+span_days経過→削除。"""
    item = _item(
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=29),
    )
    summary = _summary(passed=False)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.CONSECUTIVE_NOT_QUALIFIED_REMOVAL


# --- テストD/N: 再該当でカウント・removal_candidate_sinceともにリセット ------------


def test_d_n_pass_after_two_failures_resets_count_and_since() -> None:
    item = _item(
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=14),
    )
    summary = _summary(passed=True)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.KEEP
    assert decision.updated_item.consecutive_not_qualified_count == 0
    assert decision.updated_item.removal_candidate_since is None
    assert decision.updated_item.last_qualified_at == _NOW


# --- テストE/O: DATA_ERRORはカウント・removal_candidate_sinceを変更しない ---------


def test_e_o_data_error_preserves_count_and_since() -> None:
    since = _NOW - dt.timedelta(days=14)
    item = _item(
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=2,
        removal_candidate_since=since,
    )
    decision = evaluate_maintenance_decision(item, None, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.DATA_UNAVAILABLE
    assert decision.updated_item.consecutive_not_qualified_count == 2
    assert decision.updated_item.removal_candidate_since == since
    assert decision.updated_item.last_screened_at == _NOW


def test_e_data_error_before_maximum_unconfirmed_days_not_stale() -> None:
    item = _item(
        created_at=_NOW - dt.timedelta(days=120), last_qualified_at=_NOW - dt.timedelta(days=30)
    )
    decision = evaluate_maintenance_decision(item, None, _CONFIG, _NOW)
    assert decision.stale_unconfirmed is False


def test_data_error_past_maximum_unconfirmed_days_is_stale_but_not_removed() -> None:
    """テストC(長期確認不能): maximum_unconfirmed_days超過でも削除はしない
    (警告フラグのみ)。"""
    item = _item(
        created_at=_NOW - dt.timedelta(days=300),
        last_qualified_at=_NOW - dt.timedelta(days=200),
    )
    decision = evaluate_maintenance_decision(item, None, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.DATA_UNAVAILABLE
    assert decision.stale_unconfirmed is True


# --- テストF/G/P: 即時削除(Aルート、span_days無関係) ------------------------------


def test_f_p_debt_excess_removes_immediately_regardless_of_span_days() -> None:
    """債務超過は1回でも即削除、minimum_not_qualified_span_days(28日)未経過でも
    関係ない(Aルート、計画Part C-3)。"""
    item = _item(created_at=_NOW - dt.timedelta(days=120))  # removal_candidate_since=None
    summary = _summary(passed=False, hard_exclusion_reasons=["債務超過のため対象外です"])
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.IMMEDIATE_REMOVAL
    assert decision.removal_reason == "債務超過のため対象外です"


def test_g_going_concern_doubt_removes_immediately() -> None:
    item = _item(created_at=_NOW - dt.timedelta(days=120))
    summary = _summary(
        passed=False, hard_exclusion_reasons=["継続企業の前提に重大な疑義があります"]
    )
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.IMMEDIATE_REMOVAL


def test_reit_and_etf_reasons_also_remove_immediately() -> None:
    for reason in ("REITは対象外です", "ETFは対象外です"):
        item = _item(created_at=_NOW - dt.timedelta(days=120))
        summary = _summary(passed=False, hard_exclusion_reasons=[reason])
        decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
        assert decision.outcome == MaintenanceOutcome.IMMEDIATE_REMOVAL, reason


def test_immediate_removal_does_not_require_minimum_age() -> None:
    """Aルートはminimum_age_daysも無関係(登録直後でも即削除できる)。"""
    item = _item(created_at=_NOW - dt.timedelta(days=1))
    summary = _summary(passed=False, hard_exclusion_reasons=["債務超過のため対象外です"])
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.IMMEDIATE_REMOVAL


# --- テストH: 価格タイミングは削除判定に一切使わない -------------------------------


def test_h_price_is_never_referenced_by_the_decision_function() -> None:
    """MultiStyleMonitoringPolicyが価格接近度を参照しないため、
    evaluate_maintenance_decision()のシグネチャ自体に価格関連引数が
    存在しないことをもって設計上自動的に満たされることを明示する。"""
    item = _item(created_at=_NOW - dt.timedelta(days=120))
    summary = _summary(passed=True)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.KEEP


# --- テストL/M: minimum_not_qualified_span_daysのAND条件境界値 -------------------


def test_l_span_days_not_yet_elapsed_keeps() -> None:
    """age=120日、3回連続非該当だが、removal_candidate_sinceから14日しか
    経過していない(span_days=28未満)→削除しない。"""
    item = _item(
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=14),
    )
    summary = _summary(passed=False)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.KEEP


def test_m_span_days_elapsed_removes() -> None:
    """age=120日、3回連続非該当、removal_candidate_sinceから28日以上経過→削除。"""
    item = _item(
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=28),
    )
    summary = _summary(passed=False)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.CONSECUTIVE_NOT_QUALIFIED_REMOVAL


def test_consecutive_count_below_required_keeps_even_when_span_elapsed() -> None:
    """span_daysだけ満たしても、consecutive_not_qualified_required(3)未満なら
    削除しない(件数条件と期間条件は独立したAND、両方必要)。"""
    item = _item(
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=1,
        removal_candidate_since=_NOW - dt.timedelta(days=40),
    )
    summary = _summary(passed=False)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.KEEP


# --- 初回非該当時のremoval_candidate_since設定確認 -------------------------------


def test_first_failure_sets_removal_candidate_since_to_now() -> None:
    item = _item(created_at=_NOW - dt.timedelta(days=120))
    summary = _summary(passed=False)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.updated_item.consecutive_not_qualified_count == 1
    assert decision.updated_item.removal_candidate_since == _NOW


def test_second_failure_does_not_change_removal_candidate_since() -> None:
    since = _NOW - dt.timedelta(days=7)
    item = _item(
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=1,
        removal_candidate_since=since,
    )
    summary = _summary(passed=False)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.updated_item.consecutive_not_qualified_count == 2
    assert decision.updated_item.removal_candidate_since == since


# --- 開示リスク回帰テスト a/b/c: Bルート経由(即時削除ではない) --------------------


def test_a_disclosure_risk_single_failure_does_not_remove_immediately() -> None:
    """開示リスクキーワード検出は即時削除(Aルート)対象外であり、1回検出しても
    即削除しないこと(Bルート、3回連続+span_days経過が必要)。"""
    item = _item(created_at=_NOW - dt.timedelta(days=120))
    summary = _summary(
        passed=False, hard_exclusion_reasons=["開示情報にリスクキーワードを検出しました"]
    )
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.KEEP
    assert decision.updated_item.consecutive_not_qualified_count == 1


def test_b_disclosure_risk_three_consecutive_with_age_and_span_removes_via_b_route() -> None:
    """開示リスク3回連続+minimum_age_days+minimum_not_qualified_span_days経過→
    削除(Bルート経由、Aルートではない)。"""
    item = _item(
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=28),
    )
    summary = _summary(
        passed=False, hard_exclusion_reasons=["開示情報にリスクキーワードを検出しました"]
    )
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.CONSECUTIVE_NOT_QUALIFIED_REMOVAL


def test_c_disclosure_risk_two_failures_then_pass_resets() -> None:
    """開示リスク2回→次回PASS→カウント・removal_candidate_sinceともにリセット。"""
    item = _item(
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=14),
    )
    summary = _summary(passed=True)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.outcome == MaintenanceOutcome.KEEP
    assert decision.updated_item.consecutive_not_qualified_count == 0
    assert decision.updated_item.removal_candidate_since is None


# --- last_monitoring_score/last_matched_target_types/last_screening_resultの更新確認 ---


def test_updated_item_records_score_and_matched_types_on_pass() -> None:
    item = _item(created_at=_NOW - dt.timedelta(days=120))
    summary = _summary(passed=True, total_score=72.5, matched_target_types=["INCOME", "QUALITY"])
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.updated_item.last_monitoring_score == 72.5
    assert decision.updated_item.last_matched_target_types == ["INCOME", "QUALITY"]
    assert decision.updated_item.last_screening_result == "PASSED"
    assert decision.updated_item.last_screening_policy == "multi_style_monitoring"


def test_updated_item_records_failed_result_on_non_pass() -> None:
    item = _item(created_at=_NOW - dt.timedelta(days=120))
    summary = _summary(passed=False, total_score=30.0)
    decision = evaluate_maintenance_decision(item, summary, _CONFIG, _NOW)
    assert decision.updated_item.last_screening_result == "FAILED"
    assert decision.updated_item.last_monitoring_score == 30.0
