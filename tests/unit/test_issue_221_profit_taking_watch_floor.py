"""Issue #221 Phase 1: timing 層の WATCH 床と「直ちに利確しない理由」の充足。

本番で観測された 2 件（含み益率が FULL 閾値を大きく超えるのに、想定上限価格を
判定に使えず WATCH 止まりとなり、さらに上昇トレンドで HOLD へ落ちて
判定記録も通知も残らなかった）をローカルで再現する。

★ 銘柄コード・保有数量・取得単価は本番の値を転記せず、すべて架空値を使う。
★ Production へは一切アクセスしない。
"""

from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    IndustryClassification,
    RecommendationType,
    TimingAction,
    TrendClassification,
)
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.valuation import (
    FairValueMethodResult,
    FairValueRange,
    ProfitTakingFairValueBlockReasonCode,
)
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    evaluate_profit_taking,
)

_CONFIG = load_config()
_PT = _CONFIG.profit_taking


def _range(
    *,
    bear: Decimal,
    neutral: Decimal,
    bull: Decimal,
    method_count: int,
    usable: bool = True,
) -> FairValueRange:
    return FairValueRange(
        bear=bear,
        neutral=neutral,
        bull=bull,
        overall_confidence=ConfidenceLevel.MEDIUM,
        methods_used=[
            FairValueMethodResult(
                method=f"method{i}", fair_value=neutral, confidence=ConfidenceLevel.MEDIUM
            )
            for i in range(method_count)
        ],
        methods_excluded=[],
        usable_for_trading_judgment=usable,
    )


def _uptrend() -> MomentumSnapshot:
    return MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_UPTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )


def _evaluate(
    *,
    current_price: Decimal,
    average_purchase_price: Decimal,
    condition_inputs: ProfitTakingConditionInputs,
    mitigating_inputs: MitigatingFactorInputs | None = None,
    current_total_yield_pct: float = 4.0,
):
    return evaluate_profit_taking(
        current_price=current_price,
        average_purchase_price=average_purchase_price,
        shares=100,
        total_purchase_amount=average_purchase_price * 100,
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=current_total_yield_pct,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=mitigating_inputs or MitigatingFactorInputs(),
        config=_PT,
        condition_inputs=condition_inputs,
    )


def _wide_spread_range() -> FairValueRange:
    """レンジ自体は使えるが、利確判定側のスプレッド基準を超える適正価格レンジ。

    bull / bear がレンジ側の上限(valuation_rules の max_method_spread_ratio)
    には達しない一方、利確判定側の max_fair_value_spread_ratio_for_partial は
    超える帯に置く。
    """
    return _range(
        bear=Decimal("1000"), neutral=Decimal("1400"), bull=Decimal("1600"), method_count=5
    )


# --- U1: timing 層の WATCH 床 -------------------------------------------------


def test_wide_spread_gain_with_mitigating_and_uptrend_stays_watch() -> None:
    """本番の再現 1: 含み益率が FULL 閾値超・上値余地なし・緩和要因あり・上昇トレンド。

    修正前は timing 層の降格で HOLD となり、Recommendation が生成されず
    判定記録も通知も残らなかった。
    """
    result = _evaluate(
        current_price=Decimal("1500"),
        average_purchase_price=Decimal("1000"),  # +50%
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_wide_spread_range(),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            momentum=_uptrend(),
        ),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=5),
    )

    assert result.fair_value_action_usable is False
    assert result.upside_pct is None
    assert result.fundamental_action == RecommendationType.WATCH
    assert result.timing_action == TimingAction.WAIT_UPTREND_CONTINUES
    # 床が効き、HOLD(=通知なし)まで落ちない。
    assert result.final_action == RecommendationType.WATCH
    assert result.recommendation_type == RecommendationType.WATCH


def test_wide_spread_gain_with_two_mitigating_factors_stays_watch() -> None:
    """本番の再現 2: 緩和要因が 2 件該当しても WATCH を下回らない。"""
    result = _evaluate(
        current_price=Decimal("1380"),
        average_purchase_price=Decimal("1000"),  # +38%
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_wide_spread_range(),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            momentum=_uptrend(),
        ),
        mitigating_inputs=MitigatingFactorInputs(
            continuous_dividend_increase_years=5,
            fair_value_rising_with_earnings_growth=True,
        ),
    )

    assert result.fundamental_action == RecommendationType.WATCH
    assert result.final_action == RecommendationType.WATCH


def test_no_signal_still_results_in_hold() -> None:
    """利確シグナルが無い場合、床が誤って WATCH へ持ち上げない。"""
    result = _evaluate(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),  # +5%(watch 閾値未満)
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_range(
                bear=Decimal("1000"),
                neutral=Decimal("1100"),
                bull=Decimal("1200"),
                method_count=3,
            ),
            momentum=_uptrend(),
        ),
    )

    assert result.final_action == RecommendationType.HOLD
    assert result.recommendation_type == RecommendationType.HOLD


def test_existing_partial_floor_still_applies() -> None:
    """既存の PARTIAL 床(価格系 origin かつ raw_level >= PARTIAL)の回帰。"""
    result = _evaluate(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),  # +60%
        condition_inputs=ProfitTakingConditionInputs(
            # 上値余地が FULL 上限未満。価格系で FULL へ到達する。
            fair_value_range=_range(
                bear=Decimal("1500"),
                neutral=Decimal("1560"),
                bull=Decimal("1620"),
                method_count=3,
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            momentum=_uptrend(),
        ),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=5),
    )

    assert result.origin == "PRICE_POSITION"
    # 緩和 + timing の合計降格でも PARTIAL 未満へは落ちない。
    assert result.final_action in (
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.FULL_PROFIT_TAKE,
    )


def test_mitigating_layer_watch_floor_still_applies() -> None:
    """緩和層の既存 WATCH 床(上昇トレンドが無い場合)の回帰。"""
    result = _evaluate(
        current_price=Decimal("1500"),
        average_purchase_price=Decimal("1000"),
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_wide_spread_range(),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=5),
    )

    assert result.fundamental_action == RecommendationType.WATCH
    assert result.final_action == RecommendationType.WATCH


def test_partial_full_reachability_is_unchanged_by_the_floor() -> None:
    """誤売却防止: 床は WATCH までしか押し上げず、PARTIAL/FULL を増やさない。

    床が効く条件(raw_level > HOLD)を満たすケース群で、
    final_action が PARTIAL / FULL になるのは
    「床が無くてもそこへ到達していたケース」だけであることを確認する。
    """
    # 価格系で FULL へ到達するケース以外は、いずれも WATCH 以下に留まる。
    watch_only_cases = [
        # 上値余地なし(広いスプレッド) + 緩和 + 上昇トレンド
        (Decimal("1500"), _wide_spread_range(), True, True),
        # 上値余地なし + 緩和のみ
        (Decimal("1500"), _wide_spread_range(), True, False),
        # 上値余地なし + 上昇トレンドのみ
        (Decimal("1500"), _wide_spread_range(), False, True),
        # 上値余地なし + どちらも無し
        (Decimal("1500"), _wide_spread_range(), False, False),
    ]
    for price, fv_range, mitigating, uptrend in watch_only_cases:
        result = _evaluate(
            current_price=price,
            average_purchase_price=Decimal("1000"),
            condition_inputs=ProfitTakingConditionInputs(
                fair_value_range=fv_range,
                fair_value_reflects_latest_earnings=True,
                industry_classification=IndustryClassification.GENERAL_CORPORATE,
                momentum=_uptrend() if uptrend else None,
            ),
            mitigating_inputs=(
                MitigatingFactorInputs(continuous_dividend_increase_years=5)
                if mitigating
                else MitigatingFactorInputs()
            ),
        )
        assert result.final_action not in (
            RecommendationType.PARTIAL_PROFIT_TAKE,
            RecommendationType.FULL_PROFIT_TAKE,
        ), f"床が売却提案を増やしている: price={price} mitigating={mitigating} uptrend={uptrend}"


# --- U2: 「直ちに利確しない理由」の構造化情報 ---------------------------------


def test_wide_spread_sets_profit_taking_block_reason_code() -> None:
    """1.30 帯: レンジ自体は使えるが利確判定側の基準を超えている場合の理由コード。"""
    fv_range = _wide_spread_range()
    ratio = float(fv_range.bull / fv_range.bear)
    cbj = _PT.condition_based_judgment
    assert ratio > cbj.max_fair_value_spread_ratio_for_partial

    result = _evaluate(
        current_price=Decimal("1500"),
        average_purchase_price=Decimal("1000"),
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fv_range,
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )

    assert result.fair_value_action_usable is False
    assert (
        result.fair_value_action_block_reason_code
        == ProfitTakingFairValueBlockReasonCode.METHOD_SPREAD_TOO_WIDE_FOR_ACTION.value
    )


def test_unusable_range_does_not_set_profit_taking_block_reason_code() -> None:
    """レンジ自体が使えない場合は Issue #21 の理由コードが担当し、重複させない。"""
    result = _evaluate(
        current_price=Decimal("1500"),
        average_purchase_price=Decimal("1000"),
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_range(
                bear=Decimal("1000"),
                neutral=Decimal("1400"),
                bull=Decimal("1600"),
                method_count=5,
                usable=False,
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )

    assert result.fair_value_action_usable is False
    assert result.fair_value_action_block_reason_code is None


def test_narrow_spread_has_no_block_reason_code() -> None:
    """利確判定側の基準を満たす場合は理由コードを立てない。"""
    result = _evaluate(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_range(
                bear=Decimal("1500"),
                neutral=Decimal("1560"),
                bull=Decimal("1620"),
                method_count=3,
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )

    assert result.fair_value_action_usable is True
    assert result.fair_value_action_block_reason_code is None


def test_downgrade_absorbed_by_the_floor_is_not_reported_as_a_downgrade() -> None:
    """床が降格を吸収した場合は「1段階弱めた」と記録しない。

    緩和要因も上昇トレンドも該当するが、床により最終判定は raw_level と同じ
    WATCH のままである。この状態で「弱めました」と伝えると、実際には
    変わっていない判定について誤った説明をすることになる。
    """
    result = _evaluate(
        current_price=Decimal("1500"),
        average_purchase_price=Decimal("1000"),
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_wide_spread_range(),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            momentum=_uptrend(),
        ),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=5),
    )

    assert result.final_action == RecommendationType.WATCH
    assert result.mitigating_downgrade_applied is False
    assert result.timing_downgrade_applied is False


def test_downgrade_that_survives_the_floor_is_recorded() -> None:
    """床に吸収されずに残った降格は記録される。

    非価格系の独立条件 2 件で PARTIAL へ到達したケース(origin=OTHER_CONDITIONS)は
    PARTIAL 床の対象外のため、緩和要因による降格が実際に残る。
    """
    result = _evaluate(
        current_price=Decimal("1500"),
        average_purchase_price=Decimal("1000"),
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_wide_spread_range(),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            portfolio_concentration_over_limit=True,
        ),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=5),
        current_total_yield_pct=1.5,
    )

    assert result.origin == "OTHER_CONDITIONS"
    # 緩和要因による降格が床に吸収されず残る。
    assert result.mitigating_downgrade_applied is True


def test_downgrade_facts_are_false_when_nothing_lowered_the_level() -> None:
    """該当する材料も降格も無い場合は False のまま。"""
    result = _evaluate(
        current_price=Decimal("1500"),
        average_purchase_price=Decimal("1000"),
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_wide_spread_range(),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )

    assert result.mitigating_downgrade_applied is False
    assert result.timing_downgrade_applied is False


def test_block_reason_is_none_for_unrealized_loss() -> None:
    """含み損では利確自体が成立しないため、遮断理由の話にならない。"""
    result = _evaluate(
        current_price=Decimal("800"),
        average_purchase_price=Decimal("1000"),
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_wide_spread_range(),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )

    assert result.fair_value_action_block_reason_code is None
    assert result.final_action == RecommendationType.HOLD


def _partial_reachable_range() -> FairValueRange:
    """価格系(PRICE_POSITION)で PARTIAL へ到達する適正価格レンジ。

    上値余地が partial_upside_max_pct 未満・full_upside_max_pct 以上に入るよう
    bull を置く(含み益率 20% 以上 25% 未満と組み合わせて PARTIAL になる)。
    """
    return _range(
        bear=Decimal("1200"), neutral=Decimal("1280"), bull=Decimal("1330"), method_count=3
    )


def test_partial_floor_absorbing_mitigating_downgrade_is_not_reported() -> None:
    """F-1 の回帰: PARTIAL 床が緩和要因の降格を吸収した場合は「弱めた」と報告しない。

    origin=PRICE_POSITION / raw_level=PARTIAL / 緩和要因 1 件のとき、
    緩和層はいったん WATCH へ落とすが origin 別 PARTIAL 床が PARTIAL へ戻す。
    最終判定は raw_level と同じ PARTIAL であり、実際には何も弱まっていない。
    """
    result = _evaluate(
        current_price=Decimal("1220"),
        average_purchase_price=Decimal("1000"),  # +22%(partial_gain_pct 以上)
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_partial_reachable_range(),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=5),
    )

    assert result.origin == "PRICE_POSITION"
    assert result.fundamental_action == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.mitigating_downgrade_applied is False


def test_partial_floor_absorbing_timing_downgrade_is_not_reported() -> None:
    """F-1 の回帰(タイミング層側): 最終 PARTIAL 床が降格を吸収した場合も同様。"""
    result = _evaluate(
        current_price=Decimal("1220"),
        average_purchase_price=Decimal("1000"),
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_partial_reachable_range(),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            momentum=_uptrend(),
        ),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=5),
    )

    assert result.origin == "PRICE_POSITION"
    assert result.final_action == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.mitigating_downgrade_applied is False
    assert result.timing_downgrade_applied is False


def test_downgrade_from_full_to_partial_is_reported() -> None:
    """床に吸収されず実際に 1 段下がった場合は報告する(FULL -> PARTIAL)。"""
    result = _evaluate(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),  # +60%
        condition_inputs=ProfitTakingConditionInputs(
            # 上値余地が FULL 上限未満。価格系で FULL へ到達する。
            fair_value_range=_range(
                bear=Decimal("1500"),
                neutral=Decimal("1560"),
                bull=Decimal("1620"),
                method_count=3,
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
        mitigating_inputs=MitigatingFactorInputs(continuous_dividend_increase_years=5),
    )

    assert result.origin == "PRICE_POSITION"
    assert result.fundamental_action == RecommendationType.PARTIAL_PROFIT_TAKE
    # FULL -> PARTIAL の降格は PARTIAL 床に吸収されず残る。
    assert result.mitigating_downgrade_applied is True
