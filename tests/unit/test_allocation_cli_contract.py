"""Issue #605 最小 CLI `jstock allocation`: コマンド・guard・読取の回数・終了コードの契約テスト。

固定するもの:
  - ★ backend は明示(既定なし)。環境変数から選ばない。`AWS_LAMBDA_FUNCTION_NAME` があれば
    local を拒否。dynamodb は region と表名の完全一致が揃わなければ 1 件も読まない
    (source を作らない)★
  - `--owner` を持たない / 既存のコマンドは変わらない(登録は 1 行の追加だけ)
  - 読取: 索引 1 + skip 1 + 索引が載せた鍵つきの符号ごとに 1(最大 16)。鍵つきでない符号は読まない
  - 終了コード: 0 完全 / 1 読取の失敗(固定の文言)/ 2 引数不正 / 3 読めたが不完全
  - JSON の出力は標準出力だけ(前置きは標準エラー)・ラベルは既定で出ない
  - 書込の経路が無い(AST)・ローカルの読取は repository の get だけ
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from jstock_advisor.cli import allocation as cli
from jstock_advisor.cli.allocation_labels import LabelError
from jstock_advisor.cli.main import app as main_app
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DayInput,
    RecordInput,
    RecordState,
)
from jstock_advisor.infrastructure.aws.allocation_shadow_reader import (
    ReadFailure,
    ReadFailureKind,
    ReadStatus,
    RecordRead,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from tests.unit import test_allocation_shadow_cli_view_contract as view_tests
from tests.unit import test_allocation_shadow_compute_contract as compute_tests

_REPO = Path(__file__).resolve().parents[2]
_CLI_PATH = _REPO / "src" / "jstock_advisor" / "cli" / "allocation.py"
_runner = CliRunner()
_DAY = "2026-10-14"
_REGION = "ap-northeast-1"
_TABLE = "jstock-audit_log"
_SENTINEL = "sentinel-failure-zz"


class FakeSource:
    """記録を audit_id で返す fake。呼ばれた audit_id を数える。"""

    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        self.records = records or {}
        self.requested: list[str] = []

    def get(self, audit_id: str) -> RecordRead:
        self.requested.append(audit_id)
        record = self.records.get(audit_id)
        if record is None:
            return RecordRead(ReadStatus.NOT_FOUND)
        return RecordRead(ReadStatus.FOUND, record)


def _complete_source() -> FakeSource:
    ref = compute_tests._ref("owner-a")
    return FakeSource(
        {
            f"allocation_shadow_index:{_DAY}": view_tests._index([ref]),
            f"allocation_shadow_items:{_DAY}:{ref}": view_tests._owner_record(),
        }
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(cli, "_today_jst", lambda: dt.date(2026, 10, 14))
    monkeypatch.setattr(cli, "load_labels", lambda: {})
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


def _today(*args: str) -> Any:
    return _runner.invoke(cli.app, ["today", *args])


_LOCAL = ["--backend", "local", "--date", _DAY]
_DYNAMO = [
    "--backend",
    "dynamodb",
    "--aws-region",
    _REGION,
    "--confirm-table",
    _TABLE,
    "--date",
    _DAY,
]


# =================================================================================================
# guard(読む前に失敗する)
# =================================================================================================


def test_backend_has_no_default() -> None:
    result = _today("--date", _DAY)

    assert result.exit_code == 2


def test_an_unknown_backend_is_refused() -> None:
    assert _today("--backend", "auto", "--date", _DAY).exit_code == 2
    assert _today("--backend", "", "--date", _DAY).exit_code == 2


def test_local_is_refused_inside_a_lambda_environment_and_nothing_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _use(monkeypatch, FakeSource())
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "some-function")

    result = _today(*_LOCAL)

    assert result.exit_code == 2
    assert "AWS_LAMBDA_FUNCTION_NAME" in result.output
    assert built == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--aws-region", _REGION],
        ["--confirm-table", _TABLE],
        ["--table-prefix", "jstock"],
        ["--aws-profile", "observer"],
    ],
)
def test_local_refuses_the_dynamodb_only_options(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    built = _use(monkeypatch, FakeSource())

    result = _today(*_LOCAL, *extra)

    assert result.exit_code == 2
    assert built == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["--backend", "dynamodb", "--confirm-table", _TABLE, "--date", _DAY],  # region なし
        ["--backend", "dynamodb", "--aws-region", _REGION, "--date", _DAY],  # confirm なし
        [
            "--backend",
            "dynamodb",
            "--aws-region",
            _REGION,
            "--confirm-table",
            "other-audit_log",
            "--date",
            _DAY,
        ],
        [*_DYNAMO, "--table-prefix", "other"],  # prefix が違えば表名が一致しない
    ],
)
def test_dynamodb_reads_nothing_unless_region_and_the_exact_table_name_are_given(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    built = _use(monkeypatch, FakeSource())

    result = _today(*arguments)

    assert result.exit_code == 2
    assert built == []  # source(client)を作らない = 1 件も読まない


def test_a_confirm_table_that_is_only_a_prefix_or_a_different_case_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _use(monkeypatch, FakeSource())
    for wrong in ("jstock", "JSTOCK-AUDIT_LOG", "jstock-audit_log ", "jstock-audit_log-x"):
        arguments = [*_DYNAMO[:5], wrong, "--date", _DAY]
        assert _today(*arguments).exit_code == 2
    assert built == []


def test_the_prefix_changes_the_expected_table_name(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _use(monkeypatch, FakeSource())

    result = _today(*_DYNAMO[:5], "stg-audit_log", "--table-prefix", "stg", "--date", _DAY)

    assert result.exit_code == 3  # 読めた(索引なし)
    assert built[0].table == "stg-audit_log"


def test_the_environment_never_selects_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "production")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)

    assert _today("--date", _DAY).exit_code == 2  # 環境変数があっても backend は必須


@pytest.mark.parametrize("value", ["2026/10/14", "20261014", "tomorrow", "2026-13-01", ""])
def test_a_bad_date_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    built = _use(monkeypatch, FakeSource())

    assert _today("--backend", "local", "--date", value).exit_code == 2
    assert built == []


def test_a_future_date_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _use(monkeypatch, FakeSource())

    assert _today("--backend", "local", "--date", "2026-10-15").exit_code == 2
    assert built == []


def test_there_is_no_owner_option_and_no_write_option() -> None:
    result = _today(*_LOCAL, "--owner", "someone")

    assert result.exit_code == 2
    help_text = _runner.invoke(cli.app, ["today", "--help"]).output
    for name in ("--owner", "--execute", "--write", "--force", "--confirm-write"):
        assert name not in help_text


# =================================================================================================
# 日付の既定
# =================================================================================================


def test_the_default_date_is_the_latest_business_day_not_silently_earlier() -> None:
    calendar = BusinessCalendar(frozenset(), frozenset())
    wednesday = dt.date(2026, 10, 14)

    assert cli.resolve_date(None, wednesday, calendar) == wednesday  # 営業日なら今日
    saturday = cli.resolve_date(None, dt.date(2026, 10, 17), calendar)
    sunday = cli.resolve_date(None, dt.date(2026, 10, 18), calendar)
    holiday = cli.resolve_date(None, dt.date(2026, 10, 12), calendar)  # 祝日(月)
    assert (saturday, sunday) == (dt.date(2026, 10, 16), dt.date(2026, 10, 16))
    assert holiday == dt.date(2026, 10, 9)


def test_an_explicit_date_is_used_as_given_even_on_a_holiday() -> None:
    calendar = BusinessCalendar(frozenset(), frozenset())

    assert cli.resolve_date("2026-10-17", dt.date(2026, 10, 19), calendar) == dt.date(2026, 10, 17)


def test_without_a_date_the_default_is_used_in_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _complete_source()
    _use(monkeypatch, source)

    result = _today("--backend", "local")

    assert result.exit_code == 0
    assert f"allocation_shadow_index:{_DAY}" in source.requested


# =================================================================================================
# 読取の回数と対象
# =================================================================================================


def test_the_read_is_the_index_the_skip_and_one_per_listed_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _complete_source()
    _use(monkeypatch, source)

    result = _today(*_LOCAL)

    ref = compute_tests._ref("owner-a")
    assert result.exit_code == 0
    assert source.requested == [
        f"allocation_shadow_index:{_DAY}",
        f"allocation_shadow_day_skip:{_DAY}",
        f"allocation_shadow_items:{_DAY}:{ref}",
    ]


def test_at_most_eighteen_get_items_even_when_the_index_lists_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs: list[str] = [compute_tests._ref(f"owner-{n:02d}") for n in range(20)]
    source = FakeSource({f"allocation_shadow_index:{_DAY}": view_tests._index(refs=refs)})
    _use(monkeypatch, source)

    result = _today(*_LOCAL)

    assert len(source.requested) == 18  # 索引 1 + skip 1 + 16
    assert result.exit_code == 3


def test_a_reference_that_is_not_keyed_is_never_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    good = compute_tests._ref("owner-a")
    index = view_tests._index(refs=["kv0:" + "a" * 24, "owner-a", good])
    source = FakeSource({f"allocation_shadow_index:{_DAY}": index})
    _use(monkeypatch, source)

    _today(*_LOCAL)

    assert source.requested == [
        f"allocation_shadow_index:{_DAY}",
        f"allocation_shadow_day_skip:{_DAY}",
        f"allocation_shadow_items:{_DAY}:{good}",
    ]


def test_a_day_without_an_index_reads_only_the_index_and_the_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource()
    _use(monkeypatch, source)

    _today(*_LOCAL)

    assert source.requested == [
        f"allocation_shadow_index:{_DAY}",
        f"allocation_shadow_day_skip:{_DAY}",
    ]


def test_read_day_gives_the_view_the_found_and_missing_states() -> None:
    ref = compute_tests._ref("owner-a")
    source = FakeSource({f"allocation_shadow_index:{_DAY}": view_tests._index([ref])})

    day = cli.read_day(source, dt.date(2026, 10, 14))

    assert isinstance(day, DayInput)
    assert day.index.state is RecordState.FOUND
    assert day.skip.state is RecordState.NOT_FOUND
    assert day.items[ref] == RecordInput(RecordState.NOT_FOUND, None)


# =================================================================================================
# 終了コードと出力
# =================================================================================================


def test_a_complete_day_exits_zero_with_the_table_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _complete_source())

    result = _today(*_LOCAL)

    assert result.exit_code == 0
    assert "資金配分 Shadow" in result.stdout
    assert "backend=local" in result.stderr  # 前置きは標準エラー
    assert "backend=" not in result.stdout


@pytest.mark.parametrize(
    "records",
    [
        {},  # 索引なし
        {
            f"allocation_shadow_index:{_DAY}": view_tests._index(
                unrecorded=1, reasons={"WRITE_FAILED": 1}
            )
        },
        {
            f"allocation_shadow_index:{_DAY}": view_tests._index(refs=[], recorded=0, unrecorded=1),
        },
        {f"allocation_shadow_day_skip:{_DAY}": view_tests._skip_record()},
    ],
)
def test_an_incomplete_day_exits_three_and_never_looks_like_zero_results(
    monkeypatch: pytest.MonkeyPatch, records: dict[str, dict[str, Any]]
) -> None:
    _use(monkeypatch, FakeSource(records))

    result = _today(*_LOCAL)

    assert result.exit_code == 3
    assert "状況:" in result.stdout
    assert "購入 0 件" not in result.stdout


def test_a_read_failure_exits_one_with_a_fixed_message_that_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Failing:
        def get(self, audit_id: str) -> RecordRead:
            raise ReadFailure(ReadFailureKind.ACCESS_DENIED)

    monkeypatch.setattr(cli, "_build_source", lambda selection: Failing())

    result = _today(*_LOCAL)

    assert result.exit_code == 1
    assert "AccessDenied" in result.stderr
    assert result.stdout.strip() == ""  # 表を出さない(0 件に見せない)
    assert "allocation_shadow" not in result.stderr  # audit_id・表名を出さない


@pytest.mark.parametrize("kind", list(ReadFailureKind))
def test_every_failure_kind_has_a_fixed_message(
    monkeypatch: pytest.MonkeyPatch, kind: ReadFailureKind
) -> None:
    class Failing:
        def get(self, audit_id: str) -> RecordRead:
            raise ReadFailure(kind)

    monkeypatch.setattr(cli, "_build_source", lambda selection: Failing())

    result = _today(*_LOCAL)

    assert result.exit_code == 1
    assert "読取に失敗しました" in result.stderr


def test_the_json_output_is_pure_on_stdout_and_hides_labels_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = compute_tests._ref("owner-a").split(":")[1]
    monkeypatch.setattr(cli, "load_labels", lambda: {digest[:8]: "sentinel-表示名-zz"})
    _use(monkeypatch, _complete_source())

    result = _today(*_LOCAL, "--format", "json")

    parsed = json.loads(result.stdout)  # 標準出力が JSON だけ
    assert parsed["complete"] is True
    assert "sentinel-表示名-zz" not in result.stdout
    assert "sentinel-表示名-zz" not in result.stderr


def test_with_labels_adds_the_names_to_the_json_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = compute_tests._ref("owner-a").split(":")[1]
    monkeypatch.setattr(cli, "load_labels", lambda: {digest[:8]: "sentinel-表示名-zz"})
    _use(monkeypatch, _complete_source())

    parsed = json.loads(_today(*_LOCAL, "--format", "json", "--with-labels").stdout)

    assert parsed["owners"][0]["label"] == "sentinel-表示名-zz"


def test_the_table_shows_the_label_and_the_sharing_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = compute_tests._ref("owner-a").split(":")[1]
    monkeypatch.setattr(cli, "load_labels", lambda: {digest[:8]: "sentinel-表示名-zz"})
    _use(monkeypatch, _complete_source())

    result = _today(*_LOCAL)

    assert "sentinel-表示名-zz" in result.stdout
    assert "表示名(ラベル)が含まれます" in result.stdout


def test_the_help_says_that_the_table_contains_labels() -> None:
    assert "ラベル" in _runner.invoke(cli.app, ["today", "--help"]).output


def test_an_unreadable_label_file_does_not_stop_the_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> dict[str, str]:
        raise LabelError("x")

    monkeypatch.setattr(cli, "load_labels", broken)
    _use(monkeypatch, _complete_source())

    result = _today(*_LOCAL)

    assert result.exit_code == 0
    assert "表示名なし" in result.stderr


def test_the_preamble_names_the_backend_and_the_table_for_dynamodb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _complete_source())

    result = _today(*_DYNAMO)

    assert "backend=dynamodb" in result.stderr
    assert f"table={_TABLE}" in result.stderr
    assert "read-only" in result.stderr


# =================================================================================================
# ローカルの読取・既存の CLI・構造
# =================================================================================================


def test_the_local_source_reads_an_entry_from_the_audit_log_repository(tmp_path: Path) -> None:
    repository = AuditLogRepository(store_dir=tmp_path)
    from jstock_advisor.domain.entities.audit import AuditLogEntry

    record = view_tests._index()
    repository.save(
        AuditLogEntry(
            audit_id=f"allocation_shadow_index:{_DAY}",
            timestamp=dt.datetime(2026, 10, 14, tzinfo=dt.UTC),
            decision_type="allocation_shadow_index",
            input_values={},
            calculation_formulas={},
            output_values=record,
            data_sources=[],
            rule_version="r",
        )
    )
    source = cli.LocalRecordSource(repository)

    found = source.get(f"allocation_shadow_index:{_DAY}")
    absent = source.get("allocation_shadow_index:2026-01-01")

    assert found.status is ReadStatus.FOUND
    assert found.record == record
    assert absent.status is ReadStatus.NOT_FOUND


def test_the_local_source_calls_only_get_on_the_repository() -> None:
    class Repo:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get(self, audit_id: str) -> None:
            self.calls.append("get")

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"unexpected repository call: {name}")

    repository = Repo()

    result = cli.LocalRecordSource(repository).get("k")  # type: ignore[arg-type]

    assert result.status is ReadStatus.NOT_FOUND
    assert repository.calls == ["get"]


def test_a_repository_failure_becomes_a_fixed_read_failure() -> None:
    class Repo:
        def get(self, audit_id: str) -> None:
            raise RuntimeError(_SENTINEL)

    with pytest.raises(ReadFailure) as caught:
        cli.LocalRecordSource(Repo()).get("k")  # type: ignore[arg-type]

    assert _SENTINEL not in str(caught.value)
    assert caught.value.kind is ReadFailureKind.ERROR


def test_the_command_is_registered_once_and_the_existing_commands_are_unchanged() -> None:
    names = sorted(info.name for info in main_app.registered_groups if info.name is not None)

    assert names.count("allocation") == 1
    existing = [n for n in names if n != "allocation"]
    assert existing == sorted(
        [
            "holdings",
            "available-cash",
            "watchlist",
            "analyze",
            "audit",
            "transactions",
            "evaluation",
            "performance",
            "decision-performance",
            "feedback",
            "rules",
            "review",
            "shareholder-benefit",
            "watchlist-screening",
            "candidate-universe",
            "calibration",
            "valuation-shadow",
            "holding-decision",
            "baseline-repair",
            "trading-pause",
            "judgment-safety-shadow",
            "migrate",
            "weekly-aggregate",
        ]
    )
    assert "today" in _runner.invoke(main_app, ["allocation", "--help"]).output


_WRITE_CALLS = {
    "put_item",
    "update_item",
    "delete_item",
    "batch_write_item",
    "save",
    "save_if_absent",
    "upsert",
    "insert_if_absent",
    "delete",
    "replace_if_raw_matches",
    "record_if_absent",
    "write_text",
    "write_bytes",
    "unlink",
    "mkdir",
    "invoke",
    "publish",
    "send_message",
    "push",
    "scan",
    "query",
}


def test_the_command_module_has_no_write_scan_notification_or_environment_write() -> None:
    tree = ast.parse(_CLI_PATH.read_text(encoding="utf-8"))
    called = {
        getattr(node.func, "attr", getattr(node.func, "id", ""))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & _WRITE_CALLS
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and "environ" in ast.dump(target):
                    raise AssertionError("環境変数への書込")
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "putenv" not in names and "setdefault" not in names


def test_the_command_module_imports_no_notification_valuation_or_record_builder() -> None:
    tree = ast.parse(_CLI_PATH.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not [m for m in imported if "line_notification" in m or "notification_service" in m]
    assert not [m for m in imported if "allocation_shadow_record" in m or "shadow_compute" in m]
    assert not [m for m in imported if "raer" in m or "portfolio_allocation" in m]
    assert not [m for m in imported if m == "boto3" or "botocore" in m]


def test_the_environment_variable_is_only_read_for_the_lambda_guard() -> None:
    source = _CLI_PATH.read_text(encoding="utf-8")
    assert source.count("os.environ") == 1
    assert 'os.environ.get("AWS_LAMBDA_FUNCTION_NAME")' in source


def test_typer_is_the_only_framework_in_use() -> None:
    assert isinstance(cli.app, typer.Typer)


def test_the_dynamodb_source_is_built_from_the_explicit_region_profile_and_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_client(*, region: str, profile: str | None) -> str:
        captured["region"] = region
        captured["profile"] = profile
        return "client"

    monkeypatch.setattr(cli, "build_client", fake_build_client)
    selection = cli.BackendSelection(cli.Backend.DYNAMODB, "ap-northeast-1", "stg", "observer")

    source = cli._build_source(selection)

    assert captured == {"region": "ap-northeast-1", "profile": "observer"}
    assert source.client == "client"  # type: ignore[attr-defined]
    assert source.table_name == "stg-audit_log"  # type: ignore[attr-defined]
    assert isinstance(
        cli._build_source(cli.BackendSelection(cli.Backend.LOCAL)), cli.LocalRecordSource
    )


def test_a_missing_confirm_table_is_reported_as_missing_not_as_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, FakeSource())

    missing = _today("--backend", "dynamodb", "--aws-region", _REGION, "--date", _DAY)
    wrong = _today(*_DYNAMO[:4], "--confirm-table", "other", "--date", _DAY)

    assert missing.exit_code == wrong.exit_code == 2
    assert "入力が必要" in missing.output
    assert "一致しません" not in missing.output
    assert "一致しません" in wrong.output
