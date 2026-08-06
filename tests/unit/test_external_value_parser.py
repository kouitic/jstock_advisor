"""ExternalValueParserのテスト(外部データ正規化レイヤー)。

正常系・異常系・None・空文字・全角・Excel形式・JPX形式・CSV形式を網羅する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser

# ===== stock_code =====


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1301", "1301"),
        ("1301.0", "1301"),  # Excel由来の小数化(文字列)
        (1301.0, "1301"),  # Excel由来の小数化(float型そのもの)
        ("001301", "1301"),  # 先頭ゼロ
        ("１３０１", "1301"),  # 全角
        (" 1301 ", "1301"),  # 前後空白
        ("130a", "130A"),  # 英数字混在は大文字化
        ("130A", "130A"),
        (1301, "1301"),  # int型そのもの
    ],
)
def test_stock_code_normal_cases(raw: object, expected: str) -> None:
    assert ExternalValueParser.stock_code(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "130",  # 3桁
        "13011",  # 5桁
        "1301.5",  # 小数部が0でない
        1301.5,
        "あいう",
        "1301,5",
        float("nan"),
        True,  # boolはintのサブクラスだがstr(True)=="TRUE"化を防ぐため明示的に拒否
        False,
        "ABCD",  # 先頭が数字でない4文字英字は東証の実際のコード体系にないため拒否
        "TRUE",  # 文字列としての"TRUE"も先頭が数字でないため拒否
    ],
)
def test_stock_code_invalid_cases(raw: object) -> None:
    assert ExternalValueParser.stock_code(raw) is None


# ===== integer =====


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100", 100),
        ("100.0", 100),  # Excel由来
        (100.0, 100),  # float型
        ("1,000", 1000),  # カンマ区切り
        ("12,345", 12345),  # 2桁+3桁の正しい区切り
        ("１，０００", 1000),  # 全角
        (" 100 ", 100),
        (100, 100),
        (Decimal("100"), 100),
        (Decimal("100.0"), 100),
        ("0", 0),
        ("-5", -5),
        ("+1000", 1000),
    ],
)
def test_integer_normal_cases(raw: object, expected: int) -> None:
    assert ExternalValueParser.integer(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "abc",
        "100.5",
        100.5,
        Decimal("100.5"),
        float("nan"),
        float("inf"),
        True,  # boolはintのサブクラスだが明示的に拒否する
        "1e10",  # 指数表記は安全側デフォルトとして拒否
    ],
)
def test_integer_invalid_cases(raw: object) -> None:
    assert ExternalValueParser.integer(raw) is None


def test_integer_none_and_empty() -> None:
    assert ExternalValueParser.integer(None) is None
    assert ExternalValueParser.integer("") is None


@pytest.mark.parametrize(
    "raw",
    [
        "1,00,0",  # 桁区切り位置が不正(最初の区切りが3桁でない)
        "10,00",  # 桁区切り位置が不正
        "1,2,3",  # 桁区切り位置が不正
        "12,34.5",  # 桁区切り位置が不正
        ",100",  # 先頭カンマ
        "100,",  # 末尾カンマ
        "1,,000",  # 連続カンマ
    ],
)
def test_integer_rejects_incorrectly_positioned_commas(raw: str) -> None:
    """桁区切りカンマの位置が3桁区切りとして不正な場合は受理しない(過剰補正の防止)。"""
    assert ExternalValueParser.integer(raw) is None


# ===== decimal =====


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234", Decimal("1234")),
        ("1234.5", Decimal("1234.5")),
        ("1,234.5", Decimal("1234.5")),
        ("12,345", Decimal("12345")),
        ("-1,234.5", Decimal("-1234.5")),
        ("＋1，234", Decimal("1234")),
        ("１，２３４．５", Decimal("1234.5")),  # 全角
        (" 1234.5 ", Decimal("1234.5")),
        (1234, Decimal(1234)),
        (1234.5, Decimal("1234.5")),
        (Decimal("1234.5"), Decimal("1234.5")),
        ("0", Decimal("0")),
        ("-100.25", Decimal("-100.25")),
    ],
)
def test_decimal_normal_cases(raw: object, expected: Decimal) -> None:
    assert ExternalValueParser.decimal(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "abc",
        "1234.5.6",
        float("nan"),
        float("inf"),
        Decimal("NaN"),
        True,
        "1e10",  # 指数表記は安全側デフォルトとして拒否
    ],
)
def test_decimal_invalid_cases(raw: object) -> None:
    assert ExternalValueParser.decimal(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "1,00,0",
        "10,00",
        "1,2,3",
        "12,34.5",
        ",100",
        "100,",
        "1,,000",
    ],
)
def test_decimal_rejects_incorrectly_positioned_commas(raw: str) -> None:
    assert ExternalValueParser.decimal(raw) is None


# ===== date =====


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-31", dt.date(2026, 7, 31)),
        ("2026/07/31", dt.date(2026, 7, 31)),
        ("20260731", dt.date(2026, 7, 31)),
        ("2026/7/31", dt.date(2026, 7, 31)),  # 単一桁月日
        (" 2026-07-31 ", dt.date(2026, 7, 31)),
        ("２０２６-０７-３１", dt.date(2026, 7, 31)),  # 全角
        (dt.date(2026, 7, 31), dt.date(2026, 7, 31)),
        (dt.datetime(2026, 7, 31, 12, 0), dt.date(2026, 7, 31)),
        (20260731, dt.date(2026, 7, 31)),  # int型のYYYYMMDD
        (20260731.0, dt.date(2026, 7, 31)),  # float型のYYYYMMDD
        (Decimal("20260731"), dt.date(2026, 7, 31)),  # Decimal型のYYYYMMDD
        (Decimal("20260731.0"), dt.date(2026, 7, 31)),
        ("20260731.0", dt.date(2026, 7, 31)),  # ".0"付き文字列
        ("２０２６０７３１．０", dt.date(2026, 7, 31)),  # 全角".0"付き文字列
    ],
)
def test_date_normal_cases(raw: object, expected: dt.date) -> None:
    assert ExternalValueParser.date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "2026-13-01",  # 不正な月
        "2026年7月31日",
        "not a date",
        "20260732",  # 存在しない日
        "20260230",  # 2月30日は存在しない
        "20261301",  # 月が13
        "99999999",  # 桁数はYYYYMMDD範囲内だが月日が不正
        1e20,  # 巨大すぎる数値(YYYYMMDD範囲外)
        float("nan"),
        float("inf"),
        True,
        False,
        46234,  # Excelシリアル値(YYYYMMDD範囲外、jpx_impl.py側で個別処理する)
        46234.0,
    ],
)
def test_date_invalid_cases(raw: object) -> None:
    assert ExternalValueParser.date(raw) is None


# ===== JPX形式(実データ準拠) =====


def test_stock_code_matches_jpx_normalization_semantics() -> None:
    """providers/candidate_universe/jpx_impl.pyの_normalize_stock_codeと
    同一の正規化結果になることを確認する(9節の5ステップ)。"""
    assert ExternalValueParser.stock_code(7203.0) == "7203"
    assert ExternalValueParser.stock_code("7203") == "7203"
    assert ExternalValueParser.stock_code(7203.5) is None


def test_stock_code_restores_leading_zero_lost_by_excel_numeric_cell() -> None:
    """Excelの数値セルとして格納されたことで失われた先頭ゼロは、float型からの
    変換時のみ4桁まで復元する(文字列としての短い入力を捏造しないための区別)。"""
    assert ExternalValueParser.stock_code(301.0) == "0301"


def test_stock_code_does_not_pad_short_plain_string() -> None:
    """ "130"のような3桁の文字列は、存在しないはずの"0130"へ捏造せず拒否する。"""
    assert ExternalValueParser.stock_code("130") is None


def test_date_matches_jpx_yyyymmdd_semantics() -> None:
    """data_j.xlsの日付列(YYYYMMDD形式の8桁数値文字列)を正しく解釈できること。"""
    assert ExternalValueParser.date("20260630") == dt.date(2026, 6, 30)


# ===== CSV形式(取り込みサービス実データ準拠) =====


def test_csv_style_inputs_are_all_parsed() -> None:
    row = {
        "stock_code": "2914",
        "shares": "1,000",
        "purchase_price": "3,456.7",
        "purchase_date": "2026-01-15",
    }
    assert ExternalValueParser.stock_code(row["stock_code"]) == "2914"
    assert ExternalValueParser.integer(row["shares"]) == 1000
    assert ExternalValueParser.decimal(row["purchase_price"]) == Decimal("3456.7")
    assert ExternalValueParser.date(row["purchase_date"]) == dt.date(2026, 1, 15)
