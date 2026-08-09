"""domain/signals/historical_valuation.pyのテスト(判定精度向上機能Phase B、
コードレビュー対応で全面改修)。

銘柄自身の過去PER/PBR水準に対する現在値のランクベース評価(mid-rank
percentile)、basis(TRAILING/FORWARD)整合性チェック、データ品質フィルタ
(None/0以下/basis不一致/未来日/銘柄コード不一致/絶対レンジ外/外れ値/重複日付)、
coverage/confidence判定を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.models import (
    HistoricalValuationCategoryThresholds,
    HistoricalValuationRulesConfig,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    HistoricalValuationCategory,
    HistoricalValuationEvaluationState,
    ValuationBasis,
)
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.signals.historical_valuation import evaluate_historical_valuation
from jstock_advisor.interfaces.types import DataSourceReference, HistoricalValuation

_STOCK_CODE = "2914"
_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)


def _config(**overrides: object) -> HistoricalValuationRulesConfig:
    defaults: dict[str, object] = dict(
        model_version="historical_valuation_v2",
        min_data_points_required=2,
        per_weight=0.5,
        pbr_weight=0.5,
        outlier_detection_min_data_points=5,
        outlier_mad_threshold=3.5,
        per_absolute_min=0.0,
        per_absolute_max=500.0,
        pbr_absolute_min=0.0,
        pbr_absolute_max=50.0,
        full_confidence_data_points=4,
        coverage_high_threshold=0.8,
        coverage_medium_threshold=0.4,
        category_thresholds=HistoricalValuationCategoryThresholds(
            very_cheap=60.0, cheap=20.0, expensive=-20.0, very_expensive=-60.0
        ),
    )
    defaults.update(overrides)
    return HistoricalValuationRulesConfig.model_validate(defaults)


_CONFIG = _config()


def _hv(
    *,
    per: Decimal | None = None,
    pbr: Decimal | None = None,
    date: dt.date = dt.date(2023, 3, 31),
    per_basis: ValuationBasis = ValuationBasis.TRAILING,
    pbr_basis: ValuationBasis = ValuationBasis.TRAILING,
    stock_code: str = _STOCK_CODE,
) -> HistoricalValuation:
    return HistoricalValuation(
        stock_code=stock_code,
        date=date,
        per=per,
        pbr=pbr,
        per_basis=per_basis,
        pbr_basis=pbr_basis,
        source=_SOURCE,
    )


def _per_series(values: list[Decimal | None], start_year: int = 2020) -> list[HistoricalValuation]:
    return [
        _hv(per=v, date=dt.date(start_year + i, 3, 31))
        for i, v in enumerate(values)
    ]


def _evaluate(
    historical: list[HistoricalValuation],
    current_per: Decimal | None = None,
    current_pbr: Decimal | None = None,
    current_per_basis: ValuationBasis = ValuationBasis.TRAILING,
    current_pbr_basis: ValuationBasis = ValuationBasis.UNKNOWN,
    config: HistoricalValuationRulesConfig | None = None,
    evaluation_at: dt.datetime = _NOW,
) -> HistoricalValuationResult:
    return evaluate_historical_valuation(
        historical,
        _STOCK_CODE,
        current_per,
        current_per_basis,
        current_pbr,
        current_pbr_basis,
        evaluation_at,
        config or _CONFIG,
    )


# ===== percentile(1-6) =====


def test_percentile_cheapest_value_scores_positive_100() -> None:
    """過去の値と重複しない最安値は+100(過去レンジより安い場合と同じ扱い)。"""
    hist = _per_series([Decimal("20"), Decimal("30"), Decimal("40")])
    result = _evaluate(hist, current_per=Decimal("10"))
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    assert result.score == 100.0


def test_percentile_most_expensive_value_scores_negative_100() -> None:
    hist = _per_series([Decimal("10"), Decimal("20"), Decimal("30")])
    result = _evaluate(hist, current_per=Decimal("40"))
    assert result.score == -100.0


def test_percentile_median_scores_near_zero() -> None:
    hist = _per_series([Decimal("10"), Decimal("15"), Decimal("20")])
    result = _evaluate(hist, current_per=Decimal("15"))
    assert result.score == 0.0


def test_percentile_tie_uses_mid_rank() -> None:
    """タイがある場合、単純な0/100ではなくmid-rankで按分される。"""
    hist = _per_series([Decimal("10"), Decimal("10"), Decimal("20"), Decimal("30")])
    result = _evaluate(hist, current_per=Decimal("10"))
    # lower_count=0, equal_count=2, n=4 -> percentile=0.25 -> score=50
    assert result.score == 50.0


def test_percentile_below_historical_range_scores_positive_100() -> None:
    hist = _per_series([Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40")])
    result = _evaluate(hist, current_per=Decimal("1"))
    assert result.score == 100.0


def test_percentile_above_historical_range_scores_negative_100() -> None:
    hist = _per_series([Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40")])
    result = _evaluate(hist, current_per=Decimal("1000"))
    assert result.score == -100.0


# ===== basis整合性(7-11) =====


def test_basis_match_is_evaluated() -> None:
    hist = _per_series([Decimal("10"), Decimal("20")], start_year=2024)
    result = _evaluate(hist, current_per=Decimal("15"), current_per_basis=ValuationBasis.TRAILING)
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    assert result.per_score is not None


def test_basis_mismatch_excludes_component() -> None:
    """現在値がFORWARD basisの場合、TRAILING basisの過去データとは比較しない
    (推測でbasisを補完しない)。"""
    hist = _per_series([Decimal("10"), Decimal("20")], start_year=2024)
    result = _evaluate(hist, current_per=Decimal("15"), current_per_basis=ValuationBasis.FORWARD)
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED
    assert result.per_data_count_used == 0
    assert "BASIS_MISMATCH_EXCLUDED" in result.excluded_data_reasons


def test_basis_unknown_is_not_evaluated() -> None:
    hist = _per_series([Decimal("10"), Decimal("20")], start_year=2024)
    result = _evaluate(hist, current_per=Decimal("15"), current_per_basis=ValuationBasis.UNKNOWN)
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED


def test_per_unavailable_pbr_available_uses_pbr_only() -> None:
    hist = [
        _hv(pbr=Decimal("1.0"), date=dt.date(2024, 3, 31)),
        _hv(pbr=Decimal("2.0"), date=dt.date(2025, 3, 31)),
    ]
    result = _evaluate(
        hist,
        current_per=None,
        current_pbr=Decimal("1.0"),
        current_pbr_basis=ValuationBasis.TRAILING,
    )
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    assert result.per_score is None
    assert result.pbr_score is not None


def test_pbr_unavailable_per_available_uses_per_only() -> None:
    hist = _per_series([Decimal("10"), Decimal("20")], start_year=2024)
    result = _evaluate(hist, current_per=Decimal("15"))
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    assert result.pbr_score is None
    assert result.per_score is not None


# ===== データ品質(12-20) =====


def test_none_values_are_excluded() -> None:
    hist = [
        _hv(per=None, date=dt.date(2022, 3, 31)),
        _hv(per=Decimal("10"), date=dt.date(2023, 3, 31)),
        _hv(per=Decimal("20"), date=dt.date(2024, 3, 31)),
    ]
    result = _evaluate(hist, current_per=Decimal("15"))
    assert result.per_data_count_raw == 2  # Noneはraw集計にも含めない(値が存在しない)
    assert result.per_data_count_used == 2


def test_zero_or_negative_values_are_excluded() -> None:
    hist = [
        _hv(per=Decimal("-5"), date=dt.date(2022, 3, 31)),
        _hv(per=Decimal("0"), date=dt.date(2023, 3, 31)),
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31)),
    ]
    result = _evaluate(hist, current_per=Decimal("15"))
    assert result.per_data_count_used == 2
    assert "NONE_OR_NON_POSITIVE_EXCLUDED" in result.excluded_data_reasons


def test_future_date_data_is_excluded() -> None:
    """look-ahead bias防止: evaluation_atより後の日付を持つ過去データは除外する。"""
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31)),
        _hv(per=Decimal("30"), date=dt.date(2099, 3, 31)),  # 未来日
    ]
    result = _evaluate(hist, current_per=Decimal("15"), evaluation_at=_NOW)
    assert result.per_data_count_used == 2
    assert "FUTURE_DATE_EXCLUDED" in result.excluded_data_reasons


def test_stock_code_mismatch_is_excluded() -> None:
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31)),
        _hv(per=Decimal("30"), date=dt.date(2026, 3, 31), stock_code="9999"),
    ]
    result = _evaluate(hist, current_per=Decimal("15"))
    assert result.per_data_count_used == 2
    assert "STOCK_CODE_MISMATCH_EXCLUDED" in result.excluded_data_reasons


def test_absolute_range_exclusion() -> None:
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31)),
        _hv(per=Decimal("9999"), date=dt.date(2026, 3, 31)),  # 明らかな異常値
    ]
    result = _evaluate(hist, current_per=Decimal("15"), config=_config(per_absolute_max=500.0))
    assert result.per_data_count_used == 2
    assert "ABSOLUTE_RANGE_EXCLUDED" in result.excluded_data_reasons


def test_outlier_exclusion_via_mad() -> None:
    hist = [
        _hv(per=Decimal(str(v)), date=dt.date(2020 + i, 3, 31))
        for i, v in enumerate([10, 11, 9, 10, 200])  # 200が明確な外れ値
    ]
    result = _evaluate(
        hist, current_per=Decimal("10"), config=_config(outlier_detection_min_data_points=5)
    )
    assert "OUTLIER_EXCLUDED" in result.excluded_data_reasons
    assert result.per_data_count_used == 4


def test_insufficient_data_points_excludes_component() -> None:
    """有効データがmin_data_points_required未満の場合はNOT_EVALUATED。"""
    hist = [_hv(per=Decimal("10"), date=dt.date(2024, 3, 31))]
    result = _evaluate(hist, current_per=Decimal("15"), config=_config(min_data_points_required=2))
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED
    assert result.per_data_count_used == 1


def test_exclusion_can_drop_below_minimum_after_filtering() -> None:
    """絶対レンジ・basis等の除外の結果、元は十分な件数でも閾値未満に落ちる場合、
    その指標はNOT_EVALUATEDになる(黙って少ないデータで評価しない)。"""
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31), per_basis=ValuationBasis.FORWARD),
    ]
    result = _evaluate(hist, current_per=Decimal("15"), config=_config(min_data_points_required=2))
    assert result.per_data_count_used == 1  # 1件はbasis不一致で除外
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED


def test_duplicate_date_rows_are_excluded() -> None:
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("15"), date=dt.date(2024, 3, 31)),  # 同一日付の重複
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31)),
    ]
    result = _evaluate(hist, current_per=Decimal("15"))
    assert result.per_data_count_used == 1  # 重複日付2件は両方除外され、残るのは1件
    assert "DUPLICATE_DATE_EXCLUDED" in result.excluded_data_reasons


# ===== coverage/confidence(21-24) =====


def test_high_coverage_and_confidence_when_both_components_sufficient() -> None:
    hist = [
        _hv(per=Decimal(str(v)), pbr=Decimal(str(v / 10)), date=dt.date(2020 + i, 3, 31))
        for i, v in enumerate([10, 20, 30, 40])
    ]
    result = _evaluate(
        hist,
        current_per=Decimal("15"),
        current_pbr=Decimal("1.5"),
        current_pbr_basis=ValuationBasis.TRAILING,
    )
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    assert result.coverage == 1.0
    assert result.confidence == ConfidenceLevel.HIGH


def test_coverage_lower_when_only_one_component_available() -> None:
    hist = [
        _hv(per=Decimal(str(v)), date=dt.date(2020 + i, 3, 31))
        for i, v in enumerate([10, 20, 30, 40])
    ]
    result = _evaluate(hist, current_per=Decimal("15"))
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    assert 0.0 < result.coverage < 1.0


def test_minimum_data_points_yields_low_or_medium_confidence() -> None:
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31)),
    ]
    result = _evaluate(hist, current_per=Decimal("15"), config=_config(min_data_points_required=2))
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    assert result.confidence in (ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM)


def test_no_evaluable_data_returns_not_evaluated_with_none_score() -> None:
    result = _evaluate([], current_per=None, current_pbr=None)
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED
    assert result.score is None
    assert result.confidence is None


# ===== weighted combination・カテゴリ・model_version =====


def test_both_components_combine_with_configured_weights() -> None:
    hist = [
        _hv(per=Decimal(str(p)), pbr=Decimal(str(b)), date=dt.date(2020 + i, 3, 31))
        for i, (p, b) in enumerate([(10, 1.0), (20, 2.0), (30, 3.0), (40, 4.0)])
    ]
    # PER=10は過去4件中の最小値自身(tie) -> lower=0,equal=1,n=4 -> percentile=0.125 -> +75
    # PBR=4.0は過去4件中の最大値自身(tie) -> lower=3,equal=1,n=4 -> percentile=0.875 -> -75
    result = _evaluate(
        hist,
        current_per=Decimal("10"),
        current_pbr=Decimal("4.0"),
        current_pbr_basis=ValuationBasis.TRAILING,
        config=_config(per_weight=0.5, pbr_weight=0.5),
    )
    assert result.score is not None
    assert round(result.score, 6) == round(75.0 * 0.5 + (-75.0) * 0.5, 6)


def test_category_classification_matches_thresholds() -> None:
    hist = _per_series([Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40")])
    result = _evaluate(hist, current_per=Decimal("1"))  # score=100
    assert result.category == HistoricalValuationCategory.HISTORICALLY_VERY_CHEAP


def test_model_version_matches_config() -> None:
    hist = _per_series([Decimal("10"), Decimal("20")], start_year=2024)
    result = _evaluate(hist, current_per=Decimal("15"), config=_config(model_version="test_v99"))
    assert result.model_version == "test_v99"
