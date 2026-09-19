"""Issue #117 Phase B1b-4a: SQSメッセージの事前走査(prescan)の単体テスト。

通知サービスの構築(認証情報が必須)を要するのは、NEW_CANDIDATE_SCREENINGを
含む呼び出しだけであることを固定する。本関数は例外を送出しない(解釈できない
メッセージの失敗は本処理のループが従来どおり送出する)。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jstock_advisor.infrastructure.aws.batch_tracker import WatchlistJobType
from jstock_advisor.lambda_handlers._watchlist_notification_prescan import (
    sqs_records_require_notification_service,
)


def _event(*bodies: Any) -> dict[str, Any]:
    return {"Records": [{"body": b if isinstance(b, str) else json.dumps(b)} for b in bodies]}


def _msg(job_type: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"batch_id": "b-1", "stock_code": "1111"}
    if job_type is not None:
        body["job_type"] = job_type
    return body


def test_new_candidate_message_requires_the_service() -> None:
    event = _event(_msg("NEW_CANDIDATE_SCREENING"))
    assert sqs_records_require_notification_service(event, missing_job_type_default=None) is True


def test_maintenance_only_does_not_require_the_service() -> None:
    event = _event(_msg("WATCHLIST_MAINTENANCE"))
    assert sqs_records_require_notification_service(event, missing_job_type_default=None) is False


def test_any_new_candidate_in_a_mixed_batch_requires_the_service() -> None:
    event = _event(_msg("WATCHLIST_MAINTENANCE"), _msg("NEW_CANDIDATE_SCREENING"))
    assert sqs_records_require_notification_service(event, missing_job_type_default=None) is True


def test_no_records_does_not_require_the_service() -> None:
    assert sqs_records_require_notification_service({}, missing_job_type_default=None) is False
    assert (
        sqs_records_require_notification_service({"Records": []}, missing_job_type_default=None)
        is False
    )


def test_missing_job_type_follows_the_callers_default() -> None:
    """worker(既定なし)は欠損を要否判定から外し、terminal failure(既定=NEW_CANDIDATE)は
    本処理と同じくNEW_CANDIDATE_SCREENING扱いにする。"""
    event = _event(_msg(None))
    assert sqs_records_require_notification_service(event, missing_job_type_default=None) is False
    assert (
        sqs_records_require_notification_service(
            event, missing_job_type_default=WatchlistJobType.NEW_CANDIDATE_SCREENING
        )
        is True
    )


@pytest.mark.parametrize(
    "bad_body",
    [
        "not-json",
        json.dumps(["not", "a", "dict"]),
        json.dumps(_msg("WATCHLIST_MAINTENENCE")),  # typo=未知値
        json.dumps({"batch_id": "b-1", "job_type": 123}),
    ],
)
def test_unparseable_or_unknown_messages_are_ignored_without_raising(bad_body: str) -> None:
    """失敗は本処理のループが従来どおり送出する。prescanは例外にしない(挙動不変)。"""
    event = _event(bad_body)
    assert sqs_records_require_notification_service(event, missing_job_type_default=None) is False


def test_record_without_body_is_ignored() -> None:
    event: dict[str, Any] = {"Records": [{}]}
    assert sqs_records_require_notification_service(event, missing_job_type_default=None) is False


def test_an_unparseable_message_does_not_hide_a_new_candidate_message() -> None:
    event = _event("not-json", _msg("NEW_CANDIDATE_SCREENING"))
    assert sqs_records_require_notification_service(event, missing_job_type_default=None) is True
