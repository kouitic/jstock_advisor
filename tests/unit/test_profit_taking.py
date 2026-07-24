from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    compute_unrealized_pnl,
    evaluate_profit_taking,
)

_CONFIG = load_config()


def test_compute_unrealized_pnl() -> None:
    pnl = compute_unrealized_pnl(
        current_price=Decimal("1200"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("2000"),
        cumulative_benefit_value_received=Decimal("1000"),
    )
    assert pnl.unrealized_pnl == Decimal("20000")
    assert pnl.unrealized_pnl_pct == 20.0
    assert pnl.total_return_including_income == Decimal("23000")
    assert pnl.total_return_pct == 23.0


def test_no_signal_is_hold() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1100"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.HOLD


def test_full_gain_triggers_full_profit_take() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1500"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert result.triggered_reasons


def test_watch_level_for_moderate_gain() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("1220"),  # +22%
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1400"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.WATCH


def test_low_total_yield_triggers_signal_even_without_large_gain() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1100"),
        current_total_yield_pct=1.5,  # strong_caution(2.0%)未満
        forecast_annual_dividend_per_share=Decimal("15"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE


def test_mitigating_factors_downgrade_full_to_partial() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60% -> raw FULL
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1500"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=3),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.mitigating_factors_applied


def test_mitigating_factors_floor_at_watch_not_hold() -> None:
    # 何らかの利確シグナルが実際に発生している場合、緩和要因を積み上げても
    # HOLD(無評価)までは完全に打ち消さず、最低でもWATCH(監視継続)にとどめる。
    result = evaluate_profit_taking(
        current_price=Decimal("1220"),  # +22% -> raw WATCH
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1400"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(
            continuous_dividend_increase_years=3,
            is_progressive_or_doe_policy=True,
        ),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.WATCH
    assert len(result.mitigating_factors_applied) >= 2


def test_mitigating_factors_floor_does_not_apply_when_no_signal() -> None:
    # そもそも利確シグナルが発生していなければHOLDのまま(フロアの誤発動を防ぐ)
    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1100"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(
            continuous_dividend_increase_years=3,
            is_progressive_or_doe_policy=True,
        ),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.HOLD


def test_sell_price_levels_are_populated() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1500"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    prices = result.sell_prices
    assert prices.partial_take_start is not None
    assert prices.profit_take_recommended is not None
    assert prices.full_take_consider is not None
    assert prices.reassessment_price is not None
    assert prices.partial_take_start.price <= prices.full_take_consider.price


def test_sell_price_levels_never_fall_below_current_price() -> None:
    # 適正価格が取得単価・現在株価に比べて大幅に低い場合、一部利確開始価格・
    # 利確推奨価格・再評価価格が現在株価を下回る「既に通過済みで意味をなさない」
    # 水準になってしまうバグの回帰テスト。現在株価を下限にクリップし、
    # かつ3水準が単調非減少であることを確認する。
    current_price = Decimal("1159.5")
    result = evaluate_profit_taking(
        current_price=current_price,
        average_purchase_price=Decimal("1140"),
        shares=100,
        total_purchase_amount=Decimal("114000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("490"),
        current_total_yield_pct=1.38,
        forecast_annual_dividend_per_share=Decimal("16"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    prices = result.sell_prices
    assert prices.partial_take_start is not None
    assert prices.profit_take_recommended is not None
    assert prices.full_take_consider is not None
    assert prices.reassessment_price is not None
    assert prices.partial_take_start.price >= current_price
    assert prices.profit_take_recommended.price >= current_price
    assert prices.full_take_consider.price >= current_price
    assert prices.reassessment_price.price >= current_price
    assert prices.partial_take_start.price <= prices.profit_take_recommended.price
    assert prices.profit_take_recommended.price <= prices.full_take_consider.price
    # 適正価格ベースの水準がFULL(30%超過)にすら達しない今回のケースでは、
    # 全株利確検討価格は現在株価を明確に上回るはず(取得単価ベースの水準が支配的)
    assert prices.full_take_consider.price > current_price


def test_no_signal_when_unrealized_loss_despite_fair_value_excess() -> None:
    # 含み損の状態では「利確」が成立しないため、株価が適正価格を大幅に超過
    # していても利確シグナルは出さない(株価下落による売却判断はsell_signal側の
    # 投資前提悪化判定の担当であり、本ロジックの対象外)。
    result = evaluate_profit_taking(
        current_price=Decimal("1151"),
        average_purchase_price=Decimal("3775"),  # 現在価格より高く、含み損
        shares=100,
        total_purchase_amount=Decimal("377500"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("490"),  # 現在価格が適正価格を134.9%超過
        current_total_yield_pct=2.43,
        forecast_annual_dividend_per_share=Decimal("28"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.HOLD
    assert result.pnl.unrealized_pnl_pct < 0


def test_no_signal_when_unrealized_loss_despite_low_total_yield() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("900"),
        average_purchase_price=Decimal("1000"),  # 含み損
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1100"),
        current_total_yield_pct=1.5,  # strong_caution(2.0%)未満
        forecast_annual_dividend_per_share=Decimal("15"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.HOLD


def test_signal_fires_with_even_minimal_unrealized_gain() -> None:
    # 含み益さえあれば(わずかでも)、適正価格超過による利確判定は従来通り機能する
    result = evaluate_profit_taking(
        current_price=Decimal("1001"),
        average_purchase_price=Decimal("1000"),  # +0.1%のわずかな含み益
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("500"),  # 適正価格を100%超過
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type != RecommendationType.HOLD
