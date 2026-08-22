"""保有銘柄オーナー機能移行(M2)のpreflight検証テスト。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
    DecisionType,
)
from jstock_advisor.domain.entities.enums import AccountType, ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.collection_store import build_collection_store
from jstock_advisor.migrations.holdings_owner_preflight import (
    BUY_FAMILY_RECOMMENDATION_TYPES,
    HOLDING_FAMILY_RECOMMENDATION_TYPES,
    run_preflight,
)
from jstock_advisor.migrations.target import MigrationTarget

_NOW = dt.datetime(2026, 8, 22, 0, 0, tzinfo=dt.UTC)


def _make_recommendation(
    recommendation_id: str,
    recommendation_type: RecommendationType,
    shares_at_recommendation: int | None,
    stock_code: str = "8306",
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="テスト銘柄",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        price_at_recommendation=Decimal("1500"),
        shares_at_recommendation=shares_at_recommendation,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _seed_holding(store_dir: Path, stock_code: str = "8306", shares: int = 100) -> None:
    build_collection_store(Holding, "holdings.json", "stock_code", store_dir).upsert(
        Holding(
            stock_code=stock_code,
            stock_name="テスト銘柄",
            shares=shares,
            average_purchase_price=Decimal("1500"),
            total_purchase_amount=Decimal("1500") * shares,
            first_purchase_date=dt.date(2026, 1, 1),
            last_purchase_date=dt.date(2026, 1, 1),
            account_type=AccountType.GENERAL,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


def test_recommendation_type_classification_covers_all_members() -> None:
    """RecommendationTypeの全メンバーがBUY系/保有系いずれかに分類されていること
    (未分類メンバーがあればモジュールimport時にAssertionErrorとなるため、この
    テスト自体が通ること自体が回帰確認になる)。"""
    all_members = frozenset(RecommendationType)
    assert all_members == BUY_FAMILY_RECOMMENDATION_TYPES | HOLDING_FAMILY_RECOMMENDATION_TYPES
    assert not (BUY_FAMILY_RECOMMENDATION_TYPES & HOLDING_FAMILY_RECOMMENDATION_TYPES)


def test_preflight_passes_on_clean_data(store_dir: Path) -> None:
    _seed_holding(store_dir)
    rec_store = build_collection_store(
        Recommendation, "recommendations.json", "recommendation_id", store_dir
    )
    rec_store.upsert(_make_recommendation("rec-buy", RecommendationType.BUY, None))
    rec_store.upsert(_make_recommendation("rec-sell", RecommendationType.SELL, 100))

    report = run_preflight(MigrationTarget.LOCAL, store_dir)

    assert report.passed is True, report.render_text()


def test_preflight_detects_buy_type_with_shares_at_recommendation(store_dir: Path) -> None:
    rec_store = build_collection_store(
        Recommendation, "recommendations.json", "recommendation_id", store_dir
    )
    rec_store.upsert(_make_recommendation("rec-bad", RecommendationType.BUY, 100))

    report = run_preflight(MigrationTarget.LOCAL, store_dir)

    assert report.passed is False
    check = next(c for c in report.checks if c.name == "recommendation_scope_consistency")
    assert check.passed is False
    assert check.offending[0]["recommendation_id"] == "rec-bad"


def test_preflight_detects_holding_type_without_shares_at_recommendation(store_dir: Path) -> None:
    rec_store = build_collection_store(
        Recommendation, "recommendations.json", "recommendation_id", store_dir
    )
    rec_store.upsert(_make_recommendation("rec-bad", RecommendationType.SELL, None))

    report = run_preflight(MigrationTarget.LOCAL, store_dir)

    assert report.passed is False
    check = next(c for c in report.checks if c.name == "recommendation_scope_consistency")
    assert check.passed is False
    assert check.offending[0]["recommendation_id"] == "rec-bad"


def test_preflight_detects_unresolved_notification_log_reference(store_dir: Path) -> None:
    log_store = build_collection_store(
        NotificationLog, "notification_log.json", "notification_id", store_dir
    )
    log_store.upsert(
        NotificationLog(
            notification_id="notif-1",
            notification_type="SELL_SIGNAL",
            content_hash="hash1",
            sent_at=_NOW,
            related_recommendation_id="rec-missing",
        )
    )

    report = run_preflight(MigrationTarget.LOCAL, store_dir)

    assert report.passed is False
    check = next(c for c in report.checks if c.name == "notification_log_reference_integrity")
    assert check.passed is False
    assert check.offending[0]["notification_id"] == "notif-1"


def test_preflight_accept_unresolved_notification_log_passes(store_dir: Path) -> None:
    log_store = build_collection_store(
        NotificationLog, "notification_log.json", "notification_id", store_dir
    )
    log_store.upsert(
        NotificationLog(
            notification_id="notif-1",
            notification_type="SELL_SIGNAL",
            content_hash="hash1",
            sent_at=_NOW,
            related_recommendation_id="rec-missing",
        )
    )

    report = run_preflight(
        MigrationTarget.LOCAL,
        store_dir,
        accepted_unresolved_notification_ids=frozenset({"notif-1"}),
    )

    check = next(c for c in report.checks if c.name == "notification_log_reference_integrity")
    assert check.passed is True


def test_preflight_detects_unresolved_decision_snapshot_reference(store_dir: Path) -> None:
    snapshot_store = build_collection_store(
        DecisionSnapshot, "decision_snapshots.json", "decision_id", store_dir
    )
    snapshot_store.upsert(
        DecisionSnapshot(
            decision_id="decision-1",
            decision_type=DecisionType.BUY,
            stock_code="8306",
            evaluated_at=_NOW,
            evaluation_date_jst=_NOW.date(),
            recommendation_id="rec-missing",
            market_price=Decimal("1500"),
            rule_version="v1-mvp",
            model_version=DECISION_SNAPSHOT_MODEL_VERSION,
        )
    )

    report = run_preflight(MigrationTarget.LOCAL, store_dir)

    assert report.passed is False
    check = next(c for c in report.checks if c.name == "decision_snapshot_reference_integrity")
    assert check.passed is False
    assert check.offending[0]["decision_id"] == "decision-1"


def test_preflight_detects_orphan_purchase_lot(store_dir: Path) -> None:
    lot_store = build_collection_store(PurchaseLot, "purchase_lots.json", "lot_id", store_dir)
    lot_store.upsert(
        PurchaseLot(
            lot_id="lot-orphan",
            stock_code="9999",
            purchase_date=dt.date(2026, 1, 1),
            shares=10,
            purchase_price=Decimal("1000"),
            account_type=AccountType.GENERAL,
        )
    )

    report = run_preflight(MigrationTarget.LOCAL, store_dir)

    assert report.passed is False
    check = next(c for c in report.checks if c.name == "holding_purchase_lot_consistency")
    assert check.passed is False
    assert check.offending[0]["lot_id"] == "lot-orphan"


def test_preflight_detects_active_snapshot_without_holding(store_dir: Path) -> None:
    snapshot_store = build_collection_store(
        HoldingsSnapshotEntry, "holdings_snapshots.json", "stock_code", store_dir
    )
    snapshot_store.upsert(
        HoldingsSnapshotEntry(
            stock_code="9999",
            shares=10,
            recorded_at=dt.date(2026, 1, 1),
            active_holding=True,
        )
    )

    report = run_preflight(MigrationTarget.LOCAL, store_dir)

    assert report.passed is False
    check = next(c for c in report.checks if c.name == "holdings_snapshot_consistency")
    assert check.passed is False


def test_preflight_v2_table_check_skipped_for_local_target(store_dir: Path) -> None:
    report = run_preflight(MigrationTarget.LOCAL, store_dir)
    check = next(c for c in report.checks if c.name == "v2_tables_exist_with_holding_id_key")
    assert check.passed is True


# --- ValidationHoldingsSnapshotの整合性(normal側と独立にチェックする、追加改善)---


def test_preflight_detects_active_validation_snapshot_without_holding(store_dir: Path) -> None:
    validation_store = build_collection_store(
        HoldingsSnapshotEntry, "validation_holdings_snapshots.json", "stock_code", store_dir
    )
    validation_store.upsert(
        HoldingsSnapshotEntry(
            stock_code="9999",
            shares=10,
            recorded_at=dt.date(2026, 1, 1),
            active_holding=True,
        )
    )

    report = run_preflight(MigrationTarget.LOCAL, store_dir)

    assert report.passed is False
    check = next(
        c for c in report.checks if c.name == "validation_holdings_snapshot_consistency"
    )
    assert check.passed is False
    assert check.offending[0]["stock_code"] == "9999"


def test_preflight_passes_when_validation_snapshot_matches_holding(store_dir: Path) -> None:
    _seed_holding(store_dir, stock_code="8306")
    validation_store = build_collection_store(
        HoldingsSnapshotEntry, "validation_holdings_snapshots.json", "stock_code", store_dir
    )
    validation_store.upsert(
        HoldingsSnapshotEntry(
            stock_code="8306",
            shares=100,
            recorded_at=dt.date(2026, 1, 1),
            active_holding=True,
        )
    )

    report = run_preflight(MigrationTarget.LOCAL, store_dir)

    check = next(
        c for c in report.checks if c.name == "validation_holdings_snapshot_consistency"
    )
    assert check.passed is True
    assert report.counts["validation_holdings_snapshots"] == 1
