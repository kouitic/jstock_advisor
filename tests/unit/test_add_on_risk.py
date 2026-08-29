from decimal import Decimal

from jstock_advisor.config.models import AddOnRulesConfig
from jstock_advisor.domain.entities.enums import (
    EligibilityBlockCategory,
    PortfolioValuationBasis,
    RecommendationType,
)
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility
from jstock_advisor.domain.signals.add_on_risk import (
    BLOCK_REASON_PORTFOLIO_VALUATION_INSUFFICIENT,
    BLOCK_REASON_SECTOR_EXPOSURE_INSUFFICIENT,
    PROJECTION_BASIS_MINIMUM_TRADING_UNIT,
    AddOnRiskAssessment,
    evaluate_add_on_eligibility,
)


def _config(**overrides: object) -> AddOnRulesConfig:
    defaults: dict[str, object] = {
        "version": 1,
        "enabled": True,
        "block_add_on_single_stock_ratio": 0.20,
        "block_add_on_sector_ratio": 0.35,
        "block_on_sell_signal": True,
        "require_holding_data_consistency": True,
        "block_add_on_on_odd_lot": False,
    }
    defaults.update(overrides)
    return AddOnRulesConfig.model_validate(defaults)


def _call(**overrides: object) -> tuple[AddOnRiskAssessment, NotificationEligibility]:
    defaults: dict[str, object] = {
        "current_market_value": Decimal("100000"),
        "current_price": Decimal("1000"),
        "trading_unit": 100,
        "portfolio_total_market_value": Decimal("1000000"),
        "sector_total_market_value": Decimal("200000"),
        "portfolio_valuation_basis": PortfolioValuationBasis.MARKET_VALUE,
        "conflicting_holding_action": None,
        "holding_data_inconsistent": False,
        "holding_is_odd_lot": False,
        "config": _config(),
    }
    defaults.update(overrides)
    return evaluate_add_on_eligibility(**defaults)  # type: ignore[arg-type]


def test_eligible_when_all_conditions_pass() -> None:
    assessment, eligibility = _call()

    assert eligibility.eligible is True
    assert eligibility.block_category is None
    assert assessment.reasons == ()
    assert assessment.portfolio_data_reliable is True
    assert assessment.position_limit_exceeded is False
    assert assessment.sector_limit_exceeded is False


def test_projection_basis_is_always_one_minimum_trading_unit() -> None:
    assessment, _ = _call(trading_unit=100, current_price=Decimal("1234"))

    assert assessment.projection_basis == PROJECTION_BASIS_MINIMUM_TRADING_UNIT
    assert assessment.projected_add_on_quantity == 100
    assert assessment.projected_add_on_price == Decimal("1234")
    assert assessment.projected_add_on_amount == Decimal("123400")


def test_position_and_sector_ratios_reflect_add_on_in_both_numerator_and_denominator() -> None:
    # current_market_value=100,000, portfolio_total=1,000,000, add_on=100,000
    # current_position_ratio = 100,000 / 1,000,000 = 0.10
    # projected_position_ratio = (100,000+100,000) / (1,000,000+100,000) = 200,000/1,100,000
    assessment, _ = _call()

    assert assessment.current_position_ratio == Decimal("100000") / Decimal("1000000")
    assert assessment.projected_position_ratio == Decimal("200000") / Decimal("1100000")
    assert assessment.current_sector_ratio == Decimal("200000") / Decimal("1000000")
    assert assessment.projected_sector_ratio == Decimal("300000") / Decimal("1100000")


def test_conflicting_holding_action_blocks_when_configured() -> None:
    assessment, eligibility = _call(
        conflicting_holding_action=RecommendationType.SELL,
        config=_config(block_on_sell_signal=True),
    )

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.CONFLICTING_HOLDING_ACTION
    assert eligibility.block_reason == RecommendationType.SELL.value
    assert "CONFLICTING_HOLDING_ACTION:SELL" in assessment.reasons


def test_conflicting_holding_action_does_not_block_when_config_disabled() -> None:
    _, eligibility = _call(
        conflicting_holding_action=RecommendationType.SELL,
        config=_config(block_on_sell_signal=False),
    )

    assert eligibility.eligible is True


def test_holding_data_inconsistent_blocks_when_configured() -> None:
    _, eligibility = _call(
        holding_data_inconsistent=True,
        config=_config(require_holding_data_consistency=True),
    )

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.HOLDING_DATA_INCONSISTENT
    assert eligibility.block_reason == "HOLDING_DATA_INCONSISTENT"


def test_holding_data_inconsistent_does_not_block_when_config_disabled() -> None:
    _, eligibility = _call(
        holding_data_inconsistent=True,
        config=_config(require_holding_data_consistency=False),
    )

    assert eligibility.eligible is True


def test_odd_lot_holding_is_not_blocked_by_default() -> None:
    """単元未満株のみを理由に保有データ不整合と判定しない(v3修正)。"""
    _, eligibility = _call(holding_is_odd_lot=True, config=_config())

    assert eligibility.eligible is True


def test_odd_lot_holding_blocks_when_explicitly_configured() -> None:
    _, eligibility = _call(
        holding_is_odd_lot=True,
        config=_config(block_add_on_on_odd_lot=True),
    )

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.HOLDING_DATA_INCONSISTENT
    assert eligibility.block_reason == "ODD_LOT_HOLDING"


def test_unavailable_portfolio_basis_blocks_as_reliability_not_concentration() -> None:
    _, eligibility = _call(portfolio_valuation_basis=PortfolioValuationBasis.UNAVAILABLE)

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.PORTFOLIO_DATA_RELIABILITY


def test_missing_portfolio_total_market_value_blocks_as_reliability() -> None:
    _, eligibility = _call(portfolio_total_market_value=None)

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.PORTFOLIO_DATA_RELIABILITY


def test_missing_sector_total_blocks_only_sector_gate_and_keeps_stock_gate(
) -> None:
    """Issue #82: 業種が不明でも**銘柄集中度は評価する**。

    旧契約: sector不明 → stockもsectorもまとめてreliability block。
    新契約: sector不明 → **stock gateは評価**、sector gateのみfail-close。

    時価が揃っていれば銘柄集中度は算出できるため、業種の欠如を理由に
    銘柄集中度まで無効化しない(業種を推測で埋めることもしない)。
    """
    assessment, eligibility = _call(sector_total_market_value=None)

    # 銘柄集中度は算出されている(=巻き添えになっていない)。
    assert assessment.portfolio_data_reliable is True
    assert assessment.current_position_ratio == Decimal("100000") / Decimal("1000000")
    assert assessment.projected_position_ratio is not None
    # 業種集中度のみ不成立。推測値も0埋めもしない。
    assert assessment.sector_exposure_available is False
    assert assessment.current_sector_ratio is None
    assert assessment.projected_sector_ratio is None
    assert assessment.sector_limit_exceeded is False
    # 通知は安全側でブロックされるが、理由は業種データ不足であると識別できる。
    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.PORTFOLIO_DATA_RELIABILITY
    assert eligibility.block_reason == BLOCK_REASON_SECTOR_EXPOSURE_INSUFFICIENT
    assert BLOCK_REASON_SECTOR_EXPOSURE_INSUFFICIENT in assessment.reasons


def test_stock_concentration_still_blocks_when_sector_is_unknown() -> None:
    """業種不明でも、銘柄集中度の上限超過はきちんと検出される。

    「sector不明なら常にsectorデータ不足でブロック」ではなく、優先順位どおり
    **銘柄集中度の超過が先に**理由として返ることを固定する。
    """
    _, eligibility = _call(
        current_market_value=Decimal("500000"), sector_total_market_value=None
    )

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.POSITION_CONCENTRATION
    assert eligibility.block_reason == "POSITION_LIMIT_EXCEEDED"


def test_price_missing_fails_close_for_both_stock_and_sector_gates() -> None:
    """Issue #82: 価格欠損はポートフォリオ総額自体が不完全なため両ゲートfail-close。

    業種側だけを通す、0円で補完する、といった緩和は行わない。
    """
    assessment, eligibility = _call(
        portfolio_valuation_basis=PortfolioValuationBasis.UNAVAILABLE,
        portfolio_total_market_value=None,
        sector_total_market_value=Decimal("200000"),
    )

    assert assessment.portfolio_data_reliable is False
    assert assessment.sector_exposure_available is False
    assert assessment.current_position_ratio is None
    assert assessment.current_sector_ratio is None
    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.PORTFOLIO_DATA_RELIABILITY
    assert eligibility.block_reason == BLOCK_REASON_PORTFOLIO_VALUATION_INSUFFICIENT


def test_portfolio_valuation_insufficient_takes_precedence_over_sector_insufficient() -> None:
    """時価も業種も無い場合、理由は時価不足(より根本的な方)になる。"""
    _, eligibility = _call(
        portfolio_valuation_basis=PortfolioValuationBasis.UNAVAILABLE,
        portfolio_total_market_value=None,
        sector_total_market_value=None,
    )

    assert eligibility.block_reason == BLOCK_REASON_PORTFOLIO_VALUATION_INSUFFICIENT


def test_zero_portfolio_total_market_value_blocks_as_reliability_not_division_error() -> None:
    """分母0でZeroDivisionErrorを起こさず、信頼性不足として安全に処理する。"""
    assessment, eligibility = _call(portfolio_total_market_value=Decimal("0"))

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.PORTFOLIO_DATA_RELIABILITY
    assert assessment.current_position_ratio is None
    assert assessment.projected_position_ratio is None


def test_position_limit_exceeded_blocks_as_position_concentration() -> None:
    # projected_position_ratio = (400,000+100,000)/(1,000,000+100,000)
    #                           = 500,000/1,100,000 ≈ 0.4545 > 0.20
    _, eligibility = _call(
        current_market_value=Decimal("400000"),
        sector_total_market_value=Decimal("400000"),
    )

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.POSITION_CONCENTRATION
    assert eligibility.block_reason == "POSITION_LIMIT_EXCEEDED"


def test_sector_limit_exceeded_blocks_as_sector_concentration_when_position_ok() -> None:
    # current_market_value stays small (position ratio ok) but sector total is large.
    # current_position_ratio = 50,000/1,000,000 = 0.05
    # projected = 150,000/1,100,000 ≈ 0.136 (< 0.20, OK)
    # sector_total = 450,000; projected_sector_ratio = 550,000/1,100,000 = 0.50 (> 0.35, exceeded)
    _, eligibility = _call(
        current_market_value=Decimal("50000"),
        sector_total_market_value=Decimal("450000"),
    )

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.SECTOR_CONCENTRATION
    assert eligibility.block_reason == "SECTOR_LIMIT_EXCEEDED"


def test_priority_order_conflicting_action_wins_over_all_other_blockers() -> None:
    """複数のブロック条件が同時に該当しても、優先順位トップの理由だけが返る。"""
    assessment, eligibility = _call(
        conflicting_holding_action=RecommendationType.URGENT_REVIEW,
        holding_data_inconsistent=True,
        holding_is_odd_lot=True,
        portfolio_valuation_basis=PortfolioValuationBasis.UNAVAILABLE,
        config=_config(block_add_on_on_odd_lot=True),
    )

    assert eligibility.block_category == EligibilityBlockCategory.CONFLICTING_HOLDING_ACTION
    # reasonsには該当した全条件が記録される(監査用の完全な記録は失わない)
    assert "CONFLICTING_HOLDING_ACTION:URGENT_REVIEW" in assessment.reasons
    assert "HOLDING_DATA_INCONSISTENT" in assessment.reasons
    assert "ODD_LOT_HOLDING" in assessment.reasons
    assert "CONCENTRATION_RELIABILITY_INSUFFICIENT" in assessment.reasons


def test_priority_order_data_inconsistency_wins_over_reliability_and_concentration() -> None:
    _, eligibility = _call(
        holding_data_inconsistent=True,
        portfolio_valuation_basis=PortfolioValuationBasis.UNAVAILABLE,
        config=_config(),
    )

    assert eligibility.block_category == EligibilityBlockCategory.HOLDING_DATA_INCONSISTENT


def test_priority_order_position_concentration_wins_over_sector_concentration() -> None:
    # Both position and sector limits are exceeded; position must win (checked first).
    _, eligibility = _call(
        current_market_value=Decimal("400000"),
        sector_total_market_value=Decimal("450000"),
    )

    assert eligibility.block_category == EligibilityBlockCategory.POSITION_CONCENTRATION


def test_config_disabled_bypasses_all_blocks_regardless_of_conditions() -> None:
    """add_on.enabled=Falseなら、他の全条件が不合格でも常にeligible=Trueとなる。"""
    assessment, eligibility = _call(
        conflicting_holding_action=RecommendationType.SELL,
        holding_data_inconsistent=True,
        portfolio_valuation_basis=PortfolioValuationBasis.UNAVAILABLE,
        config=_config(enabled=False),
    )

    assert eligibility.eligible is True
    assert eligibility.block_category is None
    # assessmentの計算自体(監査用途)は行われたままである
    assert assessment.portfolio_data_reliable is False
