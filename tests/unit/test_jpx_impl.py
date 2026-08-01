"""候補ユニバース本格対応(9節)のJPX Parser正規化ロジックのテスト。

data_j.xls(旧形式Excelバイナリ)は書き込み用ライブラリ(xlwt等)が本プロジェクトの
依存関係に含まれていないため、xlrd経由のバイナリ往復テストは行わず、9節の
5ステップ正規化ロジック(_normalize_stock_code)とJPX400 CSVパーサ
(parse_jpx400_weight_csv、純粋なテキストCSVのため直接構築可能)を対象とする。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.interfaces.candidate_universe import CandidateUniverseError
from jstock_advisor.providers.candidate_universe.jpx_impl import (
    _extract_excel_date,
    _normalize_stock_code,
    _parse_date_string,
    _resolve_jpx400_weight_column,
    parse_jpx400_weight_csv,
)

# --- 9節: 証券コード正規化(5ステップ) --------------------------------------------


def test_normalize_stock_code_from_excel_float_with_zero_fraction() -> None:
    assert _normalize_stock_code(1301.0) == "1301"


def test_normalize_stock_code_from_excel_float_with_nonzero_fraction_is_invalid() -> None:
    assert _normalize_stock_code(1301.5) is None


def test_normalize_stock_code_strips_whitespace() -> None:
    assert _normalize_stock_code("  1301  ") == "1301"


def test_normalize_stock_code_applies_nfkc_fullwidth_to_halfwidth() -> None:
    assert _normalize_stock_code("１３０１") == "1301"  # 全角数字


def test_normalize_stock_code_uppercases_alphanumeric_codes() -> None:
    assert _normalize_stock_code("130a") == "130A"


def test_normalize_stock_code_rejects_wrong_length() -> None:
    assert _normalize_stock_code("130") is None
    assert _normalize_stock_code("13011") is None


def test_normalize_stock_code_rejects_non_alphanumeric() -> None:
    assert _normalize_stock_code("130-") is None


# --- 日付抽出 ------------------------------------------------------------------


def test_parse_date_string_accepts_slash_format() -> None:
    assert _parse_date_string("2026/07/31") == dt.date(2026, 7, 31)


def test_parse_date_string_accepts_compact_format() -> None:
    assert _parse_date_string("20260731") == dt.date(2026, 7, 31)


def test_parse_date_string_returns_none_for_unparseable_value() -> None:
    assert _parse_date_string("not-a-date") is None


def test_extract_excel_date_from_string_cell() -> None:
    assert _extract_excel_date("2026/07/31", datemode=0) == dt.date(2026, 7, 31)


def test_extract_excel_date_from_numeric_excel_serial() -> None:
    # 2026-07-31のExcelシリアル値(1900日付方式、datemode=0)。
    result = _extract_excel_date(46234.0, datemode=0)
    assert result == dt.date(2026, 7, 31)


def test_extract_excel_date_returns_none_for_other_types() -> None:
    assert _extract_excel_date(None, datemode=0) is None


def test_extract_excel_date_from_yyyymmdd_numeric_cell() -> None:
    # data_j.xlsの実データはExcelシリアル値ではなくYYYYMMDD形式の数値
    # (例: 20260630.0)が数値セルとして格納されている。シリアル値として
    # 解釈するとOverflowErrorになるため、YYYYMMDD形式を先に判定する必要がある。
    assert _extract_excel_date(20260630.0, datemode=0) == dt.date(2026, 6, 30)


def test_extract_excel_date_handles_overflow_gracefully() -> None:
    # YYYYMMDD形式にもExcelシリアル値の妥当範囲にも該当しない巨大な数値は、
    # OverflowErrorを送出せずNoneを返す(呼び出し元は次の行で再試行する)。
    assert _extract_excel_date(1e20, datemode=0) is None


# --- JPX400ウェイト列のエイリアス解決 --------------------------------------------


def test_resolve_jpx400_weight_column_matches_official_name() -> None:
    fieldnames = ["日付", "銘柄名", "コード", "業種", "JPX日経400に占める個別銘柄のウェイト"]
    assert _resolve_jpx400_weight_column(fieldnames) == "JPX日経400に占める個別銘柄のウェイト"


def test_resolve_jpx400_weight_column_returns_none_when_absent() -> None:
    assert _resolve_jpx400_weight_column(["日付", "コード"]) is None


# --- parse_jpx400_weight_csv ----------------------------------------------------

_JPX400_CSV = (
    "日付,銘柄名,コード,業種,JPX日経400に占める個別銘柄のウェイト\n"
    "2026/07/31,テスト株式会社,1301,水産,0.50\n"
    "2026/07/31,サンプル商事,1332,卸売業,0.30\n"
).encode("utf-8-sig")


def test_parse_jpx400_weight_csv_extracts_member_codes_and_source_date() -> None:
    result = parse_jpx400_weight_csv(_JPX400_CSV)
    assert result.member_codes == {"1301", "1332"}
    assert result.source_date == dt.date(2026, 7, 31)
    assert result.raw_row_count == 2
    assert result.invalid_code_count == 0


def test_parse_jpx400_weight_csv_counts_invalid_codes() -> None:
    csv_bytes = (
        "日付,銘柄名,コード,業種,JPX日経400に占める個別銘柄のウェイト\n"
        "2026/07/31,不正銘柄,ABC,その他,0.10\n"
    ).encode("utf-8-sig")
    result = parse_jpx400_weight_csv(csv_bytes)
    assert result.member_codes == set()
    assert result.raw_row_count == 1
    assert result.invalid_code_count == 1


def test_parse_jpx400_weight_csv_decodes_cp932_when_utf8_fails() -> None:
    csv_text = (
        "日付,銘柄名,コード,業種,JPX日経400に占める個別銘柄のウェイト\n"
        "2026/07/31,テスト,1301,水産,0.50\n"
    )
    result = parse_jpx400_weight_csv(csv_text.encode("cp932"))
    assert result.member_codes == {"1301"}


def test_parse_jpx400_weight_csv_missing_required_column_raises() -> None:
    csv_bytes = "銘柄名,コード\nテスト,1301\n".encode("utf-8-sig")
    with pytest.raises(CandidateUniverseError):
        parse_jpx400_weight_csv(csv_bytes)


def test_parse_jpx400_weight_csv_missing_weight_column_raises() -> None:
    csv_bytes = "日付,銘柄名,コード\n2026/07/31,テスト,1301\n".encode("utf-8-sig")
    with pytest.raises(CandidateUniverseError):
        parse_jpx400_weight_csv(csv_bytes)
