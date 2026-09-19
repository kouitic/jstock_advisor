"""watchlist系のSQS handlerが、LINE通知サービスの構築を要するかを事前に判定する(Issue #117)。

## なぜ必要か

worker / terminal failureの両handlerは、1回の呼び出しで複数のjob_type
(NEW_CANDIDATE_SCREENING / WATCHLIST_MAINTENANCE)のメッセージを処理しうる。
LINE通知サービスを使うのはNEW_CANDIDATE_SCREENINGのfinalizeだけであり、
WATCHLIST_MAINTENANCEしか処理しない呼び出しが、通知に不要なLINE認証情報の
欠落で新たに失敗してはならない。

一方、認証情報が必要な呼び出しは、**状態変更(リース取得・完了記録・終端記録)より前**に
失敗させなければならない。状態変更の後に構築して失敗すると、処理済みの状態だけが
残り、SQSの再配信では処理が再開されない中途状態を作る(dispatcherで実際に必要になった
のと同じ論点)。したがってメッセージ本文を先に走査(prescan)し、通知サービスが必要な
場合だけ、状態変更の前にstrictな構築を行う。

## 本処理との判定の一致

本関数は、各handlerの本処理と**同じ**`resolve_watchlist_job_type()`を使う。
本関数は例外を送出しない: 本文が解釈できないメッセージは判定から外し、その失敗は
本処理のループが従来どおり(そのメッセージの処理時に)送出する。つまり不正な
メッセージの扱いは変わらない。
"""

from __future__ import annotations

import json
from typing import Any

from jstock_advisor.infrastructure.aws.batch_tracker import (
    UnknownWatchlistJobTypeError,
    WatchlistJobType,
    resolve_watchlist_job_type,
)


def sqs_records_require_notification_service(
    event: dict[str, Any], *, missing_job_type_default: WatchlistJobType | None
) -> bool:
    """SQSイベントにNEW_CANDIDATE_SCREENINGのメッセージが1件でもあればTrueを返す。

    `missing_job_type_default`はjob_type欠損時の既定で、本処理の
    `resolve_watchlist_job_type(default=...)`と同じ値を渡すこと
    (worker = None:欠損は本処理が例外にする / terminal failure = NEW_CANDIDATE_SCREENING)。
    """
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            job_type = resolve_watchlist_job_type(
                body.get("job_type"), default=missing_job_type_default
            )
        except (ValueError, KeyError, TypeError, AttributeError, UnknownWatchlistJobTypeError):
            # 解釈できないメッセージは判定から外す。失敗は本処理のループが従来どおり送出する。
            continue
        if job_type is WatchlistJobType.NEW_CANDIDATE_SCREENING:
            return True
    return False
