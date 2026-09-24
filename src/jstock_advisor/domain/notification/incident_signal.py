"""Issue #506(#132 O-1): incident通知の入力を、発生源(CloudWatch Alarm / reconciler等)に
関わらず共通の形へ正規化する。

**純粋な関数と型だけ**である。ネットワーク・ファイル・AWS・永続化に触れない。

USER決定(#506 issuecomment-5805278274。通知経路のOption 1採用)により、IncidentNotifier
(lambda_handlers/incident_notifier_handler.py)は、CloudWatch AlarmのSNS payloadと、
reconciler等が発行するInternal structured incident payloadの両方を、本moduleが定義する
`IncidentSignal`へ正規化したうえで、以降(#502のfingerprint計算・#503のclaim/dedup・
#501の本文組み立て・LINE送信)を**完全に共通の処理**として扱う(発生源ごとに二重実装しない)。

`IncidentSignal`自体は#502の`IncidentFingerprintInput`とほぼ同じ要素を持つが、
`failure_count` / `consecutive_days` / `is_ongoing`(#501の`IncidentNotice`が受け取る
構造化フィールド)も併せて持つ点が異なる。これらはInternal payload(reconciler等)が
明示的に計算して渡す値であり、CloudWatch Alarm由来の場合はNone(該当情報を持たない)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.domain.jst import require_timezone_aware


@dataclass(frozen=True)
class IncidentSignal:
    """発生源を問わない、incident通知の共通入力。

    `source`は発生源の識別子(例: "cloudwatch_alarm" / "watchlist_reconciler")であり、
    #502のfingerprint計算の入力(environmentに相当する位置)には使わない
    (fingerprintの安定性は`job_name`/`failure_stage`/`failure_type`/`error_type`/
    `error_message`の5要素のみで決める、既存の契約を変更しないため)。
    """

    source: str
    job_name: str
    failure_stage: str
    failure_type: str
    error_type: str
    error_message: str
    occurred_at: dt.datetime
    failure_count: int | None = None
    consecutive_days: int | None = None
    is_ongoing: bool | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "source",
            "job_name",
            "failure_stage",
            "failure_type",
            "error_type",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty str, got {value!r}")
        if not isinstance(self.error_message, str):
            raise ValueError(f"error_message must be a str, got {self.error_message!r}")
        if not isinstance(self.occurred_at, dt.datetime):
            raise TypeError("occurred_at must be a datetime")
        require_timezone_aware(self.occurred_at)
        for field_name in ("failure_count", "consecutive_days"):
            value = getattr(self, field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise TypeError(f"{field_name} must be int or None, got {value!r}")
            if isinstance(value, int) and value < 0:
                raise ValueError(f"{field_name} must not be negative, got {value!r}")
        if self.is_ongoing is not None and not isinstance(self.is_ongoing, bool):
            raise TypeError("is_ongoing must be bool or None")
