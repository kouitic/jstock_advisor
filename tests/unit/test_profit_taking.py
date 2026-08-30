import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    IndustryClassification,
    ProfitTakingIndustrySector,
    RecommendationType,
    StockType,
    TimingAction,
    TrendClassification,
)
from jstock_advisor.domain.entities.holding import PurchaseLot
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.signals.profit_protection import compute_profit_protection_metrics
from jstock_advisor.domain.signals.profit_taking import (
    InvalidProfitTakingInputError,
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    compute_unrealized_pnl,
    evaluate_profit_taking,
)
from jstock_advisor.interfaces.types import PriceBar

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
    # コードレビュー対応(2026-08、上値余地の導入): 含み益率(gain)×上値余地
    # (ceiling_priceまでの距離、upside_pct)の基本マトリクスで、gain>=25%かつ
    # upside<5%であれば単独でFULLへ到達する(他の独立条件は不要)。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%
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
            # 上値余地(upside_pct)は約1.25%(FULL上限5%未満)
            fair_value_range=_fair_value_range(
                neutral=Decimal("1560"), bull=Decimal("1620"), bear=Decimal("1500"), method_count=3
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert result.fundamental_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.fair_value_action_usable is True
    assert result.upside_pct is not None and result.upside_pct < 5.0
    assert result.origin == "PRICE_POSITION"


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
    # 価格マトリクス由来(origin=PRICE_POSITION)のraw FULLは、緩和要因により
    # 最大1段階(FULL->PARTIAL)まで弱められる。ただしorigin別floor(§4-2)により
    # PARTIAL未満(WATCH等)へはこれ以上落ちない(他のテストで別途確認)。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%、上値余地約1.25% -> raw FULL(PRICE_POSITION)
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
            fair_value_range=_fair_value_range(
                neutral=Decimal("1560"), bull=Decimal("1620"), bear=Decimal("1500"), method_count=3
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
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
    # 含み益率32%×上値余地約7.9%(価格マトリクスのPARTIALゾーン: gain>=20%だが、
    # gain>=25%かつupside<5%というFULL条件はupside>=5%のため満たさない)で
    # PARTIALへ到達する(origin=PRICE_POSITION)。
    # 再コードレビュー対応(2026-08、指摘2・回帰テストA): origin=PRICE_POSITION
    # 由来のPARTIALでは、売却目安価格はceiling_price(1426円)を超えず、現在値
    # (1320円)付近の実行可能な指値とする(旧gain+50%の1500円は使わない)。
    # FULL専用のfull_profit_consideration_price/reevaluation_price_upsideは
    # 表示しない。
    result = evaluate_profit_taking(
        current_price=Decimal("1320"),  # +32%
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
            fair_value_range=_fair_value_range(
                neutral=Decimal("1300"), bull=Decimal("1426"), bear=Decimal("1200"), method_count=3
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )
    assert result.final_action == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.origin == "PRICE_POSITION"
    assert result.fair_value_action_usable is True
    assert result.ceiling_price == Decimal("1426")
    prices = result.sell_prices
    assert prices.recommended_limit_price is not None
    assert prices.recommended_limit_price.price == Decimal("1320")
    assert prices.recommended_limit_price.price <= result.ceiling_price
    assert prices.recommended_limit_price.basis.value == "IMMEDIATE_EXECUTION_REFERENCE"
    assert prices.full_profit_consideration_price is None
    assert prices.reevaluation_price_upside is None
    assert prices.immediate_execution_price is None


def test_full_profit_take_shows_full_and_immediate_price_fields() -> None:
    # 投資前提が明確に崩れた、という強い条件でFULLへ到達させる(gain単独ではない、
    # origin=FUNDAMENTAL_CRITICAL_RISK)。
    # 再コードレビュー対応(2026-08、指摘2・回帰テストC): 現在値(1600円)が
    # ceiling_price(1500円)を既に超過している場合、全株利確検討価格・即時執行
    # 目安のいずれも現在値付近を優先し、旧gain+40%等の遠い未来値(2100円)は
    # 使わない。FUNDAMENTAL_CRITICAL_RISK由来は将来の利益目標ではなく現在値
    # 付近の実行目安を優先する。
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
            fair_value_range=_degenerate_fair_value_range(Decimal("1500"), method_count=3),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            investment_premise_broken=True,
        ),
    )
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.origin == "FUNDAMENTAL_CRITICAL_RISK"
    assert result.ceiling_price == Decimal("1500")
    prices = result.sell_prices
    assert prices.full_profit_consideration_price is not None
    assert prices.full_profit_consideration_price.price == Decimal("1600")
    assert prices.immediate_execution_price is not None
    assert prices.immediate_execution_price.price == Decimal("1600")


def test_full_profit_take_price_excludes_unusable_fair_value() -> None:
    # LINE通知/監査分離のコードレビュー対応回帰テスト(最重要修正)。
    # test_full_profit_take_shows_full_and_immediate_price_fieldsと同一の入力だが、
    # 適正価格がusable_for_trading_judgment=Falseの場合、ceiling_priceは利用不能
    # (None)になる。再コードレビュー対応(2026-08、指摘2)後は、ceiling_price
    # 利用不能時は取得単価ベースの旧候補(gain_full_price等)へもフォールバック
    # せず、現在値付近を優先する(FUNDAMENTAL_CRITICAL_RISK由来は将来の利益目標
    # ではなく現在値付近の実行目安を優先する)。
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
    assert result.ceiling_price is None
    prices = result.sell_prices
    assert prices.full_profit_consideration_price is not None
    assert prices.full_profit_consideration_price.price == Decimal("1600")
    assert prices.immediate_execution_price is not None
    assert prices.immediate_execution_price.price == Decimal("1600")


def test_full_take_price_never_below_recommended_limit_price() -> None:
    # 2914(JT)の実際の通知バグの回帰テスト。含み益率(約15.4%)は価格マトリクスの
    # watch閾値(20%)未満のため、価格マトリクス経由ではFULLへ到達しない
    # (コードレビュー対応2026-08、上値余地の導入)。この判定は非価格系の中程度条件
    # (総合利回りの大幅低下1.8%<strong_caution2.0% + 株価トレンドの強い悪化)
    # 2件から発火する(origin=OTHER_CONDITIONS)。
    # 旧実装はここで「利確推奨価格」を無関係な含み益軸の値で算出した上で現在値へ丸め、
    # 「全株利確検討価格」は無条件で現在値超の値を返していた。
    # 新実装では、実際に到達した軸(適正価格)からのみ指値候補を算出し、
    # 全株利確検討価格を常に下回らないことを保証する。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_DOWNTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )
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
            momentum=momentum,
            # 強気適正価格超過は約42.0%(価格フィールド候補選択専用の水準40%を超過、
            # ただしraw_level自体はこの超過率からは決まらない)
            fair_value_range=_degenerate_fair_value_range(Decimal("4600")),
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
    # コードレビュー対応(2026-08、上値余地の導入): origin=PRICE_POSITION/
    # FAIR_VALUE_STRONG/FUNDAMENTAL_CRITICAL_RISKはそれぞれ別途floor/exemptionで
    # 保護される(他のテストで確認)ため、この基本メカニズム自体は、価格・適正価格と
    # 無関係な複数の独立条件のみで到達したPARTIAL(origin=OTHER_CONDITIONS、
    # 総合利回り低下+ポートフォリオ集中超過)で確認する。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.UPTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),  # 含み益5%(価格マトリクスのwatch閾値20%未満)
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=1.5,  # strong_caution未満
        forecast_annual_dividend_per_share=Decimal("15"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            momentum=momentum,
            portfolio_concentration_over_limit=True,
        ),
    )
    assert result.fundamental_action == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.timing_action == TimingAction.WAIT_UPTREND_CONTINUES
    assert result.final_action == RecommendationType.WATCH
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
    # 再コードレビュー対応(2026-08、指摘2): reevaluation_price_upsideは
    # ceiling-aware origin(PRICE_POSITION/FAIR_VALUE_STRONG/FUNDAMENTAL_
    # CRITICAL_RISK)のFULLでは算出しない(ceiling_priceを大きく超える未来値に
    # なりうるため)。このテストはreevaluation_price_upsideの算出式自体
    # (総合利回り/配当利回りの選択)を検証したいので、非価格系の独立条件
    # (総合利回り大幅低下+株価トレンドの強い悪化)2件でFULLへ到達させる
    # (origin=OTHER_CONDITIONS)。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_DOWNTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )
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
            momentum=momentum,
            fair_value_range=_degenerate_fair_value_range(Decimal("498")),
        ),
        annual_benefit_value_at_min_lot=Decimal("3000"),
        benefit_min_shares_required=100,
        is_benefit_eligible=True,
    )
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.origin == "OTHER_CONDITIONS"
    p = result.sell_prices.reevaluation_price_upside
    assert p is not None
    assert p.basis_type is not None
    assert p.basis_type.value == "TOTAL_YIELD_THRESHOLD"


def test_non_benefit_stock_reevaluation_price_uses_dividend_yield_only() -> None:
    # 再コードレビュー対応(2026-08、指摘2): test_benefit_eligible_reevaluation_
    # price_uses_total_yieldと同様、ceiling-aware originを避けて非価格系の独立
    # 条件2件(総合利回り大幅低下+株価トレンドの強い悪化)でFULLへ到達させる
    # (origin=OTHER_CONDITIONS)。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_DOWNTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )
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
            momentum=momentum,
            fair_value_range=_degenerate_fair_value_range(Decimal("900")),
        ),
        is_benefit_eligible=False,
    )
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.origin == "OTHER_CONDITIONS"
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
    # コードレビュー対応(2026-08、上値余地の導入): spread_ratio(bull/bear)は
    # max_fair_value_spread_ratio_for_partial(1.30)以下である必要があるため、
    # bearをbull(600)の1.25倍圏内(480)に調整する(以前のbear=400はspread_ratio
    # 1.5となり、そもそも_fair_value_partial_gate_met自体のゲートを満たさない
    # 設定だった)。
    fv_range = _fair_value_range(
        neutral=Decimal("500"),
        bull=Decimal("600"),
        bear=Decimal("480"),
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


# --- partial_sale_executable=Falseのゲート(コードレビュー対応2026-08、
# PARTIAL数量欠落不具合)。独立条件数経路(1268行目)・価格位置経路(1281行目)の
# 双方が、profit_protection strong経路と同じくpartial_sale_executable
# ゲートを課すことを確認する。保有株数が売買単位以下でodd_lot_trading_
# available=Falseの場合(evaluate_trading_unit_feasibility()参照)に相当する
# 状況をcondition_inputs.partial_sale_executable=Falseで直接再現する
# (evaluate_profit_taking()自体は生の株数/売買単位を受け取らず、呼び出し側が
# 事前計算したpartial_sale_executableのみを入力とするため)。特定銘柄・
# 特定株数のハードコードは行わない。 ---------------------------------------


def test_condition_count_partial_reached_when_partial_sale_executable() -> None:
    # 独立条件数経路(総合利回り低下+ポートフォリオ集中超過の2条件)のみで
    # PARTIALへ到達することを確認する(価格系条件は関与させないよう含み益を
    # 低く抑える)。
    result = evaluate_profit_taking(
        current_price=Decimal("1050"),  # 含み益5%、価格系の閾値には届かない
        average_purchase_price=Decimal("1000"),
        shares=800,
        total_purchase_amount=Decimal("800000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=2.0,  # total_yield_caution_pct(2.5)未満
        forecast_annual_dividend_per_share=Decimal("20"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            portfolio_concentration_over_limit=True,
            partial_sale_executable=True,
        ),
    )
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE


def test_condition_count_partial_blocked_when_partial_sale_not_executable() -> None:
    # 上と全く同じ条件でも、partial_sale_executable=Falseの場合は
    # PARTIAL_PROFIT_TAKEを成立させず、WATCHへ自然にフォールバックする
    # (一部売却が実行不能な保有については、既存のWATCHフォールバック条件
    # (partial_count>=1)がそのまま働く設計)。
    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),
        shares=800,
        total_purchase_amount=Decimal("800000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=2.0,
        forecast_annual_dividend_per_share=Decimal("20"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            portfolio_concentration_over_limit=True,
            partial_sale_executable=False,
        ),
    )
    assert result.recommendation_type != RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.recommendation_type == RecommendationType.WATCH


def test_price_position_partial_reached_when_partial_sale_executable() -> None:
    # 価格位置経路(含み益率×上値余地)のみでPARTIALへ到達することを確認する
    # (独立条件数側の条件は満たさないようにする)。
    result = evaluate_profit_taking(
        current_price=Decimal("1220"),  # 含み益22%(partial_gain_pct=20以上、full=25未満)
        average_purchase_price=Decimal("1000"),
        shares=800,
        total_purchase_amount=Decimal("800000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            # bull=1300 -> 上値余地約6.6%(partial_upside_max_pct=15未満)
            fair_value_range=_fair_value_range(
                neutral=Decimal("1250"), bull=Decimal("1300"), bear=Decimal("1200"), method_count=3
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            partial_sale_executable=True,
        ),
    )
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE


def test_price_position_partial_blocked_when_partial_sale_not_executable() -> None:
    # 上と全く同じ価格位置条件でも、partial_sale_executable=Falseの場合は
    # PARTIAL_PROFIT_TAKEを成立させず、WATCHへ自然にフォールバックする
    # (含み益率がWATCH閾値も上回るため、既存のWATCHフォールバック条件
    # (unrealized_pnl_pct>=watch_gain_threshold)がそのまま働く設計)。
    result = evaluate_profit_taking(
        current_price=Decimal("1220"),
        average_purchase_price=Decimal("1000"),
        shares=800,
        total_purchase_amount=Decimal("800000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_fair_value_range(
                neutral=Decimal("1250"), bull=Decimal("1300"), bear=Decimal("1200"), method_count=3
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            partial_sale_executable=False,
        ),
    )
    assert result.recommendation_type != RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.recommendation_type == RecommendationType.WATCH


def test_profit_protection_strong_gate_still_enforced_alongside_new_gates() -> None:
    # 既存のProfit Protection strong経路(1300行目付近)のpartial_sale_executable
    # ゲートが、今回の2経路への追加ゲートと同じ挙動のまま維持されていることを
    # 確認する(test_profit_taking_profit_protection.py::
    # test_strong_signal_requires_partial_sale_executableの回帰確認と重複する
    # 観点だが、本ファイル内でも一貫性を明示する)。
    from jstock_advisor.domain.signals.profit_protection import ProfitProtectionMetrics

    pp_metrics = ProfitProtectionMetrics(
        insufficient_data_reason=None,
        peak_price_since_entry=Decimal("1500"),
        peak_date=dt.date(2026, 6, 1),
        peak_gain_pct=50.0,
        current_gain_pct=30.0,
        drawdown_from_peak_pct=20.0,
        gain_giveback_ratio_pct=40.0,
        candidate_signal=True,
        strong_signal=True,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1300"),
        average_purchase_price=Decimal("1000"),
        shares=800,
        total_purchase_amount=Decimal("800000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            profit_protection=pp_metrics,
            partial_sale_executable=False,
        ),
    )
    assert result.recommendation_type != RecommendationType.PARTIAL_PROFIT_TAKE


# --- 上値余地(ceiling_price/upside_pct)の導入(コードレビュー対応2026-08)の
# 回帰テスト(§15、A-E) -------------------------------------------------------

_CEILING_FV_RANGE_KWARGS = {
    "neutral": Decimal("1560"),
    "bull": Decimal("1620"),
    "bear": Decimal("1500"),
    "method_count": 3,
}


def test_price_position_ceiling_blocked_for_unknown_industry_sector() -> None:
    # A. 業種不明(UNKNOWN)は「非金融業と確認済み」とはみなさず、業種別モデル
    # 適用済み(industry_model_applied)でない限りceiling_priceを主要根拠として
    # 使わない(UNKNOWNを安全な業種とみなさない)。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%
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
            fair_value_range=_fair_value_range(**_CEILING_FV_RANGE_KWARGS),
            fair_value_reflects_latest_earnings=True,
            industry_sector=ProfitTakingIndustrySector.UNKNOWN,
            industry_model_applied=False,
        ),
    )
    assert result.fair_value_action_usable is False
    assert result.ceiling_price is None
    assert result.upside_pct is None
    assert result.recommendation_type == RecommendationType.WATCH


def test_price_position_ceiling_allowed_for_general_industry_sector() -> None:
    # B. GENERAL_CORPORATE(業種別モデル未対応でも汎用PER/PBR/配当利回りモデルの
    # 前提自体は成り立つと明確に判定できた業種)は、industry_model_applied=False
    # でも他の信頼性ゲート(手法数・spread・最新決算反映等)を満たせばceiling_price
    # を使用できる(再コードレビュー対応2026-08、指摘5: financial_industry.pyの
    # IndustryClassificationで判定する)。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%
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
            fair_value_range=_fair_value_range(**_CEILING_FV_RANGE_KWARGS),
            fair_value_reflects_latest_earnings=True,
            industry_sector=ProfitTakingIndustrySector.GENERAL,
            industry_model_applied=False,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )
    assert result.fair_value_action_usable is True
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE


def test_price_position_ceiling_blocked_for_financial_industry_without_model() -> None:
    # C. 銀行等(汎用モデルの前提が成り立ちにくい業種)は、業種別モデル適用済み
    # (industry_model_applied=True)でない限りceiling_priceを使用できない
    # (再コードレビュー対応2026-08、指摘5: IndustryClassification.FINANCIALで判定)。
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),  # +60%
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
            fair_value_range=_fair_value_range(**_CEILING_FV_RANGE_KWARGS),
            fair_value_reflects_latest_earnings=True,
            industry_sector=ProfitTakingIndustrySector.BANKING,
            industry_model_applied=False,
            industry_classification=IndustryClassification.FINANCIAL,
        ),
    )
    assert result.fair_value_action_usable is False
    assert result.recommendation_type == RecommendationType.WATCH


def test_price_position_ceiling_blocked_for_insurance_and_securities_industry() -> None:
    # 再コードレビュー対応(2026-08、指摘5・回帰テスト): profit_taking_industry.py
    # (銀行・リース金融のみ識別)では保険・証券会社等を金融業として識別できず
    # generic ceilingを利用可能にしてしまう不備があった。financial_industry.py
    # ベースのIndustryClassification.FINANCIALであれば、保険・証券会社も
    # industry_model_applied=Trueでない限りceiling_priceを使用できないことを確認する。
    for classification in (
        IndustryClassification.FINANCIAL,
        IndustryClassification.UNKNOWN,
    ):
        result = evaluate_profit_taking(
            current_price=Decimal("1600"),  # +60%
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
                fair_value_range=_fair_value_range(**_CEILING_FV_RANGE_KWARGS),
                fair_value_reflects_latest_earnings=True,
                industry_sector=ProfitTakingIndustrySector.GENERAL,
                industry_model_applied=False,
                industry_classification=classification,
            ),
        )
        assert result.fair_value_action_usable is False, classification
        assert result.ceiling_price is None, classification


def test_classify_industry_identifies_insurance_and_securities_as_financial() -> None:
    # 再コードレビュー対応(2026-08、指摘5): financial_industry.pyのclassify_industry()
    # が保険・証券会社をFINANCIALとして識別することを確認する(profit_taking_service.py
    # がこの分類結果をceilingゲートへ渡す前提の単体確認)。
    from jstock_advisor.domain.classification.financial_industry import classify_industry

    insurance = classify_industry("Financial Services", "Insurance - Life")
    assert insurance.classification == IndustryClassification.FINANCIAL

    securities = classify_industry("Financial Services", "Capital Markets")
    assert securities.classification == IndustryClassification.FINANCIAL


def test_continuous_dividend_increase_no_longer_blocks_ceiling_usage() -> None:
    # 再コードレビュー対応(2026-08、指摘4): has_strong_counter_material
    # (今期増配または2年以上連続増配)は、以前はfair_value_action_usableを
    # 直接Falseにしていた(ceiling利用禁止とmitigating layerでの二重softening)。
    # 修正後は、Fair Value自体の品質ゲート(手法数・spread・最新決算反映・
    # 業種)を満たせばfair_value_action_usable=Trueとなり、連続増配等の
    # 「売らずに持つ合理性」はmitigating layerでのみ判定を弱める。
    result = evaluate_profit_taking(
        current_price=Decimal("1280"),  # +28%、上値余地約3%(FULL上限5%未満)
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
            fair_value_range=_fair_value_range(
                neutral=Decimal("1250"), bull=Decimal("1318"), bear=Decimal("1200"), method_count=3
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            has_strong_counter_material=True,  # 3年連続増配 -> 反対材料あり
        ),
    )
    # ceiling利用可否は品質ゲートのみで判定され、有効なまま(修正前はFalseだった)。
    assert result.fair_value_action_usable is True
    assert result.ceiling_price == Decimal("1318")
    # raw FULLはmitigating layer(連続増配)で1段階弱められPARTIALになるが、
    # origin floorによりPARTIAL未満へは落ちない(mitigatingでのsoftening自体は
    # 引き続き有効、二重計上ではなくmitigating layerに一本化されたことの確認)。
    assert result.final_action == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.mitigating_factors_applied


def test_origin_floor_price_position_full_survives_mitigating_and_timing_combined() -> None:
    # D. origin=PRICE_POSITIONのraw FULLは、mitigating(連続増配)とtiming
    # (上昇トレンド)の両方が同時に働いても、合計softeningでPARTIAL未満
    # (WATCH等)へは落ちない(mitigating単独の上限だけでは不十分なケースの回帰)。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_UPTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1280"),  # +28%
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
            momentum=momentum,
            # 上値余地は約3%(FULL上限5%未満)
            fair_value_range=_fair_value_range(
                neutral=Decimal("1250"), bull=Decimal("1318"), bear=Decimal("1200"), method_count=3
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )
    assert result.upside_pct is not None and result.upside_pct < 5.0
    assert result.mitigating_factors_applied
    assert result.timing_action == TimingAction.WAIT_UPTREND_CONTINUES
    assert result.final_action != RecommendationType.WATCH
    assert result.final_action == RecommendationType.PARTIAL_PROFIT_TAKE
    # 再コードレビュー対応(2026-08、指摘6): origin floorはmitigating適用直後の
    # fundamental_levelにも適用されるため、fundamental_actionがfinal_actionより
    # 弱く見える矛盾(fundamental=WATCH・final=PARTIAL)が生じない。
    assert result.fundamental_action == RecommendationType.PARTIAL_PROFIT_TAKE


def test_fundamental_critical_risk_exempt_from_all_softening() -> None:
    # E. 投資前提が明確に崩れた等のFUNDAMENTAL_CRITICAL_RISK起源の判定は、
    # 複数の緩和要因や上昇トレンドが同時に成立していても、final_actionが
    # raw_level(FULL)のまま(降格されない)。緩和要因自体は記録されるが、
    # 実際の降格には使われない(監査上の透明性は維持する)。
    momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_UPTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),  # 含み益5%(価格マトリクスのwatch閾値未満)
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(
            continuous_dividend_increase_years=5,
            is_progressive_or_doe_policy=True,
        ),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            momentum=momentum,
            investment_premise_broken=True,
        ),
    )
    assert result.fundamental_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.timing_action == TimingAction.WAIT_UPTREND_CONTINUES
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.mitigating_factors_applied


# --- Issue #30 Phase 1: is_progressive_or_doe_policyの3状態化(bool | None) ---
# SELL/利確側semantics: None(未確認/UNKNOWN)は緩和要因に該当しない
# (=False扱い。判定を弱めない)。判定条件・weight自体は不変。


def _evaluate_with_policy(policy: bool | None):
    return evaluate_profit_taking(
        current_price=Decimal("1220"),  # +22% -> raw WATCH
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(is_progressive_or_doe_policy=policy),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_degenerate_fair_value_range(Decimal("1400"))
        ),
    )


def test_policy_true_applies_mitigation_as_before() -> None:
    result = _evaluate_with_policy(True)
    assert any("累進配当またはDOE方針" in factor for factor in result.mitigating_factors_applied)


def test_policy_false_applies_no_mitigation() -> None:
    result = _evaluate_with_policy(False)
    assert not any(
        "累進配当またはDOE方針" in factor for factor in result.mitigating_factors_applied
    )


def test_policy_none_behaves_exactly_like_false() -> None:
    """None(UNKNOWN)はFalseと同一結果(緩和なし・判定を弱めない)。"""
    false_result = _evaluate_with_policy(False)
    none_result = _evaluate_with_policy(None)
    assert not any(
        "累進配当またはDOE方針" in factor for factor in none_result.mitigating_factors_applied
    )
    assert none_result.recommendation_type == false_result.recommendation_type
    assert none_result.mitigating_factors_applied == false_result.mitigating_factors_applied


# --- Issue #75 Phase B1(2026-08-30): 取得原価が不正な保有の fail-close ----------
#
# 以前は average_purchase_price <= 0 のとき含み損益率だけを 0.0 へ潰していたため、
# has_unrealized_gain が False となり、適正価格を大きく超過していても
# 「利確シグナルに該当する条件がない(HOLD)」として**沈黙で抑止**されていた。
# エラー・警告・判定不能の記録が一切残らず、運用者は正常なHOLDと区別できなかった。
#
# 本節は「判定できたうえでのHOLD」と「判定そのものが成立しない」が
# 型・制御フローで別物であることを固定する。


def _invalid_input_fair_value() -> FairValueRange:
    """現在値が中立適正価格を大幅超過する状況(本来なら利確候補が立つ)。"""
    return _fair_value_range(
        neutral=Decimal("1000"), bull=Decimal("1200"), bear=Decimal("900"), method_count=3
    )


def _evaluate_with_cost(avg: Decimal, total_cost: Decimal):
    return evaluate_profit_taking(
        current_price=Decimal("3000"),
        average_purchase_price=avg,
        shares=100,
        total_purchase_amount=total_cost,
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=1.0,
        forecast_annual_dividend_per_share=Decimal("10"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(fair_value_range=_invalid_input_fair_value()),
    )


def test_zero_average_purchase_price_is_unavailable_not_hold() -> None:
    """T1: avg=0 かつ適正価格大幅超過 → HOLDではなく明示的な判定不能。"""
    with pytest.raises(InvalidProfitTakingInputError) as excinfo:
        _evaluate_with_cost(Decimal("0"), Decimal("0"))

    assert excinfo.value.field == "average_purchase_price"
    assert excinfo.value.value == Decimal("0")
    assert "判定は不能" in excinfo.value.reason


def test_negative_average_purchase_price_is_unavailable() -> None:
    """T2: avg<0 → 判定不能。"""
    with pytest.raises(InvalidProfitTakingInputError) as excinfo:
        _evaluate_with_cost(Decimal("-500"), Decimal("-50000"))

    assert excinfo.value.field == "average_purchase_price"


def test_zero_total_purchase_amount_is_unavailable_even_when_avg_is_positive() -> None:
    """T3: avg>0 だが total_purchase_amount=0 → 判定不能。

    正常なロット集計では両者は連動するため、この状態はHoldingが持つ
    集計キャッシュ同士の内部不整合であり、正常な業務状態ではない。
    「total_return_pctだけ不明として利確判定を続ける」はしない。
    """
    with pytest.raises(InvalidProfitTakingInputError) as excinfo:
        _evaluate_with_cost(Decimal("1000"), Decimal("0"))

    assert excinfo.value.field == "total_purchase_amount"


def test_valid_cost_inputs_are_evaluated_normally() -> None:
    """T4: avg>0 かつ total_purchase_amount>0 → 従来どおり評価される(回帰)。"""
    result = _evaluate_with_cost(Decimal("1000"), Decimal("100000"))

    assert result.pnl.unrealized_pnl_pct == 200.0
    assert result.pnl.total_return_pct == 200.0
    # 判定が実際に成立している(HOLDに固定されていない)。
    assert result.recommendation_type in {
        RecommendationType.HOLD,
        RecommendationType.WATCH,
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.FULL_PROFIT_TAKE,
    }


def test_contradictory_unrealized_pnl_is_never_constructed() -> None:
    """T5: avg<=0 のとき「金額は含み益・率は0.0%」という矛盾DTOを生成しない。

    以前は unrealized_pnl=+300,000円 / unrealized_pnl_pct=0.0 という自己矛盾した
    UnrealizedPnl が下流(判定ゲート・監査・通知文言)へ流れていた。
    compute_unrealized_pnl を直接呼ぶ経路でも生成されないことを固定する。
    """
    for avg, cost in ((Decimal("0"), Decimal("0")), (Decimal("-500"), Decimal("-50000"))):
        with pytest.raises(InvalidProfitTakingInputError):
            compute_unrealized_pnl(
                current_price=Decimal("3000"),
                average_purchase_price=avg,
                shares=100,
                total_purchase_amount=cost,
                cumulative_dividend_received=Decimal("0"),
                cumulative_benefit_value_received=Decimal("0"),
            )


def test_profit_taking_and_profit_protection_agree_on_invalid_cost() -> None:
    """T6: 同じ avg<=0 について両エンジンが「判定不能」で一致する。

    DTOの型までは統一しない(profit_protectionはmetrics + reason、
    profit_takingは例外)。揃えるのは business contract である。
    """
    calendar = BusinessCalendar.from_config(_CONFIG.holiday_calendar)
    bars = [
        PriceBar(
            date=dt.date(2026, 8, 20) + dt.timedelta(days=i),
            open=Decimal("2900"),
            high=Decimal("3200"),
            low=Decimal("2800"),
            close=Decimal("3000"),
            volume=1000,
        )
        for i in range(5)
    ]
    for avg in (Decimal("0"), Decimal("-500")):
        metrics = compute_profit_protection_metrics(
            bars=bars,
            current_price=Decimal("3000"),
            average_purchase_price=avg,
            basis_date=dt.date(2026, 8, 19),
            as_of_date=dt.date(2026, 8, 24),
            business_calendar=calendar,
            config=_CONFIG.profit_taking.profit_protection,
            ratio_adjustment_event_since_basis=False,
        )
        # profit_protection: 判定不能
        assert metrics.signal_label == "DATA_INSUFFICIENT"
        assert metrics.insufficient_data_reason is not None
        # profit_taking: 判定不能(例外)。0%として評価を続行しない。
        with pytest.raises(InvalidProfitTakingInputError):
            _evaluate_with_cost(avg, Decimal("100000") if avg > 0 else Decimal("-1"))


def test_current_price_zero_behaviour_is_unchanged_by_this_issue() -> None:
    """T11: current_price<=0 の扱いを本Issueで変更していない(#52 scope)。

    avg/total costが正であれば、現在値が0でも従来どおり評価が続行される
    (含み損として扱われる)。ここを判定不能へ変えることは #52 の責務。
    """
    result = evaluate_profit_taking(
        current_price=Decimal("0"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(fair_value_range=_invalid_input_fair_value()),
    )

    assert result.pnl.unrealized_pnl_pct == -100.0
    assert result.recommendation_type == RecommendationType.HOLD


def test_historical_holding_with_invalid_cost_is_still_readable() -> None:
    """T12: 取得原価が不正な既存レコードでも entity の読み込みは壊れない。

    Issue #75 の fail-close は**判定側**で行い、`PurchaseLot` / `Holding` へ
    正値 validator を置かない。entity 側に置くと既存の歴史レコードを読む時点で
    検証が走り、bad-record isolation / historical compatibility(#63 F-F10)を
    悪化させるため。ここでは「entity は読める」「判定は止まる」の両立を固定する。
    """
    lot = PurchaseLot(
        lot_id="lot-1",
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, "9999"),
        stock_code="9999",
        purchase_date=dt.date(2024, 1, 1),
        shares=100,
        purchase_price=Decimal("0"),
        account_type=AccountType.SPECIFIC,
    )

    # entity 生成そのものは成功する(読み込み経路を壊さない)。
    assert lot.purchase_price == Decimal("0")
    assert lot.amount() == Decimal("0")

    # 判定側が fail-close する。
    with pytest.raises(InvalidProfitTakingInputError):
        _evaluate_with_cost(Decimal("0"), Decimal("0"))
