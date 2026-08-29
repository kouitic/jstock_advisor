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

from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.infrastructure.edinet.csv_parser import extract_main_document_rows
from jstock_advisor.infrastructure.edinet.document_list_cache import EdinetDocumentSource
from jstock_advisor.infrastructure.edinet.scan_window import (
    DEFAULT_REFRESH_WINDOW_DAYS,
    advance_newest_scanned,
    business_days_between,
    compute_scan_start,
)

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
        self._store: CollectionStore[EdinetDisclosureCache] = build_collection_store(
            EdinetDisclosureCache, "edinet_disclosure_cache.json", "stock_code", store_dir
        )

    def get(self, stock_code: str) -> EdinetDisclosureCache | None:
        return self._store.get(stock_code)

    def save(self, cache: EdinetDisclosureCache) -> None:
        self._store.upsert(cache)


def _sec_code5(stock_code: str) -> str:
    """4桁の証券コードをEDINETのsecCode(5桁、末尾チェックデジット0)に変換する。"""
    return f"{stock_code}0"


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
    source: EdinetDocumentSource,
    cache_repo: EdinetDisclosureCacheRepository,
    stock_code: str,
    now: dt.datetime,
    initial_lookback_days: int = _DEFAULT_INITIAL_LOOKBACK_DAYS,
    refresh_window_days: int = DEFAULT_REFRESH_WINDOW_DAYS,
) -> EdinetDisclosureCache | None:
    """対象銘柄の臨時報告書・訂正臨時報告書をキャッシュ込みで検索する。

    EDINETが設定されていない(APIキー未設定)場合はNoneを返す。

    Issue #53 Phase B1で以下を修正した。
      - 走査対象日をJST暦日(evaluation_date_jst)で決める。now.date()(UTC暦日)を
        使っていたため、JST 00:00〜08:59に起動する朝バッチでは常に前日扱いとなり、
        `newest_scanned >= today`が成立してEDINETを一度も呼ばずにキャッシュを
        返していた(domain/jst.pyの「now.date()を直接呼ばない」規約違反)。
      - 直近(当日+refresh_window_days暦日)は毎回再走査する。日付単位の
        `newest_scanned_date`だけでは「その日の何時時点まで見たか」を表現できず、
        10:00の走査で当日を走査済みと記録すると、同じ日の引け後(15:00-17:00)に
        提出された臨時報告書を永久に取得できなかったため。doc_idで重複排除して
        いるので再走査は冪等。
      - 取得に失敗した日を走査済みとしない。連続して成功した範囲の末尾までしか
        `newest_scanned_date`を前進させない(失敗日を走査済みにすると、その営業日は
        二度と走査されずリスク開示を恒久的に見落とす)。書類ZIPの取得失敗も
        その日を未完了として扱う。
    """
    if not source.is_configured:
        return None

    sec_code = _sec_code5(stock_code)
    today = evaluation_date_jst(now)
    cache = cache_repo.get(stock_code)

    records = list(cache.records) if cache else []
    known_doc_ids = {r.doc_id for r in records}

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
        date_complete = result.succeeded
        for entry in result.entries:
            if entry.sec_code != sec_code:
                continue
            if entry.doc_type_code not in _EXTRAORDINARY_REPORT_DOC_TYPE_CODES:
                continue
            if entry.doc_id in known_doc_ids or entry.submit_date_time is None:
                continue

            download = source.download_document_zip(entry.doc_id)
            if not download.succeeded or download.payload is None:
                # ZIP取得失敗はこの日の走査が未完了であることを意味する
                # (この書類を取り込めていないため、走査済みにしてはならない)。
                date_complete = False
                continue
            summary = _extract_reason_summary(download.payload)
            if summary is None:
                # 取得は成功しており、抽出対象のテキストブロックが無かっただけ。
                # 再走査しても結果は変わらないため未完了とはしない。
                continue

            try:
                submit_date = dt.datetime.strptime(
                    entry.submit_date_time, "%Y-%m-%d %H:%M"
                ).date()
            except ValueError:
                continue

            records.append(
                EdinetDisclosureRecord(
                    doc_id=entry.doc_id, submit_date=submit_date, summary=summary
                )
            )
            known_doc_ids.add(entry.doc_id)

        if not date_complete:
            gap_found = True
        elif not gap_found:
            last_complete = scan_date

    newest_scanned = advance_newest_scanned(previous_newest, last_complete)
    if newest_scanned is None:
        # 初回走査で1日も完了しなかった場合。走査済みの記録を作らない
        # (作ると、その範囲が二度と走査されないcache poisoningになる)。
        return None

    updated_cache = EdinetDisclosureCache(
        stock_code=stock_code,
        records=records,
        oldest_scanned_date=oldest_scanned,
        newest_scanned_date=newest_scanned.isoformat(),
        updated_at=now,
    )
    cache_repo.save(updated_cache)
    return updated_cache
