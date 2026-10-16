"""資金入替の観測 P の Lambda handler(Issue #604 P-b4 A1。dormant = template なし)。

P-b3 の runner(`rotation_observation_runner.observe_rotation`)へ、P-b4 の ports を渡す **皮**。
どの Lambda からも呼ばれない(template・Scheduler・IAM は A2。USER 承認 #604 …6195577714。
出荷は Scheduler = DISABLED かつ mode = OFF)。

## 守ること(契約テストで固定)

* ★ mode は設定ファイル(`config/rotation_observation.yaml`)だけで決める ★: 環境変数・イベントでは
  決めない(P 固有の環境変数は 0 件)。出荷値 OFF。無い・不正なら OFF へ縮退。★ ACTIVE は存在しない ★
* ★ OFF では ports を 1 つも作らない ★(boto3 の client・AuditService にも触れない。AWS への通信 0)
* ★ 時計を読むのはこの module の 1 か所だけ ★(`now` と総予算の開始。runner・ports は注入)
* ★ runner が `failed`(記録に失敗)を返したら例外で Lambda を失敗させる ★(Errors の指標 → Alarm。
  記録できなかった日を黙って捨てない)。失敗の理由は型名・種別だけ(識別子・値・文面を出さない)
* 判定・通知・売却・購入・表示・LINE のどこにも接続しない(#606 の gate が固定)。BUY / SELL の
  handler とは別の関数・別の Role(A2)で、P の失敗は本流へ伝わらない
* `execution_mode` / `notification_mode` を受け取ったら失敗させる(読まないキーを黙殺しない。#287)
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Final

from jstock_advisor.domain.notification.notification_category import (
    resolve_attention_origin_for_recommendation,
)
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.lambda_handlers._review_execution_mode import reject_execution_mode
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.rotation_observation_config import (
    load_rotation_observation_config,
)
from jstock_advisor.services.rotation_observation_ports import (
    BATCH_RUNS_FILE,
    EVALUATION_RECORDS_FILE,
    HOLDINGS_FILE,
    RECOMMENDATIONS_FILE,
    AuditObservationSink,
    BatchRunReader,
    BusinessCalendarAdapter,
    ScanHoldingInputs,
    load_business_calendar,
    remaining_seconds_from,
)
from jstock_advisor.services.rotation_observation_runner import (
    TOTAL_BUDGET_SECONDS,
    RotationObserver,
    RotationObserverMode,
    RotationObserverPorts,
    RunKind,
    make_rotation_observer,
    observe_rotation,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

#: その日の『最後の回』(JST)。Scheduler の最後の起動(09:00)と一致させる(A2 の契約テストが照合)。
#: 構築時に `latest_safe_run_at`(BatchRuns の TTL の内側)を `make_rotation_observer` が検証する
LAST_RUN_TIME: Final = dt.time(9, 0, 0)

MARKER_OFF: Final = "ROTATION_OBSERVATION_OFF"


def _utc_now() -> dt.datetime:
    """★ 壁時計を読む唯一の場所 ★(テストはここを差し替える)。"""
    return dt.datetime.now(dt.UTC)


def _monotonic() -> float:
    """★ 総予算の経過を測る唯一の場所 ★(テストはここを差し替える)。"""
    return time.monotonic()


class RotationObservationFailedError(RuntimeError):
    """runner が失敗(記録に失敗・想定外の例外)を返した。種別だけを持つ(値・識別子を持たない)。"""


def _build_observer(started_at: float) -> RotationObserver:
    """OFF でないときだけ呼ばれる。実在の部品から ports を組み立てる(読取は 2 つの proxy だけ)。"""
    from jstock_advisor.infrastructure.aws.rotation_observation_reader import (  # noqa: PLC0415
        build_rotation_read_client,
    )
    from jstock_advisor.infrastructure.aws.rotation_observation_scan import (  # noqa: PLC0415
        build_rotation_scan_client,
    )

    read_client = build_rotation_read_client()
    ports = RotationObserverPorts(
        batch_runs=BatchRunReader(read_client, resolve_table_name(BATCH_RUNS_FILE)),
        holdings=ScanHoldingInputs(
            scan_client=build_rotation_scan_client(),
            read_client=read_client,
            holdings_table=resolve_table_name(HOLDINGS_FILE),
            records_table=resolve_table_name(EVALUATION_RECORDS_FILE),
            recommendations_table=resolve_table_name(RECOMMENDATIONS_FILE),
            resolve_attention_origin=resolve_attention_origin_for_recommendation,
            sleep=time.sleep,
        ),
        sink=AuditObservationSink(AuditService(), _utc_now),
        calendar=BusinessCalendarAdapter(load_business_calendar()),
        remaining_seconds=remaining_seconds_from(TOTAL_BUDGET_SECONDS, started_at, _monotonic),
    )
    return make_rotation_observer(ports, last_run_time=LAST_RUN_TIME)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    reject_execution_mode(event, handler_name="rotation observation")
    config = load_rotation_observation_config()
    if config.mode is RotationObserverMode.OFF:
        logger.info("%s", MARKER_OFF)
        return {"kind": RunKind.NOT_RUN_OFF.value}
    # ★ 時計を読むのはここだけ ★(now と総予算の開始。runner・ports は注入された値を使う)
    started_at = _monotonic()
    now = _utc_now()
    result = observe_rotation(config.mode, now, _build_observer(started_at))
    if result.failed:
        raise RotationObservationFailedError(result.kind.value)
    return {
        "kind": result.kind.value,
        "day_status": None if result.day_status is None else result.day_status.value,
    }
