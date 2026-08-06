"""外部データ(CSV・Excel・LINEチャット入力等)の値正規化レイヤー。

これまでCSV取り込みサービス・LINEチャットコマンド・CLIオプション・JPX上場銘柄
一覧パーサーなど各所で個別に実装されていた「銘柄コード・株数・金額・日付を
外部入力の表記ゆれ(全角/半角、桁区切りカンマ、Excel由来の小数化、複数の
日付書式)を吸収してドメイン層の型へ変換する」処理を、この`ExternalValueParser`
へ一本化する。

変換できない入力は例外を投げず`None`を返す(呼び出し側で「Noneならエラー」と
判定する既存の流儀をそのまま使えるようにするため)。値の妥当性(正の数である
必要がある、等の業務ルール)はこの層の責務ではなく、呼び出し側で判定する。
"""

from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation

# 東証の英数字混在コードも先頭は必ず数字であるため、先頭1文字を数字限定にする
# (コードレビュー対応: 純粋な英字4文字が誤って証券コードとして通ることを防ぐ)。
_STOCK_CODE_PATTERN = re.compile(r"^[0-9][0-9A-Z]{3}$")
# providers/candidate_universe/jpx_impl.py の_parse_date_stringと同じ優先順位。
_DATE_FORMATS: tuple[str, ...] = ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d")
# YYYYMMDD形式の数値として妥当とみなす範囲(jpx_impl.py._extract_excel_dateと同じ範囲)。
# Excelシリアル値(通常5桁程度)はこの範囲より十分小さいため誤って解釈されない。
_YYYYMMDD_MIN = 19000101
_YYYYMMDD_MAX = 99991231
# 桁区切りカンマを含む数値は3桁区切りの正しい位置のみ許容する。指数表記は
# 外部CSV入力の安全側デフォルトとして明示的に拒否する。
_STRICT_NUMERIC_PATTERN = re.compile(r"^[+-]?\d+(\.\d+)?$")
_GROUPED_NUMERIC_PATTERN = re.compile(r"^[+-]?\d{1,3}(,\d{3})*(\.\d+)?$")


class ExternalValueParser:
    """外部入力値をドメイン層の型へ正規化する(staticmethodのみ)。"""

    @staticmethod
    def _normalize_text(raw: object) -> str | None:
        """全角→半角(NFKC)・前後空白除去を行う。Noneまたは空文字はNoneを返す。"""
        if raw is None:
            return None
        text = raw if isinstance(raw, str) else str(raw)
        text = unicodedata.normalize("NFKC", text).strip()
        return text or None

    @staticmethod
    def stock_code(raw: object) -> str | None:
        """銘柄コードを正規化する。

        "1301" → "1301"、"1301.0"(Excel由来の小数化) → "1301"、
        "001301"(過剰な先頭ゼロ) → "1301"、全角"１３０１" → "1301"。
        英数字4桁(大文字)として妥当でない場合はNoneを返す。

        数値セル由来(Excel等)の入力と、文字列としての先頭ゼロ付き入力とで
        扱いを分ける。前者はExcelが数値化した際に失われた先頭ゼロを4桁まで
        復元する(zfill)。後者は逆に、過剰な先頭ゼロを除去して実際の桁数
        (4桁)を確認する(lstrip)。この2つを混同すると、"130"のような
        本来3桁しかない入力まで存在しない"0130"へ捏造してしまうため区別する。
        """
        if isinstance(raw, bool):
            # boolはintのサブクラスであり、str(True)=="True"が英数字4文字として
            # 誤って証券コード扱いされてしまうため明示的に拒否する(コードレビュー対応)。
            return None
        is_numeric_origin = isinstance(raw, float)
        text = ExternalValueParser._normalize_text(raw)
        if text is None:
            return None
        # Excel由来の".0"サフィックス("1301.0"のような文字列化された数値セル)を除去する。
        if re.fullmatch(r"\d+\.0+", text):
            text = text.split(".", 1)[0]
            is_numeric_origin = True
        if text.isdigit():
            if is_numeric_origin:
                text = str(int(text)).zfill(4)
            else:
                stripped = text.lstrip("0")
                if len(stripped) != 4:
                    return None
                text = stripped
        else:
            text = text.upper()
        return text if _STOCK_CODE_PATTERN.match(text) else None

    @staticmethod
    def _normalize_numeric_text(raw: object) -> str | None:
        """カンマ区切りの位置を検証してから除去する(コードレビュー対応)。

        カンマを含む場合は正しい3桁区切り(例: "1,234.5")のみ許容し、
        "1,00,0"のような不正な位置のカンマは受理しない(過剰補正の防止)。
        カンマを含まない場合は指数表記を含まない通常の数値表現のみ許容する
        (外部CSV入力の安全側デフォルトとして指数表記を明示的に拒否する)。
        """
        text = ExternalValueParser._normalize_text(raw)
        if text is None:
            return None
        if "," in text:
            if not _GROUPED_NUMERIC_PATTERN.match(text):
                return None
            text = text.replace(",", "")
        if not _STRICT_NUMERIC_PATTERN.match(text):
            return None
        return text

    @staticmethod
    def integer(raw: object) -> int | None:
        """整数値を正規化する("1,000"や"１，０００"のような表記ゆれに対応)。

        小数点以下が0でない値(例: "100.5")は整数化できないためNoneを返す。
        """
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float):
            if not math.isfinite(raw) or not raw.is_integer():
                return None
            return int(raw)
        if isinstance(raw, Decimal):
            if raw != raw.to_integral_value():
                return None
            return int(raw)
        text = ExternalValueParser._normalize_numeric_text(raw)
        if text is None:
            return None
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None
        if not value.is_finite() or value != value.to_integral_value():
            return None
        return int(value)

    @staticmethod
    def decimal(raw: object) -> Decimal | None:
        """金額・比率等をDecimalへ正規化する("1,234.5"のような表記ゆれに対応)。"""
        if isinstance(raw, bool):
            return None
        if isinstance(raw, Decimal):
            return raw if raw.is_finite() else None
        if isinstance(raw, int):
            return Decimal(raw)
        if isinstance(raw, float):
            return Decimal(str(raw)) if math.isfinite(raw) else None
        text = ExternalValueParser._normalize_numeric_text(raw)
        if text is None:
            return None
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None
        return value if value.is_finite() else None

    @staticmethod
    def date(raw: object) -> dt.date | None:
        """日付を正規化する。"YYYY-MM-DD"/"YYYY/MM/DD"/"YYYYMMDD"に対応する。

        数値型(int/float/Decimal)のYYYYMMDD(例: 20260731、20260731.0)にも対応する
        (コードレビュー対応)。判定順序: datetime→date→bool拒否→int/float/Decimalの
        有限性・整数相当確認→8桁YYYYMMDD範囲確認→文字列のNFKC正規化→".0"サフィックス
        除去→通常の日付書式で解析。

        Excelのシリアル値(数値)からの変換は、シート側の日付原点(1900年始まりか
        1904年始まりか)に依存するためこの汎用パーサーの対象外とし、呼び出し側
        (providers/candidate_universe/jpx_impl.py等)で個別に処理する。Excelシリアル値
        (通常5桁程度)は_YYYYMMDD_MIN/MAXの範囲より十分小さいため、本メソッドが
        誤って日付として解釈することはない。

        datetime/dateの判定順序について: datetimeはdateのサブクラスのため、
        先にdate判定を行うとdatetimeインスタンスが.date()による時刻切り捨てを
        経ずに素通りしてしまう。既存の「datetime→date」の順序を維持する
        (ユーザー提示順序の「1.date 2.datetime」とは意図的に逆順)。
        """
        if isinstance(raw, dt.datetime):
            return raw.date()
        if isinstance(raw, dt.date):
            return raw
        if isinstance(raw, bool):
            return None
        text: str | None
        if isinstance(raw, (int, float, Decimal)):
            if isinstance(raw, float) and not math.isfinite(raw):
                return None
            if isinstance(raw, Decimal) and not raw.is_finite():
                return None
            if isinstance(raw, int):
                int_value = raw
            elif isinstance(raw, float):
                if not raw.is_integer():
                    return None
                int_value = int(raw)
            else:  # Decimal
                if raw != raw.to_integral_value():
                    return None
                int_value = int(raw)
            if not (_YYYYMMDD_MIN <= int_value <= _YYYYMMDD_MAX):
                return None
            text = str(int_value)
        else:
            text = ExternalValueParser._normalize_text(raw)
            if text is None:
                return None
            if re.fullmatch(r"\d{8}\.0+", text):
                text = text.split(".", 1)[0]
        for fmt in _DATE_FORMATS:
            try:
                return dt.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None
