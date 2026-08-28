"""LINE通知dedupの原子的claim(Issue #17)。

read-before-write方式の既存dedup(NotificationLogを読む→判定→LINE push→
NotificationLog保存)は、読み取りと保存の間に他実行の同一判定が入り込むと
二重送信しうる。claimは「既存のread-based再送判定がSENDを許可した後の
"今回の送信決定"」を一意化するレコードであり、insert_if_absent(DynamoDBの
条件付きPut)で原子的に取得した1実行だけがLINE pushを行う。

設計上の不変条件(Issue #17承認済み設計):
- claim層は独自の再送判定を一切行わない。identityは既存判定が確定した入力
  (notification_type / scope / #23で確定したJST暦日・既存content_hash・
  event identity / 判定が読んだ直前NotificationLogのnotification_id)のみ
  から構築する。価格・スコア等をclaim層で再評価しない。
- statusはCLAIMED/SENTの2値のみ。push失敗はclaim削除(absent=再試行可能)で
  表現し、FAILED等の状態は増やさない。
- LINE pushという外部副作用はDynamoDBトランザクションに含められないため、
  本機構はexactly-onceではなくat-least-once + effectively once
  (「pushの実行中〜SENT記録前のcrash+stale takeover」および「LINE側受理済み
  だがclient側timeout→claim削除→再送」の窓では稀な二重送信を許容する。
  通知欠落よりも二重通知を安全側とする承認済み方針)。
- TTL(cleanup専用)はDynamoDB側のttl属性で管理する。TTLは送信可否・stale
  判定には絶対に使わない(DynamoDB TTL削除は最大48時間程度遅延しうるため。
  stale判定はclaimed_at比較のみで行う)。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import NotificationType


class NotificationClaimStatus(StrEnum):
    CLAIMED = "CLAIMED"  # 送信権を取得済み(push実行前〜SENT記録前)
    SENT = "SENT"  # LINE push成功を記録済み


class NotificationClaimMember(BaseModel):
    """NotificationLog repair seed(Issue #17)。

    claim取得時点でnotification_idを確定し、push成功後の通常保存と、
    「push成功→NotificationLog保存失敗→retry」時のrepair保存の両方が
    【同一のNotificationLogレコード】を書くようにする(notification_idが
    同じため、二重repairしてもupsertで冪等)。

    owner/holding_idはIssue #33で確立したscope転記(holding-scope再送判定が
    latest_by_holding_and_type()で過去実績を発見するための必須フィールド)を
    repair経由でも欠落させないために必ず保持する。
    """

    model_config = ConfigDict(extra="forbid")

    notification_id: str
    notification_type: NotificationType
    stock_code: str
    content_hash: str
    related_recommendation_id: str | None = None
    owner: str | None = None
    holding_id: str | None = None


class NotificationClaim(Entity):
    claim_id: str  # identity文字列のSHA-256 full 64桁hex(PK)
    identity: str  # pre-hashのidentity文字列(監査・デバッグ用)
    # 取得トークン(uuid4)。claim取得・takeoverのたびに更新する。CASの条件は
    # レコード生JSON全体の一致(#data = :expected_data)だが、claim_tokenが
    # 取得ごとにレコードのバイト列を必ず一意化するため、「takeover後に遅れて
    # 戻ってきた旧実行がSENT化/deleteできてしまう」競合が構造的に排除される。
    claim_token: str
    status: NotificationClaimStatus
    claimed_at: dt.datetime  # UTC instant。stale判定の唯一の根拠
    sent_at: dt.datetime | None = None  # UTC instant(SENT化時に設定)
    # 通知の評価時刻(呼び出し元のnow)。repairで保存するNotificationLogの
    # sent_atとして使い、元実行が保存するはずだったlogと同一内容にする。
    evaluated_at: dt.datetime
    notification_type: NotificationType
    scope: str  # holding_id / stock_code / pseudo_stock_code(表示・監査用)
    members: list[NotificationClaimMember]


def compute_claim_id(identity: str) -> str:
    """claim identity文字列からclaim_id(PK)を算出する。

    新設計のためSHA-256のfull 64桁hexを使う(既存content_hash等の16桁仕様は
    変更しない。切り詰めによる衝突リスクを新規に持ち込まないため)。
    """
    return hashlib.sha256(identity.encode()).hexdigest()
