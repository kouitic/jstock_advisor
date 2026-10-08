"""Issue #145: 時刻依存テストの registry と、その registry 自体の健全性を固定する。

## なぜ registry 方式なのか

`#143`(CI が実行時刻で red/green を変える)と `#148`(テストモジュール間の
状態共有)は、いずれも「テストの基準時刻が wall clock に依存していた」ことに
起因する。再発を防ぐには、時刻に敏感なモジュールを**明示的に登録**し、
その状態を実行可能な契約として固定する必要がある。

**全テストファイルを走査する方式は採らない。** TTL・JST utility・利回り計算など、
市場セッションに接触しない wall clock の使用は正当であり、それらまで落とすと
不要な churn を生む。対象は risk-based に登録する。

## registry が黙って死なないこと

登録制の弱点は、registry が古くなると guard そのものが無効化されることである。
そのため本モジュールは **registry 自身の健全性(V1-V8)** を検証する。

- 登録モジュールが削除・rename されたら FAIL(V2)
- 既知の時刻依存モジュールが registry から消えたら FAIL(V8)
- 既存例外(ALLOWED_EXISTING)が解消されたら FAIL し、policy 更新を促す(V7)

最後の1つは意図的な forcing function である。負債が解消されたのに例外指定だけが
永久に残ることを防ぐ。

## wall clock の検出は AST で行う

正規表現による走査は、docstring やコメント中の `datetime.now(` を誤検出する。
実際に `#52` / `#143` の参照実装は解説文中で `datetime.now(` に言及しており、
正規表現では 3 件 / 2 件が誤検出される(実コードでは 0 件)。
これらを FORBIDDEN で登録すると即 FAIL し、「registry から外す」誘因を作る。
したがって **AST の Call ノード**で判定する。

## 本モジュールが行わないこと

- 既存の wall clock 依存コードの修正(それぞれ owner Issue が持つ)
- Production コードの変更
- 静的解析ツールとしての一般化(registry 登録モジュールに対する安定した guard に留める)
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

# Issue #277: registry は tests/support/ へ移動した(conftest からも参照するため)。
# 識別子は 1 文字も変えていない。ここでは import するだけで、検証(V1-V8 / O1-O9)は
# 従来どおり本モジュールが持つ。
from tests.support.time_semantics_registry import (
    _ALLOWED_EXISTING,
    _FORBIDDEN,
    _KNOWN_TIME_SENSITIVE_MODULES,
    _ORDER_CASES,
    _ORDER_SENSITIVE_COHORTS,
    _REGISTRY,
    _SOLO_PREFIX,
    _VALID_POLICIES,
    _VALID_TRIGGERS,
    _cohort_members,
    _Entry,
    _OrderCase,
)

# tests/unit/<this file> -> repository root。
# cwd に依存しない(pytest をどこから起動しても解決できる)。
_REPO_ROOT = Path(__file__).resolve().parents[2]

# --- AST による wall clock 検出 --------------------------------------------------

# 属性名 -> 直前の基底名として許容するもの。
# repo 内で実際に使われている表現(`dt.datetime.now(dt.UTC)` / `dt.date.today()`)を
# 対象化する。alias の網羅は目的としない(静的解析ツールを作らない)。
_CLOCK_CALLS: dict[str, frozenset[str]] = {
    "now": frozenset({"datetime"}),
    "today": frozenset({"date", "datetime"}),
    "time": frozenset({"time"}),
}


def _base_name(node: ast.expr) -> str | None:
    """`dt.datetime` / `datetime` のような基底の末尾名を返す。"""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def count_wall_clock_calls(source: str) -> int:
    """実コード上の wall clock 呼び出し数を返す。

    docstring・コメント・文字列リテラルは AST 上 Call ではないため計上されない。
    """
    total = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr == "utcnow":  # 非推奨。基底によらず wall clock 参照とみなす
            total += 1
            continue
        allowed_bases = _CLOCK_CALLS.get(attr)
        if allowed_bases is None:
            continue
        if _base_name(node.func.value) in allowed_bases:
            total += 1
    return total


def _read(entry: _Entry) -> str:
    return (_REPO_ROOT / entry.module).read_text(encoding="utf-8")


_IDS = [e.module.rsplit("/", 1)[-1] for e in _REGISTRY]


_ORDER_IDS = [c.name for c in _ORDER_CASES]

# --- order case の要素(Issue #744): `<path>` または `<path>::<テスト関数名>` -----------

_NODE_ID_SEPARATOR = "::"


def split_order_spec(spec: str) -> tuple[str, str | None]:
    """order case の要素を (module の path, テスト関数名 | None) に分ける。

    `<path>` は (path, None)、`<path>::<name>` は (path, name)。形式の妥当性は
    `is_well_formed_order_spec()` が別に判定する(ここでは分けるだけ)。
    """
    path, separator, name = spec.partition(_NODE_ID_SEPARATOR)
    return path, (name if separator else None)


def is_well_formed_order_spec(spec: str) -> bool:
    """要素の形式が妥当か。

    - `::` は高々 1 つ(クラス内のテストなど入れ子の node id は指定しない)
    - path 部・テスト関数名が空でない
    - テスト関数名に `[` を含まない(parametrize の id は指定しない)
    """
    if spec.count(_NODE_ID_SEPARATOR) > 1:
        return False
    path, name = split_order_spec(spec)
    if not path.strip():
        return False
    if name is None:
        return True
    return bool(name.strip()) and "[" not in name


def defined_test_names(source: str) -> frozenset[str]:
    """module 直下の関数と、module 直下のクラスのメソッドの名前を返す(AST)。

    関数の内側に入れ子になった同名の補助関数は拾わない(実在しないテストを
    実在すると誤判定しないため)。
    """
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(child.name)
    return frozenset(names)


def find_unresolved_node_ids(
    specs: tuple[str, ...], read_source: Callable[[str], str]
) -> list[str]:
    """node id の指すテスト関数が、その module に実在しない要素を返す(path のみの要素は対象外)。

    `read_source` は module の path から source を返す関数(実装は呼び出し側が与える)。
    ファイルの読み込みと判定を分けているのは、判定そのものを合成した入力で単体テスト
    できるようにするため(『常に空を返す』ような退行を検出できる)。
    """
    unresolved: list[str] = []
    for spec in specs:
        path, name = split_order_spec(spec)
        if name is None:
            continue
        if name not in defined_test_names(read_source(path)):
            unresolved.append(spec)
    return unresolved


def _case_module_paths(case: _OrderCase) -> tuple[str, ...]:
    return tuple(split_order_spec(spec)[0] for spec in case.modules)


# --- O1-O9: order metadata の自己検証 --------------------------------------------


@pytest.mark.parametrize("case", _ORDER_CASES, ids=_ORDER_IDS)
def test_o1_order_case_is_not_empty(case: _OrderCase) -> None:
    assert case.modules, f"{case.name} の modules が空です。"


@pytest.mark.parametrize("case", _ORDER_CASES, ids=_ORDER_IDS)
def test_o2_order_case_modules_are_registered(case: _OrderCase) -> None:
    """順序に現れるモジュールは registry に登録済みであること。"""
    registered = {e.module for e in _REGISTRY}
    unknown = sorted(set(_case_module_paths(case)) - registered)
    assert unknown == [], (
        f"{case.name} が registry 未登録のモジュールを参照しています: {unknown}。"
        "先に registry へ登録してください。"
    )


@pytest.mark.parametrize("case", _ORDER_CASES, ids=_ORDER_IDS)
def test_o3_order_case_has_no_duplicate_module(case: _OrderCase) -> None:
    duplicates = sorted({m for m in case.modules if case.modules.count(m) > 1})
    assert duplicates == [], f"{case.name} に重複モジュールがあります: {duplicates}"


@pytest.mark.parametrize("case", _ORDER_CASES, ids=_ORDER_IDS)
def test_o4_order_case_stays_within_its_cohort(case: _OrderCase) -> None:
    """cohort 外のモジュールを混ぜないこと(cohort の定義が曖昧になるため)。"""
    members = {e.module for e in _cohort_members(case.cohort)}
    outside = sorted(set(_case_module_paths(case)) - members)
    assert outside == [], (
        f"{case.name} が cohort '{case.cohort}' の外のモジュールを参照しています: {outside}"
    )


@pytest.mark.parametrize("case", _ORDER_CASES, ids=_ORDER_IDS)
def test_o5_known_failure_issue_is_not_blank_when_present(case: _OrderCase) -> None:
    """既知失敗を宣言する場合、owner Issue を空にしない。"""
    if not case.known_failure_issue:
        pytest.skip("known_failure_issue を持たない order case は対象外")
    assert case.known_failure_issue.strip().startswith("#"), (
        f"{case.name} の known_failure_issue は '#<番号>' 形式で記載してください: "
        f"{case.known_failure_issue!r}"
    )


@pytest.mark.parametrize("case", _ORDER_CASES, ids=_ORDER_IDS)
def test_o7_order_case_specs_are_well_formed(case: _OrderCase) -> None:
    """要素が `<path>` または `<path>::<テスト関数名>` の形であること(Issue #744)。"""
    malformed = [spec for spec in case.modules if not is_well_formed_order_spec(spec)]
    assert malformed == [], (
        f"{case.name} に形式の不正な要素があります: {malformed}。"
        "`<path>` か `<path>::<テスト関数名>` で記述してください(`::` は 1 つまで・"
        "parametrize の `[...]` は付けない)。"
    )


@pytest.mark.parametrize("case", _ORDER_CASES, ids=_ORDER_IDS)
def test_o8_order_case_test_names_exist_in_their_modules(case: _OrderCase) -> None:
    """node id が指すテスト関数が、そのファイルに実在すること(Issue #744)。

    テストの rename・削除で、宣言した順序が静かに空振りになる(指すテストが無いまま、
    順序だけが残る)のを防ぐ forcing function。実行そのものは自動化しない(数十秒かかり、
    development_workflow.md 3.5.4 が自動化しない方針のため)。metadata で検出できる範囲 =
    『指す先が実在すること』までを固定する。
    """
    unresolved = find_unresolved_node_ids(
        case.modules, lambda path: (_REPO_ROOT / path).read_text(encoding="utf-8")
    )
    assert unresolved == [], (
        f"{case.name} が指すテストが見つかりません: {unresolved}。"
        "テストを rename・削除した場合は order case も更新してください(Issue #744)。"
    )


def test_o9_contamination_case_targets_the_leaking_test() -> None:
    """ORDER_CASE_148_CONTAMINATION は、汚染を残す『テスト』を名指しすること(Issue #744)。

    モジュール単位の順序へ戻すと、汚染を残さないテストが最後に走って汚染が隠れ、
    保護(autouse fixture)を外しても落ちない空振りの順序に戻る(#744 の調査)。
    この case が node id を最低 1 件含むことを固定する。

    また、この順序は、保護(autouse fixture)がある現在は失敗しない(保護を外したときにだけ
    失敗する)ので、`known_failure_issue`(失敗する宣言)を持たないこと(Issue #851)。
    O5 は値があるときの形式しか見ないため、宣言だけが残っても他の guard は落ちない。
    """
    case = next(c for c in _ORDER_CASES if c.name == "ORDER_CASE_148_CONTAMINATION")
    node_ids = [spec for spec in case.modules if split_order_spec(spec)[1] is not None]
    assert node_ids, (
        "ORDER_CASE_148_CONTAMINATION がモジュール単位の順序になっています。"
        "汚染を残すテストを `<path>::<テスト関数名>` で名指ししてください(Issue #744)。"
    )
    # 汚染を残す側(node id)が、汚染を受ける側より先に実行される順序であること。
    first_path, first_name = split_order_spec(case.modules[0])
    assert first_name is not None, "先頭は汚染を残す側のテスト(node id)にしてください。"
    assert first_path != split_order_spec(case.modules[-1])[0]
    assert not case.known_failure_issue, (
        "ORDER_CASE_148_CONTAMINATION は保護がある現在は失敗しない順序です。"
        f"known_failure_issue={case.known_failure_issue!r} を外してください(Issue #851。"
        "実際には失敗しないのに、失敗する宣言が残っていたことが #744 の原因でした)。"
    )


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("tests/unit/a.py", ("tests/unit/a.py", None)),
        ("tests/unit/a.py::test_x", ("tests/unit/a.py", "test_x")),
        ("tests/unit/a.py::", ("tests/unit/a.py", "")),
    ],
)
def test_split_order_spec(spec: str, expected: tuple[str, str | None]) -> None:
    assert split_order_spec(spec) == expected


@pytest.mark.parametrize(
    ("spec", "well_formed"),
    [
        ("tests/unit/a.py", True),
        ("tests/unit/a.py::test_x", True),
        ("tests/unit/a.py::", False),  # テスト関数名が空
        ("::test_x", False),  # path が空
        ("tests/unit/a.py::Cls::test_x", False),  # 入れ子の node id は指定しない
        ("tests/unit/a.py::test_x[case1]", False),  # parametrize の id は付けない
    ],
)
def test_is_well_formed_order_spec(spec: str, well_formed: bool) -> None:
    assert is_well_formed_order_spec(spec) is well_formed


def test_defined_test_names_finds_functions_and_methods_but_not_nested_helpers() -> None:
    newline = chr(10)
    source = newline.join(
        [
            "def test_top():",
            "    def test_nested_helper():",
            "        pass",
            "async def test_async():",
            "    pass",
            "class TestGroup:",
            "    def test_method(self):",
            "        pass",
            "x = 1",
        ]
    )
    assert defined_test_names(source) == {"test_top", "test_async", "test_method"}
    assert "test_nested_helper" not in defined_test_names(source)


def test_find_unresolved_node_ids_reports_only_missing_tests() -> None:
    sources = {"tests/unit/a.py": "def test_exists(): pass"}
    specs = (
        "tests/unit/a.py::test_exists",
        "tests/unit/a.py::test_missing",
        "tests/unit/a.py",  # path のみの要素は対象外
    )
    assert find_unresolved_node_ids(specs, sources.__getitem__) == ["tests/unit/a.py::test_missing"]
    assert find_unresolved_node_ids(("tests/unit/a.py::test_exists",), sources.__getitem__) == []


def test_o6_order_sensitive_cohorts_declare_order_cases() -> None:
    """順序依存と宣言した cohort が ORDER_CASES を持たないことを許さない。

    宣言だけして順序を書かない(= 検証されない)状態を防ぐ。
    """
    covered = {c.cohort for c in _ORDER_CASES}
    missing = sorted(_ORDER_SENSITIVE_COHORTS - covered)
    assert missing == [], (
        f"順序依存と宣言された cohort に ORDER_CASES がありません: {missing}。"
        "検証すべき実行順序を宣言してください(Issue #145)。"
    )


def test_o6_order_sensitive_cohorts_exist_in_registry() -> None:
    """順序依存 cohort 名が registry の cohort と対応していること。"""
    cohorts = {e.cohort for e in _REGISTRY}
    unknown = sorted(_ORDER_SENSITIVE_COHORTS - cohorts)
    assert unknown == [], f"registry に存在しない cohort が順序依存と宣言されています: {unknown}"


# --- V1: registry 非空 -----------------------------------------------------------


def test_v1_registry_is_not_empty() -> None:
    """registry が空になったら FAIL(guard の実質的な無効化を防ぐ)。"""
    assert _REGISTRY, "time semantics registry が空です。登録を削除しないでください(Issue #145)。"


# --- V2: path 実在 ---------------------------------------------------------------


@pytest.mark.parametrize("entry", _REGISTRY, ids=_IDS)
def test_v2_registered_module_exists(entry: _Entry) -> None:
    """登録モジュールが実在すること(削除・rename で FAIL)。"""
    path = _REPO_ROOT / entry.module
    assert path.is_file(), (
        f"registry に登録された {entry.module} が見つかりません。"
        "モジュールを削除・rename した場合は registry も更新してください(Issue #145)。"
    )


def test_v2_paths_are_repository_relative() -> None:
    """registry の path が repository root 基準であること(cwd 非依存)。"""
    for entry in _REGISTRY:
        assert not entry.module.startswith("/"), f"絶対 path を使わないでください: {entry.module}"
        assert entry.module.startswith("tests/"), (
            f"repository root からの相対 path で記述してください: {entry.module}"
        )


# --- V3: 重複なし ----------------------------------------------------------------


def test_v3_no_duplicate_modules() -> None:
    """同一モジュールの二重登録を検出する。

    registry を dict ではなくタプル列で持つのは、dict リテラルだと
    重複キーが黙って後勝ちになり、この検証が成立しないためである。
    """
    modules = [e.module for e in _REGISTRY]
    duplicates = sorted({m for m in modules if modules.count(m) > 1})
    assert duplicates == [], f"registry に重複エントリがあります: {duplicates}"


# --- V4 / V5: cohort -------------------------------------------------------------


@pytest.mark.parametrize("entry", _REGISTRY, ids=_IDS)
def test_v4_cohort_name_is_not_empty(entry: _Entry) -> None:
    assert entry.cohort.strip(), f"{entry.module} の cohort 名が空です。"


@pytest.mark.parametrize("entry", _REGISTRY, ids=_IDS)
def test_v5_cohort_has_at_least_two_members_or_is_explicit_solo(entry: _Entry) -> None:
    """cohort は組み合わせ実行の単位であるため、1 件では意味を成さない。

    相手が存在しない場合のみ `SOLO:` を明示する。暗黙の 1 件は認めない。
    """
    if entry.cohort.startswith(_SOLO_PREFIX):
        members = _cohort_members(entry.cohort)
        assert len(members) == 1, (
            f"{entry.cohort} は SOLO 指定ですが {len(members)} 件が所属しています。"
            "複数所属する場合は SOLO を解除してください。"
        )
        return

    members = _cohort_members(entry.cohort)
    assert len(members) >= 2, (
        f"cohort '{entry.cohort}' のメンバが {len(members)} 件しかありません。"
        f"組み合わせ実行の相手が存在しない場合は '{_SOLO_PREFIX}<name>' を明示してください。"
    )


# --- V6: trigger -----------------------------------------------------------------


@pytest.mark.parametrize("entry", _REGISTRY, ids=_IDS)
def test_v6_triggers_are_valid(entry: _Entry) -> None:
    """trigger が空でなく、T1-T4 のみであること(typo・未知値を通さない)。"""
    assert entry.triggers, f"{entry.module} の triggers が空です。"
    unknown = sorted(set(entry.triggers) - _VALID_TRIGGERS)
    assert unknown == [], (
        f"{entry.module} に未知の trigger があります: {unknown}。"
        f"許容値は {sorted(_VALID_TRIGGERS)} です(docs/development_workflow.md 3.5節)。"
    )


# --- V7: wall clock policy -------------------------------------------------------


@pytest.mark.parametrize("entry", _REGISTRY, ids=_IDS)
def test_v7_policy_value_is_valid(entry: _Entry) -> None:
    assert entry.wall_clock_policy in _VALID_POLICIES, (
        f"{entry.module} の WALL_CLOCK_POLICY が不正です: {entry.wall_clock_policy}。"
        f"許容値は {sorted(_VALID_POLICIES)} です。"
    )


@pytest.mark.parametrize("entry", _REGISTRY, ids=_IDS)
def test_v7_forbidden_modules_have_no_wall_clock_call(entry: _Entry) -> None:
    """FORBIDDEN のモジュールに wall clock 呼び出しが再混入したら FAIL。"""
    if entry.wall_clock_policy != _FORBIDDEN:
        pytest.skip("FORBIDDEN 以外は対象外")
    found = count_wall_clock_calls(_read(entry))
    assert found == 0, (
        f"{entry.module} に wall clock 呼び出しが {found} 件あります。"
        "テストの基準時刻は固定値を使ってください(Issue #143 / #145)。"
    )


@pytest.mark.parametrize("entry", _REGISTRY, ids=_IDS)
def test_v7_allowed_existing_requires_rationale_and_owner(entry: _Entry) -> None:
    """既存例外には理由と owner Issue を必須とする(黙認を許さない)。"""
    if entry.wall_clock_policy != _ALLOWED_EXISTING:
        pytest.skip("ALLOWED_EXISTING 以外は対象外")
    assert entry.rationale.strip(), (
        f"{entry.module} は ALLOWED_EXISTING ですが rationale が空です。"
        "なぜ例外なのかを記述してください。"
    )
    assert entry.related_issue.strip(), (
        f"{entry.module} は ALLOWED_EXISTING ですが related_issue がありません。"
        "解消を担う owner Issue を記載してください。"
    )


@pytest.mark.parametrize("entry", _REGISTRY, ids=_IDS)
def test_v7_allowed_existing_becomes_obsolete_when_debt_is_repaid(entry: _Entry) -> None:
    """既存例外が不要になったことを検出する(意図的な forcing function)。

    負債が解消されたのに例外指定だけが永久に残ることを防ぐ。
    """
    if entry.wall_clock_policy != _ALLOWED_EXISTING:
        pytest.skip("ALLOWED_EXISTING 以外は対象外")
    found = count_wall_clock_calls(_read(entry))
    assert found > 0, (
        f"{entry.module} の wall clock 呼び出しが解消されています"
        f"(owner: {entry.related_issue})。"
        f"既存例外が不要になったため、WALL_CLOCK_POLICY を "
        f"'{_ALLOWED_EXISTING}' から '{_FORBIDDEN}' へ更新してください(Issue #145)。"
    )


# --- V8: 既知モジュールの在籍 ----------------------------------------------------


def test_v8_known_time_sensitive_modules_stay_registered() -> None:
    """既知の時刻依存モジュールが registry から消えていないこと。

    V2(path 実在)と組み合わせることで、削除・rename・登録解除のいずれでも FAIL する。
    固定するのは在籍であり、テスト総数などの時点依存の件数ではない。
    """
    registered = {e.module for e in _REGISTRY}
    missing = sorted(_KNOWN_TIME_SENSITIVE_MODULES - registered)
    assert missing == [], (
        f"既知の時刻依存モジュールが registry から欠落しています: {missing}。"
        "guard を無効化しないでください(Issue #145)。"
    )


# --- 検出器自体の健全性 ----------------------------------------------------------


def test_ast_detector_ignores_comments_and_docstrings() -> None:
    """コメント・docstring・文字列リテラル中の記述を誤検出しないこと。

    正規表現による走査ではここが誤検出となり、#52 / #143 の参照実装が
    FORBIDDEN で即 FAIL してしまう。
    """
    source = '''
"""解説: dt.datetime.now(dt.UTC) を使ってはいけない。"""
# datetime.now() も date.today() も使わない
FORBIDDEN_SNIPPET = "dt.datetime.now(dt.UTC)"
'''
    assert count_wall_clock_calls(source) == 0


def test_ast_detector_finds_real_calls() -> None:
    """実際の呼び出しは検出すること(検出器が空振りしていないこと)。"""
    source = """
import datetime as dt
import time

a = dt.datetime.now(dt.UTC)
b = dt.date.today()
c = time.time()
"""
    assert count_wall_clock_calls(source) == 3
