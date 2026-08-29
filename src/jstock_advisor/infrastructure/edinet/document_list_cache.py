"""EDINET書類一覧の日付単位キャッシュと共有ソース(Issue #53 Phase B1)。

EDINETの書類一覧API(documents.json?date=D)は「その日の全提出書類」を返す
日付単位APIであり、銘柄での絞り込みは呼び出し側で行う。従来は各finderが
銘柄ループの内側からこのAPIを直接呼んでいたため、同じ日付の同じ応答を
銘柄数だけ取得する構造になっていた(ウォッチリスト評価は最大300銘柄)。

本モジュールは取得責務を「日付単位の一覧取得(本モジュール)」と
「銘柄単位の抽出・派生結果の保存(各finder)」へ分離する。canonical identityは
`scan_date`(JST暦日)のみで、銘柄コードを一切含めない。

2層構成:
  L1  プロセス内メモ    : 同一Lambda実行内の重複取得・重複DynamoDB readを消す
  L2  DynamoDB共有cache : Lambdaプロセスを跨いで共有する
分散lease/single-flightは導入しない。同時cold missで数プロセスが同じ日付を
重複取得する可能性は許容する(Lambda並列度程度に有界。WatchlistWorkerの
ReservedConcurrentExecutionsは4)。保証するのは「銘柄数Nに比例してAPI呼び出しが
増えないこと」であり、「システム全体で必ず1回」ではない。

3つの「期限」は別概念であり混同しないこと:
  - refresh TTL(既定30分)   : refresh window内の日付の成功結果を再取得する間隔
  - negative TTL(既定5分)   : 取得失敗を再試行する間隔(障害時のstampede抑止)
  - physical retention(450日): DynamoDB itemの物理保持期間(下記参照)
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.scan_window import (
    DEFAULT_REFRESH_WINDOW_DAYS,
    validate_refresh_window_days,
)
from jstock_advisor.infrastructure.edinet.types import (
    EdinetDocumentEntry,
    EdinetDownloadResult,
    EdinetFailureReason,
    EdinetFetchStatus,
    EdinetListResult,
)

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_TTL_MINUTES = 30
DEFAULT_NEGATIVE_TTL_MINUTES = 5

# DynamoDB itemの物理保持期間。document_finderの初期lookbackが400暦日であるため、
# これより短くすると新規銘柄の追加時に古い日付を大量に再取得する構造へ戻る。
PHYSICAL_RETENTION_DAYS = 450
_RETENTION_SECONDS = PHYSICAL_RETENTION_DAYS * 24 * 60 * 60

# L2へ保存するJSONの上限(バイト)。DynamoDBのitemサイズ上限は400KBだが、
# `data`属性以外(キー・ttl)やUTF-8エンコード差分の余白を見て300KBを
# アプリケーション上限とする。超過時は切り詰めず、L2へ保存しない(下記_save参照)。
MAX_SERIALIZED_PAYLOAD_BYTES = 300 * 1024

_CACHE_FILE_NAME = "edinet_daily_document_list_cache.json"


class EdinetDailyDocumentListCache(BaseModel):
    """ある1日(JST暦日)の書類一覧の取得結果。identityはscan_dateのみ。"""

    model_config = ConfigDict(extra="forbid")

    scan_date: str
    fetch_status: EdinetFetchStatus
    failure_reason: EdinetFailureReason | None = None
    entries: list[EdinetDocumentEntry] = []
    entry_count: int = 0
    fetched_at: dt.datetime


class EdinetDailyDocumentListCacheRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[EdinetDailyDocumentListCache] = build_collection_store(
            EdinetDailyDocumentListCache,
            _CACHE_FILE_NAME,
            "scan_date",
            store_dir,
            ttl_seconds=_RETENTION_SECONDS,
        )

    def get(self, scan_date: dt.date) -> EdinetDailyDocumentListCache | None:
        return self._store.get(scan_date.isoformat())

    def save(self, cache: EdinetDailyDocumentListCache) -> None:
        self._store.upsert(cache)


@dataclass
class _MemoEntry:
    result: EdinetListResult
    fetched_at: dt.datetime


class EdinetDocumentSource:
    """finderが使うEDINETアクセス窓口(書類一覧はキャッシュ、ZIPは素通し)。

    finderが `EdinetClient.list_documents()` を直接呼ぶ構造を置き換える。
    プロセス内で1インスタンスを共有することでL1メモが効く(provider_factoryは
    disclosure/financial/dividendの3経路へ同一インスタンスを渡す)。
    """

    def __init__(
        self,
        client: EdinetClient | None = None,
        repository: EdinetDailyDocumentListCacheRepository | None = None,
        refresh_window_days: int = DEFAULT_REFRESH_WINDOW_DAYS,
        refresh_ttl_minutes: int = DEFAULT_REFRESH_TTL_MINUTES,
        negative_ttl_minutes: int = DEFAULT_NEGATIVE_TTL_MINUTES,
    ) -> None:
        self._client = client or EdinetClient()
        self._repo = repository or EdinetDailyDocumentListCacheRepository()
        self._refresh_window_days = validate_refresh_window_days(refresh_window_days)
        self._refresh_ttl_minutes = refresh_ttl_minutes
        self._negative_ttl_minutes = negative_ttl_minutes
        self._memo: dict[str, _MemoEntry] = {}

    @property
    def is_configured(self) -> bool:
        return self._client.is_configured

    @property
    def refresh_window_days(self) -> int:
        """refresh windowの正本。

        finderの走査開始日(compute_scan_start)と、日付単位キャッシュのfreshness判定は
        必ず同じ窓を使わなければならない。両者が食い違うと「finderは再走査するが、
        キャッシュ側は窓外として古い成功結果を永久にfresh扱いする」という破綻が起きる。
        そのためfinder側に独立した窓の設定は持たせず、この値を唯一の設定元とする。
        """
        return self._refresh_window_days

    def download_document_zip(self, doc_id: str) -> EdinetDownloadResult:
        """書類ZIPは書類単位(日付単位ではない)のためキャッシュせず素通しする。"""
        return self._client.download_document_zip(doc_id)

    def list_documents(self, scan_date: dt.date, now: dt.datetime) -> EdinetListResult:
        if not self._client.is_configured:
            # APIキー未設定は日付の性質ではなく実行環境の性質のため、L2へ保存しない。
            return EdinetListResult(
                EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.NOT_CONFIGURED
            )

        today = evaluation_date_jst(now)
        key = scan_date.isoformat()

        memo = self._memo.get(key)
        if memo is not None and self._is_fresh(
            memo.result.status, memo.fetched_at, scan_date, today, now
        ):
            return memo.result

        cached = self._repo.get(scan_date)
        if cached is not None and self._is_fresh(
            cached.fetch_status, cached.fetched_at, scan_date, today, now
        ):
            result = EdinetListResult(
                status=cached.fetch_status,
                entries=list(cached.entries),
                failure_reason=cached.failure_reason,
            )
            self._memo[key] = _MemoEntry(result, cached.fetched_at)
            return result

        result = self._client.list_documents(scan_date)
        self._memo[key] = _MemoEntry(result, now)
        self._save(scan_date, result, now)
        return result

    def _is_fresh(
        self,
        status: EdinetFetchStatus,
        fetched_at: dt.datetime,
        scan_date: dt.date,
        today: dt.date,
        now: dt.datetime,
    ) -> bool:
        age_minutes = (now - fetched_at).total_seconds() / 60
        if status is EdinetFetchStatus.FETCH_FAILED:
            return age_minutes <= self._negative_ttl_minutes
        if scan_date < today - dt.timedelta(days=self._refresh_window_days):
            # refresh windowより前の成功結果は確定として再取得しない。
            return True
        return age_minutes <= self._refresh_ttl_minutes

    def _save(self, scan_date: dt.date, result: EdinetListResult, now: dt.datetime) -> None:
        cache = EdinetDailyDocumentListCache(
            scan_date=scan_date.isoformat(),
            fetch_status=result.status,
            failure_reason=result.failure_reason,
            entries=list(result.entries),
            entry_count=len(result.entries),
            fetched_at=now,
        )
        serialized_bytes = len(cache.model_dump_json().encode("utf-8"))
        if serialized_bytes > MAX_SERIALIZED_PAYLOAD_BYTES:
            # 一部entriesだけを保存する(=黙ってデータを欠落させる)ことはしない。
            # L2への保存のみを見送り、今回取得した完全な結果はこのプロセスで
            # そのまま利用する(L1メモには保持済み)。
            logger.error(
                "edinet daily document list cache skipped (payload too large) "
                "scan_date=%s entry_count=%d serialized_bytes=%d limit_bytes=%d",
                cache.scan_date,
                cache.entry_count,
                serialized_bytes,
                MAX_SERIALIZED_PAYLOAD_BYTES,
            )
            return
        self._repo.save(cache)
