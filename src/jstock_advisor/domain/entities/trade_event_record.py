"""売買イベントの永続化契約(Issue #71 F-C11 Phase 1)。

TradeCooldownService._do_detect_and_apply()が検知したTradeEventを、
HoldingsSnapshotEntry更新より前に耐久性のある形で記録するためのエンティティ。
snapshot更新前にLambdaが異常終了した場合、次回実行のdetect_trade_events()が
同じイベントを再検知して復旧できるため、本レコード自体が無くても
イベントの取りこぼしは起きない。本レコードの役割は、その復旧に頼らず
「イベントが検知された」という事実自体を独立して残し、後続のconsumption
(WatchState終了。Issue #71 Phase 2)が取りこぼされたイベントを日をまたいでも
回収できるようにすることである。

event_idはholding_id(owner#stock_code)を平文で含まない(ハッシュ化する)。
レコード本体の属性としてはholding_id/stock_codeを平文で保持する
(既存のHoldingsSnapshotEntry等と同じ扱い。参照・snapshot更新に必要なため)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from hashlib import sha256

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import TransactionType

# sparse GSI(pending-marker-index)のHASHキー固定値。この属性を持つ項目のみが
# GSIに現れる(DynamoDBのsparse index特性)。consumption時にこの属性を削除すると
# GSIから自動的に消える(Phase 2で実装)。
PENDING_MARKER_VALUE = "PENDING"


class TradeEventRecord(Entity):
    # PK。build_trade_event_id()参照。holding_idを平文で含まない。
    event_id: str
    holding_id: str
    owner: str
    stock_code: str
    event_type: TransactionType
    detected_at: dt.date
    shares: int
    average_purchase_price: Decimal | None = None
    created_at: dt.datetime
    consumed_at: dt.datetime | None = None
    # sparse GSI(pending-marker-index)のHASHキー。CollectionStore.
    # query_by_index()はローカルJSON実装でもgetattr(item, key_name)で
    # 引くため(json_store.py参照)、DynamoDB専用の合成属性ではなく実際の
    # モデルfieldとして持つ(HoldingEvaluationRecord.holding_id/evaluated_atと
    # 同じ設計。TradeEventRecordRepository.create_pending()参照)。
    # 新規作成時は常にPENDING_MARKER_VALUE。Noneはconsumption後の状態
    # (Phase 2で使用。Phase 1では設定しない)。
    pending_marker: str | None = PENDING_MARKER_VALUE


def build_trade_event_id(holding_id: str, detected_at: dt.date) -> str:
    """(holding_id, detected_at)の組から決定的なevent_idを構成する。

    1回の_do_detect_and_apply()呼び出しでは、detect_trade_events()が
    holding_id単位で前回比較するため、同一holding_idのイベントは高々1件しか
    検知されない。したがって(holding_id, detected_at)の組が一意性の単位として
    十分であり、event_typeを追加でキーに含める必要はない。

    16文字(64bit相当)のハッシュ接頭辞を採用する: audit_id等の失敗ログ表示用
    ハッシュ接頭辞(8文字、record_failure_policy.pyのItemIdDisclosure.HASH既定)
    とは目的が異なる。あちらは「ログに出す量」の制御、こちらは「実際の一意性を
    担保する主キーそのもの」であるため、衝突耐性を優先しより長い接頭辞を採る。
    """
    holding_hash = sha256(holding_id.encode()).hexdigest()[:16]
    return f"{holding_hash}:{detected_at.isoformat()}"
