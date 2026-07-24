"""エンティティ共通のpydantic基底クラス。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Entity(BaseModel):
    """通常のミュータブルなドメインエンティティ。未知フィールドはエラーとする。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ImmutableSnapshot(BaseModel):
    """一度作成したら変更不可なスナップショット(推奨等)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
