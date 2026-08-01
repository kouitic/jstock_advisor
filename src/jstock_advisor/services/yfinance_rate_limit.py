"""Yahoo Financeデータ提供元障害への再試行対応(候補ユニバース本格対応・5節、案B)。

`services/screening_data_provider.py`の`StockSnapshotScreeningDataProvider`
**専用**の再試行ヘルパー。既存の`market_data`/`financial_data`/`dividend_data`
各yfinance Provider実装、`stock_snapshot_service.build_stock_snapshot()`は
一切変更しない(BUY候補・保有銘柄分析への影響を避けるため)。

このため`call_with_rate_limit_retry()`は`build_stock_snapshot()`全体を再実行する
想定で使う(呼び出し単位での再試行ではない)。**明記すべき欠点**: 内部で複数ある
yfinance呼び出しの一部が成功していても、再試行時に再度すべて実行される。
共有コードへ手を入れないことの代償として、Yahoo Financeへの実効リクエスト数は
呼び出し単位の再試行より多くなるが、この欠点は許容する。

障害の疑いがあるかどうかの判定自体(429/403/5xx・タイムアウト・接続切断・
yfinance固有例外の検知)は`services/provider_failure_classifier.py`に集約する
(運用ハードニング3節。このモジュールは再試行ループの制御にのみ責務を絞る)。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from jstock_advisor.services.provider_failure_classifier import classify_provider_failure

_BASE_DELAY_SECONDS = 2.0
_MAX_DELAY_SECONDS = 10.0
_MAX_RETRIES = 3
_JITTER_RATIO = 0.3


def _retry_after_seconds(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class RateLimitRetryResult[T]:
    value: T | None
    is_provider_failure_suspected: bool
    error: Exception | None


def call_with_rate_limit_retry[T](func: Callable[[], T]) -> RateLimitRetryResult[T]:
    """funcを最大`_MAX_RETRIES`回、データ提供元障害疑いの例外に対してのみ再試行する。

    障害疑いでない例外は再試行せずそのまま送出する(呼び出し側の既存の
    `except Exception`処理へ委ねる)。障害疑いのまま再試行上限に達した場合は
    例外を送出せず、`error`にセットして返す(呼び出し側で
    `ScreeningDataStatus.DATA_ERROR` + `is_provider_failure_suspected=True`として扱う)。
    """
    last_exception: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return RateLimitRetryResult(
                value=func(), is_provider_failure_suspected=False, error=None
            )
        except Exception as exc:  # noqa: BLE001 - 障害判定のため一旦すべて捕捉する
            if not classify_provider_failure(exc):
                raise
            last_exception = exc
            if attempt >= _MAX_RETRIES:
                break
            delay = _retry_after_seconds(exc)
            if delay is None:
                delay = min(_MAX_DELAY_SECONDS, _BASE_DELAY_SECONDS * (2**attempt))
                delay *= 1 + random.uniform(-_JITTER_RATIO, _JITTER_RATIO)
            time.sleep(max(0.0, delay))

    assert last_exception is not None  # ループはbreak前に必ず1回は例外を捕捉している
    return RateLimitRetryResult(
        value=None, is_provider_failure_suspected=True, error=last_exception
    )
