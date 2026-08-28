"""Issue #32 Phase A: NotificationLogのGSI/TTL用index属性のdual-write・
backfillスクリプトのテスト。

- キー生成pure関数(build_index_attributes等)の仕様
- 通常save経路でのDynamoDBトップレベル属性付与(data JSON本体は不変)
- backfillスクリプトのdry-run安全性・execute冪等性・parse不能item扱い・
  phase-awareな--verify
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import NotificationType
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    EXPIRES_AT_ATTRIBUTE,
    HOLDING_SCOPE_INDEX_NAME,
    HOLDING_SCOPE_KEY_ATTRIBUTE,
    NOTIFICATION_LOG_RETENTION_DAYS,
    SENT_SORT_ATTRIBUTE,
    STOCK_SCOPE_INDEX_NAME,
    STOCK_SCOPE_KEY_ATTRIBUTE,
    NotificationLogRepository,
    build_expires_at_epoch,
    build_holding_scope_key,
    build_index_attributes,
    build_sent_sort_value,
    build_stock_scope_key,
)

_REGION = "ap-northeast-1"
_TABLE_NAME = "jstock-notification_log"
_NOW = dt.datetime(2026, 8, 28, 1, 2, 3, microsecond=45, tzinfo=dt.UTC)

_SCRIPT_NAME = "backfill_notification_log_index_attributes.py"
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / _SCRIPT_NAME


def _load_backfill_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backfill_notification_log", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # `from __future__ import annotations`下のdataclassはクラス定義時に
    # sys.modules[cls.__module__]を参照するため、exec前に登録が必要。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_log(
    notification_id: str = "n-1",
    notification_type: NotificationType = NotificationType.SELL_SIGNAL,
    stock_code: str | None = "8306",
    holding_id: str | None = "owner-a#8306",
    sent_at: dt.datetime = _NOW,
) -> NotificationLog:
    return NotificationLog(
        notification_id=notification_id,
        notification_type=notification_type,
        stock_code=stock_code,
        content_hash="hash",
        sent_at=sent_at,
        related_recommendation_id="rec-1",
        owner="owner-a" if holding_id else None,
        holding_id=holding_id,
    )


# --- pure helper -------------------------------------------------------------


def test_stock_scope_key_format() -> None:
    key = build_stock_scope_key("8306", NotificationType.SELL_SIGNAL)
    assert key == "S#8306#SELL_SIGNAL"


def test_stock_scope_key_pseudo_stock_code() -> None:
    key = build_stock_scope_key(
        "__batch__:holdings_watchlist_analysis", NotificationType.BATCH_SUMMARY
    )
    assert key == "S#__batch__:holdings_watchlist_analysis#BATCH_SUMMARY"


def test_holding_scope_key_contains_owner_qualified_holding_id() -> None:
    key = build_holding_scope_key("owner-a#8306", NotificationType.PROFIT_TAKING_SIGNAL)
    assert key == "H#owner-a#8306#PROFIT_TAKING_SIGNAL"


def test_sent_sort_value_fixed_width_microseconds() -> None:
    value = build_sent_sort_value(_NOW, "n-1")
    assert value == "2026-08-28T01:02:03.000045Z#n-1"


def test_sent_sort_value_tie_break_by_notification_id() -> None:
    a = build_sent_sort_value(_NOW, "aaa")
    b = build_sent_sort_value(_NOW, "bbb")
    assert a < b  # 同一sent_atでもnotification_idで完全順序が決まる


def test_sent_sort_value_lexicographic_equals_chronological() -> None:
    earlier = build_sent_sort_value(_NOW, "zzz")
    later = build_sent_sort_value(_NOW + dt.timedelta(microseconds=1), "aaa")
    assert earlier < later  # 固定幅のため時刻が優先される


def test_expires_at_is_730_days_epoch_int() -> None:
    epoch = build_expires_at_epoch(_NOW)
    expected = int((_NOW + dt.timedelta(days=NOTIFICATION_LOG_RETENTION_DAYS)).timestamp())
    assert epoch == expected
    assert isinstance(epoch, int)


def test_naive_sent_at_is_treated_as_utc() -> None:
    naive = _NOW.replace(tzinfo=None)
    assert build_sent_sort_value(naive, "n-1") == build_sent_sort_value(_NOW, "n-1")
    assert build_expires_at_epoch(naive) == build_expires_at_epoch(_NOW)


def test_aware_non_utc_sent_at_is_converted_to_utc() -> None:
    jst = _NOW.astimezone(dt.timezone(dt.timedelta(hours=9)))
    assert build_sent_sort_value(jst, "n-1") == build_sent_sort_value(_NOW, "n-1")
    assert build_expires_at_epoch(jst) == build_expires_at_epoch(_NOW)


def test_build_index_attributes_full_scope() -> None:
    attrs = build_index_attributes(_make_log())
    assert attrs == {
        SENT_SORT_ATTRIBUTE: "2026-08-28T01:02:03.000045Z#n-1",
        STOCK_SCOPE_KEY_ATTRIBUTE: "S#8306#SELL_SIGNAL",
        HOLDING_SCOPE_KEY_ATTRIBUTE: "H#owner-a#8306#SELL_SIGNAL",
        EXPIRES_AT_ATTRIBUTE: build_expires_at_epoch(_NOW),
    }


def test_build_index_attributes_stock_scope_only() -> None:
    attrs = build_index_attributes(_make_log(holding_id=None))
    assert HOLDING_SCOPE_KEY_ATTRIBUTE not in attrs
    assert attrs[STOCK_SCOPE_KEY_ATTRIBUTE] == "S#8306#SELL_SIGNAL"


def test_build_index_attributes_no_stock_code() -> None:
    attrs = build_index_attributes(
        _make_log(stock_code=None, holding_id=None, notification_type=NotificationType.DATA_ERROR)
    )
    assert STOCK_SCOPE_KEY_ATTRIBUTE not in attrs
    assert HOLDING_SCOPE_KEY_ATTRIBUTE not in attrs
    assert SENT_SORT_ATTRIBUTE in attrs
    assert EXPIRES_AT_ATTRIBUTE in attrs


def test_attention_has_no_ttl_but_keeps_index_keys() -> None:
    attrs = build_index_attributes(
        _make_log(notification_type=NotificationType.PROFIT_PROTECTION_ATTENTION)
    )
    assert EXPIRES_AT_ATTRIBUTE not in attrs
    assert attrs[STOCK_SCOPE_KEY_ATTRIBUTE] == "S#8306#PROFIT_PROTECTION_ATTENTION"
    assert attrs[HOLDING_SCOPE_KEY_ATTRIBUTE] == "H#owner-a#8306#PROFIT_PROTECTION_ATTENTION"
    assert SENT_SORT_ATTRIBUTE in attrs


# --- DynamoDB(moto)経路 ----------------------------------------------------


@pytest.fixture
def dynamo_lambda_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "notification-log-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "notification_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "notification_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=_REGION)


@pytest.fixture
def dynamo_lambda_env_with_gsi(monkeypatch: pytest.MonkeyPatch):
    """GSI-1/GSI-2作成後(Phase C/D相当)のテーブルを模擬する。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "notification-log-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "notification_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "notification_id", "AttributeType": "S"},
                {"AttributeName": STOCK_SCOPE_KEY_ATTRIBUTE, "AttributeType": "S"},
                {"AttributeName": HOLDING_SCOPE_KEY_ATTRIBUTE, "AttributeType": "S"},
                {"AttributeName": SENT_SORT_ATTRIBUTE, "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": STOCK_SCOPE_INDEX_NAME,
                    "KeySchema": [
                        {"AttributeName": STOCK_SCOPE_KEY_ATTRIBUTE, "KeyType": "HASH"},
                        {"AttributeName": SENT_SORT_ATTRIBUTE, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": HOLDING_SCOPE_INDEX_NAME,
                    "KeySchema": [
                        {"AttributeName": HOLDING_SCOPE_KEY_ATTRIBUTE, "KeyType": "HASH"},
                        {"AttributeName": SENT_SORT_ATTRIBUTE, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=_REGION)


def test_save_writes_top_level_index_attributes(dynamo_lambda_env) -> None:
    log = _make_log()
    NotificationLogRepository().save(log)
    raw = dynamo_lambda_env.Table(_TABLE_NAME).get_item(Key={"notification_id": "n-1"})["Item"]
    assert raw[STOCK_SCOPE_KEY_ATTRIBUTE] == "S#8306#SELL_SIGNAL"
    assert raw[HOLDING_SCOPE_KEY_ATTRIBUTE] == "H#owner-a#8306#SELL_SIGNAL"
    assert raw[SENT_SORT_ATTRIBUTE] == "2026-08-28T01:02:03.000045Z#n-1"
    assert int(raw[EXPIRES_AT_ATTRIBUTE]) == build_expires_at_epoch(_NOW)
    # data JSON本体は従来と同一形式(index属性はJSONへ混入しない)
    restored = NotificationLog.model_validate_json(raw["data"])
    assert restored == log
    assert "nl_" not in raw["data"]


def test_save_and_read_back_via_existing_scan_path(dynamo_lambda_env) -> None:
    repo = NotificationLogRepository()
    repo.save(_make_log(notification_id="n-1", sent_at=_NOW))
    repo.save(_make_log(notification_id="n-2", sent_at=_NOW + dt.timedelta(minutes=1)))
    latest = repo.latest_by_stock_and_type("8306", NotificationType.SELL_SIGNAL)
    assert latest is not None and latest.notification_id == "n-2"
    latest_h = repo.latest_by_holding_and_type("owner-a#8306", NotificationType.SELL_SIGNAL)
    assert latest_h is not None and latest_h.notification_id == "n-2"


def test_legacy_item_without_index_attributes_still_readable(dynamo_lambda_env) -> None:
    """Phase A(Scan読み取り)ではindex属性が無い既存itemも従来どおり見える。"""
    legacy = _make_log(notification_id="legacy-1")
    dynamo_lambda_env.Table(_TABLE_NAME).put_item(
        Item={"notification_id": "legacy-1", "data": legacy.model_dump_json()}
    )
    latest = NotificationLogRepository().latest_by_stock_and_type(
        "8306", NotificationType.SELL_SIGNAL
    )
    assert latest is not None and latest.notification_id == "legacy-1"


def test_local_json_store_save_ignores_index_attributes(tmp_path: Path) -> None:
    repo = NotificationLogRepository(store_dir=tmp_path)
    log = _make_log()
    repo.save(log)
    assert repo.latest_by_stock_and_type("8306", NotificationType.SELL_SIGNAL) == log


# --- backfillスクリプト -------------------------------------------------------


def _put_legacy_items(resource, logs: list[NotificationLog]) -> None:
    table = resource.Table(_TABLE_NAME)
    for log in logs:
        table.put_item(Item={"notification_id": log.notification_id, "data": log.model_dump_json()})


def test_backfill_dry_run_writes_nothing(dynamo_lambda_env, capsys) -> None:
    module = _load_backfill_module()
    _put_legacy_items(dynamo_lambda_env, [_make_log(notification_id="legacy-1")])
    exit_code = module.main(["--table", _TABLE_NAME], dynamodb_resource=dynamo_lambda_env)
    assert exit_code == 0
    raw = dynamo_lambda_env.Table(_TABLE_NAME).get_item(Key={"notification_id": "legacy-1"})["Item"]
    assert SENT_SORT_ATTRIBUTE not in raw  # dry-runでは一切書き込まない
    assert "dry-run" in capsys.readouterr().out


def test_backfill_execute_requires_confirm_table(dynamo_lambda_env) -> None:
    module = _load_backfill_module()
    _put_legacy_items(dynamo_lambda_env, [_make_log(notification_id="legacy-1")])
    exit_code = module.main(
        ["--table", _TABLE_NAME, "--execute"], dynamodb_resource=dynamo_lambda_env
    )
    assert exit_code == 1
    raw = dynamo_lambda_env.Table(_TABLE_NAME).get_item(Key={"notification_id": "legacy-1"})["Item"]
    assert SENT_SORT_ATTRIBUTE not in raw

    exit_code = module.main(
        ["--table", _TABLE_NAME, "--execute", "--confirm-table", "wrong-name"],
        dynamodb_resource=dynamo_lambda_env,
    )
    assert exit_code == 1


def test_backfill_execute_adds_attributes_and_preserves_data(dynamo_lambda_env) -> None:
    module = _load_backfill_module()
    attention = _make_log(
        notification_id="legacy-attn",
        notification_type=NotificationType.PROFIT_PROTECTION_ATTENTION,
    )
    normal = _make_log(notification_id="legacy-1")
    _put_legacy_items(dynamo_lambda_env, [normal, attention])
    table = dynamo_lambda_env.Table(_TABLE_NAME)
    data_before = {
        item_id: table.get_item(Key={"notification_id": item_id})["Item"]["data"]
        for item_id in ("legacy-1", "legacy-attn")
    }

    exit_code = module.main(
        ["--table", _TABLE_NAME, "--execute", "--confirm-table", _TABLE_NAME],
        dynamodb_resource=dynamo_lambda_env,
    )
    assert exit_code == 0

    raw_normal = table.get_item(Key={"notification_id": "legacy-1"})["Item"]
    assert raw_normal[STOCK_SCOPE_KEY_ATTRIBUTE] == "S#8306#SELL_SIGNAL"
    assert raw_normal[HOLDING_SCOPE_KEY_ATTRIBUTE] == "H#owner-a#8306#SELL_SIGNAL"
    assert int(raw_normal[EXPIRES_AT_ATTRIBUTE]) == build_expires_at_epoch(_NOW)
    raw_attention = table.get_item(Key={"notification_id": "legacy-attn"})["Item"]
    assert EXPIRES_AT_ATTRIBUTE not in raw_attention  # ATTENTIONにはTTLを付けない
    assert SENT_SORT_ATTRIBUTE in raw_attention
    # data JSON本体はバイト単位で不変
    for item_id, before in data_before.items():
        after = table.get_item(Key={"notification_id": item_id})["Item"]["data"]
        assert after == before


def test_backfill_execute_is_idempotent(dynamo_lambda_env, capsys) -> None:
    module = _load_backfill_module()
    _put_legacy_items(dynamo_lambda_env, [_make_log(notification_id="legacy-1")])
    args = ["--table", _TABLE_NAME, "--execute", "--confirm-table", _TABLE_NAME]
    assert module.main(args, dynamodb_resource=dynamo_lambda_env) == 0
    capsys.readouterr()
    assert module.main(args, dynamodb_resource=dynamo_lambda_env) == 0
    assert "更新実行: 0件" in capsys.readouterr().out  # 再実行では更新0件(冪等)


def test_backfill_removes_stray_ttl_from_attention(dynamo_lambda_env) -> None:
    """ATTENTIONへ誤ってnl_expires_atが付いていた場合、executeが取り除き収束する。"""
    module = _load_backfill_module()
    attention = _make_log(
        notification_id="legacy-attn",
        notification_type=NotificationType.PROFIT_PROTECTION_ATTENTION,
    )
    table = dynamo_lambda_env.Table(_TABLE_NAME)
    table.put_item(
        Item={
            "notification_id": "legacy-attn",
            "data": attention.model_dump_json(),
            EXPIRES_AT_ATTRIBUTE: 12345,
        }
    )
    assert (
        module.main(
            ["--table", _TABLE_NAME, "--execute", "--confirm-table", _TABLE_NAME],
            dynamodb_resource=dynamo_lambda_env,
        )
        == 0
    )
    raw = table.get_item(Key={"notification_id": "legacy-attn"})["Item"]
    assert EXPIRES_AT_ATTRIBUTE not in raw


def test_backfill_parse_error_item_is_reported_and_fails(dynamo_lambda_env, capsys) -> None:
    module = _load_backfill_module()
    table = dynamo_lambda_env.Table(_TABLE_NAME)
    table.put_item(Item={"notification_id": "broken-1", "data": "{not-json"})
    _put_legacy_items(dynamo_lambda_env, [_make_log(notification_id="legacy-1")])

    exit_code = module.main(
        ["--table", _TABLE_NAME, "--execute", "--confirm-table", _TABLE_NAME],
        dynamodb_resource=dynamo_lambda_env,
    )
    assert exit_code == 1  # parse不能itemがあればmigration完了扱いにしない
    out = capsys.readouterr().out
    assert "broken-1" in out
    # 正常itemの処理自体は継続している
    raw = table.get_item(Key={"notification_id": "legacy-1"})["Item"]
    assert SENT_SORT_ATTRIBUTE in raw
    # 不正itemは変更されない
    broken = table.get_item(Key={"notification_id": "broken-1"})["Item"]
    assert SENT_SORT_ATTRIBUTE not in broken


def test_verify_phase_a_without_gsi_skips_equivalence(dynamo_lambda_env, capsys) -> None:
    """GSI未作成のPhase Aでは、coverageのみ検証しGSI等価性はskip(failureにしない)。"""
    module = _load_backfill_module()
    _put_legacy_items(dynamo_lambda_env, [_make_log(notification_id="legacy-1")])
    verify_args = ["--table", _TABLE_NAME, "--verify"]
    # backfill前: coverage不足でFAIL
    assert module.main(verify_args, dynamodb_resource=dynamo_lambda_env) == 1
    # backfill後: coverage充足でPASS(GSI等価性はskip表示)
    module.main(
        ["--table", _TABLE_NAME, "--execute", "--confirm-table", _TABLE_NAME],
        dynamodb_resource=dynamo_lambda_env,
    )
    capsys.readouterr()
    assert module.main(verify_args, dynamodb_resource=dynamo_lambda_env) == 0
    out = capsys.readouterr().out
    assert "skip" in out
    assert "PASS" in out


def test_verify_with_gsi_checks_latest_equivalence(dynamo_lambda_env_with_gsi, capsys) -> None:
    """GSI作成後は全scope keyでGSI latest == Scan latestの完全一致を検証する。"""
    module = _load_backfill_module()
    resource = dynamo_lambda_env_with_gsi
    repo = NotificationLogRepository()  # 新規writeはsaveがindex属性を付与する
    repo.save(_make_log(notification_id="n-1", sent_at=_NOW))
    repo.save(_make_log(notification_id="n-2", sent_at=_NOW + dt.timedelta(minutes=1)))
    repo.save(
        _make_log(
            notification_id="n-3",
            holding_id="owner-b#8306",
            sent_at=_NOW + dt.timedelta(minutes=2),
        )
    )
    assert module.main(["--table", _TABLE_NAME, "--verify"], dynamodb_resource=resource) == 0
    out = capsys.readouterr().out
    assert "latest等価性" in out
    assert "PASS" in out


def test_verify_with_gsi_detects_unindexed_legacy_item(dynamo_lambda_env_with_gsi, capsys) -> None:
    """index属性の無いlegacy itemが残っている場合、verifyはFAILする
    (previous=None誤再送の回帰リスクをDeploy D前に検出するゲート)。"""
    module = _load_backfill_module()
    _put_legacy_items(dynamo_lambda_env_with_gsi, [_make_log(notification_id="legacy-1")])
    exit_code = module.main(
        ["--table", _TABLE_NAME, "--verify"], dynamodb_resource=dynamo_lambda_env_with_gsi
    )
    assert exit_code == 1
    assert "FAIL" in capsys.readouterr().out
