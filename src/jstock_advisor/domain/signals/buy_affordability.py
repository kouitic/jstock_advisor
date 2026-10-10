"""BUY 側の購入可能株数(Issue #603 M2。#128 Portfolio Capital Allocation の部品。dormant)。

既に選ばれた 1 候補について、買付余力・1 銘柄最大投資額・1 銘柄最大保有比率のもとで、最大何株まで
買えるか(売買単位の倍数・床)を返す純粋関数。SELL 側の `trading_unit_feasibility` と対になる。
どこからも import されない(現行の判定・通知は変わらない)。RAER による銘柄の選択・複数候補の配分
(optimizer)・段階買い・業種上限・単元未満株の購入は、この関数の責務ではない(足さない)。

## 再利用する既存の意味論

    ・売買単位   config の default_trading_unit(TSE 全銘柄 100 株。呼び出し側が渡す)。
                 単元未満株の購入は仮定しない(最小単位 = minimum_trading_unit)
    ・買付余力   AvailableCashService.get() の戻り値をそのまま渡す。
                 ★ None = 未登録 は 0 円ではない ★(#584 / #589)。None は
                 AVAILABLE_CASH_UNAVAILABLE(株数 0)、0 円は ZERO_AVAILABLE_CASH と別の理由にして、
                 呼び出し側が区別できるようにする
    ・銘柄集中度 add_on_risk の投影『購入後の構成比 = (保有額 + 購入額) / (総額 + 購入額)』
                 (分母に購入額を含む)に揃える。この関数が返す株数は add_on_risk の判定を通り、
                 次の 1 単元を足すと通らない。総額が使えない(None・0 以下・保有額より小さい)のに
                 比率上限が設定されているときは、上限を無視せず購入不可にする
                 (fail-close。add_on_risk の方針)

## 計算の約束

    ・金額は Decimal のみ(float・int・bool は拒否)。除算・床は Fraction による厳密な有理数で行い、
      Decimal の演算精度(丸め)に依存しない
    ・株数は常に『床』(切り捨て)。天井や四捨五入を使わないことで、購入総額 <= 買付余力・各上限を
      超えないことを計算方法そのもので保証する(実行時のチェックに頼らない)
    ・入力の矛盾(負値・0 以下の価格・NaN・型の誤り)は専用の例外で拒否する。呼び出し側が 1 件ずつ
      捕捉して『読めない入力』として数える前提(捕捉しないと 1 件の破損でバッチ全体が止まる)。
      例外のメッセージに金額・株数などの値は含めない
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction


class BuyAffordabilityInputError(ValueError):
    """入力の矛盾(型・符号・範囲)。呼び出し側が 1 件ずつ捕捉して『読めない入力』として数える。"""


class BuyAffordabilityReason(StrEnum):
    #: 購入可能(余力が最も効いた制約。または上限が余力の範囲を狭めていない)
    AFFORDABLE = "AFFORDABLE"
    #: 買付余力が未登録(None)。0 円とは別。他の計算は一切しない
    AVAILABLE_CASH_UNAVAILABLE = "AVAILABLE_CASH_UNAVAILABLE"
    #: 買付余力が 0 円(登録済み)
    ZERO_AVAILABLE_CASH = "ZERO_AVAILABLE_CASH"
    #: 買付余力が 1 単元の代金に届かない
    INSUFFICIENT_CASH_FOR_MINIMUM_UNIT = "INSUFFICIENT_CASH_FOR_MINIMUM_UNIT"
    #: 1 銘柄最大投資額が購入可能株数を狭めた(0 株になった場合を含む)
    POSITION_AMOUNT_CAP_APPLIED = "POSITION_AMOUNT_CAP_APPLIED"
    #: 1 銘柄最大保有比率が購入可能株数を狭めた(0 株になった場合を含む)
    POSITION_RATIO_CAP_APPLIED = "POSITION_RATIO_CAP_APPLIED"
    #: 比率上限が設定されているのに、ポートフォリオ総額が使えない(fail-close)
    PORTFOLIO_VALUE_UNAVAILABLE = "PORTFOLIO_VALUE_UNAVAILABLE"


_ZERO_SHARE_REASONS = frozenset(
    {
        BuyAffordabilityReason.AVAILABLE_CASH_UNAVAILABLE,
        BuyAffordabilityReason.ZERO_AVAILABLE_CASH,
        BuyAffordabilityReason.INSUFFICIENT_CASH_FOR_MINIMUM_UNIT,
        BuyAffordabilityReason.PORTFOLIO_VALUE_UNAVAILABLE,
    }
)


@dataclass(frozen=True)
class BuyAffordability:
    """購入可能株数の結果。"""

    max_affordable_shares: int
    max_affordable_units: int
    #: 現時点では max_affordable_shares と同値。将来の追加の制約(段階買い等)を載せる余地
    recommended_upper_bound_shares: int
    feasible: bool
    reason_code: BuyAffordabilityReason

    def __post_init__(self) -> None:
        if self.max_affordable_shares < 0 or self.max_affordable_units < 0:
            raise ValueError("BuyAffordability: negative quantity")
        if self.recommended_upper_bound_shares != self.max_affordable_shares:
            raise ValueError("BuyAffordability: recommended bound differs from the maximum")
        if self.feasible != (self.max_affordable_shares > 0):
            raise ValueError("BuyAffordability: feasible must mean a positive quantity")
        if (self.max_affordable_units > 0) != self.feasible:
            raise ValueError("BuyAffordability: units and feasible disagree")
        if not self.feasible and self.reason_code is BuyAffordabilityReason.AFFORDABLE:
            raise ValueError("BuyAffordability: AFFORDABLE needs a positive quantity")
        if self.feasible and self.reason_code in _ZERO_SHARE_REASONS:
            raise ValueError("BuyAffordability: this reason implies zero shares")


def _require_decimal(name: str, value: object, *, minimum: Decimal | None = None) -> Decimal:
    # bool は int のサブクラス、float・int は Decimal ではない。金額に float を混ぜない
    if not isinstance(value, Decimal):
        raise BuyAffordabilityInputError(f"{name} must be a Decimal")
    if not value.is_finite():
        raise BuyAffordabilityInputError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise BuyAffordabilityInputError(f"{name} is below its minimum")
    return value


def _floor_units(numerator: Fraction, denominator: Fraction) -> int:
    """numerator / denominator の床(負なら 0)。厳密な有理数で行う。"""
    if numerator <= 0:
        return 0
    return int(numerator // denominator)


def compute_buy_affordability(
    *,
    available_cash: Decimal | None,
    candidate_price: Decimal,
    minimum_trading_unit: int,
    current_position_value: Decimal,
    portfolio_value: Decimal | None = None,
    max_single_stock_amount: Decimal | None = None,
    max_single_stock_ratio: Decimal | None = None,
) -> BuyAffordability:
    """既に選ばれた 1 候補について、各制約のもとで最大何株まで買えるか(単元の倍数・床)。

    available_cash          AvailableCashService.get() の戻り値。None = 未登録(0 円ではない)
    candidate_price         現在価格(> 0)。取得単価は使わない
    minimum_trading_unit    最小の購入単位(> 0。TSE は 100)
    current_position_value  当該銘柄の現在保有評価額(>= 0。未保有 = 0)
    portfolio_value         owner 全体の保有評価額の合計(当該銘柄を含む。買付余力は含まない)。
                            比率上限が設定されているときだけ使う。呼び出し側は、全保有銘柄の
                            時価が判明している場合だけ非 None で渡す(add_on_risk の前提)
    max_single_stock_amount 1 銘柄最大投資額(購入後の保有評価額の上限。保有評価額を含む)
    max_single_stock_ratio  1 銘柄最大保有比率(0 < r <= 1。購入後の構成比の上限)

    理由の優先順位: 余力が未登録 > 余力が 0 円 > 比率上限があるのに総額が使えない >
    余力が 1 単元に届かない > 上限が効いた(金額 > 比率の順で同点を解決)> 購入可能。
    """
    cash = (
        None
        if available_cash is None
        else _require_decimal("available_cash", available_cash, minimum=Decimal(0))
    )
    price = _require_decimal("candidate_price", candidate_price)
    if price <= 0:
        raise BuyAffordabilityInputError("candidate_price must be positive")
    if isinstance(minimum_trading_unit, bool) or not isinstance(minimum_trading_unit, int):
        raise BuyAffordabilityInputError("minimum_trading_unit must be an int")
    if minimum_trading_unit <= 0:
        raise BuyAffordabilityInputError("minimum_trading_unit must be positive")
    position = _require_decimal(
        "current_position_value", current_position_value, minimum=Decimal(0)
    )
    total = (
        None
        if portfolio_value is None
        else _require_decimal("portfolio_value", portfolio_value, minimum=Decimal(0))
    )
    amount_cap = (
        None
        if max_single_stock_amount is None
        else _require_decimal(
            "max_single_stock_amount", max_single_stock_amount, minimum=Decimal(0)
        )
    )
    ratio_cap = (
        None
        if max_single_stock_ratio is None
        else _require_decimal("max_single_stock_ratio", max_single_stock_ratio)
    )
    if ratio_cap is not None and not (Decimal(0) < ratio_cap <= Decimal(1)):
        raise BuyAffordabilityInputError("max_single_stock_ratio must be in (0, 1]")

    unit = minimum_trading_unit

    def result(units: int, reason: BuyAffordabilityReason) -> BuyAffordability:
        shares = units * unit
        return BuyAffordability(
            max_affordable_shares=shares,
            max_affordable_units=units,
            recommended_upper_bound_shares=shares,
            feasible=units > 0,
            reason_code=reason,
        )

    # 1 未登録は最優先。他の計算は一切しない(0 円と取り違えない)
    if cash is None:
        return result(0, BuyAffordabilityReason.AVAILABLE_CASH_UNAVAILABLE)
    # 2 登録済みの 0 円(未登録とは別の理由)
    if cash == 0:
        return result(0, BuyAffordabilityReason.ZERO_AVAILABLE_CASH)

    # 3 比率上限(< 1)があるのに総額が使えない -> 上限を無視せず購入不可(fail-close)。
    #   r = 1 は上限なしと同じ
    ratio_active = ratio_cap is not None and ratio_cap < 1
    if ratio_active and (total is None or total <= 0 or total < position):
        return result(0, BuyAffordabilityReason.PORTFOLIO_VALUE_UNAVAILABLE)

    unit_cost = Fraction(price) * unit
    cash_units = _floor_units(Fraction(cash), unit_cost)
    amount_units = (
        None
        if amount_cap is None
        else _floor_units(Fraction(amount_cap) - Fraction(position), unit_cost)
    )
    ratio_units = None
    if ratio_active:
        assert ratio_cap is not None and total is not None
        r = Fraction(ratio_cap)
        # (P + a) / (T + a) <= r  <=>  a <= (r * T - P) / (1 - r)   (a = 購入額、r < 1)
        ratio_units = _floor_units((r * Fraction(total) - Fraction(position)) / (1 - r), unit_cost)

    units = cash_units
    if amount_units is not None:
        units = min(units, amount_units)
    if ratio_units is not None:
        units = min(units, ratio_units)

    if cash_units == 0:
        return result(0, BuyAffordabilityReason.INSUFFICIENT_CASH_FOR_MINIMUM_UNIT)
    if units < cash_units:
        # 上限が余力の範囲を狭めた。金額と比率が同じ株数なら金額を理由にする(決定的)
        if amount_units is not None and amount_units == units:
            return result(units, BuyAffordabilityReason.POSITION_AMOUNT_CAP_APPLIED)
        return result(units, BuyAffordabilityReason.POSITION_RATIO_CAP_APPLIED)
    return result(units, BuyAffordabilityReason.AFFORDABLE)
