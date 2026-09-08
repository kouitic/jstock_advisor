"""watchlist系handlerが`execution_mode`指定を黙殺せず明示的に失敗させる(Issue #286)。

## なぜ拒否するのか(対応しないのか)

watchlist系のバッチは、実行すると次の3つが**実際に起きる**。

    実LINE送信 / 実watchlistへの書き込み / rotation cursorの前進

このうち後ろ2つを検証モードで抑止する仕組みは**存在しない**(実測)。

    for_execution_context() を持つrepository  Recommendation / WatchState /
                                              HoldingsSnapshot / DailyNotificationPriority
    ★ WatchlistRepository と watchlist_rotation_state は **持たない**
    ★ infra/template.yaml の Validation*テーブルにも **watchlist / rotation は無い**

したがって「VALIDATIONを受け付ける」ようにすると、LINEは止まっても
**watchlistが実際に書き換わり、cursorが前進する**。検証にならないどころか、
「検証のつもりで本番の状態を変えた」という最悪の結果になる。

functional_spec 12.13節も「ウォッチリスト自動追加の通知…は対象外」と定めており、
対象へ含めるかどうかは**仕様判断**である(Issue #70 の Out of scope)。

-> よって本modeは **対応せず、指定されたら失敗させる**。
   Issue #70 の受入条件「対応するか**明示的に失敗する**(黙って本番実行しない)」の
   後者を選ぶ。

## 黙殺との違い

修正前は `execution_mode` を**読まなかった**ため、`{"execution_mode": "VALIDATION"}`
で起動しても**完全な本番実行**になっていた。運用者が「検証実行」と認識した操作が
Production実行になる、という状態である。

失敗させれば、少なくとも**間違いに気づける**。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 受け取ったら失敗させるキー。`_execution_mode.resolve_execution_context()` が
#: 解釈する2つと同じ集合であり、片方だけ指定された場合も取りこぼさない。
REJECTED_KEYS = ("execution_mode", "notification_mode")


class WatchlistExecutionModeNotSupportedError(ValueError):
    """watchlist系handlerへ`execution_mode`が指定された(対応していない)。

    `ValueError` を継承するのは、既存の
    `_execution_mode.resolve_execution_context()` が不正な指定に対して
    送出するものと**同じ型で受けられる**ようにするためである
    (呼び出し側・テストが例外の種類ごとに分岐しなくてよい)。
    """


def reject_execution_mode(event: dict[str, Any], *, handler_name: str) -> None:
    """`execution_mode` / `notification_mode` が指定されていたら例外を送出する。

    指定が無ければ何もしない(EventBridge Scheduleからの自動実行は
    Inputを持たないため、この関数は素通りする)。

    ★ **黙って握りつぶさない。** 失敗させる前にERRORログへ理由を残し、
      どのhandlerがどのキーを拒否したかを監視・調査から追えるようにする。
    ★ 値が既知(VALIDATION等)か未知(typo等)かで扱いを変えない。
      watchlist系はどの値も受け付けないため、区別する意味が無い。
    """
    specified = [key for key in REJECTED_KEYS if event.get(key) is not None]
    if not specified:
        return

    logger.error(
        "%s: execution_mode is not supported for watchlist handlers, refusing to run keys=%s",
        handler_name,
        specified,
    )
    raise WatchlistExecutionModeNotSupportedError(
        f"{handler_name} does not support {'/'.join(specified)}: "
        "watchlist batches write to the production watchlist and advance the rotation "
        "cursor, and no validation-mode isolation exists for them (Issue #286). "
        "Invoke without these keys, or use the CLI for a dry inspection."
    )
