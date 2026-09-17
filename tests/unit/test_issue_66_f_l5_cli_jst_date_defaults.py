"""Issue #66 F-L5: CLI/CSV既定日付がマシンローカルのnaive日付(dt.date.today())
に依存しており、TZ=UTC等の環境で実行するとJSTより1日前になっていた欠陥。

対象4箇所すべてで、JST 00:00〜08:59(UTC上は前日)のnowを与えたときに
JST暦日(=当日)が返ることを固定する。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.cli import holding_decision as holding_decision_cli
from jstock_advisor.cli import holdings as holdings_cli
from jstock_advisor.cli import transactions as transactions_cli
from jstock_advisor.services import csv_import_service as csv_import_service_module

# JST 2026-01-02 08:00 = UTC 2026-01-01 23:00(UTC暦日はJSTの前日になる境界)。
_JST_EARLY_MORNING_UTC_INSTANT = dt.datetime(2026, 1, 1, 23, 0, tzinfo=dt.UTC)
_EXPECTED_JST_DATE = dt.date(2026, 1, 2)
_WRONG_UTC_DATE = dt.date(2026, 1, 1)


class _FixedDatetime(dt.datetime):
    """dt.datetime.now(dt.UTC)を固定するテスト用stand-in。"""

    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
        if tz is None:
            raise AssertionError("naiveなnow()呼び出しは想定していない(常にtz=dt.UTC必須)")
        return _JST_EARLY_MORNING_UTC_INSTANT.astimezone(tz)


def test_holdings_cli_default_purchase_date_uses_jst_calendar_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(holdings_cli.dt, "datetime", _FixedDatetime)
    result = holdings_cli._parse_date(None)
    assert result == _EXPECTED_JST_DATE
    assert result != _WRONG_UTC_DATE


def test_transactions_cli_default_date_uses_jst_calendar_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transactions_cli.dt, "datetime", _FixedDatetime)
    result = transactions_cli._parse_date(None)
    assert result == _EXPECTED_JST_DATE
    assert result != _WRONG_UTC_DATE


def test_holding_decision_cli_replay_end_date_default_uses_jst_calendar_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backtest()の実経路(--end-date省略時のreplayモード分岐)を実際に呼び出し、
    run_history_replay()へ渡されるend_dateがJST暦日になることを検証する
    (日付変換式をテスト側で再実装しない)。
    """
    monkeypatch.setattr(holding_decision_cli.dt, "datetime", _FixedDatetime)

    captured: dict[str, dt.date] = {}

    def _fake_run_history_replay(
        stock_codes: list[str],
        start_date: dt.date,
        end_date: dt.date,
        holding_decision_result_repo: object | None = None,
        recommendation_repo: object | None = None,
        notification_log_repo: object | None = None,
        allow_same_day_fallback: bool = False,
    ) -> list[object]:
        captured["stock_codes"] = stock_codes
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        return []

    monkeypatch.setattr(
        holding_decision_cli, "run_history_replay", _fake_run_history_replay
    )

    holding_decision_cli.backtest(
        stock_code=["9999"],
        start_date="2026-01-01",
        end_date=None,
        source="mock",
        allow_same_day_fallback=False,
        purchase_price=None,
        purchase_date=None,
        shares=None,
        csv_path=None,
    )

    assert captured["end_date"] == _EXPECTED_JST_DATE
    assert captured["end_date"] != _WRONG_UTC_DATE


def test_csv_import_default_purchase_date_uses_jst_calendar_date(
    tmp_path, monkeypatch: pytest.MonkeyPatch, csv_import_service, portfolio_service
) -> None:
    """purchase_date列を省略した行の既定値が、import開始時に1回だけ計算される
    now(JST境界)からJST暦日で決まることを、HoldingsCsvImportService経由の
    公開APIで検証する(_process_row()単体ではなくimport_file()を通す)。
    """
    from jstock_advisor.domain.entities.owner import DEFAULT_OWNER

    monkeypatch.setattr(csv_import_service_module.dt, "datetime", _FixedDatetime)

    csv_path = tmp_path / "holdings.csv"
    csv_path.write_text(
        "owner,stock_code,stock_name,shares,purchase_price,account_type\n"
        f"{DEFAULT_OWNER},2914,日本たばこ産業,100,4200,NISA\n",
        encoding="utf-8-sig",
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.error_count == 0

    holding = portfolio_service.get_holding(DEFAULT_OWNER, "2914")
    assert holding is not None
    assert holding.first_purchase_date == _EXPECTED_JST_DATE
    assert holding.first_purchase_date != _WRONG_UTC_DATE
