"""TradingPauseConfig(保有銘柄オーナー機能移行M0)の初回作成・楽観ロック・
フォールバックのテスト。

`Test*Dynamo*`クラスは、test_watchlist_rotation_state.pyで発見・回帰確認された
「create()がCollectionStore経由のdataブロブのみを書き込み、update()が生
boto3のトップレベル属性UpdateExpressionを参照する」という不整合(update()の
初回呼び出しが常にConditionalCheckFailedExceptionになる)が、本モジュールでは
発生しないことを確認する回帰テスト(create/get/updateをすべてトップレベル
属性のみで一貫させた設計)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import trading_pause_config as repo
from jstock_advisor.services.trading_pause_service import (
    TradingPauseAlreadyInitializedError,
    TradingPauseService,
)

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_REGION = "ap-northeast-1"
_TABLE_NAME = "jstock-trading_pause_config"


# --- ローカルJSONバックエンド ---------------------------------------------------


def test_is_buy_sell_paused_defaults_to_false_when_uninitialized(tmp_path: Path) -> None:
    service = TradingPauseService(store_dir=tmp_path)
    assert service.is_buy_sell_paused() is False


def test_init_then_conflict_on_second_init(tmp_path: Path) -> None:
    service = TradingPauseService(store_dir=tmp_path)
    created = service.init_config(
        pause_buy_sell=False, updated_by="tester", change_reason="M0導入", now=_NOW
    )
    assert created.config_version == 1
    assert created.pause_buy_sell is False
    with pytest.raises(TradingPauseAlreadyInitializedError):
        service.init_config(
            pause_buy_sell=True, updated_by="tester", change_reason="retry", now=_NOW
        )


def test_update_increments_version_and_is_buy_sell_paused_reflects_it(tmp_path: Path) -> None:
    service = TradingPauseService(store_dir=tmp_path)
    service.init_config(pause_buy_sell=False, updated_by="tester", change_reason="init", now=_NOW)
    assert service.is_buy_sell_paused() is False

    updated = service.update_config(
        expected_config_version=1,
        pause_buy_sell=True,
        updated_by="tester",
        change_reason="開始: 保有銘柄オーナー機能移行M2",
        now=_NOW,
    )
    assert updated.config_version == 2
    assert updated.pause_buy_sell is True
    assert service.is_buy_sell_paused() is True


def test_update_conflict_detection_on_stale_version(tmp_path: Path) -> None:
    service = TradingPauseService(store_dir=tmp_path)
    service.init_config(pause_buy_sell=False, updated_by="tester", change_reason="init", now=_NOW)
    service.update_config(
        expected_config_version=1,
        pause_buy_sell=True,
        updated_by="operator_a",
        change_reason="A's change",
        now=_NOW,
    )
    with pytest.raises(repo.TradingPauseConflictError):
        service.update_config(
            expected_config_version=1,  # stale
            pause_buy_sell=False,
            updated_by="operator_b",
            change_reason="B's stale change",
            now=_NOW,
        )


def test_is_buy_sell_paused_fails_closed_on_repository_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取得に失敗した場合、安全側(一時停止扱い)へフォールバックする。"""
    service = TradingPauseService(store_dir=tmp_path)
    service.init_config(pause_buy_sell=False, updated_by="tester", change_reason="init", now=_NOW)

    def _boom(store_dir: Path | None = None) -> None:
        raise RuntimeError("simulated repository failure")

    monkeypatch.setattr(repo, "get", _boom)
    assert service.is_buy_sell_paused() is True


# --- DynamoDBバックエンド(トップレベル属性のみで一貫させた設計の回帰確認) --------


@pytest.fixture
def dynamo_lambda_env(monkeypatch: pytest.MonkeyPatch):
    """running_on_lambda()==Trueを模擬し、本モジュールが実際に使うテーブル定義
    (config_idのみHASH key、他は全てトップレベル属性)でテーブルを作成する。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "line-webhook")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def test_dynamodb_get_returns_none_before_init(dynamo_lambda_env: object) -> None:
    assert repo.get() is None


def test_dynamodb_create_get_update_use_consistent_format(dynamo_lambda_env: object) -> None:
    """create→get→update→getが全て成功し、2回目のget()が更新後の値を正しく
    返すこと(修正前の設計であれば、update()の初回呼び出しがConditionalCheck
    FailedExceptionで失敗するはずのケース)。"""
    created = repo.init(
        pause_buy_sell=False, updated_by="tester", change_reason="M0導入", now=_NOW
    )
    assert created is not None
    assert created.config_version == 1

    fetched = repo.get()
    assert fetched is not None
    assert fetched.pause_buy_sell is False
    assert fetched.config_version == 1

    updated = repo.update(
        expected_config_version=1,
        pause_buy_sell=True,
        updated_by="tester",
        change_reason="開始: 保有銘柄オーナー機能移行M2",
        now=_NOW,
    )
    assert updated.config_version == 2
    assert updated.pause_buy_sell is True

    refetched = repo.get()
    assert refetched is not None
    assert refetched.config_version == 2
    assert refetched.pause_buy_sell is True


def test_dynamodb_second_init_returns_none(dynamo_lambda_env: object) -> None:
    repo.init(pause_buy_sell=False, updated_by="tester", change_reason="init", now=_NOW)
    second = repo.init(pause_buy_sell=True, updated_by="tester", change_reason="retry", now=_NOW)
    assert second is None


def test_dynamodb_update_conflict_on_stale_version(dynamo_lambda_env: object) -> None:
    repo.init(pause_buy_sell=False, updated_by="tester", change_reason="init", now=_NOW)
    repo.update(
        expected_config_version=1,
        pause_buy_sell=True,
        updated_by="operator_a",
        change_reason="A's change",
        now=_NOW,
    )
    with pytest.raises(repo.TradingPauseConflictError):
        repo.update(
            expected_config_version=1,  # stale
            pause_buy_sell=False,
            updated_by="operator_b",
            change_reason="B's stale change",
            now=_NOW,
        )
