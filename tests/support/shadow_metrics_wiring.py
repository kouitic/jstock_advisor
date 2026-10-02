"""Shadow 計測の `*_metrics` が取り違えなく Recommendation へ載ることを検査する共有部品(#405)。

## 何を防ぐか

S-20 系列(Issue #384)の各 service は、`isolated_shadow_observation()` で隔離して算出した
`*_metrics`(dict)を、いったんローカル変数へ hoist し、`Recommendation(...)` の kwarg へ渡す。
次の 2 つの誤りは、例外も出ず、既存のテストでも検出できなかった(Issue #403 の mutation 実測)。

    P6 / P9  2 つの変数名を取り違える(例: `market_metrics=` へ `sector_metrics` を渡す)
    P7       `*_to_metrics` の呼び出しから引数を落とす(例: timing から `current_price` を落とすと
             `current_vs_ma20_pct` が実数から None へ静かに変わる)

## 方式(MANAGER 判断 = Issue #405 の選択肢 A1 + A3)

A1(oracle 一致)  サービスが消費した snapshot から、テスト側で `*_to_metrics` を**同じ引数列で**
                 再計算し(= oracle)、Recommendation の各 field と完全一致することを assert する。
                 取り違えと引数落としの両方が、1 つの assert で落ちる。
A3(前提)         oracle 一致の検査が空振りしないための前提を、fixture ごとに固定する。
                   ・9 種の oracle の dict が互いに異なること(取り違えで差が出る)
                   ・各 `*_to_metrics` の引数を 1 つずつ None にすると oracle が変わること
                     (引数落としで差が出る。例外になる場合も「差が出る」とみなす)
                 前提を満たさない fixture(例: 引数を落としても出力が変わらない)は、検査を通っても
                 変異が生き残るため、ここで落とす。

## このモジュールが「検査している範囲」と「していない範囲」

している    呼び出し側が渡す引数列を、本モジュールの `common_metrics_specs()` が写した形(= 仕様)で
            固定する。8 種(historical_valuation / timing / earnings_surprise / earnings_trend /
            entry_price_range / market / sector / environment)。
していない  ・`isolated_shadow_*` を介さず、手書きの try/except で隔離している箇所
            ・src/ の外からの呼び出し
            ・hoist 側で `**dict` の展開や、式(関数呼び出しの結果)を直接渡している形
            ・Recommendation → DecisionSnapshot の複写(decision_snapshot_builder)での取り違え
            ・`*_to_metrics` 自身の算出内容の正しさ(それぞれの domain のテストの責務)
            ・exit_price_range_metrics(sell / 利確 / 保有判断の PR で、その service の引数列に
              合わせて追加する)
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.signals.earnings_surprise import earnings_surprise_result_to_metrics
from jstock_advisor.domain.signals.earnings_trend import earnings_trend_result_to_metrics
from jstock_advisor.domain.signals.entry_price_range import entry_price_range_result_to_metrics
from jstock_advisor.domain.signals.environment import environment_result_to_metrics
from jstock_advisor.domain.signals.historical_valuation import (
    historical_valuation_result_to_metrics,
)
from jstock_advisor.domain.signals.market_environment import market_environment_result_to_metrics
from jstock_advisor.domain.signals.sector_environment import sector_environment_result_to_metrics
from jstock_advisor.domain.signals.timing_score import timing_score_result_to_metrics

#: 引数を落とした呼び出しが例外になったときの目印(oracle とは必ず異なる)。
_RAISED_MARKER = "__raised__"


@dataclass(frozen=True)
class MetricsSpec:
    """1 つの `*_metrics` field の oracle(算出関数と、そのサービスが渡す引数列)。"""

    field: str
    func: Callable[..., dict[str, Any]]
    args: tuple[Any, ...]

    @property
    def argument_names(self) -> tuple[str, ...]:
        return tuple(inspect.signature(self.func).parameters)

    def oracle(self) -> dict[str, Any]:
        return self.func(*self.args)

    def oracle_with_argument_dropped(self, index: int) -> dict[str, Any]:
        """`index` 番目の引数を None にして算出した結果(P7 型の変異を模す)。

        例外になる場合は、本番では `isolated_shadow_observation()` が COMPUTATION_FAILED の dict へ
        変えるのと同様に、oracle と異なる目印の dict を返す(= 「差が出る」側)。
        """
        args = list(self.args)
        args[index] = None
        try:
            return self.func(*args)
        except Exception as exc:  # noqa: BLE001 - 例外も「oracle と異なる」として扱う
            return {_RAISED_MARKER: type(exc).__name__}


def common_metrics_specs(snapshot: Any, config: AppConfig) -> list[MetricsSpec]:
    """BUY / SELL / 利確 / 保有判断が共通で `*_metrics` を算出する 8 種の oracle。

    引数列は、各 service の `isolated_shadow_observation()` の lambda が渡している引数と同じ。
    この引数列が「仕様」であり、service 側が引数を落とす・入れ替えると、A1 の assert が落ちる。
    """
    return [
        MetricsSpec(
            "historical_valuation_metrics",
            historical_valuation_result_to_metrics,
            (snapshot.historical_valuation,),
        ),
        MetricsSpec(
            "timing_metrics",
            timing_score_result_to_metrics,
            (snapshot.timing, snapshot.momentum, snapshot.current_price),
        ),
        MetricsSpec(
            "earnings_surprise_metrics",
            earnings_surprise_result_to_metrics,
            (snapshot.earnings_surprise,),
        ),
        MetricsSpec(
            "earnings_trend_metrics",
            earnings_trend_result_to_metrics,
            (snapshot.earnings_trend,),
        ),
        MetricsSpec(
            "entry_price_range_metrics",
            entry_price_range_result_to_metrics,
            (
                snapshot.entry_price_range,
                snapshot.fair_value_range,
                snapshot.historical_valuation,
                snapshot.timing,
                snapshot.momentum,
                config.entry_exit_price.entry,
            ),
        ),
        MetricsSpec(
            "market_metrics",
            market_environment_result_to_metrics,
            (snapshot.market_environment,),
        ),
        MetricsSpec(
            "sector_metrics",
            sector_environment_result_to_metrics,
            (snapshot.sector_environment,),
        ),
        MetricsSpec(
            "environment_metrics",
            environment_result_to_metrics,
            (snapshot.environment, snapshot.market_environment, snapshot.sector_environment),
        ),
    ]


def swap_blind_pairs(specs: list[MetricsSpec]) -> list[tuple[str, str]]:
    """oracle の dict が等しく、取り違えても差が出ない field の組(A3: 空であること)。"""
    oracles = {spec.field: spec.oracle() for spec in specs}
    names = list(oracles)
    return [
        (a, b)
        for index, a in enumerate(names)
        for b in names[index + 1 :]
        if oracles[a] == oracles[b]
    ]


def argument_drop_blind_spots(specs: list[MetricsSpec]) -> list[tuple[str, str]]:
    """引数を 1 つ None にしても oracle が変わらない (field, 引数名) の組(A3: 空であること)。"""
    blind: list[tuple[str, str]] = []
    for spec in specs:
        baseline = spec.oracle()
        for index, name in enumerate(spec.argument_names):
            if spec.oracle_with_argument_dropped(index) == baseline:
                blind.append((spec.field, name))
    return blind


def mismatched_fields(recommendation: Any, specs: list[MetricsSpec]) -> dict[str, tuple[Any, Any]]:
    """Recommendation の `*_metrics` が oracle と一致しない field → (実際の値, oracle)。

    A1: 空であること。
    """
    mismatches: dict[str, tuple[Any, Any]] = {}
    for spec in specs:
        actual = getattr(recommendation, spec.field)
        expected = spec.oracle()
        if actual != expected:
            mismatches[spec.field] = (actual, expected)
    return mismatches
