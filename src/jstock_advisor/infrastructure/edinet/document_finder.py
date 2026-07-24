"""EDINET書類検索(証券コード指定)+ローカルキャッシュ。

書類一覧APIは日付単位でしか検索できないため、対象銘柄の直近の有価証券報告書・
半期報告書を見つけるには過去日を遡ってスキャンする必要がある。毎回全期間を
スキャンすると呼び出し回数が膨大になるため、発見済みの書類と最終スキャン日を
ローカルにキャッシュし、次回以降は未スキャン分のみを検索する。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore

_ANNUAL_DOC_TYPE_CODES = {"120", "130"}  # 有価証券報告書・訂正有価証券報告書
_SEMIANNUAL_DOC_TYPE_CODES = {"160", "170"}  # 半期報告書・訂正半期報告書
_DEFAULT_INITIAL_LOOKBACK_DAYS = 400


class EdinetFilingCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_code: str
    latest_annual_doc_id: str | None = None
    latest_annual_period_end: str | None = None
    latest_semiannual_doc_id: str | None = None
    latest_semiannual_period_end: str | None = None
    oldest_scanned_date: str
    newest_scanned_date: str
    updated_at: dt.datetime


class EdinetFilingCacheRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: JsonCollectionStore[EdinetFilingCache] = JsonCollectionStore(
            EdinetFilingCache, "edinet_filing_cache.json", "stock_code", store_dir
        )

    def get(self, stock_code: str) -> EdinetFilingCache | None:
        return self._store.get(stock_code)

    def save(self, cache: EdinetFilingCache) -> None:
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


def find_latest_filings(
    client: EdinetClient,
    cache_repo: EdinetFilingCacheRepository,
    stock_code: str,
    now: dt.datetime,
    initial_lookback_days: int = _DEFAULT_INITIAL_LOOKBACK_DAYS,
) -> EdinetFilingCache | None:
    """対象銘柄の直近の有価証券報告書・半期報告書docIDをキャッシュ込みで取得する。

    EDINETが設定されていない(APIキー未設定)場合はNoneを返す。
    """
    if not client.is_configured:
        return None

    sec_code = _sec_code5(stock_code)
    today = now.date()
    cache = cache_repo.get(stock_code)

    annual_doc_id = cache.latest_annual_doc_id if cache else None
    annual_period_end = cache.latest_annual_period_end if cache else None
    semiannual_doc_id = cache.latest_semiannual_doc_id if cache else None
    semiannual_period_end = cache.latest_semiannual_period_end if cache else None

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
            doc_type = entry.get("docTypeCode")
            period_end = entry.get("periodEnd")
            doc_id = entry.get("docID")
            if not isinstance(period_end, str) or not isinstance(doc_id, str):
                continue
            if doc_type in _ANNUAL_DOC_TYPE_CODES and (
                annual_period_end is None or period_end > annual_period_end
            ):
                annual_doc_id, annual_period_end = doc_id, period_end
            elif doc_type in _SEMIANNUAL_DOC_TYPE_CODES and (
                semiannual_period_end is None or period_end > semiannual_period_end
            ):
                semiannual_doc_id, semiannual_period_end = doc_id, period_end

    updated_cache = EdinetFilingCache(
        stock_code=stock_code,
        latest_annual_doc_id=annual_doc_id,
        latest_annual_period_end=annual_period_end,
        latest_semiannual_doc_id=semiannual_doc_id,
        latest_semiannual_period_end=semiannual_period_end,
        oldest_scanned_date=oldest_scanned,
        newest_scanned_date=today.isoformat(),
        updated_at=now,
    )
    cache_repo.save(updated_cache)
    return updated_cache
