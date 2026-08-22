"""TradeDetectionRunLockの状態管理(PROCESSING/COMPLETED)のテスト(BUY候補裾野拡大機能2026-08)。

実際のDynamoDBのConditionExpression意味論(attribute_not_exists・比較演算子)を
最小限模倣したフェイクテーブルを使う(test_batch_tracker.pyと同じ方針)。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.infrastructure.aws import trade_detection_lock

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)  # 月曜
_HID_2914 = build_holding_id(DEFAULT_OWNER, "2914")


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]["business_date"]
        condition = kwargs.get("ConditionExpression")
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.get(key, {})

        if condition == (
            "attribute_not_exists(#status) OR "
            "(#status = :processing AND lease_expires_at < :now)"
        ):
            ok = "status" not in item or (
                item.get("status") == values[":processing"]
                and item.get("lease_expires_at", "") < values[":now"]
            )
            if not ok:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                    "UpdateItem",
                )
            item.update(
                {
                    "status": values[":processing"],
                    "leased_at": values[":now"],
                    "lease_expires_at": values[":expires"],
                    "ttl": values[":ttl"],
                }
            )
        elif condition == "leased_at = :leased_at":
            if item.get("leased_at") != values[":leased_at"]:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                    "UpdateItem",
                )
            item["status"] = values[":completed"]
        else:
            raise AssertionError(f"unexpected condition: {condition}")

        self.items[key] = item
        return {"Attributes": dict(item)}

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        item = self.items.get(Key["business_date"])
        return {"Item": item} if item is not None else {}


class _FakeResource:
    def __init__(self, table: _FakeTable) -> None:
        self._table = table

    def Table(self, name: str) -> _FakeTable:  # noqa: N802
        return self._table


@pytest.fixture
def fake_table_on_lambda(monkeypatch: pytest.MonkeyPatch) -> _FakeTable:
    monkeypatch.setattr(trade_detection_lock, "running_on_lambda", lambda: True)
    table = _FakeTable()
    resource_factory = lambda *a, **kw: _FakeResource(table)  # noqa: E731
    monkeypatch.setattr(trade_detection_lock.boto3, "resource", resource_factory)
    return table


def test_local_env_always_acquires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trade_detection_lock, "running_on_lambda", lambda: False)
    assert trade_detection_lock.try_acquire("2026-08-17", _NOW, 60) is True


def test_first_acquire_succeeds_on_lambda(fake_table_on_lambda: _FakeTable) -> None:
    assert trade_detection_lock.try_acquire("2026-08-17", _NOW, 60) is True


def test_second_acquire_fails_while_processing_and_not_expired(
    fake_table_on_lambda: _FakeTable,
) -> None:
    assert trade_detection_lock.try_acquire("2026-08-17", _NOW, 60) is True
    later = _NOW + dt.timedelta(seconds=10)
    assert trade_detection_lock.try_acquire("2026-08-17", later, 60) is False


def test_stale_lock_can_be_recovered_after_lease_expires(
    fake_table_on_lambda: _FakeTable,
) -> None:
    assert trade_detection_lock.try_acquire("2026-08-17", _NOW, 60) is True
    much_later = _NOW + dt.timedelta(seconds=120)  # lease(60秒)失効後
    assert trade_detection_lock.try_acquire("2026-08-17", much_later, 60) is True


def test_mark_completed_succeeds_with_matching_lease(fake_table_on_lambda: _FakeTable) -> None:
    trade_detection_lock.try_acquire("2026-08-17", _NOW, 60)
    trade_detection_lock.mark_completed("2026-08-17", leased_at_iso=_NOW.isoformat())
    status, _ = trade_detection_lock.get_status("2026-08-17")
    assert status == trade_detection_lock.RunLockStatus.COMPLETED.value


def test_mark_completed_noop_when_lease_was_taken_over(fake_table_on_lambda: _FakeTable) -> None:
    """自分が取得したリース(leased_at)と一致しない場合は上書きしない
    (先行Lambdaが異常終了しstale lockが別のLambdaに奪取された後のケース)。"""
    trade_detection_lock.try_acquire("2026-08-17", _NOW, 60)
    # 別の(架空の)leased_atでmark_completedを試みる → 一致しないため例外を吸収し何もしない
    trade_detection_lock.mark_completed("2026-08-17", leased_at_iso="1999-01-01T00:00:00")
    status, _ = trade_detection_lock.get_status("2026-08-17")
    assert status == trade_detection_lock.RunLockStatus.PROCESSING.value


def test_get_status_returns_none_when_no_entry(fake_table_on_lambda: _FakeTable) -> None:
    status, expires = trade_detection_lock.get_status("2026-08-17")
    assert status is None
    assert expires is None


# ============================================================================
# TradeCooldownService: NORMAL/VALIDATIONのロック名前空間分離(コードレビュー
# 対応2026-08、指摘4)。VALIDATIONが先に完了しても、NORMALの検知処理自体が
# スキップされない(=snapshotが更新されない)ことを実際のdetect_and_apply()
# 経由で確認する(単にロック関数だけを直接呼ぶのではなく、E2Eで検証する)。
# ============================================================================


def _seed_baseline(repo: Any, stock_code: str, shares: int) -> None:
    from decimal import Decimal

    from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
    from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id

    repo.upsert(
        HoldingsSnapshotEntry(
            owner=DEFAULT_OWNER,
            holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
            stock_code=stock_code,
            shares=shares,
            average_purchase_price=Decimal("1000") if shares > 0 else None,
            recorded_at=dt.date(2026, 8, 14),
            active_holding=shares > 0,
        )
    )


def _current_holdings(stock_code: str, shares: int) -> dict[str, Any]:
    from decimal import Decimal

    from jstock_advisor.domain.entities.enums import AccountType
    from jstock_advisor.domain.entities.holding import Holding
    from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id

    holding_id = build_holding_id(DEFAULT_OWNER, stock_code)
    return {
        holding_id: Holding(
            owner=DEFAULT_OWNER,
            holding_id=holding_id,
            stock_code=stock_code,
            stock_name=f"銘柄{stock_code}",
            shares=shares,
            average_purchase_price=Decimal("1000"),
            total_purchase_amount=Decimal("1000") * shares,
            first_purchase_date=_NOW.date(),
            last_purchase_date=_NOW.date(),
            account_type=AccountType.SPECIFIC,
            created_at=_NOW,
            updated_at=_NOW,
        )
    }


def _build_cooldown_services(tmp_path: Any) -> tuple[Any, Any, Any, Any]:
    from jstock_advisor.config.loader import load_config
    from jstock_advisor.config.models import TradeCooldownConfig
    from jstock_advisor.domain.business_calendar import BusinessCalendar
    from jstock_advisor.domain.entities.enums import ExecutionMode
    from jstock_advisor.domain.entities.execution_context import ExecutionContext
    from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
        HoldingsSnapshotRepository,
    )
    from jstock_advisor.services.trade_cooldown_service import TradeCooldownService

    calendar = BusinessCalendar.from_config(load_config().holiday_calendar)
    config = TradeCooldownConfig(
        enabled=True, buy_business_days=5, sell_business_days=5, partial_trade_business_days=3
    )
    normal_repo = HoldingsSnapshotRepository(
        store_dir=tmp_path, file_name="holdings_snapshots.json"
    )
    validation_repo = HoldingsSnapshotRepository(
        store_dir=tmp_path, file_name="validation_holdings_snapshots.json"
    )
    normal_service = TradeCooldownService(
        business_calendar=calendar,
        config=config,
        repository=normal_repo,
        execution_context=ExecutionContext.normal(),
    )
    validation_service = TradeCooldownService(
        business_calendar=calendar,
        config=config,
        repository=validation_repo,
        execution_context=ExecutionContext(mode=ExecutionMode.VALIDATION),
    )
    return normal_service, validation_service, normal_repo, validation_repo


def test_validation_detection_first_does_not_block_normal_detection(
    fake_table_on_lambda: _FakeTable, tmp_path: Any
) -> None:
    """VALIDATIONを先に実行してロックをCOMPLETEDにした後、NORMALが
    (VALIDATIONのCOMPLETEDを自分の完了と誤認せず)独自に検知処理を実行する
    ことを確認する。"""
    normal_service, validation_service, normal_repo, validation_repo = _build_cooldown_services(
        tmp_path
    )
    _seed_baseline(normal_repo, "2914", shares=0)
    _seed_baseline(validation_repo, "2914", shares=0)
    current_holdings = _current_holdings("2914", shares=100)

    validation_outcome = validation_service.detect_and_apply(current_holdings, _NOW)
    normal_outcome = normal_service.detect_and_apply(current_holdings, _NOW)

    assert validation_outcome.confirmed is True
    assert normal_outcome.confirmed is True
    # 名前空間分離前(不具合時)は、NORMALがVALIDATIONのCOMPLETEDを自分の完了と
    # 誤認し、検知処理自体をスキップして空のevents・snapshot未更新のままになる。
    assert len(validation_outcome.events) == 1
    assert len(normal_outcome.events) == 1
    assert validation_repo.get(_HID_2914) is not None
    assert validation_repo.get(_HID_2914).cooldown_until_date is not None
    assert normal_repo.get(_HID_2914) is not None
    assert normal_repo.get(_HID_2914).cooldown_until_date is not None


def test_normal_detection_first_does_not_block_validation_detection(
    fake_table_on_lambda: _FakeTable, tmp_path: Any
) -> None:
    """NORMALを先に実行後も、VALIDATIONが独自に検知処理を実行することを確認する
    (逆順でも名前空間分離が機能する)。"""
    normal_service, validation_service, normal_repo, validation_repo = _build_cooldown_services(
        tmp_path
    )
    _seed_baseline(normal_repo, "2914", shares=0)
    _seed_baseline(validation_repo, "2914", shares=0)
    current_holdings = _current_holdings("2914", shares=100)

    normal_outcome = normal_service.detect_and_apply(current_holdings, _NOW)
    validation_outcome = validation_service.detect_and_apply(current_holdings, _NOW)

    assert normal_outcome.confirmed is True
    assert validation_outcome.confirmed is True
    assert len(normal_outcome.events) == 1
    assert len(validation_outcome.events) == 1


def test_normal_and_validation_snapshots_do_not_cross_contaminate(
    fake_table_on_lambda: _FakeTable, tmp_path: Any
) -> None:
    """NORMAL側で検知したBUYイベントが、VALIDATION側のholdings snapshotへ
    書き込まれないこと(逆も同様)を確認する。"""
    normal_service, validation_service, normal_repo, validation_repo = _build_cooldown_services(
        tmp_path
    )
    _seed_baseline(normal_repo, "2914", shares=0)
    _seed_baseline(validation_repo, "2914", shares=0)
    current_holdings = _current_holdings("2914", shares=100)

    normal_service.detect_and_apply(current_holdings, _NOW)

    # NORMAL側は更新されるが、VALIDATION側は無関係(まだshares=0のまま)。
    assert normal_repo.get(_HID_2914).shares == 100
    assert validation_repo.get(_HID_2914).shares == 0


# ============================================================================
# 再々コードレビュー対応(2026-08、JST暦日境界修正・指摘1〜3): TradeCooldownの
# 基準日が「生成側=UTC暦日」「比較側=JST暦日」で不整合になっていた不備の修正。
# TradeDetection lock keyもJST暦日基準へ統一する(指摘2)。
# ============================================================================

# 2026-08-21 08:30 JST(金)。2026-08-20 23:30 UTC。
_FRIDAY_08_30_JST = dt.datetime(2026, 8, 20, 23, 30, tzinfo=dt.UTC)
# 2026-08-21 09:10 JST(金、上記と同一JST日だがUTC暦日は異なる)。
_FRIDAY_09_10_JST = dt.datetime(2026, 8, 21, 0, 10, tzinfo=dt.UTC)


def test_td_a_same_jst_day_different_utc_date_shares_lock_key(
    fake_table_on_lambda: _FakeTable, tmp_path: Any
) -> None:
    """TD-A: 同一JST日(2026-08-21)だがUTC日付が異なる2時刻(08:30 JST/09:10 JST)
    でも、TradeDetection lock keyが同一(NORMAL:2026-08-21)になること。"""
    normal_service, _validation_service, normal_repo, _validation_repo = _build_cooldown_services(
        tmp_path
    )
    _seed_baseline(normal_repo, "2914", shares=100)
    current_holdings = _current_holdings("2914", shares=50)  # 一部売却

    first_outcome = normal_service.detect_and_apply(current_holdings, _FRIDAY_08_30_JST)
    assert first_outcome.confirmed is True
    assert "NORMAL:2026-08-21" in fake_table_on_lambda.items
    assert len(first_outcome.events) == 1

    # 同一JST日の別UTC時刻: 同じlock keyのためCOMPLETED済みとして扱われ、
    # 検知処理自体は再実行されない(=同一の基準日として扱われている証拠)。
    second_outcome = normal_service.detect_and_apply(current_holdings, _FRIDAY_09_10_JST)
    assert second_outcome.confirmed is True
    assert second_outcome.events == []
    assert len(fake_table_on_lambda.items) == 1  # 別keyが新規作成されていない


def test_td_b_next_jst_day_gets_a_separate_lock_key(
    fake_table_on_lambda: _FakeTable, tmp_path: Any
) -> None:
    """TD-B: 翌JST日は別のlock key(NORMAL:2026-08-22)になること。"""
    normal_service, _validation_service, normal_repo, _validation_repo = _build_cooldown_services(
        tmp_path
    )
    _seed_baseline(normal_repo, "2914", shares=100)
    current_holdings = _current_holdings("2914", shares=50)

    normal_service.detect_and_apply(current_holdings, _FRIDAY_08_30_JST)
    next_day = _FRIDAY_08_30_JST + dt.timedelta(days=1)  # 2026-08-22 08:30 JST(土)
    normal_service.detect_and_apply(current_holdings, next_day)

    assert "NORMAL:2026-08-21" in fake_table_on_lambda.items
    assert "NORMAL:2026-08-22" in fake_table_on_lambda.items


def test_td_c_normal_and_validation_namespaces_stay_separated_under_jst_key(
    fake_table_on_lambda: _FakeTable, tmp_path: Any
) -> None:
    """TD-C: JST暦日key化後もNORMAL/VALIDATIONの名前空間分離は維持される
    (NORMAL:2026-08-21とVALIDATION:2026-08-21は別ロックとして扱われる)。"""
    normal_service, validation_service, normal_repo, validation_repo = _build_cooldown_services(
        tmp_path
    )
    _seed_baseline(normal_repo, "2914", shares=0)
    _seed_baseline(validation_repo, "2914", shares=0)
    current_holdings = _current_holdings("2914", shares=100)

    normal_outcome = normal_service.detect_and_apply(current_holdings, _FRIDAY_08_30_JST)
    validation_outcome = validation_service.detect_and_apply(current_holdings, _FRIDAY_08_30_JST)

    assert normal_outcome.confirmed is True
    assert validation_outcome.confirmed is True
    assert len(normal_outcome.events) == 1
    assert len(validation_outcome.events) == 1
    assert "NORMAL:2026-08-21" in fake_table_on_lambda.items
    assert "VALIDATION:2026-08-21" in fake_table_on_lambda.items


def _build_line_notification_service(tmp_path: Any, holdings_snapshot_repo: Any) -> Any:
    from jstock_advisor.config.loader import load_config
    from jstock_advisor.infrastructure.local_repository.daily_notification_priority_repository import (  # noqa: E501
        DailyNotificationPriorityRepository,
    )
    from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
        NotificationLogRepository,
    )
    from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
        RecommendationRepository,
    )
    from jstock_advisor.services.line_notification_service import LineNotificationService

    class _FakeLineClient:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def push_message(self, text: str) -> None:
            self.sent.append(text)

    return LineNotificationService(
        line_client=_FakeLineClient(),
        notification_log_repository=NotificationLogRepository(store_dir=tmp_path),
        recommendation_repository=RecommendationRepository(store_dir=tmp_path),
        config=load_config(),
        holdings_snapshot_repository=holdings_snapshot_repo,
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=tmp_path
        ),
    )


def _partial_sell_recommendation(now: dt.datetime) -> Any:
    from decimal import Decimal

    from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
    from jstock_advisor.domain.entities.recommendation import Recommendation

    return Recommendation(
        recommendation_id="tc-e2e-partial-sell",
        stock_code="2914",
        stock_name="テスト銘柄",
        recommended_at=now,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _run_tc_e2e_scenario(fake_table_on_lambda: _FakeTable, tmp_path: Any, now: dt.datetime) -> Any:
    """TradeCooldownService.detect_and_apply()(生成側)→check_trade_cooldown_
    eligibility()(比較側)を実際に一連で通す(受入条件・TC-E/F)。"""
    normal_service, _validation_service, normal_repo, _validation_repo = _build_cooldown_services(
        tmp_path
    )
    _seed_baseline(normal_repo, "2914", shares=100)
    current_holdings = _current_holdings("2914", shares=50)  # 一部売却検出

    outcome = normal_service.detect_and_apply(current_holdings, now)
    assert outcome.confirmed is True
    assert len(outcome.events) == 1

    entry = normal_repo.get(_HID_2914)
    assert entry is not None
    assert entry.cooldown_until_date is not None

    notification_service = _build_line_notification_service(tmp_path, normal_repo)
    return entry.cooldown_until_date, notification_service


def test_tc_e_generation_to_comparison_e2e_at_08_30_jst(
    fake_table_on_lambda: _FakeTable, tmp_path: Any
) -> None:
    """TC-E(最重要受入条件): 2026-08-21 08:30 JST(2026-08-20 23:30 UTC)に
    PARTIAL_SELLを検出(partial_trade_business_days=3)。
    cooldown_until_date=2026-08-26(水、土日を挟んで3営業日後)。
    8/26 JSTはクールダウン中、8/27 JSTで解除されること。"""
    cooldown_until_date, notification_service = _run_tc_e2e_scenario(
        fake_table_on_lambda, tmp_path, _FRIDAY_08_30_JST
    )
    assert cooldown_until_date == dt.date(2026, 8, 26)

    rec = _partial_sell_recommendation(_FRIDAY_08_30_JST)
    still_in_cooldown = dt.datetime(2026, 8, 25, 23, 0, tzinfo=dt.UTC)  # 2026-08-26 08:00 JST
    released = dt.datetime(2026, 8, 26, 23, 0, tzinfo=dt.UTC)  # 2026-08-27 08:00 JST

    eligibility_during = notification_service.check_trade_cooldown_eligibility(
        rec, still_in_cooldown
    )
    eligibility_after = notification_service.check_trade_cooldown_eligibility(rec, released)

    assert eligibility_during.eligible is False
    assert eligibility_during.block_reason == "TRADE_COOLDOWN"
    assert eligibility_after.eligible is True


def test_tc_f_generation_to_comparison_e2e_at_09_10_jst_matches_tc_e(
    fake_table_on_lambda: _FakeTable, tmp_path: Any
) -> None:
    """TC-F: 同じ条件を2026-08-21 09:10 JST(2026-08-21 00:10 UTC、UTC暦日は
    TC-Eと異なる)で実行しても、TradeDetection business date・cooldown_until_date・
    eligibility判定がTC-Eと完全に一致すること。"""
    cooldown_until_date, notification_service = _run_tc_e2e_scenario(
        fake_table_on_lambda, tmp_path, _FRIDAY_09_10_JST
    )
    assert cooldown_until_date == dt.date(2026, 8, 26)  # TC-Eと同一

    rec = _partial_sell_recommendation(_FRIDAY_09_10_JST)
    still_in_cooldown = dt.datetime(2026, 8, 25, 23, 0, tzinfo=dt.UTC)  # 2026-08-26 08:00 JST
    released = dt.datetime(2026, 8, 26, 23, 0, tzinfo=dt.UTC)  # 2026-08-27 08:00 JST

    eligibility_during = notification_service.check_trade_cooldown_eligibility(
        rec, still_in_cooldown
    )
    eligibility_after = notification_service.check_trade_cooldown_eligibility(rec, released)

    assert eligibility_during.eligible is False
    assert eligibility_after.eligible is True


def test_tc_g_weekend_is_not_counted_as_business_day() -> None:
    """TC-G: 金曜(2026-08-21)基準+1営業日は、土日(8/22・8/23)を挟んで
    月曜(8/24)になること(TC-E/Fの3営業日計算にも同じ土日跨ぎが含まれるが、
    ここでは1営業日のみに絞って単純に確認する。BusinessCalendarの休日設定
    自体は変更していない)。"""
    from jstock_advisor.config.loader import load_config
    from jstock_advisor.domain.business_calendar import BusinessCalendar

    calendar = BusinessCalendar.from_config(load_config().holiday_calendar)
    assert calendar.add_business_days(dt.date(2026, 8, 21), 1) == dt.date(2026, 8, 24)


def test_tc_h_national_holiday_is_not_counted_as_business_day(
    fake_table_on_lambda: _FakeTable, tmp_path: Any
) -> None:
    """TC-H: 2026-08-11(火)は「山の日」(祝日)のため、月曜(8/10)基準+3営業日は
    祝日を挟んで金曜(8/14)になること(祝日を1営業日として数えていれば8/13になる。
    既存のBusinessCalendar休日判定(jpholiday)自体は変更していない)。"""
    from jstock_advisor.config.loader import load_config
    from jstock_advisor.domain.business_calendar import BusinessCalendar

    calendar = BusinessCalendar.from_config(load_config().holiday_calendar)
    assert calendar.is_business_day(dt.date(2026, 8, 11)) is False  # 山の日
    assert calendar.add_business_days(dt.date(2026, 8, 10), 3) == dt.date(2026, 8, 14)
