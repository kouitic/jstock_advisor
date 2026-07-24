"""EDINET臨時報告書(適時開示相当)の検索+ローカルキャッシュ。

document_finder.py(有価証券報告書・半期報告書向け)とは異なり、臨時報告書は
不定期に複数回提出されうるため「最新1件」ではなく「見つかった全件」を保持する。
書類本文のダウンロード・テキスト抽出は一度行えば再利用できるよう、抽出結果も
キャッシュに含める(実測検証: 臨時報告書のCSVには"提出理由"テキストブロックが
必ず含まれ、平易な日本語でなぜ提出されたかが記載されている)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.csv_parser import extract_main_document_rows
from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore

_EXTRAORDINARY_REPORT_DOC_TYPE_CODES = {"180", "190"}  # 臨時報告書・訂正臨時報告書
_DEFAULT_INITIAL_LOOKBACK_DAYS = 60
_MAX_SUMMARY_LENGTH = 2000  # 保存・通知メッセージが肥大化しないよう上限を設ける


class EdinetDisclosureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    submit_date: dt.date
    summary: str


class EdinetDisclosureCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_code: str
    records: list[EdinetDisclosureRecord] = []
    oldest_scanned_date: str
    newest_scanned_date: str
    updated_at: dt.datetime


class EdinetDisclosureCacheRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: JsonCollectionStore[EdinetDisclosureCache] = JsonCollectionStore(
            EdinetDisclosureCache, "edinet_disclosure_cache.json", "stock_code", store_dir
        )

    def get(self, stock_code: str) -> EdinetDisclosureCache | None:
        return self._store.get(stock_code)

    def save(self, cache: EdinetDisclosureCache) -> None:
        self._store.upsert(cache)


def _sec_code5(stock_code: str) -> str:
    """4桁の証券コードをEDINETのsecCode(5桁、末尾チェックデジット0)に変換する。"""
    return f"{stock_code}0"


def _business_days_between(start: dt.date, end: dt.date) -> list[dt.date]:
    days: list[dt.date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += dt.timedelta(days=1)
    return days


def _extract_reason_summary(zip_bytes: bytes) -> str | None:
    """本文CSVから、提出理由・報告内容のテキストブロックを結合して返す。

    表紙(会社名・住所等の定型情報)のテキストブロックは除外し、報告書固有の
    内容(なぜ提出したか・何が起きたか)のみを対象とする。
    """
    rows = extract_main_document_rows(zip_bytes)
    if rows is None:
        return None
    text_blocks = [
        r.value.strip()
        for r in rows
        if r.element_id.startswith("jpcrp-esr_cor:")
        and r.item_name.endswith("[テキストブロック]")
        and "表紙" not in r.item_name
        and r.value.strip()
    ]
    if not text_blocks:
        return None
    return " ".join(text_blocks)[:_MAX_SUMMARY_LENGTH]


def find_extraordinary_reports(
    client: EdinetClient,
    cache_repo: EdinetDisclosureCacheRepository,
    stock_code: str,
    now: dt.datetime,
    initial_lookback_days: int = _DEFAULT_INITIAL_LOOKBACK_DAYS,
) -> EdinetDisclosureCache | None:
    """対象銘柄の臨時報告書・訂正臨時報告書をキャッシュ込みで検索する。

    EDINETが設定されていない(APIキー未設定)場合はNoneを返す。
    """
    if not client.is_configured:
        return None

    sec_code = _sec_code5(stock_code)
    today = now.date()
    cache = cache_repo.get(stock_code)

    records = list(cache.records) if cache else []
    known_doc_ids = {r.doc_id for r in records}

    if cache is None:
        scan_dates = _business_days_between(today - dt.timedelta(days=initial_lookback_days), today)
        oldest_scanned = (today - dt.timedelta(days=initial_lookback_days)).isoformat()
    else:
        newest_scanned = dt.date.fromisoformat(cache.newest_scanned_date)
        if newest_scanned >= today:
            return cache
        scan_dates = _business_days_between(newest_scanned + dt.timedelta(days=1), today)
        oldest_scanned = cache.oldest_scanned_date

    for scan_date in scan_dates:
        for entry in client.list_documents(scan_date):
            if entry.get("secCode") != sec_code:
                continue
            if entry.get("docTypeCode") not in _EXTRAORDINARY_REPORT_DOC_TYPE_CODES:
                continue
            doc_id = entry.get("docID")
            submit_datetime = entry.get("submitDateTime")
            if not isinstance(doc_id, str) or doc_id in known_doc_ids:
                continue
            if not isinstance(submit_datetime, str):
                continue

            zip_bytes = client.download_document_zip(doc_id)
            if zip_bytes is None:
                continue
            summary = _extract_reason_summary(zip_bytes)
            if summary is None:
                continue

            try:
                submit_date = dt.datetime.strptime(submit_datetime, "%Y-%m-%d %H:%M").date()
            except ValueError:
                continue

            records.append(
                EdinetDisclosureRecord(doc_id=doc_id, submit_date=submit_date, summary=summary)
            )
            known_doc_ids.add(doc_id)

    updated_cache = EdinetDisclosureCache(
        stock_code=stock_code,
        records=records,
        oldest_scanned_date=oldest_scanned,
        newest_scanned_date=today.isoformat(),
        updated_at=now,
    )
    cache_repo.save(updated_cache)
    return updated_cache
