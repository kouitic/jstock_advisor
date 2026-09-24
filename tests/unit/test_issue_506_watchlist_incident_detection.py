"""Issue #506(#132 O-1): reconcilerによるS-2(missed schedule)/ S-4(候補ユニバース
連続失敗)検知の単体テスト。

USER決定(#506 issuecomment-5805034769)の要点をそのまま検証する:

    1 対象はwatchlist系のNEW_CANDIDATE_SCREENINGのみ
    2 S-4の閾値は3営業日連続
    3 通知はreason_codeごとに1日1回(JST)。BatchRunsTable(既存)へ状態を保持する
    4 envelopeはallowlist(source/job_name/failure_stage/failure_type/reason_code/
      occurred_at/failure_count/consecutive_days/is_ongoing)以外のキーを持たない
    5 正常なfail-soft(週末・祝日で候補が無い)は障害として扱わない(#132本文4節)
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.lambda_handlers import watchlist_batch_reconciler_handler as handler_module

_REGION = "ap-northeast-1"
_BATCH_TABLE = "jstock-batch_runs"

# 2026-09-07(月)〜09-11(金)はいずれもJPXの営業日(祝日なし。実測済み)。
_MON = dt.date(2026, 9, 7)
_TUE = dt.date(2026, 9, 8)
_WED = dt.date(2026, 9, 9)
_THU = dt.date(2026, 9, 10)
_FRI = dt.date(2026, 9, 11)
_SAT = dt.date(2026, 9, 12)  # 非営業日(週末)
_NEXT_MON = dt.date(2026, 9, 14)  # _FRIの次の営業日(週末を挟む)
_NEXT_TUE = dt.date(2026, 9, 15)


def _calendar() -> BusinessCalendar:
    from types import SimpleNamespace

    config = SimpleNamespace(
        recurring_market_closures=SimpleNamespace(dates_mm_dd=[]),
        additional_closures=SimpleNamespace(dates=[]),
    )
    return BusinessCalendar.from_config(config)


def _now_jst(date: dt.date, hour_jst: int) -> dt.datetime:
    """指定したJST暦日・JST時のnow(UTC-aware)を作る。"""
    jst_naive = dt.datetime(date.year, date.month, date.day, hour_jst, 0)
    return (jst_naive - dt.timedelta(hours=9)).replace(tzinfo=dt.UTC)


def _batch(date: dt.date, *, prefix: str = "watchlist-", failure_reason: str | None = None) -> dict:
    started_at = _now_jst(date, 6).isoformat()  # JST 06:00(dispatcherの起動時刻)
    item = {"batch_id": f"{prefix}{date.isoformat()}-abcd1234", "started_at": started_at}
    if failure_reason is not None:
        item["failure_reason"] = failure_reason
    return item


# --- S-2: missed schedule -------------------------------------------------------


def test_missed_schedule_not_checked_on_non_business_day() -> None:
    now = _now_jst(_SAT, 12)
    result = handler_module._detect_watchlist_missed_schedule([], _calendar(), now)
    assert result is None


def test_missed_schedule_not_checked_before_grace_hour() -> None:
    now = _now_jst(_MON, 6)  # JST 06:00、猶予(07:00)より前
    result = handler_module._detect_watchlist_missed_schedule([], _calendar(), now)
    assert result is None


def test_missed_schedule_detected_when_no_batch_today() -> None:
    now = _now_jst(_MON, 7)
    result = handler_module._detect_watchlist_missed_schedule([], _calendar(), now)
    assert result is not None
    assert result["reason_code"] == "watchlist_missed_schedule"
    assert result["source"] == "watchlist_reconciler"
    assert result["job_name"] == "watchlist-dispatcher"


def test_missed_schedule_not_detected_when_batch_started_today_even_if_failed() -> None:
    """★ 起動後に失敗した(DISPATCH_FAILED等)試行は「起動されなかった」ではない。"""
    now = _now_jst(_MON, 7)
    batches = [_batch(_MON, failure_reason="universe_load_failed")]
    result = handler_module._detect_watchlist_missed_schedule(batches, _calendar(), now)
    assert result is None


def test_missed_schedule_detected_when_only_other_days_have_batches() -> None:
    now = _now_jst(_TUE, 7)
    batches = [_batch(_MON)]  # 前営業日のみ。本日分が無い
    result = handler_module._detect_watchlist_missed_schedule(batches, _calendar(), now)
    assert result is not None


def test_missed_schedule_envelope_has_only_allowlisted_keys() -> None:
    now = _now_jst(_MON, 7)
    result = handler_module._detect_watchlist_missed_schedule([], _calendar(), now)
    assert result is not None
    assert set(result) <= handler_module._SNS_PAYLOAD_ALLOWLIST


# --- S-4: 候補ユニバース連続失敗 -------------------------------------------------


def test_universe_load_failure_streak_below_threshold_is_not_detected() -> None:
    now = _now_jst(_WED, 7)
    batches = [
        _batch(_TUE, failure_reason="universe_load_failed"),
        _batch(_WED, failure_reason="universe_load_failed"),
    ]
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        batches, _calendar(), now
    )
    assert result is None  # 2営業日連続(閾値3未満)


def test_universe_load_failure_streak_at_threshold_is_detected() -> None:
    now = _now_jst(_THU, 7)
    batches = [
        _batch(_TUE, failure_reason="universe_load_failed"),
        _batch(_WED, failure_reason="universe_load_failed"),
        _batch(_THU, failure_reason="universe_load_failed"),
    ]
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        batches, _calendar(), now
    )
    assert result is not None
    assert result["consecutive_days"] == 3
    assert result["failure_count"] == 3
    assert result["is_ongoing"] is True
    assert result["reason_code"] == "watchlist_universe_load_failure_streak"


def test_universe_load_failure_streak_worsening_keeps_same_reason_code() -> None:
    """★ 3日→5日と悪化しても、error_type/fingerprintの入力となるreason_codeは
    変わらない(#501/#502の設計どおり。悪化はconsecutive_days/failure_countで表す)。
    """
    now_3 = _now_jst(_THU, 7)
    batches_3 = [
        _batch(_TUE, failure_reason="universe_load_failed"),
        _batch(_WED, failure_reason="universe_load_failed"),
        _batch(_THU, failure_reason="universe_load_failed"),
    ]
    result_3 = handler_module._detect_watchlist_universe_load_failure_streak(
        batches_3, _calendar(), now_3
    )

    now_5 = _now_jst(_SAT + dt.timedelta(days=2), 7)  # 翌週月曜(月火水木金の5営業日連続)
    batches_5 = batches_3 + [
        _batch(_FRI, failure_reason="universe_load_failed"),
        _batch(_SAT + dt.timedelta(days=2), failure_reason="universe_load_failed"),
    ]
    result_5 = handler_module._detect_watchlist_universe_load_failure_streak(
        batches_5, _calendar(), now_5
    )
    assert result_3 is not None
    assert result_5 is not None
    assert result_3["reason_code"] == result_5["reason_code"]
    assert result_5["consecutive_days"] == 5


def test_universe_load_failure_streak_spans_weekend_without_resetting() -> None:
    """営業日ベースで数えるため、金曜failed→(週末は非営業日でスキップ)→月曜failed→
    火曜failedは3営業日連続として数える(休場日を「連続を切る日」にしない)。
    """
    now = _now_jst(_NEXT_TUE, 7)
    batches = [
        _batch(_FRI, failure_reason="universe_load_failed"),
        _batch(_NEXT_MON, failure_reason="universe_load_failed"),
        _batch(_NEXT_TUE, failure_reason="universe_load_failed"),
    ]
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        batches, _calendar(), now
    )
    assert result is not None
    assert result["consecutive_days"] == 3


def test_universe_load_failure_streak_gap_resets_the_streak() -> None:
    """火曜success(failure_reason無し)を挟むと連続が切れる。"""
    now = _now_jst(_THU, 7)
    batches = [
        _batch(_MON, failure_reason="universe_load_failed"),
        _batch(_TUE),  # 成功(failure_reasonなし)
        _batch(_WED, failure_reason="universe_load_failed"),
        _batch(_THU, failure_reason="universe_load_failed"),
    ]
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        batches, _calendar(), now
    )
    assert result is None  # 水木の2営業日連続のみ(閾値3未満)


def test_universe_load_failure_streak_not_checked_on_non_business_day() -> None:
    now = _now_jst(_SAT, 7)
    batches = [
        _batch(_WED, failure_reason="universe_load_failed"),
        _batch(_THU, failure_reason="universe_load_failed"),
        _batch(_FRI, failure_reason="universe_load_failed"),
    ]
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        batches, _calendar(), now
    )
    assert result is None


def test_universe_load_failure_streak_envelope_has_only_allowlisted_keys() -> None:
    now = _now_jst(_THU, 7)
    batches = [
        _batch(_TUE, failure_reason="universe_load_failed"),
        _batch(_WED, failure_reason="universe_load_failed"),
        _batch(_THU, failure_reason="universe_load_failed"),
    ]
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        batches, _calendar(), now
    )
    assert result is not None
    assert set(result) <= handler_module._SNS_PAYLOAD_ALLOWLIST


# --- 1日1回抑止(BatchRunsTableの状態) -------------------------------------------


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_BATCH_TABLE,
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def test_get_incident_detector_state_is_none_when_never_recorded(dynamo) -> None:
    assert batch_tracker.get_incident_detector_state("watchlist_missed_schedule") is None


def test_record_and_get_incident_detector_state_round_trips(dynamo) -> None:
    now = _now_jst(_MON, 7)
    batch_tracker.record_incident_detector_state(
        "watchlist_missed_schedule", "2026-09-07", None, now
    )
    state = batch_tracker.get_incident_detector_state("watchlist_missed_schedule")
    assert state is not None
    assert state["last_notified_date_jst"] == "2026-09-07"


def test_already_notified_today_is_reason_code_specific(dynamo) -> None:
    now = _now_jst(_MON, 7)
    batch_tracker.record_incident_detector_state(
        "watchlist_missed_schedule", "2026-09-07", None, now
    )
    assert handler_module._already_notified_today("watchlist_missed_schedule", _MON) is True
    assert (
        handler_module._already_notified_today("watchlist_universe_load_failure_streak", _MON)
        is False
    )
    assert handler_module._already_notified_today("watchlist_missed_schedule", _TUE) is False


def test_notify_if_new_today_publishes_once_per_day(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)
    now = _now_jst(_MON, 7)
    envelope = {"reason_code": "watchlist_missed_schedule", "source": "watchlist_reconciler"}

    first = handler_module._notify_if_new_today(envelope, _MON, now)
    second = handler_module._notify_if_new_today(envelope, _MON, now)

    assert first is True
    assert second is False  # 同日2回目は抑止される
    assert len(published) == 1


def test_notify_if_new_today_is_noop_when_no_signal(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)
    result = handler_module._notify_if_new_today(None, _MON, _now_jst(_MON, 7))
    assert result is False
    assert published == []


# --- _detect_and_notify_watchlist_incidents(): scan → 検知 → 1日1回抑止の統合 -----


def _fake_reconciler_config() -> SimpleNamespace:
    return SimpleNamespace(
        holiday_calendar=SimpleNamespace(
            recurring_market_closures=SimpleNamespace(dates_mm_dd=[]),
            additional_closures=SimpleNamespace(dates=[]),
        )
    )


def test_detect_and_notify_publishes_missed_schedule_once_per_day(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)
    config = _fake_reconciler_config()
    now = _now_jst(_MON, 7)  # 営業日・猶予後・BatchRunsTableは空

    first = handler_module._detect_and_notify_watchlist_incidents(now, config)
    second = handler_module._detect_and_notify_watchlist_incidents(now, config)

    assert first == {
        "missed_schedule_notified": True,
        "universe_load_failure_streak_notified": False,
    }
    assert second == {
        "missed_schedule_notified": False,  # 同日2回目は1日1回抑止で送られない
        "universe_load_failure_streak_notified": False,
    }
    assert len(published) == 1
    assert published[0]["reason_code"] == "watchlist_missed_schedule"


def test_detect_and_notify_finds_no_missed_schedule_when_real_batch_started_today(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_new_candidate_screening_batches()の実スキャン経由でも、本日開始した
    実際のbatch_id("watchlist-"接頭辞)があればmissed scheduleを検知しないこと。
    """
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)
    config = _fake_reconciler_config()
    now = _now_jst(_MON, 7)
    batch_tracker.try_acquire_dispatch_lease(
        f"watchlist-{_MON.isoformat()}T060000-abcd1234", "dispatcher", now, 360, 72
    )

    result = handler_module._detect_and_notify_watchlist_incidents(now, config)

    assert result["missed_schedule_notified"] is False
    assert published == []
