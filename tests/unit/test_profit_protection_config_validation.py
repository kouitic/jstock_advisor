"""ProfitProtectionConfigの相互整合validation(コードレビュー対応2026-08、指摘3)。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import (
    ProfitProtectionCandidateThresholds,
    ProfitProtectionConfig,
    ProfitProtectionStrongThresholds,
)

_DEFAULT_CANDIDATE = {
    "min_current_gain_pct": 20.0,
    "min_drawdown_from_peak_pct": 8.0,
    "min_gain_giveback_ratio_pct": 20.0,
}
_DEFAULT_STRONG = {
    "min_current_gain_pct": 25.0,
    "min_drawdown_from_peak_pct": 10.0,
    "min_gain_giveback_ratio_pct": 30.0,
}


def _config(candidate: dict, strong: dict) -> ProfitProtectionConfig:
    return ProfitProtectionConfig(
        enabled=True,
        candidate=ProfitProtectionCandidateThresholds(**candidate),
        strong=ProfitProtectionStrongThresholds(**strong),
    )


def test_current_production_values_pass() -> None:
    """現行本番値(20/8/20, 25/10/30)はPASSする。"""
    _config(_DEFAULT_CANDIDATE, _DEFAULT_STRONG)


def test_loaded_config_passes_validation() -> None:
    """実際のYAML(profit_taking_rules.yaml)から読み込んだ値もPASSする
    (起動時validationの実効性確認)。"""
    cfg = load_config()
    assert cfg.profit_taking.profit_protection.strong.min_current_gain_pct >= (
        cfg.profit_taking.profit_protection.candidate.min_current_gain_pct
    )


def test_strong_current_gain_below_candidate_rejected() -> None:
    strong = dict(_DEFAULT_STRONG, min_current_gain_pct=15.0)  # candidate(20)未満
    with pytest.raises(ValidationError, match="min_current_gain_pct"):
        _config(_DEFAULT_CANDIDATE, strong)


def test_strong_drawdown_below_candidate_rejected() -> None:
    strong = dict(_DEFAULT_STRONG, min_drawdown_from_peak_pct=5.0)  # candidate(8)未満
    with pytest.raises(ValidationError, match="min_drawdown_from_peak_pct"):
        _config(_DEFAULT_CANDIDATE, strong)


def test_strong_giveback_below_candidate_rejected() -> None:
    strong = dict(_DEFAULT_STRONG, min_gain_giveback_ratio_pct=10.0)  # candidate(20)未満
    with pytest.raises(ValidationError, match="min_gain_giveback_ratio_pct"):
        _config(_DEFAULT_CANDIDATE, strong)


def test_negative_current_gain_rejected() -> None:
    candidate = dict(_DEFAULT_CANDIDATE, min_current_gain_pct=-1.0)
    with pytest.raises(ValidationError, match="min_current_gain_pct"):
        ProfitProtectionCandidateThresholds(**candidate)


def test_negative_drawdown_rejected() -> None:
    candidate = dict(_DEFAULT_CANDIDATE, min_drawdown_from_peak_pct=-1.0)
    with pytest.raises(ValidationError, match="min_drawdown_from_peak_pct"):
        ProfitProtectionCandidateThresholds(**candidate)


def test_drawdown_over_100_rejected() -> None:
    candidate = dict(_DEFAULT_CANDIDATE, min_drawdown_from_peak_pct=101.0)
    with pytest.raises(ValidationError, match="min_drawdown_from_peak_pct"):
        ProfitProtectionCandidateThresholds(**candidate)


def test_giveback_over_100_rejected() -> None:
    strong = dict(_DEFAULT_STRONG, min_gain_giveback_ratio_pct=101.0)
    with pytest.raises(ValidationError, match="min_gain_giveback_ratio_pct"):
        ProfitProtectionStrongThresholds(**strong)


def test_negative_giveback_rejected() -> None:
    strong = dict(_DEFAULT_STRONG, min_gain_giveback_ratio_pct=-1.0)
    with pytest.raises(ValidationError, match="min_gain_giveback_ratio_pct"):
        ProfitProtectionStrongThresholds(**strong)


def test_current_gain_has_no_upper_bound() -> None:
    """株価は取得価格の2倍・3倍になり得るため、min_current_gain_pctには
    上限を設けない。"""
    strong = dict(_DEFAULT_STRONG, min_current_gain_pct=500.0)
    _config(_DEFAULT_CANDIDATE, strong)


def test_strong_exactly_equal_to_candidate_is_allowed() -> None:
    """strong>=candidateの境界(等価)はPASSする。"""
    _config(_DEFAULT_CANDIDATE, dict(_DEFAULT_STRONG, **_DEFAULT_CANDIDATE))
