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
from dataclasses import dataclass
from pathlib import Path

from jstock_advisor.domain.entities.enums import NotificationType
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.infrastructure.record_failure_policy import (
    DecodeOutcome,
    ItemIdDisclosure,
    RecordFailurePolicy,
)

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


@dataclass(frozen=True)
class NotificationLookup:
    """再送判定のための読み取り結果(Issue #279)。

    `list[NotificationLog]` を返すだけでは、**読めなかったレコードがあった事実**が
    呼び出し側へ届かない。過去の送信実績を1件でも見落とすと**重複送信**になるため、
    読めた履歴だけでなく「読めなかったか」を一緒に返す。

        records      decodeできた履歴(sent_at昇順)
        undecidable  ★ True なら**送信可否を判断できない**。呼び出し側は送信を見送る
        skipped      decodeできなかった件数(0でも記録する。黙って減らさないため)

    ★ `undecidable` と `skipped` は別物である。
      FAIL_SAFE_SUPPRESS を宣言した collection でのみ `undecidable` が立つ。
      LENIENT の collection では skip されても判断を続けてよい。
    """

    records: list[NotificationLog]
    undecidable: bool
    skipped: int

    @property
    def latest(self) -> NotificationLog | None:
        """直近の1件。`records` が空なら None。

        ★ `undecidable` が True のときに `latest` が None でも、
          それは「送っていない」ではなく「**分からない**」である。
          呼び出し側は `latest` より先に `undecidable` を見ること。
        """
        return self.records[-1] if self.records else None

    @classmethod
    def from_outcome(cls, outcome: DecodeOutcome[NotificationLog]) -> NotificationLookup:
        return cls(
            records=sorted(outcome.records, key=lambda n: n.sent_at),
            undecidable=outcome.undecidable,
            skipped=outcome.failure_count,
        )


class NotificationLogRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        # Issue #279(#63 A-U4): notification_log は再送抑止の判定材料である。
        # skipすると過去の送信実績を見落として**重複送信**になり、
        # 例外にすると**通知が出せなくなる**。どちらも困るため
        # FAIL_SAFE_SUPPRESS を宣言し、「判定できなかった」ことを
        # NotificationLookup 経由で呼び出し側へ伝えて送信を見送らせる。
        #
        # item_idはPLAIN。主キーは notification_id(UUID)であり、
        # 所有者名・銘柄コードを含まない(#135 Phase Aが実測した
        # 6 collectionと重ならない)。平文で出すことで、隔離された
        # レコードを運用で特定できる。
        self._store: CollectionStore[NotificationLog] = build_collection_store(
            NotificationLog,
            "notification_log.json",
            "notification_id",
            store_dir,
            failure_policy=RecordFailurePolicy.FAIL_SAFE_SUPPRESS,
            item_id_disclosure=ItemIdDisclosure.PLAIN,
        )

    def list_all(self) -> list[NotificationLog]:
        return self._store.list_all()

    def get(self, notification_id: str) -> NotificationLog | None:
        """notification_id単キーでの取得(Issue #17: claim repairが「対応する
        NotificationLogが既に保存済みか」を確認するために使う)。"""
        return self._store.get(notification_id)

    def list_by_stock_and_type(
        self, stock_code: str, notification_type: NotificationType
    ) -> NotificationLookup:
        """stock-scope通知の再送判定用(Issue #279で戻り値を NotificationLookup へ変更)。

        ★ 戻り値の `undecidable` を無視すると、壊れたレコードがあるときに
          「送信実績なし」と誤読して**重複送信**する。必ず先に見ること。
        """
        return NotificationLookup.from_outcome(
            self._store.find_with_outcome(
                lambda n: n.stock_code == stock_code and n.notification_type == notification_type
            )
        )

    def latest_by_stock_and_type(
        self, stock_code: str, notification_type: NotificationType
    ) -> NotificationLookup:
        """直近1件を含む再送判定用の結果(Issue #279)。

        ★ 以前は `NotificationLog | None` を返していた。None が
          「送っていない」と「読めなかった」の**両方**を意味してしまい、
          FAIL_SAFE_SUPPRESS の目的を達成できないため型を変えた。
          直近の1件は `.latest` で取得する。
        """
        return self.list_by_stock_and_type(stock_code, notification_type)

    def list_by_holding_and_type(
        self, holding_id: str, notification_type: NotificationType
    ) -> NotificationLookup:
        """M3(保有銘柄オーナー機能): holding-scope通知(SELL/PARTIAL/ATTENTION等)の
        再送判定用。同一stock_codeでも別ownerのholding_idとは互いに影響しない。

        Issue #279で戻り値を NotificationLookup へ変更した(理由は
        `list_by_stock_and_type` と同じ)。
        """
        return NotificationLookup.from_outcome(
            self._store.find_with_outcome(
                lambda n: n.holding_id == holding_id and n.notification_type == notification_type
            )
        )

    def latest_by_holding_and_type(
        self, holding_id: str, notification_type: NotificationType
    ) -> NotificationLookup:
        """直近1件を含む再送判定用の結果(Issue #279)。直近は `.latest`。"""
        return self.list_by_holding_and_type(holding_id, notification_type)

    def list_by_recommendation_id(self, recommendation_id: str) -> NotificationLookup:
        """backtest/compareのhistory replayが「実際にLINE送信が成功したか」を
        判定するために使う(コードレビュー対応)。複数件ある場合は重複送信の
        可能性があるため、呼び出し側で件数を確認すること。

        ★ この経路は**利用者への送信判断に使わない**(過去の分析・集計)。
          読めなかった1件のために分析全体を止める必要はないため、
          `undecidable` で抑止せず `skipped` を添えて返す(Issue #279 の経路4)。
          呼び出し側は件数の欠落を注記できる。
        """
        return NotificationLookup.from_outcome(
            self._store.find_with_outcome(
                lambda n: n.related_recommendation_id == recommendation_id
            )
        )

    def list_all_with_outcome(self) -> NotificationLookup:
        """`list_all()` と同じ全件に、decodeの成否を添えて返す(Issue #279 の経路5)。

        ★ `list_all()` は **signature を変えていない**。CLI・集計・テストからの
          呼び出しが多数あり、そのすべてを壊す必要が無いためである
          (この経路も送信判断には使わないため、抑止の対象ではない)。
          skip件数を知りたい呼び出し側だけが本メソッドを使う。
        """
        return NotificationLookup.from_outcome(self._store.find_with_outcome(lambda _: True))

    def save(self, log: NotificationLog) -> None:
        # Issue #32 Phase A: DynamoDBではGSI/TTL用トップレベル属性をdual-writeする
        # (data JSON本体は不変)。ローカルJSON実装はindex_attributesを無視するため
        # 従来のupsertと同一動作。
        self._store.upsert_with_index_attributes(log, build_index_attributes(log))
