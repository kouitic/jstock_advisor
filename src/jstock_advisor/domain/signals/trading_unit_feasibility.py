"""保有株数・売買単位を考慮した一部売却の実行可能性・数量(2026-07仕様レビュー
対応・要求仕様§3、2026-08コードレビュー対応Part B)。

TSE上場銘柄の単元株数は2018年10月に全銘柄100株へ統一済みであり、Providerから
個別取得する手段が無いためこの制度的事実を既定値として使う。単元未満株取引が
実際に利用可能かどうかは銘柄・口座ごとに異なり自動判定できないため、既定はFalse
(捏造しない)。

「一部売却という行為が可能か」(evaluate_trading_unit_feasibility、判定成立前に
必要)と「可能な場合に何株売るべきか」(compute_suggested_sell_shares、判定強度
(SellIntensity)が決まった後にのみ計算できる)を分離する(コードレビュー対応
2026-08、指摘B-4)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradingUnitFeasibility:
    trading_unit: int
    minimum_sellable_shares: int
    partial_sale_executable: bool
    odd_lot_trading_available: bool


@dataclass(frozen=True)
class SuggestedSellShares:
    shares: int
    ratio: float


def evaluate_trading_unit_feasibility(
    shares: int,
    trading_unit: int,
    odd_lot_trading_available: bool,
) -> TradingUnitFeasibility:
    """一部売却という行為そのものが実行可能か(数量は含まない)。

    保有株数が売買単位以下で、かつ単元未満株取引が利用できない場合、一部売却は
    実行不可(全部売却または保有継続のみが選択肢)とする。
    """
    minimum_sellable_shares = 1 if odd_lot_trading_available else trading_unit
    partial_sale_executable = odd_lot_trading_available or shares > trading_unit
    return TradingUnitFeasibility(
        trading_unit=trading_unit,
        minimum_sellable_shares=minimum_sellable_shares,
        partial_sale_executable=partial_sale_executable,
        odd_lot_trading_available=odd_lot_trading_available,
    )


def compute_suggested_sell_shares(
    shares: int,
    trading_unit: int,
    odd_lot_trading_available: bool,
    target_sell_ratio: float,
) -> SuggestedSellShares | None:
    """PARTIAL_PROFIT_TAKE成立後、判定強度から実際に提案する売却株数を算出する
    (要求仕様§B-8: floor方式)。

    呼び出し前提: partial_sale_executable=True(evaluate_trading_unit_
    feasibility()で確認済み)であること。実行不可の場合の呼び出しは想定しない
    (呼び出し側の責務)。

    保証する制約(要求仕様§B-3・B-9):
    - suggested_shares >= minimum_sellable_shares(単元未満を提案しない)
    - remaining_shares = shares - suggested_shares >= minimum_sellable_shares
      (PARTIALで全量売却しない、売却後も最低1単元残す)
    - suggested_shares < shares
    """
    if odd_lot_trading_available:
        raw = int(shares * target_sell_ratio)
        suggested = max(1, min(raw, shares - 1))
        return SuggestedSellShares(shares=suggested, ratio=suggested / shares)

    unit_count = shares // trading_unit
    if unit_count < 2:
        # 呼び出し前提(partial_sale_executable=True)が満たされていない防御的
        # ガード(1単元以下では一部売却できない)。
        return None

    raw_units = int((shares * target_sell_ratio) // trading_unit)
    max_units = unit_count - 1  # 売却後も最低1単元(minimum_sellable_shares)残す
    suggested_units = max(1, min(raw_units, max_units))
    suggested = suggested_units * trading_unit
    return SuggestedSellShares(shares=suggested, ratio=suggested / shares)
