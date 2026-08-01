"""外部データ取得(yfinance)の障害分類(運用ハードニング3節)。

429だけでなく、403/5xx・タイムアウト・接続切断・yfinance固有のレート制限/crumb/
cookie関連例外を広く「データ提供元障害の疑い」として検知する。通常の
「この銘柄のデータが存在しない」(正常な応答だが空)ケースとは区別する
(混同すると、正常系の欠損銘柄まで障害集計へ算入してしまう)。

`services/yfinance_rate_limit.py`の再試行ループから呼ばれる(再試行の実施
判断)ほか、`lambda_handlers/watchlist_worker_handler.py`が最終的に
`is_provider_failure_suspected`として進捗行へ記録する際にも使う。
"""

from __future__ import annotations

_HTTP_STATUS_FAILURE_CODES = frozenset({403, 429, 500, 502, 503, 504})

# 例外メッセージに含まれていれば障害の疑いとみなすパターン(小文字比較)。
# yfinance固有のcrumb/cookie関連は、Yahoo側のボット対策強化時に頻発することが
# 実運用で確認されているエラーメッセージのパターン。
_FAILURE_MESSAGE_PATTERNS = (
    "429",
    "too many requests",
    "rate limit",
    "ratelimit",
    "403",
    "forbidden",
    "500",
    "502",
    "503",
    "504",
    "server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "remote end closed connection",
    "crumb",
    "cookie",
    "yfratelimiterror",
    "yfinanceexception",
)


def classify_provider_failure(exc: Exception) -> bool:
    """例外がデータ提供元の障害(スロットリング・一時的な提供元障害)の疑いが
    あるかどうかを判定する。True=障害の疑い、False=それ以外(通常の例外として
    呼び出し側の既存処理に委ねる)。
    """
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code is not None and status_code in _HTTP_STATUS_FAILURE_CODES:
        return True

    exception_type_name = type(exc).__name__.lower()
    if any(pattern in exception_type_name for pattern in ("timeout", "connectionerror")):
        return True

    message = str(exc).lower()
    return any(pattern in message for pattern in _FAILURE_MESSAGE_PATTERNS)
