"""保有銘柄オーナー機能移行(M2)の各データ種別の移行内容(scope継承・
holding_id値移行・HoldingsSnapshot移行)の正しさを検証するテスト。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
    DecisionType,
)
from jstock_advisor.domain.entities.enums import AccountType, ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesis,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.transaction import Transaction
from jstock_advisor.infrastructure.aws import trading_pause_config
from jstock_advisor.infrastructure.collection_store import (
    build_collection_store,
    resolve_table_name,
)
from jstock_advisor.migrations.holdings_owner_migration import run_migration
from jstock_advisor.migrations.holdings_owner_preflight import run_preflight
from jstock_advisor.migrations.target import MigrationTarget
from jstock_advisor.migrations.v2_entities import HoldingsSnapshotEntryV2

_NOW = dt.datetime(2026, 8, 22, 0, 0, tzinfo=dt.UTC)
_STOCK = "8306"


def _set_pause(store_dir: Path, paused: bool) -> None:
    trading_pause_config.init(
        pause_buy_sell=paused, updated_by="tester", change_reason="setup", store_dir=store_dir
    )


def _seed_holding_and_lot(store_dir: Path) -> None:
    build_collection_store(PurchaseLot, "purchase_lots.json", "lot_id", store_dir).upsert(
        PurchaseLot(
            lot_id="lot-1",
            stock_code=_STOCK,
            purchase_date=dt.date(2026, 1, 1),
            shares=100,
            purchase_price=Decimal("1500"),
            account_type=AccountType.GENERAL,
        )
    )
    build_collection_store(Holding, "holdings.json", "stock_code", store_dir).upsert(
        Holding(
            stock_code=_STOCK,
            stock_name="三菱UFJ",
            shares=100,
            average_purchase_price=Decimal("1500"),
            total_purchase_amount=Decimal("150000"),
            first_purchase_date=dt.date(2026, 1, 1),
            last_purchase_date=dt.date(2026, 1, 1),
            account_type=AccountType.GENERAL,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


def _make_recommendation(
    recommendation_id: str,
    recommendation_type: RecommendationType,
    shares_at_recommendation: int | None,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK,
        stock_name="三菱UFJ",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        price_at_recommendation=Decimal("1500"),
        shares_at_recommendation=shares_at_recommendation,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def test_recommendation_migration_backfills_holding_family_only(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    rec_store = build_collection_store(
        Recommendation, "recommendations.json", "recommendation_id", store_dir
    )
    rec_store.upsert(_make_recommendation("rec-buy", RecommendationType.BUY, None))
    rec_store.upsert(_make_recommendation("rec-sell", RecommendationType.SELL, 100))

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    buy_rec = rec_store.get("rec-buy")
    sell_rec = rec_store.get("rec-sell")
    assert buy_rec is not None and buy_rec.owner is None and buy_rec.holding_id is None
    assert sell_rec is not None
    assert sell_rec.owner == "本人"
    assert sell_rec.holding_id == "本人#8306"


def test_notification_log_inherits_recommendation_scope(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    rec_store = build_collection_store(
        Recommendation, "recommendations.json", "recommendation_id", store_dir
    )
    rec_store.upsert(_make_recommendation("rec-sell", RecommendationType.SELL, 100))
    rec_store.upsert(_make_recommendation("rec-buy", RecommendationType.BUY, None))
    log_store = build_collection_store(
        NotificationLog, "notification_log.json", "notification_id", store_dir
    )
    log_store.upsert(
        NotificationLog(
            notification_id="notif-holding",
            notification_type="SELL_SIGNAL",
            content_hash="h1",
            sent_at=_NOW,
            related_recommendation_id="rec-sell",
        )
    )
    log_store.upsert(
        NotificationLog(
            notification_id="notif-buy",
            notification_type="DAILY_BUY_CANDIDATES",
            content_hash="h2",
            sent_at=_NOW,
            related_recommendation_id="rec-buy",
        )
    )
    log_store.upsert(
        NotificationLog(
            notification_id="notif-summary",
            notification_type="BATCH_SUMMARY",
            content_hash="h3",
            sent_at=_NOW,
        )
    )

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    holding_log = log_store.get("notif-holding")
    buy_log = log_store.get("notif-buy")
    summary_log = log_store.get("notif-summary")
    assert holding_log is not None
    assert holding_log.owner == "本人" and holding_log.holding_id == "本人#8306"
    assert buy_log is not None and buy_log.owner is None and buy_log.holding_id is None
    assert summary_log is not None and summary_log.owner is None and summary_log.holding_id is None


def test_decision_snapshot_inherits_recommendation_scope(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    rec_store = build_collection_store(
        Recommendation, "recommendations.json", "recommendation_id", store_dir
    )
    rec_store.upsert(_make_recommendation("rec-sell", RecommendationType.SELL, 100))
    snapshot_store = build_collection_store(
        DecisionSnapshot, "decision_snapshots.json", "decision_id", store_dir
    )
    snapshot_store.upsert(
        DecisionSnapshot(
            decision_id="decision-1",
            decision_type=DecisionType.SELL,
            stock_code=_STOCK,
            evaluated_at=_NOW,
            evaluation_date_jst=_NOW.date(),
            recommendation_id="rec-sell",
            market_price=Decimal("1500"),
            rule_version="v1-mvp",
            model_version=DECISION_SNAPSHOT_MODEL_VERSION,
        )
    )

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    migrated = snapshot_store.get("decision-1")
    assert migrated is not None
    assert migrated.owner == "本人"
    assert migrated.holding_id == "本人#8306"


def test_transaction_migration_backfills_all_records(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    tx_store = build_collection_store(Transaction, "transactions.json", "transaction_id", store_dir)
    tx_store.upsert(
        Transaction(
            transaction_id="tx-1",
            stock_code=_STOCK,
            transaction_type="BUY",
            execution_date=dt.date(2026, 1, 1),
            shares=100,
            execution_price=Decimal("1500"),
            followed_recommendation=True,
            created_at=_NOW,
        )
    )

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    migrated = tx_store.get("tx-1")
    assert migrated is not None
    assert migrated.owner == "本人"
    assert migrated.holding_id == "本人#8306"


def test_holdings_snapshot_migration_writes_v2_with_owner(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    build_collection_store(
        HoldingsSnapshotEntry, "holdings_snapshots.json", "stock_code", store_dir
    ).upsert(HoldingsSnapshotEntry(stock_code=_STOCK, shares=100, recorded_at=dt.date(2026, 1, 1)))

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    v2 = build_collection_store(
        HoldingsSnapshotEntryV2, "holdings_snapshots_v2.json", "holding_id", store_dir
    ).get("本人#8306")
    assert v2 is not None
    assert v2.owner == "本人"
    assert v2.stock_code == _STOCK


def _holding_decision_result(holding_id: str) -> HoldingDecisionResult:
    return HoldingDecisionResult(
        holding_decision_result_id="hdr-1",
        holding_id=holding_id,
        stock_code=_STOCK,
        evaluated_at=_NOW,
        company_quality=CompanyQualityScore(score=30.0, coverage_ratio=1.0),
        investment_thesis=InvestmentThesisScore(score=25.0, coverage_ratio=1.0),
        risk_deduction=RiskDeductionScore(score=10.0, coverage_ratio=1.0),
        base_score=45.0,
        hard_gate=HoldingDecisionHardGate(triggered=False),
        final_score=45.0,
        display_value=45,
        category=HoldingDecisionCategory.SELL_CONSIDERATION,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=HoldingDecisionConfidenceLevel.HIGH,
        should_notify=True,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
    )


def test_holding_decision_result_holding_id_migrates_from_stock_code_alias(
    store_dir: Path,
) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    store = build_collection_store(
        HoldingDecisionResult,
        "holding_decision_results.json",
        "holding_decision_result_id",
        store_dir,
    )
    store.upsert(_holding_decision_result(holding_id=_STOCK))  # 旧: holding_id=stock_codeエイリアス

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    migrated = store.get("hdr-1")
    assert migrated is not None
    assert migrated.holding_id == "本人#8306"
    assert migrated.holding_decision_result_id == "hdr-1"  # 識別子自体は変更しない


def test_investment_thesis_holding_id_migrates_from_stock_code_alias(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    store = build_collection_store(
        InvestmentThesis, "investment_theses.json", "investment_thesis_id", store_dir
    )
    store.upsert(
        InvestmentThesis(
            investment_thesis_id="thesis-1", holding_id=_STOCK, stock_code=_STOCK, updated_at=_NOW
        )
    )

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    migrated = store.get("thesis-1")
    assert migrated is not None
    assert migrated.holding_id == "本人#8306"


# --- V2テーブルKeySchema不正時のpreflight FAIL(AWS) ---------------------------


@pytest.fixture
def moto_wrong_key_schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-migrate")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-northeast-1")
        for file_name, key in (
            ("holdings.json", "stock_code"),
            ("purchase_lots.json", "lot_id"),
            ("holdings_snapshots.json", "stock_code"),
            ("recommendations.json", "recommendation_id"),
            ("notification_log.json", "notification_id"),
            ("decision_snapshots.json", "decision_id"),
            ("investment_thesis_baseline_sequences.json", "holding_id"),
            ("investment_thesis_baseline_pointers.json", "holding_id"),
        ):
            client.create_table(
                TableName=resolve_table_name(file_name),
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        # V2テーブルのうち1つだけ、意図的に誤ったKeySchema(stock_code)で作成する。
        client.create_table(
            TableName=resolve_table_name("holdings_v2.json"),
            KeySchema=[{"AttributeName": "stock_code", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "stock_code", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def test_preflight_fails_when_v2_table_has_wrong_key_schema(
    moto_wrong_key_schema: None,
) -> None:
    report = run_preflight(MigrationTarget.AWS)

    assert report.passed is False
    check = next(c for c in report.checks if c.name == "v2_tables_exist_with_holding_id_key")
    assert check.passed is False
    assert any("holdings_v2" in str(o.get("table", "")) for o in check.offending)


def test_preflight_fails_when_v2_table_missing(moto_wrong_key_schema: None) -> None:
    # holdings_snapshots_v2等、他のV2テーブルは作成していないため「存在しない」で検知される。
    report = run_preflight(MigrationTarget.AWS)
    check = next(c for c in report.checks if c.name == "v2_tables_exist_with_holding_id_key")
    assert any("holdings_snapshots_v2" in str(o.get("table", "")) for o in check.offending)
