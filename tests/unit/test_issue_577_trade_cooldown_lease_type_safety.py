"""Issue #577(F-L7): trade_cooldown_service.pyのstale lock判定を、ISO文字列の
辞書順比較から型付き比較(normalize_to_aware_utc経由)へ変更したことの固定テスト。

TARO設計(#577 issuecomment、2026-09-27)により、対象は
`trade_cooldown_service.py:128`の1箇所のみ(残る3箇所はDynamoDB
ConditionExpressionによるサーバーサイド比較であり、Python側の型比較へ
変更できないため対象外)。

反証: 修正前の実装(`lease_expires_at < now.isoformat()`という文字列比較)は、
`lease_expires_at`が別のUTCオフセット表記(例: `+09:00`・`-01:00`)で保存
されている場合、**辞書順と実際の時系列順が一致しない**ことがある(offset
部分の文字自体が比較に混入するため)。これは「文字列としては比較できる
ためTypeErrorにはならないが、判定結果が表現方法に依存して誤る」という、
TypeErrorより発見しにくい誤りである。型付き比較(normalize_to_aware_utc
経由)へ変更することで、この表現依存を構造的に除去する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import TradeCooldownConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.infrastructure.aws import trade_detection_lock
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.trade_event_record_repository import (
    TradeEventRecordRepository,
)
from jstock_advisor.services.trade_cooldown_service import TradeCooldownService

_NOW = dt.datetime(2026, 8, 20, 23, 30, tzinfo=dt.UTC)  # 2026-08-21 08:30 JST(金)
_HID = build_holding_id(DEFAULT_OWNER, "2914")


def _config() -> TradeCooldownConfig:
    return TradeCooldownConfig(
        enabled=True, buy_business_days=5, sell_business_days=5, partial_trade_business_days=3
    )


def _calendar() -> BusinessCalendar:
    return BusinessCalendar.from_config(load_config().holiday_calendar)


def _unchanged_holdings() -> dict[str, Holding]:
    return {
        _HID: Holding(
            owner=DEFAULT_OWNER,
            holding_id=_HID,
            stock_code="2914",
            stock_name="架空銘柄",
            shares=100,
            average_purchase_price=Decimal("1000"),
            total_purchase_amount=Decimal("100000"),
            first_purchase_date=_NOW.date(),
            last_purchase_date=_NOW.date(),
            account_type=AccountType.SPECIFIC,
            created_at=_NOW,
            updated_at=_NOW,
        )
    }


def _service(tmp_path: Path) -> TradeCooldownService:
    snapshot_repo = HoldingsSnapshotRepository(store_dir=tmp_path)
    snapshot_repo.upsert(
        HoldingsSnapshotEntry(
            owner=DEFAULT_OWNER,
            holding_id=_HID,
            stock_code="2914",
            shares=100,
            average_purchase_price=Decimal("1000"),
            recorded_at=dt.date(2026, 8, 14),
            active_holding=True,
        )
    )
    return TradeCooldownService(
        business_calendar=_calendar(),
        config=_config(),
        repository=snapshot_repo,
        execution_context=ExecutionContext.normal(),
        trade_event_repository=TradeEventRecordRepository(store_dir=tmp_path),
    )


def test_cross_offset_expired_lease_is_taken_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lease_expires_atが(自身の実際の瞬間としては)期限切れであっても、
    naive判定の辞書順比較では「期限切れでない」と誤判定され得るケースで、
    型付き比較により正しくtake overされることを固定する。

    `2026-08-20T23:30:00+09:00`(UTC 14:30:00、_NOWの9時間前で明確に期限切れ)を
    `now.isoformat()`(`2026-08-20T23:30:00+00:00`)と辞書順比較すると、
    オフセット部分の"9" > "0"により**辞書順ではlease側が大きく**なり、
    旧実装(`lease_expires_at < now.isoformat()`という文字列比較)は
    「期限切れでない」と誤判定する(このテストはその旧実装では失敗する
    ことを意図したcounter-exampleである)。
    """
    jst = dt.timezone(dt.timedelta(hours=9))
    actually_expired_lease = dt.datetime(2026, 8, 20, 23, 30, 0, tzinfo=jst).isoformat()
    call_count = {"try_acquire": 0}

    def fake_try_acquire(business_date: str, now: dt.datetime, lease_seconds: int) -> bool:
        call_count["try_acquire"] += 1
        # 1回目は他実行が保持中のため失敗、take over成功時(2回目)はTrue。
        return call_count["try_acquire"] > 1

    def fake_get_status(business_date: str) -> tuple[str | None, str | None]:
        return trade_detection_lock.RunLockStatus.PROCESSING.value, actually_expired_lease

    def fake_mark_completed(business_date: str, leased_at_iso: str) -> None:
        return None

    monkeypatch.setattr(trade_detection_lock, "try_acquire", fake_try_acquire)
    monkeypatch.setattr(trade_detection_lock, "get_status", fake_get_status)
    monkeypatch.setattr(trade_detection_lock, "mark_completed", fake_mark_completed)

    service = _service(tmp_path)
    outcome = service.detect_and_apply(_unchanged_holdings(), _NOW)

    assert outcome.confirmed is True
    assert call_count["try_acquire"] == 2  # 1回目失敗 + take over成功


def test_cross_offset_not_yet_expired_lease_is_not_taken_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lease_expires_atが(自身の実際の瞬間としては)まだ期限切れでなくても、
    naive判定の辞書順比較では「期限切れ」と誤判定され得るケースで、型付き
    比較により正しくtake overされない(「常に期限切れ」という誤判定に
    ならないことの反証)ことを固定する。

    `_NOW + 5分`のUTC瞬間を`-01:00`オフセットで表現した
    `2026-08-20T22:35:00-01:00`は、`now.isoformat()`
    (`2026-08-20T23:30:00+00:00`)と辞書順比較すると時刻部分の"22"<"23"に
    より**辞書順ではlease側が小さく**なり、旧実装は「期限切れ」と誤判定する
    (このテストはその旧実装では失敗することを意図したcounter-exampleである)。
    """
    minus_one = dt.timezone(dt.timedelta(hours=-1))
    actually_not_yet_expired_lease = (
        (_NOW + dt.timedelta(minutes=5)).astimezone(minus_one).isoformat()
    )
    call_count = {"try_acquire": 0}

    def fake_try_acquire(business_date: str, now: dt.datetime, lease_seconds: int) -> bool:
        call_count["try_acquire"] += 1
        return False  # 常に他実行が保持中

    def fake_get_status(business_date: str) -> tuple[str | None, str | None]:
        return trade_detection_lock.RunLockStatus.PROCESSING.value, actually_not_yet_expired_lease

    monkeypatch.setattr(trade_detection_lock, "try_acquire", fake_try_acquire)
    monkeypatch.setattr(trade_detection_lock, "get_status", fake_get_status)
    monkeypatch.setattr(
        "jstock_advisor.services.trade_cooldown_service._BOUNDED_RETRY_INTERVAL_SECONDS", 0.0
    )

    service = _service(tmp_path)
    outcome = service.detect_and_apply(_unchanged_holdings(), _NOW)

    assert outcome.confirmed is False
    # try_acquireは最初の1回(ロック未保持時の通常取得試行)だけ呼ばれる。
    # leaseが未失効と正しく判定されれば、それ以上のreclaim試行(リトライ
    # ループ内のtry_acquire)は一度も行われない(短絡評価。`and`の条件式で
    # lease_expiredがFalseの場合try_acquireまで評価されない)。旧実装
    # (文字列比較)ではlease_expiredが誤ってTrueになり、リトライのたびに
    # 意味のないtry_acquireが呼ばれてしまう(合計6回: 最初の1回+リトライ5回)。
    assert call_count["try_acquire"] == 1
