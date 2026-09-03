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
from dataclasses import dataclass
from pathlib import Path

import pytest

# tests/unit/<this file> -> repository root。
# cwd に依存しない(pytest をどこから起動しても解決できる)。
_REPO_ROOT = Path(__file__).resolve().parents[2]

_FORBIDDEN = "FORBIDDEN"
_ALLOWED_EXISTING = "ALLOWED_EXISTING"
_VALID_POLICIES = frozenset({_FORBIDDEN, _ALLOWED_EXISTING})

_VALID_TRIGGERS = frozenset({"T1", "T2", "T3", "T4"})

_SOLO_PREFIX = "SOLO:"


@dataclass(frozen=True)
class _Entry:
    """registry の 1 エントリ。

    module            repository root からの相対 path
    triggers          T1-T4(docs/development_workflow.md 3.5節の決定表)
    cohort            同一プロセスで組み合わせ実行する単位。
                      共有状態による相互干渉を検出するための括り。
                      相手が存在しない場合のみ `SOLO:<name>` を使う
    wall_clock_policy FORBIDDEN / ALLOWED_EXISTING
    rationale         ALLOWED_EXISTING では必須
    related_issue     ALLOWED_EXISTING では必須(解消の owner)
    """

    module: str
    triggers: tuple[str, ...]
    cohort: str
    wall_clock_policy: str
    rationale: str = ""
    related_issue: str = ""


# --- cohort の定義根拠 ----------------------------------------------------------
#
# holding_decision_runtime_config cohort:
#   `holding_decision_runtime_config_service` のモジュールレベル
#   `_cached_config` / `_cached_at` を共有する。テスト間でリセットされないため、
#   先行モジュールが設定した mode が後続モジュールへ漏れる(Issue #148)。
#   所属は名前の類似ではなく、**当該 service を実際に構築・操作するか**で判定した。
#
# market_session cohort:
#   市場セッションの日付 semantics と mock provider の系列を共有する。
#   可変のグローバル状態は持たないが、片方を変更したらもう片方も同時に
#   確認すべき「co-update の単位」である(Issue #52 / #143)。

_REGISTRY: tuple[_Entry, ...] = (
    _Entry(
        module="tests/unit/test_holdings_watchlist_handler.py",
        triggers=("T4",),
        cohort="holding_decision_runtime_config",
        wall_clock_policy=_ALLOWED_EXISTING,
        rationale=(
            "既存の wall-clock 由来 fixture(_fresh_price_as_of_date の既定値)。"
            "Issue #145 では修正せず、owner Issue で固定 clock 化するまで"
            "明示的な例外として追跡する。"
        ),
        related_issue="#148",
    ),
    _Entry(
        module="tests/unit/test_holdings_watchlist_handler_integration.py",
        triggers=("T4",),
        cohort="holding_decision_runtime_config",
        wall_clock_policy=_FORBIDDEN,
    ),
    _Entry(
        module="tests/unit/test_holding_decision_regression.py",
        triggers=("T4",),
        cohort="holding_decision_runtime_config",
        wall_clock_policy=_FORBIDDEN,
    ),
    _Entry(
        module="tests/unit/test_holding_decision_runtime_config.py",
        triggers=("T4",),
        cohort="holding_decision_runtime_config",
        wall_clock_policy=_FORBIDDEN,
    ),
    _Entry(
        module="tests/unit/test_holding_decision_service_audit_fields.py",
        triggers=("T4",),
        cohort="holding_decision_runtime_config",
        wall_clock_policy=_ALLOWED_EXISTING,
        rationale=(
            "既存の wall-clock 依存(モジュールレベル _NOW)。"
            "Issue #145 では修正せず、owner Issue で固定 clock 化するまで"
            "明示的な例外として追跡する。"
        ),
        related_issue="#149",
    ),
    _Entry(
        module="tests/unit/test_issue_52_session_aware_future_date.py",
        triggers=("T1", "T2"),
        cohort="market_session",
        wall_clock_policy=_FORBIDDEN,
    ),
    _Entry(
        module="tests/unit/test_issue_143_test_clock_determinism.py",
        triggers=("T1", "T4"),
        cohort="market_session",
        wall_clock_policy=_FORBIDDEN,
    ),
)

# V8: registry から静かに削除して guard を無効化する経路を塞ぐ。
# 固定するのは**モジュールの在籍**であり、テスト総数などの時点依存の件数ではない。
_KNOWN_TIME_SENSITIVE_MODULES = frozenset(
    {
        "tests/unit/test_holdings_watchlist_handler.py",
        "tests/unit/test_holdings_watchlist_handler_integration.py",
        "tests/unit/test_holding_decision_regression.py",
        "tests/unit/test_holding_decision_runtime_config.py",
        "tests/unit/test_holding_decision_service_audit_fields.py",
        "tests/unit/test_issue_52_session_aware_future_date.py",
        "tests/unit/test_issue_143_test_clock_determinism.py",
    }
)


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


def _cohort_members(cohort: str) -> tuple[_Entry, ...]:
    return tuple(e for e in _REGISTRY if e.cohort == cohort)


_IDS = [e.module.rsplit("/", 1)[-1] for e in _REGISTRY]


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
