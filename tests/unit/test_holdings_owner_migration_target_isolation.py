"""--target aws/local指定時に、preflight・migration本体の実行が最初から最後まで
一貫して同じバックエンドだけを参照し、local/AWSが混在しないことを検証するテスト
(レビュー指摘の是正: 従来はtarget_backend(target)が_ensure_trading_paused()内部
だけに適用されており、それ以外のStore生成はAWS_LAMBDA_FUNCTION_NAME環境変数の
有無だけに従っていたため、--target awsを指定してもpause確認後にlocal JSONへ
フォールバックしうるという不整合があった)。

各テストではローカル側とAWS側へ意図的に異なるデータを置き、--target awsの
実行結果がAWS側の件数・内容だけを反映すること、--target localの実行がAWSへ
一切アクセスしないことを確認する。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.infrastructure.aws import trading_pause_config
from jstock_advisor.infrastructure.collection_store import (
    build_collection_store,
    resolve_table_name,
)
from jstock_advisor.migrations.holdings_owner_migration import run_migration
from jstock_advisor.migrations.holdings_owner_preflight import run_preflight
from jstock_advisor.migrations.legacy_shapes import LegacyHoldingV1, LegacyPurchaseLotV1
from jstock_advisor.migrations.target import MigrationTarget, target_backend
from jstock_advisor.migrations.v2_entities import HoldingV2

_NOW = dt.datetime(2026, 8, 22, 0, 0, tzinfo=dt.UTC)

_ALL_TABLE_SPECS: tuple[tuple[str, str], ...] = (
    ("holdings.json", "stock_code"),
    ("purchase_lots.json", "lot_id"),
    ("holdings_snapshots.json", "stock_code"),
    ("validation_holdings_snapshots.json", "stock_code"),
    ("recommendations.json", "recommendation_id"),
    ("notification_log.json", "notification_id"),
    ("decision_snapshots.json", "decision_id"),
    ("transactions.json", "transaction_id"),
    ("holding_decision_results.json", "holding_decision_result_id"),
    ("investment_theses.json", "investment_thesis_id"),
    ("investment_thesis_baselines.json", "baseline_id"),
    ("investment_thesis_baseline_sequences.json", "holding_id"),
    ("investment_thesis_baseline_pointers.json", "holding_id"),
    ("trading_pause_config.json", "config_id"),
    ("holdings_v2.json", "holding_id"),
    ("holdings_snapshots_v2.json", "holding_id"),
    ("validation_holdings_snapshots_v2.json", "holding_id"),
    ("investment_thesis_baseline_sequences_v2.json", "holding_id"),
    ("investment_thesis_baseline_pointers_v2.json", "holding_id"),
)


@pytest.fixture
def aws_tables(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    # 重要: ここではAWS_LAMBDA_FUNCTION_NAMEを設定しない。--target awsの
    # バックエンド切替がtarget_backend(target)自身によって行われることを
    # 検証するのがこのテストの目的のため(手動でLambda環境を偽装すると
    # 検証対象のバグを覆い隠してしまう)。
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-northeast-1")
        for file_name, key in _ALL_TABLE_SPECS:
            client.create_table(
                TableName=resolve_table_name(file_name),
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield


def _holding(stock_code: str, shares: int) -> LegacyHoldingV1:
    return LegacyHoldingV1(
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        shares=shares,
        average_purchase_price=Decimal("1500"),
        total_purchase_amount=Decimal("1500") * shares,
        first_purchase_date=dt.date(2026, 1, 1),
        last_purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.GENERAL,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _lot(lot_id: str, stock_code: str, shares: int) -> LegacyPurchaseLotV1:
    return LegacyPurchaseLotV1(
        lot_id=lot_id,
        stock_code=stock_code,
        purchase_date=dt.date(2026, 1, 1),
        shares=shares,
        purchase_price=Decimal("1500"),
        account_type=AccountType.GENERAL,
    )


def _seed_aws_holding_and_lot(stock_code: str, shares: int, *, paused: bool = True) -> None:
    with target_backend(MigrationTarget.AWS):
        build_collection_store(LegacyHoldingV1, "holdings.json", "stock_code", None).upsert(
            _holding(stock_code, shares)
        )
        build_collection_store(LegacyPurchaseLotV1, "purchase_lots.json", "lot_id", None).upsert(
            _lot("aws-lot-1", stock_code, shares)
        )
        trading_pause_config.init(
            pause_buy_sell=paused, updated_by="tester", change_reason="aws setup"
        )


def _seed_local_holding_and_lot(
    store_dir: Path, stock_code: str, shares: int, *, paused: bool = True
) -> None:
    build_collection_store(LegacyHoldingV1, "holdings.json", "stock_code", store_dir).upsert(
        _holding(stock_code, shares)
    )
    build_collection_store(LegacyPurchaseLotV1, "purchase_lots.json", "lot_id", store_dir).upsert(
        _lot("local-lot-1", stock_code, shares)
    )
    trading_pause_config.init(
        pause_buy_sell=paused,
        updated_by="tester",
        change_reason="local setup",
        store_dir=store_dir,
    )


def _seed_local_orphan_purchase_lot(store_dir: Path, stock_code: str) -> None:
    """対応するHoldingが存在しない孤立したPurchaseLotのみをlocalへ置く
    (holding_purchase_lot_consistencyチェックが失敗する原因になる)。"""
    build_collection_store(LegacyPurchaseLotV1, "purchase_lots.json", "lot_id", store_dir).upsert(
        _lot("local-orphan-lot", stock_code, 10)
    )


# --- 1. preflight --target awsはAWS側のデータのみを読む -----------------------


def test_preflight_target_aws_reads_only_aws_data(aws_tables: None, store_dir: Path) -> None:
    _seed_aws_holding_and_lot("8306", 100)
    # localには対応するHoldingの無い孤立ロットだけを置く。もしpreflightが
    # 誤ってlocalへフォールバックすれば、holdings=0・purchase_lots=1(孤立)と
    # なりholding_purchase_lot_consistencyが失敗するはずである。
    _seed_local_orphan_purchase_lot(store_dir, "9999")

    report = run_preflight(MigrationTarget.AWS, store_dir=store_dir)

    assert report.counts["holdings"] == 1
    assert report.counts["purchase_lots"] == 1
    check = next(c for c in report.checks if c.name == "holding_purchase_lot_consistency")
    assert check.passed is True, check.offending


# --- 2. run --target aws --dry-runもAWS側のみを読み、localは一切参照しない -----


def test_dry_run_target_aws_never_reads_local(aws_tables: None, store_dir: Path) -> None:
    _seed_aws_holding_and_lot("8306", 100, paused=True)
    # store_dir配下にはtrading_pause_configすら置かない。もし途中でlocalへ
    # フォールバックすれば、AWS側の値(holdings=1)ではなく0が返るか、
    # 最悪の場合pause未初期化として中止されるはずである。

    result = run_migration(MigrationTarget.AWS, dry_run=True, store_dir=store_dir)

    assert result.dry_run is True
    assert result.counts_written["holdings"] == 1
    assert result.counts_written["purchase_lots"] == 1


# --- 3. run --target aws --no-dry-runで全対象がAWS側へ書かれる -----------------


def test_run_target_aws_writes_to_aws(aws_tables: None, store_dir: Path) -> None:
    _seed_aws_holding_and_lot("8306", 100, paused=True)

    result = run_migration(MigrationTarget.AWS, dry_run=False, store_dir=store_dir)

    assert result.counts_written["holdings"] == 1
    with target_backend(MigrationTarget.AWS):
        migrated = build_collection_store(HoldingV2, "holdings_v2.json", "holding_id", None).get(
            "本人#8306"
        )
    assert migrated is not None
    assert migrated.owner == "本人"
    assert migrated.shares == 100


# --- 4. --target localではAWSへ一切アクセスしない ------------------------------


def test_run_target_local_never_touches_aws(
    monkeypatch: pytest.MonkeyPatch, store_dir: Path
) -> None:
    _seed_local_holding_and_lot(store_dir, "8306", 100, paused=True)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("target=localなのにAWS(boto3)へアクセスしようとした")

    monkeypatch.setattr("boto3.client", _boom)
    monkeypatch.setattr("boto3.resource", _boom)

    result = run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert result.counts_written["holdings"] == 1


# --- 5. 1回のrun中にlocal/AWSの混在が発生しない --------------------------------


def test_run_does_not_mix_local_and_aws_data_in_a_single_run(
    aws_tables: None, store_dir: Path
) -> None:
    _seed_aws_holding_and_lot("8306", 100, paused=True)
    _seed_local_holding_and_lot(store_dir, "9999", 50, paused=True)

    run_migration(MigrationTarget.AWS, dry_run=False, store_dir=store_dir)

    with target_backend(MigrationTarget.AWS):
        holdings_v2_store = build_collection_store(
            HoldingV2, "holdings_v2.json", "holding_id", None
        )
        aws_migrated = holdings_v2_store.list_all()
    assert [h.holding_id for h in aws_migrated] == ["本人#8306"]

    # localのpurchase_lots.jsonは一切変更されていない(owner/holding_id未付与の
    # 旧形状のまま)ことを確認する。
    local_lots = build_collection_store(
        LegacyPurchaseLotV1, "purchase_lots.json", "lot_id", store_dir
    ).list_all()
    assert len(local_lots) == 1
    assert local_lots[0].stock_code == "9999"
