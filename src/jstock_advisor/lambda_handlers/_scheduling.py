"""Lambdaハンドラ間で共有するスケジュール判定ヘルパー。

EventBridge Schedulerのcron式は「第1土曜日」のような月内序数指定に対応していない
ため、毎週土曜に実行したうえでLambda側で「今日が当月第1土曜日か」を判定する
(当初設計の方針: 「実行日が当月第1土曜日かどうかはLambda側で判定」)。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from jstock_advisor.domain.jst import to_jst

logger = logging.getLogger(__name__)


def is_first_saturday_of_month(date: dt.date) -> bool:
    return date.weekday() == 5 and date.day <= 7


def derive_scheduled_batch_id(prefix: str, event: Mapping[str, Any], now: dt.datetime) -> str:
    """EventBridge Schedulerのretryに対して安定したbatch_idを決定する
    (Issue #65 F-E7で導入、Issue #558でbuy_candidates/holdings_watchlist
    handlerへも共通化)。

    優先順位:

    1. `event["batch_id"]`が明示されていればそのまま使う(手動での同一batch_id
       再起動、または親から決定論的に算出されたbatch_idを渡す後続起動。
       いずれも既存の挙動を変更しない)。
    2. EventBridge Schedulerのcontext attribute`<aws.scheduler.scheduled-time>`
       (`infra/template.yaml`のSchedule Input経由で`event["scheduled_time"]`
       として渡す)が有効なISO8601文字列であれば、そこから決定論的に生成する。
       AWS公式ドキュメントに「同一の論理実行のretry(再配送)間で不変」という
       明示の保証文は無いが、`<aws.scheduler.execution-id>`/
       `<aws.scheduler.attempt-number>`が「試行ごとに変わる」と明記されている
       こととの対比から、scheduled-time(スケジュール定義上の起動予定時刻。
       実際に試行した時刻ではない)は試行に依存しない値と解釈できる(妥当な
       推論。レビュー対応: PR #556 F2)。ドキュメントの例はZ付き(UTC表記)だが、
       「常にUTC」という明記も無い。offsetなし(naive)で渡ってきた場合のみ
       UTCとみなす既定へ依存する。offset付きの場合は`domain/jst.py::to_jst()`
       で変換してからフォーマットする(新しい独自のタイムゾーン変換は作らない)。
    3. 上記どちらも無い場合(手動invoke・ローカルテスト等、Scheduler経由でない
       起動)は、従来どおり時刻+ランダムサフィックスで生成する(この経路は
       Scheduler retryの対象ではないため、retry間の安定性は不要)。
    """
    explicit_batch_id = event.get("batch_id")
    if explicit_batch_id:
        return str(explicit_batch_id)

    scheduled_time_raw = event.get("scheduled_time")
    if isinstance(scheduled_time_raw, str) and scheduled_time_raw:
        try:
            scheduled_time = dt.datetime.fromisoformat(scheduled_time_raw.replace("Z", "+00:00"))
        except ValueError:
            scheduled_time = None
            logger.warning(
                "derive_scheduled_batch_id: unparseable scheduled_time=%r, "
                "falling back to random batch_id (retry-stability lost for this invocation)",
                scheduled_time_raw,
            )
        if scheduled_time is not None:
            if scheduled_time.tzinfo is None:
                scheduled_time = scheduled_time.replace(tzinfo=dt.UTC)
            jst_scheduled_time = to_jst(scheduled_time)
            return f"{prefix}-{jst_scheduled_time.strftime('%Y%m%dT%H%M%S')}"

    return f"{prefix}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
