"""Issue #373: watchlist-screening CLI(run)の出力に、候補一覧データの古さを 1 行出す。

USER 決定 A(#373 issuecomment-5962323936):
    ・受入条件を『取得に失敗したことを表示する』から『CLI が利用している候補一覧データの古さを
      表示する』へ読み替える。CLI は Downloader を実行しないので、存在しない『今回の取得失敗』は
      表示しない
    ・表示例『候補一覧のデータ: 公開日 YYYY-MM-DD(N日前)』
    ・CLI から外部取得 = NO / CLI から Production DynamoDB 参照 = NO
    ・追加 0 件でも表示される場所へ置く(通知経路ではなく _print_summary)
    ・古さに対する WARNING 閾値は新設しない(別判断)
本テストは実データ・実銘柄名を使わない(架空の値のみ)。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.cli import watchlist_screening as cli_module
from jstock_advisor.interfaces.candidate_universe import (
    CandidateUniverseItem,
    CandidateUniverseResult,
)
from jstock_advisor.lambda_handlers import watchlist_dispatcher_handler as dispatcher_module
from jstock_advisor.services.watchlist_candidate_collector import (
    WatchlistCandidateCollector,
)
from tests.unit import test_watchlist_screening_cli as base

_UTC = dt.UTC


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=_UTC)


# --- 経過日数の算出(JST の暦日基準)-------------------------------------------------


class TestUniverseDataAgeDays:
    def test_same_day_is_zero_days(self) -> None:
        # 2026-10-09 10:00 JST = 01:00 UTC
        assert cli_module._universe_data_age_days(dt.date(2026, 10, 9), _utc(2026, 10, 9, 1)) == 0

    def test_whole_days_are_counted_from_jst_midnight(self) -> None:
        assert cli_module._universe_data_age_days(dt.date(2026, 10, 2), _utc(2026, 10, 9, 1)) == 7

    def test_the_day_boundary_is_jst_midnight_not_utc_midnight(self) -> None:
        source = dt.date(2026, 10, 9)
        # 2026-10-09 23:59 JST(= 14:59 UTC)はまだ 0 日 / 00:00 JST(= 15:00 UTC)で 1 日
        assert cli_module._universe_data_age_days(source, _utc(2026, 10, 9, 14, 59)) == 0
        assert cli_module._universe_data_age_days(source, _utc(2026, 10, 9, 15, 0)) == 1
        # UTC の暦日だけを見る実装なら 0 になってしまう時刻(JST では翌日の 00:30)
        assert cli_module._universe_data_age_days(source, _utc(2026, 10, 9, 15, 30)) == 1

    def test_a_future_publication_date_is_shown_as_zero(self) -> None:
        """時計のずれ等で公開日が未来に見えても、負の日数を表示しない。"""
        assert cli_module._universe_data_age_days(dt.date(2026, 10, 20), _utc(2026, 10, 9, 1)) == 0

    def test_matches_the_dispatcher_definition_for_non_negative_ages(self) -> None:
        """dispatcher の _cache_age_days と同じ式(同じ基準)であること。CLI は private 関数を
        import しないため、式が食い違わないことをここで固定する。"""
        for source in (dt.date(2026, 9, 30), dt.date(2026, 10, 8), dt.date(2026, 10, 9)):
            for now in (
                _utc(2026, 10, 9, 0, 0),
                _utc(2026, 10, 9, 14, 59),
                _utc(2026, 10, 9, 15, 0),
                _utc(2026, 10, 12, 23, 59),
                _utc(2026, 11, 1, 0, 0),
            ):
                expected = dispatcher_module._cache_age_days(source, now)
                assert expected is not None
                if expected >= 0:
                    assert cli_module._universe_data_age_days(source, now) == expected


class TestUniverseDataAgeLine:
    def test_shows_the_publication_date_and_the_age(self) -> None:
        line = cli_module._universe_data_age_line(dt.date(2026, 9, 30), _utc(2026, 10, 9, 1))

        assert line == "候補一覧のデータ: 公開日 2026-09-30(9日前)"

    def test_unknown_publication_date_is_not_shown_as_zero_days(self) -> None:
        line = cli_module._universe_data_age_line(None, _utc(2026, 10, 9, 1))

        assert line == "候補一覧のデータ: 公開日 不明"
        assert "0日前" not in line

    def test_never_claims_that_a_fetch_failed(self) -> None:
        """CLI は取得を試みない。『取得に失敗』と書くと #234(日次バッチ)と意味が食い違う。"""
        for source in (None, dt.date(2026, 1, 1), dt.date(2026, 10, 9)):
            line = cli_module._universe_data_age_line(source, _utc(2026, 10, 9, 1))
            assert "失敗" not in line
            assert "取得" not in line


# --- collector が公開日を運ぶ -----------------------------------------------------


class _FakeUniverseProvider:
    def __init__(self, result: CandidateUniverseResult) -> None:
        self._result = result

    def get_candidate_universe(self) -> CandidateUniverseResult:
        return self._result


class _NoHoldings:
    def list_all(self) -> list[Any]:
        return []


class _NoWatchlist:
    def list_all(self) -> list[Any]:
        return []


def _collector(result: CandidateUniverseResult) -> WatchlistCandidateCollector:
    return WatchlistCandidateCollector(
        _FakeUniverseProvider(result),
        SimpleNamespace(),  # type: ignore[arg-type]
        holding_repository=_NoHoldings(),  # type: ignore[arg-type]
        watchlist_repository=_NoWatchlist(),  # type: ignore[arg-type]
    )


def _universe(source_date: dt.date | None) -> CandidateUniverseResult:
    items = [CandidateUniverseItem(stock_code="1111"), CandidateUniverseItem(stock_code="2222")]
    return CandidateUniverseResult(
        items=items,
        raw_row_count=2,
        selected_count=2,
        source_date=source_date,
    )


def test_collector_result_carries_the_universe_source_date() -> None:
    result = _collector(_universe(dt.date(2026, 9, 30))).collect_target_codes()

    assert result.universe_source_date == dt.date(2026, 9, 30)


def test_collector_result_keeps_none_when_the_provider_has_no_source_date() -> None:
    """CSV の provider など、公開日の概念が無い場合は None のまま(今日の日付などで埋めない)。"""
    result = _collector(_universe(None)).collect_target_codes()

    assert result.universe_source_date is None


def test_the_new_field_is_additive_with_a_default() -> None:
    """既存の呼び出し元(dispatcher など)が CollectorResult を従来の引数だけで作っても動く。"""
    from jstock_advisor.services.watchlist_candidate_collector import CollectorResult

    result = CollectorResult(
        stock_codes=[],
        universe_count=0,
        duplicate_count=0,
        invalid_code_count=0,
        holding_excluded_count=0,
        watchlist_excluded_count=0,
    )

    assert result.universe_source_date is None


# --- CLI run の出力 ----------------------------------------------------------------


class _CollectorWithSourceDate(base._FakeCollector):
    def __init__(self, codes: list[str], source_date: dt.date | None) -> None:
        super().__init__(codes)
        self._source_date = source_date

    def collect_target_codes(self):  # noqa: ANN201
        result = super().collect_target_codes()
        result.universe_source_date = self._source_date
        return result


def _spy_summary(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """_print_summary をそのまま呼びつつ、渡された引数(now など)を記録する。"""
    calls: list[dict[str, Any]] = []
    original = cli_module._print_summary

    def _spy(**kwargs: Any) -> None:
        calls.append(kwargs)
        original(**kwargs)

    monkeypatch.setattr(cli_module, "_print_summary", _spy)
    return calls


def _patch_collector(
    monkeypatch: pytest.MonkeyPatch, codes: list[str], source_date: dt.date | None
) -> None:
    base._patch_common(monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "WatchlistCandidateCollector",
        lambda *a, **kw: _CollectorWithSourceDate(codes, source_date),
    )


def _freeze_cli_clock(monkeypatch: pytest.MonkeyPatch, fixed: dt.datetime) -> None:
    """CLI の `dt.datetime.now(...)` だけを固定する(combine などは本物のまま)。

    固定値は呼び出しごとに作るサブクラスの閉包に持たせ、モジュール / クラスの可変
    状態を残さない(test 間・他 module へ漏れない)。
    """

    class _FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # type: ignore[override]
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(
        cli_module,
        "dt",
        SimpleNamespace(
            datetime=_FrozenDateTime,
            UTC=dt.UTC,
            date=dt.date,
            time=dt.time,
            timedelta=dt.timedelta,
        ),
    )


@pytest.mark.parametrize(
    ("now_utc", "source_date", "expected"),
    [
        # 2026-10-09 10:00 JST: 9 日前
        (
            _utc(2026, 10, 9, 1, 0),
            dt.date(2026, 9, 30),
            "候補一覧のデータ: 公開日 2026-09-30(9日前)",
        ),
        # 23:59 JST(= 14:59 UTC)はまだ 0 日前 / 00:00 JST(= 15:00 UTC)で 1 日前
        (
            _utc(2026, 10, 9, 14, 59),
            dt.date(2026, 10, 9),
            "候補一覧のデータ: 公開日 2026-10-09(0日前)",
        ),
        (
            _utc(2026, 10, 9, 15, 0),
            dt.date(2026, 10, 9),
            "候補一覧のデータ: 公開日 2026-10-09(1日前)",
        ),
        (
            _utc(2026, 10, 9, 15, 30),
            dt.date(2026, 10, 9),
            "候補一覧のデータ: 公開日 2026-10-09(1日前)",
        ),
    ],
)
def test_the_cli_prints_the_literal_age_under_a_fixed_clock(
    monkeypatch: pytest.MonkeyPatch, now_utc: dt.datetime, source_date: dt.date, expected: str
) -> None:
    """CLI 層でも期待日数をリテラルで固定する(被検査関数の戻り値から期待値を作らない)。
    式が壊れたとき、unit 層だけでなく CLI の出力でも落ちる。"""
    _patch_collector(monkeypatch, ["1234"], source_date)
    _freeze_cli_clock(monkeypatch, now_utc)

    result = base._runner.invoke(cli_module.app, ["run", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert expected in result.output.splitlines()


def test_dry_run_prints_the_data_age_line_next_to_the_universe_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_collector(monkeypatch, ["1234"], dt.date(2026, 9, 30))
    calls = _spy_summary(monkeypatch)

    result = base._runner.invoke(cli_module.app, ["run", "--dry-run"])

    assert result.exit_code == 0, result.output
    expected_days = cli_module._universe_data_age_days(dt.date(2026, 9, 30), calls[0]["now"])
    assert f"候補一覧のデータ: 公開日 2026-09-30({expected_days}日前)" in result.output
    lines = result.output.splitlines()
    universe_index = next(i for i, line in enumerate(lines) if line.startswith("対象ユニバース:"))
    assert lines[universe_index + 1].startswith("候補一覧のデータ:")


def test_the_line_is_printed_when_nothing_is_added(monkeypatch: pytest.MonkeyPatch) -> None:
    """追加 0 件の日でも出る(通知の経路ではなく _print_summary に置いたため)。"""
    _patch_collector(monkeypatch, [], dt.date(2026, 9, 30))
    _spy_summary(monkeypatch)

    result = base._runner.invoke(cli_module.app, ["run", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "候補一覧のデータ: 公開日 2026-09-30(" in result.output
    assert "追加予定: 0件" in result.output


def test_the_line_is_printed_in_a_real_run_without_additions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_collector(monkeypatch, [], dt.date(2026, 9, 30))
    monkeypatch.setattr(cli_module, "WatchlistRepository", lambda: base._FakeWatchlistRepository())
    monkeypatch.setattr(cli_module, "record_candidate_audit", lambda *a, **kw: None)
    monkeypatch.setattr(cli_module, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(cli_module, "record_repository_result_audit", lambda *a, **kw: None)
    _spy_summary(monkeypatch)

    result = base._runner.invoke(cli_module.app, ["run"])

    assert result.exit_code == 0, result.output
    assert "候補一覧のデータ: 公開日 2026-09-30(" in result.output


def test_unknown_source_date_is_printed_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_collector(monkeypatch, ["1234"], None)
    _spy_summary(monkeypatch)

    result = base._runner.invoke(cli_module.app, ["run", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "候補一覧のデータ: 公開日 不明" in result.output


def test_the_existing_summary_lines_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功日の既存の出力は変えない(1 行の追加のみ)。"""
    _patch_collector(monkeypatch, ["1234"], dt.date(2026, 9, 30))
    _spy_summary(monkeypatch)

    result = base._runner.invoke(cli_module.app, ["run", "--dry-run"])

    for expected in (
        "ウォッチリスト自動追加 dry-run",
        "対象ユニバース: 1件(重複除去: 0件)",
        "保有銘柄除外: 0件",
        "既登録除外: 0件",
        "評価対象: 1件",
        "データ取得成功: 1件",
        "追加予定: 1件",
    ):
        assert expected in result.output, expected
    added = [line for line in result.output.splitlines() if line.startswith("候補一覧のデータ")]
    assert len(added) == 1


def test_the_line_summary_does_not_receive_the_universe_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LINE 通知の summary へは universe_fetch_failed / universe_source_date を渡さない
    (CLI は取得失敗を測れない。USER 決定 A)。"""
    _patch_collector(monkeypatch, ["1234"], dt.date(2026, 9, 30))
    monkeypatch.setattr(cli_module, "WatchlistRepository", lambda: base._FakeWatchlistRepository())
    monkeypatch.setattr(cli_module, "record_candidate_audit", lambda *a, **kw: None)
    monkeypatch.setattr(cli_module, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(cli_module, "record_repository_result_audit", lambda *a, **kw: None)

    class _FakeNotificationService:
        def __init__(self, **kwargs: object) -> None:
            pass

        def notify_watchlist_additions(self, summary, content_hash):  # noqa: ANN001, ANN201
            return True

    monkeypatch.setattr(cli_module, "LineNotificationService", _FakeNotificationService)
    summary_kwargs: list[dict[str, Any]] = []
    original = cli_module.build_watchlist_addition_summary

    def _spy_builder(**kwargs: Any) -> Any:
        summary_kwargs.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(cli_module, "build_watchlist_addition_summary", _spy_builder)

    result = base._runner.invoke(cli_module.app, ["run"])

    assert result.exit_code == 0, result.output
    assert summary_kwargs, "build_watchlist_addition_summary が呼ばれていない"
    assert "universe_fetch_failed" not in summary_kwargs[0]
    assert "universe_source_date" not in summary_kwargs[0]
