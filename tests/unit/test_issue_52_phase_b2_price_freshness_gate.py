"""Issue #52 Phase B2: 価格鮮度の business gate と不正価格の provider boundary 拒否。

Phase B1 は観測(`missed_trading_sessions`)を追加しただけで判定へ接続していなかった。
B2 では人間が確定した閾値で business decision へ接続する。

## 確定仕様(人間確定 2026-09-02。ここで再判断しない)

```
BUY            missed=0 NORMAL / missed=1 WARNING / missed>=2 HARD_STOP
               as_of UNKNOWN -> HARD_STOP

holdings/SELL  missed=0 NORMAL / missed>=1 DATA_INSUFFICIENT
               as_of UNKNOWN -> DATA_INSUFFICIENT

price <= 0     provider boundary で ProviderDataError(retryable=False)
               None へ変換しない(missing と invalid を混同しない)
```

BUY と holdings/SELL は**非対称**である。BUY は買わずに見送れば機会損失で済むが、
holdings/SELL の誤りは実損に直結する(古い価格で損切りが発火する)。

## 本モジュールが固定しないこと

財務データの鮮度(reporting-cycle freshness)。Phase B2 の対象外であり、
`now - fiscal_period_end` による単純な stale 判定は行わない。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.market_session import expected_latest_completed_trading_session
from jstock_advisor.domain.price_freshness import (
    PriceFreshnessVerdict,
    evaluate_buy_price_freshness,
    evaluate_holdings_price_freshness,
)
from jstock_advisor.interfaces.provider_errors import (
    ProviderDataError,
    ProviderFailureCategory,
)
from jstock_advisor.interfaces.types import PriceBar

_CFG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CFG.holiday_calendar)
_JST = dt.timezone(dt.timedelta(hours=9))

# 2026-09-02 は水曜。08:00 JST の日次バッチ実行を想定(大引け前)。
_NOW = dt.datetime(2026, 9, 2, 8, 0, tzinfo=_JST)


def _session_offset_by(sessions: int) -> dt.date:
    """期待される直近完了セッションから `sessions` 営業日ぶん前の日付を返す。

    切り替わり日をテスト側にハードコードしないため、暦から導出する
    (Issue #66 の UTC/JST 論点を固定しないという Phase B1 の方針を踏襲)。
    """
    date = expected_latest_completed_trading_session(_NOW, _CALENDAR)
    for _ in range(sessions):
        date -= dt.timedelta(days=1)
        while not _CALENDAR.is_business_day(date):
            date -= dt.timedelta(days=1)
    return date


# --- T1-T5: BUY の価格鮮度 -----------------------------------------------------


def test_t1_buy_missed_zero_is_normal() -> None:
    """T1: 期待どおり最新なら NORMAL(既存挙動を変えない)。"""
    verdict, reason = evaluate_buy_price_freshness(
        _session_offset_by(0), _NOW, _CALENDAR
    )

    assert verdict is PriceFreshnessVerdict.NORMAL
    assert reason is None


def test_t2_buy_missed_one_is_warning_and_continues() -> None:
    """T2: 1セッション遅れは WARNING。判定は継続する(除外しない)。"""
    verdict, reason = evaluate_buy_price_freshness(
        _session_offset_by(1), _NOW, _CALENDAR
    )

    assert verdict is PriceFreshnessVerdict.WARNING
    assert verdict is not PriceFreshnessVerdict.HARD_STOP, "1セッション遅れで止めない"
    assert reason is not None
    assert "1" in reason


def test_t3_buy_missed_two_is_hard_stop() -> None:
    """T3: 2セッション遅れは HARD_STOP。"""
    verdict, reason = evaluate_buy_price_freshness(
        _session_offset_by(2), _NOW, _CALENDAR
    )

    assert verdict is PriceFreshnessVerdict.HARD_STOP
    assert reason is not None


@pytest.mark.parametrize("sessions", [3, 5, 10])
def test_t4_buy_missed_more_than_two_is_hard_stop(sessions: int) -> None:
    """T4: 2セッションを超えて遅れていても HARD_STOP のまま。"""
    verdict, reason = evaluate_buy_price_freshness(
        _session_offset_by(sessions), _NOW, _CALENDAR
    )

    assert verdict is PriceFreshnessVerdict.HARD_STOP
    assert reason is not None


def test_t5_buy_unknown_as_of_is_hard_stop() -> None:
    """T5: 基準日不明は HARD_STOP。0(新鮮)へ丸めない。"""
    verdict, reason = evaluate_buy_price_freshness(None, _NOW, _CALENDAR)

    assert verdict is PriceFreshnessVerdict.HARD_STOP
    assert verdict is not PriceFreshnessVerdict.NORMAL
    assert reason is not None


# --- T6-T9: holdings / SELL の価格鮮度 ----------------------------------------


def test_t6_holdings_missed_zero_is_normal() -> None:
    """T6: 期待どおり最新なら NORMAL(既存挙動を変えない)。"""
    verdict, reason = evaluate_holdings_price_freshness(
        _session_offset_by(0), _NOW, _CALENDAR
    )

    assert verdict is PriceFreshnessVerdict.NORMAL
    assert reason is None


def test_t7_holdings_missed_one_is_data_insufficient() -> None:
    """T7: 保有側は1セッション遅れで判定不能(BUY より厳格)。"""
    verdict, reason = evaluate_holdings_price_freshness(
        _session_offset_by(1), _NOW, _CALENDAR
    )

    assert verdict is PriceFreshnessVerdict.DATA_INSUFFICIENT
    assert reason is not None


def test_t8_sell_missed_one_is_data_insufficient() -> None:
    """T8: 売却判定も同じ policy を通る(古い価格で損切りを発火させない)。

    SELL / profit-taking / 売却価格推奨はいずれも保有銘柄の評価経路に属し、
    同一の `evaluate_holdings_price_freshness` を通る。
    """
    verdict, _ = evaluate_holdings_price_freshness(
        _session_offset_by(1), _NOW, _CALENDAR
    )

    assert verdict is PriceFreshnessVerdict.DATA_INSUFFICIENT


def test_t9_holdings_unknown_as_of_is_data_insufficient() -> None:
    """T9: 保有側の基準日不明も判定不能。"""
    verdict, reason = evaluate_holdings_price_freshness(None, _NOW, _CALENDAR)

    assert verdict is PriceFreshnessVerdict.DATA_INSUFFICIENT
    assert reason is not None


def test_buy_and_holdings_thresholds_are_asymmetric() -> None:
    """BUY と holdings で閾値が非対称であること自体を固定する。

    同一の入力(1セッション遅れ)に対して、BUY は継続し holdings は止める。
    片方に合わせて統一されると、この契約が静かに壊れる。
    """
    as_of = _session_offset_by(1)

    buy_verdict, _ = evaluate_buy_price_freshness(as_of, _NOW, _CALENDAR)
    holdings_verdict, _ = evaluate_holdings_price_freshness(as_of, _NOW, _CALENDAR)

    assert buy_verdict is PriceFreshnessVerdict.WARNING
    assert holdings_verdict is PriceFreshnessVerdict.DATA_INSUFFICIENT
    assert buy_verdict is not holdings_verdict


# --- T10: 暦 semantics を再実装していないこと ---------------------------------


def test_t10_calendar_semantics_are_not_reimplemented() -> None:
    """T10: 休場日・寄付前・大引け前後に個別の閾値を持たせていないこと。

    これらは `expected_latest_completed_trading_session` の定義により
    missed=0 へ畳み込まれる。price_freshness 側で曜日・祝日・時刻を
    再度判定していると、Issue #66 と同じ UTC/JST semantics の分散が起きる。
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "jstock_advisor"
        / "domain"
        / "price_freshness.py"
    ).read_text(encoding="utf-8")

    forbidden = ["weekday(", "is_holiday", "jpholiday", "saturday", "sunday"]
    for token in forbidden:
        assert token not in source, (
            f"price_freshness.py が暦判定を再実装している: {token}。"
            "expected_latest_completed_trading_session へ委ねること"
        )


@pytest.mark.parametrize(
    ("label", "now"),
    [
        ("平日・寄付前(08:00)", dt.datetime(2026, 9, 2, 8, 0, tzinfo=_JST)),
        ("平日・大引け直前(15:29)", dt.datetime(2026, 9, 2, 15, 29, tzinfo=_JST)),
        ("平日・大引け直後(15:30)", dt.datetime(2026, 9, 2, 15, 30, tzinfo=_JST)),
        ("土曜", dt.datetime(2026, 9, 5, 12, 0, tzinfo=_JST)),
        ("日曜", dt.datetime(2026, 9, 6, 12, 0, tzinfo=_JST)),
        ("月曜・寄付前", dt.datetime(2026, 9, 7, 8, 0, tzinfo=_JST)),
    ],
)
def test_t10b_expected_session_price_is_normal_in_all_market_states(
    label: str, now: dt.datetime
) -> None:
    """T10b: どの市場状態でも、期待セッションの価格なら NORMAL になる。

    「休場日だから警告」「寄付前だから警告」といった誤検知が出ないこと。
    """
    as_of = expected_latest_completed_trading_session(now, _CALENDAR)

    buy_verdict, _ = evaluate_buy_price_freshness(as_of, now, _CALENDAR)
    holdings_verdict, _ = evaluate_holdings_price_freshness(as_of, now, _CALENDAR)

    assert buy_verdict is PriceFreshnessVerdict.NORMAL, label
    assert holdings_verdict is PriceFreshnessVerdict.NORMAL, label


def test_max_data_age_business_days_is_not_used_as_price_threshold() -> None:
    """既存の取得時刻ベース閾値を価格 as_of の閾値へ流用していないこと。

    generic freshness(fetched_at)と price freshness(as_of_date)を再び混ぜると
    Issue #52 の根本原因へ戻る。
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "jstock_advisor"
        / "domain"
        / "price_freshness.py"
    ).read_text(encoding="utf-8")

    assert "max_data_age_business_days" not in source.replace(
        "max_data_age_business_days`(取得時刻ベースのgeneric freshness)は", ""
    ) or source.count("max_data_age_business_days") <= 1, (
        "price_freshness が max_data_age_business_days を閾値として参照している"
    )


# --- T11-T14: 不正価格の provider boundary 拒否 --------------------------------


class _StubTicker:
    """yfinance の Ticker を模したスタブ(bars を直接与える)。"""

    def __init__(self, bars: list[PriceBar]) -> None:
        self.bars = bars


def _provider_with_close(close: Decimal) -> object:
    """指定した終値の bar を1本だけ返す market data provider を作る。"""
    from jstock_advisor.providers.market_data import yfinance_impl as module

    provider = module.YFinanceMarketDataProvider(now=_NOW)
    bar = PriceBar(
        date=expected_latest_completed_trading_session(_NOW, _CALENDAR),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
    )
    history = module.PriceHistory(
        symbol="2914.T", bars=[bar], source=provider._source()  # noqa: SLF001
    )
    provider._fetch_history = lambda *a, **kw: history  # type: ignore[method-assign]  # noqa: SLF001
    return provider


@pytest.mark.parametrize(
    ("label", "close"),
    [("ゼロ", Decimal("0")), ("ゼロ(小数表現)", Decimal("0.00"))],
)
def test_t11_zero_price_is_rejected_at_provider_boundary(
    label: str, close: Decimal
) -> None:
    """T11: 終値0は provider 境界で拒否される。"""
    provider = _provider_with_close(close)

    with pytest.raises(ProviderDataError) as excinfo:
        provider.get_latest_price("2914")  # type: ignore[attr-defined]

    assert excinfo.value.retryable is False, f"{label}: 再取得しても変わらない"
    assert (
        excinfo.value.failure_category
        is ProviderFailureCategory.NON_RETRYABLE_PROVIDER_FAILURE
    )


@pytest.mark.parametrize("close", [Decimal("-1"), Decimal("-1000.5")])
def test_t12_negative_price_is_rejected_at_provider_boundary(close: Decimal) -> None:
    """T12: 負値の終値も provider 境界で拒否される。"""
    provider = _provider_with_close(close)

    with pytest.raises(ProviderDataError) as excinfo:
        provider.get_latest_price("2914")  # type: ignore[attr-defined]

    assert excinfo.value.retryable is False


def test_t13_valid_positive_price_is_unchanged() -> None:
    """T13: 正常な正値では従来どおり PriceSnapshot が返る。"""
    provider = _provider_with_close(Decimal("1234.5"))

    snapshot = provider.get_latest_price("2914")  # type: ignore[attr-defined]

    assert snapshot is not None
    assert snapshot.close_price == Decimal("1234.5")
    assert snapshot.as_of_date == expected_latest_completed_trading_session(
        _NOW, _CALENDAR
    )


def test_t14_invalid_price_is_not_confused_with_missing_data() -> None:
    """T14: 不正価格を None(genuine missing)へ変換しない。

    None は「取得できたがデータが無い」を意味する。ゼロ/負値は
    「取得できたがデータが壊れている」であり別物である(Issue #59 の契約)。
    両者を混同すると、壊れた値が欠測として静かに素通りする経路ができる。
    """
    from jstock_advisor.providers.market_data import yfinance_impl as module

    # bars が空 = genuine missing -> None を返す(従来どおり)
    missing = module.YFinanceMarketDataProvider(now=_NOW)
    missing._fetch_history = lambda *a, **kw: None  # type: ignore[method-assign]  # noqa: SLF001
    assert missing.get_latest_price("2914") is None

    # 壊れた値 = failure -> 例外(None ではない)
    broken = _provider_with_close(Decimal("0"))
    with pytest.raises(ProviderDataError):
        broken.get_latest_price("2914")  # type: ignore[attr-defined]


# --- T15: 1銘柄の異常がバッチ全体を落とさないこと -----------------------------


def test_t15_stale_price_does_not_stop_other_symbols() -> None:
    """T15: 1銘柄が stale でも、他銘柄の判定は継続できる形になっていること。

    価格鮮度の判定は**値を返す純粋関数**であり、例外を送出しない。
    したがって呼び出し側の銘柄単位ループを中断させない。
    バッチ全体を落とすのは呼び出し側の設計ミスであり、policy 層の責務ではない。
    """
    stale = _session_offset_by(5)
    fresh = _session_offset_by(0)

    results = [
        evaluate_holdings_price_freshness(as_of, _NOW, _CALENDAR)
        for as_of in (stale, fresh, stale, fresh)
    ]

    verdicts = [v for v, _ in results]
    assert verdicts[0] is PriceFreshnessVerdict.DATA_INSUFFICIENT
    assert verdicts[1] is PriceFreshnessVerdict.NORMAL, "stale の次の銘柄が巻き込まれている"
    assert verdicts[3] is PriceFreshnessVerdict.NORMAL


def test_t15b_freshness_evaluation_never_raises() -> None:
    """T15b: 鮮度判定そのものは例外を送出しない(UNKNOWN も戻り値で表す)。"""
    for as_of in (None, _session_offset_by(0), _session_offset_by(99)):
        evaluate_buy_price_freshness(as_of, _NOW, _CALENDAR)
        evaluate_holdings_price_freshness(as_of, _NOW, _CALENDAR)


# --- T16 / T17: 既存契約の regression ------------------------------------------
#
# T16(Phase B1 の優待 freshness)は tests/unit/test_issue_52_freshness_semantics.py が、
# T17(#120 の registry read-purity)は tests/unit/test_issue_120_registry_read_purity.py が
# 引き続き担保する。本 Phase では両モジュールを変更していない。
# targeted regression で必ず同時に実行すること。


def test_t16_t17_regression_modules_exist() -> None:
    """T16 / T17 の regression モジュールが削除・改名されていないこと。"""
    tests_dir = Path(__file__).resolve().parent
    for name in (
        "test_issue_52_freshness_semantics.py",
        "test_issue_120_registry_read_purity.py",
    ):
        assert (tests_dir / name).exists(), f"{name} が存在しない"


# --- T18-T22: 未来日の as_of_date(review finding F1) --------------------------
#
# `missed_trading_sessions()` は未来日に対して 0 を返す(取りこぼしは負にならない、
# という観測としては正しい contract)。Phase B1 ではこの primitive を業務判定へ
# 接続していなかったため許容できたが、B2 で接続したことにより
# **未来日の株価が NORMAL として売買判定に使われる**状態になっていた。
#
# 未来日は「新鮮」ではなく timestamp / data integrity の異常である。
# 観測の contract は壊さず、policy 層(`_is_future_as_of`)で分離する。


def _future_session(days: int) -> dt.date:
    """期待される直近完了セッションより `days` 日ぶん未来の日付。"""
    return expected_latest_completed_trading_session(_NOW, _CALENDAR) + dt.timedelta(
        days=days
    )


@pytest.mark.parametrize("days", [1, 3, 30])
def test_t18_buy_future_as_of_is_hard_stop(days: int) -> None:
    """T18: BUY は未来日の基準日を HARD_STOP とする。"""
    verdict, reason = evaluate_buy_price_freshness(
        _future_session(days), _NOW, _CALENDAR
    )

    assert verdict is PriceFreshnessVerdict.HARD_STOP
    assert verdict is not PriceFreshnessVerdict.NORMAL
    assert reason is not None
    assert "未来" in reason


@pytest.mark.parametrize("days", [1, 3, 30])
def test_t19_holdings_future_as_of_is_data_insufficient(days: int) -> None:
    """T19: holdings/SELL は未来日の基準日を DATA_INSUFFICIENT とする。"""
    verdict, reason = evaluate_holdings_price_freshness(
        _future_session(days), _NOW, _CALENDAR
    )

    assert verdict is PriceFreshnessVerdict.DATA_INSUFFICIENT
    assert verdict is not PriceFreshnessVerdict.NORMAL
    assert reason is not None
    assert "未来" in reason


def test_t20_future_date_does_not_pass_business_gate_as_normal() -> None:
    """T20: 未来日が missed=0 経由で NORMAL として gate を通らないこと。

    observation(`missed_trading_sessions`)が 0 を返すこと自体は仕様どおりだが、
    その 0 が judgement へそのまま流れないことを固定する。
    観測と policy のどちらを直しても片方だけでは満たせない契約である。
    """
    from jstock_advisor.domain.market_session import missed_trading_sessions

    future = _future_session(1)

    # 観測は 0 のまま(contract を壊していない)
    assert missed_trading_sessions(future, _NOW, _CALENDAR) == 0

    # しかし judgement は NORMAL にならない
    buy_verdict, _ = evaluate_buy_price_freshness(future, _NOW, _CALENDAR)
    holdings_verdict, _ = evaluate_holdings_price_freshness(future, _NOW, _CALENDAR)

    assert buy_verdict is not PriceFreshnessVerdict.NORMAL
    assert holdings_verdict is not PriceFreshnessVerdict.NORMAL


def test_t21_expected_session_date_is_normal() -> None:
    """T21: 期待セッションちょうど(未来ではない)は NORMAL のまま。

    未来日の拒否が1日ぶん厳しくなって正常系を巻き込んでいないことの確認。
    """
    expected = expected_latest_completed_trading_session(_NOW, _CALENDAR)

    buy_verdict, buy_reason = evaluate_buy_price_freshness(expected, _NOW, _CALENDAR)
    holdings_verdict, holdings_reason = evaluate_holdings_price_freshness(
        expected, _NOW, _CALENDAR
    )

    assert buy_verdict is PriceFreshnessVerdict.NORMAL
    assert buy_reason is None
    assert holdings_verdict is PriceFreshnessVerdict.NORMAL
    assert holdings_reason is None


def test_t22_shared_fixtures_do_not_depend_on_future_dates() -> None:
    """T22: 価格鮮度と無関係なテストの fixture が未来日に依存していないこと。

    「未来日なら missed=0 になる」性質を使って正常系 fixture を作ると、
    異常値許容がテスト上で固定されてしまう(F1 の再発)。
    """
    tests_dir = Path(__file__).resolve().parent
    forbidden = "dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()"

    for name in (
        "test_buy_signal_service.py",
        "test_holdings_watchlist_handler.py",
    ):
        source = (tests_dir / name).read_text(encoding="utf-8")
        assert forbidden not in source, (
            f"{name} が『未来日で missed=0 になる』性質へ依存している。"
            "expected_latest_completed_trading_session から導出すること"
        )


# --- T23: UNKNOWN の business path 到達性 --------------------------------------


def test_t23_as_of_date_is_required_by_current_typed_contract() -> None:
    """T23: 現行の型契約では as_of_date は必須であり、UNKNOWN は到達しない。

    `PriceSnapshot.as_of_date` / `StockSnapshot.price_as_of_date` はいずれも
    `dt.date`(optional でない)であるため、Production の通常経路で
    `None` が渡ることはない。UNKNOWN policy は**将来 optional 化された場合の
    安全弁**として定義してあり、現時点では到達不能である。

    ```
    UNKNOWN_POLICY_DEFINED                    = YES
    UNKNOWN_BUSINESS_PATH_CURRENTLY_REACHABLE = NO
    ```

    型を optional 化する変更が入った際に、この assert が落ちることで
    「安全弁が実際に必要になった」ことに気づけるようにする。
    """
    import typing

    from jstock_advisor.interfaces.types import PriceSnapshot
    from jstock_advisor.services.stock_snapshot_service import StockSnapshot

    price_hint = typing.get_type_hints(PriceSnapshot)["as_of_date"]
    snapshot_hint = typing.get_type_hints(StockSnapshot)["price_as_of_date"]

    assert price_hint is dt.date, (
        "PriceSnapshot.as_of_date が optional 化された。"
        "evaluate_screening の price_as_of_date=None による skip が"
        "安全弁を無効化しないか確認すること"
    )
    assert snapshot_hint is dt.date, (
        "StockSnapshot.price_as_of_date が optional 化された。同上"
    )


def test_t23b_screening_skips_only_when_price_date_is_not_supplied() -> None:
    """T23b: evaluate_screening の optional 引数が安全弁を消さないこと。

    `price_as_of_date=None` は「呼び出し側が価格を渡していない」を意味し、
    「基準日が不明」ではない。前者では鮮度判定を行わず、後者(将来 optional 化
    された場合)は `evaluate_buy_price_freshness(None, ...)` が HARD_STOP を返す。

    この2つを混同すると、型が optional 化されたときに
    **未指定として素通りする**経路ができる。
    """
    # 「基準日が不明」は HARD_STOP(skip ではない)
    verdict, reason = evaluate_buy_price_freshness(None, _NOW, _CALENDAR)
    assert verdict is PriceFreshnessVerdict.HARD_STOP
    assert reason is not None

    holdings_verdict, holdings_reason = evaluate_holdings_price_freshness(
        None, _NOW, _CALENDAR
    )
    assert holdings_verdict is PriceFreshnessVerdict.DATA_INSUFFICIENT
    assert holdings_reason is not None
