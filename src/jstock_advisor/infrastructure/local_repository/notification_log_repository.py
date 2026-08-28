"""LINE通知履歴のローカルリポジトリ(要求仕様10節・16節)。同一内容の重複通知防止に使用する。

Issue #32(NotificationLogの読み取りコスト構造改善): save時にDynamoDBの
トップレベルへGSI用のindex属性とTTL属性を付与する(Phase A: dual-write)。
既存の`data` JSON(モデル本体)の形式・内容は一切変更しない。読み取り側の
GSI Query化はGSI作成・既存itemのbackfill・移行検証の完了後に別フェーズで行う
(それまでは従来どおりのScan読み取り。docs/operations_manual.md 15節参照)。

キー生成はこのモジュールのpure関数へ一本化し、通常save・backfillスクリプト
(scripts/backfill_notification_log_index_attributes.py)・移行検証のすべてが
同一ロジックを共有する(生成ロジックのdrift防止)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.domain.entities.enums import NotificationType
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

# --- Issue #32: GSI/TTL用トップレベル属性(DynamoDBのみ。data JSONには含まれない) ---
# 属性名・index名はPhase C/D(template.yamlへのGSI追加・Query切替)でも同じ定数を
# 参照すること。
STOCK_SCOPE_KEY_ATTRIBUTE = "nl_stock_type_key"
HOLDING_SCOPE_KEY_ATTRIBUTE = "nl_holding_type_key"
SENT_SORT_ATTRIBUTE = "nl_sent_sort"
EXPIRES_AT_ATTRIBUTE = "nl_expires_at"
STOCK_SCOPE_INDEX_NAME = "nl_stock_type_key-index"
HOLDING_SCOPE_INDEX_NAME = "nl_holding_type_key-index"

# 保持期間(cleanup専用TTL。業務ロジックは削除時刻に依存しない)。730日の根拠は
# Issue #32設計報告(再送判定・評価ホライズン最大250営業日・backtest replay・
# 監査をすべて包含する2年)。
NOTIFICATION_LOG_RETENTION_DAYS = 730

# 同一局面の再送抑止が局面変化まで無期限に必要なため、TTL失効による稀な再通知を
# 避ける目的でPROFIT_PROTECTION_ATTENTIONのみTTL対象外とする(件数は極小)。
_TTL_EXEMPT_TYPES = frozenset({NotificationType.PROFIT_PROTECTION_ATTENTION})


def _sent_at_as_utc(sent_at: dt.datetime) -> dt.datetime:
    """sent_atをtimezone-aware UTCへ正規化する。

    書き込み経路(line_notification_service.py)は常にdt.datetime.now(dt.UTC)を
    渡すためaware UTCが正規形。naiveなsent_at(想定外の旧データ・テストデータ)は
    ローカルタイムゾーンとして暗黙解釈せず、UTCとみなす(保存値は歴史的に
    UTC基準のため。ここでローカルTZを混入させると生成キーが環境依存になる)。
    """
    if sent_at.tzinfo is None:
        return sent_at.replace(tzinfo=dt.UTC)
    return sent_at.astimezone(dt.UTC)


def build_sent_sort_value(sent_at: dt.datetime, notification_id: str) -> str:
    """GSIのRANGEキー: 固定幅ISO8601(UTC)+"#"+notification_id。

    時刻部を固定幅(マイクロ秒6桁ゼロ埋め、末尾"Z")にすることで辞書順=時刻順を
    保証し、同一sent_atのitemはnotification_idのtie-breakで完全順序が決まる
    (Query ScanIndexForward=False, Limit=1の「latest」を決定的にするため。
    notification_idの辞書順自体に業務的意味はない)。
    """
    ts = _sent_at_as_utc(sent_at)
    return f"{ts.strftime('%Y-%m-%dT%H:%M:%S')}.{ts.microsecond:06d}Z#{notification_id}"


def build_stock_scope_key(stock_code: str, notification_type: NotificationType) -> str:
    """GSI-1のHASHキー。現行のlatest_by_stock_and_type()と同じsemantics
    (stock+type一致ならholding-scope logもマッチする)を保つため、全item
    (stock_codeを持つもの)へ付与する。pseudo stock code("__batch__:*")も
    そのまま使う。notification_typeは"#"を含まないenum固定値のため、合成キーの
    最終"#"以降が常にtypeとなり、異なる(stock_code, type)組が同一キーになる
    ことはない。"""
    return f"S#{stock_code}#{notification_type}"


def build_holding_scope_key(holding_id: str, notification_type: NotificationType) -> str:
    """GSI-2のHASHキー。holding_idはbuild_holding_id()(owner.py)により
    owner + "#" + stock_codeとしてownerを構造的に内包するため、このキーだけで
    owner横断の一意なscopeになる(Issue #33のcross-owner dedup分離を維持)。"""
    return f"H#{holding_id}#{notification_type}"


def build_expires_at_epoch(sent_at: dt.datetime) -> int:
    """TTL属性値: sent_at(UTC正規化)+ 保持期間、をepoch秒の整数で返す。"""
    expires = _sent_at_as_utc(sent_at) + dt.timedelta(days=NOTIFICATION_LOG_RETENTION_DAYS)
    return int(expires.timestamp())


def build_index_attributes(log: NotificationLog) -> dict[str, str | int]:
    """NotificationLogから決定的にトップレベルindex/TTL属性を生成する(pure)。

    通常save・backfill・移行検証が必ずこの関数を共有すること。
    - nl_sent_sort: 全itemへ付与
    - nl_stock_type_key: stock_codeを持つitemのみ(sparse GSI)
    - nl_holding_type_key: holding_idを持つitemのみ(sparse GSI)
    - nl_expires_at: PROFIT_PROTECTION_ATTENTION以外のみ(TTL対象外の理由は
      _TTL_EXEMPT_TYPESのコメント参照)
    """
    attributes: dict[str, str | int] = {
        SENT_SORT_ATTRIBUTE: build_sent_sort_value(log.sent_at, log.notification_id),
    }
    if log.stock_code is not None:
        attributes[STOCK_SCOPE_KEY_ATTRIBUTE] = build_stock_scope_key(
            log.stock_code, log.notification_type
        )
    if log.holding_id is not None:
        attributes[HOLDING_SCOPE_KEY_ATTRIBUTE] = build_holding_scope_key(
            log.holding_id, log.notification_type
        )
    if log.notification_type not in _TTL_EXEMPT_TYPES:
        attributes[EXPIRES_AT_ATTRIBUTE] = build_expires_at_epoch(log.sent_at)
    return attributes


class NotificationLogRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[NotificationLog] = build_collection_store(
            NotificationLog, "notification_log.json", "notification_id", store_dir
        )

    def list_all(self) -> list[NotificationLog]:
        return self._store.list_all()

    def get(self, notification_id: str) -> NotificationLog | None:
        """notification_id単キーでの取得(Issue #17: claim repairが「対応する
        NotificationLogが既に保存済みか」を確認するために使う)。"""
        return self._store.get(notification_id)

    def list_by_stock_and_type(
        self, stock_code: str, notification_type: NotificationType
    ) -> list[NotificationLog]:
        items = self._store.find(
            lambda n: n.stock_code == stock_code and n.notification_type == notification_type
        )
        return sorted(items, key=lambda n: n.sent_at)

    def latest_by_stock_and_type(
        self, stock_code: str, notification_type: NotificationType
    ) -> NotificationLog | None:
        items = self.list_by_stock_and_type(stock_code, notification_type)
        return items[-1] if items else None

    def list_by_holding_and_type(
        self, holding_id: str, notification_type: NotificationType
    ) -> list[NotificationLog]:
        """M3(保有銘柄オーナー機能): holding-scope通知(SELL/PARTIAL/ATTENTION等)の
        再送判定用。同一stock_codeでも別ownerのholding_idとは互いに影響しない。"""
        items = self._store.find(
            lambda n: n.holding_id == holding_id and n.notification_type == notification_type
        )
        return sorted(items, key=lambda n: n.sent_at)

    def latest_by_holding_and_type(
        self, holding_id: str, notification_type: NotificationType
    ) -> NotificationLog | None:
        items = self.list_by_holding_and_type(holding_id, notification_type)
        return items[-1] if items else None

    def list_by_recommendation_id(self, recommendation_id: str) -> list[NotificationLog]:
        """backtest/compareのhistory replayが「実際にLINE送信が成功したか」を
        判定するために使う(コードレビュー対応)。複数件ある場合は重複送信の
        可能性があるため、呼び出し側で件数を確認すること。"""
        items = self._store.find(lambda n: n.related_recommendation_id == recommendation_id)
        return sorted(items, key=lambda n: n.sent_at)

    def save(self, log: NotificationLog) -> None:
        # Issue #32 Phase A: DynamoDBではGSI/TTL用トップレベル属性をdual-writeする
        # (data JSON本体は不変)。ローカルJSON実装はindex_attributesを無視するため
        # 従来のupsertと同一動作。
        self._store.upsert_with_index_attributes(log, build_index_attributes(log))
