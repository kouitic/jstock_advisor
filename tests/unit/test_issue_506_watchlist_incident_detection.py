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
import logging
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
_SUN = dt.date(2026, 9, 13)  # 非営業日(週末)


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


# --- 1日1回抑止・S-4状態永続化(BatchRunsTableの状態) ----------------------------


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


# --- S-4: 候補ユニバース連続失敗(#506レビューF1是正: 本日分のみを見て、営業日
# ごとに1回だけstreakを積み上げる永続状態。過去のBatchRunsTable行は読み返さない) --


def _evaluate_day(date: dt.date, *, failed: bool, hour_jst: int = 7) -> int:
    now = _now_jst(date, hour_jst)
    todays_batches = [_batch(date, failure_reason="universe_load_failed")] if failed else []
    return handler_module._evaluate_and_persist_universe_load_failure_streak(
        todays_batches, _calendar(), now
    )


def test_universe_load_failure_streak_below_threshold_is_not_detected(dynamo) -> None:
    _evaluate_day(_TUE, failed=True)
    streak = _evaluate_day(_WED, failed=True)
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        streak, _now_jst(_WED, 7)
    )
    assert streak == 2
    assert result is None  # 2営業日連続(閾値3未満)


def test_universe_load_failure_streak_at_threshold_is_detected(dynamo) -> None:
    _evaluate_day(_TUE, failed=True)
    _evaluate_day(_WED, failed=True)
    streak = _evaluate_day(_THU, failed=True)
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        streak, _now_jst(_THU, 7)
    )
    assert streak == 3
    assert result is not None
    assert result["consecutive_days"] == 3
    assert result["failure_count"] == 3
    assert result["is_ongoing"] is True
    assert result["reason_code"] == "watchlist_universe_load_failure_streak"


def test_universe_load_failure_streak_worsening_keeps_same_reason_code(dynamo) -> None:
    """★ 3日→5日と悪化しても、error_type/fingerprintの入力となるreason_codeは
    変わらない(#501/#502の設計どおり。悪化はconsecutive_days/failure_countで表す)。
    """
    _evaluate_day(_TUE, failed=True)
    _evaluate_day(_WED, failed=True)
    streak_3 = _evaluate_day(_THU, failed=True)
    result_3 = handler_module._detect_watchlist_universe_load_failure_streak(
        streak_3, _now_jst(_THU, 7)
    )

    _evaluate_day(_FRI, failed=True)
    streak_5 = _evaluate_day(_NEXT_MON, failed=True)
    result_5 = handler_module._detect_watchlist_universe_load_failure_streak(
        streak_5, _now_jst(_NEXT_MON, 7)
    )
    assert result_3 is not None
    assert result_5 is not None
    assert result_3["reason_code"] == result_5["reason_code"]
    assert streak_5 == 5
    assert result_5["consecutive_days"] == 5


def test_universe_load_failure_streak_spans_weekend_without_resetting(dynamo) -> None:
    """営業日ベースで数えるため、金曜failed→(週末は非営業日でスキップ)→月曜failed→
    火曜failedは3営業日連続として数える(休場日を「連続を切る日」にしない)。
    """
    _evaluate_day(_FRI, failed=True)
    _evaluate_day(_NEXT_MON, failed=True)
    streak = _evaluate_day(_NEXT_TUE, failed=True)
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        streak, _now_jst(_NEXT_TUE, 7)
    )
    assert result is not None
    assert result["consecutive_days"] == 3


def test_universe_load_failure_streak_gap_resets_the_streak(dynamo) -> None:
    """火曜success(failure_reason無し)を挟むと連続が切れる。"""
    _evaluate_day(_MON, failed=True)
    _evaluate_day(_TUE, failed=False)  # 成功
    _evaluate_day(_WED, failed=True)
    streak = _evaluate_day(_THU, failed=True)
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        streak, _now_jst(_THU, 7)
    )
    assert streak == 2  # 水木の2営業日連続のみ(閾値3未満)
    assert result is None


def test_universe_load_failure_streak_not_evaluated_on_non_business_day(dynamo) -> None:
    """非営業日は評価自体をskipし、永続状態(streak_count)を変えない。

    ★ #506レビューiteration 2 R1是正の直接固定: 戻り値は前回の営業日の値
    (例: 2)をそのまま返してはならない(それを閾値判定へ流すと、非営業日にも
    関わらず「本日確定した3営業日連続」として毎日再通知されてしまう。
    「本日はまだ確定していない」ことを表す`None`を返す)。
    """
    _evaluate_day(_WED, failed=True)
    _evaluate_day(_THU, failed=True)
    before = handler_module.get_streak_state(
        handler_module._REASON_CODE_WATCHLIST_UNIVERSE_LOAD_FAILURE_STREAK
    )
    streak_on_saturday = _evaluate_day(_SAT, failed=True)
    after = handler_module.get_streak_state(
        handler_module._REASON_CODE_WATCHLIST_UNIVERSE_LOAD_FAILURE_STREAK
    )
    assert streak_on_saturday is None  # 「本日は未確定」。2のような数値を返さない
    assert after == before  # 永続状態も変化しない(土曜は評価しない)


def test_universe_load_failure_streak_envelope_has_only_allowlisted_keys(dynamo) -> None:
    _evaluate_day(_TUE, failed=True)
    _evaluate_day(_WED, failed=True)
    streak = _evaluate_day(_THU, failed=True)
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        streak, _now_jst(_THU, 7)
    )
    assert result is not None
    assert set(result) <= handler_module._SNS_PAYLOAD_ALLOWLIST


def test_universe_load_failure_streak_ignores_same_day_reevaluation(dynamo) -> None:
    """同じ営業日内で複数回呼ばれても(reconcilerは毎時実行)、1日1回しか
    streak_countを進めない(2回評価しても2にならない)。"""
    first = _evaluate_day(_TUE, failed=True, hour_jst=7)
    second = _evaluate_day(_TUE, failed=True, hour_jst=8)
    assert first == 1
    assert second == 1


def test_universe_load_failure_streak_three_hourly_reruns_in_one_day_do_not_reach_threshold(
    dynamo,
) -> None:
    """★ #506レビューiteration 2 R3の直接固定: 「1営業日1回だけ評価する」guardが
    壊れると、reconcilerが毎時起動するだけで(実際には1日しか失敗していないのに)
    3時間後にstreak=3へ達し「3営業日連続」として誤通知しうる
    (USER決定の閾値が実質無意味になる重大な検知力の穴)。
    """
    streaks = [
        _evaluate_day(_TUE, failed=True, hour_jst=7),
        _evaluate_day(_TUE, failed=True, hour_jst=8),
        _evaluate_day(_TUE, failed=True, hour_jst=9),
    ]
    assert streaks == [1, 1, 1]
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        streaks[-1], _now_jst(_TUE, 9)
    )
    assert result is None  # 1営業日だけの失敗では閾値(3)に達しない


def test_universe_load_failure_streak_resets_on_evaluation_gap_from_downtime(
    dynamo, caplog: pytest.LogCaptureFixture
) -> None:
    """★ #506レビューiteration 2 R4の直接固定: reconcilerが複数営業日停止して
    復帰した場合(直前の評価日と「本日の前営業日」が一致しない)、断絶前の
    streakを引き継がず、本日の結果だけでリセットする(fail-safe)。
    """
    _evaluate_day(_MON, failed=True)
    _evaluate_day(_TUE, failed=True)  # streak=2。ここでreconcilerが停止したとする
    # 水曜の評価は行われない(reconciler停止のシミュレーション。_evaluate_dayを
    # 呼ばない)。

    with caplog.at_level(logging.WARNING, logger=handler_module.logger.name):
        streak = _evaluate_day(_THU, failed=True)

    assert streak == 1  # 断絶前のstreak(2)を引き継がず、本日分だけで再スタート
    assert "evaluation gap" in caplog.text


def _run_universe_load_failure_pass(date: dt.date, *, failed: bool, hour_jst: int = 7) -> bool:
    """reconcilerの1回の呼び出し相当(評価 → 閾値判定 → 1日1回抑止つきpublish)を
    模擬する。戻り値はpublishしたかどうか。
    """
    now = _now_jst(date, hour_jst)
    todays_batches = [_batch(date, failure_reason="universe_load_failed")] if failed else []
    streak = handler_module._evaluate_and_persist_universe_load_failure_streak(
        todays_batches, _calendar(), now
    )
    envelope = handler_module._detect_watchlist_universe_load_failure_streak(streak, now)
    return handler_module._notify_if_new_today(envelope, date, now)


def test_universe_load_failure_streak_does_not_renotify_on_non_business_days(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ #506レビューiteration 2 R1の直接固定(本番実データで確認): 水曜に
    3営業日連続として通知された後、土曜・日曜(非営業日)に新しいbatchが無くても
    再publishしてはならない。1日1回抑止のキーが暦日単位のため、非営業日を
    「新しい1日」として素通りさせるとここが破綻していた(累計1→3)。
    """
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)

    assert _run_universe_load_failure_pass(_MON, failed=True) is False
    assert _run_universe_load_failure_pass(_TUE, failed=True) is False
    assert _run_universe_load_failure_pass(_WED, failed=True) is True  # streak=3、初回通知
    assert len(published) == 1

    assert _run_universe_load_failure_pass(_SAT, failed=True) is False
    assert _run_universe_load_failure_pass(_SUN, failed=True) is False

    assert len(published) == 1  # 累計1のまま(週末の再通知が起きない)


def test_universe_load_failure_streak_does_not_publish_stale_value_before_grace_hour(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ #506レビューiteration 2 R2の直接固定(本番実データで確認): 猶予時刻より
    前の実行が、前日確定したstreakを「本日の状態」として誤ってpublishして
    いた(例: 木曜06:00にconsecutive_days=3・継続中として通知したのに、
    木曜08:00の当日評価ではstreak=0。1日1回抑止のため訂正も届かなかった)。
    """
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)

    assert _run_universe_load_failure_pass(_MON, failed=True) is False
    assert _run_universe_load_failure_pass(_TUE, failed=True) is False
    assert _run_universe_load_failure_pass(_WED, failed=True) is True  # streak=3、初回通知
    assert len(published) == 1

    # 木曜06:00(猶予前)。前日確定値(3)を「本日の状態」としてpublishしない。
    assert _run_universe_load_failure_pass(_THU, failed=False, hour_jst=6) is False
    assert len(published) == 1

    # 木曜08:00(猶予後)。本日は実際には失敗していない(streak=0) → 通知不要。
    assert _run_universe_load_failure_pass(_THU, failed=False, hour_jst=8) is False
    assert len(published) == 1


def test_universe_load_failure_streak_survives_batch_row_ttl_expiry(dynamo) -> None:
    """★ #506レビューF1の直接固定: 過去(火・水)のBatchRunsTable行が既にTTLで
    消えていても(週末を跨ぐ本番実測で確認された事象)、月〜木の4営業日連続を
    正しく検知できること。streak状態は`_evaluate_and_persist_universe_load_failure_streak`
    自身が営業日ごとに積み上げるため、古いbatch行の生死に依存しない。
    """
    _evaluate_day(_MON, failed=True)
    _evaluate_day(_TUE, failed=True)
    # 火曜のbatch行をTTL経過相当として明示的に削除する(本番のTTL遅延・
    # 早期削除いずれの場合も再現するため、ここでは即時削除で近似する)。
    dynamo.delete_item(
        TableName=_BATCH_TABLE,
        Key={"batch_id": {"S": f"watchlist-{_TUE.isoformat()}-abcd1234"}},
    )
    _evaluate_day(_WED, failed=True)
    streak = _evaluate_day(_THU, failed=True)
    result = handler_module._detect_watchlist_universe_load_failure_streak(
        streak, _now_jst(_THU, 7)
    )
    assert streak == 4
    assert result is not None
    assert result["consecutive_days"] == 4


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


def _fake_reconciler_config(
    *, enabled: bool = True, scheduled_run_enabled: bool = True
) -> SimpleNamespace:
    return SimpleNamespace(
        holiday_calendar=SimpleNamespace(
            recurring_market_closures=SimpleNamespace(dates_mm_dd=[]),
            additional_closures=SimpleNamespace(dates=[]),
        ),
        watchlist_screening=SimpleNamespace(
            enabled=enabled, scheduled_run_enabled=scheduled_run_enabled
        ),
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


@pytest.mark.parametrize(
    ("enabled", "scheduled_run_enabled"),
    [(False, True), (True, False), (False, False)],
)
def test_detect_and_notify_skips_entirely_when_scheduled_dispatch_disabled(
    dynamo, monkeypatch: pytest.MonkeyPatch, enabled: bool, scheduled_run_enabled: bool
) -> None:
    """★ #506レビューF2の直接固定: dispatcherが早期returnして一切batch行を
    作らない(kill switch OFF)の間、S-2は「missed schedule」を誤検知しては
    ならない(#132本文4節: 仕様どおりのfail-softは障害として扱わない)。
    """
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)
    config = _fake_reconciler_config(enabled=enabled, scheduled_run_enabled=scheduled_run_enabled)
    now = _now_jst(_MON, 7)  # 営業日・猶予後・BatchRunsTableは空(dispatcher停止中)

    result = handler_module._detect_and_notify_watchlist_incidents(now, config)

    assert result == {
        "missed_schedule_notified": False,
        "universe_load_failure_streak_notified": False,
    }
    assert published == []


def test_detect_and_notify_resumes_missed_schedule_detection_once_reenabled(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kill switchが有効に戻れば、通常どおりmissed scheduleを検知する
    (F2是正が「恒久的に検知しなくなる」副作用を持たないことの確認)。"""
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)
    now = _now_jst(_MON, 7)

    disabled_result = handler_module._detect_and_notify_watchlist_incidents(
        now, _fake_reconciler_config(enabled=False)
    )
    enabled_result = handler_module._detect_and_notify_watchlist_incidents(
        now, _fake_reconciler_config(enabled=True)
    )

    assert disabled_result["missed_schedule_notified"] is False
    assert enabled_result["missed_schedule_notified"] is True
    assert len(published) == 1


# --- #506レビュー非BLOCKING指摘(D1/D2/D4)の直接固定 ------------------------------


def test_list_new_candidate_screening_batches_includes_completed_and_aborted(dynamo) -> None:
    """D1是正: list_watchlist_batches_by_status()と違い、COMPLETED/ABORTEDを含む
    全statusを対象にすること(missed schedule検知が正常終了したバッチも
    「起動された」側として数えられなければならない)。
    """
    dynamo.put_item(
        TableName=_BATCH_TABLE,
        Item={
            "batch_id": {"S": "watchlist-completed-1"},
            "status": {"S": "COMPLETED"},
            "started_at": {"S": _now_jst(_MON, 6).isoformat()},
        },
    )
    dynamo.put_item(
        TableName=_BATCH_TABLE,
        Item={
            "batch_id": {"S": "watchlist-aborted-1"},
            "status": {"S": "ABORTED"},
            "started_at": {"S": _now_jst(_MON, 6).isoformat()},
        },
    )

    items = batch_tracker.list_new_candidate_screening_batches()

    batch_ids = {item["batch_id"] for item in items}
    assert "watchlist-completed-1" in batch_ids
    assert "watchlist-aborted-1" in batch_ids


class _FakePaginatedTable:
    """`_table().scan()`のページングだけを模擬する最小フェイク(motoを使わない)。"""

    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages
        self.calls: list[dict] = []

    def scan(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return self._pages[len(self.calls) - 1]


def test_list_new_candidate_screening_batches_completes_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4是正: LastEvaluatedKeyが返る限りScanを繰り返し、全ページを結合すること。
    S-2は「0件」を根拠に通知するため、pagination打ち切りはfalse positiveに
    直結する(#506レビュー指摘)。
    """
    page1 = {
        "Items": [{"batch_id": "watchlist-1"}],
        "LastEvaluatedKey": {"batch_id": "watchlist-1"},
    }
    page2 = {"Items": [{"batch_id": "watchlist-2"}]}
    fake_table = _FakePaginatedTable([page1, page2])
    monkeypatch.setattr(batch_tracker, "_table", lambda: fake_table)

    items = batch_tracker.list_new_candidate_screening_batches()

    assert {item["batch_id"] for item in items} == {"watchlist-1", "watchlist-2"}
    assert len(fake_table.calls) == 2
    assert "ExclusiveStartKey" not in fake_table.calls[0]
    assert fake_table.calls[1]["ExclusiveStartKey"] == {"batch_id": "watchlist-1"}


class _RecordingSnsClient:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, **kwargs: object) -> None:
        self.published.append(kwargs)


def test_publish_incident_envelope_rejects_non_allowlisted_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2是正: SNS publish直前のallowlist検査(最後の防御)を直接固定する。
    許可外のキー(例: stock_code)が混入していたらpublishせず例外にする。
    """
    fake_sns = _RecordingSnsClient()
    monkeypatch.setattr(handler_module.boto3, "client", lambda _service: fake_sns)
    monkeypatch.setenv("INCIDENT_NOTIFICATION_TOPIC_ARN", "arn:aws:sns:ap-northeast-1:1:topic")
    envelope = {"reason_code": "watchlist_missed_schedule", "stock_code": "1111"}

    with pytest.raises(ValueError, match="non-allowlisted"):
        handler_module._publish_incident_envelope(envelope)

    assert fake_sns.published == []


def test_publish_incident_envelope_publishes_allowlisted_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sns = _RecordingSnsClient()
    monkeypatch.setattr(handler_module.boto3, "client", lambda _service: fake_sns)
    monkeypatch.setenv("INCIDENT_NOTIFICATION_TOPIC_ARN", "arn:aws:sns:ap-northeast-1:1:topic")
    envelope = {"reason_code": "watchlist_missed_schedule", "source": "watchlist_reconciler"}

    handler_module._publish_incident_envelope(envelope)

    assert len(fake_sns.published) == 1
    assert fake_sns.published[0]["TopicArn"] == "arn:aws:sns:ap-northeast-1:1:topic"
