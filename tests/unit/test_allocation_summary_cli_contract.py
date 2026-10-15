"""`jstock allocation summary`(#934 BT-A PR-2)の契約テスト。

入口の作法(読取元の明示・Lambda 環境での拒否・表名の完全一致・上限)・営業日だけを読むこと・
終了コード・出力の秘密・読取専用(AST)を、fake の読取元と固定の日付だけで固定する(実データなし)。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import allocation as allocation_cli
from jstock_advisor.cli import allocation_summary as cli
from jstock_advisor.cli.main import app as main_app
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.infrastructure.aws.allocation_shadow_reader import (
    ReadFailure,
    ReadFailureKind,
    ReadStatus,
    RecordRead,
)
from tests.unit import test_allocation_shadow_cli_view_contract as view_tests
from tests.unit import test_allocation_shadow_compute_contract as compute_tests

_REPO = Path(__file__).resolve().parents[2]
_CLI_PATH = _REPO / "src" / "jstock_advisor" / "cli" / "allocation_summary.py"
_runner = CliRunner()
_REGION = "ap-northeast-1"
_TABLE = "jstock-audit_log"
_REF = compute_tests._ref("owner-a")
_DIGEST = _REF.rsplit(":", 1)[-1]


class FakeSource:
    """記録を audit_id で返す fake。呼ばれた audit_id を数える。fail_on を含む id は読取失敗。"""

    def __init__(self, days: tuple[str, ...] = (), fail_on: str | None = None) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        for day in days:
            index = view_tests._index([_REF])
            index["business_date"] = day
            self.records[f"allocation_shadow_index:{day}"] = index
            owner = view_tests._owner_record()
            owner["business_date"] = day
            owner["candidates"][0]["stock_code"] = "CANARYCODE"
            owner["cash"]["available_cash"] = "98765432"
            self.records[f"allocation_shadow_items:{day}:{_REF}"] = owner
        self.fail_on = fail_on
        self.requested: list[str] = []

    def get(self, audit_id: str) -> RecordRead:
        self.requested.append(audit_id)
        if self.fail_on is not None and self.fail_on in audit_id:
            raise ReadFailure(ReadFailureKind.ACCESS_DENIED)
        record = self.records.get(audit_id)
        return RecordRead(ReadStatus.FOUND, record) if record else RecordRead(ReadStatus.NOT_FOUND)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(cli, "_today_jst", lambda: dt.date(2026, 10, 20))
    monkeypatch.setattr(
        cli, "_default_calendar", lambda: BusinessCalendar(frozenset(), frozenset())
    )


def _use(monkeypatch: pytest.MonkeyPatch, source: FakeSource) -> list[Any]:
    built: list[Any] = []

    def build(selection: Any) -> FakeSource:
        built.append(selection)
        return source

    monkeypatch.setattr(cli, "_build_source", build)
    return built


def _run(*args: str) -> Any:
    return _runner.invoke(main_app, ["allocation", "summary", *args])


def _local(frm: str, to: str, *extra: str) -> list[str]:
    return ["--backend", "local", "--from", frm, "--to", to, *extra]


# 2026-10-09(金)・10-13(火)が営業日。10-10・11 は週末・10-12 は祝日(スポーツの日)
_FRI, _TUE = "2026-10-09", "2026-10-13"


# =================================================================================================
# 入口(読む前に失敗する)
# =================================================================================================


def test_the_command_is_registered_under_allocation_and_today_is_unchanged() -> None:
    names = {c.name or c.callback.__name__ for c in allocation_cli.app.registered_commands}
    assert "summary" in names
    assert "today" in names
    assert _runner.invoke(main_app, ["allocation", "summary", "--help"]).exit_code == 0


def test_backend_has_no_default_and_an_unknown_backend_is_refused() -> None:
    assert _run("--from", _FRI, "--to", _TUE).exit_code == 2
    assert _run("--backend", "auto", "--from", _FRI, "--to", _TUE).exit_code == 2
    assert _run("--backend", "", "--from", _FRI, "--to", _TUE).exit_code == 2


def test_local_is_refused_inside_a_lambda_environment_and_nothing_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource()
    built = _use(monkeypatch, source)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "some-function")
    result = _run(*_local(_FRI, _TUE))
    assert result.exit_code == 2
    assert built == [] and source.requested == []


@pytest.mark.parametrize(
    "extra",
    [
        [],  # region も表名も無い
        ["--aws-region", _REGION],  # 表名の確認が無い
        ["--aws-region", _REGION, "--confirm-table", "jstock-other"],  # 表名が違う
        ["--confirm-table", _TABLE],  # region が無い
    ],
)
def test_dynamodb_without_the_region_and_the_exact_table_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    source = FakeSource()
    built = _use(monkeypatch, source)
    result = _run("--backend", "dynamodb", "--from", _FRI, "--to", _TUE, *extra)
    assert result.exit_code == 2
    assert built == [] and source.requested == []


def test_dynamodb_with_the_explicit_region_and_table_reads_only_the_business_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource((_FRI, _TUE))
    built = _use(monkeypatch, source)
    result = _run(
        "--backend", "dynamodb", "--aws-region", _REGION, "--confirm-table", _TABLE,
        "--from", _FRI, "--to", _TUE, "--target-days", "2",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert len(built) == 1 and built[0].table == _TABLE
    assert "read-only" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["--from", "2026-13-01", "--to", _TUE],  # 日付の形が不正
        ["--from", _FRI, "--to", "2026/10/13"],
        ["--from", _FRI, "--to", "2026-10-21"],  # 未来日付(今日 = 10-20)
        ["--from", _TUE, "--to", _FRI],  # from > to
        ["--from", _FRI, "--to", _TUE, "--target-days", "0"],
        ["--from", _FRI, "--to", _TUE, "--target-days", "61"],
        ["--from", "2026-10-10", "--to", "2026-10-12"],  # 営業日が 0(週末・祝日だけ)
        ["--from", "2026-01-01", "--to", "2026-10-19"],  # 営業日が 60 を超える
    ],
)
def test_a_bad_argument_is_refused_before_anything_is_read(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    source = FakeSource()
    built = _use(monkeypatch, source)
    result = _run("--backend", "local", *args)
    assert result.exit_code == 2
    assert built == [] and source.requested == []


def test_the_range_may_be_exactly_sixty_business_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "_default_calendar", lambda: BusinessCalendar(frozenset(), frozenset())
    )
    days = cli.expected_business_days(
        dt.date(2026, 7, 6), dt.date(2026, 10, 19), BusinessCalendar(frozenset(), frozenset())
    )
    assert len(days) > cli.MAX_BUSINESS_DAYS  # 範囲の取り方の確認(上の拒否の閾値の根拠)
    start = days[-cli.MAX_BUSINESS_DAYS]
    exactly = cli.expected_business_days(
        start, dt.date(2026, 10, 19), BusinessCalendar(frozenset(), frozenset())
    )
    assert len(exactly) == cli.MAX_BUSINESS_DAYS
    source = FakeSource()
    _use(monkeypatch, source)
    result = _run(*_local(start.isoformat(), "2026-10-19"))
    assert result.exit_code == 3  # 読めたが記録が無く未達(拒否ではない)
    assert source.requested  # 読んだ


# =================================================================================================
# 読むのは営業日だけ・終了コード
# =================================================================================================


def test_only_business_days_are_read_not_weekends_or_holidays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource((_FRI, _TUE))
    _use(monkeypatch, source)
    result = _run(*_local(_FRI, _TUE, "--target-days", "2"))
    assert result.exit_code == 0, result.output
    days_read = {
        r.split(":")[1] if r.startswith("allocation_shadow_items") else r.split(":", 1)[1]
        for r in source.requested
    }
    assert days_read == {_FRI, _TUE}
    assert not any(
        d in r for r in source.requested for d in ("2026-10-10", "2026-10-11", "2026-10-12")
    )


def test_exit_0_when_the_target_is_reached_and_3_when_it_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, FakeSource((_FRI,)))  # 10-13 は記録なし
    assert _run(*_local(_FRI, _TUE, "--target-days", "1")).exit_code == 0
    _use(monkeypatch, FakeSource((_FRI,)))
    assert _run(*_local(_FRI, _TUE, "--target-days", "2")).exit_code == 3


def test_a_read_failure_on_one_day_exits_1_and_does_not_stop_the_other_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource((_FRI, _TUE), fail_on=_FRI)
    _use(monkeypatch, source)
    result = _run(*_local(_FRI, _TUE, "--target-days", "1", "--format", "json"))
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    by_date = {d["business_date"]: d for d in payload["days"]}
    assert by_date[_FRI]["not_counted"] == "READ_FAILED"
    assert by_date[_FRI]["read_failure"] == "ACCESS_DENIED"
    assert by_date[_TUE]["counted"] is True  # 失敗した日の後の日も読んでいる
    assert payload["counted_days"] == 1


def test_a_day_without_a_record_is_not_zero_it_is_a_day_that_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, FakeSource())
    result = _run(*_local(_FRI, _TUE, "--format", "json"))
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["counted_days"] == 0
    assert payload["not_counted"] == {"NO_INDEX": 2}
    assert payload["periods"] == []  # 版が決まる日が無い = 指標は出さない(0 を作らない)


# =================================================================================================
# 出力
# =================================================================================================


def test_the_table_shows_the_denominators_the_rule_source_and_what_is_not_measurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, FakeSource((_FRI, _TUE)))
    result = _run(*_local(_FRI, _TUE, "--target-days", "2"))
    out = result.stdout
    assert "数える日: 2 / 2(目標に到達)" in out
    assert "6165459615" in out  # 規則の出所
    assert "母数" in out
    assert "測れないもの" in out
    assert "初案・未確定" in out
    assert "版をまたいだ合算はしない" in out


def test_the_table_says_not_measurable_when_there_is_nothing_to_divide_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, FakeSource())
    out = _run(*_local(_FRI, _TUE)).stdout
    assert "版ごとの指標: 測れない" in out
    assert "0.0%" not in out


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_the_output_has_no_reference_code_amount_or_owner_table_by_default(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    _use(monkeypatch, FakeSource((_FRI, _TUE)))
    result = _run(*_local(_FRI, _TUE, "--target-days", "2", "--format", fmt))
    out = result.stdout + result.stderr
    for secret in (_REF, _DIGEST, _DIGEST[:6], "CANARYCODE", "98765432", "98,765,432", "kv1"):
        assert secret not in out, secret
    assert "owner 別" not in out and "by_owner" not in out


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_the_owner_table_appears_only_with_the_flag_and_only_the_short_name(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    _use(monkeypatch, FakeSource((_FRI, _TUE)))
    result = _run(*_local(_FRI, _TUE, "--target-days", "2", "--format", fmt, "--by-owner"))
    out = result.stdout
    assert f"owner-{_DIGEST[:6]}" in out
    for secret in (_REF, "CANARYCODE", "98765432", "98,765,432", "kv1"):
        assert secret not in out, secret
    stripped = out.replace(f"owner-{_DIGEST[:6]}", "")
    assert _DIGEST not in stripped
    if fmt == "table":
        assert "公開面へ貼らない" in out


def test_the_json_output_is_the_report_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, FakeSource((_FRI, _TUE)))
    payload = json.loads(_run(*_local(_FRI, _TUE, "--target-days", "2", "--format", "json")).stdout)
    assert payload["schema_version"] == 1
    assert payload["counted_days"] == 2 and payload["reached"] is True
    assert payload["first_counted_date"] == _FRI
    assert payload["counting_rule_source"].endswith("(USER 承認済みの暫定ルール)")


# =================================================================================================
# 読取専用(AST)
# =================================================================================================

_FORBIDDEN_CALLS = {
    "put_item", "update_item", "delete_item", "batch_write_item", "scan", "query", "upsert",
    "upsert_many", "save", "delete", "insert_if_absent", "apply_batch", "publish", "send_message",
    "write_text", "open", "print", "eval", "exec", "system", "popen",
}  # fmt: skip


def _tree() -> ast.Module:
    return ast.parse(_CLI_PATH.read_text(encoding="utf-8"))


def test_the_cli_calls_no_write_scan_query_or_notification() -> None:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            assert name not in _FORBIDDEN_CALLS, name


def test_the_cli_reads_only_through_read_days_and_never_touches_the_environment_or_clock() -> None:
    source = _CLI_PATH.read_text(encoding="utf-8")
    assert "read_days(" in source
    for token in ("os.environ", "getenv", ".now(", "datetime.now", "boto3", "client("):
        assert token not in source, token
    # 検証は select_backend を再利用する(複製しない)
    assert "select_backend(" in source
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef):
            assert node.name != "read_day", "キーの順序の実装は reader の 1 か所だけ"


def test_the_cli_imports_no_heavy_record_compute_or_policy_module() -> None:
    imported: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    own = {m for m in imported if m.startswith("jstock_advisor")}
    assert own == {
        "jstock_advisor.cli.allocation",
        "jstock_advisor.domain.business_calendar",
        "jstock_advisor.domain.notification.allocation_shadow_cli_view",
        "jstock_advisor.domain.notification.allocation_shadow_summary",
        "jstock_advisor.infrastructure.aws.allocation_shadow_reader",
    }


def test_from_after_to_is_refused_with_its_own_message(monkeypatch: pytest.MonkeyPatch) -> None:
    source = FakeSource()
    _use(monkeypatch, source)
    result = _run(*_local(_TUE, _FRI))
    assert result.exit_code == 2
    assert "以前" in result.output  # 営業日 0 の拒否とは別の、取り違えを知らせる文面
    assert source.requested == []
