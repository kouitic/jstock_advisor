"""Issue #467: 利確の上限価格(ceiling_price)が使えない原因ごとのテスト。

`_fair_value_action_usable()`(domain/signals/profit_taking.py)は、適正価格レンジの
上限(bull)を「上値余地」の主要根拠として使ってよいかを判定する。使えないとき、
利確判定はPARTIAL / FULLへ到達せず、WATCH(含み損・含み益なしではHOLD)で止まる。
本ファイルは、`False`になる**原因ごと**に、その分岐へ到達したことと、強い判定へ
到達しないことを固定する。

方針(1変数テスト):
  * 基準入力(`_evaluate`の既定値)は「使える」入力で、+60%の含み益のためFULL_PROFIT_TAKEへ到達する
    (`test_control_*`で固定)。基準が強い判定へ到達するからこそ、各原因のテストで
    「WATCHで止まった」ことが意味を持つ(基準が既にHOLDだと、原因が効いていなくても
    通ってしまう)。
  * 各テストは基準から**1つだけ**入力を変える。複数の原因が同時に効いて、どの条件が
    効いたか分からなくなることを避ける。
  * 各境界は、閾値ちょうど(使える)と閾値の外側(使えない)の対で固定する。
    閾値はconfigから読み、テスト内へ値を複製しない。

既存テストとの分担(重複させない): 業種のFINANCIAL / UNKNOWNは
test_profit_taking.py、スプレッド超過の理由コードとレンジ自体が使えない場合の
理由コードなしはtest_issue_221_profit_taking_watch_floor.pyが持つ。本ファイルは
それ以外の原因と、各境界を持つ。

★ tests-only。srcは変更しない。既存の挙動が期待と矛盾する場合は、テストを曲げず、
   srcも直さず、別Issueへ報告する。
★ 銘柄コード・保有数量・取得単価は架空値のみ。Productionへは一切アクセスしない。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    IndustryClassification,
    RecommendationType,
)
from jstock_advisor.domain.entities.valuation import (
    FairValueMethodResult,
    FairValueRange,
    ProfitTakingFairValueBlockReasonCode,
)
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    ProfitTakingResult,
    evaluate_profit_taking,
)

_CONFIG = load_config()
_PT = _CONFIG.profit_taking
_CBJ = _PT.condition_based_judgment

_AVERAGE_PRICE = Decimal("1000")
_GAIN_PRICE = Decimal("1600")  # +60%。適正価格レンジが使えれば FULL_PROFIT_TAKE へ到達する。

_STRONG_ACTIONS = (
    RecommendationType.PARTIAL_PROFIT_TAKE,
    RecommendationType.FULL_PROFIT_TAKE,
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


def _assert_ceiling_blocked(result: ProfitTakingResult) -> None:
    """上限価格が使えず、強い判定(PARTIAL / FULL)へ到達しないことを固定する。

    ceiling_priceとupside_pctがNoneであることは、「使えない」判定が価格系の判定
    (上値余地グリッド)へ伝わっていることの確認である。WATCHで止まるのは、含み益が
    あるのに価格系の根拠を失った状態(#221)であり、HOLDへ落ちて記録も通知も
    残らないことの防止も兼ねる。
    """
    assert result.fair_value_action_usable is False
    assert result.ceiling_price is None
    assert result.upside_pct is None
    assert result.final_action not in _STRONG_ACTIONS
    assert result.recommendation_type not in _STRONG_ACTIONS
    assert result.final_action == RecommendationType.WATCH
    assert result.recommendation_type == RecommendationType.WATCH


def _assert_ceiling_usable(result: ProfitTakingResult) -> None:
    assert result.fair_value_action_usable is True
    assert result.ceiling_price is not None
    assert result.upside_pct is not None


# --- 基準(対照)------------------------------------------------------------------


def test_control_base_input_uses_ceiling_and_reaches_full_profit_take() -> None:
    """基準入力は上限価格を使え、+60%の含み益でFULLへ到達する。

    これが崩れると、以降の「WATCHで止まる」テストは、原因が効いていなくても
    通ってしまう(基準がそもそも強い判定へ到達しないため)。基準の前提を固定する。
    """
    result = _evaluate()

    _assert_ceiling_usable(result)
    assert result.ceiling_price == Decimal("1620")
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.fair_value_action_block_reason_code is None


def test_control_config_thresholds_the_boundary_tests_rely_on() -> None:
    """境界テストが前提とするconfigの性質(値そのものは複製せず、性質だけを確認する)。"""
    assert _CBJ.min_fair_value_methods_for_partial >= 2  # 「1手法少ない」入力が作れる
    assert _CBJ.max_fair_value_spread_ratio_for_partial > 1.0
    # 「1営業日少ない」入力が作れる
    assert _CBJ.min_business_days_to_earnings_for_fair_value_action >= 1


# --- 上限価格が使えない原因(1変数。基準から1つだけ変える)---------------------------


def test_cause_fair_value_range_is_none() -> None:
    """レンジそのものが無い。上限価格の根拠が無いのに強い判定を出さない。"""
    _assert_ceiling_blocked(_evaluate(fair_value_range=None))


def test_cause_range_not_usable_for_trading_judgment() -> None:
    """レンジが売買判断に使えない品質(手法間の乖離等)。使えないレンジのbullを使わない。"""
    _assert_ceiling_blocked(_evaluate(fair_value_range=_range(usable=False)))


def test_cause_bull_is_none() -> None:
    """強気の適正価格(=上限価格)が算出できていない。Noneを上限として扱わない。"""
    _assert_ceiling_blocked(_evaluate(fair_value_range=_range(bull=None)))


def test_cause_industry_classification_not_specified_and_model_not_applied() -> None:
    """業種区分が未指定(None)で、業種別モデルも未適用。

    未指定は「一般事業会社と確認済み」ではない(安全側 = 使わない)。
    """
    _assert_ceiling_blocked(_evaluate(industry_classification=None))


def test_industry_model_applied_lifts_the_industry_gate() -> None:
    """業種別モデルが適用済みなら、業種区分が未指定でも業種ゲートは通る(対照)。"""
    _assert_ceiling_usable(_evaluate(industry_classification=None, industry_model_applied=True))


@pytest.mark.parametrize(
    "bear",
    [None, "0", "-1"],
    ids=["bear-none", "bear-zero", "bear-negative"],
)
def test_cause_bear_missing_or_not_positive(bear: str | None) -> None:
    """弱気の適正価格が無い・0以下。スプレッド(bull / bear)が計算できず、使えない。

    0以下で割ると比が無意味になる。計算できないスプレッドを「基準内」とみなさない。
    """
    _assert_ceiling_blocked(_evaluate(fair_value_range=_range(bear=bear)))


def test_cause_too_few_methods_and_boundary() -> None:
    """算出に使った手法が最小数未満。1手法だけの値を上限として強い判定へ使わない。"""
    minimum = _CBJ.min_fair_value_methods_for_partial

    _assert_ceiling_blocked(_evaluate(fair_value_range=_range(method_count=minimum - 1)))
    # 境界: 最小数ちょうどは使える。
    _assert_ceiling_usable(_evaluate(fair_value_range=_range(method_count=minimum)))


def test_cause_spread_ratio_above_limit_and_boundary() -> None:
    """手法間スプレッド(bull / bear)が上限を超える。バラつきが大きい上限は使わない。

    境界は「以下なら使える(<=)」。ちょうどの値は使え、わずかに超えると使えない。
    """
    limit = Decimal(str(_CBJ.max_fair_value_spread_ratio_for_partial))
    bear = Decimal("1000")
    at_limit = bear * limit
    above_limit = at_limit + Decimal("1")

    _assert_ceiling_usable(
        _evaluate(
            fair_value_range=_range(bear=str(bear), neutral="1150", bull=str(at_limit)),
        )
    )
    blocked = _evaluate(
        fair_value_range=_range(bear=str(bear), neutral="1150", bull=str(above_limit)),
    )
    _assert_ceiling_blocked(blocked)
    assert (
        blocked.fair_value_action_block_reason_code
        == ProfitTakingFairValueBlockReasonCode.METHOD_SPREAD_TOO_WIDE_FOR_ACTION.value
    )


@pytest.mark.parametrize("reflects", [False, None], ids=["not-reflected", "unknown"])
def test_cause_fair_value_does_not_reflect_latest_earnings(reflects: bool | None) -> None:
    """適正価格が最新の確定決算を反映していない(False)、または判定できない(None)。

    判定できない(None)を「反映済み」と扱わない(推測で補完しない)。古い決算に基づく
    上限を、強い判定の根拠に使わない。
    """
    _assert_ceiling_blocked(_evaluate(fair_value_reflects_latest_earnings=reflects))


def test_cause_earnings_too_close_and_boundary() -> None:
    """次回決算までの営業日数が下限未満。決算直前の適正価格は、発表で前提が変わりうる。

    境界は「下限ちょうどは使える」。営業日数が不明(None)の場合は、この条件では
    弾かない(不明を「直前」と扱わない。他の原因では別途弾かれる)。
    """
    minimum = _CBJ.min_business_days_to_earnings_for_fair_value_action

    _assert_ceiling_blocked(_evaluate(days_to_next_earnings_business_days=minimum - 1))
    _assert_ceiling_usable(_evaluate(days_to_next_earnings_business_days=minimum))
    _assert_ceiling_usable(_evaluate(days_to_next_earnings_business_days=None))


# --- 含み損益の境界(「利確」は含み益があって初めて成立する)---------------------------


@pytest.mark.parametrize(
    "price",
    ["900", "1000"],
    ids=["unrealized-loss", "break-even"],
)
def test_cause_no_unrealized_gain_is_always_unusable_and_hold(price: str) -> None:
    """含み損・損益ゼロでは、レンジが完全に使えても上限価格を使わず、HOLDにする。

    「利確」は含み益があって初めて成立する概念。含み損で適正価格超過だけを理由に
    指値候補を出さない(株価下落による売却判断はsell_signal側の担当)。
    """
    result = _evaluate(current_price=Decimal(price))

    assert result.fair_value_action_usable is False
    assert result.ceiling_price is None
    assert result.upside_pct is None
    assert result.final_action == RecommendationType.HOLD
    assert result.fair_value_action_block_reason_code is None


def test_smallest_unrealized_gain_makes_the_range_usable() -> None:
    """境界: 損益ゼロ(使えない)のすぐ上、+0.1%の含み益で上限価格を使える(対照)。

    含み益が小さいためFULL / PARTIALには到達しない(HOLD)が、上限価格の利用可否と
    強い判定への到達は別の判定であることも示す。
    """
    result = _evaluate(current_price=Decimal("1001"))

    _assert_ceiling_usable(result)
    assert result.final_action == RecommendationType.HOLD


# --- 理由コード(現状の観測。原因のうち構造化されているのはスプレッド超過のみ)-------


def test_block_reason_code_is_none_for_causes_without_a_structured_reason() -> None:
    """スプレッド超過以外の原因では、利用者・監査へ渡る理由コードが無い(現状の観測)。

    上限価格が使えない原因のうち、構造化された理由コードを持つのはスプレッド超過だけ
    である(`_fair_value_action_block_reason`のdocstringが既知の空白として明記している)。
    本テストはその現状を記録する。**理由コードを拡張する場合(#471)は、意図した変更
    として本テストを更新する**(本Issueでは理由コードを追加しない)。

    ★ 純粋なHOLD / WATCHで`usable=False`かつ`block_reason=None`が同時に成立することを
      固定する(=「使えなかった」ことは分かるが「なぜか」は伝わらない)。
    """
    causes = [
        _evaluate(fair_value_range=None),
        _evaluate(fair_value_range=_range(usable=False)),
        _evaluate(fair_value_range=_range(bull=None)),
        _evaluate(fair_value_range=_range(bear=None)),
        _evaluate(
            fair_value_range=_range(method_count=_CBJ.min_fair_value_methods_for_partial - 1)
        ),
        _evaluate(fair_value_reflects_latest_earnings=False),
        _evaluate(fair_value_reflects_latest_earnings=None),
        _evaluate(
            days_to_next_earnings_business_days=(
                _CBJ.min_business_days_to_earnings_for_fair_value_action - 1
            )
        ),
        _evaluate(industry_classification=None),
    ]

    for result in causes:
        assert result.fair_value_action_usable is False
        assert result.fair_value_action_block_reason_code is None
