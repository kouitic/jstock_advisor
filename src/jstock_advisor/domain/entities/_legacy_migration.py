"""既存保存データ(旧フィールド名)を新フィールド名へ読み替えるための共通ヘルパー。

pydanticモデルはextra="forbid"のため、フィールド名を変更すると旧JSONの
デシリアライズが失敗する。@model_validator(mode="before")と組み合わせて、
旧キーを新キーへリネーム(popして詰め替え)することで、保存済みの
Recommendation/AuditLog等を無変更で読み続けられるようにする。
"""

from __future__ import annotations

from typing import Any


def remap_legacy_fields(data: Any, mapping: dict[str, str]) -> Any:
    """dataが辞書の場合のみ、mapping(旧キー→新キー)に従ってキーをリネームする。

    新キーが既に存在する場合(新形式で保存されたデータ)は上書きしない。
    """
    if not isinstance(data, dict):
        return data
    remapped = dict(data)
    for old_key, new_key in mapping.items():
        if old_key in remapped and new_key not in remapped:
            remapped[new_key] = remapped.pop(old_key)
        elif old_key in remapped:
            remapped.pop(old_key)
    return remapped
