"""保有銘柄オーナー機能移行(M2)のpreflight検証。

migration本体(holdings_owner_migration.py)は、このモジュールが返す
PreflightReport.passed が True である場合のみ実行を許可する(fail-closed)。
preflightとmigration本体は必ず別コマンドとして実行し、1コマンドで連続実行
しない(承認済み設計)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jstock_advisor.domain.entities.decision_snapshot import DecisionSnapshot
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.collection_store import (
    build_collection_store,
    resolve_table_name,
)
from jstock_advisor.migrations.baseline_migration import (
    read_all_legacy_pointers,
    read_all_legacy_sequences,
)
from jstock_advisor.migrations.legacy_shapes import (
    LegacyHoldingsSnapshotEntryV1,
    LegacyHoldingV1,
    LegacyPurchaseLotV1,
)
from jstock_advisor.migrations.target import MigrationTarget, target_backend

# RecommendationTypeの発生元による分類(実コード確認済み、v4プラン承認事項)。
# BUY系(BuySignalService由来、stock-scope): shares_at_recommendationは常にNone。
# 保有系(SellSignalService/ProfitTakingService/HoldingDecisionService由来、
# holding-scope): shares_at_recommendationは常に設定される。
# 全RecommendationTypeメンバーを網羅すること(未分類のメンバーが1つでもあれば
# 実装上のバグであり、preflightが正しく機能しない)。
BUY_FAMILY_RECOMMENDATION_TYPES: frozenset[RecommendationType] = frozenset(
    {
        RecommendationType.BUY,
        RecommendationType.WATCH_BUY,
    }
)
HOLDING_FAMILY_RECOMMENDATION_TYPES: frozenset[RecommendationType] = frozenset(
    {
        RecommendationType.HOLD,
        RecommendationType.WATCH,
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.FULL_PROFIT_TAKE,
        RecommendationType.SELL,
        RecommendationType.URGENT_REVIEW,
        RecommendationType.WATCH_BEFORE_EARNINGS,
        RecommendationType.PARTIAL_RISK_REDUCTION,
        RecommendationType.REVIEW_AFTER_EARNINGS,
        RecommendationType.REVIEW,
        RecommendationType.MANUAL_REVIEW_REQUIRED,
        RecommendationType.REVIEW_BEFORE_EARNINGS,
        # 以下4種は承認済みプランの列挙には無かったが、実コード確認の結果、
        # いずれもholding_decision_notification_builder.py(常にHoldingから
        # shares_at_recommendationを設定する唯一の生成元)からのみ発行される
        # ことを確認したため、保有系へ追加した(buy_signal_service.py等
        # BUY候補パイプラインからは一切発行されない)。
        RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
        RecommendationType.SELL_CONSIDERATION,
        RecommendationType.STRONG_SELL_CONSIDERATION,
        RecommendationType.URGENT_HOLDING_REVIEW,
    }
)

_ALL_CLASSIFIED_TYPES = BUY_FAMILY_RECOMMENDATION_TYPES | HOLDING_FAMILY_RECOMMENDATION_TYPES
_UNCLASSIFIED_TYPES = frozenset(RecommendationType) - _ALL_CLASSIFIED_TYPES
if _UNCLASSIFIED_TYPES:
    raise AssertionError(
        f"RecommendationTypeに未分類のメンバーがあります(preflightのバグ): {_UNCLASSIFIED_TYPES}"
    )


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    detail: str
    offending: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]
    counts: dict[str, int]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def render_text(self) -> str:
        lines = [f"Preflight結果: {'PASS' if self.passed else 'FAIL'}", "", "[対象件数]"]
        for key, value in self.counts.items():
            lines.append(f"  {key}: {value}")
        lines.append("")
        lines.append("[チェック結果]")
        for check in self.checks:
            status = "OK" if check.passed else "NG"
            lines.append(f"  [{status}] {check.name}: {check.detail}")
            for offense in check.offending[:20]:
                lines.append(f"      - {offense}")
            if len(check.offending) > 20:
                lines.append(f"      ...他{len(check.offending) - 20}件")
        return "\n".join(lines)


def _check_recommendation_scope_consistency(
    recommendations: list[Recommendation],
) -> PreflightCheck:
    offending: list[dict[str, Any]] = []
    for rec in recommendations:
        is_holding_shape = rec.shares_at_recommendation is not None
        if rec.recommendation_type in BUY_FAMILY_RECOMMENDATION_TYPES and is_holding_shape:
            offending.append(
                {
                    "recommendation_id": rec.recommendation_id,
                    "stock_code": rec.stock_code,
                    "recommendation_type": rec.recommendation_type.value,
                    "shares_at_recommendation": rec.shares_at_recommendation,
                    "reason": "BUY系なのにshares_at_recommendationが設定されている",
                }
            )
        elif (
            rec.recommendation_type in HOLDING_FAMILY_RECOMMENDATION_TYPES
            and not is_holding_shape
        ):
            offending.append(
                {
                    "recommendation_id": rec.recommendation_id,
                    "stock_code": rec.stock_code,
                    "recommendation_type": rec.recommendation_type.value,
                    "shares_at_recommendation": rec.shares_at_recommendation,
                    "reason": "保有系なのにshares_at_recommendationが未設定",
                }
            )
    return PreflightCheck(
        name="recommendation_scope_consistency",
        passed=not offending,
        detail=(
            f"RecommendationType×shares_at_recommendationの交差検証: "
            f"{len(recommendations)}件中{len(offending)}件が期待外の組み合わせ"
        ),
        offending=tuple(offending),
    )


def _check_notification_log_reference_integrity(
    notification_logs: list[NotificationLog],
    recommendations_by_id: dict[str, Recommendation],
    accepted_unresolved_notification_ids: frozenset[str],
) -> PreflightCheck:
    offending: list[dict[str, Any]] = []
    for log in notification_logs:
        if log.related_recommendation_id is None:
            continue
        if log.related_recommendation_id in recommendations_by_id:
            continue
        if log.notification_id in accepted_unresolved_notification_ids:
            continue
        offending.append(
            {
                "notification_id": log.notification_id,
                "related_recommendation_id": log.related_recommendation_id,
                "notification_type": log.notification_type.value,
                "reason": "参照先Recommendationが存在しない(--accept-unresolved未指定)",
            }
        )
    return PreflightCheck(
        name="notification_log_reference_integrity",
        passed=not offending,
        detail=(
            f"NotificationLog→Recommendation参照整合性: "
            f"未解決{len(offending)}件(--accept-unresolvedで明示許可されたものを除く)"
        ),
        offending=tuple(offending),
    )


def _check_decision_snapshot_reference_integrity(
    decision_snapshots: list[DecisionSnapshot],
    recommendations_by_id: dict[str, Recommendation],
) -> PreflightCheck:
    offending: list[dict[str, Any]] = []
    for snapshot in decision_snapshots:
        if snapshot.recommendation_id is None:
            continue
        if snapshot.recommendation_id in recommendations_by_id:
            continue
        offending.append(
            {
                "decision_id": snapshot.decision_id,
                "recommendation_id": snapshot.recommendation_id,
                "reason": "参照先Recommendationが存在しない",
            }
        )
    return PreflightCheck(
        name="decision_snapshot_reference_integrity",
        passed=not offending,
        detail=f"DecisionSnapshot→Recommendation参照整合性: 未解決{len(offending)}件",
        offending=tuple(offending),
    )


def _check_holding_purchase_lot_consistency(
    holdings: list[LegacyHoldingV1], purchase_lots: list[LegacyPurchaseLotV1]
) -> PreflightCheck:
    holding_stock_codes = {h.stock_code for h in holdings}
    offending = [
        {
            "lot_id": lot.lot_id,
            "stock_code": lot.stock_code,
            "reason": "対応するHoldingが存在しない(孤立したPurchaseLot)",
        }
        for lot in purchase_lots
        if lot.stock_code not in holding_stock_codes
    ]
    return PreflightCheck(
        name="holding_purchase_lot_consistency",
        passed=not offending,
        detail=f"Holding×PurchaseLotのstock_code整合性: 孤立ロット{len(offending)}件",
        offending=tuple(offending),
    )


def _check_holdings_snapshot_consistency(
    snapshots: list[LegacyHoldingsSnapshotEntryV1], holdings: list[LegacyHoldingV1]
) -> PreflightCheck:
    holding_stock_codes = {h.stock_code for h in holdings}
    offending = [
        {
            "stock_code": entry.stock_code,
            "reason": "active_holding=Trueだが対応するHoldingが存在しない",
        }
        for entry in snapshots
        if entry.active_holding and entry.stock_code not in holding_stock_codes
    ]
    return PreflightCheck(
        name="holdings_snapshot_consistency",
        passed=not offending,
        detail=f"HoldingsSnapshot×Holdingの整合性: 不整合{len(offending)}件",
        offending=tuple(offending),
    )


def _check_validation_holdings_snapshot_consistency(
    validation_snapshots: list[LegacyHoldingsSnapshotEntryV1], holdings: list[LegacyHoldingV1]
) -> PreflightCheck:
    """ValidationHoldingsSnapshotについても、通常のHoldingsSnapshotと同じ
    active_holding=True整合性検証を独立に行う(normal側のみ検証していた
    レビュー指摘の是正)。"""
    holding_stock_codes = {h.stock_code for h in holdings}
    offending = [
        {
            "stock_code": entry.stock_code,
            "reason": "active_holding=Trueだが対応するHoldingが存在しない(validation側)",
        }
        for entry in validation_snapshots
        if entry.active_holding and entry.stock_code not in holding_stock_codes
    ]
    return PreflightCheck(
        name="validation_holdings_snapshot_consistency",
        passed=not offending,
        detail=f"ValidationHoldingsSnapshot×Holdingの整合性: 不整合{len(offending)}件",
        offending=tuple(offending),
    )


def _check_baseline_reference_integrity(
    sequences: list[Any], pointers: list[Any]
) -> PreflightCheck:
    sequence_holding_ids = {s.holding_id for s in sequences}
    offending = [
        {
            "holding_id": pointer.holding_id,
            "reason": "対応するsequenceカウンタが存在しない",
        }
        for pointer in pointers
        if pointer.holding_id not in sequence_holding_ids
    ]
    return PreflightCheck(
        name="baseline_pointer_sequence_integrity",
        passed=not offending,
        detail=f"Pointer×Sequenceの参照整合性: 不整合{len(offending)}件",
        offending=tuple(offending),
    )


def _describe_v2_table(file_name: str) -> dict[str, Any] | None:
    import boto3
    from botocore.exceptions import ClientError

    table_name = resolve_table_name(file_name)
    client = boto3.client("dynamodb")
    try:
        response = client.describe_table(TableName=table_name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise
    return dict(response["Table"])


_V2_TABLE_FILE_NAMES = (
    "holdings_v2.json",
    "holdings_snapshots_v2.json",
    "validation_holdings_snapshots_v2.json",
    "investment_thesis_baseline_sequences_v2.json",
    "investment_thesis_baseline_pointers_v2.json",
)


def _check_v2_tables_exist_with_holding_id_key(
    target: MigrationTarget,
) -> tuple[PreflightCheck, dict[str, int]]:
    """V2テーブルの存在・KeySchema(holding_id)・既存件数を確認する(AWSのみ)。

    --target localの場合、V2テーブルはJsonCollectionStoreが初回書き込み時に
    自動生成するローカルファイルのため、事前の存在確認・KeySchema確認は
    意味を持たない(常にPASS扱いとする)。
    """
    if target is MigrationTarget.LOCAL:
        return (
            PreflightCheck(
                name="v2_tables_exist_with_holding_id_key",
                passed=True,
                detail="target=localのため対象外(ローカルファイルは書き込み時に自動生成される)",
            ),
            {},
        )

    offending: list[dict[str, Any]] = []
    pre_migration_counts: dict[str, int] = {}
    for file_name in _V2_TABLE_FILE_NAMES:
        table_name = resolve_table_name(file_name)
        description = _describe_v2_table(file_name)
        if description is None:
            offending.append({"table": table_name, "reason": "テーブルが存在しない"})
            continue
        key_schema = description.get("KeySchema", [])
        is_holding_id_hash_key = key_schema == [{"AttributeName": "holding_id", "KeyType": "HASH"}]
        if not is_holding_id_hash_key:
            offending.append(
                {"table": table_name, "reason": f"KeySchemaがholding_idではない: {key_schema}"}
            )
        pre_migration_counts[table_name] = int(description.get("ItemCount", 0))

    return (
        PreflightCheck(
            name="v2_tables_exist_with_holding_id_key",
            passed=not offending,
            detail=f"V2テーブル{len(_V2_TABLE_FILE_NAMES)}件のうち{len(offending)}件に問題あり",
            offending=tuple(offending),
        ),
        pre_migration_counts,
    )


def run_preflight(
    target: MigrationTarget,
    store_dir: Path | None = None,
    accepted_unresolved_notification_ids: frozenset[str] = frozenset(),
) -> PreflightReport:
    """全preflightチェックを実行し、PreflightReportを返す(副作用なし、読み取りのみ)。

    Store生成・読み取りは全て単一のtarget_backend(target)コンテキスト内で
    行う。build_collection_store()はAWS_LAMBDA_FUNCTION_NAME環境変数の有無
    だけでlocal/AWSを切り替えるため、コンテキストが途切れると
    --target awsを指定していても一部の読み取りだけlocal JSONへ
    フォールバックしうる(local/AWSの混在)。それを避けるため、ここ1箇所で
    全Store生成を包む。
    """
    with target_backend(target):
        holding_store = build_collection_store(
            LegacyHoldingV1, "holdings.json", "stock_code", store_dir
        )
        lot_store = build_collection_store(
            LegacyPurchaseLotV1, "purchase_lots.json", "lot_id", store_dir
        )
        snapshot_store = build_collection_store(
            LegacyHoldingsSnapshotEntryV1, "holdings_snapshots.json", "stock_code", store_dir
        )
        validation_snapshot_store = build_collection_store(
            LegacyHoldingsSnapshotEntryV1,
            "validation_holdings_snapshots.json",
            "stock_code",
            store_dir,
        )
        recommendation_store = build_collection_store(
            Recommendation, "recommendations.json", "recommendation_id", store_dir
        )
        notification_store = build_collection_store(
            NotificationLog, "notification_log.json", "notification_id", store_dir
        )
        decision_snapshot_store = build_collection_store(
            DecisionSnapshot, "decision_snapshots.json", "decision_id", store_dir
        )

        holdings = holding_store.list_all()
        purchase_lots = lot_store.list_all()
        snapshots = snapshot_store.list_all()
        validation_snapshots = validation_snapshot_store.list_all()
        recommendations = recommendation_store.list_all()
        notification_logs = notification_store.list_all()
        decision_snapshots = decision_snapshot_store.list_all()
        sequences = read_all_legacy_sequences(target, store_dir)
        pointers = read_all_legacy_pointers(target, store_dir)

        recommendations_by_id = {r.recommendation_id: r for r in recommendations}

        v2_check, pre_migration_counts = _check_v2_tables_exist_with_holding_id_key(target)

        checks = (
            _check_recommendation_scope_consistency(recommendations),
            _check_notification_log_reference_integrity(
                notification_logs, recommendations_by_id, accepted_unresolved_notification_ids
            ),
            _check_decision_snapshot_reference_integrity(
                decision_snapshots, recommendations_by_id
            ),
            _check_holding_purchase_lot_consistency(holdings, purchase_lots),
            _check_holdings_snapshot_consistency(snapshots, holdings),
            _check_validation_holdings_snapshot_consistency(validation_snapshots, holdings),
            _check_baseline_reference_integrity(sequences, pointers),
            v2_check,
        )

        counts: dict[str, int] = {
            "holdings": len(holdings),
            "purchase_lots": len(purchase_lots),
            "holdings_snapshots": len(snapshots),
            "validation_holdings_snapshots": len(validation_snapshots),
            "recommendations": len(recommendations),
            "notification_logs": len(notification_logs),
            "decision_snapshots": len(decision_snapshots),
            "baseline_sequences": len(sequences),
            "baseline_pointers": len(pointers),
        }
        counts.update({f"v2_existing:{k}": v for k, v in pre_migration_counts.items()})
    return PreflightReport(checks=checks, counts=counts)
