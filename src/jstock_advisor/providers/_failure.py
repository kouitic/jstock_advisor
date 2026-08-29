"""provider例外の分類・安全ログ・送出ヘルパー(Issue #59 Phase B1)。

責務は「例外分類・安全なログ出力・retryability の保持・伝播」に限定する。
**domain値の変換は一切行わない**(SUCCESS+missing を返すかどうかは呼び出し側の契約)。

従来は provider が `except Exception: return None` で例外を消していたため、
`services/yfinance_rate_limit.py` の再試行も
`services/provider_failure_classifier.py` の障害分類も**例外を観測できず**、
一過性障害が「そのとき根拠が無かった」として恒久的に記録されていた
(retry も障害率の安全弁も構造的に発火しなかった)。

契約の詳細は `interfaces/provider_errors.py` のモジュールdocstringを参照。
"""

from __future__ import annotations

import logging
from typing import NoReturn

from jstock_advisor.interfaces.provider_errors import ProviderDataError, ProviderFailureCategory
from jstock_advisor.services.provider_failure_classifier import classify_provider_failure

logger = logging.getLogger(__name__)

# ログへ載せる例外メッセージの最大長(services/watchlist_display_name.pyと同じ方針)。
_ERROR_SUMMARY_MAX_LENGTH = 200


def raise_provider_data_error(exc: Exception, *, provider_name: str, operation: str) -> NoReturn:
    """外部アクセス中の例外を分類・記録し、ProviderDataErrorとして送出する。

    provider側で1回だけ分類し、`retryable` を属性として持たせる。再試行層は
    `classify_provider_failure()` の isinstance 短絡でこの属性を読むだけなので、
    **分類ロジックは二重実装されない**。

    ログには provider / operation / failure_category / retryable / error_type /
    安全要約(先頭200字)のみを出力する。APIキー・トークン・リクエストヘッダ・
    レスポンス本文・PII・秘密情報を含むURL/クエリは**出力しない**。
    """
    retryable = classify_provider_failure(exc)
    category = (
        ProviderFailureCategory.RETRYABLE_PROVIDER_FAILURE
        if retryable
        else ProviderFailureCategory.NON_RETRYABLE_PROVIDER_FAILURE
    )
    error_type = type(exc).__name__
    error_summary = str(exc)[:_ERROR_SUMMARY_MAX_LENGTH]
    logger.warning(
        "provider data fetch failed provider=%s operation=%s retryable=%s "
        "failure_category=%s error_type=%s error_summary=%s",
        provider_name,
        operation,
        retryable,
        category.value,
        error_type,
        error_summary,
    )
    raise ProviderDataError(
        provider_name=provider_name,
        operation=operation,
        retryable=retryable,
        failure_category=category,
        error_type=error_type,
        error_summary=error_summary,
    ) from exc
