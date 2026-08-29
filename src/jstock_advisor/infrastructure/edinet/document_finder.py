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

from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.infrastructure.edinet.document_list_cache import EdinetDocumentSource
from jstock_advisor.infrastructure.edinet.scan_window import (
    DEFAULT_REFRESH_WINDOW_DAYS,
    advance_newest_scanned,
    business_days_between,
    compute_scan_start,
)

_ANNUAL_DOC_TYPE_CODES = {"120", "130"}  # 有価証券報告書・訂正有価証券報告書
_SEMIANNUAL_DOC_TYPE_CODES = {"160", "170"}  # 半期報告書・訂正半期報告書
_DEFAULT_INITIAL_LOOKBACK_DAYS = 400


class EdinetFilingCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_code: str
    filer_name: str | None = None  # EDINET提出書類のfilerName(日本語の正式社名)
    latest_annual_doc_id: str | None = None
    latest_annual_period_end: str | None = None
    latest_semiannual_doc_id: str | None = None
    latest_semiannual_period_end: str | None = None
    oldest_scanned_date: str
    newest_scanned_date: str
    updated_at: dt.datetime


class EdinetFilingCacheRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[EdinetFilingCache] = build_collection_store(
            EdinetFilingCache, "edinet_filing_cache.json", "stock_code", store_dir
        )

    def get(self, stock_code: str) -> EdinetFilingCache | None:
        return self._store.get(stock_code)

    def save(self, cache: EdinetFilingCache) -> None:
        self._store.upsert(cache)


def _sec_code5(stock_code: str) -> str:
    """4桁の証券コードをEDINETのsecCode(5桁、末尾チェックデジット0)に変換する。"""
    return f"{stock_code}0"


def find_latest_filings(
    source: EdinetDocumentSource,
    cache_repo: EdinetFilingCacheRepository,
    stock_code: str,
    now: dt.datetime,
    initial_lookback_days: int = _DEFAULT_INITIAL_LOOKBACK_DAYS,
    refresh_window_days: int = DEFAULT_REFRESH_WINDOW_DAYS,
) -> EdinetFilingCache | None:
    """対象銘柄の直近の有価証券報告書・半期報告書docIDをキャッシュ込みで取得する。

    EDINETが設定されていない(APIキー未設定)場合はNoneを返す。

    Issue #53 Phase B1で、disclosure_finderと同じ規約へ揃えた(詳細は
    disclosure_finder.find_extraordinary_reports のdocstring参照)。
      - 走査対象日をJST暦日で決める(UTC暦日だと朝バッチが常に前日扱いになる)
      - 当日+refresh_window_days暦日は毎回再走査する
      - 取得に失敗した日を走査済みとしない(連続成功範囲までしか前進させない)
      - 書類一覧の取得は日付単位キャッシュ(EdinetDocumentSource)経由で行い、
        銘柄ごとに同じ日付のdocuments.jsonを取得しない
    """
    if not source.is_configured:
        return None

    sec_code = _sec_code5(stock_code)
    today = evaluation_date_jst(now)
    cache = cache_repo.get(stock_code)

    filer_name = cache.filer_name if cache else None
    annual_doc_id = cache.latest_annual_doc_id if cache else None
    annual_period_end = cache.latest_annual_period_end if cache else None
    semiannual_doc_id = cache.latest_semiannual_doc_id if cache else None
    semiannual_period_end = cache.latest_semiannual_period_end if cache else None

    previous_newest = (
        dt.date.fromisoformat(cache.newest_scanned_date) if cache is not None else None
    )
    scan_start = compute_scan_start(
        today, previous_newest, initial_lookback_days, refresh_window_days
    )
    oldest_scanned = cache.oldest_scanned_date if cache is not None else scan_start.isoformat()

    last_complete: dt.date | None = None
    gap_found = False
    for scan_date in business_days_between(scan_start, today):
        result = source.list_documents(scan_date, now)
        for entry in result.entries:
            if entry.sec_code != sec_code:
                continue
            if filer_name is None and entry.filer_name:
                filer_name = entry.filer_name

            if entry.period_end is None:
                continue
            if entry.doc_type_code in _ANNUAL_DOC_TYPE_CODES and (
                annual_period_end is None or entry.period_end > annual_period_end
            ):
                annual_doc_id, annual_period_end = entry.doc_id, entry.period_end
            elif entry.doc_type_code in _SEMIANNUAL_DOC_TYPE_CODES and (
                semiannual_period_end is None or entry.period_end > semiannual_period_end
            ):
                semiannual_doc_id, semiannual_period_end = entry.doc_id, entry.period_end

        if not result.succeeded:
            gap_found = True
        elif not gap_found:
            last_complete = scan_date

    newest_scanned = advance_newest_scanned(previous_newest, last_complete)
    if newest_scanned is None:
        # 初回走査で1日も完了しなかった場合は走査済みの記録を作らない
        # (作ると、その範囲が二度と走査されないcache poisoningになる)。
        return None

    updated_cache = EdinetFilingCache(
        stock_code=stock_code,
        filer_name=filer_name,
        latest_annual_doc_id=annual_doc_id,
        latest_annual_period_end=annual_period_end,
        latest_semiannual_doc_id=semiannual_doc_id,
        latest_semiannual_period_end=semiannual_period_end,
        oldest_scanned_date=oldest_scanned,
        newest_scanned_date=newest_scanned.isoformat(),
        updated_at=now,
    )
    cache_repo.save(updated_cache)
    return updated_cache
