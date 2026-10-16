"""資金入替の観測 P の runner へ渡す ports の実体(Issue #604 P-b4 A1。dormant)。

P-b3 の runner(`rotation_observation_runner`)は ports(Protocol)だけを知る。ここは、その ports の
本番の実体を、P-b2 の reader・A1 の Scan の proxy・既存の AuditService へつなぐ薄い adapter。
判定・集計の規則は持たない(P-b1 / P-b3 に在る)。環境変数・時計は読まない
(表名・client・resolver・時計は引数で受ける。読むのは handler の 1 か所だけ)。

## 守ること(契約テストで固定)

* ★ 読取は 2 つの proxy だけ ★: BatchRuns・評価記録・Recommendation = `RotationReadOnlyClient`
  (get_item / batch_get_item / query)/ 保有 ID の列挙 = `RotationScanClient`(scan だけ)。
  書込メソッドを持つ `DynamoDbCollectionStore`・repository は使わない
* ★ 書込は AuditLog への『日の集計』1 件(条件付き PutItem・先勝ち)だけ ★: 唯一の書込経路は
  `AuditObservationSink.record_if_absent` で、`AuditService.record_if_absent` を呼ぶ
  (`record`〔ランダム ID・重複する〕は呼ばない)
* 識別子・値・例外の文面を持たない・出さない(件数・状態・型名のみ)
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from jstock_advisor.config.loader import DEFAULT_CONFIG_DIR, _load_json
from jstock_advisor.config.models import HolidayCalendarConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.rotation_observation import BatchFacts, HoldingInput
from jstock_advisor.domain.signals.rotation_observation_record import (
    CONTRACT_VERSION,
    RECORD_KIND,
)
from jstock_advisor.domain.signals.rotation_source_eligibility import EligibilityWindow
from jstock_advisor.infrastructure.aws.rotation_observation_reader import (
    HoldingRecordReader,
    RecommendationReader,
    RotationReadOnlyClient,
    read_batch_facts,
)
from jstock_advisor.infrastructure.aws.rotation_observation_scan import (
    HoldingIdScanner,
    RotationScanClient,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.rotation_observation_inputs import assemble_holding_inputs

#: 読む表・書く表の論理名(`resolve_table_name` の入力。実際の表名は stack の prefix で決まる)
BATCH_RUNS_FILE: Final = "batch_runs.json"
HOLDINGS_FILE: Final = "holdings_v2.json"
EVALUATION_RECORDS_FILE: Final = "holding_evaluation_records.json"
RECOMMENDATIONS_FILE: Final = "recommendations.json"
#: 書込先は AuditLog(`AuditService` が決める)。表の論理名は契約テストが照合する
AUDIT_LOG_FILE: Final = "audit_log.json"

#: AuditLog の `decision_type`(記録の種別と同じ語)
DECISION_TYPE: Final = RECORD_KIND


class BatchRunReader:
    """`BatchRunPort` の実体。BatchRuns の保有バッチの行を読む(例外にせず None)。"""

    def __init__(self, client: RotationReadOnlyClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def get(self, batch_id: str) -> BatchFacts | None:
        return read_batch_facts(self._client, self._table_name, batch_id)


class BusinessCalendarAdapter:
    """`BusinessDayPort` の実体。営業日の判定は既存の `BusinessCalendar` へ委譲する。"""

    def __init__(self, calendar: BusinessCalendar) -> None:
        self._calendar = calendar

    def is_business_day(self, day: dt.date) -> bool:
        return self._calendar.is_business_day(day)


def load_business_calendar(config_dir: Path | None = None) -> BusinessCalendar:
    """既存の `holiday_calendar.json` から営業日のカレンダーを作る。

    読めなければ例外(handler が失敗 → Alarm)。『読めないので全日を営業日とみなす』にはしない。
    """
    directory = config_dir or DEFAULT_CONFIG_DIR
    config = HolidayCalendarConfig.model_validate(_load_json(directory / "holiday_calendar.json"))
    return BusinessCalendar.from_config(config)


class ScanHoldingInputs:
    """`HoldingInputsPort` の実体。保有 ID(Scan)→ 評価記録(Query)→ Recommendation(BatchGet)。"""

    def __init__(
        self,
        *,
        scan_client: RotationScanClient,
        read_client: RotationReadOnlyClient,
        holdings_table: str,
        records_table: str,
        recommendations_table: str,
        resolve_attention_origin: Callable[[Recommendation], str | None],
        sleep: Callable[[float], None],
    ) -> None:
        self._scan_client = scan_client
        self._holdings_table = holdings_table
        self._records = HoldingRecordReader(read_client, records_table)
        self._recommendations = RecommendationReader(
            read_client, recommendations_table, resolve_attention_origin, sleep
        )

    def inputs_for(
        self,
        window: EligibilityWindow,
        *,
        remaining_seconds: Callable[[], float],
        reserve_seconds: float,
    ) -> Sequence[HoldingInput] | None:
        scanner = HoldingIdScanner(
            self._scan_client,
            self._holdings_table,
            remaining_seconds=remaining_seconds,
            reserve_seconds=reserve_seconds,
        )
        return assemble_holding_inputs(
            window=window,
            holding_ids=scanner,
            records=self._records,
            recommendations=self._recommendations,
            remaining_seconds=remaining_seconds,
            reserve_seconds=reserve_seconds,
        )


class AuditObservationSink:
    """`ObservationSink` の実体。AuditLog へ『日の集計』を 1 件(条件付き・先勝ち)書く。"""

    def __init__(self, service: AuditService, clock: Callable[[], dt.datetime]) -> None:
        self._service = service
        self._clock = clock

    def record_if_absent(self, audit_id: str, output_values: Mapping[str, Any]) -> bool:
        entry = self._service.record_if_absent(
            audit_id=audit_id,
            decision_type=DECISION_TYPE,
            stock_code=None,
            input_values={},
            calculation_formulas={},
            output_values=dict(output_values),
            data_sources=[],
            rule_version=CONTRACT_VERSION,
            timestamp=self._clock(),
        )
        return entry is not None


def remaining_seconds_from(
    total_seconds: float, started_at: float, monotonic: Callable[[], float]
) -> Callable[[], float]:
    """起動 1 回の総予算の残り(秒)を返す関数。時計(monotonic)は注入する(ここでは読まない)。"""

    def remaining() -> float:
        return total_seconds - (monotonic() - started_at)

    return remaining
