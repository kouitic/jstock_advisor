"""週次改善レビューのGitHub Issue対応状況(ImprovementTask)を、DynamoDBの
ConditionExpression付きUpdateItemで原子的に状態遷移させる(振り返り機能改修)。

batch_tracker.py(ウォッチリスト自動追加バッチの排他制御)で確立済みの
「所有者+期限付きlease」パターンを踏襲する。新規claim(try_claim_new_*)は
既に処理中(ISSUE_CREATING/該当review_weekのcomment_claim)の状態を絶対に
奪わない。stale(claim期限切れ)の場合も、呼び出し側(github_issue_service.py)が
先にGitHub側の実在確認(reconciliation)を行ってから、別関数(try_reclaim_stale_*)
で明示的に再claimする2段階方式とする(期限切れなら即座に再claimできる設計だと、
「GitHubには作成済みだがDynamoDB更新だけ失敗した」場合に二重Issue/二重コメントを
作ってしまうため)。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import boto3
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.enums import (
    ImprovementPriority,
    ImprovementTaskStatus,
    RecommendationType,
)
from jstock_advisor.infrastructure.collection_store import resolve_table_name

_TABLE_FILE_NAME = "improvement_tasks.json"  # resolve_table_nameの命名規則に合わせる

# batch_tracker.pyの本番incident対応と同じ理由(TransactWriteItemsとUpdateItemが
# 同一項目へほぼ同時にアクセスした場合の一時的な競合)により、
# ConditionalCheckFailedExceptionと同様に扱う。
_CONDITION_FAILURE_CODES = ("ConditionalCheckFailedException", "TransactionConflictException")


def _table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))


def get_improvement_task(candidate_key: str) -> dict[str, Any] | None:
    response = _table().get_item(Key={"candidate_key": candidate_key})
    item: dict[str, Any] | None = response.get("Item")
    return item


def ensure_task_exists(
    candidate_key: str,
    recommendation_type: RecommendationType,
    rule_version: str,
    segment_key: str | None,
    priority: ImprovementPriority,
    now: dt.datetime,
) -> None:
    """初回のCandidate検出時にImprovementTask項目を作成する(既に存在する場合は
    何もしない、冪等)。"""
    now_iso = now.isoformat()
    try:
        _table().put_item(
            Item={
                "candidate_key": candidate_key,
                "recommendation_type": recommendation_type.value,
                "rule_version": rule_version,
                "segment_key": segment_key,
                "priority": priority.value,
                "status": ImprovementTaskStatus.CANDIDATE.value,
                "created_at": now_iso,
                "updated_at": now_iso,
            },
            ConditionExpression="attribute_not_exists(candidate_key)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return
        raise


def mark_skipped_not_configured(candidate_key: str, now: dt.datetime) -> None:
    """issue_creation_enabled=falseによる正常なスキップ(異常ではない)。"""
    _table().update_item(
        Key={"candidate_key": candidate_key},
        UpdateExpression="SET #status = :skipped, updated_at = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":skipped": ImprovementTaskStatus.SKIPPED_NOT_CONFIGURED.value,
            ":now": now.isoformat(),
        },
    )


def mark_configuration_error(candidate_key: str, now: dt.datetime) -> None:
    """issue_creation_enabled=trueなのにGitHub認証情報が不備・取得失敗している
    異常状態(運用エラー通知の対象)。"""
    _table().update_item(
        Key={"candidate_key": candidate_key},
        UpdateExpression="SET #status = :error, updated_at = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":error": ImprovementTaskStatus.CONFIGURATION_ERROR.value,
            ":now": now.isoformat(),
        },
    )


def try_claim_new_issue_creation(
    candidate_key: str, now: dt.datetime, timeout_minutes: int
) -> bool:
    """status<>ISSUE_CREATINGの場合のみ、ISSUE_CREATINGへ原子的に遷移する。
    ISSUE_CREATINGの間は期限に関わらず絶対に奪わない(staleな場合は
    try_reclaim_stale_issue_creationを、reconciliation後にのみ呼ぶこと)。"""
    now_iso = now.isoformat()
    expires_iso = (now + dt.timedelta(minutes=timeout_minutes)).isoformat()
    try:
        _table().update_item(
            Key={"candidate_key": candidate_key},
            UpdateExpression=(
                "SET #status = :creating, issue_claimed_at = :now, "
                "issue_claim_expires_at = :expires, updated_at = :now"
            ),
            ConditionExpression="attribute_not_exists(#status) OR #status <> :creating",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":creating": ImprovementTaskStatus.ISSUE_CREATING.value,
                ":now": now_iso,
                ":expires": expires_iso,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def try_reclaim_stale_issue_creation(
    candidate_key: str,
    expected_claimed_at: str,
    now: dt.datetime,
    timeout_minutes: int,
) -> bool:
    """ISSUE_CREATINGかつclaim期限切れの項目だけを対象に再claimする。呼び出し側は
    このメソッドを呼ぶ前に必ずGitHub側の実在確認(reconciliation)を行うこと
    (見つかればmark_issue_createdで復旧し、このメソッドは呼ばない)。
    expected_claimed_atはreconciliation時に読んだissue_claimed_atをそのまま渡し、
    その間に他の実行が既に再claimしていた場合は失敗する(楽観的排他)。"""
    now_iso = now.isoformat()
    expires_iso = (now + dt.timedelta(minutes=timeout_minutes)).isoformat()
    try:
        _table().update_item(
            Key={"candidate_key": candidate_key},
            UpdateExpression=(
                "SET issue_claimed_at = :now, issue_claim_expires_at = :expires, "
                "updated_at = :now"
            ),
            ConditionExpression=(
                "#status = :creating AND issue_claimed_at = :expected "
                "AND issue_claim_expires_at < :now"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":creating": ImprovementTaskStatus.ISSUE_CREATING.value,
                ":expected": expected_claimed_at,
                ":now": now_iso,
                ":expires": expires_iso,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def mark_issue_created(
    candidate_key: str,
    issue_number: int,
    issue_url: str,
    now: dt.datetime,
    previous_issue_number: int | None = None,
) -> None:
    """新規Issue作成成功、またはstale reconciliationでの実在Issue復旧の両方で使う。
    previous_issue_numberはClosed Issue検出後の再発Issue作成時のみ指定する。"""
    update_expression = (
        "SET #status = :created, github_issue_number = :number, "
        "github_issue_url = :url, updated_at = :now "
        "REMOVE issue_claimed_at, issue_claim_expires_at"
    )
    values: dict[str, Any] = {
        ":created": ImprovementTaskStatus.ISSUE_CREATED.value,
        ":number": issue_number,
        ":url": issue_url,
        ":now": now.isoformat(),
    }
    if previous_issue_number is not None:
        update_expression = update_expression.replace(
            "updated_at = :now ", "updated_at = :now, previous_github_issue_number = :prev "
        )
        values[":prev"] = previous_issue_number

    _table().update_item(
        Key={"candidate_key": candidate_key},
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=values,
    )


def mark_issue_creation_failed(candidate_key: str, now: dt.datetime) -> None:
    """claimを解放し(REMOVE)、次回週次実行での再試行を許可する。"""
    _table().update_item(
        Key={"candidate_key": candidate_key},
        UpdateExpression=(
            "SET #status = :failed, updated_at = :now "
            "REMOVE issue_claimed_at, issue_claim_expires_at"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":failed": ImprovementTaskStatus.ISSUE_CREATION_FAILED.value,
            ":now": now.isoformat(),
        },
    )


def try_claim_new_comment(
    candidate_key: str, review_week: str, now: dt.datetime, timeout_minutes: int
) -> bool:
    """当該review_weekについて、既存の(未失効・失効済み問わず)claimが一切無い
    場合のみ成功する。既にclaimがある場合(staleかどうかに関わらず)は失敗し、
    呼び出し側はstale判定(期限切れか)を自分で確認したうえでtry_reclaim_stale_
    commentへ進む(reconciliation後にのみ)。"""
    now_iso = now.isoformat()
    expires_iso = (now + dt.timedelta(minutes=timeout_minutes)).isoformat()
    try:
        _table().update_item(
            Key={"candidate_key": candidate_key},
            UpdateExpression=(
                "SET comment_claim_review_week = :week, comment_claim_expires_at = :expires, "
                "updated_at = :now"
            ),
            ConditionExpression=(
                "(attribute_not_exists(last_commented_review_week) OR "
                "last_commented_review_week <> :week) AND "
                "(attribute_not_exists(comment_claim_review_week) OR "
                "comment_claim_review_week <> :week)"
            ),
            ExpressionAttributeValues={
                ":week": review_week,
                ":now": now_iso,
                ":expires": expires_iso,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def try_reclaim_stale_comment(
    candidate_key: str,
    review_week: str,
    expected_claim_expires_at: str,
    now: dt.datetime,
    timeout_minutes: int,
) -> bool:
    """comment_claim_review_week=review_weekかつ失効済みの場合のみ再claimする。
    呼び出し側は必ず先にGitHub側の実在コメント確認(reconciliation)を行うこと。"""
    now_iso = now.isoformat()
    expires_iso = (now + dt.timedelta(minutes=timeout_minutes)).isoformat()
    try:
        _table().update_item(
            Key={"candidate_key": candidate_key},
            UpdateExpression="SET comment_claim_expires_at = :expires, updated_at = :now",
            ConditionExpression=(
                "comment_claim_review_week = :week AND "
                "comment_claim_expires_at = :expected AND comment_claim_expires_at < :now"
            ),
            ExpressionAttributeValues={
                ":week": review_week,
                ":expected": expected_claim_expires_at,
                ":now": now_iso,
                ":expires": expires_iso,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def mark_comment_posted(candidate_key: str, review_week: str, now: dt.datetime) -> None:
    _table().update_item(
        Key={"candidate_key": candidate_key},
        UpdateExpression=(
            "SET last_commented_review_week = :week, updated_at = :now "
            "REMOVE comment_claim_review_week, comment_claim_expires_at"
        ),
        ExpressionAttributeValues={":week": review_week, ":now": now.isoformat()},
    )
