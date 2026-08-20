"""profit_taking_service._build_not_yet_action_reasons()の業種専用モデル文言のテスト
(2026-07仕様レビュー対応、要求仕様§8)。

内部設計用語「専用モデルが未適用」をそのまま利用者向け通知に出さず、業種が
安全に取得できる場合だけ自然な文言に変換することを検証する。個別銘柄の
ハードコードではなく、ProfitTakingIndustrySectorの値のみで分岐することを
確認する。
"""

from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel, ProfitTakingIndustrySector
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    evaluate_profit_taking,
)
from jstock_advisor.domain.signals.trading_unit_feasibility import TradingUnitFeasibility
from jstock_advisor.services.profit_taking_service import _build_not_yet_action_reasons

_CONFIG = load_config()
_FEASIBLE = TradingUnitFeasibility(
    trading_unit=100,
    minimum_sellable_shares=100,
    partial_sale_executable=True,
    odd_lot_trading_available=False,
)


def _result():
    fv = FairValueRange(
        bear=Decimal("1000"),
        neutral=Decimal("1100"),
        bull=Decimal("1200"),
        overall_confidence=ConfidenceLevel.HIGH,
        methods_used=[
            FairValueMethodResult(
                method="m", fair_value=Decimal("1100"), confidence=ConfidenceLevel.HIGH
            )
        ],
        methods_excluded=[],
        usable_for_trading_judgment=True,
    )
    return evaluate_profit_taking(
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
        condition_inputs=ProfitTakingConditionInputs(fair_value_range=fv),
    )


def _reasons(
    industry_sector: ProfitTakingIndustrySector, industry_model_applied: bool
) -> list[str]:
    return _build_not_yet_action_reasons(
        result=_result(),
        config=_CONFIG,
        fair_value_overall_confidence=ConfidenceLevel.HIGH,
        industry_sector=industry_sector,
        industry_model_applied=industry_model_applied,
        days_to_next_earnings_business_days=None,
        trading_unit_feasibility=_FEASIBLE,
        has_strong_counter_material=False,
        is_uptrend=False,
    )


# テストコード削減対応2026-08: model_applied=False時の3関数(GENERAL/UNKNOWN/
# BANKING)はsector・期待文言だけが違う同一構造のため統合する。GENERALのみ
# 追加で「専用モデルが未適用」という内部設計用語が漏れないことも検証していた
# ため、must_not_containでこの観点も失わずに保持する。model_applied=True
# (test_no_industry_wording_when_model_applied)は逆方向assertのため統合せず
# 個別関数のまま維持する(要求仕様§8対応、Agent分析での明示的な推奨に従う)。
@pytest.mark.parametrize(
    ("sector", "expected_in_reasons", "must_not_contain"),
    [
        (
            ProfitTakingIndustrySector.GENERAL,
            "現在の適正価格は汎用モデルによる参考値です",
            ["専用モデルが未適用"],
        ),
        (
            ProfitTakingIndustrySector.UNKNOWN,
            "業種特性を反映した専用評価モデルではありません",
            [],
        ),
        (
            ProfitTakingIndustrySector.BANKING,
            "銀行業の事業特性を十分に反映した専用評価モデルではありません",
            [],
        ),
    ],
    ids=[
        "general_uses_generic_reference_model_wording",
        "unknown_uses_generic_fallback_wording",
        "specific_sector_includes_industry_label_when_safely_available",
    ],
)
def test_industry_wording_when_model_not_applied(
    sector: ProfitTakingIndustrySector,
    expected_in_reasons: str,
    must_not_contain: list[str],
) -> None:
    reasons = _reasons(sector, industry_model_applied=False)
    assert expected_in_reasons in reasons
    joined = " ".join(reasons)
    for forbidden in must_not_contain:
        assert forbidden not in joined


def test_no_industry_wording_when_model_applied() -> None:
    reasons = _reasons(ProfitTakingIndustrySector.GENERAL, industry_model_applied=True)
    joined = " ".join(reasons)
    assert "専用評価モデル" not in joined
    assert "汎用モデルによる参考値" not in joined
