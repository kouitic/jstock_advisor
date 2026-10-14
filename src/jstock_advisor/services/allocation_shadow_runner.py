"""購入側 Shadow の観測のループ(Issue #603 PR-F2-2b。#925 kv1 の配線)。

`allocation_shadow_service.observe_allocation_shadow`(入口。設定 OFF は最初の判定で戻る)が、
設定 ON のときだけ `wiring_factory` から受け取って呼ぶ『観測のループ』。計算(F2-2a の
`compute_owner`)・記録(F2-1 の builder)・owner の符号(kv1)を、**例外を本流へ出さずに**つなぐ。

## 流れ(設計 rev5・rev6。#603 …6160343374・…6160476713)

  1. 入口の判定: Lambda の残り時間が足りなければ、日単位の skip(`TIME_BUDGET`)を書いて戻る
  2. バッチ共通の入力(保有の Scan 1 + 推奨の一括取得)。作れなければ ★ 理由つきの日単位の skip ★
     (owner が分からない日。`owner 0 件の索引` では代替しない = CLI が『動いたが 0 件』と誤表示する)
  3. owner = 保有に現れる owner(昇順)。★ 16 を超える分は記録対象にも計算にも進めない ★(OWNER_CAP)
  4. 鍵(`DeadlineBoundKeyProvider`): 期限内の最初の成功だけをこの run で保持。取れなければ
     ★ owner 単位の記録を 1 件も書かず ★ 日単位の skip(KEY_UNAVAILABLE + 鍵の理由 + 件数)
  5. owner ごと: kv1 の符号(`resolve_owner_ref`)→ 計算 → 項目つきの記録 → 枠の記録(kv1)
  6. ★ 全 owner の後に索引 ★(実際に記録が存在する owner だけ。書けなかった owner は理由別の件数)

## 守ること(契約テストで固定)

  * 新規の記録は kv1 の符号だけ。kv0・固定の符号へは決して倒さない(鍵が取れなければ記録しない)
  * 例外・無限待ち・AccessDenied のどれも本流へ出さない(入口が二重に握る)。値・ARN・実名・保有 ID を
    ログに出さない(型名・理由コードだけ)
  * 書込は決定的な audit_id の条件付き追記(先着。再実行で重複しない)。I/O はすべて期限つき
  * 総予算が尽きたら、その時点の 1 owner の項目つきの記録だけ(io_timeout が上限)を許し、残りは
    BUDGET_EXHAUSTED。索引は最後に 1 回(io_timeout が上限)必ず試みる

## 既知の限界(管理者が R1 で受容済み)

  再実行(同じ日の 2 回目)で追加された owner の記録は、先着の索引に載らない(CLI は未記録として数える)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from jstock_advisor.domain.entities.identifier_ref import (
    IdentifierRefError,
    KeyedOwnerRef,
    KeyMaterial,
    KeyProvider,
    KeyUnavailable,
    KeyUnavailableReason,
    parse_keyed_ref,
    resolve_owner_ref,
)
from jstock_advisor.domain.jst import to_jst
from jstock_advisor.domain.signals.allocation_shadow_record import (
    MAX_INDEX_OWNERS,
    build_day_skip_record,
    build_index_record,
    day_skip_audit_id,
    index_audit_id,
    items_audit_id,
)
from jstock_advisor.domain.signals.allocation_shadow_record import (
    SCHEMA_VERSION as RECORD_SCHEMA_VERSION,
)
from jstock_advisor.services.allocation_shadow_compute import (
    BatchInputs,
    BatchUnavailable,
    OwnerComputation,
)
from jstock_advisor.services.allocation_shadow_service import (
    DECISION_TYPE,
    RULE_VERSION,
    SKIP_ID_PREFIX,
    Deadline,
    IoTimeoutError,
    ShadowOutcome,
    ShadowResult,
    ShadowRunContext,
    ShadowRunner,
    call_with_timeout,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

#: メトリクス用の固定の印(+ 理由コードだけ。値・ARN・実名を含めない)
DAY_SKIP_MARKER: Final = "ALLOCATION_SHADOW_DAY_SKIP"
INDEX_NOT_WRITTEN_MARKER: Final = "ALLOCATION_SHADOW_INDEX_NOT_WRITTEN"

ITEMS_DECISION_TYPE: Final = "allocation_shadow_items"
INDEX_DECISION_TYPE: Final = "allocation_shadow_index"
DAY_SKIP_DECISION_TYPE: Final = "allocation_shadow_day_skip"
ITEMS_RULE_VERSION: Final = "allocation_shadow_items_v1"
INDEX_RULE_VERSION: Final = "allocation_shadow_index_v1"
DAY_SKIP_RULE_VERSION: Final = "allocation_shadow_day_skip_v1"

#: バッチ共通の入力の準備(例外を送出しない契約。F2-2a の `prepare_batch_inputs` の束縛)
PrepareBatch = Callable[[Deadline, float], BatchInputs | BatchUnavailable]
#: owner 1 人の計算(例外を送出しない契約。F2-2a の `compute_owner` の束縛)
ComputeOwner = Callable[
    [BatchInputs, str, KeyedOwnerRef, Deadline, float, int | None], OwnerComputation
]


class _Write(StrEnum):
    WRITTEN = "WRITTEN"  # 新しく書いた
    EXISTS = "EXISTS"  # 同じ audit_id が既にある(先着。再実行)
    FAILED = "FAILED"  # 書けなかった(例外・期限)


class DeadlineBoundKeyProvider:
    """run の期限の壁つきの `KeyProvider`。★ `get()` は例外を送出しない ★

    最初の `get()` だけ内側の provider を呼ぶ(期限つき)。★ 期限内に成功した結果だけ ★ をこの run で
    保持する(期限後に成功した結果は捨てる)。失敗は保持しない(呼び出し側が最初の失敗で打ち切る)。
    """

    def __init__(self, inner: KeyProvider, deadline: Deadline, io_timeout_seconds: float) -> None:
        self._inner = inner
        self._deadline = deadline
        self._io_timeout = io_timeout_seconds
        self._held: KeyMaterial | None = None

    def __repr__(self) -> str:
        return "DeadlineBoundKeyProvider()"

    def get(self) -> KeyMaterial | KeyUnavailable:
        if self._held is not None:
            return self._held
        timeout = min(self._io_timeout, self._deadline.remaining())
        if timeout <= 0.0:
            return KeyUnavailable(KeyUnavailableReason.TIMEOUT)
        try:
            result = call_with_timeout(self._inner.get, timeout)
        except IoTimeoutError:
            return KeyUnavailable(KeyUnavailableReason.TIMEOUT)
        except Exception:  # noqa: BLE001 - provider の契約違反でも Shadow を止めない
            return KeyUnavailable(KeyUnavailableReason.ERROR)
        if isinstance(result, KeyMaterial):
            self._held = result
            return result
        if isinstance(result, KeyUnavailable):
            return result
        return KeyUnavailable(KeyUnavailableReason.ERROR)


def make_runner(
    *,
    prepare: PrepareBatch,
    compute_owner: ComputeOwner,
    key_provider: KeyProvider,
) -> ShadowRunner:
    """配線(`allocation_shadow_wiring`)が組み立てた部品から、『観測のループ』を作る。"""

    def run(ctx: ShadowRunContext) -> bool:
        return _Loop(ctx, prepare, compute_owner, key_provider).run()

    return run


@dataclass
class _Loop:
    ctx: ShadowRunContext
    prepare: PrepareBatch
    compute_owner: ComputeOwner
    key_provider: KeyProvider

    def __post_init__(self) -> None:
        self.business_date = to_jst(self.ctx.now).date()
        self.wrote_any = False

    # --- 書込 -----------------------------------------------------------------------------
    def _write(
        self,
        *,
        audit_id: str,
        decision_type: str,
        input_values: dict[str, Any],
        output_values: dict[str, Any],
        rule_version: str,
        timeout_seconds: float,
    ) -> _Write:
        ctx = self.ctx

        def write() -> Any:
            return ctx.audit_service.record_if_absent(
                audit_id=audit_id,
                decision_type=decision_type,
                stock_code=None,
                input_values=input_values,
                calculation_formulas={"schema_version": str(RECORD_SCHEMA_VERSION)},
                output_values=output_values,
                data_sources=[],
                rule_version=rule_version,
                timestamp=ctx.now,
            )

        try:
            written = call_with_timeout(write, timeout_seconds) is not None
        except Exception as exc:  # noqa: BLE001 - 記録の失敗も本流へ伝播させない
            logger.warning("allocation shadow write failed (%s)", type(exc).__name__)
            return _Write.FAILED
        if written:
            self.wrote_any = True
            return _Write.WRITTEN
        return _Write.EXISTS

    def _write_timeout(self, *, allow_over_budget: bool) -> float | None:
        """この書込が待ってよい秒数。総予算が尽きていれば、許されるときだけ io_timeout が上限。"""
        remaining = self.ctx.deadline.remaining()
        if remaining > 0.0:
            return min(self.ctx.io_timeout_seconds, remaining)
        return self.ctx.io_timeout_seconds if allow_over_budget else None

    def _day_skip(
        self,
        reason: str,
        *,
        key_unavailable_reason: KeyUnavailableReason | None = None,
        unrecorded_owner_count: int | None = None,
    ) -> bool:
        """日単位の skip(owner を持たない。業務日だけが key)。理由コードつきのログ 1 行つき。"""
        logger.warning("%s reason=%s", DAY_SKIP_MARKER, reason)  # 固定の印 + 理由コードだけ
        record = build_day_skip_record(
            business_date=self.business_date,
            batch_id=self.ctx.batch_id,
            shadow_mode=self.ctx.shadow_mode,
            reason=reason,
            key_unavailable_reason=key_unavailable_reason,
            unrecorded_owner_count=unrecorded_owner_count,
        )
        timeout = self._write_timeout(allow_over_budget=True)
        assert timeout is not None  # noqa: S101 - allow_over_budget=True なら None にならない
        status = self._write(
            audit_id=day_skip_audit_id(self.business_date),
            decision_type=DAY_SKIP_DECISION_TYPE,
            input_values={"batch_id": self.ctx.batch_id},
            output_values=record,
            rule_version=DAY_SKIP_RULE_VERSION,
            timeout_seconds=timeout,
        )
        return status is _Write.WRITTEN

    # --- ループ ---------------------------------------------------------------------------
    def run(self) -> bool:
        ctx = self.ctx
        precheck = ctx.precheck()
        if precheck is not None:
            return self._day_skip(precheck.value)  # TIME_BUDGET(残り時間が足りない・不明)
        inputs = self.prepare(ctx.deadline, ctx.io_timeout_seconds)
        if isinstance(inputs, BatchUnavailable):
            return self._day_skip(inputs.reason.value)
        owners_all = inputs.owners
        if not owners_all:
            logger.info("allocation shadow: no owner holds a position; nothing to record")
            return False
        owners = owners_all[:MAX_INDEX_OWNERS]
        unrecorded: dict[str, int] = {}
        if len(owners_all) > len(owners):
            unrecorded["OWNER_CAP"] = len(owners_all) - len(owners)
            logger.warning(
                "allocation shadow owner cap: %d owner(s) not recorded", unrecorded["OWNER_CAP"]
            )
        provider = DeadlineBoundKeyProvider(self.key_provider, ctx.deadline, ctx.io_timeout_seconds)
        recorded: list[KeyedOwnerRef] = []
        key_version: int | None = None
        key_failure: KeyUnavailableReason | None = None
        over_budget_used = False

        def count(reason: str, n: int = 1) -> None:
            unrecorded[reason] = unrecorded.get(reason, 0) + n

        for position, owner in enumerate(owners):
            try:
                resolved = resolve_owner_ref(owner, provider)
            except IdentifierRefError:
                count("REFUSED")  # 不正な owner(空など)。符号を作らない
                continue
            if isinstance(resolved, KeyUnavailable):
                key_failure = resolved.reason  # 鍵が取れない = 最初の失敗で打ち切る
                count("KEY_UNAVAILABLE", len(owners) - position)
                break
            key_version = parse_keyed_ref(resolved)[0]
            if ctx.precheck() is not None or (ctx.deadline.expired() and over_budget_used):
                count("BUDGET_EXHAUSTED", len(owners) - position)
                break
            try:
                computed = self.compute_owner(
                    inputs, owner, resolved, ctx.deadline, ctx.io_timeout_seconds, None
                )
            except Exception as exc:  # noqa: BLE001 - compute は例外を出さない契約だが二重に握る
                logger.warning(
                    "allocation shadow compute raised and was isolated (%s)", type(exc).__name__
                )
                count("WRITE_FAILED")
                continue
            # 総予算が尽きていれば、記録は 1 owner だけ(io_timeout が上限)許す。2 人目からは、上の
            # 判定(期限切れ かつ 予算超過の書込が使用済み)が計算の前に止める
            over_budget = ctx.deadline.expired()
            if over_budget:
                over_budget_used = True
            written = self._record_owner(resolved, computed, allow_over_budget=over_budget)
            if written is None:
                count("WRITE_FAILED")
            elif written:
                recorded.append(resolved)
            else:
                count("BUDGET_EXHAUSTED")
        if not recorded and key_failure is not None:
            return self._day_skip(
                "KEY_UNAVAILABLE",
                key_unavailable_reason=key_failure,
                unrecorded_owner_count=len(owners),
            )
        if key_version is None:
            return self.wrote_any  # 鍵を 1 度も使えず、日単位の skip を要する場合は上で戻っている
        self._write_index(recorded, unrecorded, key_version)
        return self.wrote_any

    def _record_owner(
        self, ref: KeyedOwnerRef, computed: OwnerComputation, *, allow_over_budget: bool
    ) -> bool | None:
        """項目つきの記録 → 枠の記録(最善努力)。

        戻り値: 項目つきの記録が存在する = True / 予算切れで書かなかった = False /
        項目つきの記録を作れない・書けなかった = None。
        """
        record = computed.record
        if record is None:  # header すら作れなかった日: 枠の記録だけ(索引には載せない)
            items_ok: bool | None = None
        else:
            timeout = self._write_timeout(allow_over_budget=allow_over_budget)
            if timeout is None:
                return False
            status = self._write(
                audit_id=items_audit_id(self.business_date, ref),
                decision_type=ITEMS_DECISION_TYPE,
                input_values={"batch_id": self.ctx.batch_id, "owner_ref": ref},
                output_values=record,
                rule_version=ITEMS_RULE_VERSION,
                timeout_seconds=timeout,
            )
            items_ok = True if status is not _Write.FAILED else None
        timeout = self._write_timeout(allow_over_budget=False)
        if timeout is not None:  # 枠の記録は、予算が残っているときだけ(最善努力)
            self._write(
                audit_id=_frame_audit_id(self.ctx.batch_id, ref, computed.result.outcome),
                decision_type=DECISION_TYPE,
                input_values={"batch_id": self.ctx.batch_id, "owner_ref": ref},
                output_values=_frame_output(computed.result),
                rule_version=RULE_VERSION,
                timeout_seconds=timeout,
            )
        return items_ok

    def _write_index(
        self, recorded: list[KeyedOwnerRef], unrecorded: dict[str, int], key_version: int
    ) -> None:
        """全 owner の後の索引。最後の 1 回として io_timeout を上限に必ず試みる。失敗は握る。"""
        try:
            record = build_index_record(
                business_date=self.business_date,
                batch_id=self.ctx.batch_id,
                shadow_mode=self.ctx.shadow_mode,
                key_version=key_version,
                recorded=recorded,
                unrecorded_reasons=unrecorded,
            )
            timeout = self._write_timeout(allow_over_budget=True)
            assert timeout is not None  # noqa: S101
            status = self._write(
                audit_id=index_audit_id(self.business_date),
                decision_type=INDEX_DECISION_TYPE,
                input_values={"batch_id": self.ctx.batch_id},
                output_values=record,
                rule_version=INDEX_RULE_VERSION,
                timeout_seconds=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s (%s)", INDEX_NOT_WRITTEN_MARKER, type(exc).__name__)
            return
        if status is _Write.FAILED:
            logger.warning("%s (write)", INDEX_NOT_WRITTEN_MARKER)


def _frame_audit_id(batch_id: str, ref: KeyedOwnerRef, outcome: ShadowOutcome) -> str:
    """枠の記録の決定的な audit_id(結果とスキップ・失敗は別の key。Q-B)。owner は kv1 の符号。"""
    prefix = DECISION_TYPE if outcome is ShadowOutcome.COMPUTED else SKIP_ID_PREFIX
    return f"{prefix}:{batch_id}:{ref}"


def _frame_output(result: ShadowResult) -> dict[str, Any]:
    return {
        "outcome": result.outcome.value,
        "reason": result.reason.value if result.reason is not None else None,
        **dict(result.facts),
    }
