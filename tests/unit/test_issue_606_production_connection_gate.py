"""#128 の各機能の『Production 接続 gate』を、コード上で保証する契約テスト(Issue #606)。

## 位置づけ
  Expected Return(#601)・RAER(#602)・Portfolio Allocation(#603)・Capital Rotation(#604)・
  利用者向けの出力(#605)は、次を経ずに Production の BUY / SELL / 保有判断・通知へ接続しない
  (USER 決定: #122 issuecomment-6100082651 / 6054586019):

      shadow -> replay / backtest -> human review -> USER の明示的な承認

  運用の gate は docs/operations_manual.md の新節(§37)が定める(既存の Human Gate の再利用。
  新しい gate 機構・有効化経路を作らない)。本テストは、その gate を**迂回する経路がコード上に
  無いこと**を、PR のたびに全走査で確認する。許可リストの変更は『接続を許す』変更であり、
  USER の承認の記録(Issue 番号)を要する(`_UNCONNECTED_PREFIXES` の各行に根拠の Issue を書く)。

## 固定するもの
  (0) 先行(characterization): 既存の段階的 Human Gate の記述・AllocationShadowMode・出荷 config・
      RuntimeConfigMode の参照元・dormant module の到達性は、本 PR で変更しない
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import yaml

from jstock_advisor.domain.signals.allocation_shadow_config import (
    AllocationShadowMode,
    load_allocation_shadow_config,
)

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_OPERATIONS_MANUAL = _REPO / "docs" / "operations_manual.md"
_SHIPPED_CONFIG_DIR = _REPO / "config"


def _manual() -> str:
    return _OPERATIONS_MANUAL.read_text(encoding="utf-8")


# --- import 連鎖の推移閉包(全走査。列挙しない) ---


def _module_files() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in _PACKAGE.rglob("*.py"):
        relative = path.relative_to(_PACKAGE.parent).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _direct_imports(module: str, path: Path, known: dict[str, Path]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    base = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = base[: len(base) - (node.level - 1)]
                prefix = ".".join(anchor + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            targets = [prefix] + [f"{prefix}.{a.name}" for a in node.names]
        else:
            continue
        for target in targets:
            parts = target.split(".")
            for i in range(1, len(parts) + 1):
                candidate = ".".join(parts[:i])
                if candidate in known:
                    found.add(candidate)
    return found


def _reachable(roots: set[str], known: dict[str, Path]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        stack.extend(_direct_imports(module, known[module], known) - seen)
    return seen


_HANDLER_ROOT_PREFIX = "jstock_advisor.lambda_handlers"
_CLI_ROOT_PREFIX = "jstock_advisor.cli"


def _roots_of(known: dict[str, Path], prefix: str) -> set[str]:
    return {m for m in known if m == prefix or m.startswith(prefix + ".")}


def _production_reachable() -> tuple[set[str], dict[str, Path]]:
    """Lambda handler と CLI の全 module を根として、import で到達する module の集合。"""
    known = _module_files()
    roots = _roots_of(known, _HANDLER_ROOT_PREFIX) | _roots_of(known, _CLI_ROOT_PREFIX)
    assert len(roots) > 20  # handler と CLI を根にできている(空振りでない)
    return _reachable(roots, known), known


def _reachable_from_root(prefix: str) -> set[str]:
    """片方の根(handler だけ / CLI だけ)から import で到達する module の集合。"""
    known = _module_files()
    roots = _roots_of(known, prefix)
    assert roots  # 根が空でない(空振りでない)
    return _reachable(roots, known)


# --- (0) 先行: 既存の仕組みは本 PR で変更しない ---


@pytest.mark.parametrize(
    "heading",
    [
        "### 5.1 GitHub Issue自動起票の設定",
        "### 10.2 Shadow運用手順",
        "### 10.3 Active切替手順",
        "### 10.4 kill switch運用",
        "### 28.2 切替の順序(各段が別の Human Gate。順序を入れ替えない)",
    ],
)
def test_the_existing_staged_human_gates_that_the_new_section_reuses_exist(heading: str) -> None:
    """新節は、これらの既存の Human Gate を再利用する。見出しが消えたら参照先が失われる。"""
    assert any(line.startswith(heading) for line in _manual().splitlines()), heading


def test_allocation_shadow_mode_has_no_active_member() -> None:
    """購入側 Shadow(Q')の mode は OFF / SHADOW のみ。ACTIVE へ進める値そのものが無い。"""
    assert [(m.name, m.value) for m in AllocationShadowMode] == [
        ("OFF", "OFF"),
        ("SHADOW", "SHADOW"),
    ]


def test_the_shipped_allocation_shadow_config_is_shadow_and_never_active() -> None:
    """出荷 config は SHADOW(R1。USER の別承認 = G0。#128)。★ ACTIVE へ進める値は存在しない ★
    (mode の値は OFF / SHADOW のみ = 上のテスト)。SHADOW でも判定・通知・保存・完了処理は変えない
    (本流から隔離された観測だけ。契約テストが固定)。"""
    text = (_SHIPPED_CONFIG_DIR / "allocation_shadow.yaml").read_text(encoding="utf-8")
    assert re.search(r'^mode:\s*"SHADOW"\s*$', text, flags=re.MULTILINE)
    assert load_allocation_shadow_config(_SHIPPED_CONFIG_DIR).enabled is True


def test_runtime_config_mode_is_referenced_only_by_the_approved_modules() -> None:
    """mode(legacy / shadow / active)を扱う src は、承認された module だけ。

    新しい参照元は新しい経路である。

    - 保有判断の機構(既存。§10)
    - `domain/valuation/expected_return_runtime.py`(#602 PR-1。USER 承認: #122 …6100082651)。
      型と実行計画のみで、書込・永続化・CLI・handler を持たず、どこからも到達されない
    """
    referencing = sorted(
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if "RuntimeConfigMode" in path.read_text(encoding="utf-8")
    )
    assert referencing == [
        "cli/holding_decision.py",
        "domain/entities/enums.py",
        "domain/entities/holding_decision.py",
        "domain/signals/holding_decision_execution_plan.py",
        "domain/valuation/expected_return_runtime.py",
        "infrastructure/local_repository/holding_decision_runtime_config_repository.py",
        "services/holding_decision_runtime_config_service.py",
    ]


# Production の判断へ未接続の module。接続は『shadow -> backtest -> human review -> USER 承認』
# の後だけ。接続を許す変更は、この表からの『除外』であり、USER の承認の記録(Issue 番号)を要する。
_UNCONNECTED_PREFIXES = {
    "jstock_advisor.domain.exit_architecture": "#846 / #884(Exit 統合。USER REVIEW 前)",
    "jstock_advisor.domain.valuation.expected_return": "#601 / #602(Expected Return と器)",
    "jstock_advisor.domain.signals.buy_affordability": "#603 M2(購入可能株数の純粋関数)",
    "jstock_advisor.domain.notification.today_actions_view": "#605-A(本日の投資アクションの表示)",
}
# 名前に含まれる語で、将来の module も捕らえる(RAER・配分の最適化・入替など)。
_UNCONNECTED_NAME_TOKENS = (
    "raer",
    "expected_return",
    "portfolio_allocation",
    "allocation_optimizer",
    "capital_rotation",
    "today_actions",
    "buy_affordability",
    "exit_architecture",
    # allocation_policy: ★ 前向きの網 ★(#603 PR-E)。loader は dormant で、今この語を外しても
    # 何も落ちない。実効の網は test_issue_603_pre_allocation_policy_config.py の
    # test_nothing_in_src_imports_the_policy_loader(src の全走査)。handler・CLI から
    # 到達させる変更が入ったとき、この語が test_no_handler_or_cli_reaches_an_unconnected_module
    # を落とす
    "allocation_policy",
    "allocation_inputs",  # #603 PR-F1(入力の組み立て。同じく前向きの網)
    "allocation_shadow_record",  # #603 PR-F2-1(記録の組み立て。同じく前向きの網)
    # signals.rotation_: ★ 前向きの網 ★(#604 P-b1。SUBU-20261014-047 の SHOULD-1)。
    # 資金入替の入替元の適格性(P-a `rotation_source_eligibility`)と、その観測
    # (P-b `rotation_observation*`)。これまでは P-a が `capital_rotation` を import している
    # 『偶然』で落ちていた。名前の網へ入れ、P-b2 以降の `rotation_*` の module も同じ網に入れる。
    # ★ 接続を許す変更ではなく、網を広げる変更 ★
    # (運用手順書 §37.5 が USER の承認を要求するのは『接続を許す変更』)
    "signals.rotation_",
    # rotation_observation: P-b2 の reader(infrastructure)・入力の組み立て(services)・
    # P-b3 の runner の名前。`signals.rotation_` は domain の名前しか捕らえないため、
    # 名前の語でも網に入れる(SUBU 設計レビュー)
    "rotation_observation",
)


def test_unconnected_modules_exist_so_the_check_is_not_vacuous() -> None:
    known = _module_files()
    for prefix in _UNCONNECTED_PREFIXES:
        assert any(m == prefix or m.startswith(prefix + ".") for m in known), prefix


#: ★ Shadow の枠(出荷 SHADOW)が handler から到達してよい『未接続の名前』の module = 完全一致の免除★
#: (#603 PR-F2-2b)。根拠は ★ #122 issuecomment-6138622792【1. 最優先目標】のみ ★:
#: 完了条件『本番BUY候補バッチ内でShadow計算が実行できる』『RAER・資金配分が算出できる』『既存の購入
#: 判断・通知に影響しない』と、末尾『承認済み仕様に収まる限り、追加のHuman Gateは不要』。
#: (kv1 = #122 …6142404655・coefficient_set_id = #602 …6159406045 は到達性の承認ではない。別の条件)
#: ★ token 6 語・prefix 1 行は 1 語も消さない ★。消すと、将来の `raer_*` / `expected_return_*` /
#: `portfolio_allocation*` の module(判断を動かす側を含む)が、無審査で handler へ繋がっても
#: 落ちない。免除は『この完全一致の集合だけ』で、CLI の根には適用しない。11 件目以降が実測で
#: 増えたら 1 行ずつ足す
_SHADOW_FRAME_EXEMPT: Final = {
    "jstock_advisor.domain.signals.allocation_inputs": "#122 …6138622792【1】",
    "jstock_advisor.domain.signals.allocation_policy_config": "#122 …6138622792【1】",
    "jstock_advisor.domain.signals.allocation_shadow_record": "#122 …6138622792【1】",
    "jstock_advisor.domain.signals.portfolio_allocation": "#122 …6138622792【1】",
    "jstock_advisor.domain.signals.portfolio_allocation_types": "#122 …6138622792【1】",
    "jstock_advisor.domain.valuation.expected_return": "#122 …6138622792【1】",
    "jstock_advisor.domain.valuation.raer": "#122 …6138622792【1】",
    "jstock_advisor.domain.valuation.raer_coefficients_config": "#122 …6138622792【1】",
    "jstock_advisor.domain.valuation.raer_composition": "#122 …6138622792【1】",
    "jstock_advisor.domain.valuation.raer_inputs": "#122 …6138622792【1】",
}


#: ★ #877 PR-2b-ii の『限定した Shadow 接続』の局所許可(別表)★。
#: 完全一致 7 件 = 受け手が推移的に到達する exit_architecture の module(package の __init__・
#: determination・evidence・price_facts・price_regime・verdicts・vocabulary)。
#: 根拠 = USER 決定 #122 …6182420023 の 1(PR-2b に限定した Shadow 接続の承認)・
#: サブちゃんの設計レビュー PASS(#877 …6182584823)。
#: ★ `_SHADOW_FRAME_EXEMPT`(免除 10 件)には足していない ★。handler の根にだけ適用し、CLI の根には
#: 適用しない(CLI は到達しない: `ProfitTakingService` の import の閉包に exit_architecture が無い)。
#: ★ 受け手の経由でしか到達できないこと・各行が必要なこと(1 件外すと offender)・受け手を import する
#: src の完全一致を、下のテストが固定する ★
_PRICE_REGIME_SHADOW_REASON: Final = "#877 PR-2b・#122 …6182420023 の 1"
_PRICE_REGIME_SHADOW_CONNECTION: Final = {
    "jstock_advisor.domain.exit_architecture": _PRICE_REGIME_SHADOW_REASON,
    "jstock_advisor.domain.exit_architecture.determination": _PRICE_REGIME_SHADOW_REASON,
    "jstock_advisor.domain.exit_architecture.evidence": _PRICE_REGIME_SHADOW_REASON,
    "jstock_advisor.domain.exit_architecture.price_facts": _PRICE_REGIME_SHADOW_REASON,
    "jstock_advisor.domain.exit_architecture.price_regime": _PRICE_REGIME_SHADOW_REASON,
    "jstock_advisor.domain.exit_architecture.verdicts": _PRICE_REGIME_SHADOW_REASON,
    "jstock_advisor.domain.exit_architecture.vocabulary": _PRICE_REGIME_SHADOW_REASON,
}
#: 受け手(別表の module へ到達する唯一の経路)と、それを import してよい module(完全一致)
_PRICE_REGIME_SHADOW_CONNECTOR: Final = "jstock_advisor.services.price_regime_shadow_service"
_PRICE_REGIME_SHADOW_IMPORTERS: Final = ["lambda_handlers/holdings_watchlist_handler.py"]


#: ★ #604 P-b4 A1 の『資金入替の観測 P』の局所許可(別表)★。
#: 完全一致 13 件 = 新しい handler(`rotation_observation_handler`)の閉包の『未接続の名前』の module
#: (handler・設定・ports・runner・入力の組み立て・reader・Scan の proxy・観測の純粋部分・
#: 記録・予定・入替元の適格性・`capital_rotation`)。
#: 根拠 = USER 決定 #604 …6195577714(P-b4 を統合文書 …6188895158 の §9 どおりに承認。A1・A2 は
#: 追加の確認なしに実装・テスト・PR してよい。出荷は Scheduler = DISABLED かつ mode = OFF)・
#: #122 …6195577047。★ 本番有効化(mode SHADOW・Scheduler ENABLED)・LINE・本番判断への接続は
#: 含まない ★(別の承認)。
#: ★ `_SHADOW_FRAME_EXEMPT`(10 件)にも `_PRICE_REGIME_SHADOW_CONNECTION`(7 件)にも足していない ★。
#: handler の根にだけ適用し、CLI の根には適用しない(CLI は 1 件も到達しない)。
#: ★ 新しい handler の経由でしか到達できないこと・各行が必要なこと(1 件外すと offender)を、
#: 下のテストが固定する ★
_ROTATION_OBSERVATION_REASON: Final = "#604 P-b4 A1・USER #604 …6195577714 / #122 …6195577047"
_ROTATION_OBSERVATION_HANDLER: Final = "jstock_advisor.lambda_handlers.rotation_observation_handler"
_ROTATION_OBSERVATION_CONNECTION: Final = {
    module: _ROTATION_OBSERVATION_REASON
    for module in (
        "jstock_advisor.domain.signals.capital_rotation",
        "jstock_advisor.domain.signals.rotation_observation",
        "jstock_advisor.domain.signals.rotation_observation_reads",
        "jstock_advisor.domain.signals.rotation_observation_record",
        "jstock_advisor.domain.signals.rotation_observation_schedule",
        "jstock_advisor.domain.signals.rotation_source_eligibility",
        "jstock_advisor.infrastructure.aws.rotation_observation_reader",
        "jstock_advisor.infrastructure.aws.rotation_observation_scan",
        _ROTATION_OBSERVATION_HANDLER,
        "jstock_advisor.services.rotation_observation_config",
        "jstock_advisor.services.rotation_observation_inputs",
        "jstock_advisor.services.rotation_observation_ports",
        "jstock_advisor.services.rotation_observation_runner",
    )
}
#: handler の根が到達してよい『未接続の名前』の module = 3 つの表の和集合(完全一致)
_HANDLER_ALLOWED: Final = (
    set(_SHADOW_FRAME_EXEMPT)
    | set(_PRICE_REGIME_SHADOW_CONNECTION)
    | set(_ROTATION_OBSERVATION_CONNECTION)
)


def _is_unconnected_name(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in _UNCONNECTED_PREFIXES) or any(
        token in module for token in _UNCONNECTED_NAME_TOKENS
    )


def _unconnected(reached: set[str]) -> list[str]:
    return sorted(m for m in reached if _is_unconnected_name(m))


def test_no_handler_or_cli_reaches_an_unconnected_module() -> None:
    reached, known = _production_reachable()
    assert "jstock_advisor.domain.entities.enums" in reached  # 推移閉包が実際に深く辿れている
    handler = _reachable_from_root(_HANDLER_ROOT_PREFIX)
    cli = _reachable_from_root(_CLI_ROOT_PREFIX)
    # handler の根: Shadow の枠(完全一致の免除)・#877 PR-2b の局所許可(別表)・#604 P-b4 A1 の
    # 局所許可(別表)だけが到達してよい。どれにも無い名前は 1 つも許さない
    assert [m for m in _unconnected(handler) if m not in _HANDLER_ALLOWED] == []
    # CLI の根: 免除を適用しない(CLI は記録を辞書として読むだけで、重い module へ到達しない)
    assert _unconnected(cli) == []


def test_the_exemption_is_exactly_the_unconnected_modules_the_shadow_frame_reaches() -> None:
    """補償 (a)(b): 免除は『ちょうど実際に到達する未接続の module』= 10 件。

    免除を 1 つ外せばその module が offender に挙がり(= 免除が実際に効いている・網が生きている)、
    免除に無い module が 1 つ増えれば落ちる(10 件目以降は必ず差分として見える)。
    """
    handler = _reachable_from_root(_HANDLER_ROOT_PREFIX)

    reached_unconnected = _unconnected(handler)

    assert len(_SHADOW_FRAME_EXEMPT) == 10
    # handler の根が到達する未接続の module = 免除 10 件 ∪ 別表 7 件(#877)∪ 別表 13 件(#604 A1)
    allowed = _HANDLER_ALLOWED
    assert reached_unconnected == sorted(allowed)
    for module in _SHADOW_FRAME_EXEMPT:  # 免除を外せば、全件が offender に挙がる
        remaining = allowed - {module}
        assert [m for m in reached_unconnected if m not in remaining] == [module]
    assert all(why.startswith("#122 …6138622792【1】") for why in _SHADOW_FRAME_EXEMPT.values())


def test_the_price_regime_connection_table_is_minimal_and_exact() -> None:
    """★ 別表(局所許可)の最小性 ★: 1 件外すと offender に挙がる(死んだ行を溜めない)。件数 7。"""
    handler = _reachable_from_root(_HANDLER_ROOT_PREFIX)
    reached_unconnected = _unconnected(handler)
    allowed = _HANDLER_ALLOWED

    assert len(_PRICE_REGIME_SHADOW_CONNECTION) == 7
    assert set(_PRICE_REGIME_SHADOW_CONNECTION) <= set(reached_unconnected)  # 全行が実際に到達する
    assert not set(_PRICE_REGIME_SHADOW_CONNECTION) & set(_SHADOW_FRAME_EXEMPT)  # 免除表とは別
    for module in _PRICE_REGIME_SHADOW_CONNECTION:  # 1 件外せば、その module が offender に挙がる
        remaining = allowed - {module}
        assert [m for m in reached_unconnected if m not in remaining] == [module]
    assert all(
        why == _PRICE_REGIME_SHADOW_REASON for why in _PRICE_REGIME_SHADOW_CONNECTION.values()
    )
    assert all("exit_architecture" in m for m in _PRICE_REGIME_SHADOW_CONNECTION)


def _reachable_without_expanding(
    roots: set[str], known: dict[str, Path], cut: str | set[str]
) -> set[str]:
    """`cut` の module へは到達するが、その module の import(辺)は辿らない。"""
    cuts = {cut} if isinstance(cut, str) else set(cut)
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        if module in cuts:
            continue
        stack.extend(_direct_imports(module, known[module], known) - seen)
    return seen


def test_the_price_regime_modules_are_reachable_only_through_the_connector() -> None:
    """★ 経由の固定(局所許可の本体)★: 受け手の辺を除いた import グラフでは、handler の根から
    別表の 7 件のどれにも到達しない = 『受け手を通る道しか無い』。

    ★ 7 件の package を import する src は C0 の契約テストが完全一致で固定している(受け手と
    `capital_rotation`)。後者は #604 A1 の別表(新しい handler の経由でしか到達しない)に入るため、
    2 つの import 元の辺を除いて確かめる ★
    """
    known = _module_files()
    roots = _roots_of(known, _HANDLER_ROOT_PREFIX)
    assert _PRICE_REGIME_SHADOW_CONNECTOR in known
    with_connector = _reachable(roots, known)
    assert _PRICE_REGIME_SHADOW_CONNECTOR in with_connector  # 受け手へは到達する(空振りでない)
    assert set(_PRICE_REGIME_SHADOW_CONNECTION) <= with_connector

    cut = _reachable_without_expanding(
        roots,
        known,
        {_PRICE_REGIME_SHADOW_CONNECTOR, "jstock_advisor.domain.signals.capital_rotation"},
    )
    assert not set(_PRICE_REGIME_SHADOW_CONNECTION) & cut
    assert not [m for m in cut if "exit_architecture" in m]


def test_only_the_approved_module_imports_the_connector() -> None:
    known = _module_files()
    importers = sorted(
        path.relative_to(_PACKAGE).as_posix()
        for module, path in known.items()
        if module != _PRICE_REGIME_SHADOW_CONNECTOR
        and _PRICE_REGIME_SHADOW_CONNECTOR in _direct_imports(module, path, known)
    )
    assert importers == _PRICE_REGIME_SHADOW_IMPORTERS


def test_neither_the_cli_root_nor_the_profit_taking_service_reaches_the_price_regime() -> None:
    """★ CLI の根・利確判定サービスの閉包は、別表にも受け手にも到達しない ★(CLI に免除は無い)。"""
    cli = _reachable_from_root(_CLI_ROOT_PREFIX)
    assert not cli & set(_PRICE_REGIME_SHADOW_CONNECTION)
    assert _PRICE_REGIME_SHADOW_CONNECTOR not in cli
    service = "jstock_advisor.services.profit_taking_service"
    closure = _reachable({service}, _module_files())
    assert service in closure
    assert not closure & set(_PRICE_REGIME_SHADOW_CONNECTION)
    assert _PRICE_REGIME_SHADOW_CONNECTOR not in closure


def test_the_rotation_observation_table_is_minimal_and_exact() -> None:
    """★ 別表(#604 A1)の最小性 ★: 1 件外すと offender に挙がる(死んだ行を溜めない)。件数 13。"""
    handler = _reachable_from_root(_HANDLER_ROOT_PREFIX)
    reached_unconnected = _unconnected(handler)

    assert len(_ROTATION_OBSERVATION_CONNECTION) == 13
    assert set(_ROTATION_OBSERVATION_CONNECTION) <= set(reached_unconnected)  # 全行が実際に到達する
    assert not set(_ROTATION_OBSERVATION_CONNECTION) & set(_SHADOW_FRAME_EXEMPT)  # 他の表とは別
    assert not set(_ROTATION_OBSERVATION_CONNECTION) & set(_PRICE_REGIME_SHADOW_CONNECTION)
    for module in _ROTATION_OBSERVATION_CONNECTION:  # 1 件外せば、その module が offender に挙がる
        remaining = _HANDLER_ALLOWED - {module}
        assert [m for m in reached_unconnected if m not in remaining] == [module]
    assert all(
        why == _ROTATION_OBSERVATION_REASON for why in _ROTATION_OBSERVATION_CONNECTION.values()
    )


def test_the_rotation_observation_modules_are_reachable_only_through_the_new_handler() -> None:
    """★ 経由の固定 ★: 新しい handler を根から外すと、別表の 13 件のどれにも到達しない。

    = 既存の BUY / SELL / 保有 / レビュー等の handler からは、P の module へ 1 本の道も無い
    (P を既存の関数へ相乗りさせると、ここで落ちる)。
    """
    known = _module_files()
    assert _ROTATION_OBSERVATION_HANDLER in known
    all_roots = _roots_of(known, _HANDLER_ROOT_PREFIX)
    assert _ROTATION_OBSERVATION_HANDLER in all_roots
    assert set(_ROTATION_OBSERVATION_CONNECTION) <= _reachable(all_roots, known)  # 空振りでない

    others = _reachable(all_roots - {_ROTATION_OBSERVATION_HANDLER}, known)
    assert not set(_ROTATION_OBSERVATION_CONNECTION) & others


def test_nothing_imports_the_new_handler_and_the_cli_reaches_none_of_the_table() -> None:
    known = _module_files()
    importers = sorted(
        path.relative_to(_PACKAGE).as_posix()
        for module, path in known.items()
        if module != _ROTATION_OBSERVATION_HANDLER
        and _ROTATION_OBSERVATION_HANDLER in _direct_imports(module, path, known)
    )
    assert importers == []  # handler は Lambda の入口で、他の module から import されない
    cli = _reachable_from_root(_CLI_ROOT_PREFIX)
    assert not cli & set(_ROTATION_OBSERVATION_CONNECTION)  # CLI に免除は無い


def test_exit_architecture_is_reached_from_the_new_handler_only_via_capital_rotation() -> None:
    """P の閉包は `capital_rotation`(型)経由で exit_architecture の一部へも到達する(7 件の内)。

    ★ 経由は `capital_rotation` だけ ★(C0 の契約テストが、package を import する src の完全一致
    〔`capital_rotation` と受け手〕を固定している)。P の handler 自体は package を import しない。
    """
    known = _module_files()
    capital = "jstock_advisor.domain.signals.capital_rotation"
    cut = _reachable_without_expanding({_ROTATION_OBSERVATION_HANDLER}, known, capital)
    assert capital in cut
    assert not [m for m in cut if "exit_architecture" in m]  # capital_rotation の辺を除くと無い
    full = _reachable({_ROTATION_OBSERVATION_HANDLER}, known)
    reached = {m for m in full if "exit_architecture" in m}
    assert reached  # 空振りでない: 辺を戻すと到達する
    assert reached <= set(_PRICE_REGIME_SHADOW_CONNECTION)  # 別表の 7 件の内だけ


def test_the_connector_is_outside_the_name_net_and_is_not_exempt() -> None:
    """受け手の名前は #606 の token・prefix に当たらない(別表と経由の固定で守る)。免除に無い。"""
    assert not _is_unconnected_name(_PRICE_REGIME_SHADOW_CONNECTOR)
    assert _PRICE_REGIME_SHADOW_CONNECTOR not in _SHADOW_FRAME_EXEMPT


def test_an_exit_architecture_module_outside_the_table_is_still_caught() -> None:
    module = "jstock_advisor.domain.exit_architecture.arbiter"  # 実在するが別表に無い
    allowed = set(_SHADOW_FRAME_EXEMPT) | set(_PRICE_REGIME_SHADOW_CONNECTION)
    assert module in _module_files()
    assert [m for m in _unconnected({module}) if m not in allowed] == [module]


def test_the_cli_root_reaches_none_of_the_exempt_modules() -> None:
    """免除は handler の根だけ。CLI の根は 10 件のどれにも到達しない(記録を辞書で読むだけ)。"""
    cli = _reachable_from_root(_CLI_ROOT_PREFIX)

    assert not cli & set(_SHADOW_FRAME_EXEMPT)


def test_the_token_and_prefix_nets_are_untouched_by_the_exemption() -> None:
    """★ token 6 語・prefix 1 行は 1 語も消えていない(免除は『削除』ではない)★。"""
    for token in (
        "raer",
        "expected_return",
        "portfolio_allocation",
        "allocation_policy",
        "allocation_inputs",
        "allocation_shadow_record",
    ):
        assert token in _UNCONNECTED_NAME_TOKENS, token
    assert "jstock_advisor.domain.valuation.expected_return" in _UNCONNECTED_PREFIXES


def test_the_rotation_modules_are_inside_the_unconnected_name_net() -> None:
    """★ P-a・P-b の module は、capital_rotation の import に頼らず、名前だけで網に入る ★。"""
    for module in (
        "jstock_advisor.domain.signals.rotation_source_eligibility",
        "jstock_advisor.domain.signals.rotation_observation",
        "jstock_advisor.domain.signals.rotation_observation_record",
        "jstock_advisor.domain.signals.rotation_anything_new",  # 将来の module
    ):
        assert _is_unconnected_name(module), module
    assert "signals.rotation_" in _UNCONNECTED_NAME_TOKENS


def test_the_rotation_token_is_not_vacuous_and_not_too_broad() -> None:
    known = _module_files()
    named = sorted(m for m in known if "signals.rotation_" in m)

    assert "jstock_advisor.domain.signals.rotation_source_eligibility" in named
    assert "jstock_advisor.domain.signals.rotation_observation" in named
    # 既存の接続済み module を巻き込んでいない(網は rotation_ で始まる名前だけ)
    assert all(m.rsplit(".", 1)[-1].startswith("rotation_") for m in named)


def test_the_observation_modules_outside_the_signals_package_are_in_the_net_by_name() -> None:
    """★ services・infrastructure の rotation_observation_* も、名前だけで網に入る ★。"""
    for module in (
        "jstock_advisor.services.rotation_observation_inputs",
        "jstock_advisor.services.rotation_observation_runner",  # P-b3(予定)
        "jstock_advisor.infrastructure.aws.rotation_observation_reader",
    ):
        assert _is_unconnected_name(module), module
    assert "rotation_observation" in _UNCONNECTED_NAME_TOKENS


def test_the_observation_token_is_not_vacuous_and_not_too_broad() -> None:
    named = sorted(m for m in _module_files() if "rotation_observation" in m)

    assert "jstock_advisor.services.rotation_observation_inputs" in named
    assert "jstock_advisor.infrastructure.aws.rotation_observation_reader" in named
    assert all(m.rsplit(".", 1)[-1].startswith("rotation_observation") for m in named)


def test_a_synthetic_rotation_module_reached_from_a_handler_is_caught() -> None:
    module = "jstock_advisor.domain.signals.rotation_new_unapproved"

    assert _unconnected({module}) == [module]
    assert module not in _SHADOW_FRAME_EXEMPT


@pytest.mark.parametrize(
    "module",
    [
        "jstock_advisor.domain.valuation.raer_runtime_shadow",  # 将来の raer_* の新 module
        "jstock_advisor.domain.valuation.expected_return_runtime_shadow",  # #601 の仲間
        "jstock_advisor.domain.signals.portfolio_allocation_optimizer_v2",
        "jstock_advisor.domain.signals.allocation_policy_runtime",
        "jstock_advisor.domain.signals.capital_rotation_plan",
    ],
)
def test_a_synthetic_unapproved_module_is_caught_and_is_not_exempt(module: str) -> None:
    """補償 (d): 実在しない名前でも、免除に無ければ未接続の網に掛かる(削除では失われる性質)。"""
    assert module not in _module_files()  # 実在しない合成の名前
    assert _is_unconnected_name(module)
    assert module not in _SHADOW_FRAME_EXEMPT
    assert [m for m in _unconnected({module}) if m not in _SHADOW_FRAME_EXEMPT] == [module]


#: 配分系(名前に allocation を含む)の module で、handler の根から到達してよいもの = 出荷 SHADOW の
#: Shadow の枠(Q')と、その配線(#603 PR-F2-2b)だけ。★ 完全一致 ★(#948 の形を維持)。
#: ★ `_SHADOW_FRAME_EXEMPT`(免除。raer・expected_return 系を含む 10 件)とは別の集合 ★:
#: ここは『名前に allocation を含む到達 module』の完全一致で、免除と重なるが同一ではない
#: (allocation_inputs / allocation_policy_config / allocation_shadow_record /
#: portfolio_allocation(+types)は両方に載る。免除にだけ載る = raer・expected_return 系 /
#: ここにだけ載る = config・service・compute・runner・wiring・鍵の provider)。
#: `lambda_handlers/allocation_shadow_wiring.py` は lambda_handlers 配下 = それ自体が 606 の
#: 『根』で、
#: handler が遅延 import しても静的な露出は減らない(gate を迂回しない保守的な選択)
_ALLOCATION_MODULES_REACHABLE_FROM_HANDLERS = [
    "jstock_advisor.domain.signals.allocation_inputs",
    "jstock_advisor.domain.signals.allocation_policy_config",
    "jstock_advisor.domain.signals.allocation_shadow_config",
    "jstock_advisor.domain.signals.allocation_shadow_record",
    "jstock_advisor.domain.signals.portfolio_allocation",
    "jstock_advisor.domain.signals.portfolio_allocation_types",
    "jstock_advisor.infrastructure.aws.allocation_shadow_key_provider",
    "jstock_advisor.lambda_handlers.allocation_shadow_wiring",
    "jstock_advisor.services.allocation_shadow_compute",
    "jstock_advisor.services.allocation_shadow_runner",
    "jstock_advisor.services.allocation_shadow_service",
]
#: CLI の根から到達してよい配分系の module(#605 最小 CLI + #934 の集計の副コマンド。読取専用。
#: 7 件の完全一致)。5 → 7 の追加 = cli.allocation_summary と、それが数える
#: domain.notification.allocation_shadow_summary。★ 『接続を許す変更』ではない ★(読取専用・GetItem
#: だけ・本番の判断/通知/表示へ未接続)。根拠 = R1 の完了条件 4『CLI から計算結果を確認できる』
#: (#122 …6138622792【1】)・#605 / F-78・集計の内容は USER 指示 #934 …6167918059 / …6168329925。
#: 承認不要の判断 = #934 …6169731888 の 1 節(サブちゃんの設計レビュー …6168788054 と一致)。
#: ★ 重い `allocation_shadow_record`(配分・RAER の module を推移的に import する)・
#: `allocation_shadow_compute` は含めない = CLI は記録を辞書として読むだけ ★。
#: handler の網(_UNCONNECTED_NAME_TOKENS)は外していない
#: 7 → 10 の追加 = `jstock allocation detail`(#605 追加 CLI)の 3 module
#: (cli.allocation_detail・domain.notification.allocation_shadow_detail_view・同 _detail_renderer)。
#: ★ 『接続を許す変更』ではない ★(#934 の summary と同じ class = 読取専用・GetItem だけ・
#: 本番の判断 / 通知 / 表示へ未接続)。
#: 根拠 = USER 指示 #122 …6185062118(R2 の追加 CLI は R1 の記録を読む読取専用を優先・LINE 禁止)・
#: サブちゃんの設計レビュー PASS(#605 …6185799547・…6189256361。『新 module の閉包は自分自身 +
#: 既存の画面モデル系 + 既存 7 件で閉じる・承認不要』)。
#: ★ 新 module は記録の module(allocation_shadow_record・allocation_shadow_compute)・方針の config
#: (allocation_policy_config)を import しない = 閉包は閉じたまま ★
_ALLOCATION_MODULES_REACHABLE_FROM_CLI = [
    "jstock_advisor.cli.allocation",
    "jstock_advisor.cli.allocation_detail",
    "jstock_advisor.cli.allocation_labels",
    "jstock_advisor.cli.allocation_summary",
    "jstock_advisor.domain.notification.allocation_shadow_cli_renderer",
    "jstock_advisor.domain.notification.allocation_shadow_cli_view",
    "jstock_advisor.domain.notification.allocation_shadow_detail_renderer",
    "jstock_advisor.domain.notification.allocation_shadow_detail_view",
    "jstock_advisor.domain.notification.allocation_shadow_summary",
    "jstock_advisor.infrastructure.aws.allocation_shadow_reader",
]


def test_the_only_allocation_modules_reachable_from_handlers_are_the_off_shadow_frame() -> None:
    """handler の根から到達できる配分系 = 出荷 SHADOW の枠(Q')とその配線だけ(完全一致)。"""
    reached = _reachable_from_root(_HANDLER_ROOT_PREFIX)
    assert sorted(m for m in reached if "allocation" in m) == (
        _ALLOCATION_MODULES_REACHABLE_FROM_HANDLERS
    )


def test_the_only_allocation_modules_reachable_from_the_cli_are_the_read_only_ones() -> None:
    """CLI の根から到達できる配分系 = 読取専用の CLI の 10 module だけ(完全一致)。"""
    reached = _reachable_from_root(_CLI_ROOT_PREFIX)
    assert sorted(m for m in reached if "allocation" in m) == _ALLOCATION_MODULES_REACHABLE_FROM_CLI


def test_the_cli_does_not_reach_the_heavy_record_or_compute_modules() -> None:
    """CLI は記録の組み立て・計算の組み立て(配分・RAER を推移的に import する)へ到達しない。"""
    reached = _reachable_from_root(_CLI_ROOT_PREFIX)
    assert not [m for m in reached if "allocation_shadow_record" in m or "shadow_compute" in m]


# --- (1) 有効化の経路の集合は PR の前後で差分 0(全走査) ---
#
# 『#128 の機能を有効にできる経路』= 設定型・実行計画を作る/読む src、環境変数、template、config。
# いずれも**列挙ではなく src・infra・config の全走査**で、許可リストと比べる。
# 新しい参照元が現れたら、それは新しい経路である。本テストが落ちる = 承認の記録を添えて更新する。

_HANDLE_NAMES = (
    "ExpectedReturnRuntimeConfig",
    "ExpectedReturnExecutionPlan",
    "resolve_expected_return_execution_plan",
    "allow_allocation_use",
)
# 上の名前を参照してよい src(定義するモジュール自身だけ。#602 PR-A。未 merge の間は存在しなくてよい)
_HANDLE_ALLOWED_REFERENCERS = {"domain/valuation/expected_return_runtime.py"}
_FEATURE_TOKEN = re.compile(
    r"(allocation|expected_return|raer|capital_rotation|today_actions)", re.IGNORECASE
)


def test_nothing_but_the_defining_module_references_the_expected_return_runtime_handles() -> None:
    """設定型・実行計画を『作る・読む・書く』src は、定義するモジュールの外に 1 つも無い。"""
    referencing = {
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if any(name in path.read_text(encoding="utf-8") for name in _HANDLE_NAMES)
    }
    assert referencing <= _HANDLE_ALLOWED_REFERENCERS, sorted(referencing)


@dataclass(frozen=True)
class _EnvironmentScan:
    names: frozenset[
        str
    ]  # src が読む環境変数名(literal・module 定数・dict・helper 経由を解決した結果)
    resolved_indirectly: frozenset[str]  # うち、literal でなく解決して得た名前
    unresolved: tuple[
        str, ...
    ]  # 名前を解決できなかった読取(file:line expr)。新しい書き方 = 人が確認する
    unclassified_uses: tuple[
        str, ...
    ]  # get / pop / setdefault / [...] / in 以外の os.environ の使い方


def _parse_package(package_root: Path) -> dict[str, tuple[Path, ast.Module]]:
    parsed: dict[str, tuple[Path, ast.Module]] = {}
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root.parent).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        parsed[".".join(parts)] = (path, ast.parse(path.read_text(encoding="utf-8")))
    return parsed


def _string_values(node: ast.expr) -> list[str] | None:
    """文字列 literal、または全値が文字列 literal の dict から、文字列を取り出す。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Dict) and node.values:
        values = [v.value for v in node.values if isinstance(v, ast.Constant)]
        if len(values) == len(node.values) and all(isinstance(v, str) for v in values):
            return [str(v) for v in values]
    return None


def _module_constants(tree: ast.Module) -> dict[str, list[str]]:
    constants: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        values = _string_values(value)
        if isinstance(target, ast.Name) and values is not None:
            constants[target.id] = values
    return constants


def _import_aliases(tree: ast.Module) -> dict[str, tuple[str, str | None]]:
    """名前 -> (import 元 module, 元の名前)。`import m as x` の x は (m, None)。"""
    aliases: dict[str, tuple[str, str | None]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (alias.name, None)
    return aliases


def _scan_environment(package_root: Path) -> _EnvironmentScan:
    parsed = _parse_package(package_root)
    constants = {name: _module_constants(tree) for name, (_, tree) in parsed.items()}
    aliases = {name: _import_aliases(tree) for name, (_, tree) in parsed.items()}

    def resolve(expr: ast.expr, module: str) -> list[str] | None:
        direct = _string_values(expr)
        if direct is not None:
            return direct
        if isinstance(expr, ast.Name):
            if expr.id in constants[module]:
                return constants[module][expr.id]
            origin = aliases[module].get(expr.id)
            if origin and origin[1] and origin[0] in constants:
                return constants[origin[0]].get(origin[1])
            return None
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            origin = aliases[module].get(expr.value.id)
            if origin is None:
                return None
            # `import m as x` は m、`from pkg import m` は pkg.m が、定数を持つ module
            for candidate in (origin[0], f"{origin[0]}.{origin[1]}" if origin[1] else None):
                if candidate in constants and expr.attr in constants[candidate]:
                    return constants[candidate][expr.attr]
            return None
        if isinstance(expr, ast.Subscript):  # {family: "NAME"}[family] -> 辞書の全値
            return resolve(expr.value, module)
        return None

    names: set[str] = set()
    indirect: set[str] = set()
    unresolved: list[str] = []
    unclassified: list[str] = []

    def record(values: list[str], *, literal: bool) -> None:
        names.update(values)
        if not literal:
            indirect.update(values)

    for module, (path, tree) in parsed.items():
        where = path.relative_to(package_root).as_posix()
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Attribute)
                and node.attr in {"environ", "getenv"}
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                continue
            parent = parents.get(node)
            expr: ast.expr | None = None
            if node.attr == "getenv" and isinstance(parent, ast.Call) and parent.func is node:
                expr = parent.args[0] if parent.args else None
            elif (
                isinstance(parent, ast.Attribute)
                and parent.attr in {"get", "pop", "setdefault"}
                and isinstance(parents.get(parent), ast.Call)
            ):
                call = parents[parent]
                assert isinstance(call, ast.Call)
                expr = call.args[0] if call.args else None
            elif isinstance(parent, ast.Subscript) and parent.value is node:
                expr = parent.slice
            elif isinstance(parent, ast.Compare) and node in parent.comparators:
                expr = parent.left
            else:
                unclassified.append(f"{where}:{node.lineno}")
                continue
            if expr is None:
                unresolved.append(f"{where}:{node.lineno} (引数なし)")
                continue
            direct = _string_values(expr)
            if direct is not None and isinstance(expr, ast.Constant):
                record(direct, literal=True)
                continue
            resolved = resolve(expr, module)
            if resolved is not None:
                record(resolved, literal=False)
                continue
            # 関数の引数(`_env_true(name)` のような helper): 呼び出し側の実引数を解決する
            function = parents.get(node)
            while function is not None and not isinstance(function, ast.FunctionDef):
                function = parents.get(function)
            if (
                isinstance(expr, ast.Name)
                and isinstance(function, ast.FunctionDef)
                and expr.id in [a.arg for a in function.args.args]
            ):
                index = [a.arg for a in function.args.args].index(expr.id)
                found_any = False
                for caller_module, (_, caller_tree) in parsed.items():
                    for call_node in ast.walk(caller_tree):
                        if not isinstance(call_node, ast.Call):
                            continue
                        callee = call_node.func
                        callee_name = (
                            callee.id
                            if isinstance(callee, ast.Name)
                            else callee.attr
                            if isinstance(callee, ast.Attribute)
                            else None
                        )
                        if callee_name != function.name:
                            continue
                        actual = (
                            call_node.args[index]
                            if index < len(call_node.args)
                            else next(
                                (k.value for k in call_node.keywords if k.arg == expr.id), None
                            )
                        )
                        values = resolve(actual, caller_module) if actual is not None else None
                        if values is None:
                            unresolved.append(f"{where}:{node.lineno} ({function.name} の引数)")
                        else:
                            found_any = True
                            record(values, literal=False)
                if not found_any:
                    unresolved.append(f"{where}:{node.lineno} ({function.name} の呼び出し側なし)")
                continue
            unresolved.append(f"{where}:{node.lineno} {ast.unparse(expr)}")

    return _EnvironmentScan(
        frozenset(names), frozenset(indirect), tuple(unresolved), tuple(unclassified)
    )


# src が読む環境変数名の既知の一覧(2026-10-11 時点)。**新しい名前が現れたら本テストが落ちる**。
# 落ちたら、その環境変数が #128 の機能(Expected Return・RAER・配分・入替・#605 の出力)を有効に
# できる入口でないかを人が確認し、問題なければ一覧と(必要なら)運用手順書 §37 を更新する。
# token(allocation 等)を名前に含むかには頼らない。
_KNOWN_ENVIRONMENT_VARIABLES: frozenset[str] = frozenset(
    {
        "ALLOW_FULL_MARKET_SCREENING",
        "ALLOCATION_SHADOW_REF_KEY_SECRET_ARN",  # #603 F2-2b(wiring が読む。下の専用テストで固定)
        "AWS_LAMBDA_FUNCTION_NAME",
        "BUY_CANDIDATES_FUNCTION_NAME",
        "BUY_CANDIDATE_QUEUE_URL",
        "BUY_CANDIDATE_SQS_DISPATCH_ENABLED",
        "CANDIDATE_UNIVERSE_CACHE_BUCKET",
        "DYNAMODB_TABLE_PREFIX",
        "EDINET_API_KEY",
        "GITHUB_APP_SECRET_ARN",
        "GITHUB_REPOSITORY",
        "HOLDINGS_WATCHLIST_FUNCTION_NAME",
        "HOLDINGS_WATCHLIST_QUEUE_URL",
        "HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED",
        "INCIDENT_NOTIFICATION_TOPIC_ARN",
        "JSTOCK_CONFIG_DIR",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_CHANNEL_SECRET",
        "LINE_USER_ID",
        "WATCHLIST_DISPATCHER_FUNCTION_NAME",
        "WATCHLIST_SCREENING_QUEUE_NAME",
        "WATCHLIST_SCREENING_QUEUE_URL",
        "WATCHLIST_WORKER_FUNCTION_NAME",
        "WEEKLY_AGGREGATE_READ_ENABLED",
        "WEEKLY_AGGREGATE_WRITE_ENABLED",
        "WEEKLY_EVALUATION_AGGREGATE_TABLE",
    }
)
# template の Parameter と、Lambda の環境変数(Environment.Variables)の既知の名前
_KNOWN_TEMPLATE_PARAMETERS: frozenset[str] = frozenset(
    {
        "AdminPrincipalArn",
        "AllowFullMarketScreening",
        "BuyCandidateReservedConcurrentExecutions",
        "BuyCandidateSqsBatchSize",
        "BuyCandidateSqsDispatchEnabled",
        "DeployPrincipalArn",
        "EdinetApiKeySecretArn",
        "EdinetCredentialRotationVersion",
        "GithubAppSecretArn",
        "GithubRepository",
        "HoldingsWatchlistReservedConcurrentExecutions",
        "HoldingsWatchlistSqsBatchSize",
        "HoldingsWatchlistSqsDispatchEnabled",
        "LineChannelAccessTokenSecretArn",
        "LineChannelSecretSecretArn",
        "LineCredentialRotationVersion",
        "LineUserIdSecretArn",
        # #604 P-b4 A2: 資金入替の観測 P の Scheduler の State(既定 DISABLED。#604 …6195577714)。
        # ENABLED は別の ChangeSet・別の承認(P の mode = config の SHADOW とは別の 2 つ目の gate)。
        # 既定・許可値は test_infra_issue_604_rotation_observation.py が固定する
        "RotationObservationScheduleState",
        "SecretResourcePolicyEnabled",
        "TablePrefix",
        "WatchlistReservedConcurrentExecutions",
        "WatchlistSqsBatchSize",
        "WeeklyAggregateReadEnabled",
        "WeeklyAggregateWriteEnabled",
    }
)
_KNOWN_TEMPLATE_ENVIRONMENT: frozenset[str] = frozenset(
    {
        # #925 kv1: owner の符号の鍵の Secret の ARN(値は Secret への Ref だけで、有効化の入口
        # ではない。下の専用テストで固定)
        "ALLOCATION_SHADOW_REF_KEY_SECRET_ARN",
        "ALLOW_FULL_MARKET_SCREENING",
        "BUY_CANDIDATES_FUNCTION_NAME",
        "BUY_CANDIDATE_QUEUE_URL",
        "BUY_CANDIDATE_SQS_DISPATCH_ENABLED",
        "CANDIDATE_UNIVERSE_CACHE_BUCKET",
        "DYNAMODB_TABLE_PREFIX",
        "EDINET_API_KEY",
        "EDINET_CREDENTIAL_ROTATION_VERSION",
        "GITHUB_APP_SECRET_ARN",
        "GITHUB_REPOSITORY",
        "HOLDINGS_WATCHLIST_FUNCTION_NAME",
        "HOLDINGS_WATCHLIST_QUEUE_URL",
        "HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED",
        "INCIDENT_NOTIFICATION_TOPIC_ARN",
        "JSTOCK_CONFIG_DIR",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_CHANNEL_SECRET",
        "LINE_CREDENTIAL_ROTATION_VERSION",
        "LINE_USER_ID",
        "WATCHLIST_DISPATCHER_FUNCTION_NAME",
        "WATCHLIST_SCREENING_QUEUE_NAME",
        "WATCHLIST_SCREENING_QUEUE_URL",
        "WATCHLIST_WORKER_FUNCTION_NAME",
        "WEEKLY_AGGREGATE_READ_ENABLED",
        "WEEKLY_AGGREGATE_WRITE_ENABLED",
        "WEEKLY_EVALUATION_AGGREGATE_TABLE",
    }
)


def test_the_environment_scan_resolves_constants_dicts_and_helper_arguments(
    tmp_path: Path,
) -> None:
    """走査の自己検証: literal だけでなく、定数・dict・helper・別 module の定数を解決する。"""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "consts.py").write_text('SHARED_ENV = "SHARED_NAME"\n', encoding="utf-8")
    (root / "reader.py").write_text(
        "import os\n"
        "from pkg.consts import SHARED_ENV\n"
        "from pkg import consts\n"
        'LOCAL_ENV = "LOCAL_NAME"\n'
        'BY_KIND = {"a": "DICT_NAME_A", "b": "DICT_NAME_B"}\n'
        "def _flag(name: str) -> bool:\n"
        "    return os.environ.get(name, '') == 'true'\n"
        "def run(kind: str) -> None:\n"
        "    os.environ.get('LITERAL_NAME')\n"
        "    os.environ.get(LOCAL_ENV)\n"
        "    os.environ.get(SHARED_ENV)\n"
        "    os.environ.get(consts.SHARED_ENV)\n"
        "    os.environ.get(BY_KIND[kind])\n"
        "    os.getenv('GETENV_NAME')\n"
        "    os.environ['SUBSCRIPT_NAME']\n"
        "    _flag('HELPER_LITERAL')\n"
        "    _flag(LOCAL_ENV)\n",
        encoding="utf-8",
    )

    scan = _scan_environment(root)

    assert scan.names == {
        "LITERAL_NAME",
        "LOCAL_NAME",
        "SHARED_NAME",
        "DICT_NAME_A",
        "DICT_NAME_B",
        "GETENV_NAME",
        "SUBSCRIPT_NAME",
        "HELPER_LITERAL",
    }
    assert "LITERAL_NAME" not in scan.resolved_indirectly
    assert {
        "LOCAL_NAME",
        "SHARED_NAME",
        "DICT_NAME_A",
        "HELPER_LITERAL",
    } <= scan.resolved_indirectly
    assert scan.unresolved == ()
    assert scan.unclassified_uses == ()


def test_the_environment_scan_reports_what_it_cannot_resolve(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "reader.py").write_text(
        "import os\n"
        "def run(prefix: str) -> None:\n"
        "    os.environ.get(prefix + '_X')\n"
        "    dict(os.environ)\n",
        encoding="utf-8",
    )

    scan = _scan_environment(root)

    assert len(scan.unresolved) == 1 and "reader.py:3" in scan.unresolved[0]
    assert len(scan.unclassified_uses) == 1 and "reader.py:4" in scan.unclassified_uses[0]


_REAL_SCAN = _scan_environment(_PACKAGE)


def test_the_real_scan_is_not_vacuous() -> None:
    """現に存在する書き方(literal・module 定数・dict・helper の引数)をそれぞれ解決できている。"""
    assert "AWS_LAMBDA_FUNCTION_NAME" in _REAL_SCAN.names  # literal
    assert "WEEKLY_EVALUATION_AGGREGATE_TABLE" in _REAL_SCAN.resolved_indirectly  # module 定数
    assert "BUY_CANDIDATES_FUNCTION_NAME" in _REAL_SCAN.resolved_indirectly  # dict の値
    assert "BUY_CANDIDATE_SQS_DISPATCH_ENABLED" in _REAL_SCAN.resolved_indirectly  # helper の引数
    assert len(_REAL_SCAN.resolved_indirectly) >= 8


def test_every_environment_read_in_src_has_a_resolvable_name_and_a_classified_form() -> None:
    """名前を解決できない読取・分類できない使い方が 1 件も無い(新しい書き方は人が確認する)。"""
    assert _REAL_SCAN.unresolved == ()
    assert _REAL_SCAN.unclassified_uses == ()


def test_no_new_environment_variable_is_read_by_src() -> None:
    """src が読む環境変数の名前の集合は、既知の一覧から増えていない(token 名に頼らない)。"""
    new = sorted(_REAL_SCAN.names - _KNOWN_ENVIRONMENT_VARIABLES)
    assert new == [], (
        f"新しい環境変数: {new}。#128 の機能を有効にできる入口でないかを人が確認し、"
        "問題なければ _KNOWN_ENVIRONMENT_VARIABLES を更新する"
    )


#: 名前に #128 の機能の語を含んでよい環境変数 = 鍵の Secret の ARN の 1 つだけ(完全一致)。
#: ★ 有効化の入口ではない ★(Secret への参照を渡すだけ。ON の入口は `allocation_shadow.yaml` の
#: `enabled` だけ)。template 側で #947 が同じ名前について済ませた許可リスト化の鏡
#: (承認の記録 = #925 …6159386050〔USER の構成承認〕。到達性は #122 …6138622792【1】)
_ALLOWED_FEATURE_NAMED_ENVIRONMENT: Final = frozenset({"ALLOCATION_SHADOW_REF_KEY_SECRET_ARN"})
_WIRING_PATH: Final = _PACKAGE / "lambda_handlers" / "allocation_shadow_wiring.py"


def test_no_environment_variable_can_enable_a_128_feature() -> None:
    """名前に #128 の機能の語を含む環境変数は、鍵の Secret の ARN の 1 つだけ(完全一致)。

    補償 (a): `==`(`<=` ではない)で、増えれば落ちる。(b)(c)(d) は下の専用テスト。
    """
    offenders = sorted(n for n in _REAL_SCAN.names if _FEATURE_TOKEN.search(n))
    assert offenders == sorted(_ALLOWED_FEATURE_NAMED_ENVIRONMENT)


def test_the_allowed_environment_name_is_not_a_switch() -> None:
    """補償 (b): 名前が有効化・切替の語を持たない(template 側と同じ検査を src 側にも)。"""
    # template 側と同じ 1 つ
    assert {_KEY_ENV_VARIABLE} == _ALLOWED_FEATURE_NAMED_ENVIRONMENT
    assert not _words(_KEY_ENV_VARIABLE) & _SWITCH_WORDS


def test_the_synthetic_feature_named_variables_are_caught_by_the_allow_list() -> None:
    """補償 (d): 許可リストに無い『機能の語を含む合成の環境変数名』は落ちる(空振り防止)。"""
    for name in (
        "ALLOCATION_SHADOW_ENABLED",
        "RAER_SHADOW_MODE",
        "EXPECTED_RETURN_RUNTIME",
        "CAPITAL_ROTATION_ON",
        "ALLOCATION_SHADOW_REF_KEY_SECRET_ARN_V2",
    ):
        assert _FEATURE_TOKEN.search(name)
        assert name not in _ALLOWED_FEATURE_NAMED_ENVIRONMENT
        assert sorted({name, *_ALLOWED_FEATURE_NAMED_ENVIRONMENT}) != sorted(
            _ALLOWED_FEATURE_NAMED_ENVIRONMENT
        )


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def test_the_key_environment_variable_never_reaches_a_condition() -> None:
    """補償 (c) ★最重要★: 読んだ値は provider の組み立てにしか渡らない。

    = 環境変数で Shadow を ON にできない。

    * src でこの名前を持つのは wiring だけ
    * `os.environ.get(...)` の結果は、`shared_key_provider(...)` の引数にしか渡さない
      (if / bool / 比較 / in / return / 代入のどれにも現れない)
    """
    holders = [
        path.relative_to(_PACKAGE).as_posix()
        for path in sorted(_PACKAGE.rglob("*.py"))
        if _KEY_ENV_VARIABLE in path.read_text(encoding="utf-8")
    ]
    assert holders == ["lambda_handlers/allocation_shadow_wiring.py"]

    tree = ast.parse(_WIRING_PATH.read_text(encoding="utf-8"))
    parents = _parents(tree)
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "environ"
    ]
    assert len(reads) == 1  # 読むのは 1 か所だけ(空振り防止)
    parent = parents[reads[0]]
    assert (
        isinstance(parent, ast.Call) and getattr(parent.func, "id", None) == "shared_key_provider"
    )
    # その呼び出しの結果(provider)は return されるだけで、条件・比較・真偽に使われない
    assert isinstance(parents[parent], ast.Return)
    environ_uses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}
    ]
    assert len(environ_uses) == 1


class _IgnoreTagsLoader(yaml.SafeLoader):
    """CloudFormation の短縮記法(!Ref / !Sub / !GetAtt ...)を、読み飛ばして読み込む loader。"""


def _construct_any(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]


_IgnoreTagsLoader.add_multi_constructor("!", _construct_any)


def _template_names() -> tuple[set[str], set[str]]:
    """(Parameters の名前, Environment.Variables のキー)を template 全体から全走査で集める。"""
    document = yaml.load(  # noqa: S506 - 信頼できる repo 内のファイル。タグは無視する loader
        (_REPO / "infra" / "template.yaml").read_text(encoding="utf-8"), Loader=_IgnoreTagsLoader
    )
    parameters = set(document.get("Parameters", {}))
    variables: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            environment = node.get("Environment")
            if isinstance(environment, dict) and isinstance(environment.get("Variables"), dict):
                variables.update(environment["Variables"])
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(document)
    return parameters, variables


def test_the_template_scan_is_not_vacuous() -> None:
    parameters, variables = _template_names()
    assert len(parameters) > 10 and len(variables) > 10


def test_no_new_template_parameter_or_lambda_environment_variable() -> None:
    """template の Parameter と Lambda の環境変数の名前が、既知の一覧から増えていない。"""
    parameters, variables = _template_names()
    assert sorted(parameters - _KNOWN_TEMPLATE_PARAMETERS) == [], (
        "新しい Parameter。#128 の機能の入口でないかを人が確認し、"
        "_KNOWN_TEMPLATE_PARAMETERS を更新する"
    )
    assert sorted(variables - _KNOWN_TEMPLATE_ENVIRONMENT) == [], (
        "新しい Lambda の環境変数。#128 の機能の入口でないかを人が確認し、"
        "_KNOWN_TEMPLATE_ENVIRONMENT を更新する"
    )


#: #925 kv1(owner の符号の鍵)が template に持ち込む #128 の機能の語の『完全一致』の許可リスト。
#: ★ 接続を許す変更(許可リストの拡張) ★。USER の承認の記録 = #925 の構成承認(HANAKO が提示。
#: PR 本文へ Issue コメント番号を残す)。語は『Secret・その ResourcePolicy・MetricFilter・Alarm・
#: Lambda の環境変数 1 つ』だけで、有効化の入口ではない(下の補償のテストで固定)。
#: 新しい語は新しい経路であり、この一覧へ足す変更は同じ承認を要する。
_A_925_KEY_TOKENS: frozenset[str] = frozenset(
    {
        "ALLOCATION_SHADOW_KEY_UNAVAILABLE_ANOMALY",
        "ALLOCATION_SHADOW_KEY_UNAVAILABLE_CONFIG",
        "ALLOCATION_SHADOW_REF_KEY_SECRET_ARN",
        "AllocationShadow",
        "AllocationShadowKeyAnomalyAlarm",
        "AllocationShadowKeyAnomalyFilter",
        "AllocationShadowKeyConfigIssueAlarm",
        "AllocationShadowKeyConfigIssueFilter",
        "AllocationShadowOwnerRefKeyResourcePolicy",
        "AllocationShadowOwnerRefKeyV1",
        # `-` 区切りの名前(Secret の Name・Alarm の名前)。`-` を含めて 1 つの語と数える。
        # 『allocation』だけを許可すると、将来の `allocation-…` の名前が黙って通るため全体で固定する
        "-allocation-shadow-key-anomaly",
        "-allocation-shadow-key-config-issue",
        "allocation-shadow-owner-ref-key-v1",
    }
)
_KEY_SECRET_ID = "AllocationShadowOwnerRefKeyV1"
_KEY_ENV_VARIABLE = "ALLOCATION_SHADOW_REF_KEY_SECRET_ARN"
_KEY_RESOURCE_TYPES = frozenset(
    {
        "AWS::SecretsManager::Secret",
        "AWS::SecretsManager::ResourcePolicy",
        "AWS::Logs::MetricFilter",
        "AWS::CloudWatch::Alarm",
    }
)
#: 『有効化・切替の入口』を示す語(論理 ID・環境変数・Parameter の名前の語に持たせない)
_SWITCH_WORDS = frozenset(
    {"mode", "enabled", "enable", "active", "activate", "on", "off", "switch", "toggle", "flag"}
)
_TOKEN_PATTERN = re.compile(
    r"[A-Za-z0-9_-]*(?:allocation|expected[_-]?return|raer)[\w-]*", re.IGNORECASE
)


def _template_document() -> dict:  # type: ignore[type-arg]
    return yaml.load(  # noqa: S506 - 信頼できる repo 内のファイル。タグは無視する loader
        (_REPO / "infra" / "template.yaml").read_text(encoding="utf-8"), Loader=_IgnoreTagsLoader
    )


def _words(name: str) -> set[str]:
    """名前を語へ分ける(`_`・`-`・`/`・camelCase の境界)。"""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {w.lower() for w in re.split(r"[\s_\-/]+", spaced) if w}


def test_the_template_has_only_the_approved_128_feature_words() -> None:
    """infra/template.yaml を全文走査: #128 の機能の語は、承認した許可リストと『完全一致』する。

    ★ #925 kv1 で、全文に 1 つも許さない assert から、完全一致の許可リストへ変えた(接続を許す変更)。
    同時に、大文字小文字を区別しない走査へ強化した(全大文字の名前のすり抜けを閉じる)。
    許可リストに無い語が 1 つでも増えれば落ちる。緩めた分は下の補償のテストで固定する。
    """
    template = (_REPO / "infra" / "template.yaml").read_text(encoding="utf-8")
    assert set(_TOKEN_PATTERN.findall(template)) == set(_A_925_KEY_TOKENS)


@pytest.mark.parametrize(
    "text",
    [
        "Name: allocation-shadow-items-table",  # `-` 区切りの小文字(許可した語に吸収されない)
        "TableName: !Sub ${AWS::StackName}-allocation-shadow-items",
        "AllocationShadowItemsTable:",  # CamelCase
        "ALLOCATION_SHADOW_ITEMS_TABLE: x",  # 全大文字
        "expected-return-store",
        "raer-coefficients",
    ],
)
def test_the_scan_catches_a_new_name_in_every_spelling(text: str) -> None:
    """走査は、許可した語に似た新しい名前を、`-` 区切り・CamelCase・全大文字のどれでも捕まえる。"""
    found = set(_TOKEN_PATTERN.findall(text))
    assert found and not found <= _A_925_KEY_TOKENS


def test_the_approved_words_are_the_key_secret_and_its_observation_not_an_entry_point() -> None:
    """補償: 許可した語は鍵の Secret とその観測(環境変数 1 つを含む)だけで、入口でない。"""
    document = _template_document()
    resources = document["Resources"]

    # (1) Parameter に #128 の機能の語を持たせない(有効化の入口を作らない)
    assert [n for n in document.get("Parameters", {}) if _TOKEN_PATTERN.search(n)] == []

    # (2) 語を持つ資源は 4 種類の Type だけ。Function・Table・Queue・Topic・Rule 等が無い
    featured = {
        logical_id: resource["Type"]
        for logical_id, resource in resources.items()
        if _TOKEN_PATTERN.search(logical_id)
    }
    assert featured, "許可した資源が template に無い(テストが空振り)"
    assert set(featured.values()) <= _KEY_RESOURCE_TYPES, featured

    # (3) 論理 ID・環境変数の名前に『有効化・切替』の語が無い
    names = [*featured, _KEY_ENV_VARIABLE]
    assert [n for n in names if _words(n) & _SWITCH_WORDS] == []

    # (4) 語を持つ環境変数は 1 つだけ。値は Secret への Ref だけ(固定の文字列・Parameter でない)
    holders: list[tuple[str, str, object]] = []
    for logical_id, resource in resources.items():
        variables = resource.get("Properties", {}).get("Environment", {}).get("Variables", {}) or {}
        holders += [
            (logical_id, name, value)
            for name, value in variables.items()
            if _TOKEN_PATTERN.search(name)
        ]
    assert sorted({name for _, name, _ in holders}) == [_KEY_ENV_VARIABLE]
    assert {logical_id for logical_id, _, _ in holders} == {
        "BuyCandidatesFunction",
        "BuyCandidateWorkerFunction",
    }
    # tag を無視する loader では、`!Ref X` は対象の名前 X になる
    assert {value for _, _, value in holders} == {_KEY_SECRET_ID}

    # (5) Secret: 値を template に持たない(GenerateSecretString だけ)・Retain・Name を固定
    secret = resources[_KEY_SECRET_ID]
    assert secret["Type"] == "AWS::SecretsManager::Secret"
    assert secret["DeletionPolicy"] == "Retain" and secret["UpdateReplacePolicy"] == "Retain"
    properties = secret["Properties"]
    assert "SecretString" not in properties and "GenerateSecretString" in properties
    # 256 ビット以上(英数字 62 種なら 43 文字以上)
    assert properties["GenerateSecretString"]["PasswordLength"] >= 43
    assert properties["GenerateSecretString"]["ExcludePunctuation"] is True

    # (6) ResourcePolicy は既存の Condition に相乗り(既定では作られない)
    policy = resources["AllocationShadowOwnerRefKeyResourcePolicy"]
    assert policy["Condition"] == "SecretResourcePolicyIsEnabled"

    # (7) 観測(MetricFilter・Alarm)は IncidentNotificationTopic だけを通知先にする
    for logical_id, resource in resources.items():
        if logical_id in featured and resource["Type"] == "AWS::CloudWatch::Alarm":
            assert resource["Properties"]["AlarmActions"] == ["IncidentNotificationTopic"]


#: #128 の機能の名前を持つ config と、その『役割』。全走査で許可リストと比べる
#: (新しい config は新しい経路)。allocation_policy.yaml は方針値の置き場
#: (#603 PR-E。USER 承認: #122 …6114810773【3】判断 B・…6137093887・…6138622792)で、
#: 有効化の入口ではない(下の専用テストで固定)。接続を許す変更ではない
_A_128_CONFIG_FILES = {
    "allocation_policy.yaml": "POLICY_VALUES_ONLY",
    "allocation_shadow.yaml": "SHADOW_MODE_SWITCH_SHIPPED_OFF",
    # #602 PR-E: RAER の承認済みの係数の置き場。有効化の入口ではない(下の専用テストで固定)
    "raer_coefficients.yaml": "COEFFICIENT_VALUES_ONLY",
    # #604 P-b4 A1: 資金入替の観測 P の mode(OFF / SHADOW のみ。出荷 OFF。購入側とは別の入口)。
    # USER 承認: #604 …6195577714。完全一致で承認する(下の専用テストで中身を固定)
    "rotation_observation.yaml": "P_MODE_SWITCH_SHIPPED_OFF",
}


#: config 名の網(#606)。`_FEATURE_TOKEN` に P の名前(`rotation_observation`)を足したもの。
#: ★ 網を広げる変更 ★(P の設定ファイルは、下の完全一致の許可リストで承認する)
_CONFIG_NAME_TOKEN = re.compile(
    r"(allocation|expected_return|raer|capital_rotation|today_actions|rotation_observation)",
    re.IGNORECASE,
)


def test_the_only_config_files_for_a_128_feature_are_the_off_shadow_and_the_policy_values() -> None:
    by_name = sorted(
        p.name for p in _SHIPPED_CONFIG_DIR.iterdir() if _CONFIG_NAME_TOKEN.search(p.name)
    )
    assert by_name == sorted(_A_128_CONFIG_FILES)
    by_content = sorted(
        p.name
        for p in _SHIPPED_CONFIG_DIR.iterdir()
        if p.suffix in {".yaml", ".json"}
        and p.name not in _A_128_CONFIG_FILES
        and re.search(r"(expected_return|raer\b|allocation)", p.read_text(encoding="utf-8"))
    )
    assert by_content == []


def test_the_rotation_observation_config_is_the_off_switch_and_nothing_else() -> None:
    """★ P の mode の設定: 出荷値 OFF・OFF / SHADOW だけ・ACTIVE なし・キーは 2 つだけ ★。"""
    from jstock_advisor.services.rotation_observation_config import (  # noqa: PLC0415
        load_rotation_observation_config,
    )
    from jstock_advisor.services.rotation_observation_runner import (  # noqa: PLC0415
        RotationObserverMode,
    )

    text = (_SHIPPED_CONFIG_DIR / "rotation_observation.yaml").read_text(encoding="utf-8")
    assert re.search(r'^mode:\s*"OFF"\s*$', text, flags=re.MULTILINE)
    data = yaml.safe_load(text)
    assert set(data) == {"version", "mode"}
    assert load_rotation_observation_config(_SHIPPED_CONFIG_DIR).mode is RotationObserverMode.OFF
    assert {m.value for m in RotationObserverMode} == {"OFF", "SHADOW"}  # ACTIVE は存在しない
    # 購入側 Shadow の設定とは別のファイル・別の入口(共有しない)
    assert "allocation" not in text.lower()
    assert _A_128_CONFIG_FILES["rotation_observation.yaml"] == "P_MODE_SWITCH_SHIPPED_OFF"


def _all_yaml_keys(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            keys.append(str(key))
            keys.extend(_all_yaml_keys(inner))
    elif isinstance(value, list):
        for inner in value:
            keys.extend(_all_yaml_keys(inner))
    return keys


def test_the_raer_coefficients_file_is_not_an_entry_point_for_enabling_anything() -> None:
    """raer_coefficients.yaml は承認済みの係数だけ。有効化の入口(mode / enabled の類)を持たない。

    許可リストへ足したことは『接続を許す』ことではない(運用手順書 37.5)。loader はどこからも
    import されない(dormant)。Shadow の入口は allocation_shadow.yaml だけ(出荷は SHADOW。R1 = #128。
    USER の別承認 G0。本ファイルが入口にならないことは、上の語の検査が固定する)。
    """
    assert _A_128_CONFIG_FILES["raer_coefficients.yaml"] == "COEFFICIENT_VALUES_ONLY"
    data = yaml.safe_load((_SHIPPED_CONFIG_DIR / "raer_coefficients.yaml").read_text("utf-8"))
    keys = {key.lower() for key in _all_yaml_keys(data)}
    # キーを '_' で分けた語で見る(model_version のような別の語を入口と誤判定しない)
    entrance_words = {"on", "off", "mode", "enable", "enabled", "active", "shadow", "dry", "switch"}
    assert not [key for key in keys if set(key.split("_")) & entrance_words]
    shadow = (_SHIPPED_CONFIG_DIR / "allocation_shadow.yaml").read_text(encoding="utf-8")
    assert re.search(r'^mode:\s*"?SHADOW"?\s*(#.*)?$', shadow, re.MULTILINE)


def test_the_policy_values_file_is_not_an_entry_point_for_enabling_anything() -> None:
    """allocation_policy.yaml は閾値だけ。有効化の入口(mode / enabled の類)を持たない。

    許可リストへ足したことは『接続を許す』ことではない。有効にできる入口は allocation_shadow.yaml
    (出荷 SHADOW。上のテスト)だけで、本ファイルの loader はどこからも import されない
    (`test_no_handler_or_cli_reaches_an_unconnected_module`)。
    """
    assert _A_128_CONFIG_FILES["allocation_policy.yaml"] == "POLICY_VALUES_ONLY"
    data = yaml.safe_load((_SHIPPED_CONFIG_DIR / "allocation_policy.yaml").read_text("utf-8"))
    keys = {key.lower() for key in _all_yaml_keys(data)}
    # 短い語は完全一致、長い語は部分一致(auto_enable・shadow_mode・入れ子のキーも拾う)
    assert not keys & {"on", "off"}
    assert not [
        key
        for key in keys
        if any(word in key for word in ("mode", "enable", "active", "shadow", "dry_run", "switch"))
    ]
    # 値が 'OFF' / 'SHADOW' / 'ACTIVE' の項目も無い(mode を別名で持たない)
    assert not {str(v).upper() for v in data.values() if isinstance(v, str)} & {
        "OFF",
        "SHADOW",
        "ACTIVE",
        "ON",
    }


# --- (2) 運用手順書の新節(§37) ---

_SECTION_HEADING = re.compile(r"^## 37\. ")


def _section_37() -> str:
    lines = _manual().splitlines()
    start = next(i for i, line in enumerate(lines) if _SECTION_HEADING.match(line))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _subsection(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("### ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_the_section_exists_and_names_the_gate() -> None:
    heading = next(line for line in _manual().splitlines() if _SECTION_HEADING.match(line))
    for word in ("#128", "Production 接続 gate", "#606"):
        assert word in heading, word


def test_the_section_has_the_required_subsections_in_order() -> None:
    headings = [line for line in _section_37().splitlines() if line.startswith("### 37.")]
    assert [h.split(" ")[1] for h in headings] == [
        "37.1",
        "37.2",
        "37.3",
        "37.4",
        "37.5",
        "37.6",
    ]


def _stage_text(stage: str) -> str:
    block = _subsection(_section_37(), "### 37.2")
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(stage))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("段 ")), len(lines))
    return "\n".join(lines[start:end])


@pytest.mark.parametrize("stage", ["段 A", "段 B", "段 C", "段 D"])
def test_every_stage_states_entry_exit_record_and_stop_condition(stage: str) -> None:
    text = _stage_text(stage)
    for label in ("入口", "出口", "記録", "停止条件"):
        assert label in text, (stage, label)


def test_the_four_stages_are_in_the_gate_order() -> None:
    block = _subsection(_section_37(), "### 37.2")
    positions = [block.index(f"段 {x}") for x in "ABCD"]
    assert positions == sorted(positions)
    titles = re.findall(r"^段 [ABCD] ([^\n(:：]+)", block, flags=re.MULTILINE)
    assert [t.strip() for t in titles][:4] == [
        "Shadow",
        "Backtest / Replay",
        "Human Review",
        "USER Approval",
    ]


def test_user_approval_names_its_target_and_is_separate_from_the_changeset_gates() -> None:
    stage_d = _stage_text("段 D")
    for phrase in ("明示的な承認", "承認の対象", "別々", "ChangeSet", "CREATE", "EXECUTE"):
        assert phrase in stage_d, phrase
    # 新しい制約として読まれないよう、既存の正本(#122 / §36)に基づくことを示す
    assert "新しい制約ではなく" in stage_d
    assert "#122" in stage_d and "§36" in stage_d


def test_the_section_reuses_existing_gates_and_does_not_create_a_new_one() -> None:
    text = _section_37()
    assert "新しい gate 機構" in text and "新しい有効化経路" in text
    for reference in ("§28", "§10", "§5.1", "#122", "#846", "#128"):
        assert reference in text, reference
    # 恒久規則の制定・変更は HUMAN_ONLY。導入部と『決めていないこと』の両方で明示する
    intro = text[: text.index("### 37.1")]
    assert "POLICY_AUTHORITY = HUMAN_ONLY" in intro
    assert "HUMAN_ONLY" in _subsection(text, "### 37.6")


def test_the_section_does_not_fix_thresholds_or_periods() -> None:
    """合格基準・最低期待リターン改善幅・観測期間は『shadow 運用の後に決める』。数値を書かない。"""
    text = _subsection(_section_37(), "### 37.2") + _subsection(_section_37(), "### 37.3")
    # 期間・割合・時間・件数・回数のいずれの単位の数値も書かない(検査をテスト名どおりに広げた)
    assert not re.search(
        r"\d+\s*(営業日|日間|週間|か月|ヶ月|%|％|ポイント|秒|分|時間|件|回|年)", text
    )
    undecided = _subsection(_section_37(), "### 37.6")
    for phrase in ("合格基準", "観測期間", "持ち主", "ACTIVE"):
        assert phrase in undecided, phrase


def test_the_section_states_the_dont_list_and_the_rollback() -> None:
    dont = _subsection(_section_37(), "### 37.3")
    for phrase in ("段を飛ばさ", "投資判断に使", "IAM", "有効化の経路", "merge"):
        assert phrase in dont, phrase
    rollback = _subsection(_section_37(), "### 37.4")
    for phrase in ("OFF", "legacy", "再 deploy"):
        assert phrase in rollback, phrase


def test_the_section_points_at_this_contract_test_and_it_exists() -> None:
    mapping = _subsection(_section_37(), "### 37.5")
    name = "tests/unit/test_issue_606_production_connection_gate.py"
    assert name in mapping
    assert (_REPO / name).exists()


def test_this_test_is_registered_as_a_markdown_reading_test() -> None:
    registry = (_REPO / "tests" / "ci_markdown_subset.txt").read_text(encoding="utf-8")
    assert "tests/unit/test_issue_606_production_connection_gate.py" in registry.splitlines()
