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
from jstock_advisor.domain.notification.incident_signal import FailureClass

# UNHANDLED_FAILURE用(既存。変更しない)。HANDLED_FAILURE用は_HANDLED_HEADLINE。
_HEADLINE = "⚠️ 本番処理でエラーが発生しました。システム側で調査情報を記録しました。"
# Issue #724: HANDLED_FAILUREはGitHub Issueを自動作成しないため、恒久記録を
# 示唆する文言(「システム側で調査情報を記録しました」)を含めない中立的な文面。
_HANDLED_HEADLINE = "⚠️ 本番処理の一部で問題が発生しました。"

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
    # Issue #533(#319 Phase 2): SQS worker Lambda。dispatch経路(トグル既定false)が
    # 何であってもwatchlist-worker/watchlist-dispatcherと同様、実行内容は親と同じ
    # 「買い候補チェック」であるため同一jobへ割り当てる(専用jobは作らない)。
    "buy-candidate-worker": IncidentJob.BUY_CANDIDATES,
    "holdings-watchlist": IncidentJob.HOLDINGS_WATCHLIST,
    "holdings-watchlist-worker": IncidentJob.HOLDINGS_WATCHLIST,
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


class IncidentContent(StrEnum):
    """HANDLED_FAILURE通知の「内容」行に出してよい、既知のreason_codeの
    利用者向け説明文(値がそのまま本文に出る)。IncidentJob/
    IncidentFailureStageと同じ設計(Issue #724)。"""

    BUY_CANDIDATES_ANALYSIS_FAILED = "銘柄分析の一部が完了しませんでした"
    # Issue #724 PR #740レビュー是正(MUST F-1由来の派生修正): 当初
    # HOLDINGS_WATCHLIST_EVALUATION_RECORD_SAVE_FAILEDと文言が完全一致して
    # おり、StrEnumの値重複によりaliasになっていた(IncidentContentの
    # 列挙から名前が1つ消える)ことを、網羅性guardの実装過程で発見し是正した。
    BUY_CANDIDATES_EVALUATION_RECORD_SAVE_FAILED = "買い候補の判定結果の記録保存に失敗しました"
    BUY_CANDIDATES_NOTIFICATION_OUTCOME_RECORD_UPDATE_FAILED = "通知結果の記録更新に失敗しました"
    HOLDINGS_WATCHLIST_PORTFOLIO_PRICE_FETCH_FAILED = (
        "保有資産見積もりに必要な株価取得の一部に失敗しました"
    )
    HOLDINGS_WATCHLIST_EVALUATION_RECORD_SAVE_FAILED = "保有銘柄の判定結果の記録保存に失敗しました"
    HOLDINGS_WATCHLIST_ANALYSIS_FAILED = "保有銘柄分析の一部が完了しませんでした"
    EVALUATION_AGGREGATE_COMMIT_FAILED = "評価結果の集計確定に失敗しました"
    EVALUATION_AUDIT_PERSIST_FAILED = "評価処理の記録保存に失敗しました"
    WATCHLIST_FINALIZER_REPOSITORY_ADD_FAILED = "ウォッチリストへの銘柄追加の一部に失敗しました"
    WATCHLIST_FINALIZER_UNEXPECTED_ERROR_COUNT = (
        "ウォッチリスト判定処理で想定外のエラーが発生しました"
    )
    RECONCILER_COMPLETION_RECOVERY_INVOKE_FAILED = "処理完了の復旧処理の呼び出しに失敗しました"
    RECONCILER_TRADE_EVENT_RECONCILIATION_FAILED = "売買記録の整合性確認処理に失敗しました"
    RECONCILER_FINALIZE_RETRY_UNEXPECTED_ERROR = "処理完了の再試行で想定外のエラーが発生しました"
    RECONCILER_NOTIFICATION_RETRY_UNEXPECTED_ERROR = "通知の再試行で想定外のエラーが発生しました"
    RECONCILER_TIMEOUT_FINALIZING_UNEXPECTED_ERROR = (
        "処理時間超過後の後処理で想定外のエラーが発生しました"
    )
    RECONCILER_MAINTENANCE_TRIGGER_RETRY_UNEXPECTED_ERROR = (
        "メンテナンス処理の再試行で想定外のエラーが発生しました"
    )
    WATCHLIST_MISSED_SCHEDULE = "定時実行が行われなかった可能性があります"
    WATCHLIST_UNIVERSE_LOAD_FAILURE_STREAK = "銘柄ユニバースの取得が複数日連続で失敗しています"
    WATCHLIST_QUEUE_BACKLOG = "処理待ちが滞留しています"
    WATCHLIST_DELETION_ZERO_STREAK = "ウォッチリストからの削除が複数日連続で発生していません"
    BUY_CANDIDATES_STUCK_BATCH = "買い候補チェックの処理が完了せず滞留している可能性があります"
    HOLDINGS_WATCHLIST_STUCK_BATCH = "保有株チェックの処理が完了せず滞留している可能性があります"
    CLOUDWATCH_ALARM = "システムの監視アラームが検知されました"
    OTHER = "技術的な問題を検知しました"  # 対応表に無いreason_codeの落ち先


# 内部のreason_code(IncidentSignal.error_type)→ 利用者向けの「内容」文。
# tests/unit/test_issue_501_incident_message.pyが、列挙した5箇所
# (buy_candidates/holdings_watchlist/evaluationの_notify_handled_failure_
# safely第2位置引数、finalizerのdict literal、reconcilerのboundary
# metadata)を走査し、本辞書のkeyと突き合わせる。この5箇所の書き方で
# HANDLEDの発行元を1件削除する・新しいHANDLED発行元を無登録のまま追加する、
# のいずれもテストが赤くなる。★キーワード引数での指定・列挙外の新規
# ファイルへの追加は視界外であり赤くならない(src全数の走査ではない。
# 視界外を塞ぐ仕組みは#745)。新しい発行元を足すときは、本辞書と合わせて
# 同テストの走査対象も更新すること。運用トレンド検知6件
# (watchlist_missed_schedule等)とCloudWatchAlarmの計7件は、現時点では
# envelopeがfailure_classをHANDLED_FAILUREに設定していないため到達しない
# 先行登録であり、同テストが別途明示的に固定している(発行元が将来HF化
# された場合はそちらのレビューで本コメント・テスト双方を更新すること)。
_REASON_CODE_TO_CONTENT: dict[str, IncidentContent] = {
    "BUY_CANDIDATES_ANALYSIS_FAILED": IncidentContent.BUY_CANDIDATES_ANALYSIS_FAILED,
    "BUY_CANDIDATES_EVALUATION_RECORD_SAVE_FAILED": (
        IncidentContent.BUY_CANDIDATES_EVALUATION_RECORD_SAVE_FAILED
    ),
    "BUY_CANDIDATES_NOTIFICATION_OUTCOME_RECORD_UPDATE_FAILED": (
        IncidentContent.BUY_CANDIDATES_NOTIFICATION_OUTCOME_RECORD_UPDATE_FAILED
    ),
    "HOLDINGS_WATCHLIST_PORTFOLIO_PRICE_FETCH_FAILED": (
        IncidentContent.HOLDINGS_WATCHLIST_PORTFOLIO_PRICE_FETCH_FAILED
    ),
    "HOLDINGS_WATCHLIST_EVALUATION_RECORD_SAVE_FAILED": (
        IncidentContent.HOLDINGS_WATCHLIST_EVALUATION_RECORD_SAVE_FAILED
    ),
    "HOLDINGS_WATCHLIST_ANALYSIS_FAILED": IncidentContent.HOLDINGS_WATCHLIST_ANALYSIS_FAILED,
    "EVALUATION_AGGREGATE_COMMIT_FAILED": IncidentContent.EVALUATION_AGGREGATE_COMMIT_FAILED,
    "EVALUATION_AUDIT_PERSIST_FAILED": IncidentContent.EVALUATION_AUDIT_PERSIST_FAILED,
    "watchlist_finalizer_repository_add_failed": (
        IncidentContent.WATCHLIST_FINALIZER_REPOSITORY_ADD_FAILED
    ),
    "watchlist_finalizer_unexpected_error_count": (
        IncidentContent.WATCHLIST_FINALIZER_UNEXPECTED_ERROR_COUNT
    ),
    "reconciler_completion_recovery_invoke_failed": (
        IncidentContent.RECONCILER_COMPLETION_RECOVERY_INVOKE_FAILED
    ),
    "reconciler_trade_event_reconciliation_failed": (
        IncidentContent.RECONCILER_TRADE_EVENT_RECONCILIATION_FAILED
    ),
    "reconciler_finalize_retry_unexpected_error": (
        IncidentContent.RECONCILER_FINALIZE_RETRY_UNEXPECTED_ERROR
    ),
    "reconciler_notification_retry_unexpected_error": (
        IncidentContent.RECONCILER_NOTIFICATION_RETRY_UNEXPECTED_ERROR
    ),
    "reconciler_timeout_finalizing_unexpected_error": (
        IncidentContent.RECONCILER_TIMEOUT_FINALIZING_UNEXPECTED_ERROR
    ),
    "reconciler_maintenance_trigger_retry_unexpected_error": (
        IncidentContent.RECONCILER_MAINTENANCE_TRIGGER_RETRY_UNEXPECTED_ERROR
    ),
    "watchlist_missed_schedule": IncidentContent.WATCHLIST_MISSED_SCHEDULE,
    "watchlist_universe_load_failure_streak": (
        IncidentContent.WATCHLIST_UNIVERSE_LOAD_FAILURE_STREAK
    ),
    "watchlist_queue_backlog": IncidentContent.WATCHLIST_QUEUE_BACKLOG,
    "watchlist_deletion_zero_streak": IncidentContent.WATCHLIST_DELETION_ZERO_STREAK,
    "buy_candidates_stuck_batch": IncidentContent.BUY_CANDIDATES_STUCK_BATCH,
    "holdings_watchlist_stuck_batch": IncidentContent.HOLDINGS_WATCHLIST_STUCK_BATCH,
    "CloudWatchAlarm": IncidentContent.CLOUDWATCH_ALARM,
}


def resolve_incident_content(reason_code: object) -> IncidentContent:
    """内部のreason_code生文字列を、利用者向けの「内容」文(列挙)へ引く。

    対応表に無い値・文字列でない値は`IncidentContent.OTHER`へ落ちる
    (`resolve_incident_job()`/`resolve_incident_failure_stage()`と同じ理由:
    入力の不備によって通知自体を失わない。かつ**入力の生文字列は返り値へ
    残らない**ため、将来未知のreason_codeが追加されても、対応表を更新する
    までPUBLIC repositoryへ自由文字列が漏れることはない)。
    """
    if not isinstance(reason_code, str):
        return IncidentContent.OTHER
    return _REASON_CODE_TO_CONTENT.get(reason_code, IncidentContent.OTHER)


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
    # Issue #724: HANDLED_FAILUREのときだけ「内容」行を出す(headline分岐にも使う)。
    # UNHANDLED_FAILURE(既定値)では本文は1バイトも変わらない(契約4)。
    failure_class: FailureClass = FailureClass.UNHANDLED_FAILURE
    content: IncidentContent | None = None

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
        if not isinstance(self.failure_class, FailureClass):
            raise TypeError("failure_class must be a FailureClass")
        if self.content is not None and not isinstance(self.content, IncidentContent):
            raise TypeError("content must be an IncidentContent or None")


def build_incident_message(notice: IncidentNotice) -> str:
    """「異常 1 通」の本文を組み立てる(固定の文型。allowlist の項目だけ)。

    Issue #724: HANDLED_FAILUREのときだけ、恒久記録を示唆しない中立的な
    headline(`_HANDLED_HEADLINE`)を使い、「対象」の直後に「内容」行を
    追加する。UNHANDLED_FAILUREは`_HEADLINE`のまま、内容行も追加しない
    (契約4: 既存通知契約を不用意に変更しない)。
    """
    is_handled = notice.failure_class is FailureClass.HANDLED_FAILURE
    lines = [
        _HANDLED_HEADLINE if is_handled else _HEADLINE,
        f"対象: {notice.job.value}",
    ]
    if is_handled and notice.content is not None:
        lines.append(f"内容: {notice.content.value}")
    lines.append(f"発生時刻: {to_jst(notice.occurred_at).strftime('%H:%M')}")
    if notice.failure_count is not None:
        lines.append(f"件数: {notice.failure_count}件")
    if notice.consecutive_days is not None:
        lines.append(f"連続日数: {notice.consecutive_days}日")
    if notice.is_ongoing is not None:
        lines.append(f"継続中: {'はい' if notice.is_ongoing else 'いいえ'}")
    return "\n".join(lines)
