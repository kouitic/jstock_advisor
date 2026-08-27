from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.valuation.buy_price_levels import compute_buy_price_levels
from jstock_advisor.domain.valuation.margin_of_safety import compute_margin_of_safety
from jstock_advisor.domain.valuation.valuation_confidence import determine_valuation_confidence

_CONFIG = load_config().buy_decision.margin_of_safety


def test_high_confidence_base_margins_no_adjustments() -> None:
    # BUY候補裾野拡大機能(2026-08): 打診買い(entry)の初期安全余裕率を
    # 0.10から0.05へ緩和(config/buy_decision_rules.yaml)。standard/strongは不変。
    result = compute_margin_of_safety(ConfidenceLevel.HIGH, [], _CONFIG)
    assert result.allowed is True
    assert result.entry_margin == Decimal("0.05")
    assert result.standard_margin == Decimal("0.15")
    assert result.strong_margin == Decimal("0.20")
    assert result.adjustments == ()


def test_medium_confidence_is_more_conservative_than_high() -> None:
    high = compute_margin_of_safety(ConfidenceLevel.HIGH, [], _CONFIG)
    medium = compute_margin_of_safety(ConfidenceLevel.MEDIUM, [], _CONFIG)
    assert medium.entry_margin > high.entry_margin
    assert medium.standard_margin > high.standard_margin
    assert medium.strong_margin > high.strong_margin


def test_low_confidence_does_not_generate_automatic_price() -> None:
    result = compute_margin_of_safety(ConfidenceLevel.LOW, [], _CONFIG)
    assert result.allowed is False
    assert result.entry_margin is None


def test_adjustments_stack_and_are_recorded_with_reasons() -> None:
    # industry_model_not_applied(VALUATION_UNCERTAINTY)とearnings_within_7_business_days
    # (EVENT_TIMING)は別カテゴリのため合算される。entryへは加算感応度倍率(0.50)が
    # かかるため、単純加算ではなくentry基準値(0.05、2026-08で0.10から緩和)+
    # (0.05+0.03)*0.50になる。
    result = compute_margin_of_safety(
        ConfidenceLevel.HIGH,
        ["industry_model_not_applied", "earnings_within_7_business_days"],
        _CONFIG,
    )
    assert result.entry_margin == Decimal("0.05") + (Decimal("0.05") + Decimal("0.03")) * Decimal(
        "0.50"
    )
    assert len(result.adjustments) == 2
    codes = {a.code for a in result.adjustments}
    assert codes == {"industry_model_not_applied", "earnings_within_7_business_days"}
    for adjustment in result.adjustments:
        assert adjustment.reason
        assert adjustment.superseded_by is None  # 別カテゴリのため両方採用される


def test_same_category_adjustments_only_max_value_adopted() -> None:
    # high_valuation_dispersion(5%)とvery_high_valuation_dispersion(10%)は
    # いずれもVALUATION_UNCERTAINTYカテゴリのため、単純合算(15%)ではなく
    # 最大値(10%)のみが採用される。
    result = compute_margin_of_safety(
        ConfidenceLevel.HIGH,
        ["high_valuation_dispersion", "very_high_valuation_dispersion"],
        _CONFIG,
    )
    assert result.entry_margin == Decimal("0.05") + Decimal("0.10") * Decimal("0.50")
    adopted = next(a for a in result.adjustments if a.code == "very_high_valuation_dispersion")
    superseded = next(a for a in result.adjustments if a.code == "high_valuation_dispersion")
    assert adopted.superseded_by is None
    assert superseded.superseded_by == "very_high_valuation_dispersion"


def test_margin_capped_at_maximum_does_not_collapse_three_tiers() -> None:
    # 全リスクコード該当時でも、段階別上限(entry<standard<strong)により
    # 3段階が同一価格に潰れてはならない(タチエス897円問題の再発防止)。
    all_codes = list(_CONFIG.adjustments.model_dump().keys())
    result = compute_margin_of_safety(ConfidenceLevel.MEDIUM, all_codes, _CONFIG)
    assert result.entry_margin == Decimal("0.30")
    assert result.standard_margin == Decimal("0.38")
    assert result.strong_margin == Decimal("0.45")
    assert result.entry_margin < result.standard_margin < result.strong_margin


def test_tachi_s_regression_three_tiers_are_distinct() -> None:
    # 実データ回帰テスト(タチエス7239): 現在値2,254円・valuation_anchor
    # 1,630.12円・バラつき1.93倍・MEDIUM信頼度・6件のリスクコードの条件で、
    # entry/standard/strongが3つとも異なる値になり、順序が保たれることを確認する。
    codes = [
        "high_valuation_dispersion",
        "industry_model_not_applied",
        "cyclical_industry",
        "temporary_earnings_boost_risk",
        "major_customer_dependency",
        "data_quality_warning",
    ]
    result = compute_margin_of_safety(ConfidenceLevel.MEDIUM, codes, _CONFIG)
    assert result.entry_margin == Decimal("0.30")
    assert result.standard_margin == Decimal("0.38")
    assert result.strong_margin == Decimal("0.45")
    assert result.entry_margin < result.standard_margin < result.strong_margin

    anchor = Decimal("1630.122246851966680496117182")
    from jstock_advisor.domain.valuation.buy_price_levels import compute_buy_price_levels

    levels = compute_buy_price_levels(anchor, result)
    assert levels.entry.price == Decimal("1141")
    assert levels.standard.price == Decimal("1011")
    assert levels.strong.price == Decimal("897")
    assert levels.entry.price != levels.standard.price != levels.strong.price
    assert levels.entry.price > levels.standard.price > levels.strong.price


def test_minimum_margin_gap_raises_upper_tier_when_too_close() -> None:
    # entry/standard/strongの基本値・加算感応度倍率がすべて等しい(=キャップ前は
    # 3段階とも同じ値になる)ケースでも、段階別上限に余裕があればminimum_margin_gap
    # によって上位段階が引き上げられ、3段階が異なる値になることを確認する。
    from jstock_advisor.config.models import (
        MarginAdjustments,
        MarginOfSafetyAdjustmentMultipliers,
        MarginOfSafetyConfidenceTiers,
        MarginOfSafetyConfig,
        MarginOfSafetyMaximumTiers,
        MarginOfSafetyTier,
    )

    tight_tier = MarginOfSafetyTier(entry=0.10, standard=0.10, strong=0.10)
    config = MarginOfSafetyConfig(
        confidence=MarginOfSafetyConfidenceTiers(high=tight_tier, medium=tight_tier),
        maximum_margin=MarginOfSafetyMaximumTiers(entry=0.20, standard=0.30, strong=0.40),
        minimum_margin_gap=0.06,
        adjustment_multipliers=MarginOfSafetyAdjustmentMultipliers(
            entry=0.50, standard=0.50, strong=0.50
        ),
        adjustments=MarginAdjustments(
            earnings_within_3_business_days=0.0,
            earnings_within_7_business_days=0.0,
            high_valuation_dispersion=0.0,
            very_high_valuation_dispersion=0.0,
            industry_model_not_applied=0.0,
            cyclical_industry=0.0,
            small_cap_or_low_liquidity=0.0,
            volatile_earnings=0.0,
            temporary_earnings_boost_risk=0.0,
            major_customer_dependency=0.0,
            data_quality_warning=0.30,
        ),
    )
    result = compute_margin_of_safety(ConfidenceLevel.HIGH, ["data_quality_warning"], config)
    # キャップ前はentry=standard=strong=0.10+0.30*0.50=0.25になるはずだが、
    # entryは自身の上限(0.20)でキャップされ、standard/strongはgap保証により
    # 押し上げられるため、3段階すべて異なる値になる。
    assert result.entry_margin == Decimal("0.20")
    assert result.standard_margin == Decimal("0.26")
    assert result.strong_margin == Decimal("0.32")
    assert result.entry_margin < result.standard_margin < result.strong_margin


def test_buy_price_levels_ordering_from_margins() -> None:
    margin_result = compute_margin_of_safety(ConfidenceLevel.HIGH, [], _CONFIG)
    levels = compute_buy_price_levels(Decimal("1000"), margin_result)
    assert levels.entry.price == Decimal("950")  # 1000 * (1-0.05)、2026-08で0.10から緩和
    assert levels.standard.price == Decimal("850")  # 1000 * (1-0.15)
    assert levels.strong.price == Decimal("800")  # 1000 * (1-0.20)
    assert levels.entry.price >= levels.standard.price >= levels.strong.price


def test_buy_price_levels_none_when_valuation_anchor_none() -> None:
    margin_result = compute_margin_of_safety(ConfidenceLevel.HIGH, [], _CONFIG)
    levels = compute_buy_price_levels(None, margin_result)
    assert levels.entry is None
    assert levels.standard is None
    assert levels.strong is None


def test_buy_price_levels_none_when_confidence_low() -> None:
    margin_result = compute_margin_of_safety(ConfidenceLevel.LOW, [], _CONFIG)
    levels = compute_buy_price_levels(Decimal("1000"), margin_result)
    assert levels.entry is None


def test_valuation_confidence_high_when_no_negative_factors() -> None:
    result = determine_valuation_confidence(
        methods_used_count=3,
        dispersion_ratio=1.1,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.HIGH
    assert result.reasons_not_high == []


def test_valuation_confidence_medium_when_industry_model_not_applied() -> None:
    result = determine_valuation_confidence(
        methods_used_count=3,
        dispersion_ratio=1.1,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=False,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.MEDIUM
    assert "業種別適正価格モデル未適用" in result.reasons_not_high


def test_valuation_confidence_medium_when_dispersion_above_1_60() -> None:
    result = determine_valuation_confidence(
        methods_used_count=3,
        dispersion_ratio=1.7,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.MEDIUM


def test_valuation_confidence_low_when_dispersion_above_2_00() -> None:
    result = determine_valuation_confidence(
        methods_used_count=3,
        dispersion_ratio=2.5,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.LOW
    # レビュー対応(2026-08、NO_VALUATION_ANCHOR表示不備の是正、必須テスト1・2):
    # 標準5方式が有効でも方式間乖離がauto_buy_blockを超えた場合、直接原因が
    # VALUATION_DISPERSION_TOO_HIGHとして、判定時点の実測値dispersion_ratioと
    # 実際に使用した基準値dispersion_auto_buy_blockごと構造化される。
    assert result.blocking_reason is not None
    assert result.blocking_reason.code == "VALUATION_DISPERSION_TOO_HIGH"
    assert result.blocking_reason.actual_value == 2.5
    assert result.blocking_reason.threshold_value == 2.00


def test_valuation_confidence_low_when_fewer_than_2_methods() -> None:
    result = determine_valuation_confidence(
        methods_used_count=1,
        dispersion_ratio=None,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.LOW
    # 必須テスト5: 有効方式1件のケースでTOO_FEW_VALUATION_METHODSが構造化される。
    assert result.blocking_reason is not None
    assert result.blocking_reason.code == "TOO_FEW_VALUATION_METHODS"
    assert result.blocking_reason.actual_value == 1.0
    assert result.blocking_reason.threshold_value == 2.0


def test_valuation_confidence_low_when_no_methods_used() -> None:
    """必須テスト4: 有効方式0件のケースでNO_VALID_VALUATION_METHODSが構造化される。"""
    result = determine_valuation_confidence(
        methods_used_count=0,
        dispersion_ratio=None,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.LOW
    assert result.blocking_reason is not None
    assert result.blocking_reason.code == "NO_VALID_VALUATION_METHODS"
    assert result.blocking_reason.actual_value is None
    assert result.blocking_reason.threshold_value is None


def test_valuation_confidence_medium_has_no_blocking_reason() -> None:
    """MEDIUM/HIGHではblocking_reasonが設定されない(anchorが実際に算出される
    ため、NO_VALUATION_ANCHOR原因のスナップショットは不要)ことを確認する。"""
    result = determine_valuation_confidence(
        methods_used_count=3,
        dispersion_ratio=1.7,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.MEDIUM
    assert result.blocking_reason is None
