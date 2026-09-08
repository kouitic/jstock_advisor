"""保有銘柄「所有者」(owner)の正規化・検証・holding_id生成(承認済み設計)。

owner型はEnumではなく開放的なstr型とする(本人/子供に限らず将来の追加所有者
(長男/次男等)に対応するため)。holding_idは常にこのモジュールが決定的に
生成し、外部入力から直接受け取らない(owner×stock_codeの合成キー、
区切り文字"#"で連結する)。
"""

from __future__ import annotations

import hashlib
import unicodedata

_HOLDING_ID_DELIMITER = "#"
_MAX_OWNER_LENGTH = 20

# 既定owner(M2データ移行・CLI・CSVインポートの既定値として使用)。
# owner概念導入前の唯一の利用者を表す固定値であり、Enumではなく通常のowner
# 文字列の1つ(normalize_and_validate_owner()の検証対象)として扱う。
DEFAULT_OWNER = "本人"


_LOG_REF_PREFIX = "sha256:"
_LOG_REF_HASH_LEN = 8


def log_ref(value: str) -> str:
    """ログ・例外messageへ出すための短い符号を返す(Issue #135)。

    `sha256:` + SHA-256の先頭8文字。**元の値は出さない。**

    ## なぜ要るか

    `holding_id`は`<所有者>#<銘柄コード>`であり、ownerは実在人物を指す。
    これをloggerの書式引数や例外messageへ渡すと、その値はCloudWatch Logsを
    読めるprincipalへ露出する。実行時に書き出す先も「記録」であり、
    CLAUDE.mdの個人情報の範囲に含まれる。

    ## なぜ所在が失われないか

    運用者は候補の`holding_id`を手元で同じ形にハッシュして突き合わせられる
    (対象の保有件数は小さい)。「どの保有か」は符号で追える。

    ## 形式をIssue #63・#131と揃える理由

    #63 PR-2の`ItemIdDisclosure.HASH`、#131の公開面PII検出の
    ハッシュ接頭辞と**同一の形**である。本プロジェクトのハッシュ表記を
    1つに保ち、読む側が2つの規則を覚えずに済むようにする。

    ★ ただし**関数は共有しない**。#63側の実装は
    `infrastructure/record_failure_policy.py`(共通部品S-17 = 永続化ストア層)に
    あり、そこへ依存を作ると以後この用途の都合でS-17を触る動機が生まれる
    (S-17の変更はlock対象が全領域になる)。形式が同一であることは双方の
    テストで固定する。

    空文字列はハッシュせずそのまま返す(値が無いことを符号化しても
    情報が増えず、`sha256:e3b0c442`という定数がログに並ぶだけのため)。
    """
    if not value:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{_LOG_REF_PREFIX}{digest[:_LOG_REF_HASH_LEN]}"


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

    例外messageにはowner自体を含めず`log_ref()`の符号を出す(Issue #135)。
    例外は捕捉されずtracebackごとログへ出うるため、messageも露出面である。
    **例外の型は変えない**(捕捉している呼び出し元の挙動を変えないため)。
    """
    if not owner:
        raise InvalidOwnerError("ownerは空文字列にできません")
    if len(owner) > _MAX_OWNER_LENGTH:
        raise InvalidOwnerError(
            f"ownerは{_MAX_OWNER_LENGTH}文字以内で指定してください: "
            f"owner_ref={log_ref(owner)} length={len(owner)}"
        )
    if _HOLDING_ID_DELIMITER in owner:
        raise InvalidOwnerError(
            f"ownerに区切り文字'{_HOLDING_ID_DELIMITER}'を含めることはできません: "
            f"owner_ref={log_ref(owner)}"
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


def split_holding_id(holding_id: str) -> tuple[str, str] | None:
    """holding_idを(owner, stock_code)へ分解する。

    区切り文字("#")が1つも含まれない場合は、owner対応前の旧形式
    (stock_codeそのもの)とみなしNoneを返す。区切り文字がちょうど1つの場合は
    その形式で分解する。区切り文字が2つ以上含まれる場合は、多重prefix等の
    不正な形式(例: 移行の再実行時に誤って二重にownerが付与された)として
    InvalidOwnerErrorを送出する(fail-closed。データ破損を検知して migration
    を中止させるために使う)。
    """
    count = holding_id.count(_HOLDING_ID_DELIMITER)
    if count == 0:
        return None
    if count > 1:
        raise InvalidOwnerError(
            f"holding_idの形式が不正です(区切り文字'{_HOLDING_ID_DELIMITER}'が"
            f"複数含まれています): holding_ref={log_ref(holding_id)}"
        )
    owner_part, stock_code = holding_id.split(_HOLDING_ID_DELIMITER, 1)
    return owner_part, stock_code
