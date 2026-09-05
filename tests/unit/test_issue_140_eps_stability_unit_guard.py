"""Issue #140: EPS 安定性が単位不整合により変動係数を一度も使わない不具合の回帰テスト。

企業品質スコアの安定性採点は、系列の絶対規模が小さすぎるときに変動係数ではなく
黒字期数割合へ fallback する。その下限 `min_mean_for_cv_yen`(1千万円)は
**円建ての金額系列(営業利益)のために設計された値**である。

1株当たり利益(EPS)は数百円オーダーのため、同じ円建ての下限を適用すると
必ず条件を満たしてしまい、**設計上の本命である変動係数へ一度も到達しない**。

修正方針(C1)は次のとおり。

    EPS 系列      円建ての絶対額ガードを適用しない
    営業利益系列   従来どおり円建てガードを維持する

EPS でガードを外してもゼロ除算や変動係数の発散は起きない。非正値を含む系列は
先に黒字期数割合へ分岐するため、変動係数の計算へ到達する時点で全値が正であり
平均が正であることが保証されるためである。

本テストは helper 単体ではなく、`score_company_quality()` の実経路を通して
`profitability_eps_stability` / `stability_operating_income` の
`reason` を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.classification.financial_industry import IndustryClassificationResult
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    EvidenceCoverageStatus,
    IndustryClassification,
    PeriodType,
)
from jstock_advisor.domain.financial_series import FinancialPeriodValue
from jstock_advisor.domain.signals.company_quality_scoring import (
    CompanyQualityInputs,
    score_company_quality,
)
from jstock_advisor.interfaces.types import FinancialSummary

_CFG = load_config()
_WEIGHTS = _CFG.holding_decision.company_quality_weights
_THRESHOLDS = _CFG.holding_decision.company_quality_score_thresholds
_RATIO_RULES = _CFG.holding_decision_ratio
_GENERAL = IndustryClassificationResult(IndustryClassification.GENERAL_CORPORATE)
_NOW = dt.datetime(2026, 4, 1, tzinfo=dt.UTC)

_EPS_ITEM = "profitability_eps_stability"
_INCOME_ITEM = "stability_operating_income"

_CV = "COEFFICIENT_OF_VARIATION"
_PROFIT_RATIO = "PROFIT_QUARTER_RATIO"


def _financial(**overrides) -> FinancialSummary:
    base = dict(
        stock_code="9999",
        source=DataSourceReference(provider="test", fetched_at=_NOW),
        fiscal_period_end=dt.date(2026, 3, 31),
        equity_ratio_pct=45.0,
        operating_cashflow=Decimal("1000"),
        operating_income=Decimal("900"),
        forecast_eps=Decimal("100"),
        forecast_bps=Decimal("1000"),
        is_debt_excess=False,
        is_deficit=False,
        is_going_concern_doubt=False,
    )
    base.update(overrides)
    return FinancialSummary(**base)


def _series(values: list[str]) -> list[FinancialPeriodValue]:
    """新しい順に並べても結果が変わらないよう、期末日を昇順で振る。"""
    return [
        FinancialPeriodValue(
            value=Decimal(v),
            period_end=dt.date(2023 + i, 3, 31),
            period_type=PeriodType.ANNUAL,
        )
        for i, v in enumerate(values)
    ]


def _inputs(**overrides) -> CompanyQualityInputs:
    base = dict(
        financial=_financial(),
        quarterly_operating_income_periods=[],
        quarterly_operating_cashflow_periods=[],
        eps_period_values=[],
        cashflow_decomposition=None,
        industry_classification=_GENERAL,
    )
    base.update(overrides)
    return CompanyQualityInputs(**base)


def _item(result, code: str):
    return next(i for i in result.items if i.item_code == code)


def _score(**overrides):
    return score_company_quality(_inputs(**overrides), _WEIGHTS, _THRESHOLDS, _RATIO_RULES)


def _min_periods() -> int:
    return _RATIO_RULES.min_periods_for_stability_score


# --- A / B: EPS は円建てガードに関係なく変動係数を使う ------------------------


def test_a_positive_eps_with_sufficient_periods_uses_coefficient_of_variation() -> None:
    result = _score(eps_period_values=_series(["120", "130", "125", "128"]))
    item = _item(result, _EPS_ITEM)

    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.reason == _CV


def test_b_eps_mean_far_below_yen_guard_still_uses_coefficient_of_variation() -> None:
    """本 Issue の root cause。EPS の平均は円建て下限を必ず下回る。"""
    values = ["120", "130", "125", "128"]
    mean = sum(float(v) for v in values) / len(values)

    assert mean < _RATIO_RULES.min_mean_for_cv_yen, "前提: EPS は円建て下限を大きく下回る"

    item = _item(_score(eps_period_values=_series(values)), _EPS_ITEM)

    assert item.reason == _CV, "円建て下限を EPS へ適用してはならない"


def test_b_even_single_digit_eps_uses_coefficient_of_variation() -> None:
    """極端に小さい EPS でも、全値が正なら変動係数を使う。"""
    item = _item(_score(eps_period_values=_series(["3", "4", "3.5", "3.8"])), _EPS_ITEM)

    assert item.reason == _CV


# --- C / D: EPS の既存 semantics は維持 ---------------------------------------


def test_c_eps_with_negative_value_falls_back_to_profit_quarter_ratio() -> None:
    item = _item(_score(eps_period_values=_series(["120", "-30", "125", "128"])), _EPS_ITEM)

    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.reason == _PROFIT_RATIO


def test_c_eps_with_zero_value_falls_back_to_profit_quarter_ratio() -> None:
    """ゼロは非正値として扱う(ゼロ除算を避けるための既存契約)。"""
    item = _item(_score(eps_period_values=_series(["120", "0", "125", "128"])), _EPS_ITEM)

    assert item.reason == _PROFIT_RATIO


def test_d_eps_with_insufficient_periods_is_not_evaluated() -> None:
    short = _series(["120"] * (_min_periods() - 1))
    item = _item(_score(eps_period_values=short), _EPS_ITEM)

    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED
    assert item.reason == "INSUFFICIENT_PERIODS"


def test_d_eps_absent_series_is_not_evaluated() -> None:
    item = _item(_score(eps_period_values=[]), _EPS_ITEM)

    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED


# --- E / F: 営業利益側の挙動は変えない ----------------------------------------


def test_e_operating_income_below_yen_guard_keeps_profit_quarter_ratio() -> None:
    """営業利益は円建て系列なので、下限を下回れば従来どおり fallback する。"""
    small = str(int(_RATIO_RULES.min_mean_for_cv_yen // 10))
    values = [small] * 4
    mean = float(small)

    assert mean <= _RATIO_RULES.min_mean_for_cv_yen, "前提: 円建て下限を下回る"

    item = _item(_score(quarterly_operating_income_periods=_series(values)), _INCOME_ITEM)

    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.reason == _PROFIT_RATIO, "営業利益のガードを外してはならない"


def test_f_operating_income_above_yen_guard_uses_coefficient_of_variation() -> None:
    large = int(_RATIO_RULES.min_mean_for_cv_yen * 10)
    values = [str(large), str(int(large * 1.05)), str(int(large * 0.97)), str(int(large * 1.02))]

    item = _item(_score(quarterly_operating_income_periods=_series(values)), _INCOME_ITEM)

    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.reason == _CV


def test_e_operating_income_with_negative_value_falls_back() -> None:
    large = int(_RATIO_RULES.min_mean_for_cv_yen * 10)
    values = [str(large), str(-large), str(large), str(large)]

    item = _item(_score(quarterly_operating_income_periods=_series(values)), _INCOME_ITEM)

    assert item.reason == _PROFIT_RATIO


# --- G: EPS のスコアが絶対額だけで頭打ちにならない ----------------------------


def test_g_eps_score_reflects_volatility_instead_of_saturating() -> None:
    """安定した EPS と不安定な EPS でスコアが変わること。

    修正前は両者とも黒字期数割合(全期黒字なら同一)になり、変動の差が
    スコアへ反映されなかった。
    """
    stable = _item(_score(eps_period_values=_series(["100", "101", "99", "100"])), _EPS_ITEM)
    volatile = _item(_score(eps_period_values=_series(["100", "300", "20", "180"])), _EPS_ITEM)

    assert stable.reason == _CV
    assert volatile.reason == _CV
    assert stable.points_earned > volatile.points_earned, "変動の大きさがスコアへ反映されること"


def test_g_stable_eps_is_not_penalised_by_absolute_magnitude() -> None:
    """金額の大小ではなく変動の小ささで評価されること。"""
    small = _item(_score(eps_period_values=_series(["10", "10.1", "9.9", "10"])), _EPS_ITEM)
    large = _item(_score(eps_period_values=_series(["1000", "1010", "990", "1000"])), _EPS_ITEM)

    assert small.reason == _CV
    assert large.reason == _CV
    assert small.points_earned == large.points_earned


# --- H: 新しい設定パラメータを増やしていないこと ------------------------------


def test_h_no_new_eps_threshold_parameter_is_introduced() -> None:
    """EPS 用の下限パラメータを新設しない(C1 の前提)。"""
    field_names = set(type(_RATIO_RULES).model_fields)

    assert "min_mean_for_cv_eps" not in field_names
    assert "eps_threshold" not in field_names
    assert not any("eps" in name and "min_mean" in name for name in field_names)


def test_h_yen_guard_parameter_is_unchanged() -> None:
    """既存の円建て下限そのものは変更していない。"""
    assert _RATIO_RULES.min_mean_for_cv_yen == 10000000


# --- 両系列を同時に含む実経路の回帰 -------------------------------------------


def test_eps_and_operating_income_use_their_own_unit_policy_in_one_call() -> None:
    """同一呼び出しで、EPS は変動係数・営業利益は円建てガードが効くこと。"""
    small_income = str(int(_RATIO_RULES.min_mean_for_cv_yen // 10))

    result = _score(
        eps_period_values=_series(["120", "130", "125", "128"]),
        quarterly_operating_income_periods=_series([small_income] * 4),
    )

    assert _item(result, _EPS_ITEM).reason == _CV
    assert _item(result, _INCOME_ITEM).reason == _PROFIT_RATIO
