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

## 予約フィールド(#503では書かない)

`github_issue_number` / `github_issue_create_status` / `resolved_at` / `notification_status` は、
後続の #508(GitHub Issue接続)がこのテーブルを再利用できるように、baseline 設計
(USER決定。#503 issuecomment-5796588796)が予約したフィールドである。**本 module はこれらを
一切読み書きしない**(#508 の責務)。

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


def _table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))


def get_incident_state(fingerprint: str) -> dict[str, Any] | None:
    response = _table().get_item(Key={"fingerprint": fingerprint})
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
            UpdateExpression="SET claimed_at = :ancient",
            ConditionExpression="claim_token = :token",
            ExpressionAttributeValues={":ancient": ancient_iso, ":token": claim_token},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] not in _CONDITION_FAILURE_CODES:
            raise
