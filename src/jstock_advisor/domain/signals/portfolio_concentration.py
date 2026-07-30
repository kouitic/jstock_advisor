"""ポートフォリオ集中リスク判定(2026-07仕様レビュー対応・要求仕様§14)。

銘柄単体の利確・売却条件に該当しなくても、ポートフォリオ内の保有比率が高い場合は
別途通知対象とする。企業価値判断(sell_signal/profit_taking)とは独立した判定の
ため、既存の判定結果とは別に評価・通知する。

業種(セクター)別集中度は、業種分類に財務データの完全取得が必要でありポートフォリオ
全銘柄の一括取得コストが高いため、本パスでは実装しない(捏造しない方針。個別銘柄の
時価・取得価格ベースの集中度のみを対象とする)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortfolioConcentrationResult:
    portfolio_weight_pct: float | None
    acquisition_cost_weight_pct: float | None
    is_concentrated: bool
    reasons: list[str]


def evaluate_portfolio_concentration(
    portfolio_weight_pct: float | None,
    acquisition_cost_weight_pct: float | None,
    single_stock_weight_threshold_pct: float,
) -> PortfolioConcentrationResult:
    """1銘柄の時価または取得価格ベースの保有比率が閾値以上の場合に集中警告とする。"""
    reasons: list[str] = []
    if (
        portfolio_weight_pct is not None
        and portfolio_weight_pct >= single_stock_weight_threshold_pct
    ):
        reasons.append(f"時価ベースの保有比率が{portfolio_weight_pct:.1f}%と高い")
    if (
        acquisition_cost_weight_pct is not None
        and acquisition_cost_weight_pct >= single_stock_weight_threshold_pct
    ):
        reasons.append(f"取得価格ベースの保有比率が{acquisition_cost_weight_pct:.1f}%と高い")
    return PortfolioConcentrationResult(
        portfolio_weight_pct=portfolio_weight_pct,
        acquisition_cost_weight_pct=acquisition_cost_weight_pct,
        is_concentrated=bool(reasons),
        reasons=reasons,
    )
