import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import DEFAULT_CONFIG_DIR, load_config
from jstock_advisor.config.models import AppConfig


def test_load_config_returns_valid_app_config() -> None:
    config = load_config()
    assert isinstance(config, AppConfig)


def test_screening_default_min_total_yield_is_3_5_pct() -> None:
    config = load_config()
    assert config.screening.total_yield.min_total_yield_pct == 3.5


def test_scoring_weights_sum_to_100() -> None:
    config = load_config()
    weights = config.scoring.weights.model_dump()
    assert sum(weights.values()) == pytest.approx(100.0)


def test_recommended_buy_price_ratios_are_ordered() -> None:
    config = load_config()
    p = config.valuation.recommended_buy_price
    assert p.aggressive_buy_ratio < p.standard_buy_ratio < p.tentative_buy_ratio < 1.0


def test_config_rejects_unknown_fields(tmp_path: Path) -> None:
    broken_dir = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, broken_dir)
    screening_path = broken_dir / "screening_rules.yaml"
    screening_path.write_text(
        screening_path.read_text(encoding="utf-8") + "\nunknown_field: 1\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        load_config(broken_dir)
