"""Issue #471: 利確の上限価格が使えない原因を、原因ごとの理由コードで利用者・監査へ伝える。

`_fair_value_action_usable()`(domain/signals/profit_taking.py)が False になる原因のうち、以前は
スプレッド超過の1種類だけが理由コードを持っていた。#471(USER決定 U-a〜U-d)は、次の4つを足す。

    TOO_FEW_METHODS_FOR_ACTION                   手法数が利確判定側の下限未満
    FAIR_VALUE_NOT_REFLECTING_LATEST_EARNINGS    適正価格が最新決算を反映していない(False)
    FAIR_VALUE_EARNINGS_REFLECTION_UNKNOWN       最新決算の反映を判定できない(None)
    EARNINGS_TOO_CLOSE_FOR_ACTION                次回決算までの営業日数が利確判定側の下限未満

不変条件を固定する:
  * **判定の真偽は変わらない**。理由コードが1つでも返れば`fair_value_action_usable`はFalse、
    usableなら理由コードは空。usable=Falseで理由コードが空なのは、構造化の対象外(レンジ無し・レンジ
    不可・bull/bear欠如・業種・含み損)の原因だけ(全組合せで固定)。
  * 複数原因が同時に成立する場合、利用者表示は最初の1つ、監査は全原因。
    順序は評価順で、定数として固定。
  * 「次回決算まで N 営業日」は、理由コードの文言へ置き換えた(同じ事実を2行にしない。USER決定 U-b)。
  * 文言は USER確定の原文どおり(閾値はconfigの実値)。
  * 銘柄分析の固定文言辞書は、全コードを持つ(未知のコードは従来どおり汎用文言へ落ちる)。

★ 銘柄コード・保有数量・取得単価は架空値のみ。Productionへは一切アクセスしない。
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    IndustryClassification,
    ProfitTakingIndustrySector,
    RecommendationType,
)
from jstock_advisor.domain.entities.valuation import (
    FairValueMethodResult,
    FairValueRange,
    ProfitTakingFairValueBlockReasonCode,
)
from jstock_advisor.domain.signals.profit_taking import (
    _FAIR_VALUE_ACTION_BLOCK_REASON_ORDER,
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    ProfitTakingResult,
    _fair_value_action_block_reasons,
    evaluate_profit_taking,
)
from jstock_advisor.domain.signals.trading_unit_feasibility import TradingUnitFeasibility
from jstock_advisor.services.profit_taking_service import (
    _build_not_yet_action_reasons,
    _profit_taking_fair_value_block_reason_text,
)
from jstock_advisor.services.stock_analysis_view_service import (
    _FAIR_VALUE_UNUSABLE_GENERIC_TEXT,
    _FAIR_VALUE_UNUSABLE_TEXTS,
)

_C = ProfitTakingFairValueBlockReasonCode
_CONFIG = load_config()
_PT = _CONFIG.profit_taking
_CBJ = _PT.condition_based_judgment

_AVERAGE_PRICE = Decimal("1000")
_GAIN_PRICE = Decimal("1600")  # +60%。適正価格レンジが使えれば FULL_PROFIT_TAKE へ到達する。
_FEASIBLE = TradingUnitFeasibility(
    trading_unit=100,
    minimum_sellable_shares=100,
    partial_sale_executable=True,
    odd_lot_trading_available=False,
)

_EXPECTED_ORDER = (
    _C.TOO_FEW_METHODS_FOR_ACTION,
    _C.METHOD_SPREAD_TOO_WIDE_FOR_ACTION,
    _C.FAIR_VALUE_NOT_REFLECTING_LATEST_EARNINGS,
    _C.FAIR_VALUE_EARNINGS_REFLECTION_UNKNOWN,
    _C.EARNINGS_TOO_CLOSE_FOR_ACTION,
)


def _range(
    *,
    bear: str | None = "1500",
    neutral: str | None = "1560",
    bull: str | None = "1620",
    method_count: int = 3,
    usable: bool = True,
) -> FairValueRange:
    def _d(value: str | None) -> Decimal | None:
        return None if value is None else Decimal(value)

    return FairValueRange(
        bear=_d(bear),
        neutral=_d(neutral),
        bull=_d(bull),
        overall_confidence=ConfidenceLevel.MEDIUM,
        methods_used=[
            FairValueMethodResult(
                method=f"method{i}",
                fair_value=Decimal("1560"),
                confidence=ConfidenceLevel.MEDIUM,
            )
            for i in range(method_count)
        ],
        methods_excluded=[],
        usable_for_trading_judgment=usable,
    )


def _evaluate(
    *,
    current_price: Decimal = _GAIN_PRICE,
    **condition_overrides: object,
) -> ProfitTakingResult:
    """基準入力(使える入力)から、指定した項目だけを変えて評価する。"""
    condition_kwargs: dict[str, object] = {
        "fair_value_range": _range(),
        "fair_value_reflects_latest_earnings": True,
        "industry_classification": IndustryClassification.GENERAL_CORPORATE,
    }
    condition_kwargs.update(condition_overrides)
    return evaluate_profit_taking(
        current_price=current_price,
        average_purchase_price=_AVERAGE_PRICE,
        shares=100,
        total_purchase_amount=_AVERAGE_PRICE * 100,
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_PT,
        condition_inputs=ProfitTakingConditionInputs(**condition_kwargs),  # type: ignore[arg-type]
    )


def _wide_spread_range() -> FairValueRange:
    """スプレッド(bull / bear)が利確判定側の上限を超えるレンジ。"""
    ratio = _CBJ.max_fair_value_spread_ratio_for_partial + 0.1
    return _range(bear="1000", bull=str(Decimal("1000") * Decimal(str(ratio))))


# --- 基準(対照)------------------------------------------------------------------


def test_control_base_input_has_no_block_reason() -> None:
    result = _evaluate()

    assert result.fair_value_action_usable is True
    assert result.fair_value_action_block_reason_code is None
    assert result.fair_value_action_block_reason_codes == ()
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE


def test_the_enum_has_exactly_the_five_documented_codes() -> None:
    assert {c.value for c in _C} == {
        "METHOD_SPREAD_TOO_WIDE_FOR_ACTION",
        "TOO_FEW_METHODS_FOR_ACTION",
        "FAIR_VALUE_NOT_REFLECTING_LATEST_EARNINGS",
        "FAIR_VALUE_EARNINGS_REFLECTION_UNKNOWN",
        "EARNINGS_TOO_CLOSE_FOR_ACTION",
    }


# --- 原因 → 理由コード(1変数。基準から1つだけ変える)------------------------------------


def test_too_few_methods_has_its_code_and_the_boundary() -> None:
    minimum = _CBJ.min_fair_value_methods_for_partial

    below = _evaluate(fair_value_range=_range(method_count=minimum - 1))
    at_boundary = _evaluate(fair_value_range=_range(method_count=minimum))

    assert below.fair_value_action_usable is False
    assert below.fair_value_action_block_reason_code == _C.TOO_FEW_METHODS_FOR_ACTION.value
    assert below.fair_value_action_block_reason_codes == (_C.TOO_FEW_METHODS_FOR_ACTION.value,)
    assert at_boundary.fair_value_action_usable is True
    assert at_boundary.fair_value_action_block_reason_codes == ()  # 下限ちょうどは使える


def test_spread_too_wide_keeps_its_existing_code() -> None:
    result = _evaluate(fair_value_range=_wide_spread_range())

    assert result.fair_value_action_usable is False
    assert result.fair_value_action_block_reason_code == _C.METHOD_SPREAD_TOO_WIDE_FOR_ACTION.value
    assert result.fair_value_action_block_reason_codes == (
        _C.METHOD_SPREAD_TOO_WIDE_FOR_ACTION.value,
    )


def test_earnings_not_reflected_and_unknown_are_distinguished() -> None:
    """★ 「反映していない」(False)と「判定できない」(None)を分ける。Noneを Falseと断定しない。"""
    not_reflected = _evaluate(fair_value_reflects_latest_earnings=False)
    unknown = _evaluate(fair_value_reflects_latest_earnings=None)

    assert not_reflected.fair_value_action_block_reason_codes == (
        _C.FAIR_VALUE_NOT_REFLECTING_LATEST_EARNINGS.value,
    )
    assert unknown.fair_value_action_block_reason_codes == (
        _C.FAIR_VALUE_EARNINGS_REFLECTION_UNKNOWN.value,
    )
    assert not_reflected.fair_value_action_usable is False
    assert unknown.fair_value_action_usable is False


def test_earnings_too_close_has_its_code_and_the_boundary() -> None:
    minimum = _CBJ.min_business_days_to_earnings_for_fair_value_action

    close = _evaluate(days_to_next_earnings_business_days=minimum - 1)
    at_boundary = _evaluate(days_to_next_earnings_business_days=minimum)
    unknown_days = _evaluate(days_to_next_earnings_business_days=None)

    assert close.fair_value_action_block_reason_code == _C.EARNINGS_TOO_CLOSE_FOR_ACTION.value
    assert at_boundary.fair_value_action_usable is True  # 下限ちょうどは使える
    assert at_boundary.fair_value_action_block_reason_codes == ()
    assert unknown_days.fair_value_action_block_reason_codes == ()  # 不明を「直前」と扱わない


def test_the_reason_function_applies_the_same_boundaries_as_the_usable_decision() -> None:
    """★ 理由の導出関数を直接呼ぶ(呼び出し側の「使えないときだけ」の条件を介さない)。

    別の原因(反映不明)で使えない状態でも、決算直前の境界は判定と同じ(下限ちょうどは含めない)。
    手法数・決算直前の境界を、理由の導出だけが勝手にずらす回帰を捕まえる。
    """
    minimum_days = _CBJ.min_business_days_to_earnings_for_fair_value_action
    minimum_methods = _CBJ.min_fair_value_methods_for_partial

    def _codes(*, days: int | None, methods: int) -> tuple[_C, ...]:
        return _fair_value_action_block_reasons(
            _range(method_count=methods),
            ProfitTakingConditionInputs(
                fair_value_range=_range(method_count=methods),
                fair_value_reflects_latest_earnings=None,
                days_to_next_earnings_business_days=days,
                industry_classification=IndustryClassification.GENERAL_CORPORATE,
            ),
            _PT,
        )

    unknown = _C.FAIR_VALUE_EARNINGS_REFLECTION_UNKNOWN
    assert _codes(days=minimum_days, methods=minimum_methods) == (unknown,)
    assert _codes(days=minimum_days - 1, methods=minimum_methods) == (
        unknown,
        _C.EARNINGS_TOO_CLOSE_FOR_ACTION,
    )
    assert _codes(days=None, methods=minimum_methods) == (unknown,)
    assert _codes(days=minimum_days, methods=minimum_methods - 1) == (
        _C.TOO_FEW_METHODS_FOR_ACTION,
        unknown,
    )


# --- 複数原因(U-c): 利用者表示は最初の1つ・監査は全原因・順序は定数で固定 ---------------------


def test_the_order_is_an_explicit_constant() -> None:
    assert _FAIR_VALUE_ACTION_BLOCK_REASON_ORDER == _EXPECTED_ORDER


def test_all_causes_at_once_keep_the_fixed_order_and_the_first_is_the_display_code() -> None:
    result = _evaluate(
        fair_value_range=_range(method_count=_CBJ.min_fair_value_methods_for_partial - 1),
        fair_value_reflects_latest_earnings=False,
        days_to_next_earnings_business_days=(
            _CBJ.min_business_days_to_earnings_for_fair_value_action - 1
        ),
    )

    assert result.fair_value_action_block_reason_codes == (
        _C.TOO_FEW_METHODS_FOR_ACTION.value,
        _C.FAIR_VALUE_NOT_REFLECTING_LATEST_EARNINGS.value,
        _C.EARNINGS_TOO_CLOSE_FOR_ACTION.value,
    )
    assert result.fair_value_action_block_reason_code == _C.TOO_FEW_METHODS_FOR_ACTION.value


def test_the_display_code_is_the_first_of_the_audit_codes_for_every_pair() -> None:
    """どの2原因の組合せでも、表示コード = 監査コードの先頭 = 固定順での最初。"""
    causes: dict[_C, dict[str, Any]] = {
        _C.TOO_FEW_METHODS_FOR_ACTION: {
            "fair_value_range": _range(method_count=_CBJ.min_fair_value_methods_for_partial - 1)
        },
        _C.FAIR_VALUE_NOT_REFLECTING_LATEST_EARNINGS: {
            "fair_value_reflects_latest_earnings": False
        },
        _C.EARNINGS_TOO_CLOSE_FOR_ACTION: {
            "days_to_next_earnings_business_days": (
                _CBJ.min_business_days_to_earnings_for_fair_value_action - 1
            )
        },
    }
    for a, b in itertools.combinations(causes, 2):
        overrides = {**causes[a], **causes[b]}
        result = _evaluate(**overrides)

        expected = [c.value for c in _EXPECTED_ORDER if c in (a, b)]
        assert list(result.fair_value_action_block_reason_codes) == expected
        assert result.fair_value_action_block_reason_code == expected[0]


# --- 判定の真偽は変わらない(全組合せ)---------------------------------------------------


def _out_of_scope_cause(
    fv_range: FairValueRange | None, industry: IndustryClassification | None
) -> bool:
    """構造化の対象外(USER決定 U-a)の原因が成立するか。"""
    if fv_range is None or not fv_range.usable_for_trading_judgment or fv_range.bull is None:
        return True
    if fv_range.bear is None or fv_range.bear <= 0:
        return True
    return industry != IndustryClassification.GENERAL_CORPORATE


def test_reason_codes_never_contradict_the_usable_decision_over_all_combinations() -> None:
    """★ 理由コードは、構造化する原因が成立するときだけ返る。usableなら空。

    usable=Falseで空なのは、対象外の原因(レンジ無し・不可・bull/bear欠如・業種)だけ。
    """
    ranges: list[FairValueRange | None] = [
        None,
        _range(usable=False),
        _range(bull=None),
        _range(bear=None),
        _range(bear="0"),
        _range(),
        _range(method_count=_CBJ.min_fair_value_methods_for_partial - 1),
        _wide_spread_range(),
    ]
    reflects = [True, False, None]
    minimum_days = _CBJ.min_business_days_to_earnings_for_fair_value_action
    days = [None, minimum_days - 1, minimum_days]
    industries: list[IndustryClassification | None] = [
        IndustryClassification.GENERAL_CORPORATE,
        None,
    ]

    count = 0
    for fv_range, reflect, day, industry in itertools.product(ranges, reflects, days, industries):
        result = _evaluate(
            fair_value_range=fv_range,
            fair_value_reflects_latest_earnings=reflect,
            days_to_next_earnings_business_days=day,
            industry_classification=industry,
        )
        codes = result.fair_value_action_block_reason_codes
        if result.fair_value_action_usable:
            assert codes == (), (fv_range, reflect, day, industry)
            assert result.fair_value_action_block_reason_code is None
        elif not codes:
            # 理由コードが空のまま使えない = 構造化の対象外の原因が効いている場合だけ。
            assert _out_of_scope_cause(fv_range, industry), (fv_range, reflect, day, industry)
            assert result.fair_value_action_block_reason_code is None
        else:
            assert result.fair_value_action_block_reason_code == codes[0]
        count += 1
    assert count == len(ranges) * len(reflects) * len(days) * len(industries)


def test_an_unrealized_loss_has_no_block_reason_even_if_a_cause_holds() -> None:
    """含み損では「利確」が成立しないため、原因が成立していても理由コードは付けない(#467)。"""
    result = _evaluate(current_price=Decimal("900"), fair_value_reflects_latest_earnings=False)

    assert result.fair_value_action_usable is False
    assert result.fair_value_action_block_reason_code is None
    assert result.fair_value_action_block_reason_codes == ()


# --- 利用者向け文言(USER確定の原文。nはconfigの実値)-------------------------------------


def test_every_code_has_a_user_text_and_none_raises() -> None:
    for code in _C:
        text = _profit_taking_fair_value_block_reason_text(code, _CONFIG)
        assert text


def test_user_texts_are_exactly_the_confirmed_wording() -> None:
    methods = _CBJ.min_fair_value_methods_for_partial
    days = _CBJ.min_business_days_to_earnings_for_fair_value_action - 1

    assert _profit_taking_fair_value_block_reason_text(_C.TOO_FEW_METHODS_FOR_ACTION, _CONFIG) == (
        "適正価格の根拠がまだ十分ではないため、今回は価格を基準にした利確判断を見送ります"
        f"(必要:{methods}手法以上)"
    )
    assert (
        _profit_taking_fair_value_block_reason_text(
            _C.FAIR_VALUE_NOT_REFLECTING_LATEST_EARNINGS, _CONFIG
        )
        == "適正価格に最新の決算が反映されていないため、今回は価格を基準にした利確判断を見送ります"
    )
    assert _profit_taking_fair_value_block_reason_text(
        _C.FAIR_VALUE_EARNINGS_REFLECTION_UNKNOWN, _CONFIG
    ) == (
        "適正価格に最新の決算が反映されているか確認できないため、"
        "今回は価格を基準にした利確判断を見送ります"
    )
    assert (
        _profit_taking_fair_value_block_reason_text(_C.EARNINGS_TOO_CLOSE_FOR_ACTION, _CONFIG)
        == f"次回決算まで{days}営業日以内のため、今回は価格を基準にした利確判断を見送ります"
    )


def test_the_earnings_text_uses_the_blocking_threshold_as_its_single_source() -> None:
    """決算直前の閾値は、遮断側のconfigの1か所(表示側の別configではない)。configを変えると文言も変わる。"""
    cbj = _CBJ.model_copy(update={"min_business_days_to_earnings_for_fair_value_action": 7})
    changed = _CONFIG.model_copy(
        update={"profit_taking": _PT.model_copy(update={"condition_based_judgment": cbj})}
    )
    text = _profit_taking_fair_value_block_reason_text(_C.EARNINGS_TOO_CLOSE_FOR_ACTION, changed)

    assert "次回決算まで6営業日以内" in text


def _reasons(result: ProfitTakingResult) -> list[str]:
    return _build_not_yet_action_reasons(
        result=result,
        config=_CONFIG,
        fair_value_overall_confidence=ConfidenceLevel.HIGH,
        industry_sector=ProfitTakingIndustrySector.GENERAL,
        industry_model_applied=True,
        trading_unit_feasibility=_FEASIBLE,
        has_strong_counter_material=False,
        is_uptrend=False,
        fair_value_unusable_reason_code=None,
    )


def test_the_old_earnings_line_is_replaced_not_duplicated() -> None:
    """★ U-b = REPLACE: 「次回決算まで N 営業日」の行は無く、理由コードの文言が1行だけ出る。"""
    days = _CBJ.min_business_days_to_earnings_for_fair_value_action - 1
    result = _evaluate(days_to_next_earnings_business_days=days)

    reasons = _reasons(result)

    assert f"次回決算まで{days}営業日" not in reasons  # 旧文言(完全一致)は出ない
    earnings_lines = [r for r in reasons if "次回決算まで" in r]
    assert earnings_lines == [
        f"次回決算まで{days}営業日以内のため、今回は価格を基準にした利確判断を見送ります"
    ]


def test_the_earnings_line_does_not_appear_when_the_cause_does_not_hold() -> None:
    minimum = _CBJ.min_business_days_to_earnings_for_fair_value_action

    reasons = _reasons(_evaluate(days_to_next_earnings_business_days=minimum))

    assert not any("次回決算まで" in r for r in reasons)


def test_only_the_first_cause_is_shown_to_the_user_while_all_are_in_the_audit_codes() -> None:
    result = _evaluate(
        fair_value_range=_range(method_count=_CBJ.min_fair_value_methods_for_partial - 1),
        fair_value_reflects_latest_earnings=False,
        days_to_next_earnings_business_days=(
            _CBJ.min_business_days_to_earnings_for_fair_value_action - 1
        ),
    )

    reasons = _reasons(result)
    shown = [r for r in reasons if "今回は価格を基準にした利確判断を見送ります" in r]

    assert len(shown) == 1  # 利用者表示は最初の原因1つ
    assert shown[0].startswith("適正価格の根拠がまだ十分ではないため")
    assert len(result.fair_value_action_block_reason_codes) == 3  # 監査は全原因


# --- 銘柄分析の表示(固定文言の辞書)-----------------------------------------------------


def test_the_view_dictionary_covers_every_code_and_unknown_codes_fall_back() -> None:
    for code in _C:
        assert _FAIR_VALUE_UNUSABLE_TEXTS[code.value]
    assert (
        "SOME_FUTURE_CODE" not in _FAIR_VALUE_UNUSABLE_TEXTS
    )  # 未知のコードは辞書に無い = 汎用文言
    assert _FAIR_VALUE_UNUSABLE_GENERIC_TEXT
