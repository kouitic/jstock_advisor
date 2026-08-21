"""cli/trading_pause.pyの--target厳密化のテスト(コードレビュー対応)。

以前はtarget: strの自由文字列で「"aws"以外は全てlocal」という判定だった
ため、"--target awss"のようなタイプミスが本番操作のつもりでローカル操作
として黙って成功してしまい、本番を一時停止したと誤認したままmigrationへ
進む重大事故につながりかねなかった。CliTarget(Enum)への厳密化により、
"local"/"aws"以外の値・未指定はいずれも非ゼロ終了し、何も変更しないことを
確認する。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from jstock_advisor.cli import trading_pause as cli_module
from jstock_advisor.infrastructure.aws import trading_pause_config as repo

_runner = CliRunner()
_REGION = "ap-northeast-1"
_TABLE_NAME = "jstock-trading_pause_config"


@pytest.fixture
def moto_trading_pause_table(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def test_target_aws_operates_on_aws(moto_trading_pause_table: None) -> None:
    result = _runner.invoke(
        cli_module.app,
        ["init", "--changed-by", "tester", "--reason", "test", "--target", "aws"],
    )
    assert result.exit_code == 0, result.output
    # CLI呼び出し終了後はAWS_LAMBDA_FUNCTION_NAMEが復元(未設定)されるため、
    # repo.get()経由ではなく実際のDynamoDB(moto)を直接確認する。
    client = boto3.client("dynamodb", region_name=_REGION)
    item = client.get_item(TableName=_TABLE_NAME, Key={"config_id": {"S": "trading_pause"}})
    assert "Item" in item
    assert item["Item"]["pause_buy_sell"]["BOOL"] is False


def test_target_local_operates_on_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from jstock_advisor.infrastructure.local_repository import json_store

    # 実データディレクトリ(data/local_store/)を汚染しないよう、既定の保存先を
    # tmp_pathへ差し替える(CLIはstore_dirオプションを公開していないため)。
    monkeypatch.setattr(json_store, "DEFAULT_STORE_DIR", tmp_path)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)

    result = _runner.invoke(
        cli_module.app,
        ["init", "--changed-by", "tester", "--reason", "test", "--target", "local"],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "trading_pause_config.json").exists()
    # ローカル実行時、AWS_LAMBDA_FUNCTION_NAMEは設定されていない(local操作の確認)。
    assert os.environ.get("AWS_LAMBDA_FUNCTION_NAME") is None


def test_target_typo_exits_nonzero_and_changes_nothing(
    moto_trading_pause_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Enum検証で弾かれ、AWS/ローカルいずれのバックエンドにも到達しないはずだが、
    # 万一到達してもローカルの実データディレクトリを汚染しないよう保存先を
    # 差し替えておく(安全側の確認)。
    from jstock_advisor.infrastructure.local_repository import json_store

    monkeypatch.setattr(json_store, "DEFAULT_STORE_DIR", tmp_path)

    result = _runner.invoke(
        cli_module.app,
        ["init", "--changed-by", "tester", "--reason", "test", "--target", "awss"],
    )
    assert result.exit_code != 0
    assert repo.get() is None  # AWS側(moto)に何も書き込まれていない
    assert not (tmp_path / "trading_pause_config.json").exists()  # ローカル側にも同様


def test_target_missing_exits_nonzero(
    moto_trading_pause_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jstock_advisor.infrastructure.local_repository import json_store

    monkeypatch.setattr(json_store, "DEFAULT_STORE_DIR", tmp_path)

    result = _runner.invoke(
        cli_module.app, ["init", "--changed-by", "tester", "--reason", "test"]
    )
    assert result.exit_code != 0
    assert repo.get() is None
    assert not (tmp_path / "trading_pause_config.json").exists()


def test_set_and_status_also_require_target(moto_trading_pause_table: None) -> None:
    for args in (
        ["set", "--buy-sell", "--changed-by", "tester", "--reason", "test"],
        ["status"],
    ):
        result = _runner.invoke(cli_module.app, args)
        assert result.exit_code != 0, args


def test_set_toggles_buy_sell_flag_against_aws(moto_trading_pause_table: None) -> None:
    """--buy-sell/--no-buy-sellの実際の切替(コードレビュー対応で発覚:
    boolオプションはtrue/falseという値を取らず、フラグの有無で表現する)。"""
    init_result = _runner.invoke(
        cli_module.app,
        ["init", "--changed-by", "tester", "--reason", "init", "--target", "aws"],
    )
    assert init_result.exit_code == 0, init_result.output

    on_result = _runner.invoke(
        cli_module.app,
        [
            "set",
            "--buy-sell",
            "--changed-by",
            "tester",
            "--reason",
            "pause on",
            "--target",
            "aws",
        ],
    )
    assert on_result.exit_code == 0, on_result.output
    assert "True" in on_result.output

    client = boto3.client("dynamodb", region_name=_REGION)
    item = client.get_item(TableName=_TABLE_NAME, Key={"config_id": {"S": "trading_pause"}})
    assert item["Item"]["pause_buy_sell"]["BOOL"] is True

    off_result = _runner.invoke(
        cli_module.app,
        [
            "set",
            "--no-buy-sell",
            "--changed-by",
            "tester",
            "--reason",
            "pause off",
            "--target",
            "aws",
        ],
    )
    assert off_result.exit_code == 0, off_result.output
    item = client.get_item(TableName=_TABLE_NAME, Key={"config_id": {"S": "trading_pause"}})
    assert item["Item"]["pause_buy_sell"]["BOOL"] is False
