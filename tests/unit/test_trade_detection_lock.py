"""TradeDetectionRunLockの状態管理(PROCESSING/COMPLETED)のテスト(BUY候補裾野拡大機能2026-08)。

実際のDynamoDBのConditionExpression意味論(attribute_not_exists・比較演算子)を
最小限模倣したフェイクテーブルを使う(test_batch_tracker.pyと同じ方針)。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from botocore.exceptions import ClientError

from jstock_advisor.infrastructure.aws import trade_detection_lock

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)  # 月曜


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

    repo.upsert(
        HoldingsSnapshotEntry(
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

    return {
        stock_code: Holding(
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
    assert validation_repo.get("2914") is not None
    assert validation_repo.get("2914").cooldown_until_date is not None
    assert normal_repo.get("2914") is not None
    assert normal_repo.get("2914").cooldown_until_date is not None


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
    assert normal_repo.get("2914").shares == 100
    assert validation_repo.get("2914").shares == 0
