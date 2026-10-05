"""Issue #818 案B: BuyCandidates dispatcherによるEDINET書類一覧の事前取得。

検証対象(不変条件 = Issue #53のfail-safeを弱めない):
  - 窓内の各日付が1回ずつ取得される(日付あたりの取得を1回にする)
  - 子(別プロセス相当)は新しい成功cacheを再利用し、EDINETを呼ばない
  - 事前取得が失敗しても、失敗はcacheへ保存されない(後続は従来どおり自分で取得を試みる)
  - 後続の取得が失敗すれば、従来どおりFETCH_FAILEDのまま伝わる(成功として通さない)
  - fail-soft(例外でも戻る)/ 時間の上限 / APIキー未設定では何もしない
  - 対象日が、銘柄の走査(finder)が必ず通る日付と一致する

「同時にcold missしたプロセス」は、各プロセスがL2を読んだ時点で空だった状態(各自が別の
空のcacheを持つ)としてモデル化する。実際の競合(スレッド)は使わない(local storeの
同時書込でテストが不安定になるため)。
"""

from __future__ import annotations

import datetime as dt
import inspect
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from jstock_advisor.infrastructure.edinet import window_prefetch
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.document_list_cache import (
    EdinetDailyDocumentListCache,
    EdinetDailyDocumentListCacheRepository,
    EdinetDocumentSource,
)
from jstock_advisor.infrastructure.edinet.scan_window import (
    DEFAULT_REFRESH_WINDOW_DAYS,
    business_days_between,
    compute_scan_start,
)
from jstock_advisor.infrastructure.edinet.types import (
    EdinetDocumentEntry,
    EdinetFailureReason,
    EdinetFetchStatus,
    EdinetListResult,
)
from jstock_advisor.infrastructure.edinet.window_prefetch import (
    ASSUMED_CLIENT_TIMEOUT_SECONDS,
    DEFAULT_BUDGET_SECONDS,
    MAX_ATTEMPTS_PER_DATE,
    RETRY_WAIT_SECONDS,
    prefetch_dates,
    prefetch_recent_document_lists,
    prefetch_recent_document_lists_safely,
)

_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "infra" / "template.yaml"
# 2026-10-05(月)08:00:47 JST = 2026-10-04 23:00:47 UTC(実際の障害日の朝の起動時刻)。
_NOW = dt.datetime(2026, 10, 4, 23, 0, 47, tzinfo=dt.UTC)
_TODAY_JST = dt.date(2026, 10, 5)
_EXPECTED_DATES = [
    dt.date(2026, 9, 28),
    dt.date(2026, 9, 29),
    dt.date(2026, 9, 30),
    dt.date(2026, 10, 1),
    dt.date(2026, 10, 2),
    dt.date(2026, 10, 5),
]

_OK = EdinetListResult(
    EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS,
    [
        EdinetDocumentEntry(
            sec_code="29140",
            doc_id="DOC1",
            doc_type_code="180",
            submit_date_time="2026-10-01 10:00",
        )
    ],
)
_TIMEOUT = EdinetListResult(EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.TIMEOUT)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeClient:
    def __init__(
        self,
        result: EdinetListResult = _OK,
        configured: bool = True,
        raises: Exception | None = None,
        clock: FakeClock | None = None,
        seconds_per_call: float = 0.0,
        script: dict[dt.date, list[EdinetListResult]] | None = None,
    ) -> None:
        self.result = result
        self._script = script or {}
        self._configured = configured
        self._raises = raises
        self._clock = clock
        self._seconds_per_call = seconds_per_call
        self.list_calls: list[dt.date] = []

    @property
    def is_configured(self) -> bool:
        return self._configured

    def list_documents(self, date: dt.date) -> EdinetListResult:
        self.list_calls.append(date)
        if self._clock is not None:
            self._clock.now += self._seconds_per_call
        if self._raises is not None:
            raise self._raises
        queued = self._script.get(date)
        if queued:
            return queued.pop(0)
        return self.result


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """再試行の待ち(time.sleep)で実時間を使わない。"""
    stub = SimpleNamespace(monotonic=time.monotonic, sleep=lambda _seconds: None)
    monkeypatch.setattr(window_prefetch, "time", stub)


def _repo(store_dir: Path) -> EdinetDailyDocumentListCacheRepository:
    return EdinetDailyDocumentListCacheRepository(store_dir=store_dir)


def _source(client: FakeClient, store_dir: Path) -> EdinetDocumentSource:
    return EdinetDocumentSource(
        client=client,  # type: ignore[arg-type]
        repository=_repo(store_dir),
    )


# --- 対象日 ----------------------------------------------------------------


def test_prefetch_dates_for_the_incident_day_are_the_six_weekdays() -> None:
    assert prefetch_dates(_TODAY_JST, DEFAULT_REFRESH_WINDOW_DAYS) == _EXPECTED_DATES


@pytest.mark.parametrize("day", [dt.date(2026, 10, d) for d in (5, 6, 7, 8, 9)])
def test_prefetch_dates_equal_the_dates_every_stock_scan_passes_through(day: dt.date) -> None:
    """finderの`compute_scan_start`は、どの銘柄でも`today - 7日`以前から走査する。
    事前取得の対象日は、その最小の範囲(全銘柄が必ず通る日付)と一致する。
    """
    scan_start = compute_scan_start(
        day,
        previous_newest_scanned=day - dt.timedelta(days=1),
        initial_lookback_days=60,
        refresh_window_days=DEFAULT_REFRESH_WINDOW_DAYS,
    )

    assert prefetch_dates(day, DEFAULT_REFRESH_WINDOW_DAYS) == business_days_between(
        scan_start, day
    )


# --- 取得回数と子の再利用 ---------------------------------------------------


def test_each_window_date_is_fetched_once_and_saved(tmp_path: Path) -> None:
    client = FakeClient()

    summary = prefetch_recent_document_lists(_source(client, tmp_path), _NOW)

    assert client.list_calls == _EXPECTED_DATES
    assert (summary.target_dates, summary.fetched, summary.already_fresh, summary.failed) == (
        6,
        6,
        0,
        0,
    )
    for scan_date in _EXPECTED_DATES:
        cached = _repo(tmp_path).get(scan_date)
        assert cached is not None
        assert cached.fetch_status is EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS


def test_children_reuse_the_prefetched_cache_without_calling_edinet(tmp_path: Path) -> None:
    prefetch_client = FakeClient()
    prefetch_recent_document_lists(_source(prefetch_client, tmp_path), _NOW)

    # 別プロセス相当の子(L1メモ無し・L2は共有)。事前取得の後に始まる。
    child_clients = [FakeClient() for _ in range(8)]
    for client in child_clients:
        child = _source(client, tmp_path)
        for scan_date in _EXPECTED_DATES:
            assert child.list_documents(scan_date, _NOW + dt.timedelta(seconds=30)).succeeded

    assert len(prefetch_client.list_calls) == 6
    assert [client.list_calls for client in child_clients] == [[] for _ in child_clients]


def test_children_that_missed_the_l2_each_fetch_without_prefetch_counterexample(
    tmp_path: Path,
) -> None:
    """反証: 事前取得が無く、複数のプロセスが同時にcold missした状態(各自のL2が空)では、
    プロセス数 x 日付数のEDINET呼び出しになる。上の「子はEDINETを呼ばない」は、事前取得の有無を
    この差で区別できる(事前取得を外すと失敗する)。
    """
    children = [FakeClient() for _ in range(4)]
    for index, client in enumerate(children):
        child = _source(client, tmp_path / f"cold_{index}")
        for scan_date in _EXPECTED_DATES:
            child.list_documents(scan_date, _NOW)

    assert sum(len(client.list_calls) for client in children) == 4 * len(_EXPECTED_DATES)


def test_fresh_success_is_not_fetched_again(tmp_path: Path) -> None:
    client = FakeClient()
    source = _source(client, tmp_path)
    prefetch_recent_document_lists(source, _NOW)

    second = prefetch_recent_document_lists(
        _source(client, tmp_path), _NOW + dt.timedelta(minutes=1)
    )

    assert len(client.list_calls) == 6
    assert (second.fetched, second.already_fresh) == (0, 6)


def test_success_older_than_the_refresh_ttl_is_fetched_again(tmp_path: Path) -> None:
    client = FakeClient()
    prefetch_recent_document_lists(_source(client, tmp_path), _NOW)

    again = prefetch_recent_document_lists(
        _source(client, tmp_path), _NOW + dt.timedelta(minutes=31)
    )

    # 窓内の日付は30分(refresh TTL)を超えると取り直す(既存の規則を変えない)。
    assert again.fetched == 6
    assert len(client.list_calls) == 12


# --- 失敗の扱い(不変条件) ---------------------------------------------------


def test_failed_prefetch_is_not_saved_so_children_try_on_their_own(tmp_path: Path) -> None:
    failing = FakeClient(result=_TIMEOUT)

    summary = prefetch_recent_document_lists(_source(failing, tmp_path), _NOW)

    assert (summary.fetched, summary.failed) == (0, 6)
    assert summary.failure_reasons == (("TIMEOUT", 6),)
    assert all(_repo(tmp_path).get(scan_date) is None for scan_date in _EXPECTED_DATES)

    # 失敗が保存されていないため、後続の子は(従来どおり)自分で取得を試み、成功できる。
    recovered = FakeClient()
    child = _source(recovered, tmp_path)
    assert child.list_documents(_EXPECTED_DATES[1], _NOW + dt.timedelta(seconds=10)).succeeded
    assert recovered.list_calls == [_EXPECTED_DATES[1]]


def test_child_fetch_failure_still_propagates_as_fetch_failed(tmp_path: Path) -> None:
    """取得失敗を成功として通さない(Issue #53): 事前取得が失敗し、子の取得も失敗した場合、
    子には従来どおりFETCH_FAILED(失敗の理由つき)が返る。
    """
    prefetch_recent_document_lists(_source(FakeClient(result=_TIMEOUT), tmp_path), _NOW)

    child = _source(FakeClient(result=_TIMEOUT), tmp_path)
    result = child.list_documents(_EXPECTED_DATES[1], _NOW + dt.timedelta(seconds=10))

    assert not result.succeeded
    assert result.status is EdinetFetchStatus.FETCH_FAILED
    assert result.failure_reason is EdinetFailureReason.TIMEOUT


def test_failed_cache_is_not_treated_as_fresh_by_prefetch_and_is_overwritten(
    tmp_path: Path,
) -> None:
    # 直前(1分前)の失敗がL2に残っている。事前取得は失敗を再利用せず、取り直して成功で上書きする。
    _repo(tmp_path).save(
        EdinetDailyDocumentListCache(
            scan_date=_EXPECTED_DATES[1].isoformat(),
            fetch_status=EdinetFetchStatus.FETCH_FAILED,
            failure_reason=EdinetFailureReason.TIMEOUT,
            fetched_at=_NOW - dt.timedelta(minutes=1),
        )
    )
    client = FakeClient()

    prefetch_recent_document_lists(_source(client, tmp_path), _NOW)

    assert _EXPECTED_DATES[1] in client.list_calls
    cached = _repo(tmp_path).get(_EXPECTED_DATES[1])
    assert cached is not None
    assert cached.fetch_status is EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS


def test_prefetch_does_not_reuse_a_failed_l1_memo_of_an_injected_source(tmp_path: Path) -> None:
    """source を注入する経路: `list_documents` は失敗の結果を L1 memo に入れる(negative TTL の間は
    再利用する)。その source を事前取得へ渡しても、事前取得は L1 の失敗を新しいとみなさず、
    取り直して成功を保存する(L1 の成功判定 `memo.result.succeeded` の固定)。
    """
    client = FakeClient(result=_TIMEOUT)
    source = _source(client, tmp_path)
    assert not source.list_documents(_EXPECTED_DATES[1], _NOW).succeeded
    client.result = _OK  # EDINET が回復した

    summary = prefetch_recent_document_lists(source, _NOW + dt.timedelta(seconds=10))

    assert summary.failed == 0
    assert client.list_calls.count(_EXPECTED_DATES[1]) == 2  # 失敗した 1 回 + 事前取得の取り直し
    cached = _repo(tmp_path).get(_EXPECTED_DATES[1])
    assert cached is not None
    assert cached.fetch_status is EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS


# --- fail-soft / 時間の上限 / 未設定 ---------------------------------------


def test_exception_in_the_client_never_escapes_the_safe_wrapper(tmp_path: Path) -> None:
    client = FakeClient(raises=RuntimeError("boom"))

    summary = prefetch_recent_document_lists_safely(
        _NOW, source_factory=lambda: _source(client, tmp_path)
    )

    assert summary.error is True


def test_exception_in_the_source_factory_never_escapes_the_safe_wrapper() -> None:
    def broken_factory() -> EdinetDocumentSource:
        raise RuntimeError("factory boom")

    summary = prefetch_recent_document_lists_safely(_NOW, source_factory=broken_factory)

    assert summary.error is True


def test_unconfigured_environment_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EDINET_API_KEY", raising=False)

    default_summary = prefetch_recent_document_lists_safely(_NOW)
    client = FakeClient(configured=False)
    explicit = prefetch_recent_document_lists_safely(
        _NOW, source_factory=lambda: _source(client, tmp_path)
    )

    assert default_summary.configured is False
    assert explicit.configured is False
    assert client.list_calls == []


def test_time_budget_stops_starting_new_dates(tmp_path: Path) -> None:
    clock = FakeClock()
    client = FakeClient(clock=clock, seconds_per_call=50.0)

    summary = prefetch_recent_document_lists(
        _source(client, tmp_path), _NOW, budget_seconds=120.0, clock=clock
    )

    # 経過 0・50・100 秒の時点では開始し、150 秒の時点で上限(120 秒)を超えて止まる。
    assert len(client.list_calls) == 3
    assert summary.budget_exceeded is True
    assert summary.skipped_by_budget == 3


def _buy_candidates_timeout_seconds() -> float:
    """infra/template.yaml の BuyCandidatesFunction の Timeout(個別の値。無ければ Globals)。"""

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    template: dict[str, Any] = yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)
    props = template["Resources"]["BuyCandidatesFunction"]["Properties"]
    return float(props.get("Timeout", template["Globals"]["Function"]["Timeout"]))


def test_budget_assumes_the_real_edinet_client_timeout() -> None:
    """時間の上限の前提(client の timeout 15 秒)を、EdinetClient のソースと結び付ける。"""
    source = inspect.getsource(EdinetClient.list_documents)

    assert {float(value) for value in re.findall(r"timeout=(\d+)", source)} == {
        ASSUMED_CLIENT_TIMEOUT_SECONDS
    }


def test_worst_case_edinet_delay_stays_far_below_the_dispatcher_timeout_in_the_template() -> None:
    """EDINET の呼び出しに費やす最悪の時間(予算 + client の timeout)が、template の
    BuyCandidatesFunction の Timeout の 1/4 未満であること。Timeout の値は template から読む
    (直書きしない)。template 側で Timeout を下げて余裕が無くなると、このテストが落ちる。
    L2(DynamoDB)の時間は含まない(module の docstring の「保証しない」を参照)。
    """
    timeout = _buy_candidates_timeout_seconds()

    worst_case_edinet_seconds = DEFAULT_BUDGET_SECONDS + ASSUMED_CLIENT_TIMEOUT_SECONDS
    assert worst_case_edinet_seconds * 4 < timeout, timeout


def test_module_logs_only_counts_never_stock_codes_or_document_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeClient()
    with caplog.at_level("INFO", logger=window_prefetch.logger.name):
        prefetch_recent_document_lists_safely(
            _NOW, source_factory=lambda: _source(client, tmp_path)
        )

    text = " ".join(record.getMessage() for record in caplog.records)
    assert "dates=6" in text
    assert "fetched=6" in text
    assert "29140" not in text
    assert "DOC1" not in text


def test_unconfigured_environment_never_constructs_the_cache_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APIキー未設定なら、cache表を扱うsourceを作ることすらしない(テスト・ローカルで表に触れない)。"""
    monkeypatch.delenv("EDINET_API_KEY", raising=False)

    def _must_not_construct(*args: object, **kwargs: object) -> None:
        raise AssertionError("EdinetDocumentSource must not be constructed without an API key")

    monkeypatch.setattr(window_prefetch, "EdinetDocumentSource", _must_not_construct)

    summary = prefetch_recent_document_lists_safely(_NOW)

    assert summary.configured is False
    assert summary.error is False


# --- F 限定版: TIMEOUT の再試行(失敗した日付だけ・予算の内側・失敗は保存しない) --------


def test_timeout_is_retried_and_the_recovered_result_is_saved(tmp_path: Path) -> None:
    slow = _EXPECTED_DATES[1]
    client = FakeClient(script={slow: [_TIMEOUT, _OK]})
    sleeps: list[float] = []

    summary = prefetch_recent_document_lists(_source(client, tmp_path), _NOW, sleep=sleeps.append)

    assert client.list_calls.count(slow) == 2
    assert (summary.fetched, summary.failed, summary.retries, summary.recovered_by_retry) == (
        6,
        0,
        1,
        1,
    )
    assert sleeps == [RETRY_WAIT_SECONDS]
    cached = _repo(tmp_path).get(slow)
    assert cached is not None
    assert cached.fetch_status is EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS


def test_retry_wait_is_the_approved_two_seconds(tmp_path: Path) -> None:
    """再試行の待ちの**値**を固定する(MANAGER判断 Q2: 2秒。#818 issuecomment-5987822549)。
    定数を参照せず、リテラルで比べる(定数を変えると期待値も一緒に動くテストでは、値の変更を
    検出できないため)。待ちを伸ばすと、劣化時に予算の内側で開始できる日付が減る方向へ挙動が
    変わるので、変更するときは予算の見積り(模擬の時計のテスト)も合わせて見直すこと。
    """
    slow = _EXPECTED_DATES[1]
    client = FakeClient(script={slow: [_TIMEOUT, _OK]})
    sleeps: list[float] = []

    prefetch_recent_document_lists(_source(client, tmp_path), _NOW, sleep=sleeps.append)

    assert RETRY_WAIT_SECONDS == 2.0
    assert sleeps == [2.0]


def test_only_the_failed_date_is_retried(tmp_path: Path) -> None:
    slow = _EXPECTED_DATES[1]
    client = FakeClient(script={slow: [_TIMEOUT, _OK]})

    prefetch_recent_document_lists(_source(client, tmp_path), _NOW, sleep=lambda _s: None)

    for scan_date in _EXPECTED_DATES:
        expected = 2 if scan_date == slow else 1
        assert client.list_calls.count(scan_date) == expected, scan_date


def test_retries_stop_at_the_maximum_attempts_and_the_failure_stays_unsaved(
    tmp_path: Path,
) -> None:
    slow = _EXPECTED_DATES[1]
    client = FakeClient(script={slow: [_TIMEOUT] * 10})
    sleeps: list[float] = []

    summary = prefetch_recent_document_lists(_source(client, tmp_path), _NOW, sleep=sleeps.append)

    assert client.list_calls.count(slow) == MAX_ATTEMPTS_PER_DATE
    assert len(sleeps) == MAX_ATTEMPTS_PER_DATE - 1
    assert (summary.failed, summary.failure_reasons) == (1, (("TIMEOUT", 1),))
    failed = next(item for item in summary.date_results if item.scan_date == slow)
    assert (failed.outcome, failed.attempts, failed.failure_reason) == (
        "failed",
        MAX_ATTEMPTS_PER_DATE,
        "TIMEOUT",
    )
    # 最後まで失敗した日付は保存しない(子は従来どおり自分で取得を試みる。取得失敗は成功にしない)。
    assert _repo(tmp_path).get(slow) is None


@pytest.mark.parametrize(
    "reason",
    [EdinetFailureReason.HTTP_ERROR, EdinetFailureReason.PARSE_ERROR, EdinetFailureReason.OTHER],
)
def test_failures_other_than_timeout_are_not_retried(
    tmp_path: Path, reason: EdinetFailureReason
) -> None:
    client = FakeClient(result=EdinetListResult(EdinetFetchStatus.FETCH_FAILED, [], reason))
    sleeps: list[float] = []

    summary = prefetch_recent_document_lists(_source(client, tmp_path), _NOW, sleep=sleeps.append)

    assert client.list_calls == _EXPECTED_DATES  # 各日付 1 回だけ
    assert sleeps == []
    assert (summary.failed, summary.retries) == (6, 0)


def test_retries_stay_inside_the_time_budget(tmp_path: Path) -> None:
    """全日付が TIMEOUT し続けても、EDINET の呼び出しに費やす時間は予算(120 秒)を超えない
    (各試行は想定の最大 15 秒で終わるものとする)。試行を始める条件は 経過 + 待ち + 15 秒 <= 予算。
    """
    clock = FakeClock()
    client = FakeClient(
        result=_TIMEOUT, clock=clock, seconds_per_call=ASSUMED_CLIENT_TIMEOUT_SECONDS
    )

    def advance(seconds: float) -> None:
        clock.now += seconds

    summary = prefetch_recent_document_lists(
        _source(client, tmp_path),
        _NOW,
        budget_seconds=DEFAULT_BUDGET_SECONDS,
        clock=clock,
        sleep=advance,
    )

    assert clock.now <= DEFAULT_BUDGET_SECONDS
    assert summary.elapsed_seconds <= DEFAULT_BUDGET_SECONDS
    assert summary.budget_exceeded is True
    assert summary.skipped_by_budget >= 1
    assert all(_repo(tmp_path).get(scan_date) is None for scan_date in _EXPECTED_DATES)


def test_per_date_log_lines_carry_outcome_attempts_seconds_and_reason_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    slow = _EXPECTED_DATES[1]
    client = FakeClient(script={slow: [_TIMEOUT] * 10})
    with caplog.at_level("INFO", logger=window_prefetch.logger.name):
        prefetch_recent_document_lists_safely(
            _NOW, source_factory=lambda: _source(client, tmp_path), sleep=lambda _s: None
        )

    lines = [record.getMessage() for record in caplog.records]
    failed_line = next(line for line in lines if f"date={slow.isoformat()}" in line)
    assert "outcome=failed" in failed_line
    assert f"attempts={MAX_ATTEMPTS_PER_DATE}" in failed_line
    assert "reason=TIMEOUT" in failed_line
    assert re.search(r"elapsed=\d+\.\ds", failed_line)
    ok_line = next(line for line in lines if f"date={_EXPECTED_DATES[0].isoformat()}" in line)
    assert "outcome=fetched" in ok_line and "attempts=1" in ok_line and "reason=-" in ok_line
    summary_line = next(line for line in lines if "dates=6" in line)
    assert "retries=2" in summary_line and "recovered_by_retry=0" in summary_line
    text = " ".join(lines)
    assert "29140" not in text
    assert "DOC1" not in text
