"""Issue #605 最小 CLI: 画面モデル → 表 / JSON の契約テスト(純粋・決定的)。

固定するもの:
  - 表: 免責・対象日・状況・各所有者の節・候補の表(日本語の列・RAER の暫定表記・理由=コード+日本語)
  - ★ 表は既定でラベルを出し、出したときは末尾に共有の注意を付ける。JSON は既定でラベルを出さない ★
    (`with_labels=True` のときだけ表示名を含める)
  - 索引なし・未記録・記録 0 件の日を『0 件』と表示しない(状態の文言)
  - 決定的・JSON は妥当・表の列がそろう(全角幅を考慮)・金額は文字列のまま
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.notification.allocation_shadow_cli_renderer import (
    DISCLAIMER,
    LABEL_NOTICE,
    OUTPUT_SCHEMA_VERSION,
    render_json,
    render_table,
    screen_to_dict,
)
from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DAY_STATE_TEXT,
    DayInput,
    DayState,
    RecordInput,
    RecordState,
    build_screen,
    display_ref,
)
from tests.unit import test_allocation_shadow_cli_view_contract as view_tests
from tests.unit import test_allocation_shadow_compute_contract as compute_tests

_SENTINEL_LABEL = "sentinel-表示名-zz"
_DAY = view_tests._DAY
MISSING = view_tests.MISSING


def _screen(labels: dict[str, str] | None = None) -> Any:
    return build_screen(view_tests._day_with(view_tests._owner_record()), labels)


def _label_for_owner_a() -> dict[str, str]:
    digest = compute_tests._ref("owner-a").split(":")[1]
    return {digest[:8]: _SENTINEL_LABEL}


def test_the_table_has_the_disclaimer_the_date_and_the_state() -> None:
    text = render_table(_screen())

    lines = text.splitlines()
    assert lines[0] == DISCLAIMER
    assert f"対象日: {_DAY}" in lines[1]
    assert DAY_STATE_TEXT[DayState.COMPLETE] in lines[2]
    assert text.endswith("\n")


def test_the_table_shows_every_item_the_user_approved() -> None:
    text = render_table(_screen())

    for needle in (
        "所有者:",
        "買付余力: 10,000,000円",
        "ポートフォリオ評価額: 50,000,000円",
        "購入候補(RAER の高い順)",
        "順位",
        "銘柄",
        "RAER",
        "推奨株数",
        "必要資金",
        "残余資金",
        "理由",
        "35.400 暫定",  # RAER の暫定の表記
        "配分しました(ALLOCATED)",  # 理由 = 日本語 + 内部コード
        "買付余力が残っていないため見送り(INSUFFICIENT_CASH_REMAINING)",
        "5,000,000円",
        "(不足 1,000,000円)",
        "購入 1 件 / 購入しない候補 1 件",
        "見込み残高(目安): 5,000,000円",
        "(暫定式)",
    ):
        assert needle in text, needle


def test_the_table_columns_are_aligned_in_display_width() -> None:
    text = render_table(_screen())

    def width(line: str) -> int:
        return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in line)

    table_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    assert len(table_lines) == 4  # 見出し + 区切り + 2 行
    assert len({width(line) for line in table_lines}) == 1


def test_the_table_is_deterministic() -> None:
    assert render_table(_screen()) == render_table(_screen())
    assert render_json(_screen()) == render_json(_screen())


# --- ラベル(共有の注意)---------------------------------------------------------------------------


def test_the_table_shows_labels_by_default_and_warns_about_sharing() -> None:
    text = render_table(_screen(_label_for_owner_a()))

    assert _SENTINEL_LABEL in text
    assert text.rstrip().endswith(LABEL_NOTICE)


def test_the_table_has_no_notice_when_no_label_is_shown() -> None:
    assert LABEL_NOTICE not in render_table(_screen())
    assert LABEL_NOTICE not in render_table(_screen(_label_for_owner_a()), show_labels=False)
    assert _SENTINEL_LABEL not in render_table(_screen(_label_for_owner_a()), show_labels=False)


def test_json_never_contains_a_label_by_default() -> None:
    screen = _screen(_label_for_owner_a())

    default = render_json(screen)

    assert _SENTINEL_LABEL not in default
    assert '"label"' not in default
    assert _SENTINEL_LABEL in render_json(screen, with_labels=True)


def test_the_default_json_leaks_no_label_in_any_nested_value() -> None:
    parsed = screen_to_dict(_screen(_label_for_owner_a()))

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            assert "label" not in value
            for inner in value.values():
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)
        elif isinstance(value, str):
            assert _SENTINEL_LABEL not in value

    walk(parsed)


# --- JSON -----------------------------------------------------------------------------------------


def test_the_json_is_valid_and_keeps_amounts_as_strings() -> None:
    parsed = json.loads(render_json(_screen()))

    assert parsed["schema_version"] == OUTPUT_SCHEMA_VERSION == 1
    assert parsed["business_date"] == _DAY
    assert parsed["state"] == "COMPLETE"
    assert parsed["complete"] is True
    owner = parsed["owners"][0]
    assert owner["status"] == "OK"
    assert owner["available_cash"] == "10,000,000円"
    first = owner["candidates"][0]
    assert first["raer"]["value"] == "35.400"
    assert first["decision"] == {"code": "ALLOCATED", "text": "配分"}
    assert first["reason"]["code"] == "ALLOCATED"
    assert isinstance(first["shares"], str)  # 数値にしない
    assert owner["candidates"][1]["shortfall"] == "1,000,000円"


def test_the_json_carries_the_code_and_the_japanese_text_in_separate_keys() -> None:
    owner = json.loads(render_json(_screen()))["owners"][0]

    assert owner["outcome"] == {"code": "COMPUTED", "text": "計算できた"}
    assert owner["run_status"]["code"] == "ALLOCATED_SOME"


# --- 状態の文言(黙って 0 件にしない)---------------------------------------------------------------


@pytest.mark.parametrize(
    "day",
    [
        DayInput(_DAY, MISSING, MISSING, {}),
        DayInput(_DAY, MISSING, RecordInput(RecordState.FOUND, view_tests._skip_record()), {}),
        DayInput(
            _DAY,
            RecordInput(
                RecordState.FOUND,
                view_tests._index(refs=[], recorded=0, unrecorded=2, reasons={"WRITE_FAILED": 2}),
            ),
            MISSING,
            {},
        ),
    ],
)
def test_a_day_without_records_never_reads_as_zero_results(day: DayInput) -> None:
    screen = build_screen(day)
    text = render_table(screen)

    assert DAY_STATE_TEXT[screen.state] in text
    assert "購入 0 件" not in text
    assert screen.complete is False
    assert json.loads(render_json(screen))["complete"] is False


def test_the_key_failure_day_shows_the_reason_code_and_the_owner_count() -> None:
    day = DayInput(_DAY, MISSING, RecordInput(RecordState.FOUND, view_tests._skip_record()), {})

    text = render_table(build_screen(day))

    assert "鍵を読む権限が無い(ACCESS_DENIED)" in text
    assert "記録できなかった所有者 2 件" in text
    assert "運用の確認が要ります" in text


def test_a_no_index_day_without_skip_does_not_claim_the_cause() -> None:
    text = render_table(build_screen(DayInput(_DAY, MISSING, MISSING, {})))

    assert "未実行、または記録の途中で止まった可能性" in text
    assert "鍵" not in text


def test_a_partial_day_puts_the_unrecorded_reasons_and_the_rerun_note_in_view() -> None:
    index = view_tests._index(unrecorded=2, reasons={"BUDGET_EXHAUSTED": 2})
    text = render_table(build_screen(view_tests._day_with(view_tests._owner_record(), index)))

    assert "未記録 2 件" in text
    assert "時間の予算が尽きた(BUDGET_EXHAUSTED) 2 件" in text
    assert "再実行で後から追加された記録は、先着の索引には載りません" in text


def test_a_missing_record_names_the_reference_by_its_leading_digits() -> None:
    ref = compute_tests._ref("owner-a")
    day = DayInput(
        _DAY, RecordInput(RecordState.FOUND, view_tests._index([ref])), MISSING, {ref: MISSING}
    )

    text = render_table(build_screen(day))

    version, digest = ref.split(":")
    assert f"{version}:{digest[:12]}" in text
    assert digest[:13] not in text  # 先頭 12 桁だけ
    assert "記録が見つかりません" in text


def test_the_unsupported_version_is_shown_with_the_seen_version() -> None:
    index = view_tests._index()
    index["schema_version"] = 7

    text = render_table(
        build_screen(DayInput(_DAY, RecordInput(RecordState.FOUND, index), MISSING, {}))
    )

    assert "schema_version = 7" in text


def test_the_json_of_a_day_without_an_index_has_a_null_index() -> None:
    parsed = json.loads(render_json(build_screen(DayInput(_DAY, MISSING, MISSING, {}))))

    assert parsed["index"] is None
    assert parsed["owners"] == []
    assert parsed["state"] == "NO_INDEX"


def test_no_plain_owner_or_internal_identifier_is_printed() -> None:
    """記録に無い値(batch_id・鍵の版以外の識別子・owner の平文)を画面に出さない。"""
    screen = _screen()
    text = render_table(screen) + render_json(screen)

    assert view_tests._BATCH not in text
    assert "owner-a" not in text.replace(
        "owner-" + compute_tests._ref("owner-a").split(":")[1][:6], ""
    )


def test_a_real_day_prints_no_owner_plaintext_and_no_holding_identifier(tmp_path: Path) -> None:
    """R1 の品質条件: 所有者の実名・保有 ID・保有の銘柄は画面にも JSON にも出ない(候補は出る)。"""
    day, parts = view_tests._real(tmp_path)
    screen = build_screen(day)
    text = render_table(screen) + render_json(screen) + render_json(screen, with_labels=True)

    for forbidden in ("owner-a#", "#9001", "holding_id", "stock_name", "テスト銘柄"):
        assert forbidden not in text
    assert "9001" not in text  # 保有の銘柄(候補ではない)は出さない
    assert "owner-a" not in text.replace(display_ref(parts["refs"][0]), "")
    assert "0001" in render_table(screen)  # 候補の銘柄は表に出る(空振りでない。面を特定)


def test_the_total_of_unparsed_items_is_shown_once_in_the_table() -> None:
    record = view_tests._owner_record()
    record["candidates"].append("junk")

    text = render_table(build_screen(view_tests._day_with(record)))

    assert "解釈できなかった項目: 合計 1 件" in text
