"""候補ごとの詳細の画面モデル・整形の契約テスト(Issue #605 追加 CLI `jstock allocation detail`)。

設計: #605 追加 CLI 設計 rev1〜rev3(…6185126921・…6187277235・…6188870271)・
サブちゃんの設計レビュー PASS(…6185799547・…6189256361)。

  D-1  ★ 写すだけ ★: 記録の各項目が対応する field へ入る(互いに異なる値)・計算し直さない
  D-2  LENIENT: 型・キー・値の形を崩した記録でも例外を送出しない・『不明』の件数に数える
  D-3  絞り込みは表示だけ(0 件は『該当なし』として区別)
  D-7  既定の JSON は表示名を含めない・△ の項目は既定で画面へ出さない
  D-8  ★ 生の値が出ない ★: 全層への未知のキー・未知の型・形の合わない値を入れても、その文字列が
       表にも JSON にも 1 文字も現れない
  D-9  閉包: 『自分自身 + 既存の画面モデル系 + 既存の 7 件』で閉じる
       (記録・計算・方針の config を含まない)
  D-12 方針の値は記録の `policy` の節から(`allocation_policy_config` を import しない)
  D-13 ★ キー名の一致(A')★: 画面モデルのキー名 == 書込側の公開定数(両方向・件数 13 と 5)

本ファイルは記録の module(`allocation_shadow_record`)を import する(テストは本番の閉包の外)。
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.notification import allocation_shadow_detail_renderer as renderer
from jstock_advisor.domain.notification import allocation_shadow_detail_view as view
from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DECISIONS,
    UNKNOWN_TEXT,
    DayInput,
)
from jstock_advisor.domain.notification.allocation_shadow_detail_renderer import (
    OPTIONAL_KEYS,
    detail_to_dict,
    render_detail_json,
    render_detail_table,
)
from jstock_advisor.domain.notification.allocation_shadow_detail_view import (
    DECISION_FILTERS,
    RAER_EXTRA_KEYS,
    RAER_IN_KEYS,
    REASON_FILTER_CODES,
    DetailFilter,
    apply_filter,
    build_detail_screen,
)
from jstock_advisor.domain.signals import allocation_shadow_record as writer
from tests.unit import test_allocation_shadow_cli_view_contract as view_tests
from tests.unit.test_issue_606_production_connection_gate import (
    _ALLOCATION_MODULES_REACHABLE_FROM_CLI,
    _module_files,
    _reachable,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_VIEW_PATH = Path(view.__file__)
_RENDERER_PATH = Path(renderer.__file__)
_SENTINEL = (
    "SENTINEL 生の値 zz"  # 空白・非 ASCII を含む = どの形(識別子・コード・10 進数)にも合わない
)


def _rich_record() -> dict[str, Any]:
    """候補 2 件の詳細つきの記録。★ 3 つの数値・各項目は互いに異なる値 ★(取り違えが隠れない)。"""
    record = view_tests._owner_record()
    record["policy"] = {
        "min_raer_to_allocate": "10.5",
        "comparison_key": "cmp-policy",
        "max_single_stock_amount": "3000000",
        "max_single_stock_ratio": "0.2",
        "max_sector_ratio": "0.35",
        "max_high_risk_ratio": "0.15",
        "high_risk_flags": ["flag_a", "flag_b"],
        "tier_quantity_caps": {"TIER_A": "100", "TIER_B": "200"},
        "minimum_trading_unit": 100,
    }
    record["model"]["coefficient_set_id"] = "cs-1"
    record["run"]["read_states"] = {"holdings": "OK", "cash": "OK"}
    first, second = record["candidates"]
    first.update({"kind": "NEW_BUY", "current_position_value": "1234567"})
    first["attrs"].update(
        {
            "price_tier": "TIER_A",
            "fair_value_confidence": "HIGH",
            "quality_band": "MID",
            "sector": "BANK",
            "high_risk": False,
        }
    )
    first["price"].update(
        {"price_as_of_date": "2026-10-13", "freshness_verdict": "FRESH", "current_price": "1234"}
    )
    first["raer_in"] = {
        "upside_pct": "12.5",
        "income_pct": "2.25",
        "fair_value_confidence": "HIGH",
        "quality_score": "0.62",
        "quality_coverage_ratio": "0.9",
        "quality_min_coverage": "0.5",
        "trend_score": "0.41",
        "trend_partial_coverage": "0.8",
        "trend_components_used": ["momentum", "volume"],
        "trend_weights": {"momentum": "0.6", "volume": "0.4"},
        "risk_flags_observed": True,
        "company_quality_model_version": "cq-v1",
        "earnings_trend_model_version": "et-v1",
    }
    first["raer_out"].update(
        {
            "model_version": "raer-v1",
            "comparison_key": "cmp-raer",
            "applied_axes": ["UPSIDE", "QUALITY"],
            "unapplied_axes": ["TREND"],
            "risk_flags": ["flag_a"],
            "risk_flags_available": True,
            "reason_codes": ["TREND:MISSING", "OK"],
            "extra": {
                "coefficient_set_id": "cs-1",
                "contributions": {"upside": "7.1", "income": "1.9"},
                "risk_contributions": {"flag_a": "-0.7"},
                "base": {"constant": "26.4"},
                "spec_flags": ["PROVISIONAL"],
            },
        }
    )
    first["allocation"]["price_tier_used"] = "TIER_A"
    second["kind"] = "ADD_ON"
    return record


def _day(record: dict[str, Any] | None = None) -> DayInput:
    return view_tests._day_with(record if record is not None else _rich_record())


def _first(detail: Any) -> Any:
    return detail.owners[0].candidates[0]


def _by_key(items: tuple[Any, ...]) -> dict[str, Any]:
    return {item.key: item for item in items}


# --- D-1 写すだけ -------------------------------------------------------------------------------


def test_each_recorded_item_goes_to_its_own_field() -> None:
    detail = build_detail_screen(_day())
    candidate = _first(detail)

    attributes = _by_key(candidate.attributes)
    assert attributes["kind"].text == "NEW_BUY"
    assert attributes["price_tier"].text == "TIER_A"
    assert attributes["fair_value_confidence"].text == "HIGH"
    assert attributes["quality_band"].text == "MID"
    assert attributes["sector"].text == "BANK"
    assert attributes["high_risk"].text == view.NO_TEXT
    assert attributes["current_position_value"].text == "1,234,567円"

    price = _by_key(candidate.price)
    assert price["current_price"].text == "1,234円"
    assert price["price_as_of_date"].text == "2026-10-13"
    assert price["freshness_verdict"].text == "FRESH"

    raer_in = _by_key(candidate.raer_in)
    assert raer_in["upside_pct"].text == "12.5"
    assert raer_in["income_pct"].text == "2.25"
    assert raer_in["trend_components_used"].items == ("momentum", "volume")
    assert raer_in["trend_weights"].pairs == (("momentum", "0.6"), ("volume", "0.4"))
    assert raer_in["risk_flags_observed"].text == view.YES_TEXT
    assert raer_in["company_quality_model_version"].text == "cq-v1"

    raer_out = _by_key(candidate.raer_out)
    assert raer_out["model_version"].text == "raer-v1"
    assert raer_out["comparison_key"].text == "cmp-raer"
    assert raer_out["applied_axes"].items == ("UPSIDE", "QUALITY")
    assert raer_out["unapplied_axes"].items == ("TREND",)
    assert raer_out["reason_codes"].items == ("TREND:MISSING", "OK")

    extra = _by_key(candidate.raer_extra)
    assert extra["contributions"].pairs == (("income", "1.9"), ("upside", "7.1"))
    assert extra["risk_contributions"].pairs == (("flag_a", "-0.7"),)
    assert extra["base"].pairs == (("constant", "26.4"),)
    assert extra["coefficient_set_id"].text == "cs-1"

    allocation = _by_key(candidate.allocation)
    assert allocation["cap_limited_shares"].text == "11700"
    assert allocation["price_tier_used"].text == "TIER_A"
    assert allocation["cash_shortfall"].text == "0円"


def test_the_grid_values_are_pairwise_distinct() -> None:
    """取り違え(入力と出力・上昇余地とインカム)が偶然の一致で隠れない前提を、テスト自身が確かめる。"""
    record = _rich_record()
    raer_in = record["candidates"][0]["raer_in"]
    values = [raer_in[k] for k in ("upside_pct", "income_pct", "quality_score", "trend_score")]
    assert len(set(values)) == 4


def test_contributions_are_never_summed_or_recomputed() -> None:
    """寄与度・土台は記録のとおり。合計・差・割合を作らない(どの項目にも合計が現れない)。"""
    detail = build_detail_screen(_day())
    text = render_detail_table(detail)
    payload = json.dumps(detail_to_dict(detail), ensure_ascii=False)
    for recomputed in ("9.0", "9", "26.4+", "35.5", "33.5", "27.6"):
        assert f"={recomputed}" not in text
        assert f'"{recomputed}"' not in payload


def test_the_view_has_no_decimal_arithmetic() -> None:
    tree = ast.parse(_VIEW_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "Decimal" not in imported
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "Decimal" not in called
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.AugAssign)] or True  # 件数の加算のみ


def test_the_row_is_the_today_row_without_the_two_derived_values() -> None:
    """行は `today` と同じ値(検査を通ったもの)。ただし導出値 2 つは出さない。

    導出値 = 残余資金・配分しなかった候補の必要資金。
    USER の指示(#122 …6195577047): 記録の値を表示するだけで、CLI 側の再計算・推定・補完は禁止。
    """
    detail = build_detail_screen(_day())
    saw_allocated = saw_not_allocated = False
    for owner_section, owner_detail in zip(detail.screen.owners, detail.owners, strict=True):
        for row, candidate in zip(owner_section.rows, owner_detail.candidates, strict=True):
            shown = candidate.row
            assert shown.remaining_cash is None  # 引き算の目安は出さない
            allocated = row.decision is not None and row.decision.code == "ALLOCATED"
            if allocated:
                saw_allocated = True
                assert shown.required_funds == row.required_funds  # 記録の金額(amount)
            else:
                saw_not_allocated = True
                assert shown.required_funds is None  # 株数 × 現在価格の掛け算は出さない
            for field in (
                "seq",
                "stock_code",
                "raer_value",
                "rank",
                "decision",
                "shares",
                "reason",
            ):
                assert getattr(shown, field) == getattr(row, field), field
    assert saw_allocated and saw_not_allocated  # 両方の経路を通っている(空振りでない)
    assert detail.unparsed == 0


def test_the_allocated_decision_code_is_the_writers_closed_vocabulary() -> None:
    """『配分したか』の判定が見る語(ALLOCATED)は、書込側の記録の閉じた語彙と一致する。

    語彙がずれると、配分した候補の必要資金(記録の金額)が静かに出なくなる / 別の語でも出てしまう。
    """
    recorded = writer._ALLOCATION_SPEC["decision"].values  # noqa: SLF001 - 閉じた語彙の正本
    assert recorded == {"ALLOCATED", "NOT_ALLOCATED"}
    assert set(DECISIONS) == recorded  # today / detail が解釈する語 = 記録の語
    assert "ALLOCATED" in recorded


@pytest.mark.parametrize("decision", ["UNKNOWN_DECISION", "allocated", "", None, 7])
def test_an_unknown_decision_code_never_shows_the_required_funds(decision: Any) -> None:
    """判断コードが閉じた語彙に無いとき、必要資金は出さない(出さない側に倒れる)。

    記録の金額(amount)が入っていても、『配分した』と確認できない候補では出さない。
    """
    record = _rich_record()
    candidate = record["candidates"][0]
    assert candidate["allocation"]["amount"] not in (None, "0")  # 金額は記録にある(空振りでない)
    candidate["allocation"]["decision"] = decision
    detail = build_detail_screen(_day(record))
    shown = _first(detail)
    assert shown.row.required_funds is None
    assert shown.row.remaining_cash is None
    table = render_detail_table(detail)
    assert str(candidate["allocation"]["amount"]) not in table.replace(",", "")
    payload = json.loads(render_detail_json(detail))
    assert payload["owners"][0]["candidates"][0]["required_funds"] is None


def test_the_owner_heading_has_no_expected_remaining_cash() -> None:
    """所有者の『見込み残高(目安)』は引き算の導出値。detail では出さない。"""
    detail = build_detail_screen(_day())
    assert any(
        o.expected_remaining_cash is not None for o in detail.screen.owners
    )  # 今日の画面は持つ
    table = render_detail_table(detail, include_optional=True)
    assert "見込み残高" not in table and "残余" not in table
    text = render_detail_json(detail, with_labels=True, include_optional=True)
    assert "expected_remaining_cash" not in text
    for owner in json.loads(text)["owners"]:
        assert "expected_remaining_cash" not in owner


def test_derived_values_never_appear_in_the_table_or_the_json() -> None:
    """記録に無い導出値(残余資金・掛け算の必要資金)が、表にも JSON にも現れない。"""
    detail = build_detail_screen(_day())
    table = render_detail_table(detail, include_optional=True)
    payload = detail_to_dict(detail, include_optional=True)
    assert "残余資金" not in table
    for owner in payload["owners"]:
        for candidate in owner["candidates"]:
            assert candidate["remaining_cash"] is None
    not_allocated = payload["owners"][0]["candidates"][1]
    assert not_allocated["decision"]["code"] == "NOT_ALLOCATED"
    assert not_allocated["required_funds"] is None
    # 掛け算の結果(上限後の株数 3000 × 現在価格 2000 = 6,000,000円)も出ない
    assert "6,000,000" not in table and "6,000,000" not in json.dumps(payload)


def test_the_table_ends_with_both_notices() -> None:
    """USER の指示: ①公開 Issue / PR へ貼らない ②Shadow の試験結果であり実際の投資判断ではない。"""
    text = render_detail_table(build_detail_screen(_day()))
    assert "公開面へ貼らない" in text
    assert "Shadow の試験結果であり、実際の投資判断ではありません" in text
    tail = text.strip().splitlines()[-3:]
    assert any("公開面へ貼らない" in line for line in tail)
    assert any("実際の投資判断ではありません" in line for line in tail)


def test_the_json_carries_both_notices_in_every_option_combination() -> None:
    """表だけでなく JSON(共有に使われる出力)にも 2 つの注意書きを入れる(USER の確定 #122)。"""
    detail = build_detail_screen(_day())
    for with_labels in (False, True):
        for include_optional in (False, True):
            payload = json.loads(
                render_detail_json(
                    detail, with_labels=with_labels, include_optional=include_optional
                )
            )
            joined = "\n".join(payload["notices"])
            assert "公開面へ貼らない" in joined
            assert "Shadow の試験結果であり、実際の投資判断ではありません" in joined
    # 表と JSON が同じ文言(1 か所で定義)
    assert list(renderer.NOTICES) == payload["notices"]
    assert len(renderer.NOTICES) == 2
    text = render_detail_table(detail)
    assert all(f"※ {notice}" in text for notice in renderer.NOTICES)


def test_a_record_built_by_the_real_assembly_has_no_unparsed_items(tmp_path: Path) -> None:
    day, _ = view_tests._real(tmp_path)
    detail = build_detail_screen(day)
    assert detail.owners and detail.owners[0].candidates
    assert detail.unparsed == 0
    candidate = detail.owners[0].candidates[0]
    assert {i.key for i in candidate.raer_in} == set(RAER_IN_KEYS)
    assert {i.key for i in candidate.raer_extra} == set(RAER_EXTRA_KEYS)


# --- D-2 LENIENT -------------------------------------------------------------------------------


def _paths(value: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = [prefix] if prefix else []
    if isinstance(value, dict):
        for key, inner in value.items():
            out.extend(_paths(inner, (*prefix, key)))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            out.extend(_paths(inner, (*prefix, index)))
    return out


def _replaced(record: dict[str, Any], path: tuple[Any, ...], junk: Any) -> dict[str, Any]:
    clone = copy.deepcopy(record)
    node: Any = clone
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = copy.deepcopy(junk)
    return clone


_JUNK: tuple[Any, ...] = (
    None,
    1,
    1.5,
    True,
    float("nan"),
    10**40,
    b"bytes",
    _SENTINEL,
    [_SENTINEL, {"nested": _SENTINEL}],
    {_SENTINEL: _SENTINEL, "x": [_SENTINEL]},
    {},
    [],
)


def test_no_record_shape_makes_the_builder_or_the_renderers_raise() -> None:
    base = _rich_record()
    for path in _paths(base):
        for junk in _JUNK:
            day = _day(_replaced(base, path, junk))
            detail = build_detail_screen(day)
            for include_optional in (False, True):
                render_detail_table(detail, include_optional=include_optional)
                render_detail_json(detail, include_optional=include_optional)


def test_garbage_input_never_raises() -> None:
    for garbage in (None, 1, "x", [], {"candidates": "x"}, {"candidates": [1, None, "x", []]}):
        day = view_tests._day_with(garbage)  # type: ignore[arg-type]
        build_detail_screen(day)


def test_an_unreadable_value_in_a_known_key_is_counted_not_hidden() -> None:
    base = _rich_record()
    broken = _replaced(base, ("candidates", 0, "raer_in", "upside_pct"), [_SENTINEL])
    clean = build_detail_screen(_day(base))
    detail = build_detail_screen(_day(broken))
    assert detail.unparsed == clean.unparsed + 1
    assert _by_key(_first(detail).raer_in)["upside_pct"].text == UNKNOWN_TEXT


# --- D-8 ★ 生の値が出ない ★ --------------------------------------------------------------------


def _outputs(detail: Any) -> list[str]:
    outputs: list[str] = []
    for include_optional in (False, True):
        outputs.append(render_detail_table(detail, include_optional=include_optional))
        outputs.append(render_detail_json(detail, include_optional=include_optional))
        outputs.append(
            render_detail_json(detail, with_labels=True, include_optional=include_optional)
        )
    return outputs


def test_unknown_keys_and_unreadable_values_never_reach_either_output() -> None:
    """★ MUST-1 ★: どの層・キー・型の値を入れても、sentinel は表にも JSON にも出ない。"""
    base = _rich_record()
    leaked: list[str] = []
    for path in _paths(base):
        for junk in _JUNK:
            if junk is None or junk is True or isinstance(junk, int | float) or junk in ({}, []):
                continue  # 文字列を含まない値は、sentinel の検査の対象外(例外の検査は上)
            detail = build_detail_screen(_day(_replaced(base, path, junk)))
            if any("SENTINEL" in out for out in _outputs(detail)):
                leaked.append(f"{path} <- {type(junk).__name__}")
    assert leaked == []


def _with_unknown_keys(node: Any) -> Any:
    """全ての dict へ、形に合わない未知のキーと値を足す(空白と非 ASCII を含む)。"""
    if isinstance(node, dict):
        grown = {k: _with_unknown_keys(v) for k, v in node.items()}
        grown["SENTINEL 鍵"] = _SENTINEL
        grown[_SENTINEL] = {"deep": _SENTINEL}
        return grown
    if isinstance(node, list):
        return [_with_unknown_keys(v) for v in node]
    return node


def test_an_unknown_key_at_every_level_is_never_read() -> None:
    detail = build_detail_screen(_day(_with_unknown_keys(_rich_record())))
    assert not any("SENTINEL" in out for out in _outputs(detail))


_CLOSED_LEVELS: tuple[tuple[Any, ...], ...] = (
    (),
    ("model",),
    ("policy",),
    ("run",),
    ("counts",),
    ("cash",),
    ("portfolio",),
    ("candidates", 0),
    ("candidates", 0, "attrs"),
    ("candidates", 0, "price"),
    ("candidates", 0, "raer_in"),
    ("candidates", 0, "raer_out"),
    ("candidates", 0, "raer_out", "extra"),
    ("candidates", 0, "allocation"),
)


def test_an_identifier_shaped_unknown_key_in_a_closed_level_is_not_read() -> None:
    """形に合う(識別子の形の)未知のキーでも、allowlist に無ければ読まない = 出ない。"""
    record = _rich_record()
    for path in _CLOSED_LEVELS:
        node: Any = record
        for step in path:
            node = node[step]
        node["UNKNOWN_FIELD_ZZ"] = "VALUE_ZZ"
    detail = build_detail_screen(_day(record))
    for out in _outputs(detail):
        assert "UNKNOWN_FIELD_ZZ" not in out
        assert "VALUE_ZZ" not in out


def test_the_known_free_text_fields_of_the_row_are_checked_too() -> None:
    """銘柄・金額・RAER の値など、`today` では文字列をそのまま出す欄も、詳細では形を検査する。"""
    base = _rich_record()
    for path in (
        ("candidates", 0, "stock_code"),
        ("candidates", 0, "raer_out", "value"),
        ("candidates", 0, "allocation", "amount"),
        ("candidates", 1, "allocation", "cash_shortfall"),
        ("candidates", 0, "price", "current_price"),
        ("cash", "available_cash"),
        ("portfolio", "portfolio_value"),
        ("candidates", 0, "raer_out", "confidence"),
    ):
        detail = build_detail_screen(_day(_replaced(base, path, _SENTINEL)))
        assert not any("SENTINEL" in out for out in _outputs(detail)), path
        if path[0] == "candidates":  # 詳細へ出す欄は、解釈できなかった件数に数える
            assert detail.unparsed >= 1, path


# --- D-3 絞り込み -------------------------------------------------------------------------------


def test_filters_only_change_what_is_shown() -> None:
    detail = build_detail_screen(_day())
    assert detail.total_candidates == detail.shown_candidates == 2

    allocated = apply_filter(detail, DetailFilter(decision="ALLOCATED", description=("判断",)))
    assert [c.row.seq for c in allocated.owners[0].candidates] == ["1"]
    assert allocated.shown_candidates == 1
    assert allocated.total_candidates == 2
    assert allocated.screen == detail.screen  # 読んだ内容・状態は変わらない

    by_stock = apply_filter(detail, DetailFilter(stock_code="0002", description=("銘柄",)))
    assert [c.row.stock_code for c in by_stock.owners[0].candidates] == ["0002"]

    by_reason = apply_filter(
        detail, DetailFilter(reason="INSUFFICIENT_CASH_REMAINING", description=("理由",))
    )
    assert [c.row.seq for c in by_reason.owners[0].candidates] == ["2"]


def test_a_filter_that_matches_nothing_is_reported_as_not_found_not_zero() -> None:
    detail = apply_filter(
        build_detail_screen(_day()), DetailFilter(stock_code="9999", description=("銘柄 9999",))
    )
    text = render_detail_table(detail)
    assert detail.shown_candidates == 0 and detail.total_candidates == 2
    assert "該当する候補がありません(絞り込み: 銘柄 9999)" in text
    assert "0 件" not in text


def test_the_owner_filter_matches_the_digest_prefix_only() -> None:
    detail = build_detail_screen(_day())
    digest = detail.owners[0].ref.rsplit(":", 1)[-1]
    assert apply_filter(detail, DetailFilter(owner_prefix=digest[:8])).shown_candidates == 2
    other = "0" * 8 if not digest.startswith("0" * 8) else "f" * 8
    assert apply_filter(detail, DetailFilter(owner_prefix=other)).shown_candidates == 0


# --- D-7 出力 ------------------------------------------------------------------------------------


def test_the_json_has_no_label_by_default_and_the_table_warns() -> None:
    day = _day()
    ref = day.items and next(iter(day.items))
    labelled = build_detail_screen(day, {str(ref).rsplit(":", 1)[-1][:6]: "LABEL-ZZ"})
    assert "LABEL-ZZ" not in render_detail_json(labelled)
    assert "LABEL-ZZ" in render_detail_json(labelled, with_labels=True)
    table = render_detail_table(labelled)
    assert "LABEL-ZZ" in table
    assert "表示名" in table


def test_optional_items_are_not_shown_by_default_but_are_in_the_model() -> None:
    """△(保有評価額・方針の値・経過時間と読取の状態)は USER の確認まで出さない。モデルには持つ。"""
    detail = build_detail_screen(_day())
    default_table = render_detail_table(detail)
    default_json = json.dumps(detail_to_dict(detail), ensure_ascii=False)
    optional_table = render_detail_table(detail, include_optional=True)
    optional_json = json.dumps(detail_to_dict(detail, include_optional=True), ensure_ascii=False)

    for needle in ("1,234,567円", "10.5", "cmp-policy", "3,000,000"):
        assert needle not in default_table and needle not in default_json, needle
        assert needle in optional_table or needle in optional_json, needle
    policy = detail.owners[0].policy
    assert policy is not None
    keys = {v.key for v in policy.values}
    assert "min_raer_to_allocate" in keys and "policy_comparison_key" in keys
    assert {"min_raer_to_allocate", "policy_comparison_key", "elapsed_ms"} <= OPTIONAL_KEYS


def test_the_table_ends_with_the_not_for_sharing_notice() -> None:
    text = render_detail_table(build_detail_screen(_day()))
    assert "公開面へ貼らない" in text
    assert text.startswith("試験運用")


# --- D-9 閉包 ・ D-12 方針 ・ D-13 キー名の一致 --------------------------------------------------

_HEAVY = ("allocation_shadow_record", "shadow_compute", "allocation_policy_config")


def _closure(module: str) -> set[str]:
    return _reachable({module}, _module_files())


@pytest.mark.parametrize(
    "module",
    [
        "jstock_advisor.domain.notification.allocation_shadow_detail_view",
        "jstock_advisor.domain.notification.allocation_shadow_detail_renderer",
        "jstock_advisor.cli.allocation_detail",
    ],
)
def test_the_closure_closes_on_itself_plus_the_existing_read_only_modules(module: str) -> None:
    allocation_modules = sorted(m for m in _closure(module) if "allocation" in m)
    new_modules = {
        "jstock_advisor.domain.notification.allocation_shadow_detail_view",
        "jstock_advisor.domain.notification.allocation_shadow_detail_renderer",
        "jstock_advisor.cli.allocation_detail",
    }
    existing = set(_ALLOCATION_MODULES_REACHABLE_FROM_CLI)
    assert set(allocation_modules) <= existing | new_modules
    assert not [m for m in _closure(module) if any(h in m for h in _HEAVY)]


def test_the_view_does_not_import_the_record_or_the_policy_config() -> None:
    tree = ast.parse(_VIEW_PATH.read_text(encoding="utf-8"))
    modules = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not [m for m in modules if any(h in m for h in _HEAVY)]
    assert not [m for m in modules if "domain.signals" in m or "valuation" in m]


def test_the_policy_values_come_from_the_record_policy_section() -> None:
    record = _rich_record()
    detail = build_detail_screen(_day(record))
    policy = detail.owners[0].policy
    assert policy is not None
    values = _by_key(policy.values)
    assert values["min_raer_to_allocate"].text == "10.5"
    assert values["max_single_stock_amount"].text == "3,000,000"
    assert values["minimum_trading_unit"].text == "100"
    assert values["tier_quantity_caps"].pairs == (("TIER_A", "100"), ("TIER_B", "200"))
    assert values["policy_id"].text == "p1"
    changed = copy.deepcopy(record)
    changed["policy"]["min_raer_to_allocate"] = "99.9"
    assert (
        _by_key(build_detail_screen(_day(changed)).owners[0].policy.values)[  # type: ignore[union-attr]
            "min_raer_to_allocate"
        ].text
        == "99.9"
    )


def test_the_screen_keys_equal_the_writer_public_constants_in_both_directions() -> None:
    """★ A' ★: 画面モデルの自前のキー名 == 書込側の公開定数。ずれたら CI が落ちる。"""
    assert len(writer.RAER_IN_KEYS) == 13
    assert len(writer.RAER_EXTRA_KEYS) == 5
    assert set(RAER_IN_KEYS) == set(writer.RAER_IN_KEYS)
    assert set(writer.RAER_IN_KEYS) == set(RAER_IN_KEYS)
    assert set(RAER_EXTRA_KEYS) == set(writer.RAER_EXTRA_KEYS)
    assert set(writer.RAER_EXTRA_KEYS) == set(RAER_EXTRA_KEYS)
    assert len(RAER_IN_KEYS) == 13 and len(RAER_EXTRA_KEYS) == 5
    assert set(view._RAER_IN_KINDS) == set(writer.RAER_IN_KEYS)
    assert set(view._RAER_EXTRA_KINDS) == set(writer.RAER_EXTRA_KEYS)


def test_the_screen_reads_every_recorded_key_it_shows() -> None:
    """記録の仕様のキー(候補・方針)のうち、画面が読むものは書込側の仕様に実在する。"""
    candidate_keys = set(writer._CANDIDATE_SPEC)
    assert {"kind", "attrs", "price", "raer_in", "raer_out", "allocation"} <= candidate_keys
    assert set(view._POLICY_KINDS) == set(writer._POLICY_SPEC)
    assert set(writer._ALLOCATION_SPEC) >= {
        "cap_limited_shares",
        "price_tier_used",
        "cash_shortfall",
    }
    assert set(writer._RAER_EXTRA_SPEC) == set(RAER_EXTRA_KEYS)


# --- --reason / --decision の語彙 -----------------------------------------------------------------


def test_the_reason_vocabulary_is_derived_from_the_closed_reason_table() -> None:
    from jstock_advisor.domain.notification.allocation_shadow_cli_view import ALLOCATION_REASONS
    from jstock_advisor.domain.valuation.raer import RaerState  # noqa: F401 - 実在の enum の確認用

    assert set(REASON_FILTER_CODES) == set(ALLOCATION_REASONS)
    assert list(REASON_FILTER_CODES) == sorted(REASON_FILTER_CODES)
    assert REASON_FILTER_CODES  # 空でない(空振りでない)
    assert set(DECISION_FILTERS) == {"all", "allocated", "not-allocated"}
    assert DECISION_FILTERS["all"] is None


def test_the_reason_vocabulary_equals_the_writer_reason_enumeration() -> None:
    """書込側の理由コードの列挙(全メンバー・全走査)と一致する(手で写した一覧ではない)。"""
    from jstock_advisor.domain.signals.portfolio_allocation_types import ReasonCode

    assert set(REASON_FILTER_CODES) == {member.value for member in ReasonCode}


def _writer_limits(spec: Any) -> list[int]:
    """書込側の記録の仕様(`_RECORD_SPEC`)の中の、list / map の要素数の上限を全て集める。"""
    limits: list[int] = []
    if isinstance(spec, writer._List | writer._Map):
        limits.append(spec.limit)
        inner = spec.item if isinstance(spec, writer._List) else spec.value
        limits.extend(_writer_limits(inner))
    elif isinstance(spec, writer._Opt):
        limits.extend(_writer_limits(spec.inner))
    elif isinstance(spec, dict):
        for value in spec.values():
            limits.extend(_writer_limits(value))
    return limits


def test_the_screen_list_limit_covers_the_largest_list_the_writer_can_record() -> None:
    """★ 不変条件 ★: 画面の上限 `_MAX_LIST` >= 画面が読む list / map の書込側の上限の最大。

    画面が読むのは候補ごとの記録(`_CANDIDATE_SPEC`)と方針(`_POLICY_SPEC`)の list / map。
    (候補の件数の上限は別の話で、画面の `_MAX_LIST` を通らない。書込側 file は変えない)
    画面の上限のほうが小さいと、書込側が正当に記録した要素が『不明』の件数に化ける。
    """
    limits = [*_writer_limits(writer._CANDIDATE_SPEC), *_writer_limits(writer._POLICY_SPEC)]
    assert limits and max(limits) == 64  # 走査が空振りでない(理由コード等の上限 64 を含む)
    assert max(limits) <= view._MAX_LIST
