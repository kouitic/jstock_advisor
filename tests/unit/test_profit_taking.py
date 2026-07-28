from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    RecommendationType,
    StockType,
    TimingAction,
    TrendClassification,
)
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
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


def test_gain_alone_does_not_trigger_full_profit_take() -> None:
    # 要求仕様9節: 含み益率だけでFULL_PROFIT_TAKEにならない(単一条件ではPARTIALにも
    # 届かない設計)。適正価格超過は15%未満のため、こちらも条件として成立しない。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%(全株利確閾値50%を超過)
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1500"),  # 適正価格超過は約6.7%のみ(閾値未満)
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type != RecommendationType.FULL_PROFIT_TAKE
    assert result.recommendation_type == RecommendationType.WATCH
    assert result.triggered_reasons


def test_gain_and_fair_value_excess_together_trigger_full() -> None:
    # 含み益・適正価格超過の2条件が揃って初めてFULLへ到達する。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%(全株利確閾値50%を超過)
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1200"),  # 適正価格超過は約33.3%(全株利確閾値30%を超過)
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert result.fundamental_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE


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


def test_low_total_yield_alone_only_reaches_watch() -> None:
    # 総合利回り低下という単一条件だけではPARTIAL/FULLへ到達しない(要求仕様9節)。
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
    assert result.recommendation_type == RecommendationType.WATCH


def test_mitigating_factors_downgrade_full_to_partial() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%、適正価格超過約33.3% -> raw FULL(2条件)
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1200"),
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
    assert prices.partial_profit_start_price is not None
    assert prices.recommended_limit_price is not None
    assert prices.full_profit_consideration_price is not None
    assert prices.reevaluation_price_upside is not None
    assert prices.partial_profit_start_price.price == Decimal("1300")
    assert prices.recommended_limit_price.price == Decimal("1500")
    assert prices.full_profit_consideration_price.price == Decimal("1950")
    assert prices.reevaluation_price_upside.price == Decimal("2000")
    assert prices.partial_profit_start_price.price <= prices.full_profit_consideration_price.price


def test_full_take_price_never_below_recommended_limit_price() -> None:
    # 2914(JT)の実際の通知バグの回帰テスト。含み益率(約15.4%)はFULL閾値(50%)に
    # 遠く及ばないため、この判定は適正価格超過(約34.9%、FULL水準30%以上)から
    # 発火する。旧実装はここで「利確推奨価格」を無関係な含み益軸の値で算出した
    # 上で現在値へ丸め、「全株利確検討価格」は無条件で現在値超の値を返していた。
    # 新実装では、実際に到達した軸(適正価格)からのみ指値候補を算出し、
    # 全株利確検討価格を常に下回らないことを保証する。
    # 適正価格超過(単独条件)だけではFULLへ届かない新設計のため、総合利回りの
    # 大幅低下(1.8% < strong_caution 2.0%)をもう一つの中程度条件として組み合わせる。
    result = evaluate_profit_taking(
        current_price=Decimal("6531"),
        average_purchase_price=Decimal("5660"),  # 含み益率 約15.4%
        shares=100,
        total_purchase_amount=Decimal("566000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("4840"),  # 現在値が適正価格を約34.9%超過(FULL水準)
        current_total_yield_pct=1.8,
        forecast_annual_dividend_per_share=Decimal("242"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
    )
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    prices = result.sell_prices
    assert prices.recommended_limit_price is not None
    assert prices.full_profit_consideration_price is not None
    assert prices.recommended_limit_price.price <= prices.full_profit_consideration_price.price
    # 適正価格軸(FULL水準)から算出された指値候補は、既に現在値を下回っている
    assert prices.recommended_limit_price.price == Decimal("6292")
    assert prices.recommended_limit_price.basis.value == "IMMEDIATE_EXECUTION_REFERENCE"
    assert prices.full_profit_consideration_price.price == Decimal("8490")


def test_recommended_limit_price_is_none_when_only_non_price_axes_trigger() -> None:
    # 総合利回り低下・トレンド悪化という、含み益・適正価格のいずれとも無関係な
    # 2条件でFULLへ到達した場合、無関係な指値候補を捏造せずNone(算出不能)とする。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_DOWNTREND,
        confidence=ConfidenceLevel.MEDIUM,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),  # 含み益率5%(全株利確閾値50%に遠く届かない)
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1100"),  # 適正価格超過はマイナス(条件不成立)
        current_total_yield_pct=1.5,  # strong_caution(2.0%)未満
        forecast_annual_dividend_per_share=Decimal("15"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(momentum=momentum),
    )
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert result.sell_prices.recommended_limit_price is None


def test_uncomputable_prices_are_not_floored_to_current_price() -> None:
    # 適正価格が取得単価・現在株価に比べて大幅に低い場合の回帰テスト。
    # 旧実装はここで現在値へ無条件に切り上げていたが(サンリオの事例で発覚した
    # バグ)、新実装では実際に到達した軸からのみ指値候補を算出し、既に現在値を
    # 下回る水準は「即時執行目安」として明示する(現在値への無条件フォールバックは行わない)。
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
    # 含み益率(1.71%)はWATCH閾値にすら届かないため、含み益軸からの指値は無い。
    # 適正価格軸(FULL水準)から算出された指値候補は、既に現在値を下回っている
    # ため、その実際の計算値(切り上げ前の値)がそのまま表示される。
    assert prices.recommended_limit_price is not None
    assert prices.recommended_limit_price.price == Decimal("637")
    assert prices.recommended_limit_price.basis.value == "IMMEDIATE_EXECUTION_REFERENCE"
    # 「上昇時の再評価価格」は現在値を下回る計算結果になる場合、意味をなさないため
    # 算出不能(None)とする(現在値へのフォールバックは行わない)。
    assert prices.reevaluation_price_upside is None


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


def test_growth_stock_ignores_yield_decline_as_full_take_trigger() -> None:
    # 要求仕様7節: GROWTHは配当・優待利回り低下を利確条件に含めない。
    # 含み益・適正価格軸も届かない状態で利回りだけが低い場合、GROWTHはHOLD/WATCHに留まる
    # (yield条件が全く効かないため、他の銘柄タイプなら発火するWATCHにも届かない)。

    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),  # 含み益5%(watch閾値20%未満)
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1100"),  # 適正価格超過なし
        current_total_yield_pct=1.0,  # 大幅に低いがGROWTHでは条件化されない
        forecast_annual_dividend_per_share=Decimal("10"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(stock_types=[StockType.GROWTH]),
    )
    assert result.recommendation_type == RecommendationType.HOLD


def test_income_stock_does_not_full_take_on_yield_decline_alone_above_minimum() -> None:
    # 要求仕様7節: INCOMEは最低利回りを維持していれば利回り低下だけで全利確しない。
    # ここではcaution閾値(2.5%)をわずかに下回るが、strong_caution(2.0%)は上回っており、
    # 単一のPARTIAL相当条件のみでFULLには届かないことを確認する。

    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1100"),
        current_total_yield_pct=2.4,  # caution(2.5%)未満だが、strong_caution(2.0%)以上
        forecast_annual_dividend_per_share=Decimal("24"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(stock_types=[StockType.INCOME]),
    )
    assert result.recommendation_type != RecommendationType.FULL_PROFIT_TAKE


def test_uptrend_downgrades_fundamental_action_by_one_level() -> None:
    # 要求仕様9節・10節: 上昇トレンド中はfundamental_actionとtiming_actionを分離し、
    # 適正価格レンジ上限を明確に超過していない限り、最大1段階まで判定を緩和する。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.UPTREND, confidence=ConfidenceLevel.MEDIUM
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),  # 含み益60%
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1200"),  # 適正価格超過33.3% -> 2条件でfundamental=FULL
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(momentum=momentum),
    )
    assert result.fundamental_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.timing_action == TimingAction.WAIT_UPTREND_CONTINUES
    assert result.final_action == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.recommendation_type == result.final_action


def test_uptrend_does_not_override_confirmed_hard_overvaluation() -> None:
    # 現在値が適正価格レンジ上限(bull)を明確に超過し、信頼度もLOWでない場合、
    # 上昇トレンドによる判定緩和は禁止する(トレンドだけで割高評価を無効化しない)。

    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_UPTREND, confidence=ConfidenceLevel.HIGH
    )
    fair_value_range = FairValueRange(
        bear=Decimal("1100"),
        neutral=Decimal("1200"),
        bull=Decimal("1300"),
        overall_confidence=ConfidenceLevel.HIGH,
        methods_used=[
            FairValueMethodResult(
                method="target_yield", fair_value=Decimal("1200"), confidence=ConfidenceLevel.HIGH
            )
        ],
        methods_excluded=[],
        usable_for_trading_judgment=True,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # bull(1300円)を明確に超過
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        fair_value=Decimal("1200"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            momentum=momentum, fair_value_range=fair_value_range
        ),
    )
    assert result.fundamental_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.timing_action == TimingAction.WAIT_UPTREND_CONTINUES
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE


def test_trailing_stop_reference_price_surfaced_from_momentum() -> None:
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.NEUTRAL,
        confidence=ConfidenceLevel.MEDIUM,
        trailing_stop_reference_price=Decimal("1400"),
    )
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
        condition_inputs=ProfitTakingConditionInputs(momentum=momentum),
    )
    assert result.sell_prices.trailing_stop_reference_price is not None
    assert result.sell_prices.trailing_stop_reference_price.price == Decimal("1400")
