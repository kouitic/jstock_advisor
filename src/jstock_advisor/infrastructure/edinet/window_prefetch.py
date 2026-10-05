"""BuyCandidates dispatcherによるEDINET書類一覧の事前取得(Issue #818 案B・F限定版・I)。

背景(事実。Issue #818のコード面の整理・Phase B設計):
  毎営業日の08:00、各銘柄の臨時報告書の走査は「今日と直前7暦日の平日」(通常6日)の
  書類一覧を必要とし、30分を超えて古い日はEDINETから取り直される。fan-outは銘柄ごとの
  非同期invokeで、cache(`document_list_cache.py`)にはsingle-flightが無いため、最初の
  数秒に多数のcoldなプロセスが同じ日付を重複して取得しうる。さらに、6日のうち1日でも
  取得に失敗すると、その日を走査する全銘柄がTEMPORARY_FAILURE(DATA_INSUFFICIENT)になる。

本moduleは、fan-outを始める前にdispatcher(1プロセス)が窓内の日付をあらかじめ取得して
L2(共有cache)を新しくする。子は新しい成功cacheを再利用する(日付あたりの取得を1回に近づける)。

F限定版(再試行): 取得が**TIMEOUT**で失敗した日付だけを、このmoduleの中で再試行する
(1日付あたり最大`MAX_ATTEMPTS_PER_DATE`回。試行の間に`RETRY_WAIT_SECONDS`の待ち)。
再試行するのはdispatcher 1プロセスだけで、同時接続を増やさない。TIMEOUT以外の失敗
(HTTP_ERROR・PARSE_ERROR・OTHER)は再試行しない(HTTP_ERRORはHTTPの状態を区別できず、
恒久的な失敗〔認証・権限など〕を繰り返さないため)。`EdinetClient`のtimeout(15秒)は変えない。

I(log): 日付ごとに、結果・試行回数・所要秒・失敗の種別をlogに出す(秒と件数のみ)。

不変条件(Issue #53のfail-safeを弱めない):
  - 事前取得は**成功した結果だけ**を保存する。失敗は保存しない。保存すると、negative TTL
    (5分)の間、後続の全プロセスがその失敗を再利用してしまい、現状(各プロセスが独立に
    取得を試みる)より悪くなる。保存しなければ、後続は従来どおり自分で取得を試みる。
  - 再試行しても最後まで失敗した日付は、FETCH_FAILEDのまま(何も保存しない)。後続の取得が
    失敗すれば、従来どおりFETCH_FAILEDのまま判定側へ伝わる(取得失敗を「開示なし」として
    通さない)。本moduleは判定・失敗の扱いに触れない。
  - fail-soft: どんな例外・タイムアウトでも、呼び出し元(dispatcher)のfan-outを止めない。

時間の上限(何を保証し、何を保証しないか):
  - 保証する: 試行を始める条件は「経過時間 + 1回の取得の想定最大(`ASSUMED_CLIENT_TIMEOUT_SECONDS`
    = 15秒。再試行の待ちを含む)が`DEFAULT_BUDGET_SECONDS`(120秒)以内」である。満たさない
    なら、新しい日付も再試行も始めない。したがって、EDINETの呼び出しに費やす時間は、
    各試行が想定の最大(15秒)で終わる限り、120秒を超えない。
  - 保証しない: ① urllibのtimeoutは1回のソケット操作ごとの待ちで、応答が少しずつ届く場合の
    1回の取得の総時間は15秒を超えうる(未実測)。その場合、120秒を超えうるが、dispatcherの
    Timeoutの範囲(下)に収まる。② L2(DynamoDB)の日付ごとの読み(GetItem)と保存(PutItem)の
    時間は、この上限に含まれない。repoは`botocore.config.Config`を指定しておらず(src全体の
    検索)、boto3の既定(connect / read timeoutは各60秒、DynamoDBのlegacy retryは最大10回)に
    従う。
  - 絶対上限: dispatcherのLambda Timeout(`infra/template.yaml`のBuyCandidatesFunction。900秒)。
    なお、dispatcherはfan-outの前に`start_batch`などでDynamoDBを既に使っており、DynamoDBが
    応答しない状況ではfan-out自体が成立しない。事前取得がDynamoDBについて新たに加えるリスクは
    限定的である。
  - テスト(`tests/unit/test_edinet_window_prefetch.py`)は、予算 + clientのtimeoutが、templateの
    Timeoutの1/4未満であることを、templateの実値から固定する(template側でTimeoutを下げると
    落ちる)。

log: 件数・秒・失敗の種別のみ。銘柄コード・書類の内容は出さない(そもそも銘柄を扱わない)。
日付はビジネス日であり、識別子ではない。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.document_list_cache import EdinetDocumentSource
from jstock_advisor.infrastructure.edinet.scan_window import business_days_between
from jstock_advisor.infrastructure.edinet.types import EdinetFailureReason

logger = logging.getLogger(__name__)
# Issue #413: INFO を CloudWatch Logs へ出力する(Lambda の root logger の既定は WARNING で、
# module が宣言しないと INFO は出ない)。出力する値は件数・秒・失敗の種別のみで、銘柄コード・
# 書類の内容・所有者・holding_id を含まない(本 module は銘柄を扱わない。#135 / #416)。
# PII の確認は PR に記録した。
logger.setLevel(logging.INFO)

#: 事前取得の全体の時間の上限(秒)。試行を始める条件に使う(上のdocstring参照)。
DEFAULT_BUDGET_SECONDS = 120.0
#: 1回の取得の想定最大(秒)。`EdinetClient.list_documents`のurlopen timeoutと一致させる
#: (テストがclientのソースと結び付けて固定する)。
ASSUMED_CLIENT_TIMEOUT_SECONDS = 15.0
#: 1日付あたりの最大試行回数(初回 + TIMEOUT時の再試行)。
MAX_ATTEMPTS_PER_DATE = 3
#: TIMEOUTの再試行の前の待ち(秒)。
RETRY_WAIT_SECONDS = 2.0


@dataclass(frozen=True)
class DateResult:
    """1日付の事前取得の結果(件数と秒のみ)。

    outcomeは fetched / already_fresh / failed / skipped のいずれか。
    """

    scan_date: dt.date
    outcome: str
    attempts: int = 0
    elapsed_seconds: float = 0.0
    failure_reason: str | None = None


@dataclass(frozen=True)
class PrefetchSummary:
    """事前取得の結果(件数と秒のみ)。"""

    configured: bool
    target_dates: int = 0
    fetched: int = 0
    already_fresh: int = 0
    failed: int = 0
    failure_reasons: tuple[tuple[str, int], ...] = ()
    skipped_by_budget: int = 0
    budget_exceeded: bool = False
    elapsed_seconds: float = 0.0
    error: bool = False
    retries: int = 0
    recovered_by_retry: int = 0
    date_results: tuple[DateResult, ...] = ()


def prefetch_dates(today: dt.date, refresh_window_days: int) -> list[dt.date]:
    """事前取得の対象日 = 今日と直前`refresh_window_days`暦日の平日。

    銘柄の走査(`disclosure_finder`)は`compute_scan_start`により、どの銘柄でも
    `today - refresh_window_days`以前から今日までの平日を走査する。本関数はその最小の
    範囲(全銘柄が必ず通る日付)と同じ規則(`business_days_between`)で決める。
    """
    return business_days_between(today - dt.timedelta(days=refresh_window_days), today)


def prefetch_recent_document_lists(
    source: EdinetDocumentSource,
    now: dt.datetime,
    *,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] | None = None,
) -> PrefetchSummary:
    """窓内の各日付の書類一覧を、必要なものだけ取得してL2へ保存する。

    TIMEOUTで失敗した日付だけを、予算の内側で再試行する(上のdocstring参照)。
    例外は呼び出し元へ送出する(fail-softの捕捉は`prefetch_recent_document_lists_safely`)。
    """
    do_sleep = sleep or time.sleep  # 呼び出し時に解決する(テストがtime.sleepを差し替えられる)
    started = clock()
    dates = prefetch_dates(evaluation_date_jst(now), source.refresh_window_days)
    results: list[DateResult] = []
    reasons: Counter[str] = Counter()
    exceeded = False

    def within_budget(extra_wait: float = 0.0) -> bool:
        return (clock() - started) + extra_wait + ASSUMED_CLIENT_TIMEOUT_SECONDS <= budget_seconds

    for scan_date in dates:
        if not within_budget():
            exceeded = True
            results.append(DateResult(scan_date, "skipped"))
            continue
        date_started = clock()
        attempts = 0
        while True:
            attempts += 1
            outcome = source.prefetch_success_only(scan_date, now)
            if outcome is None:
                # APIキー未設定(呼び出し側でも確認するが、二重に守る)。何もしない。
                return PrefetchSummary(configured=False, target_dates=len(dates))
            result, called = outcome
            if result.succeeded or result.failure_reason is not EdinetFailureReason.TIMEOUT:
                break
            if attempts >= MAX_ATTEMPTS_PER_DATE:
                break
            if not within_budget(RETRY_WAIT_SECONDS):
                exceeded = True
                break
            do_sleep(RETRY_WAIT_SECONDS)
        elapsed = round(clock() - date_started, 3)
        if not result.succeeded:
            reason = str(result.failure_reason or "OTHER")
            reasons[reason] += 1
            results.append(DateResult(scan_date, "failed", attempts, elapsed, reason))
        elif called:
            results.append(DateResult(scan_date, "fetched", attempts, elapsed))
        else:
            results.append(DateResult(scan_date, "already_fresh", attempts, elapsed))

    return PrefetchSummary(
        configured=True,
        target_dates=len(dates),
        fetched=sum(1 for r in results if r.outcome == "fetched"),
        already_fresh=sum(1 for r in results if r.outcome == "already_fresh"),
        failed=sum(1 for r in results if r.outcome == "failed"),
        failure_reasons=tuple(sorted(reasons.items())),
        skipped_by_budget=sum(1 for r in results if r.outcome == "skipped"),
        budget_exceeded=exceeded,
        elapsed_seconds=round(clock() - started, 3),
        retries=sum(max(r.attempts - 1, 0) for r in results),
        recovered_by_retry=sum(1 for r in results if r.outcome == "fetched" and r.attempts > 1),
        date_results=tuple(results),
    )


def _default_source() -> EdinetDocumentSource | None:
    """APIキーが設定されている場合だけsourceを作る(未設定ならcache表にも触れない)。"""
    client = EdinetClient()
    if not client.is_configured:
        return None
    return EdinetDocumentSource(client=client)


def prefetch_recent_document_lists_safely(
    now: dt.datetime,
    *,
    source_factory: Callable[[], EdinetDocumentSource | None] | None = None,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] | None = None,
) -> PrefetchSummary:
    """fail-soft版。どんな例外でも送出せず、件数と秒のみをlogに出して戻る。"""
    try:
        source = (source_factory or _default_source)()
        if source is None:
            logger.info("edinet window prefetch skipped (EDINET API key not configured)")
            return PrefetchSummary(configured=False)
        summary = prefetch_recent_document_lists(
            source, now, budget_seconds=budget_seconds, clock=clock, sleep=sleep
        )
    except Exception:  # noqa: BLE001 - dispatcherのfan-outを止めない(fail-soft)
        logger.warning("edinet window prefetch failed (fan-out continues)", exc_info=True)
        return PrefetchSummary(configured=True, error=True)
    logger.info(
        "edinet window prefetch dates=%d fetched=%d already_fresh=%d failed=%d "
        "failure_reasons=%s skipped_by_budget=%d budget_exceeded=%s retries=%d "
        "recovered_by_retry=%d elapsed=%.1fs",
        summary.target_dates,
        summary.fetched,
        summary.already_fresh,
        summary.failed,
        dict(summary.failure_reasons),
        summary.skipped_by_budget,
        summary.budget_exceeded,
        summary.retries,
        summary.recovered_by_retry,
        summary.elapsed_seconds,
    )
    for item in summary.date_results:
        logger.info(
            "edinet window prefetch date=%s outcome=%s attempts=%d elapsed=%.1fs reason=%s",
            item.scan_date.isoformat(),
            item.outcome,
            item.attempts,
            item.elapsed_seconds,
            item.failure_reason or "-",
        )
    return summary
