"""incident の GitHub Issue 起票の抑止の純粋な判定(Issue #926。#876 の大量起票への構造的な対策)。

## 背景(実測。#188 …6114701122)

新規 Issue の作成には occurrence 単位の gate がなく、既存の Issue が CLOSED(reopen しない = #508
の U-5)の間は、
届いた signal 1 件ごとに新規 Issue が 1 件立つ(同一 dedup window 内に 3 回届けると 3 件)。
全体の起票件数の
上限も、異常時の停止手段もない。

## この module が持つもの(純粋。DynamoDB・GitHub・時計に触れない)

  層 1 重複の gate    precheck_new_issue: 上限が未決定か / 同じ occurrence か(config 不要の gate)
  / 前回作成からの最小間隔
  層 2 窓            window_id: 上限の窓 = JST の日付(固定の bucket)
  層 3 停止          制御項目の key・停止の理由の語彙・自動停止の LINE 文面
  結果の型           IssueGuardOutcome: 呼び出し元(handler)へ返す、自動停止の通知が要るか

原子的な消費・停止・通知の確保は `infrastructure/aws/incident_state_tracker.py`(DynamoDB
の条件つき更新)、
組み立ては `services/incident_github_issue_service.py` が行う。

## 最重要要件(#508)

ここでの判定・停止・通知の失敗は、LINE の incident
通知の経路に一切影響しない(呼び出し側が例外を握る)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from jstock_advisor.domain.jst import require_timezone_aware, to_jst
from jstock_advisor.domain.notification.incident_issue_limits import IncidentIssueLimits

#: IncidentStateTable に置く制御用の 1 項目の key。fingerprint(SHA-256 の 16 進 64 桁)と衝突しない
CONTROL_KEY: Final = "__control__:github_issue"

KILL_SWITCH_REASON_MANUAL: Final = "MANUAL"
KILL_SWITCH_REASON_AUTO: Final = "AUTO_GLOBAL_FLOOD"


class DenyReason(StrEnum):
    """新規 Issue を作らなかった理由(fingerprint の行に最後の理由として残す)。"""

    LIMITS_UNDECIDED = "LIMITS_UNDECIDED"
    SAME_OCCURRENCE = "SAME_OCCURRENCE"
    MIN_INTERVAL = "MIN_INTERVAL"
    FINGERPRINT_LIMIT = "FINGERPRINT_LIMIT"
    GLOBAL_LIMIT = "GLOBAL_LIMIT"


@dataclass(frozen=True)
class IssueGuardOutcome:
    """process_incident_issue の結果。LINE の経路には何も要求しない(通知が要るかだけを返す)。"""

    auto_stop_notification_due: bool = False


def window_id(now: dt.datetime) -> str:
    """上限の窓の id = JST の日付(固定の bucket。日付が替わると予算が戻る)。"""
    require_timezone_aware(now)
    return to_jst(now).date().isoformat()


def precheck_new_issue(
    limits: IncidentIssueLimits,
    state: Mapping[str, Any] | None,
    occurrence_count: int,
    now: dt.datetime,
) -> DenyReason | None:
    """新規 Issue を作ってよいかの事前確認(読取だけ。原子的な消費の前に行う)。
    作らない理由、またはなければ None。

    順序: 上限が未決定 -> 同じ occurrence -> 最小間隔。上限の未決定は『新規 Issue を作らない』
    (fail-closed)。
    occurrence gate は、前回作成した時点の occurrence 以下なら作らない(config 不要。同じ
    occurrence での
    再配送・flap で 2 件目を作らない)。
    """
    if not limits.is_decided():
        return DenyReason.LIMITS_UNDECIDED
    if state is None:
        return None
    last_occurrence = state.get("github_issue_last_created_occurrence")
    if last_occurrence is not None and int(occurrence_count) <= int(last_occurrence):
        return DenyReason.SAME_OCCURRENCE
    last_created = state.get("github_issue_last_created_at")
    if last_created is not None and _within_interval(last_created, now, limits):
        return DenyReason.MIN_INTERVAL
    return None


def _within_interval(last_created: object, now: dt.datetime, limits: IncidentIssueLimits) -> bool:
    """前回作成から最小間隔の内側か。読めない値・未来の時刻は『内側』(作らない側へ倒す)。"""
    minutes = limits.new_issue_min_interval_minutes
    if minutes is None:
        return True
    try:
        created = dt.datetime.fromisoformat(str(last_created))
        elapsed = now - created
    except (TypeError, ValueError):
        return True
    return elapsed < dt.timedelta(minutes=minutes)


def build_auto_stop_message() -> str:
    """自動停止を知らせる LINE の文面(固定。fingerprint・件数・URL・ARN を含めない)。"""
    return (
        "GitHub への自動起票を自動停止しました(起票が上限を超えたためです)。"
        "LINE の通知は引き続き届きます。"
        "再開の手順は運用手順書を参照してください。"
    )
