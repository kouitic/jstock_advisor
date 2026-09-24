"""Issue #507(#132 U-5): reconcilerによるS-6(queue backlog)/ S-7(watchlist削除
実績ゼロ連続)検知の単体テスト。

USER決定(HANAKO-20260924-USERDECISION-507)の要点をそのまま検証する:

    1 S-6の主指標はSQS OldestMessageAge。閾値>600秒が15分以上継続
      (Period=300sなら3 datapoint連続相当)。throttle_rateは補助指標のみで
      単独ではincidentにしない(SNS envelopeに含めない)
    2 S-7は削除実績0件が3営業日連続でwarning(閾値3営業日)
    3 いずれも#506と同じ経路(#503)・同じ1日1回抑止・同じallowlistを再利用する
    4 永続化は既存BatchRunsTableのみ(get_streak_state/record_streak_state)。
      新規Table禁止
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.watchlist import WatchlistRemovalHistory
from jstock_advisor.infrastructure.local_repository.watchlist_removal_history_repository import (
    WatchlistRemovalHistoryRepository,
)
from jstock_advisor.lambda_handlers import watchlist_batch_reconciler_handler as handler_module

_REGION = "ap-northeast-1"
_BATCH_TABLE = "jstock-batch_runs"

# 2026-09-07(月)〜09-11(金)はいずれもJPXの営業日(祝日なし。実測済み。#506と同じ週)。
_MON = dt.date(2026, 9, 7)
_TUE = dt.date(2026, 9, 8)
_WED = dt.date(2026, 9, 9)
_THU = dt.date(2026, 9, 10)
_FRI = dt.date(2026, 9, 11)
_SAT = dt.date(2026, 9, 12)  # 非営業日(週末)


def _calendar() -> BusinessCalendar:
    config = SimpleNamespace(
        recurring_market_closures=SimpleNamespace(dates_mm_dd=[]),
        additional_closures=SimpleNamespace(dates=[]),
    )
    return BusinessCalendar.from_config(config)


def _now_jst(date: dt.date, hour_jst: int) -> dt.datetime:
    jst_naive = dt.datetime(date.year, date.month, date.day, hour_jst, 0)
    return (jst_naive - dt.timedelta(hours=9)).replace(tzinfo=dt.UTC)


# --- S-6: queue backlog(SQS OldestMessageAge) -----------------------------------


def _metrics(oldest_message_age: list[float], *, throttles: float = 0, invocations: float = 0):
    return {
        handler_module._METRIC_ID_OLDEST_MESSAGE_AGE: oldest_message_age,
        handler_module._METRIC_ID_THROTTLES: [throttles] if throttles else [],
        handler_module._METRIC_ID_INVOCATIONS: [invocations] if invocations else [],
    }


def test_queue_backlog_detected_when_three_consecutive_datapoints_exceed_threshold() -> None:
    now = _now_jst(_MON, 8)
    metrics = _metrics([601, 700, 900], invocations=100, throttles=10)

    result = handler_module._detect_watchlist_queue_backlog(metrics, now)

    assert result is not None
    assert result["reason_code"] == "watchlist_queue_backlog"
    assert result["source"] == "watchlist_reconciler"
    assert result["job_name"] == "watchlist-worker"


def test_queue_backlog_not_detected_when_fewer_than_three_datapoints() -> None:
    now = _now_jst(_MON, 8)
    metrics = _metrics([700, 900])  # 2件のみ

    result = handler_module._detect_watchlist_queue_backlog(metrics, now)

    assert result is None


def test_queue_backlog_not_detected_when_only_two_of_three_exceed() -> None:
    now = _now_jst(_MON, 8)
    metrics = _metrics([300, 700, 900])  # 3件中1件は閾値未満

    result = handler_module._detect_watchlist_queue_backlog(metrics, now)

    assert result is None


def test_queue_backlog_boundary_exactly_600_is_not_exceeding() -> None:
    """★ 境界の連続性: ちょうど600秒は「超えた」側ではない(>のみ。>=ではない)。"""
    now = _now_jst(_MON, 8)
    metrics = _metrics([600, 601, 700])

    result = handler_module._detect_watchlist_queue_backlog(metrics, now)

    assert result is None


def test_queue_backlog_only_looks_at_the_most_recent_three_datapoints() -> None:
    """4件中、最新3件だけがすべて超過していれば検知する(先頭の低い値は無視)。"""
    now = _now_jst(_MON, 8)
    metrics = _metrics([100, 700, 800, 900])

    result = handler_module._detect_watchlist_queue_backlog(metrics, now)

    assert result is not None


def test_queue_backlog_ignores_zero_invocations_without_crashing() -> None:
    """throttle_rate計算(補助指標)がinvocations=0で0除算しないこと。"""
    now = _now_jst(_MON, 8)
    metrics = _metrics([700, 800, 900], invocations=0, throttles=0)

    result = handler_module._detect_watchlist_queue_backlog(metrics, now)

    assert result is not None


def test_queue_backlog_logs_throttle_rate_when_fully_throttled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ #507レビュー非BLOCKING指摘の直接固定: 全スロットル
    (throttles>0だがinvocations=0。呼び出しが1件も受理されない、最も見たい
    状況)でもthrottle_rateのログが出ること。以前は`if invocations > 0`が
    ガードだったため、この状況ではログが1行も出なかった。
    """
    now = _now_jst(_MON, 8)
    metrics = _metrics([100, 200, 300], invocations=0, throttles=10)

    with caplog.at_level("INFO", logger=handler_module.logger.name):
        handler_module._detect_watchlist_queue_backlog(metrics, now)

    assert "throttle_rate=1.0000" in caplog.text


def test_queue_backlog_envelope_does_not_carry_throttle_rate() -> None:
    """★ USER決定の直接固定: throttle_rateは補助指標のみで、SNS envelopeへは
    含めない(#132 H-30のallowlistに比率を運ぶフィールドが無いため)。
    """
    now = _now_jst(_MON, 8)
    metrics = _metrics([700, 800, 900], invocations=1000, throttles=900)

    result = handler_module._detect_watchlist_queue_backlog(metrics, now)

    assert result is not None
    assert set(result) <= handler_module._SNS_PAYLOAD_ALLOWLIST
    assert "consecutive_days" not in result
    assert "failure_count" not in result


def _timestamps_for(count: int, *, start: dt.datetime | None = None) -> list[dt.datetime]:
    base = start if start is not None else _now_jst(_MON, 7)
    return [base + dt.timedelta(minutes=5 * i) for i in range(count)]


def test_fetch_watchlist_worker_metrics_builds_expected_metric_data_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S-6が観測するNamespace/MetricName/Dimensionsが正しいこと(実測に基づく:
    AWS/SQS ApproximateAgeOfOldestMessage(QueueName)/ AWS/Lambda Throttles・
    Invocations(FunctionName))。ScanBy=TimestampAscendingを明示していること
    (#507レビューF1是正)も固定する。
    """
    monkeypatch.setenv("WATCHLIST_SCREENING_QUEUE_NAME", "my-queue")
    monkeypatch.setenv("WATCHLIST_WORKER_FUNCTION_NAME", "my-stack-watchlist-worker")

    captured: dict[str, object] = {}
    ages_ts = _timestamps_for(3)

    class _FakeCloudWatch:
        def get_metric_data(self, **kwargs: object) -> dict:
            captured.update(kwargs)
            return {
                "MetricDataResults": [
                    {
                        "Id": "oldest_message_age",
                        "Timestamps": ages_ts,
                        "Values": [601.0, 602.0, 603.0],
                    },
                    {"Id": "throttles", "Timestamps": ages_ts[:1], "Values": [5.0]},
                    {"Id": "invocations", "Timestamps": ages_ts[:1], "Values": [100.0]},
                ]
            }

    monkeypatch.setattr(handler_module.boto3, "client", lambda _service: _FakeCloudWatch())

    metrics = handler_module._fetch_watchlist_worker_metrics(_now_jst(_MON, 8))

    assert metrics == {
        "oldest_message_age": [601.0, 602.0, 603.0],
        "throttles": [5.0],
        "invocations": [100.0],
    }
    assert captured["ScanBy"] == "TimestampAscending"
    queries = {q["Id"]: q for q in captured["MetricDataQueries"]}
    oldest = queries["oldest_message_age"]["MetricStat"]
    assert oldest["Metric"]["Namespace"] == "AWS/SQS"
    assert oldest["Metric"]["MetricName"] == "ApproximateAgeOfOldestMessage"
    assert oldest["Metric"]["Dimensions"] == [{"Name": "QueueName", "Value": "my-queue"}]
    assert oldest["Period"] == 300

    throttles = queries["throttles"]["MetricStat"]
    assert throttles["Metric"]["Namespace"] == "AWS/Lambda"
    assert throttles["Metric"]["MetricName"] == "Throttles"
    assert throttles["Metric"]["Dimensions"] == [
        {"Name": "FunctionName", "Value": "my-stack-watchlist-worker"}
    ]
    # #507レビュー非BLOCKING指摘: Periodは問い合わせ窓(20分)と一致させる
    # (以前のPeriod=86400は「本日の累積」であるかのような誤解を招いていた)。
    assert throttles["Period"] == handler_module._QUEUE_BACKLOG_LOOKBACK_MINUTES * 60

    invocations = queries["invocations"]["MetricStat"]
    assert invocations["Metric"]["MetricName"] == "Invocations"


def test_fetch_watchlist_worker_metrics_sorts_by_timestamp_regardless_of_api_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ #507レビューF1の直接固定: GetMetricDataの既定のScanBy
    (TimestampDescending。boto3のAPIモデルに明記)を模してAPIが**降順**
    (新→古)でValues/Timestampsを返してきても、`_fetch_watchlist_worker_metrics`
    は timestamp昇順(古→新)へ並べ替えて返すこと。並べ替えていなければ、
    呼び出し元の`datapoints[-3:]`は最古3点を取ってしまい、直近の滞留を
    検知できない(false negative)。
    """
    monkeypatch.setenv("WATCHLIST_SCREENING_QUEUE_NAME", "my-queue")
    monkeypatch.setenv("WATCHLIST_WORKER_FUNCTION_NAME", "my-stack-watchlist-worker")

    now = _now_jst(_MON, 8)
    # 古い→新しい の実際の時系列(値は昇順のタイムスタンプに対応させる)。
    oldest_first_timestamps = [now - dt.timedelta(minutes=15), now - dt.timedelta(minutes=10), now]
    oldest_first_values = [100.0, 601.0, 700.0]  # 直近2点(601, 700)が閾値超過

    class _DescendingFakeCloudWatch:
        def get_metric_data(self, **kwargs: object) -> dict:
            # AWSの既定(ScanBy未指定 = TimestampDescending)を模して、
            # 新しい→古い の順で返す(呼び出し側がScanByを渡していても、
            # このフェイクは無視してAPIの実際の既定挙動を再現する)。
            return {
                "MetricDataResults": [
                    {
                        "Id": "oldest_message_age",
                        "Timestamps": list(reversed(oldest_first_timestamps)),
                        "Values": list(reversed(oldest_first_values)),
                    },
                    {"Id": "throttles", "Timestamps": [now], "Values": [0.0]},
                    {"Id": "invocations", "Timestamps": [now], "Values": [0.0]},
                ]
            }

    monkeypatch.setattr(
        handler_module.boto3, "client", lambda _service: _DescendingFakeCloudWatch()
    )

    metrics = handler_module._fetch_watchlist_worker_metrics(now)

    # 並べ替え後は古い→新しいの順(=API返却順そのままではない)。
    assert metrics["oldest_message_age"] == oldest_first_values


# --- S-7: watchlist削除実績ゼロの連続営業日 ---------------------------------------


def _removal(date: dt.date, stock_code: str = "1111") -> WatchlistRemovalHistory:
    return WatchlistRemovalHistory(
        stock_code=stock_code,
        removed_at=_now_jst(date, 6),
        removal_reason="test",
        removal_category="IMMEDIATE",
        cooldown_until=_now_jst(date, 6) + dt.timedelta(days=30),
    )


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


def _evaluate_deletion_day(
    date: dt.date, removal_history: list[WatchlistRemovalHistory]
) -> int | None:
    return handler_module._evaluate_and_persist_watchlist_deletion_zero_streak(
        removal_history, _calendar(), _now_jst(date, 8)
    )


def test_deletion_zero_streak_below_threshold_is_not_detected(dynamo) -> None:
    _evaluate_deletion_day(_MON, [])
    streak = _evaluate_deletion_day(_TUE, [])

    result = handler_module._detect_watchlist_deletion_zero_streak(streak, _now_jst(_TUE, 8))

    assert streak == 2
    assert result is None


def test_deletion_zero_streak_at_threshold_is_detected(dynamo) -> None:
    _evaluate_deletion_day(_MON, [])
    _evaluate_deletion_day(_TUE, [])
    streak = _evaluate_deletion_day(_WED, [])

    result = handler_module._detect_watchlist_deletion_zero_streak(streak, _now_jst(_WED, 8))

    assert streak == 3
    assert result is not None
    assert result["reason_code"] == "watchlist_deletion_zero_streak"
    assert result["consecutive_days"] == 3
    assert result["failure_count"] == 3
    assert result["is_ongoing"] is True


def test_deletion_zero_streak_resets_when_a_deletion_happens(dynamo) -> None:
    """★ 火曜に1件でも削除があればstreakは0へ戻る。"""
    _evaluate_deletion_day(_MON, [])
    _evaluate_deletion_day(_TUE, [_removal(_TUE)])
    streak = _evaluate_deletion_day(_WED, [])

    assert streak == 1  # 火曜のリセット後、水曜1日分のみ


def test_deletion_zero_streak_ignores_removals_from_other_days(dynamo) -> None:
    """月曜のみ削除がある履歴でも、火曜・水曜の評価には影響しない
    (`removed_at`のJST暦日で本日分だけを見る)。"""
    history = [_removal(_MON)]
    _evaluate_deletion_day(_MON, history)
    _evaluate_deletion_day(_TUE, history)
    streak = _evaluate_deletion_day(_WED, history)

    assert streak == 2  # 火・水は削除実績が無い(月曜分は本日扱いにならない)


def test_deletion_zero_streak_not_evaluated_on_non_business_day(dynamo) -> None:
    _evaluate_deletion_day(_WED, [])
    _evaluate_deletion_day(_THU, [])
    streak_on_saturday = _evaluate_deletion_day(_SAT, [])

    assert streak_on_saturday is None


def test_deletion_zero_streak_envelope_has_only_allowlisted_keys(dynamo) -> None:
    _evaluate_deletion_day(_MON, [])
    _evaluate_deletion_day(_TUE, [])
    streak = _evaluate_deletion_day(_WED, [])

    result = handler_module._detect_watchlist_deletion_zero_streak(streak, _now_jst(_WED, 8))

    assert result is not None
    assert set(result) <= handler_module._SNS_PAYLOAD_ALLOWLIST


# --- WatchlistRemovalHistoryRepository.list_all() --------------------------------


def test_list_all_returns_every_upserted_item(tmp_path: Path) -> None:
    repo = WatchlistRemovalHistoryRepository(30, store_dir=tmp_path / "removal_history")
    repo.upsert(_removal(_MON, "1111"))
    repo.upsert(_removal(_TUE, "2222"))

    items = repo.list_all()

    assert {item.stock_code for item in items} == {"1111", "2222"}


def test_list_all_is_empty_when_nothing_removed(tmp_path: Path) -> None:
    repo = WatchlistRemovalHistoryRepository(30, store_dir=tmp_path / "removal_history")

    assert repo.list_all() == []


# --- _detect_and_notify_watchlist_incidents(): S-6/S-7の配線統合 -----------------


def _fake_reconciler_config() -> SimpleNamespace:
    return SimpleNamespace(
        holiday_calendar=SimpleNamespace(
            recurring_market_closures=SimpleNamespace(dates_mm_dd=[]),
            additional_closures=SimpleNamespace(dates=[]),
        ),
        watchlist_screening=SimpleNamespace(
            enabled=True,
            scheduled_run_enabled=True,
            auto_removal=SimpleNamespace(readd_cooldown_days=30),
        ),
    )


def test_detect_and_notify_publishes_queue_backlog(dynamo, monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)
    monkeypatch.setattr(
        handler_module,
        "_fetch_watchlist_worker_metrics",
        lambda now: _metrics([700, 800, 900]),
    )
    monkeypatch.setattr(
        handler_module,
        "WatchlistRemovalHistoryRepository",
        lambda *_a, **_kw: SimpleNamespace(list_all=lambda: []),
    )
    now = _now_jst(_MON, 8)

    result = handler_module._detect_and_notify_watchlist_incidents(now, _fake_reconciler_config())

    assert result["queue_backlog_notified"] is True
    assert len(published) >= 1
    assert any(p["reason_code"] == "watchlist_queue_backlog" for p in published)


def test_detect_and_notify_publishes_deletion_zero_streak(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)
    monkeypatch.setattr(handler_module, "_fetch_watchlist_worker_metrics", lambda now: {})
    monkeypatch.setattr(
        handler_module,
        "WatchlistRemovalHistoryRepository",
        lambda *_a, **_kw: SimpleNamespace(list_all=lambda: []),
    )
    config = _fake_reconciler_config()

    handler_module._detect_and_notify_watchlist_incidents(_now_jst(_MON, 8), config)
    handler_module._detect_and_notify_watchlist_incidents(_now_jst(_TUE, 8), config)
    result = handler_module._detect_and_notify_watchlist_incidents(_now_jst(_WED, 8), config)

    assert result["deletion_zero_streak_notified"] is True
    assert any(p["reason_code"] == "watchlist_deletion_zero_streak" for p in published)


def test_detect_and_notify_skips_s6_s7_when_scheduled_dispatch_disabled(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[dict] = []
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", published.append)
    fetch_calls: list[object] = []
    monkeypatch.setattr(
        handler_module,
        "_fetch_watchlist_worker_metrics",
        lambda now: fetch_calls.append(now) or _metrics([700, 800, 900]),
    )
    config = _fake_reconciler_config()
    config.watchlist_screening.enabled = False

    result = handler_module._detect_and_notify_watchlist_incidents(_now_jst(_MON, 8), config)

    assert result == {
        "missed_schedule_notified": False,
        "universe_load_failure_streak_notified": False,
        "queue_backlog_notified": False,
        "deletion_zero_streak_notified": False,
    }
    assert published == []
    assert fetch_calls == []  # kill switch OFFではCloudWatchを呼び出さない
