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
from collections.abc import Iterator

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


def _get_logger_names(tree: ast.AST) -> set[str]:
    """`getLogger` を指す名前。`from logging import getLogger as gl` の `gl` も含める。"""
    names = {"getLogger"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "logging":
            names.update(a.asname or a.name for a in node.names if a.name == "getLogger")
    return names


def _is_get_logger_call(node: ast.AST, names: set[str]) -> bool:
    """`logging.getLogger(...)` / `getLogger(...)` / 別名 import した getLogger の呼び出し。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "getLogger"
    return isinstance(func, ast.Name) and func.id in names


def _binding_pairs(target: ast.expr, value: ast.expr) -> Iterator[tuple[ast.expr, ast.expr]]:
    """(束縛先, 値)の対を作る。tuple / list 同士の代入は要素ごとに対応させる。"""
    if (
        isinstance(target, ast.Tuple | ast.List)
        and isinstance(value, ast.Tuple | ast.List)
        and len(target.elts) == len(value.elts)
    ):
        for inner_target, inner_value in zip(target.elts, value.elts, strict=True):
            yield from _binding_pairs(inner_target, inner_value)
    else:
        yield target, value


def _bound_logger_keys(tree: ast.AST) -> set[str]:
    """logger を束縛している式(`logger` / `self._logger` 等)を、ソース断片の文字列で集める。

    対応する束縛の形(PR #436 のレビュー指摘 F1):
        代入            `logger = logging.getLogger(...)`
        注釈つき代入     `logger: logging.Logger = logging.getLogger(...)`
        walrus          `(logger := logging.getLogger(...))`
        tuple 代入      `a, logger = 1, logging.getLogger(...)`
        属性への代入     `self._logger = logging.getLogger(...)`
        別名            `log = logger`(すでに logger と分かっている名前の別名)
        別名 import     `from logging import getLogger as gl` の `gl(...)`
    """
    names = _get_logger_names(tree)
    bound: set[str] = set()

    def is_logger(expression: ast.expr) -> bool:
        if _is_get_logger_call(expression, names):
            return True
        return isinstance(expression, ast.Name | ast.Attribute) and ast.unparse(expression) in bound

    changed = True
    while changed:  # 別名(log = logger)は、束縛の順序に依らず解決するため不動点まで繰り返す
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                pairs = [p for t in node.targets for p in _binding_pairs(t, node.value)]
            elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
                pairs = [(node.target, node.value)]
            else:
                continue
            for target, value in pairs:
                if not isinstance(target, ast.Name | ast.Attribute) or not is_logger(value):
                    continue
                key = ast.unparse(target)
                if key not in bound:
                    bound.add(key)
                    changed = True
    return bound


def _logger_facts(source: str) -> tuple[bool, bool]:
    """(INFO / DEBUG を呼ぶか, ログレベルを宣言しているか)を返す。

    logger は、束縛した式(`_bound_logger_keys` の各形)と、連鎖呼び出し
    (`logging.getLogger(...).info(...)` / `.setLevel(...)`)で見つける。

    **検出できない形(対象外。実際の src には無いことを、`_scan_src` の実測と
    11 件の allowlist の一致で確認している)**
        ・関数の戻り値から得た logger        `logger = make_logger()`
        ・コンテナの要素・引数として渡された logger
          `loggers["x"].info(...)` / `def f(log): log.info(...)`
        ・`getattr(...)` 等の動的な取得
    これらは検出器の限界であり、見えているから安全だとは主張しない。新しい形が src に現れたら、
    検出器を広げる(`test_known_blind_spots_*` がその変更に気づくための固定である)。

    **過剰検出がありうる形(名前の衝突。fail-close の側 = 見逃しではなく余計に拾う)**
    束縛の鍵は式の文字列(`logger` / `self._logger`)であり、scope や代入の順序を見ない
    (flow-insensitive)。そのため次の場合、logger でないものを logger と誤認しうる。
        ・同じ module の別クラスが、同じ属性名(`self._logger`)を別の用途で使う
        ・logger を束縛した変数を、あとで別の値へ再代入し、その後に `.info(...)` を呼ぶ
    誤認の結果は「未宣言と判定される module が増える」ことだけで、
    宣言の無い INFO を通す方向には働かない。
    現在の src に該当は無い(`_scan_src` の実測と 11 件の allowlist の一致)。将来この形が書かれて
    テストが落ちたときは、名前の衝突による過剰検出を疑う(理由が分かりにくい失敗になるため、ここに
    記す。scope 対応にはしていない。`test_known_over_detections_*` が現在の挙動を固定している)。
    """
    tree = ast.parse(source)
    bound = _bound_logger_keys(tree)
    names = _get_logger_names(tree)

    def is_logger(expression: ast.expr) -> bool:
        if _is_get_logger_call(expression, names):
            return True
        return isinstance(expression, ast.Name | ast.Attribute) and ast.unparse(expression) in bound

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


_SUPPORTED_BINDING_FORMS = {
    "annotated assignment": (
        "import logging\nlogger: logging.Logger = logging.getLogger(__name__)\nlogger.info('x')\n"
    ),
    "walrus": "import logging\nif (logger := logging.getLogger(__name__)):\n    logger.info('x')\n",
    "tuple assignment": (
        "import logging\nversion, logger = 1, logging.getLogger(__name__)\nlogger.info('x')\n"
    ),
    "attribute binding": (
        "import logging\nclass A:\n    def __init__(self):\n"
        "        self._logger = logging.getLogger(__name__)\n"
        "    def run(self):\n        self._logger.info('x')\n"
    ),
    "alias of a bound logger": (
        "import logging\nlogger = logging.getLogger(__name__)\nlog = logger\nlog.info('x')\n"
    ),
    "alias defined before use (order independent)": (
        "import logging\ndef f():\n    log.info('x')\n"
        "log = logger\nlogger = logging.getLogger(__name__)\n"
    ),
    "aliased getLogger import": (
        "from logging import getLogger as gl\nlogger = gl(__name__)\nlogger.info('x')\n"
    ),
    "chained call": "import logging\nlogging.getLogger(__name__).info('x')\n",
}


@pytest.mark.parametrize("form", sorted(_SUPPORTED_BINDING_FORMS))
def test_detector_recognizes_every_supported_binding_form(form: str) -> None:
    """PR #436 F1: 代入以外の束縛形でも、INFO を呼ぶ module を「未宣言」として拾う。

    検出器が見ない形があると、その形で logger を束縛した未宣言 module が黙ってすり抜け、
    PR-2〜PR-6 が依存するこの guard の保証が崩れる。
    """
    source = _SUPPORTED_BINDING_FORMS[form]

    assert _logger_facts(source) == (True, False), form


@pytest.mark.parametrize("form", sorted(_SUPPORTED_BINDING_FORMS))
def test_setlevel_is_recognized_through_every_supported_binding_form(form: str) -> None:
    """宣言(setLevel)の側も、同じ束縛形で見つける(宣言済みを未宣言と誤認しない)。"""
    source = _SUPPORTED_BINDING_FORMS[form].replace(".info('x')", ".setLevel(20)")

    assert _logger_facts(source) == (False, True), form


_KNOWN_BLIND_SPOTS = {
    "logger from a function's return value": (
        "import logging\nlogger = make_logger()\nlogger.info('x')\n"
    ),
    "logger from a container element": (
        "import logging\nloggers = {'x': logging.getLogger('x')}\nloggers['x'].info('x')\n"
    ),
    "logger passed as an argument": "def f(log):\n    log.info('x')\n",
    "dynamic lookup": "import logging\ngetattr(logging, 'x').info('x')\n",
}


@pytest.mark.parametrize("form", sorted(_KNOWN_BLIND_SPOTS))
def test_known_blind_spots_are_not_detected(form: str) -> None:
    """検出できない形(検出器の限界)を、固定して記録する。**見えているから安全、とは主張しない。**

    実際の src にこの形の未宣言 module が無いことは、`_scan_src` の実測
    (11 件の allowlist との一致)で確認している。
    新しい形が src に現れたら検出器を広げる。将来この形を検出できるようにしたときは、このテストが
    落ちて気づけるようにしてある(その場合は本テストの期待と `_logger_facts` の docstring を
    更新する)。
    """
    assert _logger_facts(_KNOWN_BLIND_SPOTS[form]) == (False, False), form


_KNOWN_OVER_DETECTIONS = {
    "same attribute name used by another class": (
        "import logging\n"
        "class A:\n    def __init__(self):\n        self._logger = logging.getLogger(__name__)\n"
        "class B:\n    def __init__(self):\n        self._logger = object()\n"
        "    def run(self):\n        self._logger.info('x')\n"
    ),
    "variable rebound to another value after the logger": (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "logger = object()\n"
        "logger.info('x')\n"
    ),
}


@pytest.mark.parametrize("form", sorted(_KNOWN_OVER_DETECTIONS))
def test_known_over_detections_are_fail_close(form: str) -> None:
    """名前の衝突による過剰検出を、固定して記録する(PR #436 のレビュー指摘 F2)。

    束縛の鍵が式の文字列で、scope・代入の順序を見ない(flow-insensitive)ため、logger でない値を
    logger と誤認することがある。**向きは fail-close**(余計に「未宣言」と判定する)で、宣言の無い
    INFO を通す方向には働かない。scope 対応にはしていない(過剰検出の分かりにくさは、docstring と
    本テストで明記する)。将来 scope 対応にしたときは、このテストの期待を更新する。
    """
    assert _logger_facts(_KNOWN_OVER_DETECTIONS[form]) == (True, False), form


def test_non_logger_bindings_are_not_mistaken_for_loggers() -> None:
    """logger でない値の束縛(同じ tuple 代入・注釈つき代入・属性代入)を、logger と誤認しない。"""
    source = (
        "import logging\n"
        "a, b = 1, 2\n"
        "counter: int = 0\n"
        "class A:\n    def __init__(self):\n        self.value = object()\n"
        "counter.info('x')\nb.info('x')\nA().value.info('x')\n"
    )

    assert _logger_facts(source) == (False, False)


def test_tuple_assignment_pairs_elements_by_position() -> None:
    """tuple 代入は位置で対応させる(2 番目だけが logger なら、1 番目を logger と誤認しない)。"""
    source = "import logging\nfirst, second = 1, logging.getLogger(__name__)\nfirst.info('x')\n"

    assert _logger_facts(source) == (False, False)
    assert _logger_facts(source.replace("first.info", "second.info")) == (True, False)


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
