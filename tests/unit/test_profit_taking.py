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


def _fair_value_range(
    *,
    neutral: Decimal,
    bull: Decimal,
    bear: Decimal,
    overall_confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
    method_count: int = 1,
    usable_for_trading_judgment: bool = True,
) -> FairValueRange:
    methods = [
        FairValueMethodResult(
            method=f"method{i}", fair_value=neutral, confidence=overall_confidence
        )
        for i in range(method_count)
    ]
    return FairValueRange(
        bear=bear,
        neutral=neutral,
        bull=bull,
        overall_confidence=overall_confidence,
        methods_used=methods,
        methods_excluded=[],
        usable_for_trading_judgment=usable_for_trading_judgment,
    )


def _degenerate_fair_value_range(value: Decimal, method_count: int = 1) -> FairValueRange:
    """旧仕様の単一スカラーfair_valueに相当するレンジ(bull=neutral=bear)。

    2026-07仕様レビュー対応により、判定はbull(強気)適正価格を主軸として使うため、
    「旧来のfair_value超過率」を再現したいテストではbull=neutral=bearとする。
    """
    return _fair_value_range(
        neutral=value,
        bull=value,
        bear=value,
        overall_confidence=ConfidenceLevel.MEDIUM,
        method_count=method_count,
    )


def _degenerate_fair_value_range_unusable(value: Decimal, method_count: int = 1) -> FairValueRange:
    """usable_for_trading_judgment=False版の_degenerate_fair_value_range。

    Fair Value使用不能時の売買目安価格ゲート(コードレビュー対応2026-08)の回帰
    テスト専用。
    """
    return _fair_value_range(
        neutral=value,
        bull=value,
        bear=value,
        overall_confidence=ConfidenceLevel.MEDIUM,
        method_count=method_count,
        usable_for_trading_judgment=False,
    )


_FULL_GATE_INPUTS = {
    "industry_model_applied": True,
    "partial_sale_executable": True,
    "days_to_next_earnings_business_days": 10,
    "has_strong_counter_material": False,
}


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
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1100"))
        ),
    )
    assert result.recommendation_type == RecommendationType.HOLD


def test_gain_alone_does_not_trigger_full_profit_take() -> None:
    # 要求仕様9節: 含み益率だけでFULL_PROFIT_TAKEにならない。強気適正価格超過も
    # 25%未満のため、こちらも条件として成立しない。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%(全株利確閾値50%を超過)
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            # 強気適正価格超過は約6.7%のみ(閾値25%未満)
            fair_value_range=_degenerate_fair_value_range(Decimal("1500"))
        ),
    )
    assert result.recommendation_type != RecommendationType.FULL_PROFIT_TAKE
    assert result.recommendation_type == RecommendationType.WATCH
    assert result.triggered_reasons


def test_gain_and_fair_value_excess_together_trigger_full() -> None:
    # 含み益・強気適正価格超過の2条件(中程度条件)が揃って初めてFULLへ到達する。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%(全株利確閾値50%を超過)
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            # 強気適正価格超過は約45.5%(全株利確閾値40%を超過)
            fair_value_range=_degenerate_fair_value_range(Decimal("1100"))
        ),
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
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1400"))
        ),
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
        current_total_yield_pct=1.5,  # strong_caution(2.0%)未満
        forecast_annual_dividend_per_share=Decimal("15"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1100"))
        ),
    )
    assert result.recommendation_type == RecommendationType.WATCH


def test_mitigating_factors_downgrade_full_to_partial() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%、強気適正価格超過約45.5% -> raw FULL(2条件)
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=3),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1100"))
        ),
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
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(
            continuous_dividend_increase_years=3,
            is_progressive_or_doe_policy=True,
        ),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1400"))
        ),
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
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(
            continuous_dividend_increase_years=3,
            is_progressive_or_doe_policy=True,
        ),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1100"))
        ),
    )
    assert result.recommendation_type == RecommendationType.HOLD


def test_sell_price_levels_are_populated_for_partial_and_scoped_to_final_action() -> None:
    # gain=45%(partial以上full未満)+強気適正価格超過31.8%の条件が組み合わさりPARTIALへ到達。
    # PARTIAL判定では一部利確開始価格・推奨指値候補のみを表示し、FULL専用の
    # full_profit_consideration_price/reevaluation_price_upsideは表示しない
    # (要求仕様レビュー対応: final_actionに応じて表示可能な価格フィールドを制限する)。
    result = evaluate_profit_taking(
        current_price=Decimal("1450"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1100"))
        ),
    )
    assert result.final_action == RecommendationType.PARTIAL_PROFIT_TAKE
    prices = result.sell_prices
    assert prices.partial_profit_start_price is not None
    assert prices.partial_profit_start_price.price == Decimal("1300")
    assert prices.recommended_limit_price is not None
    assert prices.recommended_limit_price.price == Decimal("1500")
    assert prices.recommended_limit_price.price_low is not None
    assert prices.recommended_limit_price.price_high is not None
    assert prices.recommended_limit_price.price_low < prices.recommended_limit_price.price_high
    assert prices.full_profit_consideration_price is None
    assert prices.reevaluation_price_upside is None
    assert prices.immediate_execution_price is None


def test_full_profit_take_shows_full_and_immediate_price_fields() -> None:
    # 投資前提が明確に崩れた、という強い条件でFULLへ到達させる(gain単独ではない)。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1500")),
            investment_premise_broken=True,
        ),
    )
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    prices = result.sell_prices
    assert prices.full_profit_consideration_price is not None
    assert prices.full_profit_consideration_price.price == Decimal("2100")
    # 一部利確開始価格(1300円)は現在値(1600円)を下回っているためimmediate扱いになりうるが、
    # PARTIAL専用のrecommended_limit_priceはFULL判定でも指値候補として妥当なため表示される。
    assert prices.partial_profit_start_price is not None


def test_full_profit_take_price_excludes_unusable_fair_value() -> None:
    # LINE通知/監査分離のコードレビュー対応回帰テスト(最重要修正)。
    # test_full_profit_take_shows_full_and_immediate_price_fieldsと同一の入力だが、
    # 適正価格がusable_for_trading_judgment=Falseの場合、判定ロジック側は既に
    # この適正価格を無視している(level_fv=HOLD)にもかかわらず、旧実装は目安価格
    # 構成時だけ無条件にbullを使い、2100円という「判定には使わないと決めた適正
    # 価格」由来の値を提示してしまっていた。新実装では取得単価ベースの候補
    # (gain_full_price=1500円)のみが使われることを確認する。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range_unusable(Decimal("1500")),
            investment_premise_broken=True,
        ),
    )
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    prices = result.sell_prices
    assert prices.full_profit_consideration_price is not None
    assert prices.full_profit_consideration_price.price == Decimal("1500")
    # 全株利確条件は取得単価ベースの候補(1500円)のみで成立しており、既に現在値
    # (1600円)を下回っているため、即時執行目安として現在値が提示される。
    assert prices.immediate_execution_price is not None
    assert prices.immediate_execution_price.price == Decimal("1600")


def test_full_take_price_never_below_recommended_limit_price() -> None:
    # 2914(JT)の実際の通知バグの回帰テスト。含み益率(約15.4%)はFULL閾値(50%)に
    # 遠く及ばないため、この判定は強気適正価格超過(約34.9%)から発火する。
    # 旧実装はここで「利確推奨価格」を無関係な含み益軸の値で算出した上で現在値へ丸め、
    # 「全株利確検討価格」は無条件で現在値超の値を返していた。
    # 新実装では、実際に到達した軸(適正価格)からのみ指値候補を算出し、
    # 全株利確検討価格を常に下回らないことを保証する。
    # 強気適正価格超過(単独条件)だけではFULLへ届かない新設計のため、総合利回りの
    # 大幅低下(1.8% < strong_caution 2.0%)をもう一つの中程度条件として組み合わせる。
    result = evaluate_profit_taking(
        current_price=Decimal("6531"),
        average_purchase_price=Decimal("5660"),  # 含み益率 約15.4%
        shares=100,
        total_purchase_amount=Decimal("566000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=1.8,
        forecast_annual_dividend_per_share=Decimal("242"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            # 強気適正価格超過は約42.0%(FULL中程度条件の水準40%を超過)
            fair_value_range=_degenerate_fair_value_range(Decimal("4600"))
        ),
    )
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    prices = result.sell_prices
    assert prices.recommended_limit_price is not None
    assert prices.full_profit_consideration_price is not None
    assert prices.recommended_limit_price.price <= prices.full_profit_consideration_price.price
    # 適正価格軸(FULL水準)から算出された指値候補は、既に現在値を下回っている
    assert prices.recommended_limit_price.price == Decimal("6440")
    assert prices.recommended_limit_price.basis.value == "IMMEDIATE_EXECUTION_REFERENCE"
    assert prices.full_profit_consideration_price.price == Decimal("8490")


def test_recommended_limit_price_is_none_when_only_non_price_axes_trigger() -> None:
    # 総合利回り低下・トレンド悪化という、含み益・適正価格のいずれとも無関係な
    # 2条件でFULLへ到達した場合、無関係な指値候補を捏造せずNone(算出不能)とする。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_DOWNTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),  # 含み益率5%(全株利確閾値50%に遠く届かない)
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=1.5,  # strong_caution(2.0%)未満
        forecast_annual_dividend_per_share=Decimal("15"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            momentum=momentum,
            # 強気適正価格超過はマイナス(条件不成立)
            fair_value_range=_degenerate_fair_value_range(Decimal("1100")),
        ),
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
        current_total_yield_pct=1.38,
        forecast_annual_dividend_per_share=Decimal("16"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("490"))
        ),
    )
    prices = result.sell_prices
    # 含み益率(1.71%)はWATCH閾値にすら届かないため、含み益軸からの指値は無い。
    # 適正価格軸(FULL水準)から算出された指値候補は、既に現在値を下回っている
    # ため、その実際の計算値(切り上げ前の値)がそのまま表示される。
    assert prices.recommended_limit_price is not None
    assert prices.recommended_limit_price.price == Decimal("686")
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
        current_total_yield_pct=2.43,
        forecast_annual_dividend_per_share=Decimal("28"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            # 現在価格が適正価格を134.9%超過
            fair_value_range=_degenerate_fair_value_range(Decimal("490"))
        ),
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
        current_total_yield_pct=1.5,  # strong_caution(2.0%)未満
        forecast_annual_dividend_per_share=Decimal("15"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1100"))
        ),
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
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            # 適正価格を100%超過
            fair_value_range=_degenerate_fair_value_range(Decimal("500"))
        ),
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
        current_total_yield_pct=1.0,  # 大幅に低いがGROWTHでは条件化されない
        forecast_annual_dividend_per_share=Decimal("10"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            stock_types=[StockType.GROWTH],
            # 適正価格超過なし
            fair_value_range=_degenerate_fair_value_range(Decimal("1100")),
        ),
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
        current_total_yield_pct=2.4,  # caution(2.5%)未満だが、strong_caution(2.0%)以上
        forecast_annual_dividend_per_share=Decimal("24"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            stock_types=[StockType.INCOME],
            fair_value_range=_degenerate_fair_value_range(Decimal("1100")),
        ),
    )
    assert result.recommendation_type != RecommendationType.FULL_PROFIT_TAKE


def test_uptrend_downgrades_fundamental_action_by_one_level() -> None:
    # 要求仕様9節・10節: 上昇トレンド中はfundamental_actionとtiming_actionを分離し、
    # 適正価格レンジ上限を明確に超過していない限り、最大1段階まで判定を緩和する。
    # 適正価格レンジ(fair_value_range)を与えるとhard_overvalued判定が働いてしまうため、
    # ここでは適正価格と無関係な強い条件(投資前提が崩れた)でFULLへ到達させ、
    # 緩和自体の1段階ダウングレードのみを検証する。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.UPTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),  # 含み益60%
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            momentum=momentum,
            investment_premise_broken=True,
        ),
    )
    assert result.fundamental_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.timing_action == TimingAction.WAIT_UPTREND_CONTINUES
    assert result.final_action == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.recommendation_type == result.final_action


def test_uptrend_does_not_override_confirmed_hard_overvaluation() -> None:
    # 現在値が適正価格レンジ上限(bull)を明確に超過し、信頼度もLOWでない場合、
    # 上昇トレンドによる判定緩和は禁止する(トレンドだけで割高評価を無効化しない)。

    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_UPTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.HIGH,
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
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            momentum=momentum,
            fair_value_range=fair_value_range,
            # 適正価格と無関係な強い条件でFULLへ到達させたうえで、hard_overvalued判定
            # (bull超過)がタイミング緩和を禁止することを検証する。
            investment_premise_broken=True,
        ),
    )
    assert result.fundamental_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.timing_action == TimingAction.WAIT_UPTREND_CONTINUES
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE


def test_trailing_stop_reference_price_surfaced_from_momentum() -> None:
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.NEUTRAL,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
        trailing_stop_reference_price=Decimal("1400"),
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1250"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            momentum=momentum,
            fair_value_range=_degenerate_fair_value_range(Decimal("1100")),
        ),
    )
    # gain=25%は監視水準(20%)以上のためWATCH以上となり、モメンタム層のトレーリング
    # ストップは付与される(HOLD判定の場合のみ価格提案を一切出さない、レビュー対応)。
    assert result.final_action != RecommendationType.HOLD
    assert result.sell_prices.trailing_stop_reference_price is not None
    assert result.sell_prices.trailing_stop_reference_price.price == Decimal("1400")


# --- 中立適正価格単独でのFULL条件廃止(要求仕様レビュー対応) ---------------------


def test_neutral_fair_value_expected_return_alone_does_not_trigger_full() -> None:
    # forward_return = 800/1010-1 ≒ -20.8%(閾値以下)だが、信頼度MEDIUM・手法1件のみ
    # のため強い条件の要件を満たさず、単独ではFULLへ到達しない。
    # 「利確」は含み益がある場合のみ成立するため、現在値は取得価格をわずかに上回る。
    fv_range = _fair_value_range(
        neutral=Decimal("800"),
        bull=Decimal("900"),
        bear=Decimal("750"),
        overall_confidence=ConfidenceLevel.MEDIUM,
        method_count=1,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1010"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=None,
        forecast_annual_dividend_per_share=None,
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fv_range, **_FULL_GATE_INPUTS
        ),
    )
    assert result.final_action != RecommendationType.FULL_PROFIT_TAKE
    assert result.fair_value_used_as_sole_strong_basis is False


def test_fair_value_strong_condition_requires_bull_excess() -> None:
    # 中立適正価格の期待リターンは閾値以下だが、現在値がbullを超過していないため
    # 強い条件を満たさない(手法数・信頼度・追加ゲートは満たす)。
    fv_range = _fair_value_range(
        neutral=Decimal("800"),
        bull=Decimal("1200"),  # 現在値1010はbull未満
        bear=Decimal("750"),
        overall_confidence=ConfidenceLevel.HIGH,
        method_count=3,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1010"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=None,
        forecast_annual_dividend_per_share=None,
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fv_range,
            guidance_revision_disclosed=True,
            fair_value_reflects_latest_earnings=True,
            **_FULL_GATE_INPUTS,
        ),
    )
    assert result.fair_value_used_as_sole_strong_basis is False


def test_fair_value_strong_condition_requires_high_confidence() -> None:
    fv_range = _fair_value_range(
        neutral=Decimal("800"),
        bull=Decimal("900"),
        bear=Decimal("750"),
        overall_confidence=ConfidenceLevel.MEDIUM,  # HIGHでない
        method_count=3,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1010"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=None,
        forecast_annual_dividend_per_share=None,
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fv_range,
            guidance_revision_disclosed=True,
            fair_value_reflects_latest_earnings=True,
            **_FULL_GATE_INPUTS,
        ),
    )
    assert result.final_action != RecommendationType.FULL_PROFIT_TAKE
    assert result.fair_value_used_as_sole_strong_basis is False


def test_fair_value_strong_condition_met_with_all_gates_triggers_full() -> None:
    # 「利確」は含み益がある場合のみ成立する設計のため、現在値は取得価格をわずかに
    # (gain軸単独ではWATCH閾値にすら届かない程度に)上回る値にする。
    # bull_excess_margin_pct_for_full(40%)を満たすため、bullは現在値の70%程度に設定する。
    fv_range = _fair_value_range(
        neutral=Decimal("650"),
        bull=Decimal("700"),
        bear=Decimal("600"),
        overall_confidence=ConfidenceLevel.HIGH,
        method_count=3,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1010"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=None,
        forecast_annual_dividend_per_share=None,
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fv_range,
            guidance_revision_disclosed=True,
            fair_value_reflects_latest_earnings=True,
            **_FULL_GATE_INPUTS,
        ),
    )
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.fair_value_used_as_sole_strong_basis is True


def test_fair_value_strong_condition_blocked_without_industry_model() -> None:
    # 要求仕様§5・§7: 業種別適正価格モデル未適用の場合、他の条件をすべて満たしても
    # 適正価格単独の強い条件は成立しない(HIGH信頼度・全ゲート適合でもindustry_model
    # だけが欠けているケースの回帰テスト)。
    fv_range = _fair_value_range(
        neutral=Decimal("650"),
        bull=Decimal("700"),
        bear=Decimal("600"),
        overall_confidence=ConfidenceLevel.HIGH,
        method_count=3,
    )
    gates_without_industry_model = dict(_FULL_GATE_INPUTS, industry_model_applied=False)
    result = evaluate_profit_taking(
        current_price=Decimal("1010"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=None,
        forecast_annual_dividend_per_share=None,
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fv_range,
            guidance_revision_disclosed=True,
            fair_value_reflects_latest_earnings=True,
            **gates_without_industry_model,
        ),
    )
    assert result.final_action != RecommendationType.FULL_PROFIT_TAKE
    assert result.fair_value_used_as_sole_strong_basis is False


# --- 総合利回り再評価価格(配当+優待、要求仕様レビュー対応) ---------------------


def test_benefit_eligible_reevaluation_price_none_when_value_unavailable() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("637"),
        average_purchase_price=Decimal("578"),
        shares=800,
        total_purchase_amount=Decimal("462400"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=1.5,
        forecast_annual_dividend_per_share=Decimal("22"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            investment_premise_broken=True,
            fair_value_range=_degenerate_fair_value_range(Decimal("498")),
        ),
        annual_benefit_value_at_min_lot=None,  # 優待対象だが評価額が取得できない
        benefit_min_shares_required=100,
        is_benefit_eligible=True,
    )
    assert result.sell_prices.reevaluation_price_upside is None


def test_benefit_eligible_reevaluation_price_uses_total_yield() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("637"),
        average_purchase_price=Decimal("578"),
        shares=800,
        total_purchase_amount=Decimal("462400"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=1.5,
        forecast_annual_dividend_per_share=Decimal("22"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            investment_premise_broken=True,
            fair_value_range=_degenerate_fair_value_range(Decimal("498")),
        ),
        annual_benefit_value_at_min_lot=Decimal("3000"),
        benefit_min_shares_required=100,
        is_benefit_eligible=True,
    )
    p = result.sell_prices.reevaluation_price_upside
    assert p is not None
    assert p.basis_type is not None
    assert p.basis_type.value == "TOTAL_YIELD_THRESHOLD"


def test_non_benefit_stock_reevaluation_price_uses_dividend_yield_only() -> None:
    result = evaluate_profit_taking(
        current_price=Decimal("1000"),
        average_purchase_price=Decimal("900"),
        shares=100,
        total_purchase_amount=Decimal("90000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=1.5,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            investment_premise_broken=True,
            fair_value_range=_degenerate_fair_value_range(Decimal("900")),
        ),
        is_benefit_eligible=False,
    )
    p = result.sell_prices.reevaluation_price_upside
    assert p is not None
    assert p.basis_type is not None
    assert p.basis_type.value == "DIVIDEND_YIELD_THRESHOLD"


# --- 利確判定エンジン再レビュー対応(2026-07): 強気適正価格主軸+MEDIUM厳格化 ------


def test_current_price_vs_fair_value_pct_uses_actual_current_price() -> None:
    # 要求仕様§1: 現在株価の割高率は、監視開始価格等の閾値ベースの価格ではなく、
    # 必ず実際の現在株価とfair_value_neutral/bullの比率から算出する。
    fv_range = _fair_value_range(
        neutral=Decimal("498"), bull=Decimal("657"), bear=Decimal("390"), method_count=4
    )
    result = evaluate_profit_taking(
        current_price=Decimal("641"),
        average_purchase_price=Decimal("578"),
        shares=800,
        total_purchase_amount=Decimal("462400"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=1.5,
        forecast_annual_dividend_per_share=Decimal("22"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(fair_value_range=fv_range),
    )
    assert result.current_price_vs_neutral_fair_value_pct is not None
    assert abs(result.current_price_vs_neutral_fair_value_pct - 28.71) < 0.5
    assert result.current_price_vs_bull_fair_value_pct is not None
    assert result.current_price_vs_bull_fair_value_pct < 0  # 強気適正価格は未超過


def test_current_price_below_neutral_has_no_valuation_concern() -> None:
    # 現在値が中立適正価格以下の場合、適正価格上の割高懸念は生じない(HOLD相当)。
    fv_range = _fair_value_range(
        neutral=Decimal("700"), bull=Decimal("900"), bear=Decimal("500"), method_count=3
    )
    result = evaluate_profit_taking(
        current_price=Decimal("620"),
        average_purchase_price=Decimal("578"),
        shares=800,
        total_purchase_amount=Decimal("462400"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("22"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(fair_value_range=fv_range),
    )
    assert result.recommendation_type == RecommendationType.HOLD


def test_medium_confidence_alone_does_not_reach_partial() -> None:
    # 要求仕様§5: MEDIUM信頼度で強気適正価格を25%以上超過していても、業種別モデル
    # 未適用など追加ゲートを満たさなければPARTIALへ格上げしない(WATCHにとどめる)。
    # 含み益(20%)は一部利確基準(30%)未満に抑え、強気適正価格超過が唯一の条件になる
    # ようにする(gain条件と組み合わさって通常経路でPARTIALへ到達しないようにするため)。
    fv_range = _fair_value_range(
        neutral=Decimal("500"),
        bull=Decimal("600"),
        bear=Decimal("400"),
        overall_confidence=ConfidenceLevel.MEDIUM,
        method_count=4,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("780"),  # bull(600)を30%超過
        average_purchase_price=Decimal("650"),  # 含み益20%(一部利確基準30%未満)
        shares=800,
        total_purchase_amount=Decimal("462400"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("22"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fv_range,
            fair_value_reflects_latest_earnings=True,
            days_to_next_earnings_business_days=10,
            partial_sale_executable=True,
            industry_model_applied=False,  # 未適用
        ),
    )
    assert result.recommendation_type == RecommendationType.WATCH


def test_medium_confidence_reaches_partial_when_all_gates_met() -> None:
    # 同条件で業種別モデル適用済み等11ゲートをすべて満たせばPARTIAL相当まで許可する。
    fv_range = _fair_value_range(
        neutral=Decimal("500"),
        bull=Decimal("600"),
        bear=Decimal("400"),
        overall_confidence=ConfidenceLevel.MEDIUM,
        method_count=4,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("780"),  # bull(600)を30%超過
        average_purchase_price=Decimal("500"),  # 含み益56%(一部利確基準以上)
        shares=800,
        total_purchase_amount=Decimal("400000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("22"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fv_range,
            fair_value_reflects_latest_earnings=True,
            days_to_next_earnings_business_days=10,
            partial_sale_executable=True,
            industry_model_applied=True,
            has_strong_counter_material=False,
        ),
    )
    assert result.recommendation_type in (
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.FULL_PROFIT_TAKE,
    )
