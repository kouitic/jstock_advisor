"""利確判定(evaluate_profit_taking)の入力の格子(Issue #878 PR-3a の characterization 用)。

合成した入力だけを使う(実在の銘柄・保有・価格は含まない)。格子は決定的(固定の種から自前の
線形合同法で選ぶ。標準ライブラリの乱数の実装に依存しない)で、実行のたびに同じ入力列になる。

- ``sampled_cases()``: 入力の各次元から決定的に抽出した多数の組み合わせ
- ``missing_input_cases()``: 入力が欠けた場合(適正価格が使えない・momentum が無い・含み損・
  候補なし 等)を全て列挙した格子(例外を出さず、副作用が無いことを確かめる)
- ``evaluate(kwargs)``: 現行の evaluate_profit_taking を呼ぶ(例外は型名で返す)
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    ProfitTakingIndustrySector,
    StockType,
    TrendClassification,
)
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.signals.profit_protection import ProfitProtectionMetrics
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
)

CONFIG = load_config().profit_taking

AVERAGE_PRICE = Decimal("1000")
SHARES = 100
TOTAL_AMOUNT = Decimal("100000")


def fair_value_range(
    *,
    bear: Decimal | None,
    neutral: Decimal | None,
    bull: Decimal | None,
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
    methods: int = 1,
    usable: bool = True,
) -> FairValueRange:
    used = [
        FairValueMethodResult(
            method=f"method{i}", fair_value=neutral or Decimal("1"), confidence=confidence
        )
        for i in range(methods)
    ]
    return FairValueRange(
        bear=bear,
        neutral=neutral,
        bull=bull,
        overall_confidence=confidence,
        methods_used=used,
        methods_excluded=[],
        usable_for_trading_judgment=usable,
    )


def momentum(trend: TrendClassification) -> MomentumSnapshot:
    return MomentumSnapshot(
        trend_classification=trend,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )


def profit_protection(kind: str) -> ProfitProtectionMetrics | None:
    """kind: none_given / insufficient / none / candidate / strong"""
    if kind == "none_given":
        return None
    if kind == "insufficient":
        return ProfitProtectionMetrics(
            insufficient_data_reason="history_short",
            peak_price_since_entry=None,
            peak_date=None,
            peak_gain_pct=None,
            current_gain_pct=None,
            drawdown_from_peak_pct=None,
            gain_giveback_ratio_pct=None,
            candidate_signal=False,
            strong_signal=False,
        )
    import datetime as dt

    return ProfitProtectionMetrics(
        insufficient_data_reason=None,
        peak_price_since_entry=Decimal("1500"),
        peak_date=dt.date(2026, 1, 5),
        peak_gain_pct=50.0,
        current_gain_pct=25.0,
        drawdown_from_peak_pct=16.7,
        gain_giveback_ratio_pct=50.0,
        candidate_signal=kind in ("candidate", "strong"),
        strong_signal=kind == "strong",
    )


_FAIR_VALUE_CHOICES: dict[str, Callable[[], FairValueRange | None]] = {
    "none": lambda: None,
    "unusable": lambda: fair_value_range(
        bear=Decimal("1000"), neutral=Decimal("1100"), bull=Decimal("1300"), usable=False
    ),
    "low_conf": lambda: fair_value_range(
        bear=Decimal("900"),
        neutral=Decimal("1000"),
        bull=Decimal("1200"),
        confidence=ConfidenceLevel.LOW,
    ),
    "medium_3": lambda: fair_value_range(
        bear=Decimal("1000"),
        neutral=Decimal("1150"),
        bull=Decimal("1300"),
        methods=3,
    ),
    "high_3_tight": lambda: fair_value_range(
        bear=Decimal("1000"),
        neutral=Decimal("1050"),
        bull=Decimal("1100"),
        confidence=ConfidenceLevel.HIGH,
        methods=3,
    ),
    "high_3_wide": lambda: fair_value_range(
        bear=Decimal("500"),
        neutral=Decimal("900"),
        bull=Decimal("1500"),
        confidence=ConfidenceLevel.HIGH,
        methods=3,
    ),
    "no_bull": lambda: fair_value_range(
        bear=Decimal("900"), neutral=Decimal("1000"), bull=None, methods=2
    ),
    "no_bear": lambda: fair_value_range(
        bear=None, neutral=Decimal("1000"), bull=Decimal("1200"), methods=2
    ),
    "no_neutral": lambda: fair_value_range(
        bear=Decimal("900"), neutral=None, bull=Decimal("1200"), methods=2
    ),
}

_MITIGATIONS: dict[str, MitigatingFactorInputs] = {
    "none": MitigatingFactorInputs(),
    "fv_rising": MitigatingFactorInputs(fair_value_rising_with_earnings_growth=True),
    "dividend_streak": MitigatingFactorInputs(continuous_dividend_increase_years=10),
    "dividend_streak_unknown": MitigatingFactorInputs(continuous_dividend_increase_years=None),
    "progressive": MitigatingFactorInputs(is_progressive_or_doe_policy=True),
    "benefit_and_nisa": MitigatingFactorInputs(
        long_term_holding_benefit_imminent=True, is_nisa_account=True
    ),
    "all": MitigatingFactorInputs(
        fair_value_rising_with_earnings_growth=True,
        continuous_dividend_increase_years=10,
        is_progressive_or_doe_policy=True,
        long_term_holding_benefit_imminent=True,
        few_reinvestment_alternatives=True,
        is_nisa_account=True,
    ),
}


@dataclass(frozen=True)
class Case:
    case_id: str
    kwargs: dict[str, Any]


def build_kwargs(
    *,
    price: Decimal,
    fair_value: str,
    trend: TrendClassification | None,
    pp: str,
    executable: bool,
    days: int | None,
    counter: bool,
    industry_model: bool,
    sector: ProfitTakingIndustrySector | None,
    stock_types: tuple[StockType, ...],
    guidance: bool,
    severe: bool,
    dividend_outcome: DividendComparisonOutcome | None,
    cashflow: bool | None,
    concentration: bool,
    earnings_rationale: bool,
    reflects: bool | None,
    yield_pct: float | None,
    mitigation: str,
    target_price: Decimal | None,
    target_rate: float | None,
    premise_broken: bool,
    accounting: bool,
) -> dict[str, Any]:
    condition_inputs = ProfitTakingConditionInputs(
        stock_types=list(stock_types),
        fair_value_range=_FAIR_VALUE_CHOICES[fair_value](),
        momentum=momentum(trend) if trend is not None else None,
        dividend_comparison_outcome=dividend_outcome,
        cashflow_fundamentally_driven=cashflow,
        guidance_revision_disclosed=guidance,
        severe_earnings_decline=severe,
        investment_premise_broken=premise_broken,
        accounting_or_scandal_or_delisting_risk=accounting,
        portfolio_concentration_over_limit=concentration,
        earnings_event_risk_reduction_rationale=earnings_rationale,
        profit_target_price=target_price,
        profit_target_rate=target_rate,
        fair_value_reflects_latest_earnings=reflects,
        industry_model_applied=industry_model,
        industry_sector=sector,
        partial_sale_executable=executable,
        days_to_next_earnings_business_days=days,
        has_strong_counter_material=counter,
        profit_protection=profit_protection(pp),
    )
    return {
        "current_price": price,
        "average_purchase_price": AVERAGE_PRICE,
        "shares": SHARES,
        "total_purchase_amount": TOTAL_AMOUNT,
        "cumulative_dividend_received": Decimal("0"),
        "cumulative_benefit_value_received": Decimal("0"),
        "current_total_yield_pct": yield_pct,
        "forecast_annual_dividend_per_share": Decimal("40"),
        "mitigating_inputs": _MITIGATIONS[mitigation],
        "config": CONFIG,
        "condition_inputs": condition_inputs,
    }


# --- 決定的な抽出 -----------------------------------------------------------------------------

_PRICES = [
    Decimal(p) for p in ("700", "1000", "1010", "1100", "1200", "1250", "1350", "1520", "1700")
]
_TRENDS: list[TrendClassification | None] = [None, *TrendClassification]
_PP = ["none_given", "insufficient", "none", "candidate", "strong"]
_DIVIDEND: list[DividendComparisonOutcome | None] = [
    None,
    DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT,
    DividendComparisonOutcome.DIVIDEND_INCREASE,
]
_STOCK_TYPES: list[tuple[StockType, ...]] = [(), (StockType.GROWTH,), (StockType.INCOME,)]
_TARGETS: list[tuple[Decimal | None, float | None]] = [
    (None, None),
    (Decimal("1300"), None),
    (None, 30.0),
    (Decimal("1700"), 50.0),
]
_YIELDS: list[float | None] = [None, 1.5, 2.2, 4.0]


class _Lcg:
    """標準ライブラリの乱数の実装に依存しない、固定の線形合同法。"""

    def __init__(self, seed: int) -> None:
        self._state = seed % (2**31)

    def below(self, n: int) -> int:
        self._state = (1103515245 * self._state + 12345) % (2**31)
        return (self._state >> 8) % n

    def pick[T](self, items: list[T]) -> T:
        return items[self.below(len(items))]


def sampled_cases(count: int = 4000, seed: int = 20261010) -> Iterator[Case]:
    rng = _Lcg(seed)
    for index in range(count):
        target_price, target_rate = rng.pick(_TARGETS)
        kwargs = build_kwargs(
            price=rng.pick(_PRICES),
            fair_value=rng.pick(list(_FAIR_VALUE_CHOICES)),
            trend=rng.pick(_TRENDS),
            pp=rng.pick(_PP),
            executable=rng.below(5) != 0,
            days=rng.pick([None, 1, 3, 10]),
            counter=rng.below(6) == 0,
            industry_model=rng.below(3) == 0,
            sector=rng.pick([None, ProfitTakingIndustrySector.GENERAL]),
            stock_types=rng.pick(_STOCK_TYPES),
            guidance=rng.below(3) == 0,
            severe=rng.below(4) == 0,
            dividend_outcome=rng.pick(_DIVIDEND),
            cashflow=rng.pick([None, True, False]),
            concentration=rng.below(4) == 0,
            earnings_rationale=rng.below(4) == 0,
            reflects=rng.pick([None, True, False]),
            yield_pct=rng.pick(_YIELDS),
            mitigation=rng.pick(list(_MITIGATIONS)),
            target_price=target_price,
            target_rate=target_rate,
            premise_broken=rng.below(12) == 0,
            accounting=rng.below(12) == 0,
        )
        yield Case(f"sampled-{index:05d}", kwargs)


def focused_cases(count: int = 2000, seed: int = 878) -> Iterator[Case]:
    """適正価格ベースの経路(強い条件・partial gate)と価格 × 上値余地の経路が開く入力に絞った抽出
    (一般の抽出では、これらの gate が全て開く入力が少ないため)。"""
    rng = _Lcg(seed)
    for index in range(count):
        target_price, target_rate = rng.pick(_TARGETS)
        kwargs = build_kwargs(
            price=rng.pick([Decimal(p) for p in ("1120", "1250", "1350", "1520", "1700", "2100")]),
            fair_value=rng.pick(["high_3_tight", "high_3_wide", "medium_3", "low_conf"]),
            trend=rng.pick(_TRENDS),
            pp=rng.pick(_PP),
            executable=rng.below(8) != 0,
            days=rng.pick([2, 10, 20]),
            counter=rng.below(10) == 0,
            industry_model=True,
            sector=rng.pick([None, ProfitTakingIndustrySector.GENERAL]),
            stock_types=rng.pick(_STOCK_TYPES),
            guidance=rng.below(2) == 0,
            severe=rng.below(3) == 0,
            dividend_outcome=rng.pick(_DIVIDEND),
            cashflow=rng.pick([None, True, False]),
            concentration=rng.below(3) == 0,
            earnings_rationale=rng.below(3) == 0,
            reflects=rng.pick([True, True, None, False]),
            yield_pct=rng.pick(_YIELDS),
            mitigation=rng.pick(list(_MITIGATIONS)),
            target_price=target_price,
            target_rate=target_rate,
            premise_broken=False,
            accounting=False,
        )
        yield Case(f"focused-{index:05d}", kwargs)


def boundary_cases() -> Iterator[Case]:
    """閾値ちょうど・その前後の入力(ユーザー目標の到達、含み益・総合利回りの閾値、上限価格 bull との
    比較)。比較演算子の境界(>= と >)を取り違える変更を捕まえるための格子。"""
    combinations = itertools.product(
        [
            Decimal(p)
            for p in ("1199", "1200", "1250", "1299", "1300", "1301", "1499", "1500", "1501")
        ],
        ["none", "medium_3", "high_3_tight"],
        [None, TrendClassification.UPTREND, TrendClassification.DOWNTREND],
        _TARGETS_BOUNDARY,
        [None, 2.5, 2.4, 2.0, 1.9],
    )
    for index, (price, fair_value, trend, (target_price, target_rate), yield_pct) in enumerate(
        combinations
    ):
        kwargs = build_kwargs(
            price=price,
            fair_value=fair_value,
            trend=trend,
            pp="none_given",
            executable=True,
            days=10,
            counter=False,
            industry_model=True,
            sector=None,
            stock_types=(),
            guidance=False,
            severe=False,
            dividend_outcome=None,
            cashflow=None,
            concentration=False,
            earnings_rationale=False,
            reflects=True,
            yield_pct=yield_pct,
            mitigation="none",
            target_price=target_price,
            target_rate=target_rate,
            premise_broken=False,
            accounting=False,
        )
        yield Case(f"boundary-{index:05d}", kwargs)


_TARGETS_BOUNDARY: list[tuple[Decimal | None, float | None]] = [
    (None, None),
    (Decimal("1300"), None),
    (None, 30.0),
    (None, 50.0),
]


def gate_cases() -> Iterator[Case]:
    """適正価格ベースの経路の gate が『単独で』効く入力(一般の抽出では現れにくい)。

    - 一部利確の gate(`partial_count >= min or fv_partial_gate_ok` の右の項): 件数条件の票を
      持たない(利回り・トレンド悪化・集中度・成長鈍化の票なし)入力で、適正価格の gate だけが
      PARTIAL へ導く。強気適正価格超過の閾値ちょうど(25.0%)とその前後を含む
    - 上限価格(ceiling)が使えない業種区分(`industry_classification` 未指定かつ業種モデル未適用)
      でも、GENERAL 業種なら gate は開く(Issue #583)。このとき価格 × 上値余地の経路は WATCH 止まり
      になり、適正価格の gate が単独で最終判定を決める
    - 全株の強い条件(FAIR_VALUE_STRONG): 高信頼度・強気超過・業績予想の下方修正・最新決算の反映
    """
    combinations = itertools.product(
        [Decimal(p) for p in ("1624", "1625", "1626", "1700", "1800", "2100")],
        ["medium_3", "high_3_tight", "high_3_wide"],
        [None, TrendClassification.UPTREND],
        [(False, False), (True, False), (False, True)],
        [3, 10],
        [True, False],
        [True, False],
        [True, False],
    )
    for index, (
        price,
        fair_value,
        trend,
        (guidance, severe),
        days,
        executable,
        reflects,
        ceiling_usable,
    ) in enumerate(combinations):
        kwargs = build_kwargs(
            price=price,
            fair_value=fair_value,
            trend=trend,
            pp="none_given",
            executable=executable,
            days=days,
            counter=False,
            industry_model=ceiling_usable,
            sector=None if ceiling_usable else ProfitTakingIndustrySector.GENERAL,
            stock_types=(),
            guidance=guidance,
            severe=severe,
            dividend_outcome=None,
            cashflow=None,
            concentration=False,
            earnings_rationale=False,
            reflects=reflects,
            yield_pct=4.0,
            mitigation="none",
            target_price=None,
            target_rate=None,
            premise_broken=False,
            accounting=False,
        )
        yield Case(f"gate-{index:05d}", kwargs)


def missing_input_cases() -> Iterator[Case]:
    """入力が欠けた場合を全て列挙する(含み損・適正価格が使えない・bull 等の欠落・momentum なし・
    候補なし)。hard_overvalued の式を uptrend の枝の外で常に算出しても、例外を出さない。"""
    combinations = itertools.product(
        _PRICES,
        list(_FAIR_VALUE_CHOICES),
        _TRENDS,
        ["none_given", "strong"],
        ["none", "all"],
    )
    for index, (price, fair_value, trend, pp, mitigation) in enumerate(combinations):
        kwargs = build_kwargs(
            price=price,
            fair_value=fair_value,
            trend=trend,
            pp=pp,
            executable=True,
            days=10,
            counter=False,
            industry_model=True,
            sector=None,
            stock_types=(),
            guidance=False,
            severe=False,
            dividend_outcome=None,
            cashflow=None,
            concentration=False,
            earnings_rationale=False,
            reflects=True,
            yield_pct=None,
            mitigation=mitigation,
            target_price=None,
            target_rate=None,
            premise_broken=False,
            accounting=False,
        )
        yield Case(f"missing-{index:05d}", kwargs)


def evaluate(function: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    """例外は型名の文字列で返す(例外が出ること自体も characterization の対象)。"""
    try:
        return function(**kwargs)
    except Exception as exc:  # noqa: BLE001 - 例外の型を固定するための捕捉
        return f"EXC:{type(exc).__name__}"


def digest(result: Any) -> str:
    """結果の全 field を含む repr のハッシュ(結果が 1 つでも変わると変わる)。"""
    return hashlib.sha256(repr(result).encode("utf-8")).hexdigest()[:24]


def compact(result: Any) -> str:
    """golden に保存する 1 行: ハッシュ|最終 action|origin|売却強度(読む人の確認用。照合は全体)。"""
    if isinstance(result, str):
        return result
    s = summary(result)
    return f"{digest(result)}|{s['final_action']}|{s['origin']}|{s['sell_intensity']}"


def summary(result: Any) -> dict[str, Any]:
    """読めるように、主要な field を併記する(照合は digest が担う)。"""
    if isinstance(result, str):
        return {"exception": result}
    return {
        "final_action": result.final_action.value,
        "fundamental_action": result.fundamental_action.value,
        "timing_action": result.timing_action.value,
        "origin": result.origin,
        "sell_intensity": result.sell_intensity.value if result.sell_intensity else None,
        "mitigating_downgrade_applied": result.mitigating_downgrade_applied,
        "timing_downgrade_applied": result.timing_downgrade_applied,
    }
