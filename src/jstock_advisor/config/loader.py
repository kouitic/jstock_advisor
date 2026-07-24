"""config/ 配下のYAML/JSONファイルを読み込み、AppConfig(pydantic)に変換する。

設定ファイルはユーザーが直接編集する運用を前提とするため(要求仕様14節)、
読み込み時にスキーマ検証を行い、不正な値があれば起動時に例外を送出する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml

from jstock_advisor.config.models import (
    AppConfig,
    DataValidationRulesConfig,
    HolidayCalendarConfig,
    NotificationRulesConfig,
    ProfitTakingRulesConfig,
    ScheduleConfig,
    ScoringWeightsConfig,
    ScreeningRulesConfig,
    SellRulesConfig,
    ValuationRulesConfig,
)

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def _strip_comment_keys(value: Any) -> Any:
    """先頭が"_"のキー(コメント用途)を再帰的に除去する。"""
    if isinstance(value, dict):
        return {
            key: _strip_comment_keys(val) for key, val in value.items() if not key.startswith("_")
        }
    if isinstance(value, list):
        return [_strip_comment_keys(item) for item in value]
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"設定ファイルの形式が不正です(辞書ではありません): {path}")
    return cast(dict[str, Any], _strip_comment_keys(data))


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"設定ファイルの形式が不正です(辞書ではありません): {path}")
    return cast(dict[str, Any], _strip_comment_keys(data))


def load_config(config_dir: Path | None = None) -> AppConfig:
    """config_dir配下の全設定ファイルを読み込み、検証済みのAppConfigを返す。"""
    directory = config_dir or DEFAULT_CONFIG_DIR

    screening = ScreeningRulesConfig.model_validate(_load_yaml(directory / "screening_rules.yaml"))
    valuation = ValuationRulesConfig.model_validate(_load_yaml(directory / "valuation_rules.yaml"))
    profit_taking = ProfitTakingRulesConfig.model_validate(
        _load_yaml(directory / "profit_taking_rules.yaml")
    )
    sell = SellRulesConfig.model_validate(_load_yaml(directory / "sell_rules.yaml"))
    scoring = ScoringWeightsConfig.model_validate(_load_yaml(directory / "scoring_weights.yaml"))
    schedule = ScheduleConfig.model_validate(_load_yaml(directory / "schedule.yaml"))
    notification = NotificationRulesConfig.model_validate(
        _load_yaml(directory / "notification_rules.yaml")
    )
    data_validation = DataValidationRulesConfig.model_validate(
        _load_yaml(directory / "data_validation_rules.yaml")
    )
    holiday_calendar = HolidayCalendarConfig.model_validate(
        _load_json(directory / "holiday_calendar.json")
    )

    return AppConfig(
        screening=screening,
        valuation=valuation,
        profit_taking=profit_taking,
        sell=sell,
        scoring=scoring,
        schedule=schedule,
        notification=notification,
        data_validation=data_validation,
        holiday_calendar=holiday_calendar,
    )
