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


# --- ★ cli/*.py の全走査: 保存先が環境変数だけで決まる CLI の一覧(MUST-1。サブちゃんの設計レビュー)

_PACKAGE = _REPO / "src" / "jstock_advisor"
_SRC = _REPO / "src"
_STORE_MODULE = "jstock_advisor.infrastructure.collection_store"
_UNSUPPORTED = "未対応(#953 で判断。保存先は環境変数だけで決まる)"

#: ★ 完全一致の許可リスト ★: collection_store へ到達し(= 保存先が running_on_lambda() で決まる)、
#: ガード(AWS_LAMBDA_FUNCTION_NAME の拒否)も対象を明示する option も持たない CLI と、その理由。
#: 新しい CLI が同じ形で増えたら落ちる。1 件直すたびに 1 行消える(進捗が差分で見える)。
#: ★ 18 件を直すことは #923 の範囲外(#953 が持つ。本テストは『見える一覧』にするだけ)★
_UNGUARDED_CLI_MODULES: dict[str, str] = {
    "analyze.py": _UNSUPPORTED,
    "audit.py": _UNSUPPORTED,
    "baseline_repair.py": _UNSUPPORTED,
    "calibration.py": _UNSUPPORTED,
    "candidate_universe.py": _UNSUPPORTED,
    "decision_performance.py": _UNSUPPORTED,
    "evaluation.py": _UNSUPPORTED,
    "feedback.py": _UNSUPPORTED,
    "holdings.py": _UNSUPPORTED,
    "judgment_safety_shadow.py": _UNSUPPORTED,
    "performance.py": _UNSUPPORTED,
    "review.py": _UNSUPPORTED,
    "rules.py": _UNSUPPORTED,
    "shareholder_benefit.py": _UNSUPPORTED,
    "transactions.py": _UNSUPPORTED,
    "valuation_shadow.py": _UNSUPPORTED,
    "watchlist.py": _UNSUPPORTED,
    # 意図的: 本番の BatchRuns・進捗の表を直接操作する運用コマンド。運用手順書が
    # AWS_LAMBDA_FUNCTION_NAME による本番の選択を前提に書かれている
    "watchlist_screening.py": "意図的(手順書が環境変数による本番の選択を前提にした運用コマンド)",
}


def _module_path(name: str) -> Path | None:
    candidate = _SRC / (name.replace(".", "/") + ".py")
    if candidate.exists():
        return candidate
    package = _SRC / name.replace(".", "/") / "__init__.py"
    return package if package.exists() else None


def _imported_modules(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                target = ".".join(parts + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target.startswith("jstock_advisor"):
                found.add(target)
                found.update(f"{target}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("jstock_advisor"))
    return found


def _import_closure(start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        path = _module_path(module)
        if path is None:
            continue
        seen.add(module)
        stack.extend(_imported_modules(path, module))
    return seen


def _has_command(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr == "command":
                    return True
    return False


def _has_guard(tree: ast.AST) -> bool:
    """AWS_LAMBDA_FUNCTION_NAME の拒否(running_on_lambda の呼出・環境変数名の定数)を持つか。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "AWS_LAMBDA_FUNCTION_NAME":
            return True
        if isinstance(node, ast.Name) and node.id in {"running_on_lambda", "_AWS_OVERRIDE_ENV_VAR"}:
            return True
    return False


_EXPLICIT_TARGET = re.compile(r'"--(?:target|backend|aws-region)"')


def _has_explicit_target(text: str) -> bool:
    """対象(保存先)を明示する option(--target・--backend・--aws-region)を持つか。"""
    return _EXPLICIT_TARGET.search(text) is not None


def _unguarded_cli_modules() -> set[str]:
    found: set[str] = set()
    for path in sorted((_PACKAGE / "cli").glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        if not _has_command(tree):  # main.py のように、副 app を束ねるだけの module は対象外
            continue
        if _STORE_MODULE not in _import_closure(f"jstock_advisor.cli.{path.stem}"):
            continue  # 保存先が環境変数で決まらない(collection_store へ到達しない)
        if _has_guard(tree) or _has_explicit_target(text):
            continue
        found.add(path.name)
    return found


def test_every_cli_that_picks_its_store_from_the_environment_is_guarded_or_listed() -> None:
    """★ cli/*.py の全走査 ★: 保存先が環境変数だけで決まる CLI は、ガードを持つか一覧にある。

    一覧は完全一致(`==`)。新しい CLI が同じ形で増えたら落ちる。available_cash.py は本 PR の是正で
    一覧から外れている(= 是正が入った証拠)。
    """
    assert _unguarded_cli_modules() == set(_UNGUARDED_CLI_MODULES)


def test_the_unguarded_list_is_not_vacuous_and_does_not_hide_a_guarded_module() -> None:
    assert "available_cash.py" not in _UNGUARDED_CLI_MODULES
    assert all(reason.strip() for reason in _UNGUARDED_CLI_MODULES.values())
    # 一覧の各 module は実在し、確かに collection_store へ到達し、ガードを持たない(空振りの防止)
    for name in _UNGUARDED_CLI_MODULES:
        path = _PACKAGE / "cli" / name
        text = path.read_text(encoding="utf-8")
        assert _STORE_MODULE in _import_closure(f"jstock_advisor.cli.{path.stem}"), name
        assert not _has_guard(ast.parse(text)) and not _has_explicit_target(text), name
    # 検出器の確認: ガードを持つ module は、一覧に出ず、ガードとして検出される
    for name in ("allocation.py", "holding_decision.py", "trading_pause.py", "weekly_aggregate.py"):
        assert name not in _UNGUARDED_CLI_MODULES
        assert _has_guard(ast.parse((_PACKAGE / "cli" / name).read_text(encoding="utf-8"))), name


@pytest.mark.parametrize(
    ("source", "guarded"),
    [
        ("from x import running_on_lambda\nrunning_on_lambda()\n", True),
        ('_AWS_OVERRIDE_ENV_VAR = "AWS_LAMBDA_FUNCTION_NAME"\n', True),
        ('import os\nos.environ.get("AWS_LAMBDA_FUNCTION_NAME")\n', True),
        ("import os\nos.environ.get('OTHER')\n", False),
        ("def f():\n    pass\n", False),
    ],
)
def test_the_guard_detector_recognizes_each_form(source: str, guarded: bool) -> None:
    assert _has_guard(ast.parse(source)) is guarded


def test_the_explicit_target_detector_recognizes_the_options() -> None:
    assert _has_explicit_target('typer.Option("local", "--target")')
    assert _has_explicit_target('typer.Option(..., "--backend")')
    assert not _has_explicit_target('typer.Option("x", "--other")')
