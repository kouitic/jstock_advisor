"""EDINET API v2 の薄いHTTPクライアント(要求仕様13節・21節)。

書類一覧取得・書類ダウンロードのみを担当する。APIキーは環境変数(EDINET_API_KEY)
から取得し、コード・ログには一切記録しない。認証情報が無い場合やAPI呼び出しに
失敗した場合は例外を投げずNone/空リストを返し、呼び出し側で「取得不可」として
扱えるようにする(推測で補完しないため)。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request

_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"


class EdinetClient:
    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("EDINET_API_KEY")

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def list_documents(self, date: dt.date) -> list[dict[str, object]]:
        """指定日に提出された書類の一覧を取得する。取得できなければ空リスト。"""
        if not self._api_key:
            return []
        params = urllib.parse.urlencode(
            {"date": date.isoformat(), "type": "2", "Subscription-Key": self._api_key}
        )
        url = f"{_BASE_URL}/documents.json?{params}"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return []
        results = body.get("results")
        return list(results) if isinstance(results, list) else []

    def download_document_zip(self, doc_id: str) -> bytes | None:
        """書類のCSV同梱ZIP(type=5)をダウンロードする。取得できなければNone。"""
        if not self._api_key:
            return None
        params = urllib.parse.urlencode({"type": "5", "Subscription-Key": self._api_key})
        url = f"{_BASE_URL}/documents/{doc_id}?{params}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
                data: bytes = resp.read()
                return data
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
