"""時刻依存テストの registry(Issue #145)。

## なぜ test ではなくここに置くか

registry は `test_time_semantics_guard.py` の中にあったが、Issue #277 で
`conftest.py` からも参照する必要が生じた。conftest から**テストモジュールを
import する**のは収集経路として不健全である(収集時にテスト本体が実行される)。
そのため、データと語彙だけをここへ**純粋に移動**した。

## この移動で変えていないこと

**識別子を 1 文字も変えていない。** 先頭のアンダースコアも残している。
byte 一致の移動であることを diff で確認できるようにするためである
(公開名へ改名するなら、それは別の変更として行う)。
ロジックも 1 行も変えていない。検証(V1-V8 / O1-O6)は guard 側に残る。
"""

from __future__ import annotations

from dataclasses import dataclass

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


def _cohort_members(cohort: str) -> tuple[_Entry, ...]:
    return tuple(e for e in _REGISTRY if e.cohort == cohort)


# --- order-sensitive cohort ------------------------------------------------------
#
# cohort のメンバを揃えるだけでは、共有 module-global state による汚染を
# 検出できない場合がある。汚染には**方向**があるためである。
#
# 実測(Issue #148):
#   integration -> handler   handler 側 11 件が失敗する
#   handler -> integration   失敗しない
#
# pytest の収集順(アルファベット)は handler -> integration であり、
# full CI ではこの汚染方向を通らない。したがって「cohort を同一プロセスで
# 実行する」だけでは不十分で、**既知・高リスクな順序を明示的に宣言**する必要がある。
#
# 全順列(cohort が 5 件なら 120 通り)は要求しない。組合せ爆発になるうえ、
# 大半は検証価値が無い。宣言された順序のみを対象とする。


@dataclass(frozen=True)
class _OrderCase:
    """cohort 内で明示的に検証する実行順序。

    name                 識別子
    cohort               所属 cohort(cohort 外のモジュールは参照できない)
    modules              実行順。registry 登録済みのモジュールのみ
    known_failure_issue  この順序で既知の失敗が出る場合、その owner Issue。
                         「red だが既知」で済ませず、失敗集合の一致を確認するための印。
    """

    name: str
    cohort: str
    modules: tuple[str, ...]
    known_failure_issue: str = ""


# 順序依存が既知、または合理的に疑われる cohort。
# ここに挙げた cohort は ORDER_CASES を最低 1 件持たなければならない(O6)。
_ORDER_SENSITIVE_COHORTS = frozenset({"holding_decision_runtime_config"})

_ORDER_CASES: tuple[_OrderCase, ...] = (
    _OrderCase(
        name="ORDER_CASE_CANONICAL",
        cohort="holding_decision_runtime_config",
        modules=(
            "tests/unit/test_holdings_watchlist_handler.py",
            "tests/unit/test_holdings_watchlist_handler_integration.py",
        ),
    ),
    _OrderCase(
        name="ORDER_CASE_148_CONTAMINATION",
        cohort="holding_decision_runtime_config",
        modules=(
            "tests/unit/test_holdings_watchlist_handler_integration.py",
            "tests/unit/test_holdings_watchlist_handler.py",
        ),
        known_failure_issue="#148",
    ),
)
