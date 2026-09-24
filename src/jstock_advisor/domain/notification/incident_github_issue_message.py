"""Issue #508(#132 X-9): 本番ジョブ異常のGitHub Issue自動起票の本文を組み立てる。

**純粋な関数と型だけ**である(ネットワーク・AWS・永続化に触れない。GitHub API呼び出しは
`services/incident_github_issue_service.py`の責務)。#501(`incident_message.py`)と
同じallowlist契約を踏襲する: jobの利用者向け名称・時刻・件数・日数・真偽値・
occurrence_count・failure_stage(固定の分類語。#506 allowlistのfailure_stageと同じ)・
fingerprint(dedup用マーカー)のみを本文へ出す。

識別子・銘柄・所有者・stack trace・生exception message・AWS account ID・ARN・
request ID・secret・tokenのいずれも受け取らない(型で締める)。PUBLIC repositoryへ
そのまま公開されることを前提とする(CLAUDE.md §2 / operations_manual.md 21節)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.domain.jst import require_timezone_aware, to_jst
from jstock_advisor.domain.notification.incident_message import IncidentJob

_ISSUE_TITLE_PREFIX = "[Production Incident]"


def _require_count(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int or None")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")


def issue_marker(fingerprint: str) -> str:
    """dedup用マーカー(HTMLコメント)。stale claim復旧時の実在確認
    (`services/incident_github_issue_service.py`)でも同じ文字列を検索に使うため公開する。
    """
    return f"<!-- incident_fingerprint: {fingerprint} -->"


def comment_marker(fingerprint: str, occurrence_count: int) -> str:
    """コメント用マーカー(HTMLコメント)。stale comment claim復旧時の実在確認でも
    同じ文字列を検索に使うため公開する。
    """
    return (
        f"<!-- incident_fingerprint: {fingerprint} -->\n"
        f"<!-- occurrence_count: {occurrence_count} -->"
    )


@dataclass(frozen=True)
class IncidentIssueNotice:
    """GitHub Issue本文に載せてよい情報の全て(これ以外は受け取れない)。"""

    job: IncidentJob
    occurred_at: dt.datetime  # timezone-aware(内部はUTCのまま。表示時にJSTへ変換)
    fingerprint: str  # dedup用マーカーとしてのみ本文へ埋め込む(#502)
    occurrence_count: int
    failure_stage: str
    failure_count: int | None = None
    consecutive_days: int | None = None
    is_ongoing: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job, IncidentJob):
            raise TypeError("job must be an IncidentJob")
        if not isinstance(self.occurred_at, dt.datetime):
            raise TypeError("occurred_at must be a datetime")
        require_timezone_aware(self.occurred_at)
        if not isinstance(self.fingerprint, str) or not self.fingerprint:
            raise ValueError("fingerprint must be a non-empty str")
        if isinstance(self.occurrence_count, bool) or not isinstance(self.occurrence_count, int):
            raise TypeError("occurrence_count must be int")
        if self.occurrence_count < 1:
            raise ValueError("occurrence_count must be >= 1")
        if not isinstance(self.failure_stage, str) or not self.failure_stage:
            raise ValueError("failure_stage must be a non-empty str")
        _require_count("failure_count", self.failure_count)
        _require_count("consecutive_days", self.consecutive_days)
        if self.is_ongoing is not None and not isinstance(self.is_ongoing, bool):
            raise TypeError("is_ongoing must be bool or None")


def build_incident_issue_title(notice: IncidentIssueNotice) -> str:
    return f"{_ISSUE_TITLE_PREFIX} {notice.job.value}で異常を検知しました"


def build_incident_issue_body(
    notice: IncidentIssueNotice, *, previous_issue_number: int | None = None
) -> str:
    """新規Issue作成時の本文(固定の文型。allowlistの項目だけ)。

    `previous_issue_number`は、直前のIssueがCLOSEDだったため新規作成した場合のみ
    指定する(#508決定事項: reopenせず新規Issue+旧番号参照)。
    """
    lines = [
        "## 概要",
        f"{notice.job.value}で本番ジョブ異常を検知しました。詳細はCloudWatch Logs等、"
        "運用側の記録を確認してください(本Issueには詳細ログ・stack trace・"
        "AWSリソース識別子を含みません)。",
    ]
    if previous_issue_number is not None:
        lines.append(f"\nPrevious issue: #{previous_issue_number}")
    lines += [
        "",
        "## 検知情報",
        f"対象: {notice.job.value}",
        f"分類: {notice.failure_stage}",
        f"発生時刻: {to_jst(notice.occurred_at).strftime('%Y-%m-%d %H:%M')} JST",
        f"発生回数: {notice.occurrence_count}回目",
    ]
    if notice.failure_count is not None:
        lines.append(f"件数: {notice.failure_count}件")
    if notice.consecutive_days is not None:
        lines.append(f"連続日数: {notice.consecutive_days}日")
    if notice.is_ongoing is not None:
        lines.append(f"継続中: {'はい' if notice.is_ongoing else 'いいえ'}")
    lines += [
        "",
        "## 参考",
        "運用手順は障害対応runbook(Issue #500)を参照してください。",
        "",
        issue_marker(notice.fingerprint),
    ]
    return "\n".join(lines)


def build_incident_comment_body(notice: IncidentIssueNotice) -> str:
    """既存OPEN Issueへの再発コメント本文(固定の文型。allowlistの項目だけ)。"""
    lines = [
        f"### 再発({notice.occurrence_count}回目)",
        f"発生時刻: {to_jst(notice.occurred_at).strftime('%Y-%m-%d %H:%M')} JST",
    ]
    if notice.failure_count is not None:
        lines.append(f"件数: {notice.failure_count}件")
    if notice.consecutive_days is not None:
        lines.append(f"連続日数: {notice.consecutive_days}日")
    if notice.is_ongoing is not None:
        lines.append(f"継続中: {'はい' if notice.is_ongoing else 'いいえ'}")
    lines += ["", comment_marker(notice.fingerprint, notice.occurrence_count)]
    return "\n".join(lines)
