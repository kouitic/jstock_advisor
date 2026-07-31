"""統合BUY候補パイプラインの通知可否判定結果(2026-07)。

データ品質・買い増し固有リスク(集中度・売却競合等)・再送防止・ランキング
上限のいずれかで通知対象外になった場合に、理由を構造化して監査ログへ
残すために使う。集中度超過(ポートフォリオ制約)とデータ異常(データ品質)を
同じ理由コードで混同しないことが目的。
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.domain.entities.enums import EligibilityBlockCategory


@dataclass(frozen=True)
class NotificationEligibility:
    eligible: bool
    block_category: EligibilityBlockCategory | None = None
    block_reason: str | None = None
