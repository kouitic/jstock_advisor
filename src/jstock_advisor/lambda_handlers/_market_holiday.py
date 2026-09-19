"""市場休場日(JPX非営業日)に、市場依存のentryをskipする共通gate(Issue #440)。

## なぜ必要か

EventBridgeは月〜金のcronで起動し、祝日を考慮しない(9/21の敬老の日・9/22の国民の休日・
9/23の秋分の日のような平日の休場日にも発火する)。市場データ・当日売買を前提とする判定・
通知が、市場が閉まっている日に実行されると、直前営業日の終値を「今日の価格」として扱った判定や
通知が出てしまう。

## 方針(USER決定: #440 issuecomment-5741656160)

- 「休場日=全Lambdaをskip」にはしない。skipするのは市場依存の**3 entryのみ**
  (BuyCandidates parent / HoldingsWatchlist parent / WatchlistDispatcher NEW_CANDIDATE_SCREENING)。
  適時開示・評価・週次/月次/四半期レビュー・recovery・child・worker・reconciler等は止めない。
- 新しい営業日判定は作らない。`BusinessCalendar.is_business_day`(S-04)を再利用し、日付は
  `evaluation_date_jst(now)`(UTCの日付をそのまま使わない)で決める。
- ★ **validationを先に行う**。不正なevent・modeを「休場日だから」という理由で隠さない
  (INVALID EVENT + MARKET HOLIDAY = INVALID EVENT)。本module自身も、bypassフラグの検証を
  休場日の判定より先に行う。
- skipは「正常なno-op」。最初の状態変更(detect_and_apply・batch行・lease・SQS・child fan-out・
  市場依存のLINE通知)より前に行う。呼び出し側の責務。

## VALIDATIONの明示bypass(D1)

`allow_market_closed`(default=false)。**VALIDATIONかつtrueのときだけ**休場日でも実行できる。
NORMALでtrueはエラー。bool以外の値は休場日の判定より先にエラー。recovery/childには適用しない
(それらは本gateを通らない)。暗黙のbypassは無い。使用した事実はPIIなしでログへ残す。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

#: VALIDATION限定の明示bypassのevent key。
ALLOW_MARKET_CLOSED_KEY = "allow_market_closed"

#: skipしたhandlerが返す`skipped`の値。
SKIP_REASON = "MARKET_CLOSED"

MARKET_CLOSED_SKIP_EVENT = "MARKET_CLOSED_SKIP"
MARKET_CLOSED_BYPASS_EVENT = "MARKET_CLOSED_BYPASS"


class MarketClosedBypassError(ValueError):
    """`allow_market_closed`の指定が不正(型が不正 / NORMALでtrue)。

    `ValueError`を継承するのは、既存の`resolve_execution_context()`が不正な指定に対して
    送出するものと同じ型で受けられるようにするため(呼び出し側・テストが型ごとに分岐しない)。
    """


def resolve_allow_market_closed(event: dict[str, Any], execution_context: ExecutionContext) -> bool:
    """`allow_market_closed`を検証して返す。休場日の判定より**前**に呼ぶこと。

    - キー無し / null(None)は false(既定)。
    - bool以外(文字列"true"・数値1等)はエラー。真偽値へ黙って変換しない。
    - VALIDATION以外(NORMAL)でtrueはエラー(暗黙のbypassを作らない)。
    """
    if ALLOW_MARKET_CLOSED_KEY not in event or event[ALLOW_MARKET_CLOSED_KEY] is None:
        return False
    raw = event[ALLOW_MARKET_CLOSED_KEY]
    if not isinstance(raw, bool):
        raise MarketClosedBypassError(
            f"{ALLOW_MARKET_CLOSED_KEY} must be a boolean (got {type(raw).__name__})"
        )
    if raw and not execution_context.is_validation:
        raise MarketClosedBypassError(
            f"{ALLOW_MARKET_CLOSED_KEY}=true requires execution_mode=VALIDATION"
        )
    return raw


def is_market_closed(business_date_jst: dt.date, config: AppConfig) -> bool:
    """`business_date_jst`(JST暦日)がJPXの非営業日か。判定は`BusinessCalendar`(S-04)のみ。

    ★ テストの既定fixture(tests/unit/conftest.py)がこの関数を「常に営業日」へ差し替える
      (実時刻が休場日のとき、親経路の既存テストが落ちないようにするため)。gateそのものの
      テストは`real_market_calendar` markerで実関数へ戻す。
    """
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    return not calendar.is_business_day(business_date_jst)


@dataclass(frozen=True)
class MarketClosedDecision:
    skip: bool
    business_date_jst: dt.date
    bypassed: bool


def decide_market_closed(
    now: dt.datetime, config: AppConfig, *, allow_market_closed: bool
) -> MarketClosedDecision:
    """休場日か・skipするか・bypassしたかを判定する(副作用なし)。

    `now`はtimezone-awareであること(naiveを暗黙にUTC扱いしない)。日付は
    `evaluation_date_jst(now)`で決める(JST 00:00-09:00にUTC上の前日になる境界に注意)。
    """
    require_timezone_aware(now)
    business_date = evaluation_date_jst(now)
    closed = is_market_closed(business_date, config)
    return MarketClosedDecision(
        skip=closed and not allow_market_closed,
        business_date_jst=business_date,
        bypassed=closed and allow_market_closed,
    )


def should_skip_for_market_closed(
    event: dict[str, Any],
    execution_context: ExecutionContext,
    now: dt.datetime,
    config: AppConfig,
    *,
    handler: str,
    job_type: str | None = None,
) -> bool:
    """市場依存のentryを、この呼び出しでskipすべきならTrueを返す(skip時は構造化INFOを1件出す)。

    呼び出し順序(handler側の契約):
      1 event/modeのvalidation(不正なら従来どおりエラー)
      2 recovery/child等の非対象分岐(これらは本gateを通らない)
      3 本関数(bypass指定の検証 → JST日付 → is_business_day)
      4 skip時はno-opを返す。最初の状態変更より前であること
    """
    allow = resolve_allow_market_closed(event, execution_context)
    decision = decide_market_closed(now, config, allow_market_closed=allow)
    if decision.skip:
        _log(
            MARKET_CLOSED_SKIP_EVENT,
            handler,
            decision.business_date_jst,
            execution_context,
            job_type,
        )
        return True
    if decision.bypassed:
        _log(
            MARKET_CLOSED_BYPASS_EVENT,
            handler,
            decision.business_date_jst,
            execution_context,
            job_type,
        )
    return False


def _log(
    event_name: str,
    handler: str,
    business_date_jst: dt.date,
    execution_context: ExecutionContext,
    job_type: str | None,
) -> None:
    # PIIを含めない(銘柄コード・owner・保有数量・価格は出さない。#135)。
    parts = [
        f"event={event_name}",
        f"handler={handler}",
        f"business_date_jst={business_date_jst.isoformat()}",
        f"execution_mode={execution_context.mode.value}",
    ]
    if job_type is not None:
        parts.append(f"job_type={job_type}")
    logger.info(" ".join(parts))
