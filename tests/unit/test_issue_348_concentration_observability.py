"""Issue #348: 集中度判定の発火件数を数える手段が無く、#329の本番検証が
構造的に未了のままになる問題への対策。

`evaluate_household_concentration_and_notify()`終了時に、判定した銘柄数・
発火件数・判定不能件数(株価取得失敗)をログへ1行出す。0件の日も必ず出すことで
「出力が無い」と「0件だった」を区別できるようにする(受入条件1・3)。
銘柄コード・owner・holding_id・数量・単価・評価額は出さない(受入条件2)。

fixtureは架空値のみ。銘柄コードは割り当てが存在しない"0000"/"0001"を使う。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import AccountType, SourceType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.interfaces.types import PriceSnapshot
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module
from jstock_advisor.services.line_notification_service import (
    NotificationOutcome,
    NotificationStatus,
)

_STOCK_A = "0000"
_STOCK_B = "0001"
# ★ 割り当てが存在しないコードをさらに4つ使い、6銘柄均等(各16.7%)で
# 全銘柄が閾値20%未満になるfixtureを作る(_STOCK_A/_Bと衝突しない)。
_EVEN_STOCK_CODES = ["0000", "0001", "0002", "0003", "0004", "0005"]
_OWNER_A = "所有者A"
_NOW = dt.datetime(2026, 6, 30, 9, 0, tzinfo=dt.UTC)
_FETCHED_AT = dt.datetime(2026, 6, 30, 8, 55, tzinfo=dt.UTC)

_SUMMARY_MESSAGE_PREFIX = "portfolio concentration summary"


def _holding(owner: str, stock_code: str, shares: int, cost: str) -> Holding:
    return Holding(
        owner=owner,
        holding_id=f"{owner}#{stock_code}",
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=shares,
        average_purchase_price=Decimal(cost) / shares,
        total_purchase_amount=Decimal(cost),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _price(stock_code: str, close: str) -> PriceSnapshot:
    return PriceSnapshot(
        stock_code=stock_code,
        as_of_date=dt.date(2026, 6, 30),
        close_price=Decimal(close),
        source=DataSourceReference(
            provider="test-market-data",
            fetched_at=_FETCHED_AT,
            source_type=SourceType.CONTRACTED_PROVIDER,
        ),
    )


class _FakeMarketData:
    def __init__(self, prices: dict[str, PriceSnapshot]) -> None:
        self._prices = prices

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        return self._prices.get(stock_code)


class _FakeProviders:
    def __init__(self, market_data: _FakeMarketData) -> None:
        self.market_data = market_data


class _SpyRepo:
    def __init__(self) -> None:
        self.saved: list[object] = []

    def save(self, recommendation: object) -> None:
        self.saved.append(recommendation)


class _SpyNotificationService:
    def notify_recommendation_with_status(
        self, recommendation: object, now: dt.datetime
    ) -> NotificationOutcome:
        return NotificationOutcome(status=NotificationStatus.SENT, sent=True)


class _FakeRuleVersionService:
    def get_active_version_or(self, default: str) -> str:
        return "rule-v-test"


def _run(holdings: list[Holding], prices: dict[str, PriceSnapshot]) -> None:
    market = _FakeMarketData(prices)
    providers = _FakeProviders(market)
    handler_module.evaluate_household_concentration_and_notify(
        holdings,
        providers,
        handler_module.load_config(),
        _SpyRepo(),
        _SpyNotificationService(),
        _FakeRuleVersionService(),
        _NOW,
        True,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
    )


def _run_and_capture_summary(
    caplog: pytest.LogCaptureFixture, holdings: list[Holding], prices: dict[str, PriceSnapshot]
) -> logging.LogRecord:
    with caplog.at_level("INFO"):
        _run(holdings, prices)
    matches = [r for r in caplog.records if r.message.startswith(_SUMMARY_MESSAGE_PREFIX)]
    assert len(matches) == 1, f"expected exactly one summary log, got {len(matches)}"
    return matches[0]


# --- 受入条件3: 発火0件の日も判定数と0件が出る -------------------------------------------


def test_summary_logged_even_when_nothing_triggers(caplog: pytest.LogCaptureFixture) -> None:
    """★★ 誰も閾値を超えない日でも、サマリー行は必ず1回出る。

    「出力が無い」と「発火0件だった」を区別できることの直接固定。
    """
    # 6銘柄均等(各16.7%)。時価・取得価格どちらのbasisでも閾値20%未満。
    holdings = [_holding(_OWNER_A, code, 100, "100000") for code in _EVEN_STOCK_CODES]
    prices = {code: _price(code, "1000") for code in _EVEN_STOCK_CODES}

    record = _run_and_capture_summary(caplog, holdings, prices)

    assert "evaluated=6" in record.message
    assert "triggered=0" in record.message
    assert "unjudgeable=0" in record.message


# --- 受入条件1: 判定数・発火件数・判定不能件数がそれぞれ正しい ---------------------------


def test_summary_counts_triggered_position_when_price_available(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ 閾値を超えかつ株価が取れた銘柄は triggered としてのみ数える。

    A=85%(閾値超え) / B=15%(閾値未満)。時価・取得価格の両basisで
    比率が一致するよう、株価は取得単価と同額にしている。
    """
    holdings = [
        _holding(_OWNER_A, _STOCK_A, 100, "850000"),
        _holding(_OWNER_A, _STOCK_B, 100, "150000"),
    ]
    prices = {_STOCK_A: _price(_STOCK_A, "8500"), _STOCK_B: _price(_STOCK_B, "1500")}

    record = _run_and_capture_summary(caplog, holdings, prices)

    assert "evaluated=2" in record.message
    assert "triggered=1" in record.message
    assert "unjudgeable=0" in record.message


def test_summary_counts_unjudgeable_when_own_price_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★★ 閾値を超えたが対象銘柄自身の価格が取れない場合は unjudgeable として数える
    (triggeredには含めない。Recommendationが作られていないため)。

    取得価格ベースの判定は株価に依存しないため、A(85%)はAの価格が
    無くてもis_concentrated=Trueになる(M-1)。Bは取得価格ベースで15%
    (閾値未満)、時価ベースはAの価格欠落により全体が算出不能でNoneになる
    ため、Bはis_concentrated=Falseのまま(triggeredにもunjudgeableにも
    数えない)。
    """
    holdings = [
        _holding(_OWNER_A, _STOCK_A, 100, "850000"),
        _holding(_OWNER_A, _STOCK_B, 100, "150000"),
    ]
    prices = {_STOCK_B: _price(_STOCK_B, "1500")}  # _STOCK_Aの価格は取れない

    record = _run_and_capture_summary(caplog, holdings, prices)

    assert "evaluated=2" in record.message
    assert "triggered=0" in record.message
    assert "unjudgeable=1" in record.message


# --- 受入条件2: 銘柄コード・owner等の機微情報を含まない ----------------------------------


def test_summary_log_does_not_leak_stock_code_or_owner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★★ サマリー行は件数のみで、銘柄コード・owner名を含まない。"""
    holdings = [_holding(_OWNER_A, _STOCK_A, 100, "240000")]
    prices = {_STOCK_A: _price(_STOCK_A, "2400")}

    record = _run_and_capture_summary(caplog, holdings, prices)

    assert _STOCK_A not in record.message
    assert _OWNER_A not in record.message
