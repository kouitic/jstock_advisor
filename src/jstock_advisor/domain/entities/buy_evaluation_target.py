"""気になる銘柄と保有銘柄を統合したBUY候補評価対象(2026-07)。

対象統合の時点では静的な保有情報(株数・平均取得単価)のみを保持し、
株価・評価損益(current_market_value/unrealized_profit_loss等)は一切
計算しない。これらはBuySignalServiceの分析で実際に使われたcurrent_price
から事後計算する(対象統合時とワーカー実行時とで株価取得が二重にならない
ようにするため)。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.domain.entities.enums import CandidateSource


@dataclass(frozen=True)
class BuyEvaluationTarget:
    stock_code: str
    stock_name: str | None
    source: CandidateSource
    holding_quantity: int | None
    average_acquisition_price: Decimal | None
