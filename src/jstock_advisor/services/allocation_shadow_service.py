"""購入側 Shadow(Portfolio Allocation。Q')の『枠』(Issue #603)。

#128 の Portfolio Allocation(#603)を、買い候補バッチの finalize が**完了した後**に、Production の
判断へ一切接続せずに観測するための入口と保護。この module は『枠』(入口の判定・総予算・期限つきの
I/O・読取の補助)だけを持つ。観測のループ(owner の符号 kv1・記録・索引・日単位の skip)は
`allocation_shadow_runner`、配線は `lambda_handlers/allocation_shadow_wiring`(#603 PR-F2-2b)。

## 入口の契約(契約テストで固定する。USER 承認: #122 の Q' の最終設計。#128 …6100461428 /
## サブちゃんの独立確認 …6101393320 への回答 …6101499922)

  1. 設定 OFF(既定・設定不備を含む)なら、**最初の判定で return**。I/O・AuditLog・
     AvailableCash・計算のいずれにも入らない。OFF のとき追加の監査書込もしない
  2. **例外を送出しない**(読取・計算・追記・期限のどれが失敗しても本流へ伝播させない)。
     再試行・障害通知・LINE の重複に影響しない
  3. 非通常実行(VALIDATION・DRY_RUN)は何も書かずに戻る。残り時間が
     `min_remaining_seconds` 未満・残り時間が不明のときは、実行せずに理由を記録してスキップする。
     残り時間は **owner ごとの開始時に毎回読む**(使い回さない。Q-F)
  4. 協調的な期限(`time_limit_seconds` = 30 秒)は強制停止ではなく、**Shadow 区間全体の総予算**
     (owner ごとではない。Q-G)。外部 I/O は**期限つきの使い捨てワーカー**(daemon スレッド +
     `join(timeout)`。`ThreadPoolExecutor` は使わない。Q-C)で実行し、待ち切れなければ結果を捨てて
     打ち切る。各 I/O の待ちは `min(io_timeout_seconds, 総予算の残り)`。予算が尽きたら、その時点の
     1 件だけを(`io_timeout_seconds` を上限に)記録して打ち切り、残りの owner は件数をログに出すのみ
     (最悪の所要 = 総予算 + 記録 1 回)。I/O は owner あたり高々 2 回(AvailableCash の読取 1 +
     AuditLog の条件付き追記 1)
  5. 重複防止は『事前の存在チェック』ではなく条件付き書込: `AuditService.record_if_absent`
     -> `insert_if_absent`(DynamoDB の `attribute_not_exists` の条件付き `put_item`)。
     audit_id は決定的で、結果 = `allocation_shadow:{batch_id}:{owner の符号}`、スキップ・失敗 =
     `allocation_shadow_skip:{batch_id}:{owner の符号}`(Q-B)。再試行・重複・競合でも各 ID は 1 件
  6. 記録する値は件数・理由コード・真偽・短い識別子のみ(`ShadowResult.facts` は scalar だけ)。
     銘柄・株数・金額・owner の実名は記録しない(owner は `owner_ref` の符号。#925 段階 0)

## 既知の振る舞い(契約)

  * **同じ batch・owner で、結果とスキップ(TIME_LIMIT 等)の 2 件が並びうる**: 待ち切れず捨てた
    ワーカーが後から成功した場合(Q-B)。読む側は『結果があれば結果を優先・無ければスキップ理由』
    と扱う。どちらの ID も、先に書いた方が残り、同じ ID への 2 回目は何もしない(内容は決定的)
  * **残り時間が取れないときは『実行しない』(fail-closed)**: 既存の `TimeBudget`
    (`recommendation_evaluation_service` / `evaluation_handler`)は『context が残り時間を提供しない
    場合は無制限』として扱う。向きが逆だが、評価バッチは必須の処理で『測れないなら制限しない』が、
    Shadow は任意の処理で『測れないなら実行しない』が妥当なため、再利用せず専用の判定を持つ。
    2 つの意味が repo に併存する(Q-E)

## 安全性の根拠と残余リスク(Q-A)

  捨てたワーカーは『次の凍結で止まる』のではない(凍結はスレッドの一時停止で、次の invocation で
  再開する)。安全性の根拠は、I/O が owner あたり高々 2 回で、読取は副作用がなく、追記は
  `attribute_not_exists` の条件付きで冪等であること = **後で完了しても害がない**。
  残余リスク: 捨てたワーカーは次の invocation の実行時間の中で DynamoDB への I/O を完了しうる
  (botocore の既定の timeout・再試行のまま走る)。害は、同じ決定的 ID の条件付き追記が後で成立
  しうることに限られる(上記の既知の振る舞い)。

  既知の限界: 総予算が尽きた owner は、索引の未記録(BUDGET_EXHAUSTED)として数える。finalize のみの
  再実行(recovery)の経路には置かないため、再実行で finalize した batch は記録なし。

## 本 module に置かないもの

  配分の計算の式・閾値・優先順位(#603 の optimizer・#602 の RAER)・IAM・設定を ON に
  すること(別の USER 承認)・#604 の P(独立実行)・LINE の表示
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.signals.allocation_shadow_config import (
    AllocationShadowConfig,
    load_allocation_shadow_config,
)
from jstock_advisor.services.audit_service import AuditService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DECISION_TYPE: Final = "allocation_shadow"
SKIP_ID_PREFIX: Final = "allocation_shadow_skip"
SCHEMA_VERSION: Final = 1
RULE_VERSION: Final = "allocation_shadow_frame_v1"

_IAM_DENIED_CODES: Final = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
)
_MAX_FACT_STRING_LENGTH: Final = 64


class ShadowOutcome(StrEnum):
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    COMPUTED = "COMPUTED"


class ShadowSkipReason(StrEnum):
    """実行しなかった・できなかった理由(AuditLog に記録する)。"""

    TIME_BUDGET = "TIME_BUDGET"  # 残り時間が min_remaining_seconds 未満、または不明
    TIME_LIMIT = "TIME_LIMIT"  # 協調的な期限(time_limit_seconds)を超えた / I/O を打ち切った
    CASH_NOT_REGISTERED = "CASH_NOT_REGISTERED"  # 買付余力が未登録(0 円ではない)
    IAM_MISSING = "IAM_MISSING"  # AvailableCash の GetItem が AccessDenied(IAM が未反映)
    INPUT_INCONSISTENT = "INPUT_INCONSISTENT"
    COMPUTE_NOT_IMPLEMENTED = "COMPUTE_NOT_IMPLEMENTED"  # 計算の差し込み先が無い
    COMPUTATION_FAILED = "COMPUTATION_FAILED"  # 計算が例外になった(型名のみ記録)
    # --- PR-F2-2a(計算の組み立て)の入力の不足・読取の失敗 ---
    HOLDINGS_READ_FAILED = "HOLDINGS_READ_FAILED"  # 保有の Scan が失敗した
    RECOMMENDATIONS_READ_FAILED = "RECOMMENDATIONS_READ_FAILED"  # 推奨の一括取得が失敗した
    INPUT_LIMIT_EXCEEDED = "INPUT_LIMIT_EXCEEDED"  # 候補 ∪ 保有の銘柄数が上限を超えた(読まない)
    COEFFICIENTS_UNAVAILABLE = "COEFFICIENTS_UNAVAILABLE"  # RAER の係数が読めない(縮退)


_Scalar = int | float | str | bool


@dataclass(frozen=True)
class ShadowResult:
    """1 owner の結果。facts は scalar のみ(件数・理由コード・真偽。実名・金額・株数を入れない)。"""

    outcome: ShadowOutcome
    reason: ShadowSkipReason | None = None
    facts: Mapping[str, _Scalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome is ShadowOutcome.COMPUTED:
            if self.reason is not None:
                raise ValueError("COMPUTED は理由を持たない")
        elif self.reason is None:
            raise ValueError("SKIPPED / FAILED は理由が要る")
        for key, value in self.facts.items():
            if not isinstance(key, str) or not isinstance(value, int | float | str | bool):
                raise ValueError("facts は scalar のみ")
            if isinstance(value, str) and len(value) > _MAX_FACT_STRING_LENGTH:
                raise ValueError("facts の文字列が長すぎる")


class Deadline:
    """協調的な期限。強制停止はしない(呼び出し側が expired() を確認する)。"""

    def __init__(self, limit_seconds: float, monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self._started = monotonic()
        self._limit = limit_seconds

    def remaining(self) -> float:
        return max(0.0, self._limit - (self._monotonic() - self._started))

    def expired(self) -> bool:
        return self.remaining() <= 0.0


class IoTimeoutError(Exception):
    """外部 I/O が期限内に終わらなかった(結果は捨てる)。"""


def call_with_timeout[T](fn: Callable[[], T], timeout_seconds: float) -> T:
    """`fn` を使い捨てのワーカー(daemon スレッド)で実行し、`timeout_seconds` だけ待つ。

    待ち切れなければ結果を捨てて `IoTimeoutError`。ワーカーは強制停止できないが、呼び出し側は
    結果を待たずに戻る(daemon のため Lambda の終了を妨げない)。`fn` の例外はそのまま再送出する。
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 呼び出し側へ運ぶ
            box["error"] = exc

    worker = threading.Thread(target=target, name="allocation-shadow-io", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise IoTimeoutError()
    if "error" in box:
        error = box["error"]
        if isinstance(error, Exception):
            raise error
        raise RuntimeError(type(error).__name__)
    return box["value"]  # type: ignore[no-any-return]


class CashReadStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_REGISTERED = "NOT_REGISTERED"
    IAM_MISSING = "IAM_MISSING"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


@dataclass(frozen=True)
class CashRead:
    status: CashReadStatus
    amount: Decimal | None = None
    last_reconciled_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None

    def as_skip_reason(self) -> ShadowSkipReason | None:
        """計算に進めない状態を、スキップの理由へ写す(AVAILABLE は None)。"""
        return {
            CashReadStatus.NOT_REGISTERED: ShadowSkipReason.CASH_NOT_REGISTERED,
            CashReadStatus.IAM_MISSING: ShadowSkipReason.IAM_MISSING,
            CashReadStatus.TIMEOUT: ShadowSkipReason.TIME_LIMIT,
            CashReadStatus.ERROR: ShadowSkipReason.COMPUTATION_FAILED,
        }.get(self.status)


def _default_cash_getter(owner: str) -> AvailableCash | None:
    # 遅延 import: OFF のとき AvailableCash の module・repository に触れない
    from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
        AvailableCashRepository,
    )

    return AvailableCashRepository().get(owner)


def read_available_cash(
    owner: str,
    *,
    timeout_seconds: float,
    get: Callable[[str], AvailableCash | None] | None = None,
) -> CashRead:
    """owner の買付余力を読む(GetItem 1 回。期限つき)。未登録は 0 円ではなく NOT_REGISTERED。"""
    getter = get if get is not None else _default_cash_getter
    try:
        record = call_with_timeout(lambda: getter(owner), timeout_seconds)
    except IoTimeoutError:
        return CashRead(CashReadStatus.TIMEOUT)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _IAM_DENIED_CODES:
            return CashRead(CashReadStatus.IAM_MISSING)
        return CashRead(CashReadStatus.ERROR)
    except Exception:  # noqa: BLE001 - 本流へ伝播させない(型名は呼び出し側で記録しない)
        return CashRead(CashReadStatus.ERROR)
    if record is None:
        return CashRead(CashReadStatus.NOT_REGISTERED)
    return CashRead(
        CashReadStatus.AVAILABLE,
        record.available_cash,
        last_reconciled_at=record.last_reconciled_at,
        updated_at=record.updated_at,
    )


def _precheck(
    config: AllocationShadowConfig,
    remaining_time_ms: Callable[[], int] | None,
) -> ShadowSkipReason | None:
    """実行前の条件。満たさなければ、実行せずに記録するスキップの理由を返す。"""
    if remaining_time_ms is None:
        return ShadowSkipReason.TIME_BUDGET  # 残り時間が不明なら実行しない(fail-closed)
    try:
        remaining_ms = remaining_time_ms()
    except Exception:  # noqa: BLE001
        return ShadowSkipReason.TIME_BUDGET
    if remaining_ms < config.min_remaining_seconds * 1000.0:
        return ShadowSkipReason.TIME_BUDGET
    return None


@dataclass(frozen=True)
class ShadowRunContext:
    """観測のループ(`allocation_shadow_runner`)へ渡す、1 batch 分の文脈。"""

    batch_id: str
    now: dt.datetime
    audit_service: AuditService
    deadline: Deadline
    io_timeout_seconds: float
    shadow_mode: str
    #: owner ごとの開始時に毎回読む(使い回さない。Q-F)。実行してよければ None
    precheck: Callable[[], ShadowSkipReason | None]


#: 設定 ON のときだけ呼ばれる『観測のループ』。例外を送出しない契約(入口が二重に握る)。
#: 記録したら True
ShadowRunner = Callable[[ShadowRunContext], bool]


def observe_allocation_shadow(
    *,
    batch_id: str | None,
    now: dt.datetime,
    execution_context: ExecutionContext,
    audit_service: AuditService,
    remaining_time_ms: Callable[[], int] | None,
    shadow_config: AllocationShadowConfig | None = None,
    wiring_factory: Callable[[], ShadowRunner] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """handler の finalize 完了後の合流点から呼ぶ入口。**例外を送出しない**。記録したら True。

    設定 OFF なら最初の判定で False を返す(何もしない。`wiring_factory` も呼ばない = repository・
    鍵の provider を生成しない)。ON のときだけ、`wiring_factory` が『観測のループ』を返す。
    """
    config = shadow_config if shadow_config is not None else load_allocation_shadow_config()
    if not config.enabled:
        return False
    try:
        return _observe(
            config,
            batch_id=batch_id,
            now=now,
            execution_context=execution_context,
            audit_service=audit_service,
            remaining_time_ms=remaining_time_ms,
            wiring_factory=wiring_factory,
            monotonic=monotonic,
        )
    except Exception as exc:  # noqa: BLE001 - 本流を落とさない。型名だけを警告する
        logger.warning("allocation shadow failed and was isolated (%s)", type(exc).__name__)
        return False


def _observe(
    config: AllocationShadowConfig,
    *,
    batch_id: str | None,
    now: dt.datetime,
    execution_context: ExecutionContext,
    audit_service: AuditService,
    remaining_time_ms: Callable[[], int] | None,
    wiring_factory: Callable[[], ShadowRunner] | None,
    monotonic: Callable[[], float],
) -> bool:
    if batch_id is None:
        logger.info("allocation shadow skipped: no batch_id (not a scheduled batch)")
        return False
    if execution_context.is_validation:
        # 非通常実行(VALIDATION・DRY_RUN)は、何も書かずに戻る(検証の経路へ副作用を持ち込まない)
        logger.info("allocation shadow skipped: non-normal execution")
        return False
    if wiring_factory is None:
        logger.info("allocation shadow skipped: no wiring")
        return False
    deadline = Deadline(config.time_limit_seconds, monotonic)  # 区間全体で 1 つ(総予算。Q-G)
    run = wiring_factory()
    return run(
        ShadowRunContext(
            batch_id=batch_id,
            now=now,
            audit_service=audit_service,
            deadline=deadline,
            io_timeout_seconds=config.io_timeout_seconds,
            shadow_mode=config.mode.value,
            precheck=lambda: _precheck(config, remaining_time_ms),
        )
    )
