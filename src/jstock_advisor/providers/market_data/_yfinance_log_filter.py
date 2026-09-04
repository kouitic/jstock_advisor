"""yfinanceライブラリが出力する「期待される恒久missing」ログのseverity降格(Issue #125)。

yfinanceは既定で例外を隠す設定(`hide_exceptions`)のため、銘柄が存在しない・
上場廃止相当といった**恒久的に取得できない**ケースでも例外を送出せず、
ライブラリ自身のloggerへ`ERROR`を出力して空のデータフレームを返す。
その結果アプリ側のprovider分類器には一度も到達せず、
`ProviderDataError = 0` でありながら毎営業日固定件数の`ERROR`だけが残り、
真の障害(権限エラー等)がそのノイズに埋もれる。

本モジュールは対象loggerへ`logging.Filter`を1つ追加し、
**期待される恒久missingのsignatureに一致するレコードだけ**を
structuredな`WARNING`へ降格する。一致しないレコードは一切改変せず素通しする
(timeout / connection error / HTTP 5xx / 想定外例外は`ERROR`のまま)。

本モジュールはログのseverityのみを扱い、例外経路・retry・失敗率集計には
一切触れない(Issue #59のprovider failure semanticsは不変)。
"""

from __future__ import annotations

import logging
import re
from typing import Final

YFINANCE_LOGGER_NAME: Final = "yfinance"

_PROVIDER_NAME: Final = "yfinance"
_TICKER_SUFFIX: Final = ".T"

#: 恒久missing signature 1。
#: yfinanceは銘柄が存在しない/上場廃止相当/timezone情報が無い場合に
#: `$<ticker>: possibly delisted; <理由>` という文言を出力する。
#: この文言はmissing銘柄専用の例外型からのみ生成されるため、
#: timeout等の一時障害が同じ形になることはない。
_SIG_POSSIBLY_DELISTED: Final = re.compile(
    r"^\$(?P<ticker>[^:\s]+): possibly delisted;\s*(?P<reason>.*)$"
)

#: 恒久missing signature 2。
#: `Failed to get ticker '<ticker>' reason: <例外>` は
#: **例外種別を問わない共通のcatch-all経路**から出力されるため、
#: この接頭辞だけで降格してはならない(timeout・connection errorも同じ形になる)。
#: 恒久missingと判定するには、理由側に404かつ「見つからない」旨のmarkerが
#: 揃っていることを追加条件として要求する。
_SIG_FAILED_TO_GET_TICKER: Final = re.compile(
    r"^Failed to get ticker '(?P<ticker>[^']+)' reason:\s*(?P<reason>.*)$"
)

#: signature 2 を恒久missingと確定させるために理由側へ要求するmarker。
#: **すべて**満たす場合のみ降格する(ANDであってORではない)。
#: 実際の理由文言は `404 Client Error: Not Found for url: ...` の形を取る。
#: 一方 timeout は `Read timed out`、HTTP 5xx は `500 Server Error`、
#: connection error は `Failed to establish a new connection` となり、
#: いずれも404を含まないため降格対象にならない。
_PERMANENT_MISSING_REASON_MARKERS: Final = ("404", "not found")


class YfinanceExpectedMissingLogFilter(logging.Filter):
    """期待される恒久missingのERRORのみをstructured WARNINGへ降格するフィルタ。

    - レコードを**削除しない**(常に``True``を返す)。「消す」のではなく
      「ERRORではないと分かる形で残す」ことが目的。
    - 一致しないレコードは属性を一切変更しない。
    - 既に降格済みのレコード(``WARNING``)には再度作用しないため、
      万一多重登録されても変換が重複しない。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True

        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - ログ整形の失敗で本処理を壊さない
            return True

        classified = _classify(message)
        if classified is None:
            return True

        signature, ticker, reason = classified
        _downgrade(record, signature=signature, ticker=ticker, reason=reason)
        return True


def _classify(message: str) -> tuple[str, str, str] | None:
    """恒久missingに一致すれば ``(signature, ticker, reason)`` を返す。"""
    matched = _SIG_POSSIBLY_DELISTED.match(message)
    if matched is not None:
        return (
            "PERMANENT_MISSING_SYMBOL",
            matched.group("ticker"),
            matched.group("reason").strip(),
        )

    matched = _SIG_FAILED_TO_GET_TICKER.match(message)
    if matched is not None:
        reason = matched.group("reason").strip()
        lowered = reason.lower()
        if all(marker in lowered for marker in _PERMANENT_MISSING_REASON_MARKERS):
            return "QUOTE_NOT_FOUND_404", matched.group("ticker"), reason

    return None


def _to_stock_code(ticker: str) -> str:
    if ticker.endswith(_TICKER_SUFFIX):
        return ticker[: -len(_TICKER_SUFFIX)]
    return ticker


def _downgrade(
    record: logging.LogRecord, *, signature: str, ticker: str, reason: str
) -> None:
    original_level = record.levelname
    stock_code = _to_stock_code(ticker)

    record.levelno = logging.WARNING
    record.levelname = logging.getLevelName(logging.WARNING)
    record.msg = (
        "yfinance expected permanent missing symbol: "
        f"provider={_PROVIDER_NAME} stock_code={stock_code} ticker={ticker} "
        f"signature={signature} expected=true "
        f'reason="{reason}" original_level={original_level}'
    )
    record.args = ()

    # 構造化handlerから機械的に扱えるよう属性としても保持する。
    record.jstock_expected_permanent_missing = True
    record.jstock_provider = _PROVIDER_NAME
    record.jstock_stock_code = stock_code
    record.jstock_ticker = ticker
    record.jstock_signature = signature
    record.jstock_reason = reason
    record.jstock_original_level = original_level


def install_yfinance_expected_missing_log_filter(
    logger: logging.Logger | None = None,
) -> bool:
    """対象loggerへフィルタを冪等に登録する。

    既に同種のフィルタが登録済みなら何もしない。モジュール初期化時に一度呼ぶ
    想定であり、実行中にloggerの状態を変更して戻す方式は採らない
    (Lambdaのwarm再利用・並行実行で壊れないことを優先する)。

    Returns:
        新たに登録した場合 ``True``、既に登録済みで何もしなかった場合 ``False``。
    """
    target = logger if logger is not None else logging.getLogger(YFINANCE_LOGGER_NAME)
    if any(isinstance(f, YfinanceExpectedMissingLogFilter) for f in target.filters):
        return False
    target.addFilter(YfinanceExpectedMissingLogFilter())
    return True
