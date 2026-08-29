"""provider例外の分類・安全ログ・送出ヘルパー(Issue #59 Phase B1 / B2)。

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
import re
from typing import NoReturn

from jstock_advisor.interfaces.provider_errors import ProviderDataError, ProviderFailureCategory
from jstock_advisor.services.provider_failure_classifier import classify_provider_failure

logger = logging.getLogger(__name__)

# ログへ載せる例外メッセージの最大長(services/watchlist_display_name.pyと同じ方針)。
_ERROR_SUMMARY_MAX_LENGTH = 200

REDACTED = "***REDACTED***"

# 秘密情報を含みうるキー名(大文字小文字を区別しない)。データ提供元の例外メッセージには
# リクエストURL・ヘッダ断片がそのまま含まれることがあるため、既知のcredential patternは
# 値を伏せる。網羅は原理的に不可能だが、**既知パターンは必ず伏せる**ことを契約とする。
_CREDENTIAL_KEYS = (
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "subscription-key",
    "subscription_key",
    "authorization",
    "cookie",
    "crumb",
    "password",
    "secret",
)

# `key=value` / `key: value`(URLクエリ・ヘッダ断片の両方)。値は空白・`&`・引用符・
# セミコロンまでを1つの値とみなす。
_CREDENTIAL_KEY_ALTERNATION = "|".join(re.escape(key) for key in _CREDENTIAL_KEYS)
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(" + _CREDENTIAL_KEY_ALTERNATION + r")(\s*[=:]\s*)([^\s&;,'\"]+)"
)

# `Authorization: Bearer <value>` / `Basic <value>` のスキーム付き形式。
# 上の_KEY_VALUE_PATTERNは "authorization: Bearer" までしか伏せないため、
# スキームの後続値も個別に伏せる。
_AUTH_SCHEME_PATTERN = re.compile(r"(?i)\b(Bearer|Basic)(\s+)([^\s&;,'\"]+)")


def sanitize_error_summary(message: str) -> str:
    """例外メッセージから既知のcredential値を伏せる(Issue #59)。

    `str(exc)` をそのまま出すと、データ提供元ライブラリが例外文言へ埋め込んだ
    リクエストURL・ヘッダ断片(`?token=...`、`Subscription-Key=...`、
    `Authorization: Bearer ...` 等)が、ログ・`ProviderDataError.error_summary`・
    例外メッセージの3箇所へそのまま流出する。

    **必ずtruncateより先に呼ぶこと。** 先にtruncateすると、切り詰めで壊れた
    credential断片が伏せられずに残る可能性がある。

    レスポンス本文全体のような任意文字列から秘密を完全一般に判定することはできない。
    本関数が保証するのは「**既知のcredential patternは必ず伏せる**」までであり、
    それ以上の一般的な秘密検出は行わない(過剰なマスクで障害切り分けを妨げないため)。
    """
    # スキーム付き(`Authorization: Bearer <value>`)を先に処理する。逆順にすると
    # key=value側が "Authorization: Bearer" までを伏せてしまい、後続の実値が
    # スキーム語を失って残る。
    sanitized = _AUTH_SCHEME_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", message)
    return _KEY_VALUE_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", sanitized)


def build_error_summary(exc: Exception) -> str:
    """例外から安全な要約を組み立てる(sanitize → truncate の順)。"""
    return sanitize_error_summary(str(exc))[:_ERROR_SUMMARY_MAX_LENGTH]


def raise_provider_data_error(exc: Exception, *, provider_name: str, operation: str) -> NoReturn:
    """外部アクセス中の例外を分類・記録し、ProviderDataErrorとして送出する。

    provider側で1回だけ分類し、`retryable` を属性として持たせる。再試行層は
    `classify_provider_failure()` の isinstance 短絡でこの属性を読むだけなので、
    **分類ロジックは二重実装されない**。

    ログには provider / operation / failure_category / retryable / error_type /
    安全要約(sanitize済み・先頭200字)のみを出力する。APIキー・トークン・
    リクエストヘッダ・秘密情報を含むURL/クエリは**出力しない**。
    sanitize済みの同じ要約を logger・`ProviderDataError.error_summary`・例外メッセージの
    **3箇所すべて**へ渡す(ログだけ伏せて属性へ生の秘密を残さない)。

    元例外は `__cause__` に保持するが、ここでは `exc_info=True` を付けず
    **rawなtracebackを出力しない**。
    """
    retryable = classify_provider_failure(exc)
    category = (
        ProviderFailureCategory.RETRYABLE_PROVIDER_FAILURE
        if retryable
        else ProviderFailureCategory.NON_RETRYABLE_PROVIDER_FAILURE
    )
    error_type = type(exc).__name__
    error_summary = build_error_summary(exc)
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
