"""資金入替の観測 P の runner(Issue #604 P-b3。dormant)。

P-b1(日の集計)・P-b2(読取)の部品を 1 回の起動へ組み立てる。設計: #604 P-b 設計 rev7
(…6169348315)+ rev8(…6169691749。総予算)+ 設計レビュー PASS(…6169650442)+ USER 決定
(#122 …6174293468 の 7。P-b3 の先行・mode は OFF / SHADOW のみ・ACTIVE への接続なし)。

**どこからも import されない**(配線は P-b4。USER 判断待ち)。具体の client・repository・
AuditService・boto3・他の services を import しない(書込・読取は Protocol の ports。
時計・環境変数・設定も読まない)。

## 1 回の流れ(最初に当たったもので戻る)

1 mode が OFF → `NOT_RUN_OFF`。★ ports を 1 回も呼ばない・時計(now)を検査しない ★
2 now が naive → 契約エラー(時刻の型。例外を送出する)
3 対象日 = now の JST の暦日。営業日でない → `NOT_RUN_NOT_BUSINESS_DAY`
  (★ 平日の休場日に gap を作らない ★)
4 総予算の確認(残り ≦ 書込の予約)→ 読めない日として扱う / 保有バッチの行を読む →
  完了なら保有の入力を組み立てる
5 `observe_day`(P-b1)の結果:
    OBSERVED  → `record_if_absent`(True = `WRITTEN` / False = `ALREADY_PRESENT`。先に書いた回が勝つ)
    それ以外(完了前・読取失敗・上限超過)
      now < その日の最後の回の時刻  → `DEFERRED`(書かない。次の回が再評価する。
                                      一時的な状態が日を固定しない)
      now >= 最後の回の時刻          → gap の記録を書く(`GAP_WRITTEN` / `GAP_ALREADY_PRESENT`)
6 書込が例外 → `WRITE_FAILED`(再試行しない。次の回が同じ ID で再試行する)/ それ以外の
  想定外の例外 → `UNEXPECTED_ERROR`(型名だけ)。★ `failed` が真のとき、P-b4 の handler は
  Lambda を失敗させる ★(Errors の指標 → Alarm。記録できなかった日を黙って捨てない)。
  runner 自体は例外を送出せず、結果で返す

## 設計の補足(rev7 からの差分)

* 最後の回の指定は『その日の時刻』(`last_run_time`。JST の naive な time)。Lambda は毎日
  起動するため、固定の日時ではなく『各日の最後の回』を表す。★ 構築時に『最後の回 ≦ 予定時刻
  + TTL − 余裕(`latest_safe_run_at`)』を検証する ★(BatchRuns の行は TTL 6 時間で消える。
  窓を TTL の外に置いた配線は構築で落ちる)
* ★ 総予算(rev8 SHOULD-3)★: `TIME_BUDGET_SECONDS`(読取)+ `WRITE_RESERVE_SECONDS`
  (書込の予約)。runner は『残り時間』を返す注入された関数 `remaining_seconds()` を読取の
  ports へ渡す(runner は時計を読まない)。残りが予約以下になったら読取は部分結果にせず
  失敗(None)を返す → 読めない日 → 最後の回は gap が書ける
* 読取の port(batch_runs・holdings・calendar)の失敗は値(None)で返す契約。読取の port が
  例外を送出するのは契約違反で `UNEXPECTED_ERROR` になる。書込の port(sink)は値による失敗の
  経路を持たないため、その例外は想定された失敗として `WRITE_FAILED` になる(§6)

## 守ること(契約テストで固定)

* ACTIVE は存在しない。mode の未知の値・None・文字列は契約エラー(OFF にも SHADOW にも暗黙変換しない)
* 識別子・値・例外の文面をログ・結果・sink へ渡さない(件数・状態・理由コード・型名のみ)
* `except Exception` はこの module に 1 か所だけ
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from jstock_advisor.domain.jst import to_jst
from jstock_advisor.domain.signals.rotation_observation import (
    BatchCompletionState,
    BatchFacts,
    CapitalRotationObservation,
    DayStatus,
    HoldingInput,
    judge_batch_completion,
    observe_day,
)
from jstock_advisor.domain.signals.rotation_observation_record import audit_id_for, to_output_values
from jstock_advisor.domain.signals.rotation_observation_schedule import (
    DEFAULT_SCHEDULED_TIME,
    holdings_batch_id,
    latest_safe_run_at,
    scheduled_at,
)
from jstock_advisor.domain.signals.rotation_source_eligibility import EligibilityWindow

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

#: 読取に使ってよい時間の上限(秒)。通常の実行は 1〜3 秒で当たらない。
#: 暴走の安全弁(投資判断の閾値ではない)
TIME_BUDGET_SECONDS: Final = 40
#: 書込 1 回の最悪の待ち + 余裕(秒)。読取はこの時間を残して止まる
#: (最後の回の gap を書けるようにする)
WRITE_RESERVE_SECONDS: Final = 10
#: 起動 1 回の総予算(秒)= 読取 + 書込の予約。`remaining_seconds()` はこの総予算の残りを返す
TOTAL_BUDGET_SECONDS: Final = TIME_BUDGET_SECONDS + WRITE_RESERVE_SECONDS

#: ログの印(件数・状態・理由コードだけ。識別子・値を含めない)
MARKER_RUN: Final = "ROTATION_OBSERVATION_RUN"
MARKER_FAILED: Final = "ROTATION_OBSERVATION_FAILED"
MARKER_GAP: Final = "ROTATION_OBSERVATION_GAP"


class RotationRunnerContractError(ValueError):
    """入力の型・値が契約に反する(組み立て側の誤り)。値を含めない。"""


class RotationObserverMode(StrEnum):
    """P の mode。★ ACTIVE は存在しない ★(投資判断・表示・LINE への接続は別の承認)。"""

    OFF = "OFF"
    SHADOW = "SHADOW"


class RunKind(StrEnum):
    NOT_RUN_OFF = "NOT_RUN_OFF"
    NOT_RUN_NOT_BUSINESS_DAY = "NOT_RUN_NOT_BUSINESS_DAY"
    DEFERRED = "DEFERRED"
    WRITTEN = "WRITTEN"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    GAP_WRITTEN = "GAP_WRITTEN"
    GAP_ALREADY_PRESENT = "GAP_ALREADY_PRESENT"
    WRITE_FAILED = "WRITE_FAILED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


_FAILED_KINDS: Final = frozenset({RunKind.WRITE_FAILED, RunKind.UNEXPECTED_ERROR})


@dataclass(frozen=True)
class RotationRunResult:
    """1 回の起動の結果。閉じた語彙 + 日の状態 + 型名だけ。値・識別子を持たない。"""

    kind: RunKind
    day_status: DayStatus | None = None
    error_type: str | None = None

    @property
    def failed(self) -> bool:
        """真のとき P-b4 の handler は Lambda を失敗させる(Errors の指標 → Alarm)。"""
        return self.kind in _FAILED_KINDS


# --- ports(すべて Protocol。注入)------------------------------------------------------------


class BatchRunPort(Protocol):
    def get(self, batch_id: str) -> BatchFacts | None:
        """保有バッチの行。読めない・無いなら None(例外にしない契約)。"""
        ...


class HoldingInputsPort(Protocol):
    def inputs_for(
        self,
        window: EligibilityWindow,
        *,
        remaining_seconds: Callable[[], float],
        reserve_seconds: float,
    ) -> Sequence[HoldingInput] | None:
        """窓の内側の保有の入力。列挙・総予算で読めなければ None(部分結果にしない)。"""
        ...


class ObservationSink(Protocol):
    def record_if_absent(self, audit_id: str, output_values: Mapping[str, Any]) -> bool:
        """True = 書いた / False = 既にあった(最初の 1 件が勝つ)。

        具体の AuditService への adapter は P-b4。
        """
        ...


class BusinessDayPort(Protocol):
    def is_business_day(self, day: dt.date) -> bool: ...


@dataclass(frozen=True)
class RotationObserverPorts:
    batch_runs: BatchRunPort
    holdings: HoldingInputsPort
    sink: ObservationSink
    calendar: BusinessDayPort
    remaining_seconds: Callable[[], float]


@dataclass(frozen=True)
class RotationObserver:
    ports: RotationObserverPorts
    last_run_time: dt.time
    scheduled_time: dt.time
    batch_id_for: Callable[[dt.date], str]

    def last_run_at(self, target_date: dt.date) -> dt.datetime:
        """対象日の『最後の回』の時刻(JST の aware)。"""
        return scheduled_at(target_date, self.last_run_time)


def _default_resolver(scheduled_time: dt.time) -> Callable[[dt.date], str]:
    return lambda day: holdings_batch_id(day, scheduled_time)


def make_rotation_observer(
    ports: RotationObserverPorts,
    *,
    last_run_time: dt.time,
    scheduled_time: dt.time = DEFAULT_SCHEDULED_TIME,
    batch_id_for: Callable[[dt.date], str] | None = None,
) -> RotationObserver:
    """observer を作る。★ 構築時に『最後の回』が BatchRuns の TTL の内側であることを検証する ★。"""
    if not isinstance(ports, RotationObserverPorts):
        raise RotationRunnerContractError("ports must be RotationObserverPorts")
    for name, value in (("last_run_time", last_run_time), ("scheduled_time", scheduled_time)):
        if not isinstance(value, dt.time) or value.tzinfo is not None:
            raise RotationRunnerContractError(f"{name} must be a naive time of day (JST)")
    # 日付に依らない比較(基準の日付は date.min。時計を読まない)
    reference = dt.date.min
    if scheduled_at(reference, last_run_time) < scheduled_at(reference, scheduled_time):
        raise RotationRunnerContractError("the last run must not be before the scheduled time")
    if scheduled_at(reference, last_run_time) > latest_safe_run_at(reference, scheduled_time):
        raise RotationRunnerContractError(
            "the last run is beyond the BatchRuns TTL safe limit (latest_safe_run_at)"
        )
    resolver = batch_id_for if batch_id_for is not None else _default_resolver(scheduled_time)
    return RotationObserver(ports, last_run_time, scheduled_time, resolver)


def _check_mode(mode: object) -> RotationObserverMode:
    if not isinstance(mode, RotationObserverMode):
        raise RotationRunnerContractError("mode must be a RotationObserverMode")
    return mode


def _check_now(now: object) -> dt.datetime:
    if not isinstance(now, dt.datetime) or now.tzinfo is None:
        raise RotationRunnerContractError("now must be a timezone-aware datetime")
    return now


def _observation(observer: RotationObserver, target_date: dt.date) -> CapitalRotationObservation:
    """対象日の観測(読取。書込はしない)。総予算が尽きていれば読めない日として扱う。"""
    ports = observer.ports
    if ports.remaining_seconds() <= WRITE_RESERVE_SECONDS:
        return observe_day(target_date=target_date, batch=None, holdings=None)
    batch = ports.batch_runs.get(observer.batch_id_for(target_date))
    judgement = judge_batch_completion(batch, target_date)
    holdings: Sequence[HoldingInput] | None = None
    if judgement.state is BatchCompletionState.COMPLETE and judgement.window is not None:
        holdings = ports.holdings.inputs_for(
            judgement.window,
            remaining_seconds=ports.remaining_seconds,
            reserve_seconds=WRITE_RESERVE_SECONDS,
        )
    return observe_day(target_date=target_date, batch=batch, holdings=holdings)


def observe_rotation(
    mode: RotationObserverMode, now: dt.datetime, observer: RotationObserver
) -> RotationRunResult:
    """1 回の起動の観測(上の流れ)。例外は送出せず結果で返す(契約エラーを除く)。"""
    if _check_mode(mode) is RotationObserverMode.OFF:
        return RotationRunResult(RunKind.NOT_RUN_OFF)
    moment = _check_now(now)
    if not isinstance(observer, RotationObserver):
        raise RotationRunnerContractError("observer must be a RotationObserver")
    stage = "read"
    status: DayStatus | None = None
    try:
        target_date = to_jst(moment).date()
        if not observer.ports.calendar.is_business_day(target_date):
            return RotationRunResult(RunKind.NOT_RUN_NOT_BUSINESS_DAY)
        observation = _observation(observer, target_date)
        status = observation.day_status
        is_gap = status is not DayStatus.OBSERVED
        if is_gap and moment < observer.last_run_at(target_date):
            return RotationRunResult(RunKind.DEFERRED, status)
        audit_id = audit_id_for(observation)
        output_values = to_output_values(observation)
        stage = "write"
        written = observer.ports.sink.record_if_absent(audit_id, output_values)
        if is_gap:
            kind = RunKind.GAP_WRITTEN if written else RunKind.GAP_ALREADY_PRESENT
            logger.warning("%s status=%s kind=%s", MARKER_GAP, status.value, kind.value)
        else:
            kind = RunKind.WRITTEN if written else RunKind.ALREADY_PRESENT
            logger.info("%s kind=%s", MARKER_RUN, kind.value)
        return RotationRunResult(kind, status)
    except Exception as error:  # noqa: BLE001 - 唯一の捕捉。値・識別子・文面を持たず型名だけを残す
        kind = RunKind.WRITE_FAILED if stage == "write" else RunKind.UNEXPECTED_ERROR
        logger.warning("%s kind=%s error_type=%s", MARKER_FAILED, kind.value, type(error).__name__)
        return RotationRunResult(kind, status, type(error).__name__)
