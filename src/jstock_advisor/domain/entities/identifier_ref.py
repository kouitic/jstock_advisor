"""新しい AuditLog の記録へ載せる owner の符号(Issue #925。#603 F2-0 + 段階 2 = kv1)。

## 位置づけ

AuditLog は追記のみで TTL が無く、一度書いた平文は永続する。新しい記録(#603 PR-F2 の
項目つきの記録など)は、**識別子を最初から載せない**ことを既定とし、owner を結び付ける
必要があるときだけ、この module の符号を使う。

## ★ kv0 の性質(『保護』ではない)★

kv0 は**鍵なし**の SHA-256 の先頭 24 桁である。これは『記録に平文の owner を載せない』を達成するが、
**『再識別を防ぐ』ものではない**: 攻撃は衝突を探すことではなく入力を当てることで、owner の候補は
ごく少数・既知のため、記録を読める者は候補を同じ形にハッシュして突き合わせられる(桁数は効かない)。
再識別を防ぐ符号は鍵つき(kv1 = HMAC-SHA256 + 鍵の版。#925 段階 2)。USER の決定(#925 …6142405238):
kv1 を採用し R1 の必須とする。実在の owner を kv0 で永続化することは禁止(`require_persistable`)。

## NEW_RECORD_IDENTIFIER_RULE(コードとして強制する。文書化は POLICY_AUTHORITY = HUMAN_ONLY)

1. 最初から識別子を載せない(件数・順番・銘柄コード・理由コードで足りるなら、それだけを載せる)
2. owner を結び付ける記録は鍵つきの符号 `KeyedOwnerRef` のみ(平文の owner を載せない)。
   保有 ID の一覧は載せない。保有の突き合わせが要る記録が現れたら、その PR で
   `holding_ref` を足す(今は使う記録が無い)
3. 符号の計算はこの module に集約する(呼び出し側は関数の名前だけに依存する)
4. 型で守る: 新しい記録の builder は `KeyedOwnerRef` だけを受け取る(平文の str・kv0 の `OwnerRef` を
   渡すと mypy が落ち、実行時にも拒否する)
5. 鍵が取れないときは★『記録しない』★。固定の符号・kv0 へ倒さない(`resolve_owner_ref`)

## 形式と版

kv0: `kv0:` + SHA-256(用途ラベル + 正規化した入力)の先頭 24 桁の 16 進(96 ビット)。
kv1: `kv1.{鍵の版}:` + HMAC-SHA256(鍵, b"owner:" + 正規化した owner)の先頭 24 桁の 16 進。鍵の版は
1〜999 の整数(先頭の 0 なし)。鍵を持つ処理だけが kv1 を作れる
(CLI・観測用 profile・開発者は持たない)。
reader は `parse_ref()` の版で読み分ける。

`log_ref`(ログ・例外メッセージ用の 8 桁。`owner.py`)は用途が違うため変更しない。
log_ref は空文字をそのまま返すが、本 module は空・空白だけの入力を**拒否**する
(空の符号が記録に残ると、全ての空の入力が同じ符号になり、区別できない記録になる)。

## ★ 鍵の扱い(kv1)★

* 鍵は `KeyMaterial` にだけ入れる。repr・str・format・pickle・copy・dataclass 変換・例外に出ない
* 鍵の取得は `KeyProvider`(Protocol。例外を送出しない契約)。取得できなければ `KeyUnavailable`
  (理由コードだけ。値・ARN・メッセージを持たない)
* ★ 鍵が取れないとき、符号を作らない。kv0 や固定の符号へ倒さない(`resolve_owner_ref`)。
  呼び出し側は『記録しない』を選ぶ(既存の BUY 判定には影響させない)★

## 例外

`IdentifierRefError`(メッセージに値を含めない。構築は `_fail` の 1 箇所)。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NewType, NoReturn, Protocol

from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, normalize_owner

OwnerRef = NewType("OwnerRef", str)
#: 鍵つきの符号(kv1)。新しい記録の書き手が受け取れるのはこれだけ
KeyedOwnerRef = NewType("KeyedOwnerRef", OwnerRef)

#: `owner_ref()`(鍵なし)の版。実在の owner の永続には使えない(`require_persistable`)
CURRENT_VERSION: Final = "kv0"
KEYED_VERSION: Final = "kv1"
#: 受理する版 = 実装済みの版だけ
KNOWN_VERSIONS: Final = frozenset({"kv0", "kv1"})
MIN_KEY_VERSION: Final = 1
MAX_KEY_VERSION: Final = 999
#: HMAC の鍵の長さの下限(バイト)。鍵を取得する殻(provider)はこれより厳しい検査をする
MIN_KEY_BYTES: Final = 32
_DIGEST_HEX_LENGTH: Final = 24
_OWNER_LABEL: Final = "owner"
_REF_PATTERN: Final = re.compile(
    rf"^(kv0|kv[1-9][0-9]*)(?:\.([1-9][0-9]{{0,2}}))?:([0-9a-f]{{{_DIGEST_HEX_LENGTH}}})$"
)


class IdentifierRefError(ValueError):
    """符号化できない入力・不正な符号。メッセージに値を含めない。"""


class KeyedReferenceRequiredError(IdentifierRefError):
    """実在の owner の記録には鍵つきの符号(kv1)が要る。kv0 では永続しない(USER 決定 #925)。"""


def _fail(problem: str) -> IdentifierRefError:
    return IdentifierRefError(f"identifier_ref: {problem}")


def _keyed_required() -> KeyedReferenceRequiredError:
    return KeyedReferenceRequiredError("identifier_ref: a keyed reference (kv1) is required")


def _digest(label: str, value: str) -> str:
    material = f"{label}:{value}".encode()
    return hashlib.sha256(material).hexdigest()[:_DIGEST_HEX_LENGTH]


def _normalized(owner: str) -> str:
    if not isinstance(owner, str):
        raise _fail("owner must be a str")
    normalized = normalize_owner(owner)
    if not normalized:
        raise _fail("owner must not be empty")
    return normalized


def owner_ref(owner: str) -> OwnerRef:
    """owner の符号(kv0。鍵なし)。owner.py の正規化(NFKC・空白)を通す(表記ゆれは同じ符号)。

    ★ 実在の owner の永続には使えない(`require_persistable`)。新しい記録の書き手は kv1 を使う。
    """
    return OwnerRef(f"{CURRENT_VERSION}:{_digest(_OWNER_LABEL, _normalized(owner))}")


def require_persistable(owner: str) -> None:
    """kv0 の符号で記録してよい owner か。DEFAULT_OWNER 以外は拒否する(例外に値を含めない)。

    USER の決定(#925 …6142405238): 実在の owner を kv0(鍵なし)で永続化することを禁止する。
    現在の Shadow の枠の記録の配線は DEFAULT_OWNER だけを渡す。実在の owner を記録する変更
    (PR-F2-2 の配線)は kv1(`resolve_owner_ref`)を経由する。
    """
    if not isinstance(owner, str) or normalize_owner(owner) != normalize_owner(DEFAULT_OWNER):
        raise _keyed_required()


# --- kv1: 鍵つきの符号 --------------------------------------------------------------------------


class KeyUnavailableReason(StrEnum):
    """鍵が取れなかった理由(値を持たない語彙。ログ・記録・メトリクスの理由コード)。"""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    MALFORMED = "MALFORMED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class KeyUnavailable:
    """鍵が取れなかった。理由コードだけを持つ(値・ARN・メッセージを持たない)。"""

    reason: KeyUnavailableReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, KeyUnavailableReason):
            raise _fail("a key-unavailable reason must be a KeyUnavailableReason")


def _refuse(what: str) -> NoReturn:
    raise TypeError(f"KeyMaterial cannot be {what}")


class KeyMaterial:
    """HMAC の鍵 + 鍵の版。★ 鍵の bytes は repr・str・pickle・copy・dataclass 変換・例外に出ない ★

    鍵を取り出す accessor は無い(`keyed_owner_ref` だけが `_hmac` を使う)。

    ★ 保護の範囲(USER の SHOULD。#925 …6159386050)★: 通常の表示・直列化・複製・例外の経路から
    鍵が出ないことまで。**内部属性への直接アクセス**(`object.__getattribute__`・`_hmac` の呼び出し・
    デバッガ・gc の走査)までは防止できない(同じプロセスで動くコードは鍵を読める)。鍵の本当の
    境界は『鍵を持つのは Lambda の 2 role だけ(CLI・観測用 profile・開発者は持たない)』であり、
    この型は誤って鍵を出力・記録・ログへ出す事故を防ぐ層である。
    """

    __slots__ = ("_key", "_key_version")
    _key: bytes
    _key_version: int

    def __init__(self, *, key_version: int, key: bytes) -> None:
        if isinstance(key_version, bool) or not isinstance(key_version, int):
            raise _fail("key_version must be an int")
        if not MIN_KEY_VERSION <= key_version <= MAX_KEY_VERSION:
            raise _fail("key_version is out of range")
        if not isinstance(key, bytes):
            raise _fail("key must be bytes")
        if len(key) < MIN_KEY_BYTES:
            raise _fail("key is too short")
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_key_version", key_version)

    @property
    def key_version(self) -> int:
        return self._key_version

    def _hmac(self, message: bytes) -> str:
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("KeyMaterial is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("KeyMaterial is immutable")

    def __repr__(self) -> str:
        return f"KeyMaterial(key_version={self._key_version})"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, KeyMaterial):
            return NotImplemented
        return self._key_version == other._key_version and hmac.compare_digest(
            self._key, other._key
        )

    def __hash__(self) -> int:
        return hash(("KeyMaterial", self._key_version))

    def __reduce__(self) -> NoReturn:
        _refuse("pickled")

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        _refuse("pickled or copied")

    def __copy__(self) -> NoReturn:
        _refuse("copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        _refuse("copied")

    def __getstate__(self) -> NoReturn:
        _refuse("serialized")


class KeyProvider(Protocol):
    """鍵の取得元。★ 例外を送出しない契約(取れなければ KeyUnavailable を返す)★。殻が実装する。"""

    def get(self) -> KeyMaterial | KeyUnavailable: ...


def keyed_owner_ref(owner: str, key: KeyMaterial) -> KeyedOwnerRef:
    """owner の鍵つきの符号(kv1)。HMAC-SHA256(鍵, b"owner:" + 正規化した owner)の先頭 24 桁。"""
    if not isinstance(key, KeyMaterial):
        raise _fail("key must be a KeyMaterial")
    message = f"{_OWNER_LABEL}:{_normalized(owner)}".encode()
    digest = key._hmac(message)[:_DIGEST_HEX_LENGTH]
    return KeyedOwnerRef(OwnerRef(f"{KEYED_VERSION}.{key.key_version}:{digest}"))


def resolve_owner_ref(owner: str, provider: KeyProvider) -> KeyedOwnerRef | KeyUnavailable:
    """provider から鍵を取り、kv1 の符号を返す。★ 鍵が取れなければ KeyUnavailable(符号を作らない)★。

    provider が例外を出す・想定外の型を返す場合も KeyUnavailable(ERROR)に畳む(例外を伝播させない。
    BUY 判定に影響させない)。kv0・固定の符号へは決して倒さない。
    owner が不正(空・非 str)なのは呼び出し側の誤りで、IdentifierRefError(値なし)を送出する。
    """
    _normalized(owner)  # 先に検査(不正な owner を鍵の取得の失敗と取り違えない)
    try:
        result = provider.get()
    except Exception:  # noqa: BLE001 - 契約違反の provider も Shadow を止めない(BUY を守る)
        return KeyUnavailable(KeyUnavailableReason.ERROR)
    if isinstance(result, KeyUnavailable):
        return result
    if isinstance(result, KeyMaterial):
        return keyed_owner_ref(owner, result)
    return KeyUnavailable(KeyUnavailableReason.ERROR)


def require_keyed(ref: str) -> KeyedOwnerRef:
    """書込側の入口: kv1 の符号であることを確かめる。kv0・壊れた形式は拒否(値を含めない)。"""
    try:
        version, _digest_hex = parse_ref(ref)
    except IdentifierRefError:
        raise _keyed_required() from None
    if version != KEYED_VERSION:
        raise _keyed_required()
    return KeyedOwnerRef(OwnerRef(ref))


def parse_ref(ref: str) -> tuple[str, str]:
    """符号を (版, 桁) に分ける。未知の版・壊れた形式は拒否する(reader が版で分岐する)。

    版は `kv0` または `kv1`(kv1 の鍵の版は `parse_keyed_ref` で取る)。
    """
    match = _REF_PATTERN.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        raise _fail("not a reference")
    version, key_version, digest = match.group(1), match.group(2), match.group(3)
    if version not in KNOWN_VERSIONS:
        raise _fail("unknown reference version")
    if version == KEYED_VERSION:
        if key_version is None or not MIN_KEY_VERSION <= int(key_version) <= MAX_KEY_VERSION:
            raise _fail("not a reference")
    elif key_version is not None:  # kv0 は鍵の版を持たない
        raise _fail("not a reference")
    return version, digest


def parse_keyed_ref(ref: str) -> tuple[int, str]:
    """kv1 の符号を (鍵の版, 桁) に分ける。kv0 は拒否する。"""
    version, digest = parse_ref(ref)
    if version != KEYED_VERSION:
        raise _keyed_required()
    match = _REF_PATTERN.fullmatch(ref)
    assert match is not None and match.group(2) is not None  # parse_ref が検査済み
    return int(match.group(2)), digest
