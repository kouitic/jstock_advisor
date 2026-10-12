"""Issue #926: services/incident_github_issue_service.py の起票の抑止(層 1〜3)の契約テスト。

DynamoDB は moto の実テーブル、GitHub API は urllib.request.urlopen のスタブ(実 API
へは接続しない)。
実測(#188 …6114701122): 既存 Issue が CLOSED のとき、同一 dedup window 内に signal を 3
回届けると新規 Issue が
3 件立った。本テストはそれを『1 件』に抑え、上限・最小間隔・自動停止・手動停止・fail-closed
を固定する。
★最重要要件: process_incident_issue は例外を伝播させない(LINE の経路を壊さない)。
"""

from __future__ import annotations

import datetime as dt
import json
import random
import urllib.error
from typing import Any

import boto3
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from moto import mock_aws

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.notification import incident_issue_guard as guard
from jstock_advisor.domain.notification.incident_github_issue_message import (
    IncidentFailureStage,
    IncidentIssueNotice,
)
from jstock_advisor.domain.notification.incident_issue_limits import (
    IncidentIssueLimits,
    undecided_limits,
)
from jstock_advisor.domain.notification.incident_message import IncidentJob
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.github import client as github_client_module
from jstock_advisor.services import incident_github_issue_service as service

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 10, 12, 0, 0, tzinfo=dt.UTC)  # JST 09:00
_SECRET = "github-app-incident-926"
_FP_A, _FP_B, _FP_C = "a" * 64, "b" * 64, "c" * 64


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
    ).decode()


@pytest.fixture
def secret_arn(monkeypatch: pytest.MonkeyPatch):
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
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(
            Name=_SECRET,
            SecretString=json.dumps({"app_id": "1", "installation_id": "2", "private_key": _pem()}),
        )
        yield sm.describe_secret(SecretId=_SECRET)["ARN"]


@pytest.fixture
def config():
    return load_config().incident_notification.model_copy(
        update={"issue_creation_enabled": True, "github_issue_claim_timeout_minutes": 10}
    )


def decided(**over: int) -> IncidentIssueLimits:
    values = {
        "new_issue_min_interval_minutes": 360,
        "new_issue_limit_per_fingerprint": 2,
        "new_issue_limit_global": 5,
        "auto_stop_denied_threshold": 10,
    }
    values.update(over)
    return IncidentIssueLimits(**values)


class FakeGithub:
    """URL とメソッドで応答する GitHub のスタブ。
    作成した Issue は close_all() で閉じる(人が閉じる操作)。"""

    def __init__(self, first_number: int = 100) -> None:
        self.next_number = first_number
        self.state: dict[int, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_create = False
        self.before_create: Any = None  # 作成の直前に呼ぶ(行の状態を観測する)

    def __call__(self, request: Any, timeout: int = 15) -> Any:
        method, url = request.get_method(), request.full_url
        self.requests.append((method, url))
        if url.endswith("/access_tokens"):
            return _Resp({"token": "ghs_dummy", "expires_at": "2099-01-01T00:00:00Z"})
        if method == "POST" and url.endswith("/comments"):
            return _Resp(None)
        if method == "POST" and url.endswith("/issues"):
            if self.before_create is not None:
                self.before_create()
            if self.fail_create:
                raise urllib.error.URLError("boom")
            number = self.next_number
            self.next_number += 1
            self.state[number] = "open"
            return _Resp(self._issue(number))
        if method == "GET" and "/search/issues" in url:
            return _Resp({"items": []})  # stale 復旧の実在確認: 既存の Issue は無い
        if method == "GET" and "/issues/" in url:
            number = int(url.rsplit("/", 1)[1])
            return _Resp(self._issue(number))
        raise AssertionError(f"unexpected request {method} {url}")

    def _issue(self, number: int) -> dict[str, Any]:
        return {
            "number": number,
            "html_url": f"https://github.com/owner/repo/issues/{number}",
            "state": self.state.get(number, "open"),
            "body": "",
        }

    def close_all(self) -> None:
        self.state = {n: "closed" for n in self.state}

    @property
    def creations(self) -> int:
        return sum(1 for m, u in self.requests if m == "POST" and u.endswith("/issues"))

    @property
    def comments(self) -> int:
        return sum(1 for m, u in self.requests if m == "POST" and u.endswith("/comments"))


class _Resp:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode() if payload is not None else b""

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGithub:
    fake = FakeGithub()
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    return fake


def line_row(fingerprint: str, now: dt.datetime = _NOW) -> None:
    """LINE の claim が作る行(本番では GitHub 経路の前に必ず存在する)。"""
    _, token = tracker.try_claim(
        fingerprint, now, dt.timedelta(minutes=30), dt.timedelta(minutes=5)
    )
    assert token is not None
    tracker.mark_sent(fingerprint, token, now)


def notice(
    fingerprint: str = _FP_A, occurrence: int = 1, now: dt.datetime = _NOW
) -> IncidentIssueNotice:
    return IncidentIssueNotice(
        job=IncidentJob.BUY_CANDIDATES,
        occurred_at=now,
        fingerprint=fingerprint,
        occurrence_count=occurrence,
        failure_stage=IncidentFailureStage.CLOUDWATCH_ALARM,
    )


def run(
    secret_arn: str,
    config: Any,
    limits: IncidentIssueLimits | None,
    fingerprint: str = _FP_A,
    occurrence: int = 1,
    now: dt.datetime = _NOW,
) -> guard.IssueGuardOutcome:
    return service.process_incident_issue(
        notice(fingerprint, occurrence, now),
        config,
        now,
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=secret_arn,
        limits=limits,
    )


def state(fingerprint: str = _FP_A) -> dict[str, Any]:
    found = tracker.get_incident_state(fingerprint)
    assert found is not None
    return found


# --- 層 1: 重複の gate ----------------------------------------------------------------------


def test_a_first_occurrence_creates_one_issue_and_records_the_gate_inputs(
    secret_arn, config, github
) -> None:
    line_row(_FP_A)
    outcome = run(secret_arn, config, decided())
    assert outcome == guard.IssueGuardOutcome()
    st = state()
    assert st["github_issue_create_status"] == "CREATED" and int(st["github_issue_number"]) == 100
    assert st["github_issue_last_created_at"] == _NOW.isoformat()
    assert int(st["github_issue_last_created_occurrence"]) == 1
    assert int(st["github_issue_window_created_count"]) == 1
    control = tracker.read_github_issue_control()
    assert control is not None and int(control["global_created_count"]) == 1
    assert github.creations == 1


def test_three_signals_of_the_same_occurrence_with_a_closed_issue_create_one_issue(
    secret_arn, config, github
) -> None:
    """実測の再現(#188 …6114701122): 修正前は新規 Issue が 3 件立った。"""
    line_row(_FP_A)
    run(secret_arn, config, decided())
    github.close_all()
    for _ in range(3):
        run(secret_arn, config, decided())  # 同じ occurrence の再配送・flap
    assert github.creations == 1
    st = state()
    assert int(st["github_issue_number"]) == 100
    assert int(st["github_issue_suppressed_count"]) == 3
    assert st["github_issue_last_denied_reason"] == "SAME_OCCURRENCE"


def test_a_new_occurrence_within_the_minimum_interval_is_suppressed(
    secret_arn, config, github
) -> None:
    line_row(_FP_A)
    run(secret_arn, config, decided())
    github.close_all()
    later = _NOW + dt.timedelta(minutes=359)
    run(secret_arn, config, decided(), occurrence=2, now=later)
    assert github.creations == 1
    assert state()["github_issue_last_denied_reason"] == "MIN_INTERVAL"


def test_a_new_occurrence_after_the_minimum_interval_creates_a_new_issue_with_the_reference(
    secret_arn, config, github
) -> None:
    line_row(_FP_A)
    run(secret_arn, config, decided())
    github.close_all()
    later = _NOW + dt.timedelta(minutes=360)  # ちょうど最小間隔
    run(secret_arn, config, decided(), occurrence=2, now=later)
    assert github.creations == 2
    st = state()
    assert int(st["github_issue_number"]) == 101 and int(st["previous_github_issue_number"]) == 100


def test_an_open_issue_still_gets_comments_not_new_issues(secret_arn, config, github) -> None:
    line_row(_FP_A)
    run(secret_arn, config, decided())
    run(secret_arn, config, decided(), occurrence=2, now=_NOW + dt.timedelta(minutes=40))
    assert github.creations == 1 and github.comments == 1


# --- 層 2: 起票の上限
# ------------------------------------------------------------------------------


def test_the_per_fingerprint_limit_stops_the_third_issue_of_the_day(
    secret_arn, config, github
) -> None:
    limits = decided(new_issue_min_interval_minutes=1, new_issue_limit_per_fingerprint=2)
    line_row(_FP_A)
    for i in range(1, 5):
        run(secret_arn, config, limits, occurrence=i, now=_NOW + dt.timedelta(minutes=10 * i))
        github.close_all()
    assert github.creations == 2
    st = state()
    assert st["github_issue_last_denied_reason"] == "FINGERPRINT_LIMIT"
    assert int(st["github_issue_suppressed_count"]) == 2
    assert st["github_issue_create_status"] == "SUPPRESSED"


def test_the_per_fingerprint_budget_returns_in_the_next_jst_day(secret_arn, config, github) -> None:
    limits = decided(new_issue_min_interval_minutes=1, new_issue_limit_per_fingerprint=1)
    line_row(_FP_A)
    run(secret_arn, config, limits, occurrence=1, now=_NOW)
    github.close_all()
    run(secret_arn, config, limits, occurrence=2, now=_NOW + dt.timedelta(hours=1))
    assert github.creations == 1
    run(secret_arn, config, limits, occurrence=3, now=_NOW + dt.timedelta(hours=16))  # JST の翌日
    assert github.creations == 2


def test_the_global_limit_stops_other_fingerprints_and_returns_their_budget(
    secret_arn, config, github
) -> None:
    limits = decided(new_issue_limit_global=2)
    for fp in (_FP_A, _FP_B, _FP_C):
        line_row(fp)
        run(secret_arn, config, limits, fingerprint=fp)
    assert github.creations == 2
    st = state(_FP_C)
    assert st["github_issue_last_denied_reason"] == "GLOBAL_LIMIT"
    assert st["github_issue_create_status"] == "SUPPRESSED"
    assert int(st.get("github_issue_window_created_count", 0)) == 0  # 全体で落ちた分は返却される
    control = tracker.read_github_issue_control()
    assert control is not None and int(control["global_created_count"]) == 2
    assert int(control["global_denied_count"]) == 1


def test_a_denied_fingerprint_can_create_after_the_budget_returns(
    secret_arn, config, github
) -> None:
    limits = decided(new_issue_limit_global=1)
    for fp in (_FP_A, _FP_B):
        line_row(fp)
        run(secret_arn, config, limits, fingerprint=fp)
    assert github.creations == 1
    run(
        secret_arn,
        config,
        limits,
        fingerprint=_FP_B,
        occurrence=2,
        now=_NOW + dt.timedelta(hours=16),
    )
    assert github.creations == 2


def test_the_limits_are_boundary_exact(secret_arn, config, github) -> None:
    limits = decided(new_issue_limit_global=3, auto_stop_denied_threshold=99)
    fps = [f"{i:064x}" for i in range(1, 6)]
    for fp in fps:
        line_row(fp)
        run(secret_arn, config, limits, fingerprint=fp)
    assert github.creations == 3  # limit ちょうどは作り、超えたら作らない


# --- 層 3: kill switch
# ------------------------------------------------------------------------------


def _flood(
    secret_arn: str, config: Any, limits: IncidentIssueLimits, n: int
) -> list[guard.IssueGuardOutcome]:
    outcomes = []
    for i in range(1, n + 1):
        fp = f"{i:064x}"
        line_row(fp)
        outcomes.append(run(secret_arn, config, limits, fingerprint=fp))
    return outcomes


def test_exceeding_the_global_limit_enough_times_trips_the_kill_switch_once(
    secret_arn, config, github
) -> None:
    limits = decided(new_issue_limit_global=1, auto_stop_denied_threshold=3)
    outcomes = _flood(secret_arn, config, limits, 5)
    control = tracker.read_github_issue_control()
    assert control is not None and tracker.is_kill_switch_on(control)
    assert control["kill_switch_reason"] == guard.KILL_SWITCH_REASON_AUTO
    assert github.creations == 1
    # 1 件目は作成、2〜4 件目は上限超過(3 回で停止)、5 件目は停止中
    assert [o.auto_stop_notification_due for o in outcomes] == [False, False, False, True, True]


def test_the_notification_is_due_until_it_is_claimed(secret_arn, config, github) -> None:
    limits = decided(new_issue_limit_global=1, auto_stop_denied_threshold=1)
    outcomes = _flood(secret_arn, config, limits, 2)
    assert outcomes[-1].auto_stop_notification_due is True
    assert tracker.try_claim_kill_switch_notification(_NOW) is True
    line_row(_FP_C)
    assert run(secret_arn, config, limits, fingerprint=_FP_C).auto_stop_notification_due is False


def test_fingerprint_level_suppressions_never_count_toward_the_auto_stop(
    secret_arn, config, github
) -> None:
    limits = decided(auto_stop_denied_threshold=1)
    line_row(_FP_A)
    run(secret_arn, config, limits)
    github.close_all()
    for _ in range(20):
        run(secret_arn, config, limits)  # 同じ occurrence の flap を何度届けても全体は止まらない
    control = tracker.read_github_issue_control()
    assert control is not None and not tracker.is_kill_switch_on(control)
    assert int(control.get("global_denied_count", 0)) == 0


def test_a_stopped_switch_blocks_new_issues_and_comments_and_every_github_call(
    secret_arn, config, github
) -> None:
    line_row(_FP_A)
    run(secret_arn, config, decided())  # Issue #100 が OPEN
    before = len(github.requests)
    tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_MANUAL)
    run(
        secret_arn, config, decided(), occurrence=2, now=_NOW + dt.timedelta(hours=1)
    )  # コメントの機会
    line_row(_FP_B)
    run(secret_arn, config, decided(), fingerprint=_FP_B)  # 新規作成の機会
    assert len(github.requests) == before  # token の取得を含め、GitHub を一切呼ばない
    assert "github_issue_number" not in state(_FP_B)


def test_a_manual_stop_does_not_ask_for_a_notification(secret_arn, config, github) -> None:
    tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_MANUAL)
    line_row(_FP_A)
    assert run(secret_arn, config, decided()).auto_stop_notification_due is False


def test_an_unreadable_control_item_is_fail_closed(
    secret_arn, config, github, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(tracker, "read_github_issue_control", boom)
    line_row(_FP_A)
    outcome = run(secret_arn, config, decided())  # 例外を伝播させない
    assert outcome == guard.IssueGuardOutcome()
    assert github.requests == []


# --- 上限が未決定のとき
# -----------------------------------------------------------------------------


def test_undecided_limits_create_no_issue_and_record_the_reason(secret_arn, config, github) -> None:
    line_row(_FP_A)
    run(secret_arn, config, undecided_limits())
    assert github.requests == []
    st = state()
    assert "github_issue_number" not in st
    assert st["github_issue_last_denied_reason"] == "LIMITS_UNDECIDED"
    assert int(st["github_issue_suppressed_count"]) == 1
    assert st["status"] == "SENT"  # LINE の状態は不変


def test_undecided_limits_still_allow_comments_on_an_open_issue(secret_arn, config, github) -> None:
    line_row(_FP_A)
    run(secret_arn, config, decided())
    run(secret_arn, config, undecided_limits(), occurrence=2, now=_NOW + dt.timedelta(minutes=40))
    assert github.creations == 1 and github.comments == 1


def test_the_default_limits_are_the_shipped_undecided_file(secret_arn, config, github) -> None:
    line_row(_FP_A)
    run(secret_arn, config, None)  # limits を渡さない = 出荷の専用 YAML(全項目 null)を読む
    assert github.requests == []
    assert state()["github_issue_last_denied_reason"] == "LIMITS_UNDECIDED"


# --- 失敗時の返却・claim の解放
# -----------------------------------------------------------------------


def test_a_github_failure_returns_both_budgets(secret_arn, config, github) -> None:
    github.fail_create = True
    line_row(_FP_A)
    run(secret_arn, config, decided())
    st = state()
    assert st["github_issue_create_status"] == "ISSUE_CREATION_FAILED"
    assert int(st.get("github_issue_window_created_count", 0)) == 0
    control = tracker.read_github_issue_control()
    assert control is not None and int(control["global_created_count"]) == 0
    github.fail_create = False
    run(secret_arn, config, decided(), occurrence=2, now=_NOW + dt.timedelta(minutes=5))
    assert len(github.state) == 1  # 失敗で予算・間隔を食わない(成功した作成は 1 件)


def test_a_suppressed_claim_is_released(secret_arn, config, github) -> None:
    limits = decided(new_issue_limit_global=1)
    for fp in (_FP_A, _FP_B):
        line_row(fp)
        run(secret_arn, config, limits, fingerprint=fp)
    st = state(_FP_B)
    assert st["github_issue_create_status"] == "SUPPRESSED"
    assert "github_issue_claimed_at" not in st and "github_issue_claim_expires_at" not in st


# --- 最重要要件・無効・既存の挙動
# ----------------------------------------------------------------------


def test_disabled_config_touches_nothing(
    secret_arn, config, github, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never() -> None:
        raise AssertionError("制御項目を読んではいけない")

    monkeypatch.setattr(tracker, "read_github_issue_control", never)
    disabled = config.model_copy(update={"issue_creation_enabled": False})
    assert run(secret_arn, disabled, decided()) == guard.IssueGuardOutcome()
    assert github.requests == [] and tracker.get_incident_state(_FP_A) is None


def test_the_guard_failing_unexpectedly_never_propagates(
    secret_arn, config, github, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("bug in the guard")

    monkeypatch.setattr(guard, "precheck_new_issue", boom)
    line_row(_FP_A)
    assert run(secret_arn, config, decided()) == guard.IssueGuardOutcome()


def test_a_missing_row_is_never_created_by_a_suppression(secret_arn, config, github) -> None:
    # 本番では LINE の claim が先に行を作る。作られていない場合も、部分的な行を作らない
    run(secret_arn, config, undecided_limits(), fingerprint=_FP_B)
    assert tracker.get_incident_state(_FP_B) is None


# --- 不変条件(ランダムな到着列。独立した参照の数え方)
# ------------------------------------------------------


def test_random_arrivals_never_exceed_the_limits(secret_arn, config, github) -> None:
    rng = random.Random(926)
    limits = decided(
        new_issue_min_interval_minutes=120,
        new_issue_limit_per_fingerprint=2,
        new_issue_limit_global=4,
        auto_stop_denied_threshold=10_000,  # 自動停止は別のテストで見る
    )
    fps = [f"{i:064x}" for i in range(1, 5)]
    for fp in fps:
        line_row(fp)
    occurrence = dict.fromkeys(fps, 0)
    clock = _NOW
    created: list[tuple[str, dt.datetime]] = []
    for _ in range(250):
        clock += dt.timedelta(minutes=rng.choice([1, 5, 20, 45, 90, 200, 700]))
        fp = rng.choice(fps)
        if rng.random() < 0.5:
            occurrence[fp] += 1
        before = github.creations
        run(
            secret_arn, config, limits, fingerprint=fp, occurrence=max(1, occurrence[fp]), now=clock
        )
        if github.creations > before:
            created.append((fp, clock))
        if rng.random() < 0.8:
            github.close_all()  # 人が閉じる
    assert created, "少なくとも 1 件は作られる(空振りしていない)"
    jst = lambda t: (t + dt.timedelta(hours=9)).date()  # noqa: E731 - 独立した日付の数え方
    for fp in fps:
        mine = [t for f, t in created if f == fp]
        for day in {jst(t) for t in mine}:
            assert sum(1 for t in mine if jst(t) == day) <= 2
        for a, b in zip(mine, mine[1:], strict=False):
            assert b - a >= dt.timedelta(minutes=120)
    for day in {jst(t) for _, t in created}:
        assert sum(1 for _, t in created if jst(t) == day) <= 4


# --- 変異試験で見つかった穴 -------------------------------------------------------------------


def test_a_per_fingerprint_denial_after_the_claim_never_counts_toward_the_auto_stop(
    secret_arn, config, github
) -> None:
    limits = decided(
        new_issue_min_interval_minutes=1,
        new_issue_limit_per_fingerprint=1,
        auto_stop_denied_threshold=1,
    )
    line_row(_FP_A)
    run(secret_arn, config, limits)
    github.close_all()
    later = _NOW + dt.timedelta(minutes=5)
    run(secret_arn, config, limits, occurrence=2, now=later)  # 事前確認は通り、fp の上限で拒否
    assert state()["github_issue_last_denied_reason"] == "FINGERPRINT_LIMIT"
    control = tracker.read_github_issue_control()
    assert control is not None and not tracker.is_kill_switch_on(control)
    assert int(control.get("global_denied_count", 0)) == 0


def test_a_stale_claim_recreation_records_the_gate_inputs(secret_arn, config, github) -> None:
    line_row(_FP_A)
    tracker.try_claim_new_github_issue_creation(
        _FP_A, _NOW, config.github_issue_claim_timeout_minutes
    )
    later = _NOW + dt.timedelta(minutes=config.github_issue_claim_timeout_minutes + 1)
    run(secret_arn, config, decided(), occurrence=3, now=later)
    st = state()
    assert st["github_issue_create_status"] == "CREATED"
    assert st["github_issue_last_created_at"] == later.isoformat()
    assert int(st["github_issue_last_created_occurrence"]) == 3
    # 次の同じ occurrence は作られない(復旧の経路でも gate が効く)
    github.close_all()
    run(secret_arn, config, decided(), occurrence=3, now=later + dt.timedelta(hours=7))
    assert github.creations == 1


def test_a_failing_fingerprint_refund_does_not_stop_the_global_refund(
    monkeypatch: pytest.MonkeyPatch, secret_arn, config, github
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("dynamodb")

    monkeypatch.setattr(tracker, "refund_fingerprint_budget", boom)
    github.fail_create = True
    line_row(_FP_A)
    run(secret_arn, config, decided())
    control = tracker.read_github_issue_control()
    assert control is not None and int(control["global_created_count"]) == 0


def test_a_failing_global_refund_does_not_hide_the_fingerprint_refund(
    monkeypatch: pytest.MonkeyPatch, secret_arn, config, github
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("dynamodb")

    monkeypatch.setattr(tracker, "refund_global_budget", boom)
    github.fail_create = True
    line_row(_FP_A)
    run(secret_arn, config, decided())
    assert int(state().get("github_issue_window_created_count", 0)) == 0


# --- stale 復旧は、上限を超えて起票しない・二重に消費しない(PR #928 の独立 review の SHOULD-1) ---


def _later(config: Any) -> dt.datetime:
    return _NOW + dt.timedelta(minutes=config.github_issue_claim_timeout_minutes + 1)


def _crash_after_claim(config: Any, fingerprint: str) -> None:
    """claim を取った直後(予算の消費の前)に落ちた起動の状態を作る。"""
    line_row(fingerprint)
    assert tracker.try_claim_new_github_issue_creation(
        fingerprint, _NOW, config.github_issue_claim_timeout_minutes
    )


def _crash_after_consumption(config: Any, fingerprint: str) -> None:
    """予算を消費して印を残した直後(作成の前)に落ちた起動の状態を作る。"""
    _crash_after_claim(config, fingerprint)
    window = guard.window_id(_NOW)
    assert tracker.try_consume_fingerprint_budget(fingerprint, window, 2)
    assert tracker.try_consume_global_budget(window, 5)
    tracker.mark_github_issue_budget_consumed(fingerprint, window)


def _counts(fingerprint: str) -> tuple[int, int]:
    control = tracker.read_github_issue_control() or {}
    return (
        int(state(fingerprint).get("github_issue_window_created_count", 0)),
        int(control.get("global_created_count", 0)),
    )


def test_a_recovery_after_a_crash_before_consumption_never_exceeds_the_global_limit(
    secret_arn, config, github
) -> None:
    limits = decided(new_issue_limit_global=1)
    line_row(_FP_A)
    run(secret_arn, config, limits, fingerprint=_FP_A)  # 全体の上限(1)を使い切る
    _crash_after_claim(config, _FP_B)
    run(secret_arn, config, limits, fingerprint=_FP_B, now=_later(config))
    assert github.creations == 1  # 復旧が上限を超えて 2 件目を作らない
    assert state(_FP_B)["github_issue_create_status"] == "SUPPRESSED"
    assert state(_FP_B)["github_issue_last_denied_reason"] == "GLOBAL_LIMIT"
    assert "github_issue_claimed_at" not in state(_FP_B)


def test_a_recovery_after_a_crash_before_consumption_consumes_exactly_once(
    secret_arn, config, github
) -> None:
    _crash_after_claim(config, _FP_A)
    assert _counts(_FP_A) == (0, 0)
    run(secret_arn, config, decided(), fingerprint=_FP_A, occurrence=2, now=_later(config))
    assert github.creations == 1
    assert state()["github_issue_create_status"] == "CREATED"
    assert _counts(_FP_A) == (1, 1)
    assert "github_issue_budget_window" not in state()


def test_a_recovery_after_a_crash_before_consumption_respects_the_fingerprint_limit(
    secret_arn, config, github
) -> None:
    limits = decided(new_issue_limit_per_fingerprint=1, new_issue_min_interval_minutes=1)
    line_row(_FP_A)
    run(secret_arn, config, limits)  # この fingerprint の 1 窓の上限(1)を使い切る
    github.close_all()
    tracker.mark_github_issue_creation_failed(_FP_A)  # 次の claim ができる状態へ
    assert tracker.try_claim_new_github_issue_creation(
        _FP_A, _NOW, config.github_issue_claim_timeout_minutes
    )
    run(secret_arn, config, limits, occurrence=2, now=_later(config))
    assert github.creations == 1
    assert state()["github_issue_last_denied_reason"] == "FINGERPRINT_LIMIT"


def test_a_recovery_after_a_crash_after_consumption_does_not_consume_again(
    secret_arn, config, github
) -> None:
    _crash_after_consumption(config, _FP_A)
    assert _counts(_FP_A) == (1, 1)
    run(secret_arn, config, decided(), fingerprint=_FP_A, occurrence=2, now=_later(config))
    assert github.creations == 1
    assert state()["github_issue_create_status"] == "CREATED"
    assert _counts(_FP_A) == (1, 1)  # 二重に消費しない
    assert "github_issue_budget_window" not in state()


def test_a_budget_mark_from_another_window_is_not_trusted(secret_arn, config, github) -> None:
    _crash_after_claim(config, _FP_A)
    tracker.mark_github_issue_budget_consumed(_FP_A, "2026-10-11")  # 前日の窓で消費した印
    run(secret_arn, config, decided(), occurrence=2, now=_later(config))
    assert github.creations == 1
    assert _counts(_FP_A) == (1, 1)  # 今の窓では、改めて消費する


def test_a_failed_recovery_returns_the_budget_and_clears_the_mark(
    secret_arn, config, github
) -> None:
    _crash_after_consumption(config, _FP_A)
    github.fail_create = True
    run(secret_arn, config, decided(), occurrence=2, now=_later(config))
    assert state()["github_issue_create_status"] == "ISSUE_CREATION_FAILED"
    assert "github_issue_budget_window" not in state()
    assert _counts(_FP_A) == (0, 0)  # 印を消してから返す = 後の復旧が消費せずに起票しない


def test_the_budget_mark_is_present_while_the_issue_is_being_created(
    secret_arn, config, github
) -> None:
    seen: list[object] = []
    github.before_create = lambda: seen.append(state().get("github_issue_budget_window"))
    line_row(_FP_A)
    run(secret_arn, config, decided())
    assert seen == [guard.window_id(_NOW)]
    assert "github_issue_budget_window" not in state()  # 作成後は消える


def test_an_unexpected_error_after_consumption_clears_the_mark_and_returns_the_budget(
    monkeypatch: pytest.MonkeyPatch, secret_arn, config, github
) -> None:
    def boom(*a: object, **k: object) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(service, "build_incident_issue_title", boom)
    line_row(_FP_A)
    outcome = run(secret_arn, config, decided())
    assert outcome == guard.IssueGuardOutcome()  # 例外は伝播しない
    st = state()
    assert (
        st["github_issue_create_status"] == "CREATING"
    )  # 終端の状態にはならない(stale 復旧の対象)
    assert "github_issue_budget_window" not in st
    assert _counts(_FP_A) == (0, 0)
    assert github.creations == 0


def test_when_the_mark_cannot_be_cleared_the_budget_stays_consumed(
    monkeypatch: pytest.MonkeyPatch, secret_arn, config, github
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("dynamodb")

    monkeypatch.setattr(tracker, "clear_github_issue_budget_window", boom)
    github.fail_create = True
    line_row(_FP_A)
    run(secret_arn, config, decided())
    # 印が残るなら、返さない(返すと、後の復旧が『消費済み』とみなして上限を超える)
    assert _counts(_FP_A) == (1, 1)
