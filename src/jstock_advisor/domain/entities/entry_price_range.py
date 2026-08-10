"""判定精度向上機能次フェーズSTEP2: Entry Price Range Shadowの評価結果
スナップショット。

既存のBUY判定(entry_buy_price/standard_buy_price/strong_buy_price/
buy_prices)には一切影響しない、DecisionSnapshot記録専用のShadow計測値。
Historical Valuation Score/Timing Scoreのような単一のscoreフィールドを
持たず、代わりに4段階の価格(strong/preferred/starter/max)とstop_review_
priceを持つため、DecisionPerformance分析等が「評価済みレコードだけを
安全に抽出する」ための識別子としてstateを明示フィールド化している。

不変条件(model_validator、コードレビュー対応STEP2 §7・§12・§14、および
残Medium対応でvaluation_ceiling自体の必須・正値検証を追加):
- state=EVALUATEDなら4価格・valuation_ceilingすべて必須・正であり、
  strong<=preferred<=starter<=max<=valuation_ceilingの順序を満たす。
- state=NOT_EVALUATED/NOT_APPLICABLEなら4価格・confidenceはすべてNone
  (Entry Price Range全体が評価不能な場合、stop_review_priceも含め全ての
  価格をNoneとする。理由: state=NOT_EVALUATEDと一部価格の非Noneが共存する
  状態を避け、DecisionPerformance分析・監査ロジックを単純に保つため。
  fair_value_range.bearのみからstop_review_priceだけを算出できる場合でも、
  Entry Price Range全体としては「評価不能」を優先する)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import model_validator

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import ConfidenceLevel, PriceRangeEvaluationState
from jstock_advisor.domain.jst import require_timezone_aware


class EntryPriceRangeResult(ImmutableSnapshot):
    state: PriceRangeEvaluationState
    current_price: Decimal
    # = fair_value_range.neutral(調整前、監査・不変条件検証用の絶対上限)。
    valuation_ceiling: Decimal | None = None

    starter_entry_price: Decimal | None = None
    preferred_entry_price: Decimal | None = None
    strong_entry_price: Decimal | None = None
    max_entry_price: Decimal | None = None
    # = fair_value_range.bear(bearが無ければNone)。「損切りライン」ではなく
    # 「弱気シナリオに達した場合に前提を見直すべき参考価格」の意味。
    stop_review_price: Decimal | None = None

    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0
    reason_codes: tuple[str, ...] = ()
    # top-down正規化により、いずれかの価格がmin()キャップで実際に
    # 引き下げられた場合True(監査用)。
    ordering_adjusted: bool = False

    evaluated_at: dt.datetime
    model_version: str

    @model_validator(mode="after")
    def _validate_invariants(self) -> EntryPriceRangeResult:
        require_timezone_aware(self.evaluated_at)
        if self.current_price <= 0:
            raise ValueError("current_priceは正である必要があります")
        if not (0 <= self.coverage <= 1):
            raise ValueError("coverageは0〜1の範囲である必要があります")
        if not self.model_version:
            raise ValueError("model_versionは必須です")

        prices = (
            self.strong_entry_price,
            self.preferred_entry_price,
            self.starter_entry_price,
            self.max_entry_price,
        )
        if self.state == PriceRangeEvaluationState.EVALUATED:
            if self.confidence is None:
                raise ValueError("state=EVALUATEDならconfidenceは必須です")
            if any(p is None for p in prices):
                raise ValueError(
                    "state=EVALUATEDなら4価格(strong/preferred/starter/max)は全て必須です"
                )
            strong = self.strong_entry_price
            preferred = self.preferred_entry_price
            starter = self.starter_entry_price
            max_price = self.max_entry_price
            assert strong is not None  # noqa: S101 型絞り込み用(mypy対応)
            assert preferred is not None  # noqa: S101
            assert starter is not None  # noqa: S101
            assert max_price is not None  # noqa: S101
            if any(p <= 0 for p in (strong, preferred, starter, max_price)):
                raise ValueError("state=EVALUATEDなら4価格は全て正である必要があります")
            if not (strong <= preferred <= starter <= max_price):
                raise ValueError(
                    "strong_entry_price<=preferred_entry_price<=starter_entry_price"
                    "<=max_entry_priceである必要があります"
                )
            if self.valuation_ceiling is None:
                raise ValueError("state=EVALUATEDならvaluation_ceilingは必須です")
            if self.valuation_ceiling <= 0:
                raise ValueError("valuation_ceilingは正である必要があります")
            if max_price > self.valuation_ceiling:
                raise ValueError("max_entry_priceはvaluation_ceilingを超えてはいけません")
            if self.stop_review_price is not None and self.stop_review_price <= 0:
                raise ValueError("stop_review_priceを設定する場合は正である必要があります")
        else:
            if any(p is not None for p in prices):
                raise ValueError("state=NOT_EVALUATED/NOT_APPLICABLEでは4価格を生成しません")
            if self.stop_review_price is not None:
                raise ValueError(
                    "state=NOT_EVALUATED/NOT_APPLICABLEではstop_review_priceもNoneに"
                    "する必要があります(Entry Price Range全体が評価不能な場合、"
                    "一部価格のみ非Noneにはしない)"
                )
            if self.confidence is not None:
                raise ValueError(
                    "state=NOT_EVALUATED/NOT_APPLICABLEではconfidenceはNoneである必要があります"
                )
        return self
