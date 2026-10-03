"""評価・レビュー系handlerが`execution_mode`指定を黙殺せず明示的に失敗させる(Issue #287)。

対象: `evaluation_handler` / `weekly_review_handler` / `monthly_review_handler` /
`quarterly_review_handler`(Issue #70 F-B9)。watchlist系の同型は
`_watchlist_execution_mode.py`(Issue #286)である。

## なぜ拒否するのか(対応しないのか)

これらのhandlerは`execution_mode` / `notification_mode`を**読まない**。そのため
`{"execution_mode": "VALIDATION"}`で手動起動しても、**検証にならず本番の処理が
そのまま実行される**(評価結果・監査ログの書き込み、週次レビューのLINE送信・
GitHub Issue起票)。運用者が「検証実行」と認識した操作がProduction実行になる。

これらの処理には、検証モードで副作用を隔離する仕組みが無い。対応するかどうかは
仕様判断であり(USER決定 2026-10-03: 対応せず、明示的に拒否する = A)、
Issue #70の受入条件「対応するか**明示的に失敗する**(黙って本番実行しない)」の
後者を選ぶ。

## 運用上の帰結

Lambdaが例外で終わると`Errors` metricに計上され、4 handlerとも`Errors`の
CloudWatch Alarmを持つため、**拒否されたときはAlarmが作動しうる**
(IncidentNotifier経由のLINE異常通知)。「検証のつもりで本番を動かした」事故を、
静かな成功ではなく**目に見える失敗**にすることが目的である。

EventBridge Schedulerによる自動実行は、4つともInputを持たない(= このキーを
含み得ない)ため、この関数は素通りし、挙動は変わらない。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 受け取ったら失敗させるキー。`_execution_mode.resolve_execution_context()` が
#: 解釈する2つと同じ集合であり、片方だけ指定された場合も取りこぼさない。
REJECTED_KEYS = ("execution_mode", "notification_mode")


class ReviewExecutionModeNotSupportedError(ValueError):
    """評価・レビュー系handlerへ`execution_mode`が指定された(対応していない)。

    `ValueError`を継承するのは、既存の`resolve_execution_context()`が不正な指定に
    対して送出するものと**同じ型で受けられる**ようにするためである。
    """


def reject_execution_mode(event: Any, *, handler_name: str) -> None:
    """`execution_mode` / `notification_mode`が指定されていたら例外を送出する。

    指定が無ければ何もしない(EventBridge Schedulerからの自動実行はInputを持たない
    ため、この関数は素通りする)。**handlerの最初の文として呼ぶこと**: 拒否時に
    config・provider・LINE client・serviceの生成や外部への書き込みが1つも
    起きないようにするためである(呼び出し位置はテストがASTで固定する)。

    ★ **黙って握りつぶさない。** 失敗させる前にERRORログを1件残す。ログに出すのは
      handler名と拒否したキー名だけで、キーの**値**・eventの内容は出さない。
    ★ 値が既知(VALIDATION等)か未知(typo等)かで扱いを変えない。
    ★ 値が`None`のキーは「指定なし」として扱う(#286と同じ)。
    ★ `event`がdictでないとき(Lambda consoleの不正な呼び出し等)は何もせず通す。
      従来の挙動を変えないためで、形式不正のeventの扱いは本Issueの範囲外である。
    """
    if not isinstance(event, dict):
        return
    specified = [key for key in REJECTED_KEYS if event.get(key) is not None]
    if not specified:
        return

    logger.error(
        "%s: execution_mode is not supported for review handlers, refusing to run keys=%s",
        handler_name,
        specified,
    )
    raise ReviewExecutionModeNotSupportedError(
        f"{handler_name} does not support {'/'.join(specified)}: "
        "evaluation and review batches write production results and audit records "
        "(and the weekly review sends LINE messages and files GitHub Issues), and no "
        "validation-mode isolation exists for them (Issue #287). "
        "Invoke without these keys."
    )
