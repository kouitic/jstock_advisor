"""Issue #413 PR-1: INFO を出す module は、ログレベルを明示的に宣言する(guard)。

## なぜ必要か

Lambda の root logger の既定レベルは WARNING である。module が

    logger = logging.getLogger(__name__)
    logger.info("...")

と書いても、その module が `logger.setLevel(...)` を宣言していなければ、**INFO は CloudWatch Logs へ
出力されない**。Lambda handler は module 直下で `logger.setLevel(logging.INFO)` を宣言しているが、
services 層の 11 module は宣言が無く、書かれた INFO が黙って出力されていない(#413)。

**「出ない」ことが偶然の防波堤になっていた実例が #416 である**(所有者名を含む baseline_id が INFO へ
出るコードがあったが、INFO が出力されなかったため露出しなかった)。そこで、出力の可否を module ごとに
**明示的に宣言**させ(案B。MANAGER 判断 #413 issuecomment-5741203463)、宣言の無い module を新規に
増やさないことを、機械で強制する。

## 何を検証するか

    1 静的(AST): logging.getLogger を束縛し info / debug を呼ぶ module は、同じ module で
      setLevel を宣言する。
      現在未宣言の 11 module は allowlist に置く(= 新規の未宣言 module を**直ちに**禁止できる)。
    2 allowlist の項目が実際に未宣言であること。宣言済みになった module が allowlist に
      残っていたら落ちる
      (各 PR が 1 件ずつ外し、最後に allowlist が空になる)。
    3 実行時: 宣言している module を import し、その logger の level が NOTSET でないこと
      (宣言が実際に効いている。Lambda の root 既定に左右されない)。

## 何を検証しないか

**「INFO を有効にした module が、生の所有者名・holding_id を出さないか」は見ない**
(それは #135 / #416 の AST guard = tests/unit/test_issue_135_no_pii_in_logs.py が見る)。
本テストが保証するのは
「出力の可否が宣言されていること」だけである。INFO を有効にする PR は、そのモジュールが生の値を
出していないかを、有効化の時点で確認する。
"""

from __future__ import annotations

import ast
import importlib
import logging
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"

#: 現在、ログレベルを宣言していない(= INFO が出力されない)module。
#: #413 の Phase A の走査と一致する 11 件。
#: 各 PR が有効化(または WARNING の明示)とともに 1 件ずつ外す。最後に空になる。
#:
#:   PR-2  audit_service / _finalize_recovery / watchlist_batch_finalizer      (D9・D4・D1・D3)
#:   PR-3  recommendation_evaluation_service / weekly_improvement_review_service (D7・D5)
#:   PR-4  watch_state_service                                                 (D4・D1・D5)
#:   PR-5  shareholder_benefit_registry_service / watchlist_display_name /
#:         watchlist_data_cache / cross_validating_impl                        (D6・D4・D8 ほか。
#:         watchlist_data_cache と cross_validating_impl は「明示的に WARNING」)
#:   PR-6  investment_thesis_service                                            (D3。#416 は済み)
_UNDECLARED_ALLOWLIST = {
    "src/jstock_advisor/lambda_handlers/_finalize_recovery.py",
    "src/jstock_advisor/providers/dividend_data/cross_validating_impl.py",
    "src/jstock_advisor/services/audit_service.py",
    "src/jstock_advisor/services/investment_thesis_service.py",
    "src/jstock_advisor/services/recommendation_evaluation_service.py",
    "src/jstock_advisor/services/shareholder_benefit_registry_service.py",
    "src/jstock_advisor/services/watch_state_service.py",
    "src/jstock_advisor/services/watchlist_batch_finalizer.py",
    "src/jstock_advisor/services/watchlist_data_cache.py",
    "src/jstock_advisor/services/watchlist_display_name.py",
    "src/jstock_advisor/services/weekly_improvement_review_service.py",
}

_QUIET_LEVEL_METHODS = {"info", "debug"}


def _is_get_logger_call(node: ast.AST) -> bool:
    """`logging.getLogger(...)` または `getLogger(...)` の呼び出し。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "getLogger"
    return isinstance(func, ast.Name) and func.id == "getLogger"


def _logger_facts(source: str) -> tuple[bool, bool]:
    """(INFO / DEBUG を呼ぶか, ログレベルを宣言しているか)を返す。

    logger は次の 2 形で見つける。
        束縛:  `name = logging.getLogger(...)` の name への `name.info(...)` / `name.setLevel(...)`
        連鎖:  `logging.getLogger(...).info(...)` / `.setLevel(...)`
    """
    tree = ast.parse(source)
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_get_logger_call(node.value):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))

    def is_logger(expression: ast.expr) -> bool:
        return (isinstance(expression, ast.Name) and expression.id in bound) or _is_get_logger_call(
            expression
        )

    calls_quiet_level = False
    declares_level = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not is_logger(node.func.value):
            continue
        if node.func.attr in _QUIET_LEVEL_METHODS:
            calls_quiet_level = True
        elif node.func.attr == "setLevel":
            declares_level = True
    return calls_quiet_level, declares_level


def _scan_src(root: pathlib.Path = _SRC) -> tuple[set[str], set[str]]:
    """(INFO / DEBUG を呼ぶのに宣言が無い module, 宣言している module)の相対パス集合。"""
    undeclared: set[str] = set()
    declared: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        calls_quiet_level, declares_level = _logger_facts(path.read_text(encoding="utf-8"))
        relative = path.relative_to(root.parent).as_posix()
        if declares_level:
            declared.add(relative)
        elif calls_quiet_level:
            undeclared.add(relative)
    return undeclared, declared


# --- 1・2 静的(AST)---------------------------------------------------------------------


def test_no_new_module_calls_info_without_declaring_the_log_level() -> None:
    """新しく INFO / DEBUG を書く module は、ログレベルの宣言を同時に持たなければならない。

    宣言が無いと、Lambda の root 既定(WARNING)により、書いた INFO が黙って出力されない(#413)。
    出力する(setLevel(logging.INFO))か、出力しないと決める(setLevel(logging.WARNING) と理由)かを、
    明示する。INFO を有効にするなら、生の所有者名・holding_id を出さないかも、その時点で確認する
    (#135 / #416)。
    """
    undeclared, _ = _scan_src()

    new_offenders = sorted(undeclared - _UNDECLARED_ALLOWLIST)

    assert new_offenders == [], (
        "logger.info / debug を呼ぶが、ログレベルを宣言していない module がある"
        f"(setLevel を宣言するか、意図して静音にするなら WARNING を明示する): {new_offenders}"
    )


def test_allowlist_contains_only_modules_that_are_still_undeclared() -> None:
    """宣言済みになった module(または INFO を呼ばなくなった module)は、allowlist から外す。

    allowlist は「今は未宣言だが、各 PR が 1 件ずつ解消する」ためのもので、恒久の免除ではない。
    宣言したのに残っていると、後で宣言が消えても検出されない。
    """
    undeclared, _ = _scan_src()

    stale = sorted(_UNDECLARED_ALLOWLIST - undeclared)

    assert stale == [], f"allowlist から外すこと(既に宣言済み、または INFO を呼ばない): {stale}"


def test_allowlist_paths_exist() -> None:
    missing = sorted(p for p in _UNDECLARED_ALLOWLIST if not (_REPO_ROOT / p).exists())

    assert missing == [], f"存在しない module が allowlist にある: {missing}"


# --- 検出器そのものの検証 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "calls_quiet_level", "declares_level"),
    [
        ("import logging\nlogger = logging.getLogger(__name__)\nlogger.info('x')\n", True, False),
        ("import logging\nlogger = logging.getLogger(__name__)\nlogger.debug('x')\n", True, False),
        (
            "import logging\nlogger = logging.getLogger(__name__)\n"
            "logger.setLevel(logging.INFO)\nlogger.info('x')\n",
            True,
            True,
        ),
        (
            "import logging\nlogger = logging.getLogger(__name__)\n"
            "logger.setLevel(logging.WARNING)\nlogger.info('x')\n",
            True,
            True,
        ),
        ("import logging\nlogging.getLogger(__name__).info('x')\n", True, False),
        (
            "import logging\nlogging.getLogger(__name__).setLevel(logging.INFO)\n",
            False,
            True,
        ),
        (
            "import logging\nlogger = logging.getLogger(__name__)\nlogger.warning('x')\n",
            False,
            False,
        ),
        ("import logging\nlogger = logging.getLogger(__name__)\nlogger.error('x')\n", False, False),
        ("logger = something_else()\nlogger.info('x')\n", False, False),
        ("import logging\nother = object()\nother.info('x')\nother.setLevel(1)\n", False, False),
    ],
)
def test_detector_classifies_logger_usage(
    source: str, calls_quiet_level: bool, declares_level: bool
) -> None:
    assert _logger_facts(source) == (calls_quiet_level, declares_level)


def test_scan_reports_an_undeclared_module_and_ignores_a_declared_one(
    tmp_path: pathlib.Path,
) -> None:
    """検出器が実際に「未宣言」を拾う(0 件だから通る、という形にしない)。"""
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "quiet.py").write_text(
        "import logging\nlogger = logging.getLogger(__name__)\nlogger.info('x')\n", encoding="utf-8"
    )
    (root / "pkg" / "declared.py").write_text(
        "import logging\nlogger = logging.getLogger(__name__)\n"
        "logger.setLevel(logging.INFO)\nlogger.info('x')\n",
        encoding="utf-8",
    )

    undeclared, declared = _scan_src(root)

    assert undeclared == {"src/pkg/quiet.py"}
    assert declared == {"src/pkg/declared.py"}


def test_current_undeclared_modules_match_the_allowlist_exactly() -> None:
    """実測(11 件)と allowlist が完全に一致する(#413 の Phase A の測定と同じ件数)。"""
    undeclared, _ = _scan_src()

    assert undeclared == _UNDECLARED_ALLOWLIST
    assert len(_UNDECLARED_ALLOWLIST) == 11


# --- 3 実行時 ------------------------------------------------------------------------------


def _module_name(relative_path: str) -> str:
    return (
        pathlib.PurePosixPath(relative_path)
        .relative_to("src")
        .with_suffix("")
        .as_posix()
        .replace("/", ".")
    )


def _declaring_modules() -> list[str]:
    _, declared = _scan_src()
    return sorted(declared)


@pytest.mark.parametrize("relative_path", _declaring_modules())
def test_declared_level_is_actually_in_effect(relative_path: str) -> None:
    """宣言している module は、import した結果、その logger の level が NOTSET でない。

    「setLevel と書いてある」ことと「実際に効いている」ことは別である(たとえば条件分岐の中や
    関数の中でしか呼ばれない宣言は、import しただけでは効かない)。Lambda の root 既定(WARNING)に
    左右されず INFO を出せる、という宣言の意味を、実行時に確認する。
    """
    module_name = _module_name(relative_path)

    importlib.import_module(module_name)

    assert logging.getLogger(module_name).level != logging.NOTSET, (
        f"{module_name}: import しても logger の level が宣言されていない"
    )


def test_runtime_check_covers_at_least_the_lambda_handlers() -> None:
    """実行時テストの対象が空にならない(宣言している module が 1 件以上ある)。"""
    modules = _declaring_modules()

    assert modules, "宣言している module が 1 件も見つからない(検出器の不具合)"
    assert any("/lambda_handlers/" in m for m in modules)
