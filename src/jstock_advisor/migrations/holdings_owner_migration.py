"""保有銘柄オーナー機能移行(M2)本体。

必ずpreflight(holdings_owner_preflight.run_preflight())が独立したコマンドで
事前に実行・人間によって確認された後、別操作として実行すること
(承認済み設計。preflightとmigration本体を1コマンドで連続実行しない)。

本体はさらに、実行そのものの直前にも独立してpreflightを再検証し
(fail-closed)、TradingPauseConfig.pause_buy_sellがtrueであることも
このコード自身が確認する(CLI運用手順だけに依存しない。取得失敗・未初期化・
false のいずれの場合もfail-closedで中止する)。

冪等性: holding_idはowner×stock_codeから決定的に導出されるため、同じ旧
データに対して何度実行しても同じholding_id・同じ内容のレコードが生成される
(重複しない)。書き込みは常に単純な上書き(put_item/upsert)であり、
sequenceのADD増加・pointer_versionの条件付き増加といった「実行そのものが
カウントを進める」操作は一切行わない(sequence/pointerは読み取った値を
そのままコピーするのみ)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from jstock_advisor.domain.entities.decision_snapshot import DecisionSnapshot
from jstock_advisor.domain.entities.holding_decision import (
    HoldingDecisionResult,
    InvestmentThesis,
    InvestmentThesisBaseline,
)
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.owner import build_holding_id, normalize_and_validate_owner
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.transaction import Transaction
from jstock_advisor.infrastructure.collection_store import build_collection_store
from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore
from jstock_advisor.migrations.baseline_migration import (
    migrated_holding_id_for_stock_code,
    read_all_legacy_pointers,
    read_all_legacy_sequences,
    write_pointer_v2,
    write_sequence_v2,
)
from jstock_advisor.migrations.conversions import (
    DEFAULT_MIGRATION_OWNER,
    convert_holding,
    convert_holdings_snapshot_entry,
    convert_purchase_lot,
    recommendation_scope_for_migration,
)
from jstock_advisor.migrations.holdings_owner_preflight import PreflightReport, run_preflight
from jstock_advisor.migrations.legacy_shapes import (
    LegacyHoldingsSnapshotEntryV1,
    LegacyHoldingV1,
    LegacyPurchaseLotV1,
)
from jstock_advisor.migrations.target import MigrationTarget, target_backend
from jstock_advisor.migrations.v2_entities import (
    HoldingsSnapshotEntryV2,
    HoldingV2,
    PurchaseLotV2,
)


class MigrationAbortedError(Exception):
    """migrationがfail-closedで中止された(pause未確認・preflight不合格等)。"""


@dataclass(frozen=True)
class MigrationResult:
    dry_run: bool
    preflight: PreflightReport
    counts_written: dict[str, int]

    def render_text(self) -> str:
        lines = [
            f"migration結果: {'DRY-RUN(書き込みなし)' if self.dry_run else '実行完了'}",
            "",
            "[書き込み(予定)件数]",
        ]
        for key, value in self.counts_written.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)


def _ensure_trading_paused(target: MigrationTarget, store_dir: Path | None) -> None:
    """TradingPauseConfig.pause_buy_sell==trueであることをこのコード自身が
    確認する(CLIの運用手順だけに依存しない)。取得失敗・未初期化・false の
    いずれもfail-closedで中止する。"""
    from jstock_advisor.infrastructure.aws import trading_pause_config

    with target_backend(target):
        try:
            config = trading_pause_config.get(store_dir)
        except Exception as e:
            raise MigrationAbortedError(
                f"TradingPauseConfigの取得に失敗しました(fail-closedで中止): {e}"
            ) from e
    if config is None:
        raise MigrationAbortedError(
            "TradingPauseConfigが未初期化です(fail-closedで中止)。"
            "先にtrading-pause initおよびsetでpause_buy_sell=trueにしてください。"
        )
    if not config.pause_buy_sell:
        raise MigrationAbortedError(
            "pause_buy_sell=falseのため中止しました(fail-closed)。"
            "先にtrading-pause set --buy-sellでBUY/SELLを一時停止してください。"
        )


def _migrate_holdings(store_dir: Path | None, owner: str, dry_run: bool) -> int:
    old_store = build_collection_store(LegacyHoldingV1, "holdings.json", "stock_code", store_dir)
    new_store = build_collection_store(HoldingV2, "holdings_v2.json", "holding_id", store_dir)
    count = 0
    for legacy in old_store.list_all():
        v2 = convert_holding(legacy, owner)
        if not dry_run:
            new_store.upsert(v2)
        count += 1
    return count


def _migrate_purchase_lots(
    target: MigrationTarget, store_dir: Path | None, owner: str, dry_run: bool
) -> int:
    """PurchaseLotsTableは新テーブルを作らず、既存の"purchase_lots.json"を
    そのままowner/holding_id付きの形状へ上書きする。

    target=localの場合、JsonCollectionStore.upsert()は書き込み前に対象
    ファイル全体を「書き込み先のモデル型(PurchaseLotV2)」で読み直すため、
    旧形状(owner/holding_idを持たない)のレコードがまだ1件でも残っている
    間はupsert()自体がバリデーションエラーで失敗する(全レコードをまとめて
    変換した後に初めて書き込む必要がある)。そのため、変換済みの全件を集めて
    から1回のアトミック書き込み(_write_all、production同梱の安全な
    tempfile+os.replaceパターンをそのまま再利用)で反映する。target=aws
    (DynamoDB)は項目ごとに独立して読み書きできるため、この制約が無く、
    従来どおり1件ずつupsertする。
    """
    old_store = build_collection_store(
        LegacyPurchaseLotV1, "purchase_lots.json", "lot_id", store_dir
    )
    converted = [convert_purchase_lot(legacy, owner) for legacy in old_store.list_all()]
    if not dry_run:
        new_store = build_collection_store(
            PurchaseLotV2, "purchase_lots.json", "lot_id", store_dir
        )
        if target is MigrationTarget.LOCAL:
            assert isinstance(new_store, JsonCollectionStore)
            new_store._write_all({lot.lot_id: lot for lot in converted})  # noqa: SLF001
        else:
            for lot in converted:
                new_store.upsert(lot)
    return len(converted)


def _migrate_holdings_snapshot(
    store_dir: Path | None, owner: str, dry_run: bool, file_name: str, v2_file_name: str
) -> int:
    old_store = build_collection_store(
        LegacyHoldingsSnapshotEntryV1, file_name, "stock_code", store_dir
    )
    new_store = build_collection_store(
        HoldingsSnapshotEntryV2, v2_file_name, "holding_id", store_dir
    )
    count = 0
    for legacy in old_store.list_all():
        v2 = convert_holdings_snapshot_entry(legacy, owner)
        if not dry_run:
            new_store.upsert(v2)
        count += 1
    return count


def _migrate_recommendations(store_dir: Path | None, owner: str, dry_run: bool) -> int:
    """保有系Recommendationのみowner/holding_idをバックフィルする。BUY系は
    stock-scopeのままowner/holding_id=Noneで維持し、書き込み自体を行わない。"""
    store = build_collection_store(
        Recommendation, "recommendations.json", "recommendation_id", store_dir
    )
    count = 0
    for rec in store.list_all():
        new_owner, new_holding_id = recommendation_scope_for_migration(rec, owner)
        if new_holding_id is None:
            continue
        updated = rec.model_copy(update={"owner": new_owner, "holding_id": new_holding_id})
        if not dry_run:
            store.upsert(updated)
        count += 1
    return count


def _migrate_notification_logs(
    store_dir: Path | None,
    owner: str,
    dry_run: bool,
    accepted_unresolved_notification_ids: frozenset[str],
) -> int:
    """related_recommendation_idが指すRecommendationのscopeを引き継ぐ。scope
    はRecommendationストアへの書き込み結果を読み直すのではなく、元の
    Recommendationオブジェクトから直接再計算する(dry-run時も実行順序に
    依存せず同じ結果になるようにするため)。"""
    log_store = build_collection_store(
        NotificationLog, "notification_log.json", "notification_id", store_dir
    )
    rec_store = build_collection_store(
        Recommendation, "recommendations.json", "recommendation_id", store_dir
    )
    recommendations_by_id = {r.recommendation_id: r for r in rec_store.list_all()}
    count = 0
    for log in log_store.list_all():
        if log.related_recommendation_id is None:
            continue
        if log.notification_id in accepted_unresolved_notification_ids:
            continue
        rec = recommendations_by_id.get(log.related_recommendation_id)
        if rec is None:
            continue
        new_owner, new_holding_id = recommendation_scope_for_migration(rec, owner)
        updated = log.model_copy(update={"owner": new_owner, "holding_id": new_holding_id})
        if not dry_run:
            log_store.upsert(updated)
        count += 1
    return count


def _migrate_decision_snapshots(store_dir: Path | None, owner: str, dry_run: bool) -> int:
    snapshot_store = build_collection_store(
        DecisionSnapshot, "decision_snapshots.json", "decision_id", store_dir
    )
    rec_store = build_collection_store(
        Recommendation, "recommendations.json", "recommendation_id", store_dir
    )
    recommendations_by_id = {r.recommendation_id: r for r in rec_store.list_all()}
    count = 0
    for snapshot in snapshot_store.list_all():
        if snapshot.recommendation_id is None:
            continue
        rec = recommendations_by_id.get(snapshot.recommendation_id)
        if rec is None:
            continue
        new_owner, new_holding_id = recommendation_scope_for_migration(rec, owner)
        updated = snapshot.model_copy(update={"owner": new_owner, "holding_id": new_holding_id})
        if not dry_run:
            snapshot_store.upsert(updated)
        count += 1
    return count


def _migrate_transactions(store_dir: Path | None, owner: str, dry_run: bool) -> int:
    """Transactionは取引そのものであり常にholding-scope。全件バックフィルする。"""
    store = build_collection_store(Transaction, "transactions.json", "transaction_id", store_dir)
    normalized_owner = normalize_and_validate_owner(owner)
    count = 0
    for transaction in store.list_all():
        holding_id = build_holding_id(normalized_owner, transaction.stock_code)
        updated = transaction.model_copy(
            update={"owner": normalized_owner, "holding_id": holding_id}
        )
        if not dry_run:
            store.upsert(updated)
        count += 1
    return count


def _migrate_holding_id_field_only[T: BaseModel](
    model_type: type[T],
    file_name: str,
    id_field: str,
    store_dir: Path | None,
    owner: str,
    dry_run: bool,
) -> int:
    """holding_id値のみを新形式へ移行する(HoldingDecisionResult/InvestmentThesis/
    InvestmentThesisBaseline共通、いずれも既にholding_id: strフィールドを持つ)。
    現在holding_idはstock_codeの1:1エイリアスのため、旧holding_id(=stock_code)
    からowner付き新holding_idを導出する。baseline_id等、他の識別子は一切
    変更しない(不変スナップショットの識別子を勝手に再採番しない)。"""
    store = build_collection_store(model_type, file_name, id_field, store_dir)
    count = 0
    for item in store.list_all():
        old_holding_id = getattr(item, "holding_id")  # noqa: B009
        new_holding_id = migrated_holding_id_for_stock_code(old_holding_id, owner)
        updated = item.model_copy(update={"holding_id": new_holding_id})
        if not dry_run:
            store.upsert(updated)
        count += 1
    return count


def _migrate_baseline_sequences(
    target: MigrationTarget, store_dir: Path | None, owner: str, dry_run: bool
) -> int:
    count = 0
    for entry in read_all_legacy_sequences(target, store_dir):
        new_holding_id = migrated_holding_id_for_stock_code(entry.holding_id, owner)
        if not dry_run:
            write_sequence_v2(entry, new_holding_id, target, store_dir)
        count += 1
    return count


def _migrate_baseline_pointers(
    target: MigrationTarget, store_dir: Path | None, owner: str, dry_run: bool
) -> int:
    count = 0
    for pointer in read_all_legacy_pointers(target, store_dir):
        new_holding_id = migrated_holding_id_for_stock_code(pointer.holding_id, owner)
        if not dry_run:
            write_pointer_v2(pointer, new_holding_id, store_dir)
        count += 1
    return count


def run_migration(
    target: MigrationTarget,
    dry_run: bool,
    store_dir: Path | None = None,
    owner: str = DEFAULT_MIGRATION_OWNER,
    accepted_unresolved_notification_ids: frozenset[str] = frozenset(),
) -> MigrationResult:
    normalized_owner = normalize_and_validate_owner(owner)

    _ensure_trading_paused(target, store_dir)

    report = run_preflight(target, store_dir, accepted_unresolved_notification_ids)
    if not report.passed:
        raise MigrationAbortedError(
            "preflightに失敗しているためmigrationを中止しました(fail-closed):\n"
            + report.render_text()
        )

    counts_written: dict[str, int] = {
        "holdings": _migrate_holdings(store_dir, normalized_owner, dry_run),
        "purchase_lots": _migrate_purchase_lots(target, store_dir, normalized_owner, dry_run),
        "holdings_snapshots": _migrate_holdings_snapshot(
            store_dir,
            normalized_owner,
            dry_run,
            "holdings_snapshots.json",
            "holdings_snapshots_v2.json",
        ),
        "validation_holdings_snapshots": _migrate_holdings_snapshot(
            store_dir,
            normalized_owner,
            dry_run,
            "validation_holdings_snapshots.json",
            "validation_holdings_snapshots_v2.json",
        ),
        "recommendations": _migrate_recommendations(store_dir, normalized_owner, dry_run),
        "notification_logs": _migrate_notification_logs(
            store_dir, normalized_owner, dry_run, accepted_unresolved_notification_ids
        ),
        "decision_snapshots": _migrate_decision_snapshots(store_dir, normalized_owner, dry_run),
        "transactions": _migrate_transactions(store_dir, normalized_owner, dry_run),
        "holding_decision_results": _migrate_holding_id_field_only(
            HoldingDecisionResult,
            "holding_decision_results.json",
            "holding_decision_result_id",
            store_dir,
            normalized_owner,
            dry_run,
        ),
        "investment_theses": _migrate_holding_id_field_only(
            InvestmentThesis,
            "investment_theses.json",
            "investment_thesis_id",
            store_dir,
            normalized_owner,
            dry_run,
        ),
        "investment_thesis_baselines": _migrate_holding_id_field_only(
            InvestmentThesisBaseline,
            "investment_thesis_baselines.json",
            "baseline_id",
            store_dir,
            normalized_owner,
            dry_run,
        ),
        "baseline_sequences": _migrate_baseline_sequences(
            target, store_dir, normalized_owner, dry_run
        ),
        "baseline_pointers": _migrate_baseline_pointers(
            target, store_dir, normalized_owner, dry_run
        ),
    }

    return MigrationResult(dry_run=dry_run, preflight=report, counts_written=counts_written)
