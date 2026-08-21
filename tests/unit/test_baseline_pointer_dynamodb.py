"""InvestmentThesisBaselinePointerのDynamoDB(PK + data JSON文字列)スキーマ
での回帰テスト(2026-08修正、本番検証で発覚)。

`running_on_lambda()`をTrueにして`_update_pointer_dynamodb`(Lambda/DynamoDB
経路)を実際に駆動する。以前この経路は、CollectionStore.insert_if_absent()が
実際に書き込む「PK(holding_id) + data(JSON文字列)」スキーマと異なり、
pointer_version等をトップレベルのネイティブ属性として直接update_itemして
いたため、ConditionExpressionが常にConditionalCheckFailedExceptionとなり
update_pointer()が一度も成功していなかった(ローカルJSON版のテストだけでは
検知できなかった不具合。watchlist_rotation_state.pyのrotation commitと
同じ不具合パターン)。
"""

from __future__ import annotations

import datetime as dt

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import baseline_pointer

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_REGION = "ap-northeast-1"
_TABLE_NAME = "jstock-investment_thesis_baseline_pointers"


@pytest.fixture
def dynamo_lambda_env(monkeypatch: pytest.MonkeyPatch):
    """running_on_lambda()==Trueを模擬し、DynamoDbCollectionStoreと同一の
    テーブル定義(holding_idのみHASH key、他は全てdata属性内)を作成する。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "investment-thesis-service")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "holding_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "holding_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def test_dynamodb_create_then_update_pointer_round_trip(dynamo_lambda_env: object) -> None:
    """create_pointer()が書き込んだ{holding_id, data}アイテムに対し、
    update_pointer()が実際に成功しpointer_versionが進むこと(これが以前は常に
    BaselinePointerConflictErrorになっていた、本件の中核回帰テスト)。"""
    created = baseline_pointer.create_pointer("7203", "baseline-1", 1, now=_NOW)
    assert created is not None
    assert created.pointer_version == 1

    updated = baseline_pointer.update_pointer(
        "7203",
        new_baseline_id="baseline-2",
        new_baseline_version=2,
        expected_pointer_version=1,
        now=_NOW,
    )
    assert updated.pointer_version == 2
    assert updated.active_baseline_id == "baseline-2"
    assert updated.active_baseline_version == 2

    persisted = baseline_pointer.get_pointer("7203")
    assert persisted is not None
    assert persisted.pointer_version == 2
    assert persisted.active_baseline_id == "baseline-2"


def test_dynamodb_update_pointer_conflict_on_stale_version(dynamo_lambda_env: object) -> None:
    baseline_pointer.create_pointer("7203", "baseline-1", 1, now=_NOW)
    baseline_pointer.update_pointer(
        "7203",
        new_baseline_id="baseline-2",
        new_baseline_version=2,
        expected_pointer_version=1,
        now=_NOW,
    )

    with pytest.raises(baseline_pointer.BaselinePointerConflictError):
        baseline_pointer.update_pointer(
            "7203",
            new_baseline_id="baseline-3",
            new_baseline_version=3,
            expected_pointer_version=1,  # stale
            now=_NOW,
        )

    unchanged = baseline_pointer.get_pointer("7203")
    assert unchanged is not None
    assert unchanged.pointer_version == 2
    assert unchanged.active_baseline_id == "baseline-2"


def test_dynamodb_update_pointer_conflict_when_absent(dynamo_lambda_env: object) -> None:
    with pytest.raises(baseline_pointer.BaselinePointerConflictError):
        baseline_pointer.update_pointer(
            "7203",
            new_baseline_id="baseline-1",
            new_baseline_version=1,
            expected_pointer_version=1,
            now=_NOW,
        )
