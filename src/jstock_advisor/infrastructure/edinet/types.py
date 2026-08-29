"""EDINET取得結果の型(Issue #53 Phase B1)。

「取得できて0件だった」と「取得に失敗した」を型で区別するために導入する。
従来は EdinetClient が例外・APIキー未設定・HTTPエラーをすべて `[]`/`None` へ
潰しており、呼び出し側が両者を区別できなかった(リスク開示ゼロと取得失敗が
同義になっていた)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EdinetFetchStatus(StrEnum):
    """EDINET取得の結果区分。

    書類一覧(list_documents)は3値すべてを使う。書類ダウンロード
    (download_document_zip)は SUCCESS_WITH_DOCUMENTS / FETCH_FAILED のみを使う
    (0バイト応答は実質的に利用不能なため FETCH_FAILED として扱う)。
    """

    SUCCESS_WITH_DOCUMENTS = "SUCCESS_WITH_DOCUMENTS"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    FETCH_FAILED = "FETCH_FAILED"


class EdinetFailureReason(StrEnum):
    """FETCH_FAILED の内訳(運用上の切り分け用)。

    NOT_CONFIGURED(APIキー未設定=恒久的な設定不備)と TIMEOUT/HTTP_ERROR
    (一過性)は運用対応が異なるため区別する。判定側の扱い(取得不能)は同じ。
    """

    NOT_CONFIGURED = "NOT_CONFIGURED"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"
    OTHER = "OTHER"


# 日付単位キャッシュへ保存する書類種別(有報120/訂正有報130/半期160/訂正半期170/
# 臨時190・180)。EDINETの1日分の書類一覧には大量保有報告書等が多数含まれるが、
# 本システムの2つのfinder(document_finder/disclosure_finder)はこの6種しか
# 参照しないため、保存前に射影してitemサイズを抑える。**新しい書類種別を使う
# 実装を追加する場合は必ずこの定数へ追加すること**(ここに無い種別はキャッシュ
# 経由では一切見えない)。
CACHED_DOC_TYPE_CODES: frozenset[str] = frozenset({"120", "130", "160", "170", "180", "190"})


class EdinetDocumentEntry(BaseModel):
    """書類一覧APIの1件を、2つのfinderが必要とする項目だけへ射影したもの。

    disclosure_finder: sec_code / doc_type_code / doc_id / submit_date_time
    document_finder:   sec_code / doc_type_code / doc_id / period_end / filer_name
    """

    model_config = ConfigDict(extra="forbid")

    sec_code: str
    doc_id: str
    doc_type_code: str
    submit_date_time: str | None = None
    period_end: str | None = None
    filer_name: str | None = None


@dataclass(frozen=True)
class EdinetListResult:
    """書類一覧の取得結果。FETCH_FAILED時のentriesは必ず空。"""

    status: EdinetFetchStatus
    entries: list[EdinetDocumentEntry]
    failure_reason: EdinetFailureReason | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is not EdinetFetchStatus.FETCH_FAILED


@dataclass(frozen=True)
class EdinetDownloadResult:
    """書類ZIPのダウンロード結果。FETCH_FAILED時のpayloadは必ずNone。"""

    status: EdinetFetchStatus
    payload: bytes | None = None
    failure_reason: EdinetFailureReason | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is not EdinetFetchStatus.FETCH_FAILED
