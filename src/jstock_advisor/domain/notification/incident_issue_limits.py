"""incident の GitHub Issue 起票の上限値(専用 YAML と専用 loader。Issue #926)。

## 位置づけ

#876 で出荷値を有効にした GitHub Issue の自動起票(#508)に、新規 Issue の作成を抑える構造的な仕組み
(同じ occurrence の重複の防止・前回作成からの最小間隔・期間内の上限・自動停止)を足す。この module は
その『人が決める値』だけを持つ。値は USER が決める(暫定案は #926 の設計に根拠つきで示した)。

  new_issue_min_interval_minutes   同じ fingerprint の新規 Issue を、前回作成からこの分数以上空ける
  new_issue_limit_per_fingerprint  JST の日付の窓あたり、1 fingerprint が作れる新規 Issue の数
  new_issue_limit_global           JST の日付の窓あたり、全体で作れる新規 Issue の数
  auto_stop_denied_threshold       全体の上限を超えた試行の数がこの回数に達したら、自動停止にする

## 未決定の扱い(fail-closed)

出荷値は全項目 null = 未決定。1 つでも未決定・読めない・不正なら、新規 Issue を作らない
(コメントと LINE は
従来どおり)。loader は例外を送出しない(LINE の経路を落とさない)。部分的に効いた設定を作らないため、
不正な値が 1 つでもあれば、決定済みの他の値も使わず全項目を未決定へ縮退する。

## AppConfig(共通部品 S-13)へ載せない理由

`AppConfig` へ field を足すと全領域の lock 検討が要る(development_workflow 2.6.5)。
#160 / #582 / 購入側
Shadow の設定(`allocation_shadow_config.py`)と同じ理由で、専用のモデルと専用 loader にする。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictInt, model_validator

from jstock_advisor.config.loader import DEFAULT_CONFIG_DIR, _load_yaml

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CONFIG_FILE_NAME = "incident_issue_limits.yaml"

#: 決めるべき値の名前(この並びで未決定を報告する)
_VALUE_NAMES = (
    "new_issue_min_interval_minutes",
    "new_issue_limit_per_fingerprint",
    "new_issue_limit_global",
    "auto_stop_denied_threshold",
)


class IncidentIssueLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    new_issue_min_interval_minutes: StrictInt | None = None
    new_issue_limit_per_fingerprint: StrictInt | None = None
    new_issue_limit_global: StrictInt | None = None
    auto_stop_denied_threshold: StrictInt | None = None

    @model_validator(mode="after")
    def _check_positive(self) -> IncidentIssueLimits:
        for name in _VALUE_NAMES:
            value = getattr(self, name)
            if value is not None and value <= 0:
                # bool は StrictInt が拒否する。0 以下は『上限なし』にも『常に拒否』にも読めて危険
                raise ValueError(f"{name} must be a positive integer")
        return self

    def missing(self) -> tuple[str, ...]:
        """未決定の項目名(固定の並び)。空でなければ新規 Issue を作らない。"""
        return tuple(name for name in _VALUE_NAMES if getattr(self, name) is None)

    def is_decided(self) -> bool:
        return not self.missing()


def undecided_limits() -> IncidentIssueLimits:
    """全項目が未決定(新規 Issue を作らない。fail-closed の縮退先)。"""
    return IncidentIssueLimits()


def load_incident_issue_limits(config_dir: Path | None = None) -> IncidentIssueLimits:
    """設定を読む。無い・読めない・不正なら全項目を未決定へ縮退する(例外を送出しない)。"""
    path = (config_dir or DEFAULT_CONFIG_DIR) / CONFIG_FILE_NAME
    try:
        return IncidentIssueLimits.model_validate(_load_yaml(path))
    except Exception as exc:  # LINE の経路を落とさない。原因の型だけを警告する(値・path は出さない)
        logger.warning(
            "incident_issue_limits unavailable; treating every limit as undecided (%s)",
            type(exc).__name__,
        )
        return undecided_limits()
