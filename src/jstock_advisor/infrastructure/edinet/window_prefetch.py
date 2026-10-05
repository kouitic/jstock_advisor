"""BuyCandidates dispatcherによるEDINET書類一覧の事前取得(Issue #818 案B)。

背景(事実。Issue #818のコード面の整理・Phase B設計):
  毎営業日の08:00、各銘柄の臨時報告書の走査は「今日と直前7暦日の平日」(通常6日)の
  書類一覧を必要とし、30分を超えて古い日はEDINETから取り直される。fan-outは銘柄ごとの
  非同期invokeで、cache(`document_list_cache.py`)にはsingle-flightが無いため、最初の
  数秒に多数のcoldなプロセスが同じ日付を重複して取得しうる。さらに、6日のうち1日でも
  取得に失敗すると、その日を走査する全銘柄がTEMPORARY_FAILURE(DATA_INSUFFICIENT)になる。

本moduleは、fan-outを始める前にdispatcher(1プロセス)が窓内の日付をあらかじめ取得して
L2(共有cache)を新しくする。子は新しい成功cacheを再利用する(日付あたりの取得を1回に近づける)。

不変条件(Issue #53のfail-safeを弱めない):
  - 事前取得は**成功した結果だけ**を保存する。失敗は保存しない。保存すると、negative TTL
    (5分)の間、後続の全プロセスがその失敗を再利用してしまい、現状(各プロセスが独立に
    取得を試みる)より悪くなる。保存しなければ、後続は従来どおり自分で取得を試みる。
  - 後続の取得が失敗すれば、従来どおりFETCH_FAILEDのまま判定側へ伝わる
    (取得失敗を「開示なし」として通さない)。本moduleは判定・失敗の扱いに触れない。
  - fail-soft: どんな例外・タイムアウトでも、呼び出し元(dispatcher)のfan-outを止めない。

時間の上限(何を保証し、何を保証しないか):
  - 保証する: 事前取得の全体が`DEFAULT_BUDGET_SECONDS`(120秒)を超えたら、新しい日付の取得を
    始めない。EDINETへの1回の取得は最大15秒(`EdinetClient`のurlopen timeout)なので、EDINETの
    呼び出しに費やす時間は、最悪でも`DEFAULT_BUDGET_SECONDS` + 15秒(= 135秒)である。
  - 保証しない: L2(DynamoDB)の日付ごとの読み(GetItem)と保存(PutItem)の時間は、この上限に
    含まれない。repoは`botocore.config.Config`を指定しておらず(src全体の検索)、
    boto3の既定(connect / read timeoutは各60秒、DynamoDBのlegacy retryは最大10回)に従う。
    したがって135秒は「EDINET側の上限」であり、事前取得全体の絶対上限ではない。
  - 絶対上限: dispatcherのLambda Timeout(`infra/template.yaml`のBuyCandidatesFunction。900秒)。
    なお、dispatcherはfan-outの前に`start_batch`などでDynamoDBを既に使っており、DynamoDBが
    応答しない状況ではfan-out自体が成立しない。事前取得がDynamoDBについて新たに加えるリスクは
    限定的である。
  - テスト(`tests/unit/test_edinet_window_prefetch.py`)は、予算 + clientのtimeoutが、templateの
    Timeoutの1/4未満であることを、templateの実値から固定する(template側でTimeoutを下げると
    落ちる)。

log: 件数と種別のみ。銘柄コード・書類の内容は出さない(そもそも銘柄を扱わない)。
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

logger = logging.getLogger(__name__)
# Issue #413: INFO を CloudWatch Logs へ出力する(Lambda の root logger の既定は WARNING で、
# module が宣言しないと INFO は出ない)。出力する値は件数と失敗の種別のみで、銘柄コード・
# 書類の内容・所有者・holding_id を含まない(本 module は銘柄を扱わない。#135 / #416)。
# PII の確認は PR に記録した。
logger.setLevel(logging.INFO)

#: 事前取得の全体の時間の上限(秒)。これを超えたら新しい日付の取得を始めない。
DEFAULT_BUDGET_SECONDS = 120.0


@dataclass(frozen=True)
class PrefetchSummary:
    """事前取得の結果(件数のみ)。"""

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
) -> PrefetchSummary:
    """窓内の各日付の書類一覧を、必要なものだけ1回ずつ取得してL2へ保存する。

    例外は呼び出し元へ送出する(fail-softの捕捉は`prefetch_recent_document_lists_safely`)。
    """
    started = clock()
    dates = prefetch_dates(evaluation_date_jst(now), source.refresh_window_days)
    fetched = 0
    fresh = 0
    failed = 0
    skipped = 0
    reasons: Counter[str] = Counter()
    exceeded = False
    for index, scan_date in enumerate(dates):
        if clock() - started >= budget_seconds:
            exceeded = True
            skipped = len(dates) - index
            break
        outcome = source.prefetch_success_only(scan_date, now)
        if outcome is None:
            # APIキー未設定(呼び出し側でも確認するが、二重に守る)。何もしない。
            return PrefetchSummary(configured=False, target_dates=len(dates))
        result, called = outcome
        if not result.succeeded:
            failed += 1
            reasons[str(result.failure_reason or "OTHER")] += 1
        elif called:
            fetched += 1
        else:
            fresh += 1
    return PrefetchSummary(
        configured=True,
        target_dates=len(dates),
        fetched=fetched,
        already_fresh=fresh,
        failed=failed,
        failure_reasons=tuple(sorted(reasons.items())),
        skipped_by_budget=skipped,
        budget_exceeded=exceeded,
        elapsed_seconds=round(clock() - started, 3),
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
) -> PrefetchSummary:
    """fail-soft版。どんな例外でも送出せず、件数のみをlogに出して戻る。"""
    try:
        source = (source_factory or _default_source)()
        if source is None:
            logger.info("edinet window prefetch skipped (EDINET API key not configured)")
            return PrefetchSummary(configured=False)
        summary = prefetch_recent_document_lists(
            source, now, budget_seconds=budget_seconds, clock=clock
        )
    except Exception:  # noqa: BLE001 - dispatcherのfan-outを止めない(fail-soft)
        logger.warning("edinet window prefetch failed (fan-out continues)", exc_info=True)
        return PrefetchSummary(configured=True, error=True)
    logger.info(
        "edinet window prefetch dates=%d fetched=%d already_fresh=%d failed=%d "
        "failure_reasons=%s skipped_by_budget=%d budget_exceeded=%s elapsed=%.1fs",
        summary.target_dates,
        summary.fetched,
        summary.already_fresh,
        summary.failed,
        dict(summary.failure_reasons),
        summary.skipped_by_budget,
        summary.budget_exceeded,
        summary.elapsed_seconds,
    )
    return summary
