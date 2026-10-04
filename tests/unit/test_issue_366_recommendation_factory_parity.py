"""Issue #366(#275 層 2)PR-0: `build_recommendation` の parity guard。

背景:
    `tests/factories.py` の `build_recommendation` は、本番が設定する field に既定値を持つ
    (#647)。本番の構築箇所(src の `Recommendation(...)`)が field を足しても、factory が
    追随したかを機械で固定するテストが無かった。#580 が `scope_type` を足した時点で、
    factory は追随していない(guard が無いため CI は検出しなかった)。

固定するもの(MANAGER 判断 D-1(b) = #366 issuecomment-5979339174):
    1. ALWAYS(src の全構築箇所が渡す keyword 引数の積集合)を src の AST から導く(数を直書きしない)
    2. factory が既定を持つ field(`model_fields_set`)が、ALWAYS から「免除 field」を除いた
       全部を満たす(factory の更新漏れを CI が検出する)
    3. 免除 field(`EXEMPT_FACTORY_FIELDS`)は理由つきで明示し、★ 免除が黙って増えないよう、
       (a) ALWAYS に含まれ (b) entity の既定を持ち(必須でなく) (c) factory が既定を持たない
       ことを固定する(どれかが崩れた免除は古い免除として失敗する)
    4. src に `**kwargs` 展開・位置引数の構築が現れたら、ALWAYS を導けない(UNKNOWN)ため失敗する
       (guard の前提が崩れた事実を黙って通さない)

限界:
    ・field 名の有無のみを見る。値の「意味」の一致(例: scope_type と recommendation_type の整合)は
      検証しない(層 3 の領分)
    ・構築を関数へ包む箇所は積集合に入らないため、src の構築が `Recommendation(...)` の直接呼び出し
      であることに依存する(4 で失敗にして検出する)

fixture は架空値のみ(実在の氏名・所有者・数量・単価を使わない)。
"""

from __future__ import annotations

import ast
from pathlib import Path

from jstock_advisor.domain.entities.recommendation import Recommendation
from tests.factories import build_recommendation

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

# (所在 label, 行, keyword 引数名, **展開の有無, 位置引数の数)
_Site = tuple[str, int, set[str], bool, int]

# factory が既定を持たない field(entity の既定に任せる)と、その理由。
# 追加するときは、理由と「なぜテストの表示・判定の検証に直接関わらないか」を書くこと。
EXEMPT_FACTORY_FIELDS: dict[str, str] = {
    "raw_recommendation_type": (
        "entity の既定(None)で足りる。recommendation_type と連動して意味が決まるため、"
        "factory が既定を持つとテストの意図を隠しうる"
    ),
    "config_values_used": (
        "entity の既定(空 dict)で足りる。通知・判定の表示を検証するテストが依存する field ではない"
    ),
    "scope_type": (
        "entity の既定(None)で足りる。recommendation_type と連動する(#580)ため、factory が既定を"
        "持つと、override で recommendation_type を変えたときに食い違い、テストの意図を隠しうる"
    ),
}


def _is_recommendation_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "Recommendation"
    if isinstance(func, ast.Attribute):
        return func.attr == "Recommendation"
    return False


def _construction_sites(source: str, label: str) -> list[_Site]:
    """source 内の `Recommendation(...)` の呼び出しを、_Site の並びで返す。"""
    sites: list[_Site] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _is_recommendation_call(node):
            names = {kw.arg for kw in node.keywords if kw.arg is not None}
            has_star = any(kw.arg is None for kw in node.keywords)
            sites.append((label, node.lineno, names, has_star, len(node.args)))
    return sites


def _always_fields(sites: list[_Site]) -> set[str]:
    """全構築箇所が渡す keyword 引数の積集合(ALWAYS)。構築箇所が無ければ空。"""
    if not sites:
        return set()
    return set.intersection(*[site[2] for site in sites])


def _unknown_sites(sites: list[_Site]) -> list[str]:
    """ALWAYS を導けない構築(`**kwargs` 展開 / 位置引数)の所在。"""
    return [f"{label}:{lineno}" for label, lineno, _, has_star, n_pos in sites if has_star or n_pos]


def _missing_in_factory(always: set[str], exempt: set[str], factory_set: set[str]) -> set[str]:
    return always - exempt - factory_set


def _src_sites() -> list[_Site]:
    sites: list[_Site] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        label = path.relative_to(_SRC_ROOT).as_posix()
        sites.extend(_construction_sites(path.read_text(encoding="utf-8"), label))
    return sites


def _factory_set_fields() -> set[str]:
    """`build_recommendation()` が既定として渡す field(pydantic の `model_fields_set`)。"""
    return set(build_recommendation().model_fields_set)


# --- guard 本体 -------------------------------------------------------------


def test_src_construction_sites_exist_and_are_all_derivable() -> None:
    """src の構築箇所が 1 件以上あり、全件が keyword 引数のみの直接呼び出しである(UNKNOWN が 0)。"""
    sites = _src_sites()

    assert sites, "src に Recommendation(...) の構築箇所が見つからない(AST の検出が壊れている)"
    assert _unknown_sites(sites) == [], (
        "ALWAYS を導けない構築(**kwargs 展開 / 位置引数)が src に現れた。"
        "guard の前提が崩れている: " + ", ".join(_unknown_sites(sites))
    )


def test_factory_sets_every_always_field_except_declared_exemptions() -> None:
    """factory の既定が、ALWAYS から免除 field を除いた全部を満たす。

    落ちたら: src の構築が新しい field を渡すようになったのに factory が追随していない。
    factory へ既定を足すか、足さない理由を `EXEMPT_FACTORY_FIELDS` に書く。
    """
    always = _always_fields(_src_sites())

    missing = _missing_in_factory(always, set(EXEMPT_FACTORY_FIELDS), _factory_set_fields())

    assert missing == set(), f"factory が既定を持たない ALWAYS field: {sorted(missing)}"


def test_exemptions_are_current_and_justified() -> None:
    """免除は「ALWAYS に含まれ・entity の既定を持ち・factory が既定を持たない」field に限る。

    どれかが崩れた免除は古い(または不当な)免除として失敗する。免除が黙って増えない。
    """
    always = _always_fields(_src_sites())
    factory_set = _factory_set_fields()

    for name, reason in EXEMPT_FACTORY_FIELDS.items():
        assert reason.strip(), f"免除 {name} に理由がない"
        assert name in always, f"免除 {name} は ALWAYS に含まれない(古い免除。削除する)"
        assert name in Recommendation.model_fields, f"免除 {name} は entity の field ではない"
        assert not Recommendation.model_fields[name].is_required(), (
            f"免除 {name} は entity の必須 field(既定が無い)。免除できない"
        )
        assert name not in factory_set, (
            f"免除 {name} を factory が既定として持っている(古い免除。削除する)"
        )


def test_factory_default_fields_are_all_entity_fields() -> None:
    """factory が既定として渡す field は、すべて entity の field である。"""
    assert _factory_set_fields() <= set(Recommendation.model_fields)


# --- guard 部品の自己検証(変異・退行に強くするため、合成の source で判定ロジックを固定) -----------


def test_always_is_intersection_of_keyword_names() -> None:
    sites = _construction_sites(
        "a = Recommendation(x=1, y=2, z=3)\nb = m.Recommendation(x=1, y=2)\n", "synthetic"
    )

    assert _always_fields(sites) == {"x", "y"}
    assert _unknown_sites(sites) == []


def test_always_is_empty_when_no_construction() -> None:
    assert _always_fields(_construction_sites("a = Other(x=1)\n", "synthetic")) == set()


def test_star_expansion_and_positional_are_reported_unknown() -> None:
    sites = _construction_sites(
        "a = Recommendation(**kw)\nb = Recommendation(1, x=2)\nc = Recommendation(x=1)\n",
        "synthetic",
    )

    assert _unknown_sites(sites) == ["synthetic:1", "synthetic:2"]


def test_missing_in_factory_detects_gap_and_honours_exemption() -> None:
    always = {"a", "b", "c"}

    assert _missing_in_factory(always, set(), {"a", "b"}) == {"c"}
    assert _missing_in_factory(always, {"c"}, {"a", "b"}) == set()
    assert _missing_in_factory(always, set(), {"a", "b", "c", "extra"}) == set()
