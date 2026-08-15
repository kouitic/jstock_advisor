"""永続ラウンドロビン方式(計画Part A-4)の状態管理モジュールの単体テスト。

`running_on_lambda()`がFalse(既定のテスト実行環境)の場合、`_commit_local`
(JsonCollectionStore経由)が使われる。`store_dir`をtmp_pathへ束縛することで、
実データディレクトリ(data/local_store/)を一切汚染しない。

`Test*Dynamo*`クラス(本番検証2026-08対応)は`AWS_LAMBDA_FUNCTION_NAME`を
設定して`_commit_dynamodb`(Lambda/DynamoDB経路)を実際に駆動する。以前この
経路は、DynamoDbCollectionStoreが実際に書き込む「PK(rotation_id) + data
(JSON文字列)」スキーマと異なり、pointer_version等をトップレベルのネイティブ
属性として直接update_itemしていたため、ConditionExpressionが常に
ConditionalCheckFailedExceptionとなりcommitが恒久的に失敗していた
(ローカルJSON版のテストだけでは検知できなかった不具合)。ここでは
`DynamoDbCollectionStore`と全く同じテーブル定義・アイテム形式でテーブルを
作成し、ローカル版と同じ挙動になることを回帰確認する。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws.watchlist_rotation_state import (
    create_rotation_state_if_absent,
    get_rotation_state,
    try_commit_rotation_advance,
)

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_REGION = "ap-northeast-1"
_TABLE_NAME = "jstock-watchlist_screening_rotation_state"


def test_get_rotation_state_returns_none_when_not_created(tmp_path: Path) -> None:
    assert get_rotation_state(store_dir=tmp_path) is None


def test_create_rotation_state_if_absent_starts_from_head(tmp_path: Path) -> None:
    state = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    assert state.pointer_version == 1
    assert state.cycle_number == 1
    assert state.last_stock_code is None
    assert state.last_market_segment is None
    assert state.cycle_progress_selected_count == 0


def test_create_rotation_state_if_absent_is_idempotent(tmp_path: Path) -> None:
    first = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    later = _NOW + dt.timedelta(days=7)
    second = create_rotation_state_if_absent(later, store_dir=tmp_path)
    assert second.pointer_version == first.pointer_version
    assert second.last_started_at == first.last_started_at  # 2回目の呼び出しでリセットされない


def test_try_commit_rotation_advance_success_without_wrap(tmp_path: Path) -> None:
    state = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    committed = try_commit_rotation_advance(
        state.pointer_version,
        "Prime",
        "0300",
        wrapped=False,
        selected_count=300,
        now=_NOW,
        store_dir=tmp_path,
    )
    assert committed is True

    updated = get_rotation_state(store_dir=tmp_path)
    assert updated is not None
    assert updated.pointer_version == state.pointer_version + 1
    assert updated.last_market_segment == "Prime"
    assert updated.last_stock_code == "0300"
    assert updated.cycle_number == 1  # wrapped=Falseのため据え置き
    assert updated.cycle_progress_selected_count == 300


def test_try_commit_rotation_advance_wrap_increments_cycle_and_resets_progress(
    tmp_path: Path,
) -> None:
    state = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    try_commit_rotation_advance(
        state.pointer_version,
        "Prime",
        "0300",
        wrapped=False,
        selected_count=300,
        now=_NOW,
        store_dir=tmp_path,
    )
    after_first = get_rotation_state(store_dir=tmp_path)
    assert after_first is not None

    later = _NOW + dt.timedelta(days=7)
    committed = try_commit_rotation_advance(
        after_first.pointer_version,
        "Prime",
        "0050",
        wrapped=True,
        selected_count=120,
        now=later,
        store_dir=tmp_path,
    )
    assert committed is True

    after_wrap = get_rotation_state(store_dir=tmp_path)
    assert after_wrap is not None
    assert after_wrap.cycle_number == 2
    assert after_wrap.cycle_progress_selected_count == 120  # リセットされ今回選択件数のみ
    assert after_wrap.last_started_at == later


def test_try_commit_rotation_advance_conflict_on_stale_version(tmp_path: Path) -> None:
    state = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    first = try_commit_rotation_advance(
        state.pointer_version,
        "Prime",
        "0300",
        wrapped=False,
        selected_count=300,
        now=_NOW,
        store_dir=tmp_path,
    )
    assert first is True

    # 2つのDispatcherが同時に古いpointer_versionでcommitを試みたケースを模擬する。
    second = try_commit_rotation_advance(
        state.pointer_version,
        "Prime",
        "9999",
        wrapped=False,
        selected_count=300,
        now=_NOW,
        store_dir=tmp_path,
    )
    assert second is False

    # 負けた側の値では上書きされない(先勝ちの結果が維持される)。
    unchanged = get_rotation_state(store_dir=tmp_path)
    assert unchanged is not None
    assert unchanged.last_stock_code == "0300"


def test_try_commit_rotation_advance_returns_false_when_state_absent(tmp_path: Path) -> None:
    committed = try_commit_rotation_advance(
        1, "Prime", "0300", wrapped=False, selected_count=300, now=_NOW, store_dir=tmp_path
    )
    assert committed is False


# --- 本番検証2026-08対応: DynamoDB(PK + data JSON文字列)スキーマでの回帰テスト ---


@pytest.fixture
def dynamo_lambda_env(monkeypatch: pytest.MonkeyPatch):
    """running_on_lambda()==Trueを模擬し、DynamoDbCollectionStoreと同一の
    テーブル定義(rotation_idのみHASH key、他は全てdata属性内)を作成する。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "watchlist-dispatcher")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "rotation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "rotation_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def test_dynamodb_scenario_a_initial_commit_succeeds_and_advances_cursor(
    dynamo_lambda_env: object,
) -> None:
    """A: pointer_version=1・last_stock_code=Noneの初期状態からexpected_version=1
    でcommit → True・pointer_version=2・cursor更新(これが以前は常にFalseに
    なっていた、本件の中核回帰テスト)。"""
    state = create_rotation_state_if_absent(_NOW)
    assert state.pointer_version == 1
    assert state.last_stock_code is None

    committed = try_commit_rotation_advance(
        1, "プライム（内国株式）", "0300", wrapped=False, selected_count=300, now=_NOW
    )
    assert committed is True

    updated = get_rotation_state()
    assert updated is not None
    assert updated.pointer_version == 2
    assert updated.last_market_segment == "プライム（内国株式）"
    assert updated.last_stock_code == "0300"


def test_dynamodb_scenario_b_stale_expected_version_fails_without_side_effect(
    dynamo_lambda_env: object,
) -> None:
    """B: 現在version=2に対しexpected_version=1でcommit → False・state変更なし。"""
    create_rotation_state_if_absent(_NOW)
    first = try_commit_rotation_advance(
        1, "Prime", "0300", wrapped=False, selected_count=300, now=_NOW
    )
    assert first is True

    stale = try_commit_rotation_advance(
        1, "Prime", "9999", wrapped=False, selected_count=300, now=_NOW
    )
    assert stale is False

    unchanged = get_rotation_state()
    assert unchanged is not None
    assert unchanged.pointer_version == 2
    assert unchanged.last_stock_code == "0300"  # 負けた側の値では上書きされない


def test_dynamodb_scenario_c_no_wrap_accumulates_progress_without_cycle_change(
    dynamo_lambda_env: object,
) -> None:
    """C: wrapped=False → cycle_progress_selected_count加算・cycle_number不変。"""
    create_rotation_state_if_absent(_NOW)
    try_commit_rotation_advance(1, "Prime", "0300", wrapped=False, selected_count=300, now=_NOW)

    updated = get_rotation_state()
    assert updated is not None
    assert updated.cycle_number == 1
    assert updated.cycle_progress_selected_count == 300

    try_commit_rotation_advance(2, "Prime", "0600", wrapped=False, selected_count=150, now=_NOW)
    updated2 = get_rotation_state()
    assert updated2 is not None
    assert updated2.cycle_number == 1
    assert updated2.cycle_progress_selected_count == 450


def test_dynamodb_scenario_d_wrap_increments_cycle_and_resets_progress(
    dynamo_lambda_env: object,
) -> None:
    """D: wrapped=True → cycle_number+1・cycle_progress_selected_countリセット・
    last_started_at更新。"""
    create_rotation_state_if_absent(_NOW)
    try_commit_rotation_advance(1, "Prime", "0300", wrapped=False, selected_count=300, now=_NOW)

    later = _NOW + dt.timedelta(days=7)
    committed = try_commit_rotation_advance(
        2, "Prime", "0050", wrapped=True, selected_count=120, now=later
    )
    assert committed is True

    after_wrap = get_rotation_state()
    assert after_wrap is not None
    assert after_wrap.cycle_number == 2
    assert after_wrap.cycle_progress_selected_count == 120
    assert after_wrap.last_started_at == later


def test_dynamodb_scenario_e_preexisting_pk_plus_data_item_updates_without_migration(
    dynamo_lambda_env: object,
) -> None:
    """E: 本番に既に存在する{rotation_id, data}形式のレコード(migration無し)を
    そのまま読み込んでcommitできることを保証する。DynamoDbCollectionStoreを
    一切経由せず、生のput_itemで本番と同じ形の項目を直接作る。"""
    import json

    dynamo_lambda_env.put_item(
        TableName=_TABLE_NAME,
        Item={
            "rotation_id": {"S": "default"},
            "data": {
                "S": json.dumps(
                    {
                        "rotation_id": "default",
                        "pointer_version": 1,
                        "last_market_segment": None,
                        "last_stock_code": None,
                        "cycle_number": 1,
                        "cycle_progress_selected_count": 0,
                        "universe_signature": None,
                        "last_started_at": _NOW.isoformat(),
                        "last_completed_at": None,
                    }
                )
            },
        },
    )

    committed = try_commit_rotation_advance(
        1, "Prime", "0300", wrapped=False, selected_count=300, now=_NOW
    )
    assert committed is True

    updated = get_rotation_state()
    assert updated is not None
    assert updated.pointer_version == 2
    assert updated.last_stock_code == "0300"


def test_dynamodb_scenario_f_concurrent_clients_only_one_of_two_succeeds(
    dynamo_lambda_env: object,
) -> None:
    """F: 2クライアントが同じversion=1を読んだケース: 片方だけcommit成功、
    もう片方はFalse、pointer_versionは2までしか進まない(600件分二重前進しない)。"""
    state = create_rotation_state_if_absent(_NOW)
    assert state.pointer_version == 1

    client_a_result = try_commit_rotation_advance(
        1, "Prime", "0300", wrapped=False, selected_count=300, now=_NOW
    )
    client_b_result = try_commit_rotation_advance(
        1, "Prime", "0300", wrapped=False, selected_count=300, now=_NOW
    )

    assert {client_a_result, client_b_result} == {True, False}

    final = get_rotation_state()
    assert final is not None
    assert final.pointer_version == 2
