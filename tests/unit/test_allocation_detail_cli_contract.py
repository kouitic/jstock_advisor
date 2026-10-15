"""`jstock allocation detail` の guard・読取の回数・終了コード・絞り込みの契約テスト(#605)。

設計: #605 追加 CLI 設計 rev1〜rev3。固定するもの:
  - ★ 登録は 1 行の追加だけ。`today`・`summary`・`label` は変わらない ★
  - ★ 入口の検証は `today` と同じ関数(複製しない)★: backend は明示・region と表名の完全一致が
    揃わなければ source を作らない(1 件も読まない)
  - ★ 読取は `today` と同じ(索引 1 + skip 1 + 鍵つきの符号ごとに 1)。絞り込みで読取は変わらない ★
  - ★ `--date` に営業日でない日を明示したときは、読まずに終了コード 2(source を作らない)★
  - 省略時の直近の営業日は CLI の層が決める / 未来日・形式の誤りは 2
  - `--reason` の語彙は閉じた理由コードの表から導く。未知の語は 2 で、使える語の一覧を出す
  - 終了コード: 0 完全 / 1 読取の失敗(固定の文言)/ 2 引数不正 / 3 読めたが不完全
  - JSON は標準出力だけ・ラベルは既定で出ない / 書込・Scan・LINE の経路が無い(AST)
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import allocation as cli
from jstock_advisor.cli import allocation_detail as detail_cli
from jstock_advisor.cli.main import app as main_app
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.notification.allocation_shadow_detail_view import REASON_FILTER_CODES
from jstock_advisor.infrastructure.aws.allocation_shadow_reader import (
    ReadFailure,
    ReadFailureKind,
    ReadStatus,
    RecordRead,
)
from tests.unit import test_allocation_shadow_cli_view_contract as view_tests
from tests.unit import test_allocation_shadow_compute_contract as compute_tests
from tests.unit.test_allocation_shadow_detail_contract import _rich_record

_CLI_PATH = Path(detail_cli.__file__)
_runner = CliRunner()
_DAY = "2026-10-14"  # 水曜
_REGION = "ap-northeast-1"
_TABLE = "jstock-audit_log"
_REF = compute_tests._ref("owner-a")


class FakeSource:
    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        self.records = records or {}
        self.requested: list[str] = []

    def get(self, audit_id: str) -> RecordRead:
        self.requested.append(audit_id)
        record = self.records.get(audit_id)
        if record is None:
            return RecordRead(ReadStatus.NOT_FOUND)
        return RecordRead(ReadStatus.FOUND, record)


def _source(day: str = _DAY, *, unrecorded: bool = False) -> FakeSource:
    index = view_tests._index([_REF], unrecorded=1 if unrecorded else 0)
    return FakeSource(
        {
            f"allocation_shadow_index:{day}": index,
            f"allocation_shadow_items:{day}:{_REF}": _rich_record(),
        }
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(detail_cli, "_today_jst", lambda: dt.date(2026, 10, 14))
    monkeypatch.setattr(detail_cli, "_load_labels_or_none", lambda: {})
    monkeypatch.setattr(
        detail_cli, "_default_calendar", lambda: BusinessCalendar(frozenset(), frozenset())
    )
    monkeypatch.setattr(cli, "load_labels_counting_dropped", lambda: ({}, 0))
    monkeypatch.setattr(
        cli, "_default_calendar", lambda: BusinessCalendar(frozenset(), frozenset())
    )
    monkeypatch.setattr(cli, "_today_jst", lambda: dt.date(2026, 10, 14))


def _use(monkeypatch: pytest.MonkeyPatch, source: FakeSource) -> list[Any]:
    built: list[Any] = []

    def build(selection: Any) -> FakeSource:
        built.append(selection)
        return source

    monkeypatch.setattr(detail_cli, "_build_source", build)
    monkeypatch.setattr(cli, "_build_source", build)
    return built


def _detail(*args: str) -> Any:
    return _runner.invoke(cli.app, ["detail", *args])


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


# --- 登録・既存のコマンドが変わらない ----------


def test_detail_is_registered_through_the_main_app_next_to_the_existing_commands() -> None:
    result = _runner.invoke(main_app, ["allocation", "--help"])
    assert result.exit_code == 0
    for name in ("today", "summary", "detail", "label"):
        assert name in result.stdout
    assert _runner.invoke(main_app, ["allocation", "detail", "--help"]).exit_code == 0


def test_the_entry_validation_is_the_same_function_as_today() -> None:
    assert detail_cli.select_backend is cli.select_backend
    assert detail_cli.read_day.__module__.endswith("allocation_shadow_reader")


# --- guard ----------


def test_backend_has_no_default_and_an_unknown_backend_is_refused() -> None:
    assert _detail("--date", _DAY).exit_code == 2
    assert _detail("--backend", "auto", "--date", _DAY).exit_code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["--backend", "dynamodb", "--confirm-table", _TABLE, "--date", _DAY],
        ["--backend", "dynamodb", "--aws-region", _REGION, "--date", _DAY],
        [*_DYNAMO[:-3], "other-audit_log", "--date", _DAY],
        [*_DYNAMO, "--table-prefix", "other"],
    ],
)
def test_dynamodb_reads_nothing_unless_region_and_the_exact_table_name_are_given(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    built = _use(monkeypatch, _source())
    assert _detail(*arguments).exit_code == 2
    assert built == []


def test_local_is_refused_inside_a_lambda_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _use(monkeypatch, _source())
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "some-function")
    assert _detail(*_LOCAL).exit_code == 2
    assert built == []


# --- 読取(today と同じ・絞り込みで変わらない)----------


def test_the_reads_are_the_same_as_today_and_filters_do_not_change_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today_source = _source()
    _use(monkeypatch, today_source)
    assert _runner.invoke(cli.app, ["today", *_LOCAL]).exit_code == 0

    plain = _source()
    _use(monkeypatch, plain)
    assert _detail(*_LOCAL).exit_code == 0

    filtered = _source()
    _use(monkeypatch, filtered)
    assert (
        _detail(
            *_LOCAL,
            "--decision",
            "not-allocated",
            "--stock",
            "0002",
            "--reason",
            "INSUFFICIENT_CASH_REMAINING",
        ).exit_code
        == 0
    )

    expected = [
        f"allocation_shadow_index:{_DAY}",
        f"allocation_shadow_items:{_DAY}:{_REF}",
    ]
    assert plain.requested == today_source.requested
    assert filtered.requested == plain.requested  # ★ 絞り込みは読取の回数・順序を変えない ★
    assert [r for r in plain.requested if r in expected] == expected
    assert len(plain.requested) == 3  # 索引 1 + 日単位 skip 1 + 符号ごとに 1


# --- --date ----------


def test_an_explicit_non_business_day_is_refused_before_anything_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source("2026-10-17")
    built = _use(monkeypatch, source)
    monkeypatch.setattr(detail_cli, "_today_jst", lambda: dt.date(2026, 10, 20))
    monkeypatch.setattr(cli, "_today_jst", lambda: dt.date(2026, 10, 20))

    result = _detail("--backend", "local", "--date", "2026-10-17")  # 土曜

    assert result.exit_code == 2
    assert "営業日ではありません" in result.output
    assert built == []  # source を作らない
    assert source.requested == []  # GetItem 0 回


def test_an_omitted_date_is_the_latest_business_day_decided_by_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source("2026-10-16")  # 金曜
    _use(monkeypatch, source)
    monkeypatch.setattr(detail_cli, "_today_jst", lambda: dt.date(2026, 10, 17))  # 土曜
    monkeypatch.setattr(cli, "_today_jst", lambda: dt.date(2026, 10, 17))

    result = _detail("--backend", "local")

    assert result.exit_code == 0
    assert "対象日: 2026-10-16" in result.stdout
    assert all("2026-10-16" in r for r in source.requested)


@pytest.mark.parametrize("value", ["2026-10-15", "2026/10/14", "2026-13-01", "x"])
def test_a_future_date_or_a_malformed_date_is_refused(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    built = _use(monkeypatch, _source())
    assert _detail("--backend", "local", "--date", value).exit_code == 2
    assert built == []


# --- 絞り込みの指定の検証 ----------


def test_an_unknown_reason_is_refused_and_the_usable_words_are_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _use(monkeypatch, _source())
    result = _detail(*_LOCAL, "--reason", "NO_SUCH_REASON")
    assert result.exit_code == 2
    for code in REASON_FILTER_CODES:
        assert code in result.output
    assert built == []


def test_every_reason_word_of_the_closed_table_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _source())
    for code in REASON_FILTER_CODES:
        assert _detail(*_LOCAL, "--reason", code).exit_code in (0, 3), code


@pytest.mark.parametrize(
    "extra",
    [
        ["--decision", "maybe"],
        ["--owner", "xyz"],
        ["--owner", "abc"],
        ["--stock", "bad code"],
        ["--stock", ""],
    ],
)
def test_malformed_filters_are_refused_without_reading(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    built = _use(monkeypatch, _source())
    assert _detail(*_LOCAL, *extra).exit_code == 2
    assert built == []


def test_a_filter_with_no_match_says_not_found_and_keeps_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _source())
    result = _detail(*_LOCAL, "--stock", "9999")
    assert result.exit_code == 0  # 日の状態(完全)は絞り込みで変わらない
    assert "該当する候補がありません(絞り込み: 銘柄 9999)" in result.stdout


# --- 終了コード・出力 ----------


def test_a_complete_day_exits_zero_and_an_incomplete_day_exits_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _source())
    complete = _detail(*_LOCAL)
    assert complete.exit_code == 0
    assert "候補 #1" in complete.stdout and "RAER" in complete.stdout
    assert "backend=local" in complete.stderr and "backend=" not in complete.stdout

    _use(monkeypatch, _source(unrecorded=True))
    incomplete = _detail(*_LOCAL)
    assert incomplete.exit_code == 3


def test_a_missing_day_exits_three_and_never_says_zero_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, FakeSource())
    result = _detail(*_LOCAL)
    assert result.exit_code == 3
    assert "その日の索引がありません" in result.stdout
    assert "候補 0 件" not in result.stdout


@pytest.mark.parametrize("kind", list(ReadFailureKind))
def test_a_read_failure_exits_one_with_a_fixed_message_that_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch, kind: ReadFailureKind
) -> None:
    class Failing(FakeSource):
        def get(self, audit_id: str) -> RecordRead:
            raise ReadFailure(kind)

    _use(monkeypatch, Failing())
    result = _detail(*_LOCAL)
    assert result.exit_code == 1
    assert "読取に失敗しました" in result.stderr
    assert result.stdout.strip() == ""
    assert "allocation_shadow" not in result.stderr


def test_the_json_is_on_stdout_only_and_has_no_label_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _source())
    digest = _REF.rsplit(":", 1)[-1]
    monkeypatch.setattr(detail_cli, "_load_labels_or_none", lambda: {digest[:6]: "LABEL-ZZ"})

    plain = _detail(*_LOCAL, "--format", "json")
    parsed = json.loads(plain.stdout)  # 標準出力が JSON だけ
    assert parsed["business_date"] == _DAY
    assert "LABEL-ZZ" not in plain.stdout
    assert parsed["owners"][0]["candidates"][0]["raer_in"]["upside_pct"] == "12.5"

    labelled = _detail(*_LOCAL, "--format", "json", "--with-labels")
    assert json.loads(labelled.stdout)["owners"][0]["label"] == "LABEL-ZZ"

    table = _detail(*_LOCAL)
    assert "LABEL-ZZ" in table.stdout  # 表は既定でラベルを出し、共有の注意を付ける
    assert "表示名" in table.stdout


# --- 書込・Scan・LINE の経路が無い(AST)----------


def test_the_cli_module_has_no_write_scan_clock_or_notification_path() -> None:
    tree = ast.parse(_CLI_PATH.read_text(encoding="utf-8"))
    called = {
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name | ast.Attribute)
    }
    forbidden = {
        "put_item",
        "update_item",
        "delete_item",
        "batch_write_item",
        "scan",
        "query",
        "now",
        "today",
        "utcnow",
        "send",
        "notify",
        "push",
        "open",
        "print",
    }
    assert not called & forbidden
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not [m for m in imports if "line" in m or "notification_service" in m or "boto3" in m]
    assert not [m for m in imports if "allocation_shadow_record" in m or "shadow_compute" in m]


def test_the_optional_items_are_shown_by_the_cli_as_the_user_answered() -> None:
    assert detail_cli.INCLUDE_OPTIONAL is True


@pytest.mark.parametrize(
    ("decision", "shown", "hidden"),
    [
        ("allocated", "候補 #1", "候補 #2"),
        ("not-allocated", "候補 #2", "候補 #1"),
        ("all", "候補 #1", None),
    ],
)
def test_the_decision_word_selects_the_matching_candidates_only(
    monkeypatch: pytest.MonkeyPatch, decision: str, shown: str, hidden: str | None
) -> None:
    _use(monkeypatch, _source())
    result = _detail(*_LOCAL, "--decision", decision)
    assert result.exit_code == 0
    assert shown in result.stdout
    if hidden is not None:
        assert hidden not in result.stdout
    else:
        assert "候補 #2" in result.stdout


def test_the_reason_word_selects_the_matching_candidates_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _source())
    result = _detail(*_LOCAL, "--reason", "INSUFFICIENT_CASH_REMAINING")
    assert "候補 #2" in result.stdout and "候補 #1" not in result.stdout


def test_the_optional_items_appear_in_the_cli_table_and_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USER の回答: 保有評価額・方針の値・経過時間と読取の状態も出す(記録の policy 節の値だけ)。"""
    _use(monkeypatch, _source())
    table = _detail(*_LOCAL).stdout
    for needle in (
        "保有評価額=1,234,567円",
        "RAER の最低基準=10.5",
        "経過時間(ms)=5",
        "公開面へ貼らない",
    ):
        assert needle in table, needle
    parsed = json.loads(_detail(*_LOCAL, "--format", "json").stdout)
    owner = parsed["owners"][0]
    assert owner["policy"]["min_raer_to_allocate"] == "10.5"
    assert owner["run"]["elapsed_ms"] == "5"
    assert owner["candidates"][0]["attributes"]["current_position_value"] == "1,234,567円"


def test_no_unreadable_value_reaches_the_cli_output_even_with_the_optional_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUST-1 を CLI の出力で確かめる: 方針・実行・保有評価額の欄に形の合わない値を入れる。"""
    record = _rich_record()
    junk = "SENTINEL 生の値 zz"
    record["policy"]["min_raer_to_allocate"] = junk
    record["policy"]["tier_quantity_caps"] = {junk: junk}
    record["run"]["elapsed_ms"] = junk
    record["run"]["read_states"] = {junk: junk}
    record["candidates"][0]["current_position_value"] = junk
    source = FakeSource(
        {
            f"allocation_shadow_index:{_DAY}": view_tests._index([_REF]),
            f"allocation_shadow_items:{_DAY}:{_REF}": record,
        }
    )
    _use(monkeypatch, source)
    for arguments in ([], ["--format", "json"], ["--format", "json", "--with-labels"]):
        output = _detail(*_LOCAL, *arguments).stdout
        assert "SENTINEL" not in output, arguments
        assert "不明" in output


def test_the_private_names_of_the_today_command_that_detail_reuses_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detail は today の private 名を再利用する(summary と同じ)。名前が消えたらここで落ちる。"""
    monkeypatch.undo()  # 自動の fixture が差し替えた名前を元へ戻し、本物どうしの同一性を見る
    for name in (
        "_FAILURE_TEXT",
        "_build_source",
        "_default_calendar",
        "_load_labels_or_none",
        "_today_jst",
    ):
        assert hasattr(cli, name), name
        assert getattr(detail_cli, name) is getattr(cli, name), name
    assert callable(cli._build_source) and callable(cli._default_calendar)
    assert callable(cli._load_labels_or_none) and callable(cli._today_jst)
    assert set(cli._FAILURE_TEXT) == set(ReadFailureKind)  # detail が参照する全ての失敗の種別を覆う
