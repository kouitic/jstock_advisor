"""外部データ取得の失敗を表す共通例外(Issue #59 Phase B1)。

**原則: 外部データ取得の失敗は、retryable / non-retryable にかかわらず、
正常な欠測値(None / [] / 0)へ変換しない。**

区別すべきは2軸であり、retryable は「失敗かどうか」ではなく
「その失敗を再試行するか」という属性として扱う。

  SUCCESS + value    : 値
  SUCCESS + zero     : 真の0(無配・イベント0件)
  SUCCESS + empty    : 応答は正常だが行が空
  SUCCESS + missing  : 項目が未公表・存在しない → 既存contractどおり None / [] / 0
  FAILURE            : 外部アクセス中の例外 → ProviderDataError を送出(値へ変換しない)

`services/provider_failure_classifier.py`(分類)と `providers/_failure.py`
(分類+ログ+送出)の双方から参照されるため、循環importを避けてinterfaces層へ置く。
domain/service層はこの例外型のみを知り、yfinance等のライブラリ固有例外型へは依存しない。
"""

from __future__ import annotations

from enum import StrEnum


class ProviderFailureCategory(StrEnum):
    """失敗の区分(運用上の切り分け用)。

    RETRYABLE_PROVIDER_FAILURE はデータ提供元側の一過性障害の疑い
    (429/403/5xx・タイムアウト・接続断・提供元固有のレート制限等)。
    それ以外は再試行しても結果が変わらないと判断されるもの。
    """

    RETRYABLE_PROVIDER_FAILURE = "RETRYABLE_PROVIDER_FAILURE"
    NON_RETRYABLE_PROVIDER_FAILURE = "NON_RETRYABLE_PROVIDER_FAILURE"


class ProviderDataError(Exception):
    """外部データ取得の失敗を表す薄い共通例外。

    original例外は `raise ProviderDataError(...) from exc` により `__cause__` へ保持する。
    **domain値は保持しない**(値の有無・欠測の意味は呼び出し側の契約)。

    メッセージには当方が構築した安全要約のみを載せる。APIキー・トークン・
    リクエストヘッダ・レスポンス本文・PII・秘密情報を含むURL/クエリは**載せない**。
    """

    def __init__(
        self,
        provider_name: str,
        operation: str,
        retryable: bool,
        failure_category: ProviderFailureCategory,
        error_type: str,
        error_summary: str,
    ) -> None:
        self.provider_name = provider_name
        self.operation = operation
        self.retryable = retryable
        self.failure_category = failure_category
        self.error_type = error_type
        self.error_summary = error_summary
        super().__init__(
            f"provider={provider_name} operation={operation} "
            f"retryable={retryable} category={failure_category.value} "
            f"error_type={error_type} error_summary={error_summary}"
        )
