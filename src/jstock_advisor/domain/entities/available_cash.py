"""owner単位の買付余力(available_cash)の永続モデル(Issue #584、#128 A1)。

#128(Portfolio Capital Allocation Epic)本体(配分最適化・期待リターン・
Capital Rotation)には接続しない独立した基盤機能である。trade登録時の
cash増減・atomicity・insufficient cash拒否等はA1のscope外(将来のA2以降で
別Issueとして扱う)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import model_validator

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import AvailableCashUpdateType
from jstock_advisor.domain.jst import require_timezone_aware


class AvailableCash(Entity):
    """owner単位で1レコード(ownerなしのglobal cashは作らない)。

    レコード不存在(UNKNOWN/NOT_INITIALIZED)と、存在するが0円であることを
    区別する。この区別はrepository層の`get()`が`None`を返すことで表現し、
    本entity自体に「未初期化」を表す値は持たせない(0円はamount=0という
    正当な状態であり、未初期化の代用にしない)。

    - `updated_at`: 残高そのものが最後に変更された時刻。
    - `last_reconciled_at`: 利用者が証券会社等の実額と照合して残高を明示的に
      棚卸しした最終時刻。TRADE_UPDATEでupdated_atが新しくなっても
      last_reconciled_atは更新しない(売買継続と棚卸し済みを混同しない)。
      未棚卸しの場合はNone。
    """

    owner: str
    available_cash: Decimal
    updated_at: dt.datetime
    last_update_type: AvailableCashUpdateType
    last_reconciled_at: dt.datetime | None = None

    @model_validator(mode="after")
    def _check_non_negative(self) -> AvailableCash:
        if self.available_cash < 0:
            raise ValueError(
                f"available_cashは0以上である必要があります(現在{self.available_cash})"
            )
        return self

    @model_validator(mode="after")
    def _check_timestamps_timezone_aware(self) -> AvailableCash:
        require_timezone_aware(self.updated_at)
        if self.last_reconciled_at is not None:
            require_timezone_aware(self.last_reconciled_at)
        return self
