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
                   ・oracle の dict が互いに異なること(取り違えで差が出る)。対象は共通 8 種
                     (BUY の PR-1)。SELL / 利確 / 保有判断では exit_price_range_metrics を
                     加えた 9 種
                   ・各 `*_to_metrics` の引数を 1 つずつ None にすると oracle が変わること
                     (引数落としで差が出る。例外になる場合も「差が出る」とみなす)
                 前提を満たさない fixture(例: 引数を落としても出力が変わらない)は、検査を通っても
                 変異が生き残るため、ここで落とす。

## このモジュールが「検査している範囲」と「していない範囲」

している    呼び出し側が渡す引数列を、本モジュールの `common_metrics_specs()` が写した形(= 仕様)で
            固定する。共通 8 種(historical_valuation / timing / earnings_surprise /
            earnings_trend / entry_price_range / market / sector / environment)。
            SELL(Issue #405 PR-2)は、これに `exit_price_range_metrics_spec()` の 1 種を
            加えた 9 種と、
            exit_price_range の算出自体の引数列(`exit_price_range_computation_spec()`)を固定する。
していない  ・`isolated_shadow_*` を介さず、手書きの try/except で隔離している箇所
            ・src/ の外からの呼び出し
            ・hoist 側で `**dict` の展開や、式(関数呼び出しの結果)を直接渡している形
            ・Recommendation → DecisionSnapshot の複写(decision_snapshot_builder)での取り違え
            ・`*_to_metrics` 自身の算出内容の正しさ(それぞれの domain のテストの責務)
            ・利確 / 保有判断の exit_price_range(それぞれの PR で、その service の引数列に合わせて
              追加する)
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
from jstock_advisor.domain.signals.exit_price_range import (
    evaluate_exit_price_range,
    exit_price_range_result_to_metrics,
)
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


def exit_price_range_metrics_spec(
    snapshot: Any, config: AppConfig, average_purchase_price: Any, now: Any
) -> MetricsSpec:
    """SELL の `exit_price_range_metrics` の oracle(Issue #405 PR-2)。

    sell_signal_service は、先に算出した exit_price_range(`ExitPriceRangeResult`)を第 1 引数に
    渡して `exit_price_range_result_to_metrics` を呼ぶ。oracle も同じ呼び出しを、**テスト側で
    `evaluate_exit_price_range` を再計算した結果**を第 1 引数にして行う(service の変数を使わない)。
    """
    exit_config = config.entry_exit_price.exit
    result = evaluate_exit_price_range(
        snapshot.fair_value_range,
        snapshot.historical_valuation,
        snapshot.timing,
        average_purchase_price,
        snapshot.current_price,
        now,
        exit_config,
    )
    return MetricsSpec(
        "exit_price_range_metrics",
        exit_price_range_result_to_metrics,
        (
            result,
            snapshot.fair_value_range,
            snapshot.historical_valuation,
            snapshot.timing,
            average_purchase_price,
            exit_config,
        ),
    )


#: `exit_price_range` の算出結果(`ExitPriceRangeResult`)のうち、Recommendation の field へ
#: コピーされる 9 項目(Recommendation の field 名 → 結果の属性名)。
EXIT_PRICE_RANGE_RECOMMENDATION_FIELDS: dict[str, str] = {
    "exit_price_range_state": "state",
    "exit_price_range_confidence": "confidence",
    "exit_price_range_coverage": "coverage",
    "exit_price_range_reason_codes": "reason_codes",
    "exit_price_range_partial_low_price": "partial_profit_take_low_price",
    "exit_price_range_partial_high_price": "partial_profit_take_high_price",
    "exit_price_range_strong_price": "strong_profit_take_price",
    "exit_price_range_downside_review_price": "downside_review_price",
    "exit_price_range_exit_review_price": "exit_review_price",
}


def _normalized(value: Any) -> Any:
    """tuple と list の違いだけで不一致にしない(Recommendation は list で保持する場合がある)。"""
    return list(value) if isinstance(value, tuple) else value


@dataclass(frozen=True)
class ComputationSpec:
    """dict ではなく結果オブジェクトを返す算出(`isolated_shadow_computation`)の oracle。

    `field_map` の各 Recommendation field が、算出結果のどの属性と一致するべきかを持つ。
    """

    name: str
    func: Callable[..., Any]
    args: tuple[Any, ...]
    field_map: dict[str, str]

    @property
    def argument_names(self) -> tuple[str, ...]:
        return tuple(inspect.signature(self.func).parameters)

    def projection(self, result: Any) -> dict[str, Any]:
        return {
            field: _normalized(getattr(result, attribute))
            for field, attribute in self.field_map.items()
        }

    def oracle(self) -> dict[str, Any]:
        return self.projection(self.func(*self.args))

    def oracle_with_argument_dropped(self, index: int) -> dict[str, Any]:
        """`index` 番目の引数を None にして算出した結果(P7 型の変異を模す)。例外は目印の dict。"""
        args = list(self.args)
        args[index] = None
        try:
            return self.projection(self.func(*args))
        except Exception as exc:  # noqa: BLE001 - 例外も「oracle と異なる」として扱う
            return {_RAISED_MARKER: type(exc).__name__}


def exit_price_range_computation_spec(
    snapshot: Any, config: AppConfig, average_purchase_price: Any, now: Any
) -> ComputationSpec:
    """SELL の exit_price_range の算出(`evaluate_exit_price_range`)の oracle(Issue #405 PR-2)。

    引数列は、sell_signal_service が `isolated_shadow_computation` の lambda で渡している 7 引数と
    同じ。算出結果のうち Recommendation へコピーされる 9 項目を比べる。
    """
    return ComputationSpec(
        "exit_price_range",
        evaluate_exit_price_range,
        (
            snapshot.fair_value_range,
            snapshot.historical_valuation,
            snapshot.timing,
            average_purchase_price,
            snapshot.current_price,
            now,
            config.entry_exit_price.exit,
        ),
        EXIT_PRICE_RANGE_RECOMMENDATION_FIELDS,
    )


def computation_argument_drop_blind_spots(spec: ComputationSpec) -> list[str]:
    """引数を 1 つ None にしても oracle が変わらない引数名(A3: 空であること)。"""
    baseline = spec.oracle()
    return [
        name
        for index, name in enumerate(spec.argument_names)
        if spec.oracle_with_argument_dropped(index) == baseline
    ]


def mismatched_computation_fields(
    recommendation: Any, spec: ComputationSpec
) -> dict[str, tuple[Any, Any]]:
    """Recommendation の算出結果由来の field が oracle と一致しない field → (実際の値, oracle)。

    A1(算出の引数列): 空であること。
    """
    expected = spec.oracle()
    mismatches: dict[str, tuple[Any, Any]] = {}
    for field in spec.field_map:
        actual = _normalized(getattr(recommendation, field))
        if actual != expected[field]:
            mismatches[field] = (actual, expected[field])
    return mismatches


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
