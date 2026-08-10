"""判定精度向上機能次フェーズSTEP2: Exit Price Range Shadowの評価結果
スナップショット。

既存のSELL(legacy)判定・ProfitTaking判定(sell_prices)には一切影響しない、
DecisionSnapshot記録専用のShadow計測値。

不変条件(model_validator、コードレビュー対応STEP2 §8・§11・§12・§14):
- state=EVALUATEDならpartial_profit_take_low_price<=partial_profit_take_
  high_price<=strong_profit_take_priceの順序を満たし、全て正である必要がある。
- downside_review_price/exit_review_priceは、上記3価格とは意味の異なる
  average_purchase_price基準の別系統であり、順序不変条件の対象外だが、
  state=EVALUATEDの場合は正であることを検証する。
- state=NOT_EVALUATED/NOT_APPLICABLEなら、downside_review_price/exit_
  review_priceを含む5価格すべてがNoneである必要がある(コードレビュー
  対応STEP2 §11: これら2価格はaverage_purchase_priceのみから技術的には
  算出可能だが、state=NOT_EVALUATEDと一部価格の非Noneが共存する状態を
  避けるため、Exit Price Range全体が評価不能な場合は一律Noneとする。
  「取得単価のみを使うReview Range」を将来必要とする場合は、本Resultとは
  別の独立した概念として新設する)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import model_validator

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import ConfidenceLevel, PriceRangeEvaluationState
from jstock_advisor.domain.jst import require_timezone_aware


class ExitPriceRangeResult(ImmutableSnapshot):
    state: PriceRangeEvaluationState
    current_price: Decimal
    # 監査用(調整前のfair value)。
    neutral_anchor: Decimal | None = None
    bull_anchor: Decimal | None = None

    partial_profit_take_low_price: Decimal | None = None
    partial_profit_take_high_price: Decimal | None = None
    strong_profit_take_price: Decimal | None = None
    # average_purchase_price基準の別系統(上記3価格には一切影響しない)。
    downside_review_price: Decimal | None = None
    exit_review_price: Decimal | None = None

    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0
    reason_codes: tuple[str, ...] = ()
    ordering_adjusted: bool = False

    evaluated_at: dt.datetime
    model_version: str

    @model_validator(mode="after")
    def _validate_invariants(self) -> ExitPriceRangeResult:
        require_timezone_aware(self.evaluated_at)
        if self.current_price <= 0:
            raise ValueError("current_priceは正である必要があります")
        if not (0 <= self.coverage <= 1):
            raise ValueError("coverageは0〜1の範囲である必要があります")
        if not self.model_version:
            raise ValueError("model_versionは必須です")

        core_prices = (
            self.partial_profit_take_low_price,
            self.partial_profit_take_high_price,
            self.strong_profit_take_price,
        )
        review_prices = (self.downside_review_price, self.exit_review_price)

        if self.state == PriceRangeEvaluationState.EVALUATED:
            if self.confidence is None:
                raise ValueError("state=EVALUATEDならconfidenceは必須です")
            if any(p is None for p in core_prices):
                raise ValueError(
                    "state=EVALUATEDならpartial_profit_take_low/high_price・"
                    "strong_profit_take_priceは全て必須です"
                )
            partial_low = self.partial_profit_take_low_price
            partial_high = self.partial_profit_take_high_price
            strong = self.strong_profit_take_price
            assert partial_low is not None  # noqa: S101
            assert partial_high is not None  # noqa: S101
            assert strong is not None  # noqa: S101
            if any(p <= 0 for p in (partial_low, partial_high, strong)):
                raise ValueError("state=EVALUATEDなら3価格は全て正である必要があります")
            if not (partial_low <= partial_high <= strong):
                raise ValueError(
                    "partial_profit_take_low_price<=partial_profit_take_high_price"
                    "<=strong_profit_take_priceである必要があります"
                )
            if any(p is not None and p <= 0 for p in review_prices):
                raise ValueError(
                    "downside_review_price/exit_review_priceを設定する場合は正である必要があります"
                )
        else:
            if any(p is not None for p in core_prices) or any(p is not None for p in review_prices):
                raise ValueError(
                    "state=NOT_EVALUATED/NOT_APPLICABLEでは5価格(partial_low/high・"
                    "strong・downside_review・exit_review)すべてNoneである必要が"
                    "あります"
                )
            if self.confidence is not None:
                raise ValueError(
                    "state=NOT_EVALUATED/NOT_APPLICABLEではconfidenceはNoneである必要があります"
                )
        return self
