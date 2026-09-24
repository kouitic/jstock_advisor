"""Issue #501(#132 X-2): 本番ジョブの異常を LINE へ知らせる「異常 1 通」の本文を組み立てる。

**純粋な関数と型だけ**である。ネットワーク・ファイル・AWS・永続化に触れない(送信・接続は
#503 = X-4 の責務)。既存の通知の判定・文面・送信経路を変更しない(新規 module の追加のみ)。

本文は **allowlist(#132 H-30)を不変条件**とする。出してよいのは、job の利用者向けの名称・
時刻・件数・日数・真偽値だけである。次は本文に現れない(現れない**構造**にしてある)。

    識別子 / 銘柄 / 所有者 / stack trace / exception message / ARN / account ID / request ID /
    DynamoDB key / 内部パス / secret / 個人情報 / 保有株数 / 取得価格 / 内部の関数名・job 名

どう締めているか:

    ・自由な文字列を受け取らない。`IncidentNotice` は、`IncidentJob`(列挙。値は利用者向けの名称)・
      時刻・件数・日数・真偽値だけを、型と値の検査つきで受け取る(`bool` を件数として通さない等)。
    ・内部の関数名・job 名は `resolve_incident_job()` で列挙へ引くためだけに使い、**返り値にも
      本文にも残らない**。対応表に無い名前は、汎用の名称(`IncidentJob.OTHER`)へ落ちる。
    ・本文は固定の文型(`_HEADLINE` + 対象 + 発生時刻 + 任意の件数・日数・継続中)だけで組み立てる。

時刻は JST の「時:分」で表示する(内部の日時は UTC のまま渡す。`domain/jst.py`)。現在時刻は
内部で取得しない(呼び出し側が `occurred_at` を渡す)。

例:

    ⚠️ 本番処理でエラーが発生しました。システム側で調査情報を記録しました。
    対象: 買い候補チェック
    発生時刻: 08:03
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from jstock_advisor.domain.jst import require_timezone_aware, to_jst

_HEADLINE = "⚠️ 本番処理でエラーが発生しました。システム側で調査情報を記録しました。"

# Lambda の関数名は、スタック名が前置される(`jstock-advisor-buy-candidates` 等)。
_STACK_PREFIX = "jstock-advisor-"


class IncidentJob(StrEnum):
    """異常の対象となる job の**利用者向けの名称**(値がそのまま本文に出る)。"""

    BUY_CANDIDATES = "買い候補チェック"
    HOLDINGS_WATCHLIST = "保有株チェック"
    DISCLOSURE_CHECK = "開示チェック"
    EVALUATION = "過去の推奨の評価"
    WATCHLIST_SCREENING = "ウォッチリスト自動追加"
    WEEKLY_REVIEW = "週次レビュー"
    MONTHLY_REVIEW = "月次レビュー"
    QUARTERLY_REVIEW = "四半期レビュー"
    LINE_WEBHOOK = "LINE の応答"
    # Issue #503: IncidentNotifierFunction自身(異常通知の中継Lambda)。自身のErrors Alarmは
    # 自己再帰を避けるため同一Topicへは接続しない(本段階では実際にはこの経路を通らない)が、
    # 対応表の網羅性テスト(全Lambda関数を列挙する)のために明示のIncidentJobを持つ。
    INCIDENT_NOTIFIER = "異常通知の中継処理"
    # Issue #349: AsyncInvokeFailureDLQは、BuyCandidatesFunction/HoldingsWatchlistFunctionの
    # 両方が非同期呼び出し失敗時の送信先として共有する(#318)。DLQへ入ったメッセージ単体からは
    # どちらの関数由来かを区別できないため、既存の2 job(買い候補チェック/保有株チェック)の
    # どちらか一方へ誤って割り当てず、専用の名称を持つ(USER決定)。
    ASYNC_INVOKE_FAILURE = "非同期実行の失敗"
    OTHER = "その他の処理"  # 対応表に無い名前の落ち先(内部名を出さない)


# 内部の関数名(スタック名の前置を除いたもの)→ 利用者向けの名称。
# 全 Lambda 関数を網羅する(tests/unit/test_issue_501_incident_message.py が infra/template.yaml と
# 突き合わせる。関数が増えたら、ここへ足すまでテストが赤になる)。
_INTERNAL_NAME_TO_JOB: dict[str, IncidentJob] = {
    "buy-candidates": IncidentJob.BUY_CANDIDATES,
    "holdings-watchlist": IncidentJob.HOLDINGS_WATCHLIST,
    "disclosure-check": IncidentJob.DISCLOSURE_CHECK,
    "evaluation": IncidentJob.EVALUATION,
    "watchlist-dispatcher": IncidentJob.WATCHLIST_SCREENING,
    "watchlist-worker": IncidentJob.WATCHLIST_SCREENING,
    "watchlist-terminal-failure-handler": IncidentJob.WATCHLIST_SCREENING,
    "watchlist-batch-reconciler": IncidentJob.WATCHLIST_SCREENING,
    "weekly-review": IncidentJob.WEEKLY_REVIEW,
    "monthly-review": IncidentJob.MONTHLY_REVIEW,
    "quarterly-review": IncidentJob.QUARTERLY_REVIEW,
    "line-webhook": IncidentJob.LINE_WEBHOOK,
    "incident-notifier": IncidentJob.INCIDENT_NOTIFIER,
}

# Issue #349: SQS の DLQ(キュー名。スタック名の前置を除いたもの)→ 利用者向けの名称。
# `_INTERNAL_NAME_TO_JOB` とは別の対応表にする(全 Lambda 関数を網羅する既存の網羅性テスト
# `test_every_lambda_function_in_the_template_has_an_entry` が完全一致検査のため、Lambda
# 関数ではないキュー名をそこへ混ぜると赤くなる)。
_QUEUE_NAME_TO_JOB: dict[str, IncidentJob] = {
    "watchlist-terminal-failure-dlq": IncidentJob.WATCHLIST_SCREENING,
    "buy-candidate-terminal-failure-dlq": IncidentJob.BUY_CANDIDATES,
    "holdings-watchlist-terminal-failure-dlq": IncidentJob.HOLDINGS_WATCHLIST,
    "async-invoke-failure-dlq": IncidentJob.ASYNC_INVOKE_FAILURE,
}


def resolve_incident_job(internal_name: object) -> IncidentJob:
    """内部の関数名・キュー名・job 名を、利用者向けの名称(列挙)へ引く。

    対応表に無い名前・文字列でない値は `IncidentJob.OTHER` へ落ちる(例外にしない: 異常の通知を
    組み立てる経路で、入力の不備によって通知自体が失われないようにする)。**入力の文字列は返り値へ
    残らない**(識別子・ARN・例外文が紛れ込んでいても、列挙のどれかへ引かれるか OTHER になる)。
    Lambda 関数名の対応表(`_INTERNAL_NAME_TO_JOB`)を先に見て、無ければ SQS キュー名の対応表
    (`_QUEUE_NAME_TO_JOB`。Issue #349)を見る。
    """
    if not isinstance(internal_name, str):
        return IncidentJob.OTHER
    name = internal_name.removeprefix(_STACK_PREFIX)
    if name in _INTERNAL_NAME_TO_JOB:
        return _INTERNAL_NAME_TO_JOB[name]
    return _QUEUE_NAME_TO_JOB.get(name, IncidentJob.OTHER)


def _require_count(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int or None")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")


@dataclass(frozen=True)
class IncidentNotice:
    """「異常 1 通」に載せてよい情報の全て(これ以外は受け取れない)。"""

    job: IncidentJob
    occurred_at: dt.datetime  # timezone-aware(内部は UTC のままでよい。表示時に JST へ変換する)
    failure_count: int | None = None
    consecutive_days: int | None = None
    is_ongoing: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job, IncidentJob):
            raise TypeError("job must be an IncidentJob")
        if not isinstance(self.occurred_at, dt.datetime):
            raise TypeError("occurred_at must be a datetime")
        require_timezone_aware(self.occurred_at)
        _require_count("failure_count", self.failure_count)
        _require_count("consecutive_days", self.consecutive_days)
        if self.is_ongoing is not None and not isinstance(self.is_ongoing, bool):
            raise TypeError("is_ongoing must be bool or None")


def build_incident_message(notice: IncidentNotice) -> str:
    """「異常 1 通」の本文を組み立てる(固定の文型。allowlist の項目だけ)。"""
    lines = [
        _HEADLINE,
        f"対象: {notice.job.value}",
        f"発生時刻: {to_jst(notice.occurred_at).strftime('%H:%M')}",
    ]
    if notice.failure_count is not None:
        lines.append(f"件数: {notice.failure_count}件")
    if notice.consecutive_days is not None:
        lines.append(f"連続日数: {notice.consecutive_days}日")
    if notice.is_ongoing is not None:
        lines.append(f"継続中: {'はい' if notice.is_ongoing else 'いいえ'}")
    return "\n".join(lines)
