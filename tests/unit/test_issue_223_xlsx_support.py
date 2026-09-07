"""Issue #223 PR-2: 東証上場銘柄一覧の .xls / .xlsx 両対応の検証。

JPX は 2026-09-03 に data_j.xls を data_j.xlsx へ差し替えたが、
**URL のトークンもファイル名の幹も変えなかった**。したがって拡張子は
判別の根拠にならず、中身の先頭バイト（マジックナンバー）で判別する。

中心は 2 点。
  1  .xlsx を openpyxl で読めること（往復テスト）
  2  **判定・正規化・集計は 1 実装のまま**であること
     （容れ物が変わっても、パース後の結果が同じになること）

実データ・実在の銘柄名は使用しない（架空の銘柄コードと名称のみ）。
JPX から取得したファイルを fixture にもしていない。
"""

from __future__ import annotations

import datetime as dt
import io

import openpyxl
import pytest

from jstock_advisor.interfaces.candidate_universe import CandidateUniverseError
from jstock_advisor.providers.candidate_universe.jpx_impl import (
    _XLS_MAGIC,
    _XLSX_MAGIC,
    _extract_excel_date,
    _normalize_code_cell,
    _read_listed_issues_rows,
    parse_listed_issues_xls,
)

_HEADER = [
    "日付",
    "コード",
    "銘柄名",
    "市場・商品区分",
    "33業種コード",
    "33業種区分",
    "17業種コード",
    "17業種区分",
    "規模コード",
    "規模区分",
]
_PRIME = "プライム（内国株式）"
_STANDARD = "スタンダード（内国株式）"
_GROWTH = "グロース（内国株式）"
_TARGET = {_PRIME, _STANDARD}


def _row(code: object, segment: str, date: object = 20260831) -> list[object]:
    return [date, code, "架空銘柄", segment, "0050", "架空業種", "1", "架空17", "4", "架空規模"]


def _xlsx(rows: list[list[object]], header: list[str] | None = None) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(header if header is not None else _HEADER)
    for row in rows:
        sheet.append(row)
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


# --- 先頭バイトによる判別 -----------------------------------------------------------


def test_magic_numbers_are_the_documented_values() -> None:
    """.xls は OLE2 複合ドキュメント、.xlsx は ZIP。"""
    assert bytes.fromhex("d0cf11e0") == _XLS_MAGIC
    assert bytes.fromhex("504b0304") == _XLSX_MAGIC


def test_xlsx_is_detected_by_leading_bytes_not_by_extension() -> None:
    """★ 拡張子を見ていないこと。

    JPX は URL のトークンもファイル名の幹も変えずに中身だけ差し替えた。
    ファイル名を根拠にすると、同じことが起きたときに再び取り違える。
    """
    data = _xlsx([_row(9001, _PRIME)])
    assert data.startswith(_XLSX_MAGIC)
    rows, datemode = _read_listed_issues_rows(data)
    assert rows[0] == _HEADER
    assert datemode is None  # datemode は .xls 固有。openpyxl 経路では持たない


def test_unknown_container_is_rejected_with_a_clear_error() -> None:
    """HTML エラーページ等が来たときに、握りつぶさず落ちること。"""
    with pytest.raises(CandidateUniverseError, match="形式を判別できません"):
        _read_listed_issues_rows(b"<html><body>error</body></html>")


def test_empty_payload_is_rejected() -> None:
    with pytest.raises(CandidateUniverseError):
        _read_listed_issues_rows(b"")


# --- .xlsx の往復 -------------------------------------------------------------------


def test_xlsx_round_trip_parses_items_and_source_date() -> None:
    data = _xlsx(
        [
            _row(9001, _PRIME),
            _row("256A", _STANDARD),  # 英字を含む 4 桁コード
            _row(9002, _GROWTH),  # 対象外の市場区分
        ]
    )
    result = parse_listed_issues_xls(data, _TARGET)

    assert [item.stock_code for item in result.items] == ["9001", "256A"]
    assert result.raw_row_count == 2  # 対象区分のみを数える（グロースは含めない）
    assert result.source_date == dt.date(2026, 8, 31)
    assert result.invalid_code_count == 0
    assert result.duplicate_count == 0
    assert result.unknown_market_segment_count == 0


def test_xlsx_normalizes_codes_and_counts_duplicates_the_same_way() -> None:
    """★ パース後のロジックが 1 実装のままであることの確認。

    ゼロ埋め・重複・不正コードの扱いは容れ物に依存しない。
    """
    data = _xlsx(
        [
            _row(1301, _PRIME),
            _row("1301", _PRIME),  # 重複
            _row("あいう", _PRIME),  # 不正コード
        ]
    )
    result = parse_listed_issues_xls(data, _TARGET)

    assert [item.stock_code for item in result.items] == ["1301"]
    assert result.raw_row_count == 3
    assert result.duplicate_count == 1
    assert result.invalid_code_count == 1


def test_xlsx_counts_unknown_market_segment_before_the_target_filter() -> None:
    data = _xlsx([_row(9001, _PRIME), _row(9002, "架空の区分")])
    result = parse_listed_issues_xls(data, _TARGET)
    assert result.unknown_market_segment_count == 1
    assert result.raw_row_count == 1  # 未知区分は対象外なので items にも入らない


def test_xlsx_missing_required_column_is_rejected() -> None:
    broken = [name for name in _HEADER if name != "コード"]
    data = _xlsx([], header=broken)
    with pytest.raises(CandidateUniverseError, match="必須列がありません"):
        parse_listed_issues_xls(data, _TARGET)


def test_xlsx_tolerates_short_trailing_rows() -> None:
    """末尾に列数の足りない行があっても落ちないこと。"""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(_HEADER)
    sheet.append(_row(9001, _PRIME))
    sheet.append([None])  # 列数の足りない行
    buf = io.BytesIO()
    workbook.save(buf)
    result = parse_listed_issues_xls(buf.getvalue(), _TARGET)
    assert [item.stock_code for item in result.items] == ["9001"]


# --- 日付の解釈（容れ物ごとにセルの型が違う） ---------------------------------------


def test_extract_excel_date_accepts_the_types_each_reader_returns() -> None:
    """xlrd は数値セルを float、openpyxl は int / datetime で返す。

    どちらでも同じ dt.date になること。**実データの「日付」列は
    YYYYMMDD 形式**（Excel のシリアル値ではない）。
    """
    expected = dt.date(2026, 8, 31)
    assert _extract_excel_date(20260831) == expected  # openpyxl: int
    assert _extract_excel_date(20260831.0, datemode=0) == expected  # xlrd: float
    assert _extract_excel_date(20260831.0) == expected  # datemode なしでも同じ
    assert _extract_excel_date(dt.datetime(2026, 8, 31, 12, 0)) == expected
    assert _extract_excel_date(dt.date(2026, 8, 31)) == expected
    assert _extract_excel_date("2026-08-31") == expected


def test_extract_excel_date_without_datemode_does_not_call_xlrd() -> None:
    """★ datemode は .xls 固有の概念であり、openpyxl 経路では持たない。

    シリアル値らしき小さな数値が来ても、datemode が無ければ解釈しない
    （xlrd へ委ねて誤った日付を作らない）。
    """
    assert _extract_excel_date(45000.0) is None
    assert _extract_excel_date(45000.0, datemode=0) is not None  # .xls 経路なら解釈する


@pytest.mark.parametrize("value", [None, True, False, "", "not-a-date", object()])
def test_extract_excel_date_returns_none_for_unusable_values(value: object) -> None:
    assert _extract_excel_date(value) is None


# --- 現行の検証しきい値が通ること ---------------------------------------------------


def test_realistic_row_volume_stays_within_the_validator_bounds() -> None:
    """★ 本番相当の件数で `_validate` の行数境界（2,500〜4,000）に収まること。

    2026-09-07 の実測では、全 4,441 行のうち対象区分は 3,111 行だった。
    raw_row_count は **対象区分のみ**を数えるため、全行数が上限 4,000 を
    超えていても検証は通る。この関係を架空データで再現して固定する。
    """
    rows = [_row(f"{1000 + i:04d}", _PRIME) for i in range(3111)]
    rows += [_row(f"{5000 + i:04d}", _GROWTH) for i in range(1330)]  # 合計 4,441 行
    result = parse_listed_issues_xls(_xlsx(rows), _TARGET)

    assert result.raw_row_count == 3111
    assert 2500 <= result.raw_row_count <= 4000  # _ROW_COUNT_BOUNDS["listed_issues"]
    assert len(result.items) == 3111
    assert result.unknown_market_segment_count == 0


# --- 読み手の違いを吸収していること（exact diff review F-2 / F-3） -------------------


def test_empty_cells_become_none_not_the_string_none() -> None:
    """★ F-2: openpyxl は空セルを None、xlrd は "" で返す。

    読み出し層で吸収しないと、後段の `str(value).strip()` が文字列 "None" を
    作り、銘柄名・業種コード・規模区分へ "None" が入る。市場・商品区分では
    未知区分として数えられてしまう。
    """
    data = _xlsx([[20260831, 9001, None, _PRIME, None, None, None, None, None, None]])
    result = parse_listed_issues_xls(data, _TARGET)
    item = result.items[0]

    assert item.stock_name is None
    assert item.industry_33_code is None
    assert item.industry_33_name is None
    assert item.industry_17_code is None
    assert item.size_code is None
    assert item.size_name is None
    assert result.unknown_market_segment_count == 0  # 区分は埋まっているので未知ではない


def test_empty_market_segment_is_counted_as_unknown_not_as_the_string_none() -> None:
    data = _xlsx([[20260831, 9001, "架空銘柄", None, None, None, None, None, None, None]])
    result = parse_listed_issues_xls(data, target_market_segments=None)
    assert result.items[0].market_segment is None
    assert result.unknown_market_segment_count == 1


def test_numeric_code_columns_do_not_depend_on_the_reader() -> None:
    """★ F-3: 業種コード・規模コードは JPX のファイル上で **数値**として
    格納されており、同じ値でも読み手で型が違う。

        .xls  / xlrd     float 50.0
        .xlsx / openpyxl int   50

    素朴な str() のままだと "50.0" と "50" に分かれ、**容れ物が変わっただけで
    同じ銘柄の業種コードが変わる**。`industry_33_code` は canonical の
    「安定キー」と定められた値であり、揺れてはならない。
    """
    assert _normalize_code_cell(50.0) == "50"  # xlrd が返す形
    assert _normalize_code_cell(50) == "50"  # openpyxl が返す形
    assert _normalize_code_cell(1050.0) == _normalize_code_cell(1050) == "1050"
    # 数値でない値（ETF/REIT 行の "-" 等）はそのまま
    assert _normalize_code_cell("-") == "-"
    assert _normalize_code_cell(" 0050 ") == "0050"
    # 空・非数値
    assert _normalize_code_cell(None) is None
    assert _normalize_code_cell("") is None
    assert _normalize_code_cell(True) is None
    # 整数でない float は落とさない（情報を失わない）
    assert _normalize_code_cell(50.5) == "50.5"


def test_xlsx_industry_codes_have_no_float_artifact() -> None:
    """通しで見ても ".0" が付かないこと。"""
    data = _xlsx([[20260831, 9001, "架空銘柄", _PRIME, 50, "架空業種", 1, "架空17", 6, "架空規模"]])
    item = parse_listed_issues_xls(data, _TARGET).items[0]
    assert item.industry_33_code == "50"
    assert item.industry_17_code == "1"
    assert item.size_code == "6"
