"""保有株数・売買単位を考慮した一部売却の実行可能性(2026-07仕様レビュー対応・要求仕様§3)。

TSE上場銘柄の単元株数は2018年10月に全銘柄100株へ統一済みであり、Providerから
個別取得する手段が無いためこの制度的事実を既定値として使う。単元未満株取引が
実際に利用可能かどうかは銘柄・口座ごとに異なり自動判定できないため、既定はFalse
(捏造しない)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradingUnitFeasibility:
    trading_unit: int
    minimum_sellable_shares: int
    partial_sale_executable: bool
    suggested_sell_shares: int | None
    odd_lot_trading_available: bool


def evaluate_trading_unit_feasibility(
    shares: int,
    trading_unit: int,
    odd_lot_trading_available: bool,
) -> TradingUnitFeasibility:
    """一部売却の実行可能性を判定する。

    保有株数が売買単位以下で、かつ単元未満株取引が利用できない場合、一部売却は
    実行不可(全部売却または保有継続のみが選択肢)とする。
    """
    minimum_sellable_shares = 1 if odd_lot_trading_available else trading_unit
    if shares <= trading_unit and not odd_lot_trading_available:
        return TradingUnitFeasibility(
            trading_unit=trading_unit,
            minimum_sellable_shares=minimum_sellable_shares,
            partial_sale_executable=False,
            suggested_sell_shares=None,
            odd_lot_trading_available=odd_lot_trading_available,
        )

    if odd_lot_trading_available:
        # 単元未満株取引が可能な場合のみ、保有株数の概ね半分程度(単位未満株丸め)を提案する。
        suggested = max(1, shares // 2)
    else:
        # 単元株単位で、保有株数の半分に最も近い単元の倍数を提案する。
        units_held = shares // trading_unit
        suggested_units = max(1, units_held // 2)
        suggested = suggested_units * trading_unit

    return TradingUnitFeasibility(
        trading_unit=trading_unit,
        minimum_sellable_shares=minimum_sellable_shares,
        partial_sale_executable=True,
        suggested_sell_shares=suggested,
        odd_lot_trading_available=odd_lot_trading_available,
    )
