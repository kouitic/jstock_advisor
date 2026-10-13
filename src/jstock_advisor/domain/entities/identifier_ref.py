"""新しい AuditLog の記録へ載せる owner の符号(Issue #925 段階 0。#603 F2-0)。

## 位置づけ

AuditLog は追記のみで TTL が無く、一度書いた平文は永続する。新しい記録(#603 PR-F2 の
項目つきの記録など)は、**識別子を最初から載せない**ことを既定とし、owner を結び付ける
必要があるときだけ、この module の符号(`owner_ref`)を使う。

## ★ kv0 の性質(『保護』ではない)★

kv0 は**鍵なし**の SHA-256 の先頭 24 桁である。これは『記録に平文の owner を載せない』を達成するが、
**『再識別を防ぐ』ものではない**: 攻撃は衝突を探すことではなく入力を当てることで、owner の候補は
ごく少数・既知のため、記録を読める者は候補を同じ形にハッシュして突き合わせられる(桁数は効かない)。
再識別を防ぐ符号は鍵つき(kv1 = HMAC + 鍵の版。#925 段階 2。鍵の管理・IAM は USER の承認が要る)。
kv0 の記録を R1 で永続してよいか、kv1 を前倒しするかは USER の判断事項である(#925 の判断資料)。

## NEW_RECORD_IDENTIFIER_RULE(コードとして強制する。文書化は POLICY_AUTHORITY = HUMAN_ONLY)

1. 最初から識別子を載せない(件数・順番・銘柄コード・理由コードで足りるなら、それだけを載せる)
2. owner を結び付ける記録は `OwnerRef` のみ(平文の owner を載せない)。保有 ID の一覧は載せない。
   保有の突き合わせが要る記録が現れたら、その PR で `holding_ref` を足す(今は使う記録が無い)
3. 符号化はこの module の `owner_ref()` に集約する(呼び出し側は関数の名前だけに依存し、
   符号の方式を差し替えても書き手を直さない)
4. 型で守る: 新しい記録の builder は `OwnerRef` だけを受け取る(平文の str を渡すと mypy が落ちる)
5. 鍵が取れない(将来の鍵つき符号 kv1 以降)ときは、★『記録しない』★。固定の符号へ倒さない

## 形式と版

`kv0:` + SHA-256(用途ラベル + 正規化した入力)の先頭 24 桁の 16 進(96 ビット)。鍵つき符号
(kv1)は段階 2 で、**新しい記録から** kv1 を書く。reader(CLI・#934)は `parse_ref()` の版で
読み分ける。受理する版は**実装済みの版だけ**(kv1 を作る側が無いうちは kv1 を受理しない。
段階 2 の PR で足す)。

`log_ref`(ログ・例外メッセージ用の 8 桁。`owner.py`)は用途が違うため変更しない。
log_ref は空文字をそのまま返すが、本 module は空・空白だけの入力を**拒否**する
(空の符号が記録に残ると、全ての空の入力が同じ符号になり、区別できない記録になる)。

## 既存の記録の扱いの根拠

audit_id の接頭辞が `sha256:`(log_ref)から `kv0:` に変わる(別の key になる)。購入側 Shadow の
出荷設定は OFF(`config/allocation_shadow.yaml`。Production の deploy SHA 03c2a022 でも OFF)で、
記録が書かれる経路が無い(table の中身は AWS の読取が要るため未確認)。local の既存の記録は
開発用で、捨てて構わない。

## 例外

`IdentifierRefError`(メッセージに値を含めない。構築は `_fail` の 1 箇所)。
"""

from __future__ import annotations

import hashlib
import re
from typing import Final, NewType

from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, normalize_owner

OwnerRef = NewType("OwnerRef", str)

#: 現行の版(鍵なし)。鍵つき(kv1)は #925 段階 2 で足す
CURRENT_VERSION: Final = "kv0"
#: 受理する版 = 実装済みの版だけ(kv1 は段階 2 の PR で足す)
KNOWN_VERSIONS: Final = frozenset({"kv0"})
_DIGEST_HEX_LENGTH: Final = 24
_OWNER_LABEL: Final = "owner"
_REF_PATTERN: Final = re.compile(rf"^(kv[0-9]+):([0-9a-f]{{{_DIGEST_HEX_LENGTH}}})$")


class IdentifierRefError(ValueError):
    """符号化できない入力・不正な符号。メッセージに値を含めない。"""


class KeyedReferenceRequiredError(IdentifierRefError):
    """実在の owner の記録には鍵つきの符号(kv1)が要る。kv0 では永続しない(USER 決定 #925 …6142405238)。"""


def _fail(problem: str) -> IdentifierRefError:
    return IdentifierRefError(f"identifier_ref: {problem}")


def _digest(label: str, value: str) -> str:
    material = f"{label}:{value}".encode()
    return hashlib.sha256(material).hexdigest()[:_DIGEST_HEX_LENGTH]


def owner_ref(owner: str) -> OwnerRef:
    """owner の符号。owner.py の正規化(NFKC・空白)を通すので、表記ゆれが別の符号にならない。"""
    if not isinstance(owner, str):
        raise _fail("owner must be a str")
    normalized = normalize_owner(owner)
    if not normalized:
        raise _fail("owner must not be empty")
    return OwnerRef(f"{CURRENT_VERSION}:{_digest(_OWNER_LABEL, normalized)}")


def require_persistable(owner: str) -> None:
    """kv0 の符号で記録してよい owner か。DEFAULT_OWNER 以外は拒否する(例外に値を含めない)。

    USER の決定(#925 …6142405238): 実在の owner を kv0(鍵なし)で永続化することを禁止する。
    現在の Shadow の配線は DEFAULT_OWNER だけを渡す。実在の owner を渡す変更(PR-F2-2 の配線)は、
    鍵つきの符号(kv1)の実装の後で、その時にこの関数の判定を kv1 のときの許可へ替える。
    """
    if not isinstance(owner, str) or normalize_owner(owner) != normalize_owner(DEFAULT_OWNER):
        raise KeyedReferenceRequiredError("identifier_ref: a keyed reference (kv1) is required")


def parse_ref(ref: str) -> tuple[str, str]:
    """符号を (版, 桁) に分ける。未知の版・壊れた形式は拒否する(reader が版で分岐する)。"""
    match = _REF_PATTERN.match(ref) if isinstance(ref, str) else None
    if match is None:
        raise _fail("not a reference")
    version, digest = match.group(1), match.group(2)
    if version not in KNOWN_VERSIONS:
        raise _fail("unknown reference version")
    return version, digest
