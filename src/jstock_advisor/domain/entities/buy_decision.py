"""購入判断の理由(2026-07 BUYパイプライン再設計。要求仕様18節)。

通知層は判定理由を再計算せず、サービス層で確定したBuyDecisionReasonを
そのまま表示する(要求仕様18節末尾)。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot


class BuyDecisionReason(ImmutableSnapshot):
    code: str
    message: str
    actual_value: Decimal | float | str | None = None
    threshold_value: Decimal | float | str | None = None
    source: str | None = None
