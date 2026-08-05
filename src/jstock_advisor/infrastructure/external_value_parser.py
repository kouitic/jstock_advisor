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

_STOCK_CODE_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
# providers/candidate_universe/jpx_impl.py の_parse_date_stringと同じ優先順位。
_DATE_FORMATS: tuple[str, ...] = ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d")


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
        text = ExternalValueParser._normalize_text(raw)
        if text is None:
            return None
        text = text.replace(",", "")
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
        text = ExternalValueParser._normalize_text(raw)
        if text is None:
            return None
        text = text.replace(",", "")
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None
        return value if value.is_finite() else None

    @staticmethod
    def date(raw: object) -> dt.date | None:
        """日付を正規化する。"YYYY-MM-DD"/"YYYY/MM/DD"/"YYYYMMDD"に対応する。

        Excelのシリアル値(数値)からの変換は、シート側の日付原点(1900年始まりか
        1904年始まりか)に依存するためこの汎用パーサーの対象外とし、呼び出し側
        (providers/candidate_universe/jpx_impl.py等)で個別に処理する。
        """
        if isinstance(raw, dt.datetime):
            return raw.date()
        if isinstance(raw, dt.date):
            return raw
        text = ExternalValueParser._normalize_text(raw)
        if text is None:
            return None
        for fmt in _DATE_FORMATS:
            try:
                return dt.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None
