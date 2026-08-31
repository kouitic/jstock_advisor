"""finalize-only recovery のイベント契約(Issue #57 Phase B2)。

毎時の watchlist reconciler が、finalize が完了していない buy/holdings バッチを
検出して当該 Lambda を非同期 invoke するときの payload 契約をここに集約する。

**この経路は「締めくくり処理だけ」を再実行する。**通常 worker の処理
(銘柄評価・Recommendation 再生成・DecisionSnapshot 再生成・EvaluationRecord
再保存・Audit 再登録・record_result・fanout)は**一切実行してはならない**。
#71(fanout の重複起動)が未修正のままでも、この経路が worker 処理を再実行
しないことで判定履歴の複製を持ち込まない。

検証は **fail-close**。少しでも整合しない payload は「何もしない」を選ぶ
(誤った family の finalize を走らせるより、実行されないまま次の毎時
reconciler の対象として残るほうが安全。#56 で確立した方針と同じ)。
"""

from __future__ import annotations

import logging
from typing import Any

from jstock_advisor.domain.entities.enums import ExecutionMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.aws.batch_tracker import (
    BatchFamily,
    CompletionBatchRecord,
    get_completion_batch,
)

logger = logging.getLogger(__name__)

RECOVERY_ACTION_KEY = "recovery_action"
FINALIZE_ONLY_ACTION = "FINALIZE_ONLY"


def build_finalize_only_payload(record: CompletionBatchRecord) -> dict[str, Any]:
    """reconciler が送る finalize-only invoke の payload を構築する。

    `execution_mode` は既存の worker payload と同じキー名を使い、invoke 先の
    `resolve_execution_context()` が**通常経路とまったく同じ方法で**
    ExecutionContext を組み立てられるようにする(recovery 専用の解決経路を
    作らない)。`notification_mode` は VALIDATION 専用の補助設定であり、
    自動 recovery の対象は NORMAL のみのため載せない。
    """
    if record.family is None or record.execution_context is None:
        raise ValueError(f"cannot build finalize-only payload: batch_id={record.batch_id}")
    return {
        RECOVERY_ACTION_KEY: FINALIZE_ONLY_ACTION,
        "batch_id": record.batch_id,
        "batch_family": record.family.value,
        "execution_mode": record.execution_context.mode.value,
    }


def is_recovery_event(event: dict[str, Any]) -> bool:
    """recovery payload として扱うべきイベントか(値の正当性は問わない)。

    未知・不正な `recovery_action` もここでは True を返し、
    `resolve_finalize_only_request()` 側で明示的に拒否する
    (未知の action が通常 worker 経路へ落ちて銘柄評価が走るのを防ぐため)。
    """
    return event.get(RECOVERY_ACTION_KEY) is not None


def resolve_finalize_only_request(
    event: dict[str, Any],
    expected_family: BatchFamily,
    execution_context: ExecutionContext,
) -> CompletionBatchRecord | None:
    """finalize-only recovery の実行可否を判定する(Issue #57 B2)。

    実行してよい場合のみ `CompletionBatchRecord` を返す。**それ以外はすべて
    None を返し、呼び出し側は何もしない**(fail-close)。

    検証内容:

    1. `recovery_action` が `FINALIZE_ONLY` であること(未知の action は拒否)
    2. `batch_id` が非空文字列であること
    3. batch 項目が存在すること(TTL 経過後は対象外)
    4. `batch_family` が復元でき、**この Lambda の family と一致**すること
       (buy Lambda へ holdings のバッチを渡す cross-family payload を拒否)
    5. payload の `batch_family` も一致すること(reconciler 側の取り違え検出)
    6. 永続化された実行文脈が復元でき、**NORMAL であること**
       (VALIDATION は自動 recovery の対象にしない。人間が意図して起動した
       検証実行を1時間後に自動再実行する契約にはしない)
    7. invoke 先で解決された実行文脈も NORMAL で、永続化値と一致すること
    8. 既に finalize 済みでないこと(no-op)

    取得回数上限は `try_acquire_completion_finalize()` の
    ConditionExpression が正本であり、ここでは事前 skip として扱うだけで
    最終判定は行わない。
    """
    action = event.get(RECOVERY_ACTION_KEY)
    if action != FINALIZE_ONLY_ACTION:
        logger.error(
            "finalize recovery rejected: unknown recovery_action=%r family=%s",
            action,
            expected_family.value,
        )
        return None

    batch_id = event.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        logger.error(
            "finalize recovery rejected: missing batch_id family=%s",
            expected_family.value,
        )
        return None

    payload_family = event.get("batch_family")
    if payload_family != expected_family.value:
        logger.error(
            "finalize recovery rejected: payload family mismatch batch_id=%s "
            "payload_family=%r expected=%s",
            batch_id,
            payload_family,
            expected_family.value,
        )
        return None

    record = get_completion_batch(batch_id)
    if record is None:
        logger.error(
            "finalize recovery rejected: batch record not found batch_id=%s family=%s",
            batch_id,
            expected_family.value,
        )
        return None

    if record.family != expected_family:
        logger.error(
            "finalize recovery rejected: persisted family mismatch batch_id=%s "
            "persisted_family=%r expected=%s",
            batch_id,
            record.family.value if record.family is not None else None,
            expected_family.value,
        )
        return None

    if record.execution_context is None:
        logger.error(
            "finalize recovery rejected: execution context unavailable batch_id=%s",
            batch_id,
        )
        return None

    if record.execution_context.mode != ExecutionMode.NORMAL:
        logger.info(
            "finalize recovery skipped: non-NORMAL batch is not auto re-driven "
            "batch_id=%s execution_mode=%s",
            batch_id,
            record.execution_context.mode.value,
        )
        return None

    if execution_context.mode != ExecutionMode.NORMAL:
        logger.error(
            "finalize recovery rejected: event execution_mode is not NORMAL "
            "batch_id=%s execution_mode=%s",
            batch_id,
            execution_context.mode.value,
        )
        return None

    if record.is_finalized:
        logger.info(
            "finalize recovery no-op: already finalized batch_id=%s",
            batch_id,
        )
        return None

    return record
