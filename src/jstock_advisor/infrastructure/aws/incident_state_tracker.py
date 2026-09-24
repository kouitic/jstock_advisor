"""本番ジョブ異常(incident)の通知状態を、DynamoDBのConditionExpression付き
UpdateItem/PutItemで原子的に管理する(Issue #132 X-4〔#503〕)。

`improvement_task_tracker.py` / `batch_tracker.py` で確立済みの「専用 module + raw boto3 +
フィールド単位の ConditionExpression」パターンを踏襲する(`NotificationClaim` が使う
「generic CollectionStore + レコード全体の生JSON一致」パターンは使わない。IncidentState は
claim / stale takeover / 再claim という複数の遷移経路を持つ状態機械であり、`status` 等の
特定フィールドだけを条件にできる方が素直に対応できるため)。

## 状態機械

```
(無し) --try_claim(新規)--> CLAIMED --mark_sent--> SENT --try_claim(window経過)--> CLAIMED(再)...
                               │                      │
                               └--release_claim-------┘
                          (LINE push失敗。次のretryが即座に再claimできるようにする)

CLAIMED は、claimed_at から claim_stale を超えたら他の実行が takeover できる
(LINE push成功後SENT記録前にLambdaがcrashした場合の回復)。
```

`claim_token`(uuid4)は、claim を取得した実行だけが持つ fencing token である。`mark_sent` /
`release_claim` は、呼び出し元が渡す `claim_token` が現在の値と一致する場合にのみ成功する
(`NotificationClaim.claim_token` と同じ役割。ただし比較はレコード全体の生JSON一致ではなく、
`claim_token` 1 フィールドの `ConditionExpression` で行う)。

## occurrence_count の扱い

新規 claim(`CLAIMED_NEW` / `CLAIMED_AFTER_DEDUP_WINDOW` / `CLAIMED_STALE_TAKEOVER`)が成立した
ときだけ `occurrence_count` を +1 する。`SUPPRESSED_*`(抑止)のときは増やさない(同一実行の
retry を新しい occurrence として二重に数えないため。#502 の目的「retry で3通にならない」と
対称)。

## GitHub Issue接続(#508)

`github_issue_number` / `github_issue_create_status` は、baseline 設計(USER決定。
#503 issuecomment-5796588796)が予約していたフィールドを #508 で実装したものである。
LINE の `status`(CLAIMED/SENT)とは独立した別の状態機械とし、LINE の claim/dedup 判定に
一切影響しない(`try_claim()` 等の既存関数は変更していない)。`resolved_at` /
`notification_status` は本 Issue の scope 外のため未実装のまま予約を継続する。

```
(無し) --try_claim_new_github_issue_creation--> CREATING --mark_github_issue_created--> CREATED
  CREATINGは失敗すると2種類の終端状態へ遷移する(mark_github_issue_creation_failed →
  ISSUE_CREATION_FAILED、mark_github_issue_configuration_error → CONFIGURATION_ERROR)。
  いずれも次のoccurrenceでCREATINGへ即座に再claim可能(claimを解放しているため)。

CREATED 到達後、同一fingerprintの再発時は github_issue_number へ GitHub 側の実在確認
(get_issue)を行い、OPEN ならコメント追記(comment_claim_occurrence_count による
occurrence単位の重複防止)、CLOSED なら新規Issueを作成して github_issue_number を
更新する(previous_github_issue_number に旧番号を残す)。reopenはしない。
```

CREATING の claim(`github_issue_claimed_at` / `github_issue_claim_expires_at`)が
timeout を超えて stale になった場合、`improvement_task_tracker.py` と同じ2段階方式
(呼び出し側が先にGitHub側の実在確認を行い、見つからない場合のみ
`try_reclaim_stale_github_issue_creation` で明示的に再claim)を踏襲する。

## TTL

`ttl` 属性(DynamoDB TTL)は cleanup 専用である。dedup判定・stale判定・送信可否のいずれにも
使わない(Issue #17 の `NotificationClaim` と同じ理由: DynamoDB TTLによる削除は最大48時間程度
遅延しうるため、判定の根拠にはできない)。本 module は `ttl` を書かない(cleanup の実施は
運用側の別の仕組みに委ねる。#503 の scope 外)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError

from jstock_advisor.infrastructure.collection_store import resolve_table_name

_TABLE_FILE_NAME = "incident_state.json"  # resolve_table_nameの命名規則に合わせる

# batch_tracker.py / improvement_task_tracker.py と同じ理由(TransactWriteItemsと
# UpdateItemが同一項目へほぼ同時にアクセスした場合の一時的な競合)により、
# ConditionalCheckFailedExceptionと同様に扱う。
_CONDITION_FAILURE_CODES = ("ConditionalCheckFailedException", "TransactionConflictException")

_STATUS_CLAIMED = "CLAIMED"
_STATUS_SENT = "SENT"


class IncidentClaimOutcome(StrEnum):
    """`try_claim()` の結果。`CLAIMED_*` のときだけ LINE push へ進んでよい。"""

    CLAIMED_NEW = "CLAIMED_NEW"  # fingerprint 初出
    CLAIMED_AFTER_DEDUP_WINDOW = "CLAIMED_AFTER_DEDUP_WINDOW"  # SENT + dedup_window経過 → 再通知
    CLAIMED_STALE_TAKEOVER = "CLAIMED_STALE_TAKEOVER"  # CLAIMED + claim_stale経過 → 引き継ぎ
    SUPPRESSED_DUPLICATE = "SUPPRESSED_DUPLICATE"  # SENT + dedup_window内 → 抑止(通知しない)
    SUPPRESSED_ACTIVE_CLAIM = "SUPPRESSED_ACTIVE_CLAIM"  # CLAIMED + claim_stale未経過 → 抑止


class IncidentGithubIssueStatus(StrEnum):
    """`github_issue_create_status` の値(Issue #508)。LINEの`status`とは独立。"""

    CREATING = "CREATING"
    CREATED = "CREATED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    ISSUE_CREATION_FAILED = "ISSUE_CREATION_FAILED"


def _table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))


def get_incident_state(fingerprint: str) -> dict[str, Any] | None:
    """現在の状態を読む。

    ★ レビュー指摘 F5: `try_claim()` は自分が直前に書いた値(`occurrence_count` 等)を
    呼び出し元(handler)へ本文用に返すため、結果整合読み取り(デフォルト)では自分自身の
    書き込みが読めない可能性がある。`ConsistentRead=True` を指定し、直前の書き込みを
    確実に読む。
    """
    response = _table().get_item(Key={"fingerprint": fingerprint}, ConsistentRead=True)
    item: dict[str, Any] | None = response.get("Item")
    return item


def _put_new(fingerprint: str, claim_token: str, now_iso: str) -> bool:
    """fingerprint が未出のときだけ、新規 item を原子的に作る(claim = CLAIMED)。"""
    try:
        _table().put_item(
            Item={
                "fingerprint": fingerprint,
                "status": _STATUS_CLAIMED,
                "claim_token": claim_token,
                "claimed_at": now_iso,
                "first_seen_at": now_iso,
                "last_seen_at": now_iso,
                "occurrence_count": 1,
            },
            ConditionExpression="attribute_not_exists(fingerprint)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def _try_claim_after_dedup_window(
    fingerprint: str, claim_token: str, now: dt.datetime, dedup_window: dt.timedelta
) -> bool:
    """status=SENT かつ last_notified_at が dedup_window 以上前のときだけ CLAIMED へ遷移する。

    #502 `is_duplicate_within_window()` と同じ境界(`now - last_notified_at >= window` を
    非重複側とする。`<=` ではなく `<` を使うのは cutoff を「window だけ遡った時刻」とし、
    `last_notified_at <= cutoff` で表すため)。
    """
    now_iso = now.isoformat()
    cutoff_iso = (now - dedup_window).isoformat()
    try:
        _table().update_item(
            Key={"fingerprint": fingerprint},
            UpdateExpression=(
                "SET #status = :claimed, claim_token = :token, claimed_at = :now, "
                "last_seen_at = :now ADD occurrence_count :one"
            ),
            ConditionExpression="#status = :sent AND last_notified_at <= :cutoff",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":claimed": _STATUS_CLAIMED,
                ":sent": _STATUS_SENT,
                ":token": claim_token,
                ":now": now_iso,
                ":cutoff": cutoff_iso,
                ":one": 1,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def _try_stale_takeover(
    fingerprint: str, claim_token: str, now: dt.datetime, claim_stale: dt.timedelta
) -> bool:
    """status=CLAIMED かつ claimed_at が claim_stale 以上前のときだけ、claim_token を
    差し替えて引き継ぐ(occurrence_countは増やす。旧実行が処理できなかった1件を
    数える意味で、新しいoccurrenceとして扱う)。
    """
    now_iso = now.isoformat()
    cutoff_iso = (now - claim_stale).isoformat()
    try:
        _table().update_item(
            Key={"fingerprint": fingerprint},
            UpdateExpression=(
                "SET claim_token = :token, claimed_at = :now, last_seen_at = :now "
                "ADD occurrence_count :one"
            ),
            ConditionExpression="#status = :claimed AND claimed_at <= :cutoff",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":claimed": _STATUS_CLAIMED,
                ":token": claim_token,
                ":now": now_iso,
                ":cutoff": cutoff_iso,
                ":one": 1,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def try_claim(
    fingerprint: str,
    now: dt.datetime,
    dedup_window: dt.timedelta,
    claim_stale: dt.timedelta,
) -> tuple[IncidentClaimOutcome, str | None]:
    """fingerprint の claim を原子的に取得する。戻り値は (outcome, claim_token)。

    `claim_token` は `outcome` が `CLAIMED_*` のときだけ値を持つ(呼び出し側が
    `mark_sent()` / `release_claim()` へ渡す fencing token)。`SUPPRESSED_*` のときは
    `None`(このLambda実行はLINE pushを行わない)。

    実装は「新規 → SENT+window経過 → CLAIMED+stale経過」の順に、それぞれ専用の
    条件付き書き込みを試す。途中の GetItem は判断材料であり、実際の可否は書き込み時の
    ConditionExpression が決める(GetItemとUpdateItemの間に他の実行が状態を変えても、
    最終的な書き込みは原子的に失敗する。#71系のretry-then-suppressと同じ設計)。
    """
    now_iso = now.isoformat()
    claim_token = str(uuid.uuid4())

    if _put_new(fingerprint, claim_token, now_iso):
        return IncidentClaimOutcome.CLAIMED_NEW, claim_token

    state = get_incident_state(fingerprint)
    if state is None:
        # 直前のGetItemとこのGetItemの間に、item自体が消えることは無い(本moduleは
        # DeleteItemをrelease_claim(is_new=True)でしか行わず、それはCLAIMED_NEW失敗時
        # のみ)。念のため新規側へフォールバックする(通知を失わない側)。
        if _put_new(fingerprint, claim_token, now_iso):
            return IncidentClaimOutcome.CLAIMED_NEW, claim_token
        return IncidentClaimOutcome.SUPPRESSED_ACTIVE_CLAIM, None

    if state.get("status") == _STATUS_SENT:
        if _try_claim_after_dedup_window(fingerprint, claim_token, now, dedup_window):
            return IncidentClaimOutcome.CLAIMED_AFTER_DEDUP_WINDOW, claim_token
        return IncidentClaimOutcome.SUPPRESSED_DUPLICATE, None

    # status == CLAIMED(他の値は無い契約。未知の値が来ても安全側=抑止で扱う)
    if state.get("status") == _STATUS_CLAIMED and _try_stale_takeover(
        fingerprint, claim_token, now, claim_stale
    ):
        return IncidentClaimOutcome.CLAIMED_STALE_TAKEOVER, claim_token
    return IncidentClaimOutcome.SUPPRESSED_ACTIVE_CLAIM, None


def mark_sent(fingerprint: str, claim_token: str, now: dt.datetime) -> bool:
    """LINE push成功後、CLAIMED→SENTへ遷移させる。

    `claim_token` が現在の値と一致する場合のみ成功する(このclaimの持ち主だけが遷移できる。
    一致しなければ、既に他の実行がtakeoverした後ということなのでFalseを返す。呼び出し元は
    二重にLINEを送っているため、これ以上何もしない)。
    """
    try:
        _table().update_item(
            Key={"fingerprint": fingerprint},
            UpdateExpression="SET #status = :sent, last_notified_at = :now",
            ConditionExpression="claim_token = :token",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":sent": _STATUS_SENT,
                ":now": now.isoformat(),
                ":token": claim_token,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def release_claim(fingerprint: str, claim_token: str, *, is_new: bool) -> None:
    """LINE push失敗時、claimを解放し、次のretryが待たずに再claimできるようにする。

    `is_new=True`(`CLAIMED_NEW` からの失敗): item ごと削除する(`NotificationClaim` と同じ。
    「初出」の履歴はまだ何も無いため、消しても失うものが無い。次のretryは初出として
    即座に再claimできる)。

    `is_new=False`(再claim/takeoverからの失敗): item は消さない(`occurrence_count` /
    `first_seen_at` 等の履歴を失う)。代わりに `claimed_at` を「claim_stale を超えて
    十分に古い値」へ書き換え、次のretryが**待たずに** stale takeover できるようにする
    (baseline「LINE push失敗 → claim解除 → retryに任せる」の具体化。claim_stale の
    満了を待つと、SNS/Lambdaの速いretryが5分間ずっと抑止されてしまうため)。

    ★ レビュー指摘 F4(実測欠陥): このとき `occurrence_count` を **-1 して、この claim が
    加算した分を打ち消す**。打ち消さないと、次の retry が stale takeover でもう一度 +1 する
    ため、同じ 1 回の incident が 2 回分としてカウントされ、利用者が受け取る本文の「件数」が
    不当に増える(`is_new=True` の delete は item ごと消すことで同じ効果を得ている。
    こちらは履歴を残したまま、加算だけを対称的に打ち消す)。

    いずれも `claim_token` が現在の値と一致する場合のみ実行する(他の実行が既に
    takeoverしていたら、そのclaimを壊さない)。
    """
    if is_new:
        try:
            _table().delete_item(
                Key={"fingerprint": fingerprint},
                ConditionExpression="claim_token = :token",
                ExpressionAttributeValues={":token": claim_token},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] not in _CONDITION_FAILURE_CODES:
                raise
        return
    # claim_stale の判定式は "claimed_at <= now - claim_stale" なので、そのcutoffより
    # 確実に古い値(datetime.min相当)へ書き換えれば、claim_stale の長さに関わらず
    # 即座にstale takeover可能になる。
    ancient_iso = dt.datetime.min.replace(tzinfo=dt.UTC).isoformat()
    try:
        _table().update_item(
            Key={"fingerprint": fingerprint},
            UpdateExpression="SET claimed_at = :ancient ADD occurrence_count :minus_one",
            ConditionExpression="claim_token = :token",
            ExpressionAttributeValues={
                ":ancient": ancient_iso,
                ":token": claim_token,
                ":minus_one": -1,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] not in _CONDITION_FAILURE_CODES:
            raise


# --- GitHub Issue接続(Issue #508) ---------------------------------------------
#
# `improvement_task_tracker.py`(週次改善レビューのGitHub連携)で確立済みの
# 「claim(原子的なUpdateItem) → stale時はGitHub側の実在確認 → 明示的な再claim」
# という2段階方式をそのまま踏襲する。LINEの`status`/`claim_token`とは別の属性
# (`github_issue_create_status`等)で完全に独立した状態機械とする。


def try_claim_new_github_issue_creation(
    fingerprint: str, now: dt.datetime, timeout_minutes: int
) -> bool:
    """github_issue_create_stateが「進行中(CREATING)」ではないときだけ、原子的に
    CREATINGへ遷移する。CREATING中は期限に関わらず絶対に奪わない(staleな場合は
    呼び出し側が先にGitHub側の実在確認〔reconciliation〕を行ってから、
    `try_reclaim_stale_github_issue_creation`を明示的に呼ぶこと)。
    """
    now_iso = now.isoformat()
    expires_iso = (now + dt.timedelta(minutes=timeout_minutes)).isoformat()
    try:
        _table().update_item(
            Key={"fingerprint": fingerprint},
            UpdateExpression=(
                "SET github_issue_create_status = :creating, "
                "github_issue_claimed_at = :now, github_issue_claim_expires_at = :expires"
            ),
            ConditionExpression=(
                "attribute_not_exists(github_issue_create_status) OR "
                "github_issue_create_status <> :creating"
            ),
            ExpressionAttributeValues={
                ":creating": IncidentGithubIssueStatus.CREATING.value,
                ":now": now_iso,
                ":expires": expires_iso,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def try_reclaim_stale_github_issue_creation(
    fingerprint: str,
    expected_claimed_at: str,
    now: dt.datetime,
    timeout_minutes: int,
) -> bool:
    """CREATINGかつclaim期限切れの項目だけを対象に再claimする。呼び出し側は必ず
    このメソッドを呼ぶ前にGitHub側の実在確認(reconciliation)を行うこと(見つかれば
    `mark_github_issue_created`で復旧し、このメソッドは呼ばない)。
    `expected_claimed_at`はreconciliation時に読んだ`github_issue_claimed_at`を
    そのまま渡し、その間に他の実行が既に再claimしていた場合は失敗する(楽観的排他)。
    """
    now_iso = now.isoformat()
    expires_iso = (now + dt.timedelta(minutes=timeout_minutes)).isoformat()
    try:
        _table().update_item(
            Key={"fingerprint": fingerprint},
            UpdateExpression=(
                "SET github_issue_claimed_at = :now, github_issue_claim_expires_at = :expires"
            ),
            ConditionExpression=(
                "github_issue_create_status = :creating AND "
                "github_issue_claimed_at = :expected AND "
                "github_issue_claim_expires_at < :now"
            ),
            ExpressionAttributeValues={
                ":creating": IncidentGithubIssueStatus.CREATING.value,
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


def mark_github_issue_created(
    fingerprint: str,
    issue_number: int,
    *,
    previous_issue_number: int | None = None,
) -> None:
    """新規Issue作成成功、stale reconciliationでの実在Issue復旧、またはClosed Issue
    検出後の再発Issue作成のいずれでも使う。`previous_issue_number`はClosed Issue
    検出後の再発Issue作成時のみ指定する。
    """
    update_expression = (
        "SET github_issue_create_status = :created, github_issue_number = :number "
        "REMOVE github_issue_claimed_at, github_issue_claim_expires_at"
    )
    values: dict[str, Any] = {
        ":created": IncidentGithubIssueStatus.CREATED.value,
        ":number": issue_number,
    }
    if previous_issue_number is not None:
        update_expression = update_expression.replace(
            "github_issue_number = :number ",
            "github_issue_number = :number, previous_github_issue_number = :prev ",
        )
        values[":prev"] = previous_issue_number
    _table().update_item(
        Key={"fingerprint": fingerprint},
        UpdateExpression=update_expression,
        ExpressionAttributeValues=values,
    )


def mark_github_issue_creation_failed(fingerprint: str) -> None:
    """GitHub API呼び出し自体の失敗(timeout・5xx・4xx等)。claimを解放し(REMOVE)、
    次のoccurrence(次にこのfingerprintが検知されたとき)での再試行を許可する。
    """
    _table().update_item(
        Key={"fingerprint": fingerprint},
        UpdateExpression=(
            "SET github_issue_create_status = :failed "
            "REMOVE github_issue_claimed_at, github_issue_claim_expires_at"
        ),
        ExpressionAttributeValues={
            ":failed": IncidentGithubIssueStatus.ISSUE_CREATION_FAILED.value
        },
    )


def mark_github_issue_configuration_error(fingerprint: str) -> None:
    """issue_creation_enabled=trueなのにGitHub認証情報が不備・取得失敗している状態。
    claimを解放し、次のoccurrenceでの再試行を許可する(週次改善レビューと同じ設計。
    設定不備が解消されない限り同じ失敗を繰り返すが、実害は小さい)。
    """
    _table().update_item(
        Key={"fingerprint": fingerprint},
        UpdateExpression=(
            "SET github_issue_create_status = :error "
            "REMOVE github_issue_claimed_at, github_issue_claim_expires_at"
        ),
        ExpressionAttributeValues={":error": IncidentGithubIssueStatus.CONFIGURATION_ERROR.value},
    )


def try_claim_new_github_comment(
    fingerprint: str, occurrence_count: int, now: dt.datetime, timeout_minutes: int
) -> bool:
    """当該occurrence_countについて、既存の(未失効・失効済み問わず)claimが一切
    無く、かつ既にこのoccurrenceへコメント済みでもない場合のみ成功する。既に
    claimがある場合(staleかどうかに関わらず)は失敗し、呼び出し側はstale判定
    (期限切れか)を自分で確認したうえで`try_reclaim_stale_github_comment`へ
    進む(reconciliation後にのみ)。
    """
    expires_iso = (now + dt.timedelta(minutes=timeout_minutes)).isoformat()
    try:
        _table().update_item(
            Key={"fingerprint": fingerprint},
            UpdateExpression=(
                "SET comment_claim_occurrence_count = :occ, comment_claim_expires_at = :expires"
            ),
            ConditionExpression=(
                "(attribute_not_exists(last_commented_occurrence_count) OR "
                "last_commented_occurrence_count <> :occ) AND "
                "(attribute_not_exists(comment_claim_occurrence_count) OR "
                "comment_claim_occurrence_count <> :occ)"
            ),
            ExpressionAttributeValues={
                ":occ": occurrence_count,
                ":expires": expires_iso,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def try_reclaim_stale_github_comment(
    fingerprint: str,
    occurrence_count: int,
    expected_claim_expires_at: str,
    now: dt.datetime,
    timeout_minutes: int,
) -> bool:
    """comment_claim_occurrence_count=occurrence_countかつ失効済みの場合のみ
    再claimする。呼び出し側は必ず先にGitHub側の実在コメント確認(reconciliation)を
    行うこと。
    """
    now_iso = now.isoformat()
    expires_iso = (now + dt.timedelta(minutes=timeout_minutes)).isoformat()
    try:
        _table().update_item(
            Key={"fingerprint": fingerprint},
            UpdateExpression="SET comment_claim_expires_at = :expires",
            ConditionExpression=(
                "comment_claim_occurrence_count = :occ AND "
                "comment_claim_expires_at = :expected AND comment_claim_expires_at < :now"
            ),
            ExpressionAttributeValues={
                ":occ": occurrence_count,
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


def mark_github_comment_posted(fingerprint: str, occurrence_count: int) -> None:
    _table().update_item(
        Key={"fingerprint": fingerprint},
        UpdateExpression=(
            "SET last_commented_occurrence_count = :occ "
            "REMOVE comment_claim_occurrence_count, comment_claim_expires_at"
        ),
        ExpressionAttributeValues={":occ": occurrence_count},
    )
