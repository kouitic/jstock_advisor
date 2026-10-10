"""holding-decision の compare / backtest の拒否 guard(AWS_LAMBDA_FUNCTION_NAME。#886)の契約テスト。

背景: 保存先は `running_on_lambda()`(AWS_LAMBDA_FUNCTION_NAME が空でない値で設定されているか)
だけで決まる。設定されたシェルで compare / backtest(live)を実行すると、
`HoldingDecisionService.evaluate()` が Production の DynamoDB へ thesis・baseline・AuditLog を
書き込みうる(拒否の guard が無かった)。

固定するもの
  ・環境変数が設定されていたら、compare・backtest(live・replay の全モード)は終了コード 2 で拒否する
  ・拒否は、銘柄の解決(ストアの構築)・設定の読込・provider の構築・各 service の呼出の前に起きる
    (読取・書込・ネットワークの呼出が 0 件。呼ぶと失敗する fake で固定)
  ・環境変数が未設定または空文字なら、従来どおり動く
  ・回帰防止(AST): cli/ の中で compare / backtest の service を呼ぶ関数は、先頭の文で guard を呼ぶ。
    guard を呼ぶのは compare と backtest だけ(他の CLI の挙動を変えない)

時間意味論: 時計・営業日・timezone を扱わない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import holding_decision as cli_module

_ENV = "AWS_LAMBDA_FUNCTION_NAME"
_GUARD = "_reject_when_aws_env_is_set"
_runner = CliRunner()

_COMPARE = ["compare", "--stock-code", "2914", "--source", "mock"]
_BACKTEST_LIVE = ["backtest", "--stock-code", "2914", "--source", "mock"]
_BACKTEST_REPLAY = [
    "backtest",
    "--stock-code",
    "2914",
    "--start-date",
    "2026-08-01",
    "--end-date",
    "2026-08-31",
]
_MODES = [
    pytest.param(_COMPARE, "compare", id="compare"),
    pytest.param(_BACKTEST_LIVE, "backtest", id="backtest-live"),
    pytest.param(_BACKTEST_REPLAY, "backtest", id="backtest-replay"),
]

# CLI の module が名前で参照する、保存・読取・外部取得に進む入口。拒否が先なら 1 つも呼ばれない。
_CLI_ENTRY_POINTS = (
    "resolve_target_stock_codes",
    "load_config",
    "build_real_provider_bundle",
    "build_mock_provider_bundle",
    "run_compare",
    "run_live_comparison",
    "run_history_replay",
    "write_compare_csv",
    "write_backtest_csv",
)


@pytest.fixture
def forbidden_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """拒否が先に起きるなら 1 回も呼ばれない入口・AWS・ネットワーク。呼ばれたら記録して失敗する。"""
    calls: list[str] = []

    def make(name: str):  # type: ignore[no-untyped-def]
        def forbidden(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"{name} は呼んではならない")

        return forbidden

    for name in _CLI_ENTRY_POINTS:
        monkeypatch.setattr(cli_module, name, make(name))
    for target in ("boto3.client", "boto3.resource", "boto3.Session"):
        monkeypatch.setattr(target, make(target))
    monkeypatch.setattr(socket.socket, "connect", make("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", make("socket.create_connection"))
    return calls


@pytest.mark.parametrize(("args", "command"), _MODES)
def test_rejected_before_any_read_write_or_network_call(
    args: list[str], command: str, forbidden_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "holding-decision-cli")
    result = _runner.invoke(cli_module.app, args)
    assert result.exit_code == 2, result.output
    assert forbidden_calls == []
    assert _ENV in result.output
    assert "拒否" in result.output
    assert command in result.output


@pytest.mark.parametrize("value", ["x", "cli-target-aws-override", "0", "false", " "])
def test_any_non_empty_value_rejects(
    value: str, forbidden_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """値の中身は見ない(running_on_lambda() と同じ定義: 空でなければ設定されている)。"""
    monkeypatch.setenv(_ENV, value)
    for args in (_COMPARE, _BACKTEST_LIVE, _BACKTEST_REPLAY):
        result = _runner.invoke(cli_module.app, args)
        assert result.exit_code == 2, (value, args)
    assert forbidden_calls == []


@pytest.mark.parametrize(("args", "command"), _MODES)
def test_unset_environment_variable_keeps_the_existing_behavior(
    args: list[str], command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    result = _runner.invoke(cli_module.app, args)
    assert result.exit_code == 0, result.output
    assert "拒否" not in result.output


@pytest.mark.parametrize(("args", "command"), _MODES)
def test_empty_environment_variable_is_treated_as_unset(
    args: list[str], command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空文字は未設定として扱う(running_on_lambda() の定義 bool(os.environ.get(...)) と一致)。"""
    monkeypatch.setenv(_ENV, "")
    result = _runner.invoke(cli_module.app, args)
    assert result.exit_code == 0, result.output
    assert "拒否" not in result.output


# --- 回帰防止(AST) ---------------------------------------------------------------------------

_CLI_DIR = Path(cli_module.__file__).resolve().parent
_SERVICE_ENTRY_NAMES = frozenset(
    {"run_compare", "run_live_comparison", "run_history_replay", "HoldingDecisionService"}
)


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _first_statement_after_docstring(function: ast.FunctionDef) -> ast.stmt | None:
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[0] if body else None


def _is_guard_call(statement: ast.stmt | None) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == _GUARD
    )


def _functions_in_cli() -> list[tuple[str, ast.FunctionDef]]:
    found: list[tuple[str, ast.FunctionDef]] = []
    for path in sorted(_CLI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                found.append((path.name, node))
    return found


def test_every_cli_function_that_calls_the_services_starts_with_the_guard() -> None:
    """新しく service を呼ぶ CLI が、guard なしで足されたら落ちる。"""
    callers = [
        (file, function)
        for file, function in _functions_in_cli()
        if _called_names(function) & _SERVICE_ENTRY_NAMES
    ]
    # 空振りの防止: compare と backtest が見つかっていること(AST の探索が壊れたら落ちる)
    assert {(file, function.name) for file, function in callers} == {
        ("holding_decision.py", "compare"),
        ("holding_decision.py", "backtest"),
    }
    for file, function in callers:
        assert _is_guard_call(_first_statement_after_docstring(function)), (file, function.name)


def test_the_guard_is_called_only_by_compare_and_backtest() -> None:
    """他の CLI(runtime-config 系・thesis 系・weekly-aggregate 等)の挙動を変えない。"""
    guard_callers = {
        (file, function.name)
        for file, function in _functions_in_cli()
        if _GUARD in _called_names(function)
    }
    assert guard_callers == {
        ("holding_decision.py", "compare"),
        ("holding_decision.py", "backtest"),
    }


def test_the_guard_helper_exists_and_is_not_a_command() -> None:
    assert callable(getattr(cli_module, _GUARD))
    registered = {command.callback.__name__ for command in cli_module.app.registered_commands}
    assert _GUARD not in registered
