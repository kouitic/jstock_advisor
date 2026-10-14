"""出荷 config(flag = true)+ USER 承認の上限値での起票の挙動の契約テスト(Issue #837)。

#926 の抑止の層 1〜3 は `test_issue_926_incident_github_issue_guard_service.py` が、
`decided()` の値(= 承認された上限値と同じ値)で固定している。本ファイルは、★ 出荷されるもの
そのもの(出荷の `config/incident_notification.yaml` を上書きせず・出荷の
`config/incident_issue_limits.yaml` を読む)★ で、有効化後に実際に起きることを固定する。

  A  出荷の上限値は USER 承認の値(最小間隔 360 分・同一 fingerprint 2 件・全体 5 件・自動停止
     10 件)で、#926 のテストが使う `decided()` と同じ値(= #926 の挙動のテストが出荷値に当たる)
  B  出荷 config(flag = true)+ 出荷の上限値で:
       ・全体の上限 5 件: 6 件目(別の fingerprint)は GLOBAL_LIMIT で起票しない
       ・最小間隔 360 分: 359 分後の新しい occurrence は MIN_INTERVAL で起票せず、360 分で起票
       ・同一 fingerprint の日あたり 2 件: 3 件目は FINGERPRINT_LIMIT
       ・自動停止 10 件: 全体の上限を超えた試行が 10 回に達したら GitHub を一切呼ばない停止になる
       ・同じ occurrence の再配送(既存 Issue が closed)は新規 Issue を作らない(#188 の再現)
  C  ★ これらは『有効化したときの挙動』の固定であり、Production の有効化ではない ★
     (GitHub は fake。Production の flag の本番反映は USER の別の承認・ChangeSet 確認後)
"""

from __future__ import annotations

import datetime as dt
import json

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.notification import incident_issue_guard as guard
from jstock_advisor.domain.notification.incident_issue_limits import (
    IncidentIssueLimits,
    load_incident_issue_limits,
)
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.github import client as github_client_module
from tests.unit.test_issue_926_incident_github_issue_guard_service import (
    _FP_A,
    _FP_B,
    _NOW,
    FakeGithub,
    _pem,
    decided,
    line_row,
    run,
    state,
)

_REGION = "ap-northeast-1"
_SECRET_NAME = "github-app-incident-837-behaviour"


@pytest.fixture
def activation_secret_arn(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        boto3.client("dynamodb", region_name=_REGION).create_table(
            TableName="jstock-incident_state",
            KeySchema=[{"AttributeName": "fingerprint", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "fingerprint", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        secrets = boto3.client("secretsmanager", region_name=_REGION)
        secrets.create_secret(
            Name=_SECRET_NAME,
            SecretString=json.dumps({"app_id": "1", "installation_id": "2", "private_key": _pem()}),
        )
        yield secrets.describe_secret(SecretId=_SECRET_NAME)["ARN"]


@pytest.fixture
def activation_github(monkeypatch: pytest.MonkeyPatch) -> FakeGithub:
    fake = FakeGithub()
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    return fake


def _shipped_config():
    """★ 出荷の config をそのまま(flag を上書きしない)★。"""
    return load_config().incident_notification


def _fp(index: int) -> str:
    return f"{index:064x}"


def test_the_shipped_limits_are_the_user_approved_values_and_match_the_926_helper() -> None:
    shipped = load_incident_issue_limits()
    assert shipped == IncidentIssueLimits(
        new_issue_min_interval_minutes=360,
        new_issue_limit_per_fingerprint=2,
        new_issue_limit_global=5,
        auto_stop_denied_threshold=10,
    )
    assert shipped.is_decided()
    assert shipped == decided()  # #926 の挙動のテストが使う値 = 出荷値


def test_the_shipped_config_has_the_flag_on_so_the_scenarios_below_use_it_unmodified() -> None:
    assert _shipped_config().issue_creation_enabled is True


def test_a_first_signal_creates_one_issue_and_a_closed_issue_flap_creates_no_more(
    activation_secret_arn, activation_github
) -> None:
    config = _shipped_config()
    line_row(_FP_A)
    run(activation_secret_arn, config, None)  # limits を渡さない = 出荷の上限値
    assert activation_github.creations == 1
    activation_github.close_all()
    for _ in range(3):
        run(activation_secret_arn, config, None)  # 同じ occurrence の再配送(#188 の再現)
    assert activation_github.creations == 1
    assert state()["github_issue_last_denied_reason"] == "SAME_OCCURRENCE"


def test_the_sixth_fingerprint_is_denied_by_the_global_limit_of_five(
    activation_secret_arn, activation_github
) -> None:
    config = _shipped_config()
    for index in range(1, 7):
        line_row(_fp(index))
        run(activation_secret_arn, config, None, fingerprint=_fp(index))
    assert activation_github.creations == 5
    assert state(_fp(6))["github_issue_last_denied_reason"] == "GLOBAL_LIMIT"
    assert state(_fp(5))["github_issue_create_status"] == "CREATED"


def test_the_minimum_interval_is_exactly_360_minutes(
    activation_secret_arn, activation_github
) -> None:
    config = _shipped_config()
    line_row(_FP_A)
    run(activation_secret_arn, config, None)
    activation_github.close_all()
    run(activation_secret_arn, config, None, occurrence=2, now=_NOW + dt.timedelta(minutes=359))
    assert activation_github.creations == 1
    assert state()["github_issue_last_denied_reason"] == "MIN_INTERVAL"
    run(activation_secret_arn, config, None, occurrence=2, now=_NOW + dt.timedelta(minutes=360))
    assert activation_github.creations == 2


def test_the_same_fingerprint_is_limited_to_two_issues_a_day(
    activation_secret_arn, activation_github
) -> None:
    config = _shipped_config()
    line_row(_FP_A)
    for occurrence, minutes in ((1, 0), (2, 360), (3, 720)):
        run(
            activation_secret_arn,
            config,
            None,
            occurrence=occurrence,
            now=_NOW + dt.timedelta(minutes=minutes),  # 09:00 / 15:00 / 21:00 JST(同じ JST の日)
        )
        activation_github.close_all()
    assert activation_github.creations == 2
    assert state()["github_issue_last_denied_reason"] == "FINGERPRINT_LIMIT"


def test_ten_denied_attempts_over_the_global_limit_stop_every_github_call(
    activation_secret_arn, activation_github
) -> None:
    config = _shipped_config()
    for index in range(1, 16):  # 5 件は作成・10 件は全体の上限超過(= 自動停止のしきい値)
        line_row(_fp(index))
        run(activation_secret_arn, config, None, fingerprint=_fp(index))
    assert activation_github.creations == 5
    control = tracker.read_github_issue_control()
    assert control is not None and tracker.is_kill_switch_on(control)
    assert control["kill_switch_reason"] == guard.KILL_SWITCH_REASON_AUTO
    before = len(activation_github.requests)
    line_row(_FP_B)
    run(activation_secret_arn, config, None, fingerprint=_FP_B)
    assert (
        len(activation_github.requests) == before
    )  # 停止中は token の取得を含め GitHub を呼ばない
    assert "github_issue_number" not in state(_FP_B)


def test_nine_denied_attempts_do_not_stop_it_yet(activation_secret_arn, activation_github) -> None:
    config = _shipped_config()
    for index in range(1, 15):  # 5 件作成 + 9 件の上限超過
        line_row(_fp(index))
        run(activation_secret_arn, config, None, fingerprint=_fp(index))
    control = tracker.read_github_issue_control()
    assert control is not None and not tracker.is_kill_switch_on(control)
