"""観測・Shadow計測専用処理をv1の判定経路から隔離する共有部品(Issue #384)。

Shadow計測・DecisionSnapshot整形専用の計算は、v1の判定へ接続しないが、
算出がanalyze()等の本流にinlineで置かれている限り、例外が出ればその銘柄
だけでなくbatch全体の判定出力が失われる(Issue #22 C2 / #371で確認された
リスク)。buy_signal_service.pyの`_isolated_shadow_observation()`(Issue #22
C2で新設)を共有部品として抽出したもの(本PRは振る舞い変更のないrefactor)。

契約:
  失敗を握りつぶさない(COMPUTATION_FAILEDと例外の型をfactsへ残し、
  warningをログへ出す)
  失敗時に0.0・空dict・空値へ偽装しない(「算出できなかった」を
  「値が無い」と読める形で保存しない)
  判定ロジック・閾値は変更しない

銘柄コード等の識別子はログへ出さない(Issue #135)。どの銘柄かはfacts側の
Recommendation等に紐づいており、ログへ平文で出す必要がない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

SHADOW_STATE_COMPUTATION_FAILED = "COMPUTATION_FAILED"


def isolated_shadow_observation(
    observation_name: str,
    build: Callable[[], dict[str, object]],
) -> dict[str, object]:
    """観測1件分の算出を、v1の判定経路から隔離して実行する。

    build()の中で例外が出ても、ここで捕捉してfactsへCOMPUTATION_FAILEDと
    例外の型を記録し、呼び出し元(v1の判定)へは伝播させない。
    """
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - shadowの失敗をv1へ伝播させない
        logger.warning(
            "shadow observation failed and was recorded as %s: observation=%s error=%s",
            SHADOW_STATE_COMPUTATION_FAILED,
            observation_name,
            type(exc).__name__,
            exc_info=True,
        )
        return {
            "shadow_state": SHADOW_STATE_COMPUTATION_FAILED,
            "error_type": type(exc).__name__,
        }


def isolated_shadow_computation[T](
    observation_name: str,
    build: Callable[[], T],
    on_failure: Callable[[Exception], T],
) -> T:
    """dict以外(domain object)を返すshadow計測の隔離(Issue #384 PR-3)。

    isolated_shadow_observation()はdict専用のため、domain object(例:
    HistoricalValuationResult等のResult型)を返す算出には使えない。
    on_failureは呼び出し側が型ごとに用意するfallback constructorであり、
    「業務上のNOT_EVALUATED」と区別できる形(reason_codesへ
    SHADOW_STATE_COMPUTATION_FAILEDを含むタグを積む等)で構築すること。
    既存のisolated_shadow_observation()の実装・契約は変更しない
    (本関数は追加のみ)。
    """
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - shadowの失敗をv1へ伝播させない
        logger.warning(
            "shadow observation failed and was recorded as %s: observation=%s error=%s",
            SHADOW_STATE_COMPUTATION_FAILED,
            observation_name,
            type(exc).__name__,
            exc_info=True,
        )
        return on_failure(exc)
