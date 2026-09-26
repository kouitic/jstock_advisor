"""Issue #475(#472の修正。time semantics): `build_stock_snapshot`の価格窓・
52週安値の基準日をUTC暦日(`now.date()`)からJST暦日(`evaluation_date_jst(now)`)へ
統一する。

## 何が問題だったか

`build_stock_snapshot`(`services/stock_snapshot_service.py`)は決算関連の暦日比較
には`evaluation_date_jst(now)`(JST暦日)を使う一方、価格履歴・52週安値・過去株価
レンジの基準日には`now.date()`(**UTC暦日**)を使っていた(#472 Phase A実測)。
JST 00:00-08:59台(=前日のUTC)の実行では、価格窓の始端・終端が本来のJST暦日より
1日ずれる。

## 検証方針

`compute_52_week_low`/`compute_historical_range_price`(`domain/valuation/
fair_value.py`)自体は変更しない(呼び出し側が渡す基準日の選択のみが対象)。
downstream の集計(outlier filter・5手法の加重平均)を経由すると検証対象が
不必要に複雑になるため、**サービス関数が実際にどの日付を各呼び出し先へ渡したか**
を直接記録・検証する(呼び出し先自体はwrapし、本物の実装を呼んだうえで
実際に渡された引数だけを記録する。返り値の計算ロジックは変更しない)。

## 本モジュールが固定する契約

```
C-BS  窓の境界日(365日前)の足が、窓に入る/入らないかでJST暦日基準を使うこと
      (get_price_history/compute_52_week_low/compute_historical_range_priceへ
      渡す基準日がすべてevaluation_date_jst(now)と一致すること)
C-CS  08:00 JST(前日23:00 UTC)と10:00 JSTで、同じ入力から同じ基準日になること
      (UTC暦日基準ではこの2時刻でUTC日付が異なり、基準日がずれてしまっていた)
```
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.valuation.fair_value import (
    compute_52_week_low,
    compute_historical_range_price,
)
from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.interfaces.types import PriceHistory
from jstock_advisor.services import stock_snapshot_service
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_CFG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CFG.holiday_calendar)
_STOCK_CODE = "2914"

# 08:00 JST(前日23:00 UTC)。UTC暦日=2026-09-14 / JST暦日=2026-09-15。
_NOW_0800_JST = dt.datetime(2026, 9, 14, 23, 0, tzinfo=dt.UTC)
# 10:00 JST(同日01:00 UTC)。UTC暦日もJST暦日も2026-09-15で一致する。
_NOW_1000_JST = dt.datetime(2026, 9, 15, 1, 0, tzinfo=dt.UTC)


class _RecordingMarketDataProvider:
    """既存のmock market data providerへ処理を委譲しつつ、`get_price_history`/
    `get_benchmark_price_history`へ渡された`start`/`end`(基準日の実測)を記録する。
    データそのものは変更しない(既存mockのデータをそのまま使う)。
    """

    def __init__(self, delegate: MarketDataProvider) -> None:
        self._delegate = delegate
        self.price_history_calls: list[tuple[dt.date, dt.date]] = []
        self.benchmark_calls: list[tuple[str, dt.date, dt.date]] = []

    def get_latest_price(self, stock_code: str) -> object | None:
        return self._delegate.get_latest_price(stock_code)

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        self.price_history_calls.append((start, end))
        return self._delegate.get_price_history(stock_code, start, end)

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        return self._delegate.get_average_trading_value(stock_code, business_days)

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        self.benchmark_calls.append((symbol, start, end))
        return self._delegate.get_benchmark_price_history(symbol, start, end)


def _build_with_recording(now: dt.datetime) -> tuple[object, _RecordingMarketDataProvider]:
    bundle = build_mock_provider_bundle(now)
    recorder = _RecordingMarketDataProvider(bundle.market_data)
    return dataclasses.replace(bundle, market_data=recorder), recorder


def _run(now: dt.datetime) -> tuple[object, _RecordingMarketDataProvider, object, object]:
    """`build_stock_snapshot`を実行し、実際に各呼び出し先へ渡された基準日を
    記録して返す(recorder / compute_52_week_low spy / compute_historical_range_price spy)。
    """
    bundle, recorder = _build_with_recording(now)
    with (
        patch.object(
            stock_snapshot_service, "compute_52_week_low", wraps=compute_52_week_low
        ) as spy_52w,
        patch.object(
            stock_snapshot_service,
            "compute_historical_range_price",
            wraps=compute_historical_range_price,
        ) as spy_range,
    ):
        snapshot, error = build_stock_snapshot(bundle, _STOCK_CODE, now, _CFG, _CALENDAR)
    assert error is None, f"snapshot構築に失敗した: {error}"
    assert snapshot is not None
    return snapshot, recorder, spy_52w, spy_range


# --- C-BS: 4箇所すべてがevaluation_date_jst(now)を渡していること -------------


def test_c_bs_history_start_and_end_use_jst_calendar_date() -> None:
    """`get_price_history`のstart/end(:315/:318)がJST暦日基準であること。"""
    now = _NOW_0800_JST
    expected = evaluation_date_jst(now)
    wrong_utc_date = now.date()
    assert expected != wrong_utc_date, "この瞬間ではJST/UTC暦日境界を跨いでいない"

    _snapshot, recorder, _spy_52w, _spy_range = _run(now)

    assert recorder.price_history_calls, "get_price_historyが呼ばれていない"
    lookback_years = _CFG.valuation.historical_range_method.lookback_years
    expected_start = expected - dt.timedelta(days=365 * lookback_years)
    for start, end in recorder.price_history_calls:
        assert end == expected, (
            f"get_price_historyのend={end}(期待={expected})。"
            f"UTC暦日({wrong_utc_date})基準の再発疑い"
        )
        assert start == expected_start, f"get_price_historyのstart={start}(期待={expected_start})"


def test_c_bs_benchmark_history_uses_jst_calendar_date() -> None:
    """TOPIX/セクターETFのベンチマーク履歴取得(:322/:327)もJST暦日基準であること。"""
    now = _NOW_0800_JST
    expected = evaluation_date_jst(now)

    _snapshot, recorder, _spy_52w, _spy_range = _run(now)

    assert recorder.benchmark_calls, (
        "get_benchmark_price_historyが呼ばれていない(TOPIXは必ず呼ばれる想定)"
    )
    for _symbol, _start, end in recorder.benchmark_calls:
        assert end == expected, f"ベンチマーク履歴のend={end}(期待={expected})"


def test_c_bs_compute_historical_range_price_uses_jst_calendar_date() -> None:
    """`compute_historical_range_price`(:393)へ渡すas_of_dateがJST暦日基準であること。"""
    now = _NOW_0800_JST
    expected = evaluation_date_jst(now)

    _snapshot, _recorder, _spy_52w, spy_range = _run(now)

    spy_range.assert_called_once()
    as_of_date_arg = spy_range.call_args.args[1]
    assert as_of_date_arg == expected, (
        f"compute_historical_range_priceのas_of_date={as_of_date_arg}(期待={expected})。"
        f"UTC暦日({now.date()})基準の再発疑い"
    )


def test_c_bs_compute_52_week_low_uses_jst_calendar_date() -> None:
    """`compute_52_week_low`(:461)へ渡すas_of_dateがJST暦日基準であること。"""
    now = _NOW_0800_JST
    expected = evaluation_date_jst(now)

    _snapshot, _recorder, spy_52w, _spy_range = _run(now)

    spy_52w.assert_called_once()
    as_of_date_arg = spy_52w.call_args.args[1]
    assert as_of_date_arg == expected, (
        f"compute_52_week_lowのas_of_date={as_of_date_arg}(期待={expected})。"
        f"UTC暦日({now.date()})基準の再発疑い"
    )


# --- C-CS: 08:00 JSTと10:00 JSTで同じ基準日になること ---------------------------


def test_c_cs_same_jst_calendar_date_gives_identical_reference_dates() -> None:
    """JST暦日が同じ(2026-09-15)08:00 JSTと10:00 JSTで、渡される基準日が
    一致すること(UTC暦日基準では、この2時刻でUTC日付が異なり基準日がずれていた)。
    """
    assert evaluation_date_jst(_NOW_0800_JST) == evaluation_date_jst(_NOW_1000_JST), (
        "テスト前提が崩れている: 2つのnowのJST暦日が一致していない"
    )
    assert _NOW_0800_JST.date() != _NOW_1000_JST.date(), (
        "テスト前提が崩れている: 2つのnowのUTC暦日が一致してしまっている"
        "(UTC暦日基準でも差が出ない=このcontrolでは検証にならない)"
    )

    _snapshot_a, recorder_a, spy_52w_a, spy_range_a = _run(_NOW_0800_JST)
    _snapshot_b, recorder_b, spy_52w_b, spy_range_b = _run(_NOW_1000_JST)

    assert recorder_a.price_history_calls == recorder_b.price_history_calls, (
        f"08:00 JST={recorder_a.price_history_calls} / "
        f"10:00 JST={recorder_b.price_history_calls}"
    )
    assert spy_52w_a.call_args.args[1] == spy_52w_b.call_args.args[1]
    assert spy_range_a.call_args.args[1] == spy_range_b.call_args.args[1]
