"""利益保全(Profit Protection)判定と既存利確ロジックとの統合テスト
(2026-08、サンリオ8136の含み益吐き出し事例対応)。

profit_protection.py自体の指標算出(境界値・データ品質)はtest_profit_protection.pyで
検証済みのため、本ファイルではevaluate_profit_taking()への統合(既存SELL/FULL/URGENT系
との優先順位、Fair Value confidenceからの独立性、origin floor、価格フィールド)を検証する。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    IndustryClassification,
    RecommendationType,
    SellIntensity,
    TrendClassification,
)
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.signals.profit_protection import ProfitProtectionMetrics
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    evaluate_profit_taking,
    resolve_sell_intensity,
)
from jstock_advisor.domain.signals.trading_unit_feasibility import compute_suggested_sell_shares

_CONFIG = load_config()


def _pp_metrics(
    *,
    candidate: bool = False,
    strong: bool = False,
    peak_gain_pct: float = 58.1,
    current_gain_pct: float = 33.4,
    drawdown_from_peak_pct: float = 15.6,
    gain_giveback_ratio_pct: float = 42.5,
) -> ProfitProtectionMetrics:
    return ProfitProtectionMetrics(
        insufficient_data_reason=None,
        peak_price_since_entry=Decimal("1454.5"),
        peak_gain_pct=peak_gain_pct,
        current_gain_pct=current_gain_pct,
        drawdown_from_peak_pct=drawdown_from_peak_pct,
        gain_giveback_ratio_pct=gain_giveback_ratio_pct,
        candidate_signal=candidate,
        strong_signal=strong,
    )


def _insufficient_pp_metrics() -> ProfitProtectionMetrics:
    return ProfitProtectionMetrics(
        insufficient_data_reason="保有期間中に株式分割・併合等があり判定不能",
        peak_price_since_entry=None,
        peak_gain_pct=None,
        current_gain_pct=None,
        drawdown_from_peak_pct=None,
        gain_giveback_ratio_pct=None,
        candidate_signal=False,
        strong_signal=False,
    )


def _fair_value_range_low_confidence() -> FairValueRange:
    """Fair Value confidence=LOW(かつusable_for_trading_judgment=False)、
    ceiling_priceが一切使えない状態(サンリオ8136で実際に起きていた状況)。
    """
    return FairValueRange(
        bear=None,
        neutral=None,
        bull=None,
        overall_confidence=ConfidenceLevel.LOW,
        methods_used=[],
        methods_excluded=[
            FairValueMethodResult(method="per", fair_value=None, confidence=ConfidenceLevel.LOW)
        ],
        usable_for_trading_judgment=False,
    )


def _evaluate(
    *,
    current_price: Decimal = Decimal("1227"),
    average_purchase_price: Decimal = Decimal("920"),
    profit_protection: ProfitProtectionMetrics | None,
    fair_value_range: FairValueRange | None = None,
    momentum: MomentumSnapshot | None = None,
    partial_sale_executable: bool = True,
    investment_premise_broken: bool = False,
    accounting_or_scandal_or_delisting_risk: bool = False,
):
    return evaluate_profit_taking(
        current_price=current_price,
        average_purchase_price=average_purchase_price,
        shares=500,
        total_purchase_amount=average_purchase_price * 500,
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=3.0,
        forecast_annual_dividend_per_share=Decimal("20"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fair_value_range if fair_value_range is not None else (
                _fair_value_range_low_confidence()
            ),
            momentum=momentum,
            profit_protection=profit_protection,
            partial_sale_executable=partial_sale_executable,
            investment_premise_broken=investment_premise_broken,
            accounting_or_scandal_or_delisting_risk=accounting_or_scandal_or_delisting_risk,
        ),
    )


def test_strong_signal_triggers_partial_despite_fair_value_confidence_low() -> None:
    """要求仕様§3B: Fair Value confidence=LOWでもStrong Profit Protectionは
    PARTIAL_PROFIT_TAKEを成立させる(サンリオ8136の回帰確認)。
    """
    result = _evaluate(profit_protection=_pp_metrics(candidate=True, strong=True))
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.origin == "PROFIT_PROTECTION_STRONG"
    assert result.fair_value_action_usable is False


def test_strong_signal_triggers_partial_with_fair_value_range_none() -> None:
    """Fair Value confidence=UNKNOWN相当(fair_value_range自体が未算出)でも
    Strong Profit Protectionは成立する。
    """
    result = _evaluate(
        profit_protection=_pp_metrics(candidate=True, strong=True), fair_value_range=None
    )
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.origin == "PROFIT_PROTECTION_STRONG"


def test_candidate_alone_does_not_trigger_partial() -> None:
    """candidateシグナル単独(他の独立条件が無い)ではPARTIALへ到達しない
    (要求仕様§3A: 既存の補助情報と組み合わせられる設計)。
    """
    result = _evaluate(profit_protection=_pp_metrics(candidate=True, strong=False))
    assert result.recommendation_type != RecommendationType.PARTIAL_PROFIT_TAKE


def test_candidate_combined_with_trend_downgrade_reaches_partial() -> None:
    """candidateシグナル + 既存条件(トレンド悪化)の組み合わせで
    min_conditions_for_partial(既定2)に到達しPARTIALへ到達する。
    """
    downtrend_momentum = MomentumSnapshot(
        trend_classification=TrendClassification.DOWNTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )
    result = _evaluate(
        profit_protection=_pp_metrics(candidate=True, strong=False), momentum=downtrend_momentum
    )
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.origin == "OTHER_CONDITIONS"


def test_no_profit_protection_metrics_does_not_change_existing_behavior() -> None:
    """condition_inputs.profit_protection=None(未算出)の場合、既存の判定に
    一切影響しない(後方互換)。"""
    result = _evaluate(profit_protection=None)
    assert result.profit_protection_signal == "NONE"
    assert result.profit_protection_peak_price is None
    assert result.recommendation_type != RecommendationType.PARTIAL_PROFIT_TAKE


def test_data_insufficient_metrics_does_not_trigger_signal() -> None:
    """peak_price算出不能(株式分割等)の場合、candidate/strongいずれも不成立で
    あるため通常の判定に一切影響しない。insufficient_reasonは監査・原因調査用に
    ProfitTakingResultへ伝播する(コードレビュー対応2026-08、指摘2)。"""
    result = _evaluate(profit_protection=_insufficient_pp_metrics())
    assert result.profit_protection_signal == "DATA_INSUFFICIENT"
    assert result.recommendation_type != RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.profit_protection_insufficient_reason == (
        "保有期間中に株式分割・併合等があり判定不能"
    )


def test_normal_strong_signal_has_no_insufficient_reason() -> None:
    """正常にStrongが成立した場合、insufficient_reasonはNoneのまま
    (コードレビュー対応2026-08、指摘2)。"""
    result = _evaluate(profit_protection=_pp_metrics(candidate=True, strong=True))
    assert result.profit_protection_insufficient_reason is None


def test_no_profit_protection_metrics_has_no_insufficient_reason() -> None:
    """condition_inputs.profit_protection=None(未算出)の場合もNoneのまま。"""
    result = _evaluate(profit_protection=None)
    assert result.profit_protection_insufficient_reason is None


def test_strong_signal_requires_partial_sale_executable() -> None:
    """単元未満で一部売却が実行できない場合、Strong Profit Protectionは
    成立させない(実行不能な推奨を出さないため)。"""
    result = _evaluate(
        profit_protection=_pp_metrics(candidate=True, strong=True),
        partial_sale_executable=False,
    )
    assert result.recommendation_type != RecommendationType.PARTIAL_PROFIT_TAKE


def test_strong_signal_loses_to_fundamental_critical_risk_full() -> None:
    """投資前提崩壊等のFUNDAMENTAL_CRITICAL_RISK(FULL)は、Profit Protectionの
    PARTIALより常に優先される(要求仕様§6: 既存の強い売却判定を弱めない)。"""
    result = _evaluate(
        profit_protection=_pp_metrics(candidate=True, strong=True),
        investment_premise_broken=True,
    )
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert result.origin == "FUNDAMENTAL_CRITICAL_RISK"


def test_strong_signal_loses_to_fair_value_strong_full() -> None:
    """適正価格ベースの強いFULL条件が同時に成立している場合、そちらが優先される
    (Profit Protection追加によって既存のFULL判定が弱まらないことの確認)。"""
    strong_fv_range = FairValueRange(
        bear=Decimal("900"),
        neutral=Decimal("1000"),
        bull=Decimal("1000"),
        overall_confidence=ConfidenceLevel.HIGH,
        methods_used=[
            FairValueMethodResult(
                method=f"m{i}", fair_value=Decimal("1000"), confidence=ConfidenceLevel.HIGH
            )
            for i in range(3)
        ],
        methods_excluded=[],
        usable_for_trading_judgment=True,
    )
    result = evaluate_profit_taking(
        current_price=Decimal("1500"),  # bullを50%超過
        average_purchase_price=Decimal("920"),
        shares=500,
        total_purchase_amount=Decimal("920") * 500,
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=3.0,
        forecast_annual_dividend_per_share=Decimal("20"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=strong_fv_range,
            fair_value_reflects_latest_earnings=True,
            guidance_revision_disclosed=True,
            industry_model_applied=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            days_to_next_earnings_business_days=30,
            partial_sale_executable=True,
            profit_protection=_pp_metrics(candidate=True, strong=True),
        ),
    )
    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert result.origin == "FAIR_VALUE_STRONG"


def test_strong_signal_survives_mitigating_and_timing_combined() -> None:
    """origin=PROFIT_PROTECTION_STRONGは、緩和要因+タイミング層(上昇トレンド)の
    合計softeningでもPARTIAL未満へは降格しない(既存のPRICE_POSITION/
    FAIR_VALUE_STRONGと同じorigin floor)。"""
    uptrend_momentum = MomentumSnapshot(
        trend_classification=TrendClassification.STRONG_UPTREND,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.HIGH,
    )
    result = _evaluate(
        profit_protection=_pp_metrics(candidate=True, strong=True),
        momentum=uptrend_momentum,
    )
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.fundamental_action == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.final_action == RecommendationType.PARTIAL_PROFIT_TAKE


def test_strong_signal_sell_prices_use_current_price_reference() -> None:
    """Strong Profit Protection由来のPARTIALは、ceiling_price(適正価格)ではなく
    現在値付近を執行目安とする価格フィールドを持つ(適正価格を根拠にしないため)。"""
    result = _evaluate(profit_protection=_pp_metrics(candidate=True, strong=True))
    assert result.sell_prices.partial_profit_start_price is not None
    assert result.sell_prices.partial_profit_start_price.price == Decimal("1227")
    assert result.sell_prices.recommended_limit_price is not None
    assert result.sell_prices.recommended_limit_price.price == Decimal("1227")


def test_result_exposes_profit_protection_metrics_for_traceability() -> None:
    """要求仕様§8: 判定結果からProfit Protectionの判定理由を再現できる。"""
    result = _evaluate(profit_protection=_pp_metrics(candidate=True, strong=True))
    assert result.profit_protection_signal == "STRONG"
    assert result.profit_protection_peak_price == Decimal("1454.5")
    assert result.profit_protection_peak_gain_pct == 58.1
    assert result.profit_protection_current_gain_pct == 33.4
    assert result.profit_protection_drawdown_from_peak_pct == 15.6
    assert result.profit_protection_gain_giveback_ratio_pct == 42.5
    assert any("Strong Profit Protection" in r for r in result.triggered_reasons)


# --- sell_intensity(コードレビュー対応2026-08、Part B) ---


def _momentum(trend: TrendClassification) -> MomentumSnapshot:
    return MomentumSnapshot(
        trend_classification=trend,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
        confidence=ConfidenceLevel.MEDIUM,
    )


def test_resolve_sell_intensity_profit_protection_strong_without_downtrend() -> None:
    assert resolve_sell_intensity("PROFIT_PROTECTION_STRONG", None) == SellIntensity.STRONG
    assert (
        resolve_sell_intensity(
            "PROFIT_PROTECTION_STRONG", _momentum(TrendClassification.UPTREND)
        )
        == SellIntensity.STRONG
    )


def test_resolve_sell_intensity_profit_protection_strong_with_downtrend_is_very_strong() -> None:
    assert (
        resolve_sell_intensity(
            "PROFIT_PROTECTION_STRONG", _momentum(TrendClassification.DOWNTREND)
        )
        == SellIntensity.VERY_STRONG
    )
    assert (
        resolve_sell_intensity(
            "PROFIT_PROTECTION_STRONG", _momentum(TrendClassification.STRONG_DOWNTREND)
        )
        == SellIntensity.VERY_STRONG
    )


def test_resolve_sell_intensity_other_conditions_is_light() -> None:
    assert resolve_sell_intensity("OTHER_CONDITIONS", None) == SellIntensity.LIGHT


def test_resolve_sell_intensity_price_position_and_fair_value_strong_are_standard() -> None:
    assert resolve_sell_intensity("PRICE_POSITION", None) == SellIntensity.STANDARD
    assert resolve_sell_intensity("FAIR_VALUE_STRONG", None) == SellIntensity.STANDARD


def test_strong_signal_result_has_strong_sell_intensity() -> None:
    result = _evaluate(profit_protection=_pp_metrics(candidate=True, strong=True))
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.sell_intensity == SellIntensity.STRONG


def test_strong_signal_with_downtrend_has_very_strong_sell_intensity() -> None:
    result = _evaluate(
        profit_protection=_pp_metrics(candidate=True, strong=True),
        momentum=_momentum(TrendClassification.DOWNTREND),
    )
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.sell_intensity == SellIntensity.VERY_STRONG


def test_other_conditions_origin_partial_has_light_sell_intensity() -> None:
    result = _evaluate(
        profit_protection=_pp_metrics(candidate=True, strong=False),
        momentum=_momentum(TrendClassification.DOWNTREND),
    )
    assert result.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.origin == "OTHER_CONDITIONS"
    assert result.sell_intensity == SellIntensity.LIGHT


def test_non_partial_result_has_no_sell_intensity() -> None:
    result = _evaluate(profit_protection=None)
    assert result.recommendation_type != RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.sell_intensity is None


# --- サンリオ8136 A〜E: 判定+数量の一覧(コードレビュー対応2026-08、Part D) ---


def test_sanrio_8136_full_table_partial_and_quantity() -> None:
    """500株保有・平均取得単価920円の回帰ケースについて、判定結果(candidate/
    strong/RecommendationType/sell_intensity)と、500株保有時の実際の提案
    株数(suggested_sell_shares/suggested_sell_ratio/remaining_shares)を
    一覧で確認する。"""
    ratios = _CONFIG.profit_taking.partial_sell_ratios
    intensity_ratio = {
        SellIntensity.LIGHT: ratios.light,
        SellIntensity.STANDARD: ratios.standard,
        SellIntensity.STRONG: ratios.strong,
        SellIntensity.VERY_STRONG: ratios.very_strong,
    }
    cases = {
        "A_1400": (Decimal("1400"), False, False, RecommendationType.WATCH),
        "B_1350": (Decimal("1350"), False, False, RecommendationType.WATCH),
        "C_1300": (Decimal("1300"), True, False, RecommendationType.WATCH),
        "D_1282_5": (Decimal("1282.5"), True, True, RecommendationType.PARTIAL_PROFIT_TAKE),
        "E_1227": (Decimal("1227"), True, True, RecommendationType.PARTIAL_PROFIT_TAKE),
    }
    for label, (price, candidate, strong, expected_type) in cases.items():
        pp = _pp_metrics(
            candidate=candidate,
            strong=strong,
            current_gain_pct=float(price / Decimal("920") - 1) * 100,
        )
        result = _evaluate(current_price=price, profit_protection=pp)
        assert result.recommendation_type == expected_type, label
        if expected_type == RecommendationType.PARTIAL_PROFIT_TAKE:
            assert result.sell_intensity == SellIntensity.STRONG, label
            suggestion = compute_suggested_sell_shares(
                shares=500,
                trading_unit=100,
                odd_lot_trading_available=False,
                target_sell_ratio=intensity_ratio[result.sell_intensity],
            )
            assert suggestion is not None, label
            assert suggestion.shares == 300, label  # strong比率0.60 -> floor(500*0.6/100)=3単元
            assert 500 - suggestion.shares >= 100, label
        else:
            assert result.sell_intensity is None, label
