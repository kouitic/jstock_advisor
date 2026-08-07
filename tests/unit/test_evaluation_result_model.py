"""EvaluationResultのhorizon_business_days/horizon_calendar_days排他制約テスト
(振り返り機能改修)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.domain.entities.enums import EvaluationLabel
from jstock_advisor.domain.entities.evaluation import EvaluationResult

_NOW = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)


def _base_kwargs() -> dict:
    return {
        "evaluation_id": "e-1",
        "recommendation_id": "r-1",
        "evaluated_at": _NOW,
        "evaluation_date": _NOW.date(),
        "price_at_evaluation": Decimal("1100"),
        "price_return_pct": 10.0,
        "evaluation_label": EvaluationLabel.SUCCESS,
        "label_evidence": "test",
    }


def test_business_days_only_is_valid() -> None:
    result = EvaluationResult(**_base_kwargs(), horizon_business_days=20)
    assert result.horizon_business_days == 20
    assert result.horizon_calendar_days is None


def test_calendar_days_only_is_valid() -> None:
    result = EvaluationResult(**_base_kwargs(), horizon_calendar_days=7)
    assert result.horizon_calendar_days == 7
    assert result.horizon_business_days is None


def test_both_set_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EvaluationResult(**_base_kwargs(), horizon_business_days=20, horizon_calendar_days=7)


def test_neither_set_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EvaluationResult(**_base_kwargs())


def test_existing_json_with_only_horizon_business_days_still_loads() -> None:
    """既存データ(horizon_calendar_daysフィールドが無いJSON)がそのまま読み込める
    こと(後方互換、model_validate経由)。"""
    payload = {**_base_kwargs(), "horizon_business_days": 60}
    result = EvaluationResult.model_validate(payload)
    assert result.horizon_business_days == 60
    assert result.horizon_calendar_days is None
