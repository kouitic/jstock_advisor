"""yfinanceライブラリが出力する「期待される恒久missing」ログのseverity降格(Issue #125)。

yfinanceは既定で例外を隠す設定(`hide_exceptions`)のため、銘柄が存在しない・
上場廃止相当といった**恒久的に取得できない**ケースでも例外を送出せず、
ライブラリ自身のloggerへ`ERROR`を出力して空のデータフレームを返す。
その結果アプリ側のprovider分類器には一度も到達せず、
`ProviderDataError = 0` でありながら毎営業日固定件数の`ERROR`だけが残り、
真の障害(権限エラー等)がそのノイズに埋もれる。

## ★ `possibly delisted` を単独で降格してはならない理由

installed版の実装を確認した結果、`$<ticker>: possibly delisted; <理由>` は
**恒久上場廃止の確定を意味しない**ことが分かった。当該文言は次の経路で出力される。

    銘柄のtimezone取得が失敗する
      -> tz = None
      -> 履歴取得側で「有効な銘柄にtimezoneが無いのはおかしい」と判断され
         `possibly delisted; no timezone found` が出力される

    価格取得のHTTP要求が例外で終わる(例外は握り潰される)
      -> 応答が None のまま
      -> `possibly delisted; no price data found ...` が出力される

いずれも**timeout・接続断・HTTP 5xxでも同じ文言になる**。すなわち
`possibly delisted` は「理由が判然としないときに付く推測のprefix」であり、
これを無条件に降格すると真の障害を隠す(TRUE_ERROR_VISIBILITYの毀損)。

## 採用した方式

    UNAMBIGUOUS       Yahoo側が404 Not Foundを返した事実は、銘柄が存在しない
                      ことの十分に明確な根拠であるため、単独で降格する。

    SECONDARY         同一の取得コンテキスト内で同一tickerについて
                      confirmed 404 を観測した**後続**の `possibly delisted` は、
                      その404から派生した二次ノイズとみなして降格する。

    それ以外           `possibly delisted` は `ERROR` のまま残す。

相関のスコープは `yfinance_fetch_context()` で明示的に囲まれた範囲に限定する。
状態は`ContextVar`が保持するため、warm再利用・並行実行・別tickerの混入・
別invocationへの持ち越しが起きない(コンテキスト終了時に必ずreset)。

本モジュールはログのseverityのみを扱い、例外経路・retry・失敗率集計には
一切触れない(Issue #59のprovider failure semanticsは不変)。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

YFINANCE_LOGGER_NAME: Final = "yfinance"

_PROVIDER_NAME: Final = "yfinance"
_TICKER_SUFFIX: Final = ".T"

SIGNATURE_QUOTE_NOT_FOUND: Final = "QUOTE_NOT_FOUND_404"
SIGNATURE_SECONDARY_MISSING: Final = "PERMANENT_MISSING_SECONDARY"

#: 曖昧なsignature。**単独では降格根拠にならない。**
#: 恒久missingでも一時障害でも同じ文言になるため、同一コンテキスト内の
#: confirmed 404 と相関が取れた場合に限り二次ノイズとして降格する。
_SIG_POSSIBLY_DELISTED: Final = re.compile(
    r"^\$(?P<ticker>[^:\s]+): possibly delisted;\s*(?P<reason>.*)$"
)

#: 確定的なsignatureの候補。ただしこの接頭辞は
#: **例外種別を問わない共通のcatch-all経路**から出力されるため、
#: 接頭辞だけで降格してはならない(timeout・接続断も同じ形になる)。
_SIG_FAILED_TO_GET_TICKER: Final = re.compile(
    r"^Failed to get ticker '(?P<ticker>[^']+)' reason:\s*(?P<reason>.*)$"
)

#: signature を「銘柄が存在しない」と確定させるために理由側へ要求するmarker。
#: **すべて**満たす場合のみ降格する(ANDであってORではない)。
#: 実際の理由文言は `404 Client Error: Not Found for url: ...` の形を取る。
#: 一方 timeout は `Read timed out`、HTTP 5xx は `500 Server Error`、
#: 接続断は `Failed to establish a new connection`、レート制限は `429` となり、
#: いずれも404を含まないため降格対象にならない。
#: 404単独ではなく「見つからない」旨も併せて要求することで、
#: 404を含むだけの別種メッセージを巻き込まない最小安全条件とする。
_CONFIRMED_MISSING_REASON_MARKERS: Final = ("404", "not found")

#: 現在の取得コンテキスト内でconfirmed 404を観測したtickerの集合。
#: コンテキスト外(``None``)では相関を行わない = `possibly delisted` は
#: `ERROR` のまま残る(fail-safe側へ倒す)。
_confirmed_missing_tickers: ContextVar[set[str] | None] = ContextVar(
    "jstock_yfinance_confirmed_missing_tickers", default=None
)


@contextmanager
def yfinance_fetch_context() -> Iterator[None]:
    """1回のデータ取得の境界を明示し、その範囲でのみログ相関を有効にする。

    `ContextVar` を使うためスレッド・非同期タスクごとに独立し、
    ``finally`` で必ずresetするため、warm再利用や後続invocationへ状態が
    持ち越されることはない(例外で抜けた場合も同様)。入れ子にした場合は
    内側が独立した集合を持つため、外側の観測結果が混入しない。
    """
    token = _confirmed_missing_tickers.set(set())
    try:
        yield
    finally:
        _confirmed_missing_tickers.reset(token)


class YfinanceExpectedMissingLogFilter(logging.Filter):
    """期待される恒久missingの``ERROR``のみをstructured ``WARNING``へ降格する。

    - レコードを**削除しない**(常に``True``を返す)。「消す」のではなく
      「ERRORではないと分かる形で残す」ことが目的。
    - 対象は``ERROR``**ちょうど**。``CRITICAL``は決して降格しない。
    - 一致しないレコードは属性を一切変更しない。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # CRITICAL を降格しないため、不等号ではなく厳密一致で判定する。
        if record.levelno != logging.ERROR:
            return True

        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - ログ整形の失敗で本処理を壊さない
            return True

        confirmed = _SIG_FAILED_TO_GET_TICKER.match(message)
        if confirmed is not None:
            reason = confirmed.group("reason").strip()
            lowered = reason.lower()
            if all(marker in lowered for marker in _CONFIRMED_MISSING_REASON_MARKERS):
                ticker = confirmed.group("ticker")
                _remember_confirmed_missing(ticker)
                _downgrade(
                    record,
                    signature=SIGNATURE_QUOTE_NOT_FOUND,
                    ticker=ticker,
                    reason=reason,
                )
            return True

        ambiguous = _SIG_POSSIBLY_DELISTED.match(message)
        if ambiguous is not None:
            ticker = ambiguous.group("ticker")
            if _is_confirmed_missing(ticker):
                _downgrade(
                    record,
                    signature=SIGNATURE_SECONDARY_MISSING,
                    ticker=ticker,
                    reason=ambiguous.group("reason").strip(),
                )
            # 相関が取れない場合は ERROR のまま残す(降格しない)。
        return True


def _remember_confirmed_missing(ticker: str) -> None:
    confirmed = _confirmed_missing_tickers.get()
    if confirmed is not None:
        confirmed.add(ticker)


def _is_confirmed_missing(ticker: str) -> bool:
    confirmed = _confirmed_missing_tickers.get()
    return confirmed is not None and ticker in confirmed


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
