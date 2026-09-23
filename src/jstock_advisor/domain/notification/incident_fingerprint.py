"""Issue #502(#132 X-3): incident の fingerprint 計算と dedup の判定。

**純粋な関数と型だけ**である。ネットワーク・ファイル・AWS・永続化に触れない(fingerprint を
どこかへ保存し、次回呼び出し時に前回値を引く経路の接続は、別 Issue〔X-3b〕の責務)。

fingerprint の候補(#132 本文 §8): `environment + job_name + failure_stage + failure_type +
normalized_error_signature`。**異なる root cause を誤って同一 incident にまとめないこと**を
最優先する(逆に、同一 root cause を別の incident として扱う〔= 通知が増える〕方向の誤りは、
本番の異常を見落とすよりは安全側)。

``job_name`` は `domain/notification/incident_message.py` の `IncidentJob`(本文へ出す
**利用者向けの集約名**。例: dispatcher/worker/terminal_failure/reconciler の 4 関数がいずれも
`WATCHLIST_SCREENING` へ集約される)ではなく、**呼び出し元が渡す内部の job 識別子をそのまま
使う**(例: `watchlist-dispatcher` と `watchlist-worker` は別の fingerprint になる)。fingerprint は
「同一障害の重複通知を防ぐ」ための識別であり、本文表示用の集約とは目的が違う。集約すると、
別の Lambda で同時に起きた別の障害が同一 incident に丸められてしまう(このファイルの docstring
で最優先とした「誤って同一にまとめない」に反する)。

**識別子・銘柄・所有者・例外メッセージの生の値は、fingerprint 自体にも `normalize_error_signature()`
の出力にも残さない**(H-30 と整合。#132 §14 の `job_name` は運用上の Lambda 関数名であり、
個人・銘柄の識別子ではないため、fingerprint の入力に含めてよい)。fingerprint は SHA-256 の
hex 文字列で返す(`NotificationClaim.claim_id` と同じ「identity 文字列を SHA-256 で 1 意の
短い値にする」パターンを踏襲。#502 の PR 本文に、`NotificationClaim` 自体の再利用可否の検討を
記録する)。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass

from jstock_advisor.domain.jst import require_timezone_aware

# 正規化で吸収する「揺れ」。左から順に適用する(適用順序が結果に影響するため固定する)。
#   1. UUID 形式の識別子
#   2. ISO8601 風の日時(タイムゾーン付き・無しの両方。区切りの T の有無は問わない)
#   3. 16進数の並び(8桁以上。request-id・hash 等)
#   4. 残る数字の並び(件数・行番号・ポート番号等)
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ISO_DATETIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_HEX_RUN_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_DIGIT_RUN_RE = re.compile(r"\d+")

# 正規化後の長さの上限(#132 §8 の「identifier / timestamp / 件数の揺れを吸収する」目的に対して
# 十分な長さ。無限に伸びる stack trace 全文をそのまま fingerprint の入力へ含めないための保険。
# 切り詰めても、先頭に含まれる例外の種類・主要メッセージで通常は十分に区別できる)。
_MAX_NORMALIZED_LENGTH = 500


def normalize_error_signature(error_type: str, error_message: str) -> str:
    """例外の種類とメッセージから、識別子・タイムスタンプ・件数等の揺れを吸収した署名を作る。

    **同じ根本原因の別発生(異なる ID・時刻・件数を持つだけ)を同じ値にし、異なる例外の種類は
    区別する**(`error_type` を先頭に必ず含めるため、正規化後のメッセージが偶然一致しても
    例外の種類が違えば別の値になる)。`error_message` に識別子・銘柄・所有者・stack trace の
    生の値が入っていても、この関数を通した時点でそれらは除去される(数字・UUID・16進数の並びを
    プレースホルダへ置換するため)。
    """
    text = error_message
    text = _UUID_RE.sub("<ID>", text)
    text = _ISO_DATETIME_RE.sub("<TS>", text)
    text = _HEX_RUN_RE.sub("<HEX>", text)
    text = _DIGIT_RUN_RE.sub("<N>", text)
    text = " ".join(text.split())  # 空白の揺れ(改行・連続空白)も吸収する
    signature = f"{error_type}: {text}"
    return signature[:_MAX_NORMALIZED_LENGTH]


@dataclass(frozen=True)
class IncidentFingerprintInput:
    """fingerprint を計算する 5 要素(#132 §8)。

    すべて呼び出し元が確定させた文字列を渡す(この module 側では既定値・推測を行わない)。
    `job_name` は内部の job 識別子(#132 §14 の `job_name` の粒度。`IncidentJob` の集約名では
    ない)。`error_type` / `error_message` から `normalized_error_signature` を導くのは
    `compute_fingerprint()` の責務であり、呼び出し元が正規化済みの値を渡す必要はない。
    """

    environment: str
    job_name: str
    failure_stage: str
    failure_type: str
    error_type: str
    error_message: str

    def __post_init__(self) -> None:
        for field_name in (
            "environment",
            "job_name",
            "failure_stage",
            "failure_type",
            "error_type",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty str, got {value!r}")
        if not isinstance(self.error_message, str):
            raise ValueError(f"error_message must be a str, got {self.error_message!r}")


def compute_fingerprint(signal: IncidentFingerprintInput) -> str:
    """5 要素から、決定的な fingerprint(SHA-256 hex、64 桁)を計算する。

    同じ入力からは常に同じ値、異なる `failure_type` / `failure_stage` / `job_name` /
    `environment` / 例外の種類・正規化後メッセージのいずれかが違えば異なる値になる。
    要素は**名前付き**で区切り文字とともに連結するため(`key=value` を `|` で結合)、
    ある要素の末尾と次の要素の先頭がたまたま連結して同じ文字列になる、という衝突を避ける。
    フィールドの並び順は `IncidentFingerprintInput` の定義順に固定されており(dataclass の
    フィールド順は inputs の与え方に依存しない)、呼び出し側がキーワード引数をどの順で
    渡しても同じ `IncidentFingerprintInput` になり、同じ fingerprint になる。
    """
    normalized = normalize_error_signature(signal.error_type, signal.error_message)
    identity = "|".join(
        [
            f"environment={signal.environment}",
            f"job_name={signal.job_name}",
            f"failure_stage={signal.failure_stage}",
            f"failure_type={signal.failure_type}",
            f"normalized_error_signature={normalized}",
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def is_duplicate_within_window(
    *,
    now: dt.datetime,
    last_notified_at: dt.datetime | None,
    window: dt.timedelta,
) -> bool:
    """同一 fingerprint について、直近の通知から `window` 以内かどうかを判定する。

    `last_notified_at` が `None`(その fingerprint の通知履歴が無い)なら常に重複ではない
    (`False`)。**境界(ちょうど `window` が経過した瞬間)は「重複ではない」側**とする
    (`now - last_notified_at >= window` を非重複とする。`>=` であり `>` ではない)。
    理由: window は「この時間内は再通知しない」という**下限**の意味であり、ちょうど window
    が経過した時点は「もう window を満たした」とみなすのが自然(window 境界を再通知が
    起きない側へ倒すと、システム時計のわずかな遅れで意図せず抑止が延びる方向の誤りを避けられる。
    #132 §8 は「retry で 3 通にならない」ことが目的であり、境界を広く取りすぎて本来必要な
    再通知まで抑止しないことを優先する)。

    `now` は呼び出し元が渡す(内部で取得しない。2.5 節・既存の週次レビュー等と同じ規約)。
    """
    require_timezone_aware(now)
    if last_notified_at is None:
        return False
    require_timezone_aware(last_notified_at)
    if window < dt.timedelta(0):
        raise ValueError(f"window must not be negative, got {window!r}")
    elapsed = now - last_notified_at
    if elapsed < dt.timedelta(0):
        # last_notified_at が未来(呼び出し元の入力誤り)。安全側(=重複扱いにして誤通知を防ぐ)。
        return True
    return elapsed < window
