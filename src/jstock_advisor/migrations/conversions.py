"""Legacy(V1)形状から新形状(V2)への変換関数群。

移行前の全データは、owner概念が存在しなかった時点のものであり、定義上
すべて「本人」の保有だったことが確定している(承認済み設計。owner
概念自体が無かった以上、他の所有者があり得なかったため、誤帰属のリスクは
無い)。holding_idは外部から受け取らず、常にnormalize_and_validate_owner()
とbuild_holding_id()から決定的に導出する。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.owner import build_holding_id, normalize_and_validate_owner
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.migrations.legacy_shapes import (
    LegacyHoldingsSnapshotEntryV1,
    LegacyHoldingV1,
    LegacyPurchaseLotV1,
)
from jstock_advisor.migrations.v2_entities import (
    HoldingsSnapshotEntryV2,
    HoldingV2,
    PurchaseLotV2,
)

DEFAULT_MIGRATION_OWNER = "本人"

# RecommendationType分類はholdings_owner_preflight.pyに正本を置く
# (preflightの交差検証と完全に同じ分類基準を、migration本体でも使い回す
# 必要があるため。ここでの遅延importは循環import回避のため)。


def recommendation_scope_for_migration(
    rec: Recommendation, owner: str = DEFAULT_MIGRATION_OWNER
) -> tuple[str | None, str | None]:
    """バックフィル予定の(owner, holding_id)を、Recommendation自体がまだ
    書き込まれているか(dry-runか否か)に関わらず、常に元のRecommendation
    オブジェクトから直接計算する(読み直しに依存しない設計。dry-run時も
    実行時も同一の計算結果になることを保証する)。

    保有系RecommendationTypeのみバックフィル対象、BUY系はNoneのまま
    (owner=None, holding_id=None)を返す。
    """
    from jstock_advisor.migrations.holdings_owner_preflight import (
        HOLDING_FAMILY_RECOMMENDATION_TYPES,
    )

    if rec.recommendation_type not in HOLDING_FAMILY_RECOMMENDATION_TYPES:
        return None, None
    normalized_owner = normalize_and_validate_owner(owner)
    return normalized_owner, build_holding_id(normalized_owner, rec.stock_code)


def convert_holding(legacy: LegacyHoldingV1, owner: str = DEFAULT_MIGRATION_OWNER) -> HoldingV2:
    normalized_owner = normalize_and_validate_owner(owner)
    holding_id = build_holding_id(normalized_owner, legacy.stock_code)
    return HoldingV2(holding_id=holding_id, owner=normalized_owner, **legacy.model_dump())


def convert_purchase_lot(
    legacy: LegacyPurchaseLotV1, owner: str = DEFAULT_MIGRATION_OWNER
) -> PurchaseLotV2:
    normalized_owner = normalize_and_validate_owner(owner)
    holding_id = build_holding_id(normalized_owner, legacy.stock_code)
    return PurchaseLotV2(holding_id=holding_id, owner=normalized_owner, **legacy.model_dump())


def convert_holdings_snapshot_entry(
    legacy: LegacyHoldingsSnapshotEntryV1, owner: str = DEFAULT_MIGRATION_OWNER
) -> HoldingsSnapshotEntryV2:
    normalized_owner = normalize_and_validate_owner(owner)
    holding_id = build_holding_id(normalized_owner, legacy.stock_code)
    return HoldingsSnapshotEntryV2(
        holding_id=holding_id, owner=normalized_owner, **legacy.model_dump()
    )
