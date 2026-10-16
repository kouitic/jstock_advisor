"""#992: `jstock allocation today` / `summary` の JSON に、`detail` と同一の `notices`(2 つ)を足す。

USER 確定(#122 …6214117097。以前の『today の仕様を変更しない』への限定的な例外承認)の条件を固定する:

  ① 既存項目の名称・型・値・意味を変えない
  ② 判定ロジックを変えない
  ③ `notices` 以外の出力差分を出さない
       ← 変更前の出力の全文(`tests/fixtures/allocation_shadow_json_before_992.json`)との
         完全一致で固定する(JSON は `notices` を除いて・表は全文)
  ④ 既存の利用箇所への互換性影響を確認する(PR 本文)
  ⑤ 回帰テストを追加する(本ファイル)

3 画面(today / summary / detail)の注意書きは同一で、定義は 1 か所
(`allocation_shadow_cli_renderer`)。JSON を出す関数を全走査し、`schema_version` を持つ JSON の
文書がすべて `notices` を持つことも固定する
(画面が増えたとき、`notices` の欠落を誰かが気づく前に落ちる。列挙ではなく走査)。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.cli import allocation_summary as summary_cli
from jstock_advisor.domain.notification import allocation_shadow_detail_renderer as detail_renderer
from jstock_advisor.domain.notification.allocation_shadow_cli_renderer import (
    NOTICE_NO_PUBLIC_SURFACE,
    NOTICE_NOT_INVESTMENT_ADVICE,
    NOTICES,
    render_table,
    screen_to_dict,
)
from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DayInput,
    build_screen,
)
from jstock_advisor.domain.notification.allocation_shadow_detail_view import build_detail_screen
from jstock_advisor.domain.notification.allocation_shadow_summary import (
    DayRecord,
    build_ten_day_report,
    report_to_dict,
    summarize,
)
from tests.unit import test_allocation_shadow_cli_view_contract as view_tests
from tests.unit import test_allocation_shadow_compute_contract as compute_tests
from tests.unit import test_allocation_shadow_detail_contract as detail_tests

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "jstock_advisor"
_GOLDEN = _REPO / "tests" / "fixtures" / "allocation_shadow_json_before_992.json"

_DAYS = ("2026-10-09", "2026-10-13")
_LABELS = {compute_tests._ref("owner-a").split(":")[1][:8]: "ラベルA"}

# 合成の文言(2 つ)。定義の単一性の検査に使う(全文一致)
_SENTENCES = (
    "この出力は利用者の端末だけで確認してください(Issue・PR などの公開面へ貼らない)",
    "これは Shadow の試験結果であり、実際の投資判断ではありません",
)


# =================================================================================================
# 入力の合成(実在の銘柄・保有・価格・数量を使わない)
# =================================================================================================


def _today_screen(labels: dict[str, str] | None = None) -> Any:
    return build_screen(view_tests._day_with(view_tests._owner_record()), labels)


def _summary_inputs() -> tuple[Any, Any]:
    ref = compute_tests._ref("owner-a")
    records = []
    for day in _DAYS:
        index = view_tests._index([ref])
        index["business_date"] = day
        owner = view_tests._owner_record()
        owner["business_date"] = day
        found = view_tests._found
        day_input = DayInput(day, found(index), view_tests.MISSING, {ref: found(owner)})
        records.append(DayRecord(day, build_screen(day_input, {}), None))
    summary = summarize(records)
    report = build_ten_day_report(summary, list(_DAYS), target_days=2)
    return report, summary


def current_outputs() -> dict[str, Any]:
    """現在の実装の出力(JSON は dict・表は文字列)。

    変更前の出力の固定(golden)も、これと同じ入力で作った。
    """
    screen = _today_screen(_LABELS)
    report, summary = _summary_inputs()
    return {
        "today_json": screen_to_dict(screen, with_labels=False),
        "today_json_with_labels": screen_to_dict(screen, with_labels=True),
        "today_table": render_table(screen, show_labels=True),
        "summary_json": report_to_dict(report, summary, include_by_owner=False),
        "summary_json_by_owner": report_to_dict(report, summary, include_by_owner=True),
        "summary_table": summary_cli.render_table(report, summary, by_owner=False),
        "summary_table_by_owner": summary_cli.render_table(report, summary, by_owner=True),
    }


def _golden() -> dict[str, Any]:
    data = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


_JSON_CASES = [
    # (キー, `notices` を置く位置の直前のキー)
    ("today_json", "disclaimer"),
    ("today_json_with_labels", "disclaimer"),
    ("summary_json", "schema_version"),
    ("summary_json_by_owner", "schema_version"),
]
_TABLE_CASES = ["today_table", "summary_table", "summary_table_by_owner"]


# =================================================================================================
# T1・T2 notices の中身
# =================================================================================================


@pytest.mark.parametrize("case_name", [k for k, _ in _JSON_CASES])
def test_t1_t2_the_json_carries_the_two_notices_in_order(case_name: str) -> None:
    notices = current_outputs()[case_name]["notices"]
    assert notices == list(NOTICES) == list(_SENTENCES)
    assert isinstance(notices, list) and len(notices) == 2  # tuple のまま JSON 化していない(型)


# =================================================================================================
# T3 3 画面で同一(detail を含む)
# =================================================================================================


def test_t3_today_summary_and_detail_share_the_same_notices() -> None:
    outputs = current_outputs()
    detail_screen = build_detail_screen(detail_tests._day())
    detail = json.loads(detail_renderer.render_detail_json(detail_screen))
    assert (
        outputs["today_json"]["notices"]
        == outputs["summary_json"]["notices"]
        == detail["notices"]
        == list(_SENTENCES)
    )


def test_t3_the_detail_renderer_still_exposes_the_same_names() -> None:
    """定義は cli_renderer へ移したが、detail_renderer の module 属性として残る。"""
    assert detail_renderer.NOTICES is NOTICES
    assert detail_renderer.NOTICE_NO_PUBLIC_SURFACE == NOTICE_NO_PUBLIC_SURFACE
    assert detail_renderer.NOTICE_NOT_INVESTMENT_ADVICE == NOTICE_NOT_INVESTMENT_ADVICE


# =================================================================================================
# T4 `notices` 以外の出力差分 0(変更前の全文との完全一致)
# =================================================================================================


@pytest.mark.parametrize(("case_name", "after"), _JSON_CASES)
def test_t4_nothing_but_notices_differs_from_the_output_before_the_change(
    case_name: str, after: str
) -> None:
    new = current_outputs()[case_name]
    before = _golden()[case_name]
    # ① キー集合 = 変更前 ∪ {notices}
    assert set(new) == set(before) | {"notices"}
    assert "notices" not in before
    # ② `notices` を除いた全体が変更前と完全一致(値・型・入れ子・順序)
    without = {k: v for k, v in new.items() if k != "notices"}
    assert without == before
    assert list(without) == list(before)
    # ③ `notices` の位置(detail と同じ相対位置 = disclaimer の直後。summary は版の直後)
    field_names = list(new)
    assert field_names[field_names.index(after) + 1] == "notices"
    # ④ 既存の schema_version は不変(追加のみ = 後方互換)
    assert new["schema_version"] == before["schema_version"] == 1


def test_t4_the_golden_itself_is_not_vacuous() -> None:
    golden = _golden()
    assert set(golden) == {k for k, _ in _JSON_CASES} | set(_TABLE_CASES)
    for case_name, _ in _JSON_CASES:
        assert len(golden[case_name]) >= 8  # 画面の実体(空の dict ではない)


# =================================================================================================
# T5 表の出力は不変
# =================================================================================================


@pytest.mark.parametrize("case_name", _TABLE_CASES)
def test_t5_the_tables_are_byte_for_byte_unchanged(case_name: str) -> None:
    assert current_outputs()[case_name] == _golden()[case_name]


# =================================================================================================
# T7 定義の単一性 + SHOULD-1(JSON 文書を全走査して `notices` の欠落を検出)
# =================================================================================================


def _src_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_t7_each_notice_sentence_is_defined_in_exactly_one_place() -> None:
    for sentence in _SENTENCES:
        homes = [
            p
            for p in _src_files()
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant) and node.value == sentence
        ]
        assert homes == [_SRC / "domain" / "notification" / "allocation_shadow_cli_renderer.py"]


def _json_document_dicts() -> list[tuple[Path, int, set[str]]]:
    """`allocation` 系の表示層で、`schema_version` を持つ dict リテラル = JSON 文書の最上位。"""
    targets = sorted(
        {
            *(_SRC / "domain" / "notification").glob("allocation_shadow_*.py"),
            *(_SRC / "cli").glob("allocation*.py"),
        }
    )
    found: list[tuple[Path, int, set[str]]] = []
    for path in targets:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                field_names = {
                    k.value
                    for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                if "schema_version" in field_names:
                    found.append((path, node.lineno, field_names))
    return found


def test_should1_every_json_document_in_the_allocation_screens_carries_notices() -> None:
    documents = _json_document_dicts()
    # 空振り防止: today / summary / detail の 3 つ以上を見つけている
    assert len(documents) >= 3, documents
    missing = [(p.name, line) for p, line, field_names in documents if "notices" not in field_names]
    assert missing == [], f"`notices` の無い JSON 文書: {missing}"


def test_should1_the_scan_is_not_vacuous_it_sees_the_three_known_documents() -> None:
    names = {p.name for p, _, _ in _json_document_dicts()}
    assert {
        "allocation_shadow_cli_renderer.py",
        "allocation_shadow_detail_renderer.py",
        "allocation_shadow_summary.py",
    } <= names
