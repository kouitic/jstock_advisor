"""yfinanceライブラリが出力する「期待される恒久missing」ログのseverity降格(Issue #125)。

yfinanceは既定で例外を隠す設定(`hide_exceptions`)のため、銘柄が存在しない・
上場廃止相当といった**恒久的に取得できない**ケースでも例外を送出せず、
ライブラリ自身のloggerへ`ERROR`を出力して空のデータフレームを返す。
その結果アプリ側のprovider分類器には一度も到達せず、
`ProviderDataError = 0` でありながら毎営業日固定件数の`ERROR`だけが残り、
真の障害(権限エラー等)がそのノイズに埋もれる。

## 認識する2つの確定signature

Production実測(Phase A2 / forensic)により、恒久missingの404は次の形で出力される。

    HTTP Error <status>: <reason句><応答body>

これは `str(例外) + 応答body` の**単純連結**であり、ticker はbody内の
`description` にしか現れない。reason句はHTTP/2では空になり得るため、
**reason句の内容に依存してはならない**。bodyを意味的に解釈して判定する。

もう一方の形式は別のexcept節(TickerBase側のcatch-all)から出る。

    Failed to get ticker '<ticker>' reason: <例外>

こちらは例外種別を問わない共通経路のため、接頭辞だけでは降格根拠にならない。
理由側に404と「見つからない」旨のmarkerが揃うことを追加条件として要求する。

## ★ `possibly delisted` を単独で降格してはならない理由

`$<ticker>: possibly delisted; <理由>` は**恒久上場廃止の確定を意味しない**。
timezone取得や価格取得のHTTP要求がtimeout・接続断・HTTP 5xxで終わった場合にも
例外が握り潰されて同じ文言になる。無条件に降格すると真の障害を隠す。

そこで、同一の取得コンテキスト内で同一tickerについて確定404を観測した
**後続**のものだけを、その404から派生した二次ノイズとして降格する。

相関のスコープは `yfinance_fetch_context()` で明示的に囲まれた範囲に限定する。
状態は`ContextVar`が保持するため、warm再利用・並行実行・別tickerの混入・
別invocationへの持ち越しが起きない(コンテキスト終了時に必ずreset)。

本モジュールはログのseverityのみを扱い、例外経路・retry・失敗率集計には
一切触れない(Issue #59のprovider failure semanticsは不変)。
依存ライブラリへのmonkey patchも行わない。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final

YFINANCE_LOGGER_NAME: Final = "yfinance"

_PROVIDER_NAME: Final = "yfinance"
_TICKER_SUFFIX: Final = ".T"

SIGNATURE_QUOTE_NOT_FOUND: Final = "QUOTE_NOT_FOUND_404"
SIGNATURE_SECONDARY_MISSING: Final = "PERMANENT_MISSING_SECONDARY"

#: Production実形式。`str(例外) + 応答body` の連結であり、reason句の有無は問わない。
#: status は本文から取り出して404のみを対象とする(他のstatusへは広げない)。
_SIG_HTTP_ERROR: Final = re.compile(
    r"^HTTP Error (?P<status>\d{3}):(?P<rest>.*)$", re.DOTALL
)

#: 曖昧なsignature。**単独では降格根拠にならない。**
#: 恒久missingでも一時障害でも同じ文言になるため、同一コンテキスト内の
#: 確定404と相関が取れた場合に限り二次ノイズとして降格する。
_SIG_POSSIBLY_DELISTED: Final = re.compile(
    r"^\$(?P<ticker>[^:\s]+): possibly delisted;\s*(?P<reason>.*)$"
)

#: 旧経路(TickerBase側のcatch-all)。**例外種別を問わない**ため接頭辞だけでは
#: 降格してはならない(timeout・接続断も同じ形になる)。
_SIG_FAILED_TO_GET_TICKER: Final = re.compile(
    r"^Failed to get ticker '(?P<ticker>[^']+)' reason:\s*(?P<reason>.*)$"
)

#: 旧経路を「銘柄が存在しない」と確定させるために理由側へ要求するmarker。
#: **すべて**満たす場合のみ降格する(ANDであってORではない)。
#: timeout は `Read timed out`、HTTP 5xx は `500 Server Error`、
#: 接続断は `Failed to establish a new connection`、レート制限は `429` となり、
#: いずれも404を含まないため降格対象にならない。
_LEGACY_MISSING_REASON_MARKERS: Final = ("404", "not found")

_NOT_FOUND_STATUS: Final = "404"
_NOT_FOUND_CODE: Final = "not found"
_QUOTE_NOT_FOUND_DESCRIPTION_PREFIX: Final = "quote not found for symbol:"

#: 解析を試みるbodyの上限。想定外に巨大な応答をログ処理内で解析しない
#: (超過時は解析せず`ERROR`のまま素通しする = fail-safe)。
_MAX_PARSED_BODY_CHARS: Final = 65_536


@dataclass
class _FetchContext:
    """1回のデータ取得の境界。取得中のtickerと、その中で確定したmissingを保持する。"""

    ticker: str
    confirmed_missing: set[str] = field(default_factory=set)


#: 現在の取得コンテキスト。コンテキスト外(``None``)では相関を行わず、
#: Production実形式の降格も成立させない(fail-safe側へ倒す)。
_fetch_context: ContextVar[_FetchContext | None] = ContextVar(
    "jstock_yfinance_fetch_context", default=None
)


@contextmanager
def yfinance_fetch_context(ticker: str) -> Iterator[None]:
    """1回のデータ取得の境界を明示し、その範囲でのみログの再分類・相関を有効にする。

    `ContextVar` を使うためスレッド・非同期タスクごとに独立し、
    ``finally`` で必ずresetするため、warm再利用や後続invocationへ状態が
    持ち越されることはない(例外で抜けた場合も同様)。入れ子にした場合は
    内側が独立した状態を持つため、外側の観測結果が混入しない。

    Args:
        ticker: この取得で問い合わせている提供元側のティッカーシンボル。
            ログから抽出したtickerがこれと一致することを降格の条件にする。
    """
    token = _fetch_context.set(_FetchContext(ticker=ticker))
    try:
        yield
    finally:
        _fetch_context.reset(token)


class YfinanceExpectedMissingLogFilter(logging.Filter):
    """期待される恒久missingの``ERROR``のみをstructured ``WARNING``へ降格する。

    - レコードを**削除しない**(常に``True``を返す)。「消す」のではなく
      「ERRORではないと分かる形で残す」ことが目的。
    - 対象は``ERROR``**ちょうど**。``CRITICAL``は決して降格しない。
    - 一致しないレコードは属性を一切変更しない。
    - 解析処理自体が例外を出した場合もレコードをそのまま通す
      (ログ出力経路を壊さない)。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # CRITICAL を降格しないため、不等号ではなく厳密一致で判定する。
        if record.levelno != logging.ERROR:
            return True

        try:
            classified = _classify(record.getMessage())
        except Exception:  # noqa: BLE001 - 解析の失敗でログ出力経路を壊さない
            return True

        if classified is None:
            return True

        signature, ticker, reason = classified
        if signature == SIGNATURE_QUOTE_NOT_FOUND:
            _remember_confirmed_missing(ticker)
        _downgrade(record, signature=signature, ticker=ticker, reason=reason)
        return True


def _classify(message: str) -> tuple[str, str, str] | None:
    """降格対象なら ``(signature, ticker, reason)`` を返す。該当しなければ ``None``。"""
    http_error = _SIG_HTTP_ERROR.match(message)
    if http_error is not None:
        return _classify_http_error(http_error)

    legacy = _SIG_FAILED_TO_GET_TICKER.match(message)
    if legacy is not None:
        reason = legacy.group("reason").strip()
        lowered = reason.lower()
        if all(marker in lowered for marker in _LEGACY_MISSING_REASON_MARKERS):
            return SIGNATURE_QUOTE_NOT_FOUND, legacy.group("ticker"), reason
        return None

    ambiguous = _SIG_POSSIBLY_DELISTED.match(message)
    if ambiguous is not None:
        ticker = ambiguous.group("ticker")
        if _is_confirmed_missing(ticker):
            return SIGNATURE_SECONDARY_MISSING, ticker, ambiguous.group("reason").strip()
        # 相関が取れない場合は ERROR のまま残す(降格しない)。

    return None


def _classify_http_error(matched: re.Match[str]) -> tuple[str, str, str] | None:
    """Production実形式の404を意味的に検証する。1つでも欠ければ ``None``。"""
    if matched.group("status") != _NOT_FOUND_STATUS:
        return None

    rest = matched.group("rest")
    if len(rest) > _MAX_PARSED_BODY_CHARS:
        return None

    body_start = rest.find("{")
    if body_start < 0:
        return None

    try:
        body = json.loads(rest[body_start:])
    except (ValueError, RecursionError):
        return None

    extracted = _extract_quote_not_found(body)
    if extracted is None:
        return None

    ticker, description = extracted
    context = _fetch_context.get()
    if context is None or context.ticker != ticker:
        # 取得中の銘柄と一致しないものは降格しない(取り違えを構造的に防ぐ)。
        return None

    return SIGNATURE_QUOTE_NOT_FOUND, ticker, description


def _extract_quote_not_found(body: Any) -> tuple[str, str] | None:
    """応答bodyが「指定銘柄が見つからない」を表す場合のみ ``(ticker, 説明)`` を返す。"""
    if not isinstance(body, dict):
        return None
    quote_summary = body.get("quoteSummary")
    if not isinstance(quote_summary, dict):
        return None
    error = quote_summary.get("error")
    if not isinstance(error, dict):
        return None

    code = error.get("code")
    if not isinstance(code, str) or code.strip().casefold() != _NOT_FOUND_CODE:
        return None

    description = error.get("description")
    if not isinstance(description, str):
        return None
    stripped = description.strip()
    if not stripped.casefold().startswith(_QUOTE_NOT_FOUND_DESCRIPTION_PREFIX):
        return None

    ticker = stripped[len(_QUOTE_NOT_FOUND_DESCRIPTION_PREFIX) :].strip()
    if not ticker or any(char.isspace() for char in ticker):
        return None

    return ticker, stripped


def _remember_confirmed_missing(ticker: str) -> None:
    context = _fetch_context.get()
    if context is not None:
        context.confirmed_missing.add(ticker)


def _is_confirmed_missing(ticker: str) -> bool:
    context = _fetch_context.get()
    return context is not None and ticker in context.confirmed_missing


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
