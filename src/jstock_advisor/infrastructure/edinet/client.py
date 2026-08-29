"""EDINET API v2 の薄いHTTPクライアント(要求仕様13節・21節)。

書類一覧取得・書類ダウンロードのみを担当する。APIキーは環境変数(EDINET_API_KEY)
から取得し、コード・ログには一切記録しない。

Issue #53 Phase B1: 認証情報が無い場合・API呼び出しに失敗した場合も例外を投げず、
`EdinetFetchStatus.FETCH_FAILED` と失敗理由を持つ結果オブジェクトを返す
(従来は `[]`/`None` へ潰しており、「取得できて0件」と区別できなかった)。
例外を伝播させない方針自体は従来どおり(1銘柄の障害で銘柄ループ全体を
止めないため)。

書類一覧は本システムが参照する6種別(types.CACHED_DOC_TYPE_CODES)かつ
secCodeを持つ書類だけへ射影して返す。日付単位キャッシュ(document_list_cache)
のitemサイズを抑えるためであり、射影対象はfinder2つの必要項目の和集合。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from jstock_advisor.infrastructure.edinet.types import (
    CACHED_DOC_TYPE_CODES,
    EdinetDocumentEntry,
    EdinetDownloadResult,
    EdinetFailureReason,
    EdinetFetchStatus,
    EdinetListResult,
)

_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"

_NOT_CONFIGURED_LIST_RESULT = EdinetListResult(
    status=EdinetFetchStatus.FETCH_FAILED,
    entries=[],
    failure_reason=EdinetFailureReason.NOT_CONFIGURED,
)
_NOT_CONFIGURED_DOWNLOAD_RESULT = EdinetDownloadResult(
    status=EdinetFetchStatus.FETCH_FAILED,
    payload=None,
    failure_reason=EdinetFailureReason.NOT_CONFIGURED,
)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _to_entry(raw: object) -> EdinetDocumentEntry | None:
    """書類一覧の1件を射影する。対象外・必須項目欠損はNone(=保存しない)。"""
    if not isinstance(raw, dict):
        return None
    sec_code = _optional_str(raw.get("secCode"))
    doc_id = _optional_str(raw.get("docID"))
    doc_type_code = _optional_str(raw.get("docTypeCode"))
    if sec_code is None or doc_id is None or doc_type_code is None:
        return None
    if doc_type_code not in CACHED_DOC_TYPE_CODES:
        return None
    return EdinetDocumentEntry(
        sec_code=sec_code,
        doc_id=doc_id,
        doc_type_code=doc_type_code,
        submit_date_time=_optional_str(raw.get("submitDateTime")),
        period_end=_optional_str(raw.get("periodEnd")),
        filer_name=_optional_str(raw.get("filerName")),
    )


class EdinetClient:
    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("EDINET_API_KEY")

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def list_documents(self, date: dt.date) -> EdinetListResult:
        """指定日(JST暦日)に提出された書類の一覧を取得する。"""
        if not self._api_key:
            return _NOT_CONFIGURED_LIST_RESULT
        params = urllib.parse.urlencode(
            {"date": date.isoformat(), "type": "2", "Subscription-Key": self._api_key}
        )
        url = f"{_BASE_URL}/documents.json?{params}"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
        except TimeoutError:
            return EdinetListResult(
                EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.TIMEOUT
            )
        except urllib.error.URLError:
            return EdinetListResult(
                EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.HTTP_ERROR
            )
        except ValueError:
            return EdinetListResult(
                EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.PARSE_ERROR
            )
        except OSError:
            return EdinetListResult(EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.OTHER)

        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            # resultsが無い/型が異なる応答は「0件」ではなく応答形式の異常として扱う。
            return EdinetListResult(
                EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.PARSE_ERROR
            )
        entries = [entry for entry in (_to_entry(raw) for raw in results) if entry is not None]
        status = (
            EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS
            if entries
            else EdinetFetchStatus.SUCCESS_EMPTY
        )
        return EdinetListResult(status, entries)

    def download_document_zip(self, doc_id: str) -> EdinetDownloadResult:
        """書類のCSV同梱ZIP(type=5)をダウンロードする。"""
        if not self._api_key:
            return _NOT_CONFIGURED_DOWNLOAD_RESULT
        params = urllib.parse.urlencode({"type": "5", "Subscription-Key": self._api_key})
        url = f"{_BASE_URL}/documents/{doc_id}?{params}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
                data: bytes = resp.read()
        except TimeoutError:
            return EdinetDownloadResult(
                EdinetFetchStatus.FETCH_FAILED, None, EdinetFailureReason.TIMEOUT
            )
        except (urllib.error.URLError, OSError):
            return EdinetDownloadResult(
                EdinetFetchStatus.FETCH_FAILED, None, EdinetFailureReason.DOWNLOAD_ERROR
            )
        if not data:
            return EdinetDownloadResult(
                EdinetFetchStatus.FETCH_FAILED, None, EdinetFailureReason.DOWNLOAD_ERROR
            )
        return EdinetDownloadResult(EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS, data)
