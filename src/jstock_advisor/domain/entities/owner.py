"""保有銘柄「所有者」(owner)の正規化・検証・holding_id生成(承認済み設計)。

owner型はEnumではなく開放的なstr型とする(本人/子供に限らず将来の追加所有者
(長男/次男等)に対応するため)。holding_idは常にこのモジュールが決定的に
生成し、外部入力から直接受け取らない(owner×stock_codeの合成キー、
区切り文字"#"で連結する)。
"""

from __future__ import annotations

import unicodedata

_HOLDING_ID_DELIMITER = "#"
_MAX_OWNER_LENGTH = 20


class InvalidOwnerError(ValueError):
    """owner正規化・検証に失敗した(空文字列・最大長超過・禁止文字を含む等)。"""


def normalize_owner(raw: str) -> str:
    """Unicode NFKC正規化→前後空白除去→内部の連続空白を1つへ圧縮する。

    全角/半角の入力揺れ(同じ「本人」という意図の文字列が別のowner文字列と
    して扱われてしまう事態)を防ぐための正規化。バリデーションは行わない
    (validate_owner()を別途呼ぶこと)。
    """
    normalized = unicodedata.normalize("NFKC", raw).strip()
    return " ".join(normalized.split())


def validate_owner(owner: str) -> None:
    """正規化済みownerの妥当性を検証する。不正な場合はInvalidOwnerErrorを送出する。

    - 空文字列(正規化後に空白のみだった場合を含む)は拒否する
    - 最大長(20文字)を超える値は拒否する
    - holding_idの区切り文字("#")を含む値は拒否する(holding_idの分解が
      一意にできなくなるため)
    """
    if not owner:
        raise InvalidOwnerError("ownerは空文字列にできません")
    if len(owner) > _MAX_OWNER_LENGTH:
        raise InvalidOwnerError(f"ownerは{_MAX_OWNER_LENGTH}文字以内で指定してください: {owner!r}")
    if _HOLDING_ID_DELIMITER in owner:
        raise InvalidOwnerError(
            f"ownerに区切り文字'{_HOLDING_ID_DELIMITER}'を含めることはできません: {owner!r}"
        )


def normalize_and_validate_owner(raw: str) -> str:
    """normalize_owner()とvalidate_owner()を続けて行う便宜関数。"""
    owner = normalize_owner(raw)
    validate_owner(owner)
    return owner


def build_holding_id(owner: str, stock_code: str) -> str:
    """owner×stock_codeから決定的にholding_idを生成する(例: "本人#8306")。

    呼び出し側はあらかじめnormalize_and_validate_owner()で正規化・検証済みの
    ownerを渡すこと(このモジュールはstock_codeの形式検証は行わない)。
    """
    return f"{owner}{_HOLDING_ID_DELIMITER}{stock_code}"
