"""available-cash の show / reconcile の拒否 guard(AWS_LAMBDA_FUNCTION_NAME。#923)の契約テスト。

背景: 保存先は `running_on_lambda()`(AWS_LAMBDA_FUNCTION_NAME が空でない値で設定されているか)
だけで決まる。設定されたシェルで show / reconcile を実行すると、`AvailableCashService()` が
Production の DynamoDB(AvailableCashTable)を読み書きしうる。運用手順書 3.5 節は『ローカル専用』
と述べていたため、実装を記述へ合わせる(#886 / #833 と同じ型)。

固定するもの
  ・環境変数が設定されていたら、show・reconcile は終了コード 2 で拒否する
  ・拒否は、service の構築・読取・書込・ネットワークの呼出の前に起きる(呼ぶと失敗する fake で固定)
  ・環境変数が未設定または空文字なら、従来どおり動く
  ・回帰防止(AST): available_cash.py の全コマンド(@app.command)は、先頭の文で guard を呼ぶ
    (新しいコマンドを足して漏れると落ちる)。guard を呼ぶのは show と reconcile だけ
  ・運用手順書 3.5・33.2 節が『ローカル専用・Production に反映されない・LINE が更新経路』を述べ、
    『CLI で Production を初期化する』と読める文を含まない

時間意味論: 時計・営業日・timezone を扱わない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import ast
import re
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import available_cash as cli_module

_ENV = "AWS_LAMBDA_FUNCTION_NAME"
_GUARD = "_reject_when_aws_env_is_set"
_runner = CliRunner()
_REPO = Path(__file__).resolve().parents[2]
_MANUAL = _REPO / "docs" / "operations_manual.md"

_SHOW = ["show", "--owner", "owner-a"]
_RECONCILE = ["reconcile", "--owner", "owner-a", "--amount", "500000"]
_MODES = [
    pytest.param(_SHOW, "show", id="show"),
    pytest.param(_RECONCILE, "reconcile", id="reconcile"),
]


@pytest.fixture
def forbidden_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """拒否が先に起きるなら 1 回も呼ばれない入口・AWS・ネットワーク。呼ばれたら記録して失敗する。"""
    calls: list[str] = []

    def make(name: str):  # type: ignore[no-untyped-def]
        def forbidden(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"{name} は呼んではならない")

        return forbidden

    monkeypatch.setattr(cli_module, "AvailableCashService", make("AvailableCashService"))
    for target in ("boto3.client", "boto3.resource", "boto3.Session"):
        monkeypatch.setattr(target, make(target))
    monkeypatch.setattr(socket.socket, "connect", make("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", make("socket.create_connection"))
    return calls


@pytest.mark.parametrize(("args", "command"), _MODES)
def test_rejected_before_any_read_write_or_network_call(
    args: list[str], command: str, forbidden_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "available-cash-cli")
    result = _runner.invoke(cli_module.app, args)
    assert result.exit_code == 2, result.output
    assert forbidden_calls == []  # ★ service の構築も 0 回 = 読取・書込 0 件 ★
    assert _ENV in result.output
    assert "拒否" in result.output
    assert command in result.output
    assert "LINE" in result.output  # Production の登録・確認の経路を案内する


@pytest.mark.parametrize("value", ["x", "cli-target-aws-override", "0", "false", " "])
def test_any_non_empty_value_rejects(
    value: str, forbidden_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """値の中身は見ない(running_on_lambda() と同じ定義: 空でなければ設定されている)。"""
    monkeypatch.setenv(_ENV, value)
    for args in (_SHOW, _RECONCILE):
        assert _runner.invoke(cli_module.app, args).exit_code == 2, (value, args)
    assert forbidden_calls == []


class _FakeService:
    """ローカルの動作の確認用。呼ばれたことを記録する。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, owner: str) -> None:
        self.calls.append("get")

    def reconcile(self, owner: str, amount: object, now: object) -> None:
        self.calls.append("reconcile")
        raise ValueError("fake")


@pytest.fixture
def local_service(monkeypatch: pytest.MonkeyPatch) -> _FakeService:
    fake = _FakeService()
    monkeypatch.setattr(cli_module, "AvailableCashService", lambda: fake)
    return fake


@pytest.mark.parametrize(("args", "command"), _MODES)
def test_unset_environment_variable_keeps_the_existing_behavior(
    args: list[str], command: str, local_service: _FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    result = _runner.invoke(cli_module.app, args)
    assert "拒否" not in result.output
    assert result.exit_code != 2
    assert local_service.calls  # service まで進んだ(拒否されていない)


@pytest.mark.parametrize(("args", "command"), _MODES)
def test_empty_environment_variable_is_treated_as_unset(
    args: list[str], command: str, local_service: _FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空文字は未設定として扱う(running_on_lambda() の定義 bool(os.environ.get(...)) と一致)。"""
    monkeypatch.setenv(_ENV, "")
    result = _runner.invoke(cli_module.app, args)
    assert "拒否" not in result.output
    assert local_service.calls


# --- 回帰防止(AST)---------------------------------------------------------------------------

_CLI_FILE = Path(cli_module.__file__).resolve()


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


def _is_app_command(function: ast.FunctionDef) -> bool:
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "command":
            return True
    return False


def _functions() -> list[ast.FunctionDef]:
    tree = ast.parse(_CLI_FILE.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]


def test_every_command_starts_with_the_guard() -> None:
    """@app.command の関数は、先頭の文(docstring の次)で guard を呼ぶ。新コマンドの漏れを検出。"""
    commands = [function for function in _functions() if _is_app_command(function)]
    # 空振りの防止: 2 つのコマンドが見つかっていること(AST の探索が壊れたら落ちる)
    assert {function.name for function in commands} == {
        "show_available_cash",
        "reconcile_available_cash",
    }
    for function in commands:
        assert _is_guard_call(_first_statement_after_docstring(function)), function.name


def test_the_guard_is_called_only_by_the_commands() -> None:
    callers = set()
    for function in _functions():
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == _GUARD
            ):
                callers.add(function.name)
    assert callers == {"show_available_cash", "reconcile_available_cash"}


def test_the_guard_helper_exists_and_is_not_a_command() -> None:
    assert callable(getattr(cli_module, _GUARD))
    helper = next(function for function in _functions() if function.name == _GUARD)
    assert not _is_app_command(helper)


# --- 運用手順書の記述(3.5・33.2。文字列の契約)------------------------------------------------


def _section(heading_pattern: str) -> str:
    text = _MANUAL.read_text(encoding="utf-8")
    match = re.search(heading_pattern, text, flags=re.MULTILINE)
    assert match is not None, heading_pattern
    start = match.start()
    following = re.search(r"^#{2,3} |^---$", text[match.end() :], flags=re.MULTILINE)
    end = match.end() + following.start() if following else len(text)
    return text[start:end]


def test_manual_3_5_says_the_cli_is_local_only_and_not_a_production_path() -> None:
    section = _section(r"^### 3\.5 ")
    assert "ローカル" in section and "Productionの買付余力には反映されません" in section
    assert "LINE" in section and "余力管理" in section
    assert "AWS_LAMBDA_FUNCTION_NAME" in section and "拒否" in section
    # 旧記述(環境変数の有無に関わらず常にローカルのみ読み書きする)は、実装と食い違うため残さない
    assert "有無に関わらず" not in section


def test_manual_33_2_does_not_tell_the_operator_to_initialize_production_with_the_cli() -> None:
    section = _section(r"^### 33\.2 ")
    assert "LINE" in section and "余力管理" in section
    assert "jstock available-cash reconcile" not in section
    assert "Productionには" in section and "反映されない" in section
