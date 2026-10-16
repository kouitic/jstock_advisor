"""事前登録の検証(Issue #880 R1a の a-7)。『使った値 = 登録した値』を機械で確かめる。

純粋関数。標準ライブラリ(`hashlib`・`json`)だけ。事前登録の様式(#880 …6094069279)の
『機械可読の本体』を正規化して SHA-256 を取り、報告書が引用する登録 ID・ハッシュ・使った値が、
登録の本体と一致するかを検証する(報告書の自己申告に頼らない)。

## 正規化(様式の 8 節と同じ)

キーを昇順・UTF-8・区切りの空白なし(`separators=(",", ":")`)。
`CONTENT_HASH = SHA-256(正規化した本体)`。
本体に使える型は JSON の型(辞書・列・文字列・整数・真偽値・None)だけ(浮動小数・Decimal・集合・
NaN は契約違反 = MALFORMED。値の表現のずれを持ち込まない)。

## 検証の結果(固定の語彙)

  MATCH          ハッシュが一致し、使った値が登録の値と完全に一致する
  HASH_MISMATCH  報告書が引用するハッシュが、登録の本体のハッシュと一致しない(報告書は無効)
  ID_MISMATCH    報告書が引用する登録 ID が、登録の本体の登録 ID と一致しない
  VALUES_DIFFER  使った値が、登録の値と違う(欠落・余り・値の違いを別に報告する)
  MALFORMED      本体・引用の形が契約に反する(値を作らない)
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jstock_advisor.domain.backtest.errors import ReplayInputError

_JSON_SCALARS = (str, int, bool, type(None))


def _check_json(value: object) -> None:
    if isinstance(value, _JSON_SCALARS):
        return
    if isinstance(value, list | tuple):
        for item in value:
            _check_json(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReplayInputError("keys must be strings")
            _check_json(item)
        return
    raise ReplayInputError("value type is not allowed in a registration body")


def canonical_json(body: Mapping[str, Any]) -> str:
    """キー昇順・UTF-8・空白なしの正規化。JSON にできない型は契約違反。"""
    if not isinstance(body, Mapping):
        raise ReplayInputError("body must be a mapping")
    _check_json(body)
    return json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def content_hash(body: Mapping[str, Any]) -> str:
    """正規化した本体の SHA-256(16 進)。"""
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


class RegistrationCheck(StrEnum):
    MATCH = "MATCH"
    HASH_MISMATCH = "HASH_MISMATCH"
    ID_MISMATCH = "ID_MISMATCH"
    VALUES_DIFFER = "VALUES_DIFFER"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True)
class ValuesComparison:
    """使った値と登録の値の差(名前の昇順)。ok は 3 つとも空のとき。"""

    missing: tuple[str, ...]  # 登録されているのに使われていない
    unexpected: tuple[str, ...]  # 登録に無いのに使われた
    differing: tuple[str, ...]  # 両方にあるが値が違う

    @property
    def ok(self) -> bool:
        return not (self.missing or self.unexpected or self.differing)


def registered_values(body: Mapping[str, Any]) -> dict[str, Any]:
    """本体の `parameters`(名前 → 値)を取り出す。形が違えば契約違反。"""
    parameters = body.get("parameters")
    if not isinstance(parameters, list | tuple):
        raise ReplayInputError("parameters must be a list")
    values: dict[str, Any] = {}
    for entry in parameters:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
            raise ReplayInputError("each parameter needs a name")
        name = entry["name"]
        if name in values or "value" not in entry:
            raise ReplayInputError("parameter names must be unique and carry a value")
        values[name] = entry["value"]
    return values


def compare_used_values(body: Mapping[str, Any], used: Mapping[str, Any]) -> ValuesComparison:
    """使った値(名前 → 値)を、登録の本体の値と突き合わせる。"""
    registered = registered_values(body)
    _check_json(dict(used))
    return ValuesComparison(
        missing=tuple(sorted(set(registered) - set(used))),
        unexpected=tuple(sorted(set(used) - set(registered))),
        differing=tuple(sorted(n for n in set(registered) & set(used) if registered[n] != used[n])),
    )


def check_report(
    body: Mapping[str, Any],
    *,
    cited_registration_id: str,
    cited_hash: str,
    used: Mapping[str, Any],
) -> RegistrationCheck:
    """報告書が引用する登録 ID・ハッシュ・使った値を、登録の本体と照合する(上の固定の語彙で返す)。

    判定の順序は固定: 形 → ハッシュ → 登録 ID → 使った値。最初に当たったもので返る。
    """
    try:
        actual_hash = content_hash(body)
        registration_id = body.get("registration_id")
        comparison = compare_used_values(body, used)
    except ReplayInputError:
        return RegistrationCheck.MALFORMED
    if not isinstance(registration_id, str) or not registration_id:
        return RegistrationCheck.MALFORMED
    if not isinstance(cited_hash, str) or cited_hash != actual_hash:
        return RegistrationCheck.HASH_MISMATCH
    if cited_registration_id != registration_id:
        return RegistrationCheck.ID_MISMATCH
    if not comparison.ok:
        return RegistrationCheck.VALUES_DIFFER
    return RegistrationCheck.MATCH
