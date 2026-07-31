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


def test_margin_of_safety_ratios_are_ordered() -> None:
    """固定95%/90%/85%方式(旧recommended_buy_price)は2026-07 BUYパイプライン
    再設計で廃止し、信頼度別のmargin_of_safety(安全余裕率)へ置き換えた。
    """
    config = load_config()
    high = config.buy_decision.margin_of_safety.confidence.high
    assert high.entry < high.standard < high.strong < 1.0


def test_maximum_margin_is_ordered_and_within_bounds() -> None:
    """BUYパイプライン第2次修正(2026-07)。要求仕様5節: 段階別上限は
    entry <= standard <= strong <= 0.45を満たす必要がある。
    """
    config = load_config()
    maximum_margin = config.buy_decision.margin_of_safety.maximum_margin
    assert 0 < maximum_margin.entry <= maximum_margin.standard <= maximum_margin.strong <= 0.45


def test_adjustment_multipliers_are_ordered() -> None:
    config = load_config()
    multipliers = config.buy_decision.margin_of_safety.adjustment_multipliers
    assert 0 <= multipliers.entry <= multipliers.standard <= multipliers.strong


def test_minimum_margin_gap_is_within_bounds() -> None:
    config = load_config()
    assert 0 <= config.buy_decision.margin_of_safety.minimum_margin_gap < 0.45


def test_notification_send_empty_summary_is_boolean() -> None:
    config = load_config()
    assert isinstance(config.notification.send_empty_summary, bool)


def test_maximum_margin_rejects_descending_order() -> None:
    from jstock_advisor.config.models import MarginOfSafetyMaximumTiers

    with pytest.raises(ValidationError):
        MarginOfSafetyMaximumTiers(entry=0.40, standard=0.30, strong=0.45)


def test_maximum_margin_rejects_value_above_0_45() -> None:
    from jstock_advisor.config.models import MarginOfSafetyMaximumTiers

    with pytest.raises(ValidationError):
        MarginOfSafetyMaximumTiers(entry=0.30, standard=0.38, strong=0.50)


def test_adjustment_multipliers_reject_descending_order() -> None:
    from jstock_advisor.config.models import MarginOfSafetyAdjustmentMultipliers

    with pytest.raises(ValidationError):
        MarginOfSafetyAdjustmentMultipliers(entry=1.0, standard=0.75, strong=0.50)


def test_config_rejects_unknown_fields(tmp_path: Path) -> None:
    broken_dir = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, broken_dir)
    screening_path = broken_dir / "screening_rules.yaml"
    screening_path.write_text(
        screening_path.read_text(encoding="utf-8") + "\nunknown_field: 1\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        load_config(broken_dir)


# --- watchlist_screening_rules.yaml(ウォッチリスト自動追加機能) --------------


def test_watchlist_screening_defaults_are_loaded() -> None:
    config = load_config().watchlist_screening
    assert config.enabled is True
    assert config.candidate_universe.provider == "csv"
    assert config.max_watchlist_additions_per_run == 20


def test_watchlist_screening_scoring_weights_sum_to_100() -> None:
    scoring = load_config().watchlist_screening.scoring
    total = (
        scoring.dividend_yield.weight
        + scoring.equity_ratio.weight
        + scoring.payout_ratio.weight
        + scoring.dividend_growth.weight
        + scoring.shareholder_benefit.weight
    )
    assert total == pytest.approx(100.0)


def test_watchlist_screening_scoring_rejects_weights_not_summing_to_100() -> None:
    from jstock_advisor.config.models import (
        DividendGrowthScoringConfig,
        DividendYieldScoringConfig,
        EquityRatioScoringConfig,
        PayoutRatioScoringConfig,
        ShareholderBenefitScoringConfig,
        WatchlistScreeningScoringConfig,
    )

    with pytest.raises(ValidationError, match="配点合計は100点"):
        WatchlistScreeningScoringConfig(
            minimum_total_score=60.0,
            dividend_yield=DividendYieldScoringConfig(
                weight=10.0, zero_at_pct=3.5, full_at_pct=6.0
            ),
            equity_ratio=EquityRatioScoringConfig(
                weight=10.0, zero_at_pct=40.0, full_at_pct=70.0
            ),
            payout_ratio=PayoutRatioScoringConfig(
                weight=10.0, healthy_min_pct=20.0, healthy_max_pct=60.0
            ),
            dividend_growth=DividendGrowthScoringConfig(
                weight=10.0, zero_at_years=0, full_at_years=10
            ),
            shareholder_benefit=ShareholderBenefitScoringConfig(
                weight=10.0, yield_full_at_pct=2.0, presence_only_score_ratio=0.5
            ),
        )


def test_watchlist_screening_rejects_zero_max_watchlist_additions_per_run() -> None:
    from jstock_advisor.config.models import WatchlistScreeningRulesConfig

    base = load_config().watchlist_screening
    with pytest.raises(ValidationError):
        WatchlistScreeningRulesConfig(
            **{**base.model_dump(), "max_watchlist_additions_per_run": 0}
        )


def test_watchlist_screening_rejects_negative_minimum_market_cap() -> None:
    from jstock_advisor.config.models import WatchlistScreeningThresholds

    base = load_config().watchlist_screening.thresholds
    with pytest.raises(ValidationError):
        WatchlistScreeningThresholds(
            **{**base.model_dump(), "minimum_market_cap_yen": -1}
        )


def test_watchlist_screening_rejects_unknown_candidate_universe_provider() -> None:
    from jstock_advisor.config.models import CandidateUniverseConfig

    with pytest.raises(ValidationError):
        CandidateUniverseConfig(provider="prime_market", csv_path="data/universe/x.csv")


def test_watchlist_screening_rejects_unknown_screening_policy() -> None:
    from jstock_advisor.config.models import WatchlistScreeningRulesConfig

    base = load_config().watchlist_screening
    with pytest.raises(ValidationError):
        WatchlistScreeningRulesConfig(
            **{**base.model_dump(), "screening_policy": "some_future_policy"}
        )
