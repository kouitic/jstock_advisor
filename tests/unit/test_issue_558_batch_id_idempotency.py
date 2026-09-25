"""Issue #558: EventBridge Schedulerのretryによるbuy_candidates/holdings_watchlist
のbatch二重dispatch防止のテスト。

#65 F-E7(watchlist_dispatcher_handler.py)と同型の欠陥(batch_idがretryごとに
再生成される)だが、こちらは既存のbatch開始処理(`start_batch()`)にlease機構が
一切無かったため、batch_id決定論化(`derive_scheduled_batch_id()`。#65 F-E7と
共有)に加えて`start_batch()`自体のatomicity化(ConditionExpression付きput_item)
が必要だった(Phase A整理: #558 issuecomment-5827435403、Phase B設計:
HANAKO-20260925-048)。

T1/T2/T3: `derive_scheduled_batch_id()`がbuy-candidates/holdings-watchlistの
prefixでも決定論的に動くこと(純粋関数本体の網羅的なテストは
test_issue_65_f_e7_batch_id_idempotency.pyが担うため、ここではprefix差分の
確認のみ)。
T4/T5: `start_batch()`のatomicity(初回True・item作成/2回目同一batch_idでFalse・
item上書きなし)。
T6/T7: handler()を同一scheduled_timeで2回呼び、2回目のfanoutが0件になること
(buy_candidates/holdings_watchlistそれぞれ)。
T8: duplicate時にERROR/例外が送出されないこと(T6/T7の戻り値そのもので確認)。
T9: 通常の初回実行は従来どおりfanoutすること(回帰)。
T10: scheduled_timeが無い経路(VALIDATION/manual/test)は従来どおりuuid付き
batch_idになること(回帰)。
T11: total=0(対象0件)の既存挙動(fanoutなし・duplicate扱いにしない)を
維持すること。
T12: 異なるscheduled_time(別batch_id)なら独立して開始できること。
"""

from __future__ import annotations

import datetime as dt

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.lambda_handlers import buy_candidates_handler, holdings_watchlist_handler
from jstock_advisor.lambda_handlers._scheduling import derive_scheduled_batch_id

_REGION = "ap-northeast-1"
_FAMILY = batch_tracker.BatchFamily.BUY_CANDIDATES
_CONTEXT = ExecutionContext.normal()

# 2026-07-29(水)はJPXの通常営業日(既存テストの_NOWと同じ日付。休場日gateで
# skipされないことが既存テスト群で確認済み)。
_SCHEDULED_TIME = "2026-07-28T23:00:00Z"  # JST 2026-07-29 08:00起動相当


# --- T1/T2/T3: derive_scheduled_batch_id()のprefix差分 -----------------------


def test_t1_buy_candidates_prefix_is_deterministic_for_the_same_scheduled_time() -> None:
    event = {"scheduled_time": _SCHEDULED_TIME}
    first_now = dt.datetime(2026, 7, 28, 23, 0, 5, tzinfo=dt.UTC)
    retry_now = dt.datetime(2026, 7, 28, 23, 15, 0, tzinfo=dt.UTC)

    first = derive_scheduled_batch_id("buy-candidates", event, first_now)
    retry = derive_scheduled_batch_id("buy-candidates", event, retry_now)

    assert first == retry
    assert first.startswith("buy-candidates-20260729T080000")


def test_t2_holdings_watchlist_prefix_is_deterministic_for_the_same_scheduled_time() -> None:
    event = {"scheduled_time": _SCHEDULED_TIME}
    first_now = dt.datetime(2026, 7, 28, 23, 0, 5, tzinfo=dt.UTC)
    retry_now = dt.datetime(2026, 7, 28, 23, 20, 0, tzinfo=dt.UTC)

    first = derive_scheduled_batch_id("holdings-watchlist", event, first_now)
    retry = derive_scheduled_batch_id("holdings-watchlist", event, retry_now)

    assert first == retry
    assert first.startswith("holdings-watchlist-20260729T080000")


def test_t3_a_different_scheduled_time_yields_a_different_batch_id() -> None:
    now = dt.datetime.now(dt.UTC)
    today = derive_scheduled_batch_id(
        "buy-candidates", {"scheduled_time": "2026-07-28T23:00:00Z"}, now
    )
    tomorrow = derive_scheduled_batch_id(
        "buy-candidates", {"scheduled_time": "2026-07-29T23:00:00Z"}, now
    )

    assert today != tomorrow


# --- T4/T5: start_batch()のatomicity -----------------------------------------


@pytest.fixture
def moto_batch_runs_dynamodb(monkeypatch: pytest.MonkeyPatch):
    """batch_tracker.running_on_lambda()だけをTrueへ差し替える(他モジュールの
    running_on_lambda()経由の分岐には影響しない。test_issue_65_f_e7_batch_id_
    idempotency.pyと同じ技法)。"""
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName="jstock-batch_runs",
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def test_t4_start_batch_first_call_acquires_and_creates_the_item(
    moto_batch_runs_dynamodb: None,
) -> None:
    now = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)

    acquired = batch_tracker.start_batch("batch-558-1", 3, now, _FAMILY, _CONTEXT)

    assert acquired is True
    progress = batch_tracker.record_result("batch-558-1", "sent")
    assert progress is not None  # itemが実際に作成されていることの間接確認


def test_t5_start_batch_second_call_with_the_same_batch_id_is_rejected_without_overwrite(
    moto_batch_runs_dynamodb: None,
) -> None:
    now = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)
    retry_now = dt.datetime(2026, 7, 29, 7, 2, 0, tzinfo=dt.UTC)

    first = batch_tracker.start_batch("batch-558-2", 3, now, _FAMILY, _CONTEXT)
    # 1件処理を進めてからretryが上書きしないことを確認する。
    batch_tracker.record_result("batch-558-2", "sent")
    second = batch_tracker.start_batch(
        "batch-558-2", 3, retry_now, _FAMILY, _CONTEXT, holding_count=99
    )

    assert first is True
    assert second is False
    # 上書きされていれば completed が 0 に戻る(新しいitemに置き換わるため)。
    progress = batch_tracker.record_result("batch-558-2", "sent")
    assert progress is not None
    assert progress.completed == 2  # 1回目のsent + この呼び出し分。上書きなら1になる


# --- T6/T7/T8/T9: handler()レベルのretry ------------------------------------


class _FakeBuyContext:
    function_name = "jstock-advisor-buy-candidates"


class _FakeHoldingsContext:
    function_name = "jstock-advisor-holdings-watchlist"


class _FakeTradeCooldownService:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def detect_and_apply(self, current_holdings: object, now: object) -> object:
        from jstock_advisor.services.trade_cooldown_service import TradeDetectionOutcome

        return TradeDetectionOutcome(confirmed=True, events=[])


def _patch_buy_candidates_common(monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    monkeypatch.setattr(
        buy_candidates_handler, "build_real_provider_bundle", lambda now, config: object()
    )
    monkeypatch.setattr(
        buy_candidates_handler, "build_line_client_for_run", lambda **kw: object()
    )
    monkeypatch.setattr(
        buy_candidates_handler,
        "build_stock_snapshot",
        lambda *a, **kw: (
            SimpleNamespace(
                current_price=Decimal("1000"),
                financial=SimpleNamespace(industry="Auto Parts", sector="Consumer Cyclical"),
                stock_type_classification=SimpleNamespace(types=[]),
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        buy_candidates_handler, "AuditService", lambda *a, **kw: _NoopAuditService()
    )
    monkeypatch.setattr(
        buy_candidates_handler, "TradeCooldownService", _FakeTradeCooldownService
    )
    monkeypatch.setattr(
        buy_candidates_handler,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc", (), {"notify_data_error": lambda self, *a, **kw: False}
        )(),
    )
    monkeypatch.setattr(buy_candidates_handler.WatchlistService, "list_items", lambda self: [])
    monkeypatch.setattr(
        buy_candidates_handler.PortfolioService, "list_holdings", lambda self: []
    )
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")


def _patch_holdings_watchlist_common(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeMarketData:
        def get_latest_price(self, stock_code: str) -> object | None:
            return None

    class _FakeProviders:
        market_data = _FakeMarketData()

    monkeypatch.setattr(
        holdings_watchlist_handler,
        "build_real_provider_bundle",
        lambda now, config: _FakeProviders(),
    )
    monkeypatch.setattr(
        holdings_watchlist_handler, "build_line_client_for_run", lambda **kw: object()
    )
    monkeypatch.setattr(
        holdings_watchlist_handler, "TradeCooldownService", _FakeTradeCooldownService
    )
    monkeypatch.setattr(
        holdings_watchlist_handler,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc",
            (),
            {
                "notify_data_error": lambda self, *a, **kw: False,
                "notify_recommendation": lambda self, *a, **kw: False,
            },
        )(),
    )
    monkeypatch.setattr(
        holdings_watchlist_handler.PortfolioService, "list_holdings", lambda self: []
    )


class _NoopAuditService:
    def record(self, *args: object, **kwargs: object) -> None:
        return None


def _watchlist_item(stock_code: str):
    from jstock_advisor.domain.entities.watchlist import WatchlistItem

    now = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)
    return WatchlistItem(
        stock_code=stock_code, stock_name=f"銘柄{stock_code}", created_at=now, updated_at=now
    )


def test_t6_buy_candidates_retry_with_the_same_scheduled_time_dispatches_nothing_the_second_time(
    moto_batch_runs_dynamodb: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_buy_candidates_common(monkeypatch)
    items = [_watchlist_item("2914"), _watchlist_item("8136")]
    monkeypatch.setattr(
        buy_candidates_handler.WatchlistService, "list_items", lambda self: items
    )

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        buy_candidates_handler,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    event = {"scheduled_time": _SCHEDULED_TIME}
    first_result = buy_candidates_handler.handler(event, _FakeBuyContext())
    first_dispatch_count = len(dispatched)
    dispatched.clear()

    retry_result = buy_candidates_handler.handler(event, _FakeBuyContext())

    assert first_result == {"dispatched": 2}
    assert first_dispatch_count == 2
    # T7/T9(初回は従来どおりfanoutする)の再確認を兼ねる。
    assert retry_result == {"dispatched": 0, "skipped": "duplicate_batch_start"}
    assert dispatched == []  # T8: 2回目はfanoutしない(ERRORでもない)


def test_t7_holdings_watchlist_retry_with_the_same_scheduled_time_dispatches_nothing_the_second_time(  # noqa: E501
    moto_batch_runs_dynamodb: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decimal import Decimal

    from jstock_advisor.domain.entities.enums import AccountType
    from jstock_advisor.domain.entities.holding import Holding
    from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id

    _patch_holdings_watchlist_common(monkeypatch)

    def _holding(stock_code: str) -> Holding:
        now = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)
        return Holding(
            owner=DEFAULT_OWNER,
            holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
            stock_code=stock_code,
            stock_name=f"銘柄{stock_code}",
            shares=100,
            average_purchase_price=Decimal("1000"),
            total_purchase_amount=Decimal("100000"),
            first_purchase_date=dt.date(2024, 1, 1),
            last_purchase_date=dt.date(2024, 1, 1),
            account_type=AccountType.SPECIFIC,
            created_at=now,
            updated_at=now,
        )

    holdings = [_holding("2914"), _holding("8136")]
    monkeypatch.setattr(
        holdings_watchlist_handler.PortfolioService, "list_holdings", lambda self: holdings
    )

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        holdings_watchlist_handler,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    event = {"scheduled_time": _SCHEDULED_TIME}
    first_result = holdings_watchlist_handler.handler(event, _FakeHoldingsContext())
    first_dispatch_count = len(dispatched)
    dispatched.clear()

    retry_result = holdings_watchlist_handler.handler(event, _FakeHoldingsContext())

    assert first_result == {"dispatched_holdings": 2}
    assert first_dispatch_count == 2
    assert retry_result == {"dispatched_holdings": 0, "skipped": "duplicate_batch_start"}
    assert dispatched == []  # T8: 2回目はfanoutしない(ERRORでもない)


# --- T10: scheduled_timeが無い経路(VALIDATION/manual/test)は従来どおり ------


def test_t10_manual_invocation_without_scheduled_time_still_uses_a_random_suffix_batch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """running_on_lambda()=False(既存テスト群と同じローカル実行)のため
    start_batch()はno-opでTrueを返す。batch_id自体がscheduled_time無しの
    fallback(時刻+乱数)のままであることのみ確認する(回帰)。"""
    _patch_buy_candidates_common(monkeypatch)
    items = [_watchlist_item("2914")]
    monkeypatch.setattr(
        buy_candidates_handler.WatchlistService, "list_items", lambda self: items
    )

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        buy_candidates_handler,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    result = buy_candidates_handler.handler({}, _FakeBuyContext())

    assert result == {"dispatched": 1}
    batch_id = dispatched[0]["batch_id"]
    assert isinstance(batch_id, str)
    assert batch_id.startswith("buy-candidates-")
    # fallback形式: buy-candidates-<time>-<hex8> (scheduled_time由来の形式には
    # ランダムサフィックスが付かない)。
    assert len(batch_id.rsplit("-", 1)[-1]) == 8


# --- T11: total=0の既存挙動 ---------------------------------------------------


def test_t11_zero_targets_dispatches_nothing_and_is_not_treated_as_duplicate(
    moto_batch_runs_dynamodb: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_buy_candidates_common(monkeypatch)
    # WatchlistService.list_items/PortfolioService.list_holdingsとも
    # _patch_buy_candidates_commonの既定で空リストのため、targets=0件になる。

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        buy_candidates_handler,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    event = {"scheduled_time": _SCHEDULED_TIME}
    result = buy_candidates_handler.handler(event, _FakeBuyContext())

    # total<=0の場合、start_batch()は無条件Trueを返すため、duplicate skipには
    # ならず、従来どおりdispatched=0のまま正常終了する。
    assert result == {"dispatched": 0}
    assert dispatched == []


# --- T12: 異なるscheduled_time(別batch_id)は独立して開始できる ---------------


def test_t12_a_different_scheduled_time_starts_independently(
    moto_batch_runs_dynamodb: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_buy_candidates_common(monkeypatch)
    items = [_watchlist_item("2914")]
    monkeypatch.setattr(
        buy_candidates_handler.WatchlistService, "list_items", lambda self: items
    )

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        buy_candidates_handler,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    first_result = buy_candidates_handler.handler(
        {"scheduled_time": "2026-07-28T23:00:00Z"}, _FakeBuyContext()
    )
    second_result = buy_candidates_handler.handler(
        {"scheduled_time": "2026-07-29T23:00:00Z"}, _FakeBuyContext()
    )

    assert first_result == {"dispatched": 1}
    assert second_result == {"dispatched": 1}  # 別batch_idのため拒否されない
    assert len(dispatched) == 2
    assert dispatched[0]["batch_id"] != dispatched[1]["batch_id"]
