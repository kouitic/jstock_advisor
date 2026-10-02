"""profit_taking_service._build_not_yet_action_reasons()/_build_valuation_caveats()の
表示文言のテスト(2026-07仕様レビュー対応、要求仕様§8。Issue #701で両関数へ分離)。

Issue #701(2026-10、USER確定方針): 「まだ利確しない理由」は実際に判定を遮断・
降格した事実のみ(A/B categories)、「判断上の留意点」は判定を直接変更して
いない参考情報のみ(C category)、PARTIAL固有の実行制約は
effective_recommendation_type==PARTIAL_PROFIT_TAKEの場合のみ(D category、
OD-1)。内部設計用語「専用モデルが未適用」をそのまま利用者向け通知に出さず、
業種が安全に取得できる場合だけ自然な文言に変換することも引き続き検証する。
"""

import dataclasses
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    ProfitTakingIndustrySector,
    RecommendationType,
)
from jstock_advisor.domain.entities.valuation import (
    FairValueMethodResult,
    FairValueRange,
    FairValueUnusableReasonCode,
    ProfitTakingFairValueBlockReasonCode,
)
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    ProfitTakingResult,
    evaluate_profit_taking,
)
from jstock_advisor.domain.signals.trading_unit_feasibility import TradingUnitFeasibility
from jstock_advisor.services.profit_taking_service import (
    _build_not_yet_action_reasons,
    _build_valuation_caveats,
)

_CONFIG = load_config()
_FEASIBLE = TradingUnitFeasibility(
    trading_unit=100,
    minimum_sellable_shares=100,
    partial_sale_executable=True,
    odd_lot_trading_available=False,
)
_INFEASIBLE = TradingUnitFeasibility(
    trading_unit=100,
    minimum_sellable_shares=100,
    partial_sale_executable=False,
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


def _reasons_for(
    result: ProfitTakingResult,
    *,
    trading_unit_feasibility: TradingUnitFeasibility = _FEASIBLE,
    fair_value_unusable_reason_code: FairValueUnusableReasonCode | None = None,
    effective_recommendation_type: RecommendationType = RecommendationType.FULL_PROFIT_TAKE,
) -> list[str]:
    return _build_not_yet_action_reasons(
        result=result,
        config=_CONFIG,
        trading_unit_feasibility=trading_unit_feasibility,
        fair_value_unusable_reason_code=fair_value_unusable_reason_code,
        effective_recommendation_type=effective_recommendation_type,
    )


def _caveats(
    industry_sector: ProfitTakingIndustrySector = ProfitTakingIndustrySector.GENERAL,
    industry_model_applied: bool = True,
    fair_value_overall_confidence: ConfidenceLevel | None = ConfidenceLevel.HIGH,
    has_strong_counter_material: bool = False,
    is_uptrend: bool = False,
    mitigating_downgrade_applied: bool = False,
    timing_downgrade_applied: bool = False,
) -> list[str]:
    return _build_valuation_caveats(
        fair_value_overall_confidence=fair_value_overall_confidence,
        industry_sector=industry_sector,
        industry_model_applied=industry_model_applied,
        has_strong_counter_material=has_strong_counter_material,
        is_uptrend=is_uptrend,
        mitigating_downgrade_applied=mitigating_downgrade_applied,
        timing_downgrade_applied=timing_downgrade_applied,
    )


# --- 業種専用モデル文言(「判断上の留意点」側、_build_valuation_caveats) -------
# テストコード削減対応2026-08: model_applied=False時の3関数(GENERAL/UNKNOWN/
# BANKING)はsector・期待文言だけが違う同一構造のため統合する。GENERALのみ
# 追加で「専用モデルが未適用」という内部設計用語が漏れないことも検証していた
# ため、must_not_containでこの観点も失わずに保持する。model_applied=True
# (test_no_industry_wording_when_model_applied)は逆方向assertのため統合せず
# 個別関数のまま維持する(要求仕様§8対応、Agent分析での明示的な推奨に従う)。
@pytest.mark.parametrize(
    ("sector", "expected_in_caveats", "must_not_contain"),
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
    expected_in_caveats: str,
    must_not_contain: list[str],
) -> None:
    caveats = _caveats(sector, industry_model_applied=False)
    assert expected_in_caveats in caveats
    joined = " ".join(caveats)
    for forbidden in must_not_contain:
        assert forbidden not in joined


def test_no_industry_wording_when_model_applied() -> None:
    caveats = _caveats(ProfitTakingIndustrySector.GENERAL, industry_model_applied=True)
    joined = " ".join(caveats)
    assert "専用評価モデル" not in joined
    assert "汎用モデルによる参考値" not in joined


# --- Issue #21(2026-08-28): usable_for_trading_judgment=False時の実遮断理由表示 ---
# 従来は、実際に価格基準の利確判定を遮断した理由(手法不足/乖離過大)が
# どこにも表示されず、ほぼ常時発火する業種モデル文言だけが見えていた。
# 分岐は構造化code(FairValueUnusableReasonCode)で行い、自由文をparseしない。
# (「まだ利確しない理由」側、_build_not_yet_action_reasons。Issue #701後も
# B categoryとして維持される)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            FairValueUnusableReasonCode.NO_VALID_METHODS,
            "適正価格を算出できる有効な評価手法がないため、価格基準の利確判定に使用していません",
        ),
        (
            FairValueUnusableReasonCode.TOO_FEW_METHODS,
            "適正価格を算出できる評価手法が不足しているため、価格基準の利確判定に使用していません",
        ),
        (
            FairValueUnusableReasonCode.METHOD_SPREAD_TOO_WIDE,
            "適正価格の算出手法間の乖離が大きいため、価格基準の利確判定に使用していません",
        ),
    ],
    ids=["no_valid_methods", "too_few_methods", "method_spread_too_wide"],
)
def test_issue21_unusable_reason_text_by_code(
    code: FairValueUnusableReasonCode, expected: str
) -> None:
    reasons = _reasons_for(_result(), fair_value_unusable_reason_code=code)
    assert expected in reasons


def test_issue21_no_unusable_wording_when_code_is_none() -> None:
    """usable=True(code=None)では新文言は一切追加されない(従来表示のまま)。"""
    reasons = _reasons_for(_result(), fair_value_unusable_reason_code=None)
    joined = " ".join(reasons)
    assert "価格基準の利確判定に使用していません" not in joined


# --- Issue #221 Phase 1(U2): 遮断要因と降格の事実を理由へ出す ---------------


def test_profit_taking_spread_block_reason_is_shown_with_config_threshold() -> None:
    """1.30 帯の遮断理由が表示され、閾値は config の実値が埋め込まれる。"""
    base = _result()
    result = dataclasses.replace(
        base,
        fair_value_action_block_reason_code=(
            ProfitTakingFairValueBlockReasonCode.METHOD_SPREAD_TOO_WIDE_FOR_ACTION.value
        ),
    )
    reasons = _reasons_for(result)

    cbj = _CONFIG.profit_taking.condition_based_judgment
    threshold = cbj.max_fair_value_spread_ratio_for_partial
    matched = [r for r in reasons if "手法間の広がり" in r]
    assert matched, reasons
    # config の実値をそのまま使い、書式のみ指定する(値はハードコードしない)。
    assert f"{threshold:.2f}倍" in matched[0]


def test_no_spread_block_reason_when_code_is_absent() -> None:
    reasons = _reasons_for(_result())
    assert not [r for r in reasons if "手法間の広がり" in r]


def test_downgrade_facts_are_shown_in_reasons() -> None:
    """緩和要因・上昇トレンドが実際に判定を下げた事実が理由に出る。"""
    result = dataclasses.replace(
        _result(), mitigating_downgrade_applied=True, timing_downgrade_applied=True
    )
    reasons = _reasons_for(result)

    assert any("反対材料により、利確の判定を1段階弱めています" in r for r in reasons), reasons
    assert any("上昇トレンドの継続により、利確の判定を1段階弱めています" in r for r in reasons), (
        reasons
    )


def test_downgrade_facts_absent_when_not_applied() -> None:
    """材料が該当していても、実際に降格していなければ「まだ利確しない理由」
    には書かない(該当の事実自体は「判断上の留意点」側で扱う、OD-5)。"""
    reasons = _reasons_for(_result())

    assert not [r for r in reasons if "1段階弱めています" in r]
    assert not [r for r in reasons if "強い上昇トレンドが継続" in r]


# --- Issue #701: OD-1〜OD-5 + USER確定の12項目の最低限テスト -----------------


def test_1_full_medium_uptrend_partial_infeasible() -> None:
    """1. FULL+MEDIUM+uptrend+partial不可。
    旧30%閾値・PARTIAL専用execution constraintはいずれも出ない。
    MEDIUM・uptrendは「判断上の留意点」として必要に応じ出る。"""
    reasons = _reasons_for(
        _result(),
        trading_unit_feasibility=_INFEASIBLE,
        effective_recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
    )
    assert not [r for r in reasons if "一部利確基準" in r]
    assert not [r for r in reasons if "一部売却が実行できない" in r]

    caveats = _caveats(fair_value_overall_confidence=ConfidenceLevel.MEDIUM, is_uptrend=True)
    assert "適正価格モデルの信頼度がMEDIUM" in caveats
    assert "強い上昇トレンドが継続" in caveats


def test_2_full_high_no_unnecessary_reasons() -> None:
    """2. FULL+HIGH、実際の遮断要因も無い場合。不要な「まだ利確しない理由」は
    出ない。caveatも不要なら空。"""
    # _result()の既定fixtureはmethods_used=1件のため、evaluate_profit_taking()
    # 自身がTOO_FEW_METHODS_FOR_ACTIONを計算する(手法不足という実際の遮断
    # 要因)。本テストは「遮断要因が無い場合」を検証したいため、明示的に
    # Noneへ戻す(B categoryの遮断要因自体のテストはtest_issue21_*が別途担う)。
    result = dataclasses.replace(
        _result(), fair_value_action_block_reason_code=None, fair_value_action_block_reason_codes=()
    )
    reasons = _reasons_for(
        result, effective_recommendation_type=RecommendationType.FULL_PROFIT_TAKE
    )
    assert reasons == []

    caveats = _caveats(fair_value_overall_confidence=ConfidenceLevel.HIGH)
    assert caveats == []


def test_3_partial_feasible_no_execution_constraint() -> None:
    """3. PARTIAL+partial_sale_executable=true。execution constraintなし。"""
    reasons = _reasons_for(
        _result(),
        trading_unit_feasibility=_FEASIBLE,
        effective_recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
    )
    assert not [r for r in reasons if "一部売却が実行できない" in r]


def test_4_partial_infeasible_contract_guard() -> None:
    """4. PARTIAL+partial_sale_executable=false。ガード単体の契約テストとして
    execution constraintあり(実運用では#700の早期returnにより到達不能だが、
    _build_not_yet_action_reasons()自体の契約として固定する)。"""
    reasons = _reasons_for(
        _result(),
        trading_unit_feasibility=_INFEASIBLE,
        effective_recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
    )
    assert any("一部売却が実行できない" in r for r in reasons), reasons


def test_5_full_infeasible_no_execution_constraint() -> None:
    """5. FULL+partial_sale_executable=false。execution constraintなし
    (#700症状の回帰テストを兼ねる)。"""
    reasons = _reasons_for(
        _result(),
        trading_unit_feasibility=_INFEASIBLE,
        effective_recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
    )
    assert not [r for r in reasons if "一部売却が実行できない" in r]
    assert not [r for r in reasons if "届かず" in r]


def test_6_watch_actual_fair_value_blocker() -> None:
    """6. WATCH+actual fair-value blocker。blockerは「まだ利確しない理由」。"""
    reasons = _reasons_for(
        _result(),
        fair_value_unusable_reason_code=FairValueUnusableReasonCode.NO_VALID_METHODS,
        effective_recommendation_type=RecommendationType.WATCH,
    )
    assert any("価格基準の利確判定に使用していません" in r for r in reasons), reasons


def test_7_uptrend_with_timing_downgrade_applied() -> None:
    """7. is_uptrend=true+timing_downgrade_applied=true。actual downgrade理由
    あり/同内容のcaveatなし。"""
    result = dataclasses.replace(_result(), timing_downgrade_applied=True)
    reasons = _reasons_for(result)
    assert any("上昇トレンドの継続により、利確の判定を1段階弱めています" in r for r in reasons), (
        reasons
    )

    caveats = _caveats(is_uptrend=True, timing_downgrade_applied=True)
    assert not [c for c in caveats if "強い上昇トレンドが継続" in c]


def test_8_uptrend_without_timing_downgrade_applied() -> None:
    """8. is_uptrend=true+timing_downgrade_applied=false。downgrade理由なし/
    caveatあり。"""
    reasons = _reasons_for(_result())
    assert not [r for r in reasons if "1段階弱めています" in r]

    caveats = _caveats(is_uptrend=True, timing_downgrade_applied=False)
    assert "強い上昇トレンドが継続" in caveats


def test_9_counter_material_with_mitigating_downgrade_applied() -> None:
    """9. has_strong_counter_material=true+mitigating_downgrade_applied=true。
    actual downgrade理由あり/同内容caveatなし。"""
    result = dataclasses.replace(_result(), mitigating_downgrade_applied=True)
    reasons = _reasons_for(result)
    assert any("反対材料により、利確の判定を1段階弱めています" in r for r in reasons), reasons

    caveats = _caveats(has_strong_counter_material=True, mitigating_downgrade_applied=True)
    assert not [c for c in caveats if "増益・増配などの反対材料がある" in c]


def test_10_counter_material_without_mitigating_downgrade_applied() -> None:
    """10. has_strong_counter_material=true+mitigating_downgrade_applied=false。
    caveatあり。"""
    caveats = _caveats(has_strong_counter_material=True, mitigating_downgrade_applied=False)
    assert "増益・増配などの反対材料がある" in caveats


def test_11_full_profit_take_with_gain_below_stale_30pct_threshold() -> None:
    """11. gain=28.1/upside=-16.9相当でFULL_PROFIT_TAKEへ到達したケース
    (西部ガスHD 9536相当、#701発見契機)。「一部利確基準(30%)未満」は出ない。
    FULLと矛盾する「まだ利確しない理由」も出ない。"""
    result = dataclasses.replace(_result(), recommendation_type=RecommendationType.FULL_PROFIT_TAKE)
    reasons = _reasons_for(
        result, effective_recommendation_type=RecommendationType.FULL_PROFIT_TAKE
    )
    assert not [r for r in reasons if "一部利確基準" in r]
    assert not [r for r in reasons if "未満" in r]


def test_12_empty_reasons_list_is_allowed() -> None:
    """12. not_yet_action_reasons=[]を許容する(fallback文言を捏造しない)。"""
    result = dataclasses.replace(
        _result(), fair_value_action_block_reason_code=None, fair_value_action_block_reason_codes=()
    )
    reasons = _reasons_for(result)
    assert reasons == []
    # 空リストであることが契約であり、ここに何らかのfallback文言が
    # 無条件挿入されていないことを明示的に確認する。
    assert "適正価格モデルには手法間のばらつき等の不確実性がある" not in reasons
