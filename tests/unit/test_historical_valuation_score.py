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

import pytest
from pydantic import ValidationError

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
        model_version="historical_valuation_v4",
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
    available_at: dt.datetime = _NOW,
    pbr_is_approximate: bool = False,
) -> HistoricalValuation:
    return HistoricalValuation(
        stock_code=stock_code,
        date=date,
        per=per,
        pbr=pbr,
        per_basis=per_basis,
        pbr_basis=pbr_basis,
        available_at=available_at,
        pbr_is_approximate=pbr_is_approximate,
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
    # コードレビュー対応(第2回、現在値の絶対レンジチェック追加)で、レンジ外の
    # current_per(旧テストの1000はper_absolute_max=500超過)は評価除外される
    # ようになったため、レンジ内かつ過去レンジより高い値(450)を使う。
    hist = _per_series([Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40")])
    result = _evaluate(hist, current_per=Decimal("450"))
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


# ===== available_at(look-ahead bias防止、コードレビュー第2回対応、25-29) =====


def test_available_at_after_evaluation_at_excludes_data() -> None:
    """period_endが評価日より前でも、available_at(データが実際に利用可能に
    なった日時)がevaluation_atより後なら使用しない(look-ahead bias防止)。"""
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31), available_at=_NOW),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31), available_at=_NOW),
        _hv(
            per=Decimal("30"),
            date=dt.date(2020, 3, 31),  # period_endは評価日よりずっと前
            available_at=_NOW + dt.timedelta(days=1),  # だがavailable_atは評価時点より後
        ),
    ]
    result = _evaluate(hist, current_per=Decimal("15"), evaluation_at=_NOW)
    assert result.per_data_count_used == 2
    assert "DATA_NOT_AVAILABLE_AT_EVALUATION_TIME_EXCLUDED" in result.excluded_data_reasons


def test_available_at_equal_to_evaluation_at_is_usable() -> None:
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31), available_at=_NOW),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31), available_at=_NOW),
    ]
    result = _evaluate(hist, current_per=Decimal("15"), evaluation_at=_NOW)
    assert result.per_data_count_used == 2
    assert result.state == HistoricalValuationEvaluationState.EVALUATED


def test_available_at_before_evaluation_at_is_usable() -> None:
    hist = [
        _hv(
            per=Decimal("10"),
            date=dt.date(2024, 3, 31),
            available_at=_NOW - dt.timedelta(days=10),
        ),
        _hv(
            per=Decimal("20"),
            date=dt.date(2025, 3, 31),
            available_at=_NOW - dt.timedelta(days=5),
        ),
    ]
    result = _evaluate(hist, current_per=Decimal("15"), evaluation_at=_NOW)
    assert result.per_data_count_used == 2


def test_available_at_filter_can_drop_below_minimum() -> None:
    """available_atフィルタの結果、有効データがmin_data_points_required未満に
    落ちる場合はNOT_EVALUATED(「未来情報で高精度に見える結果」より
    「評価できない」ことを優先する)。"""
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31), available_at=_NOW),
        _hv(
            per=Decimal("20"),
            date=dt.date(2025, 3, 31),
            available_at=_NOW + dt.timedelta(days=1),
        ),
    ]
    result = _evaluate(
        hist,
        current_per=Decimal("15"),
        evaluation_at=_NOW,
        config=_config(min_data_points_required=2),
    )
    assert result.per_data_count_used == 1
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED


def test_available_at_exclusion_reason_code_recorded_precisely() -> None:
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31), available_at=_NOW),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31), available_at=_NOW),
        _hv(
            per=Decimal("30"),
            date=dt.date(2020, 3, 31),
            available_at=_NOW + dt.timedelta(hours=1),
        ),
    ]
    result = _evaluate(hist, current_per=Decimal("15"), evaluation_at=_NOW)
    assert result.excluded_data_reasons == ("DATA_NOT_AVAILABLE_AT_EVALUATION_TIME_EXCLUDED",)


# ===== 現在値PER/PBRの絶対レンジチェック(コードレビュー第2回対応、30-34) =====


def test_current_per_above_absolute_max_excludes_component() -> None:
    hist = _per_series([Decimal("10"), Decimal("20")], start_year=2024)
    result = _evaluate(hist, current_per=Decimal("600"), config=_config(per_absolute_max=500.0))
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED
    assert "CURRENT_PER_OUT_OF_RANGE" in result.reason_codes


def test_current_per_below_absolute_min_excludes_component() -> None:
    hist = _per_series([Decimal("10"), Decimal("20")], start_year=2024)
    result = _evaluate(hist, current_per=Decimal("-5"), config=_config(per_absolute_min=0.0))
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED
    assert "CURRENT_PER_OUT_OF_RANGE" in result.reason_codes


def test_current_pbr_above_absolute_max_excludes_component() -> None:
    hist = [
        _hv(pbr=Decimal("1.0"), date=dt.date(2024, 3, 31)),
        _hv(pbr=Decimal("2.0"), date=dt.date(2025, 3, 31)),
    ]
    result = _evaluate(
        hist,
        current_per=None,
        current_pbr=Decimal("100"),
        current_pbr_basis=ValuationBasis.TRAILING,
        config=_config(pbr_absolute_max=50.0),
    )
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED
    assert "CURRENT_PBR_OUT_OF_RANGE" in result.reason_codes


def test_current_per_out_of_range_falls_back_to_pbr_only() -> None:
    """既存の片側フォールバック設計は維持: PERのみレンジ外でもPBRが正常なら
    PBR単独での評価を継続する。"""
    hist = [
        _hv(per=Decimal("10"), pbr=Decimal("1.0"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("20"), pbr=Decimal("2.0"), date=dt.date(2025, 3, 31)),
    ]
    result = _evaluate(
        hist,
        current_per=Decimal("600"),
        current_pbr=Decimal("1.5"),
        current_pbr_basis=ValuationBasis.TRAILING,
        config=_config(per_absolute_max=500.0),
    )
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    assert result.per_score is None
    assert result.pbr_score is not None
    assert "CURRENT_PER_OUT_OF_RANGE" in result.reason_codes


def test_current_per_and_pbr_both_out_of_range_returns_not_evaluated() -> None:
    hist = [
        _hv(per=Decimal("10"), pbr=Decimal("1.0"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("20"), pbr=Decimal("2.0"), date=dt.date(2025, 3, 31)),
    ]
    result = _evaluate(
        hist,
        current_per=Decimal("600"),
        current_pbr=Decimal("100"),
        current_pbr_basis=ValuationBasis.TRAILING,
        config=_config(per_absolute_max=500.0, pbr_absolute_max=50.0),
    )
    assert result.state == HistoricalValuationEvaluationState.NOT_EVALUATED
    assert "CURRENT_PER_OUT_OF_RANGE" in result.reason_codes
    assert "CURRENT_PBR_OUT_OF_RANGE" in result.reason_codes


# ===== PBR近似(株式数近似)とconfidenceの整合(コードレビュー第2回対応、35-37) =====


def test_approximate_pbr_only_never_yields_high_confidence() -> None:
    """近似PBR(株式数近似)がスコアに使われた場合、coverageがHIGH閾値を満たし
    excluded_data_reasonsが無くても、confidenceはHIGHへ到達しない(MEDIUMへ
    強制的に引き下げられる)。"""
    hist = [
        _hv(pbr=Decimal(str(v)), date=dt.date(2020 + i, 3, 31), pbr_is_approximate=True)
        for i, v in enumerate(["1.0", "2.0", "3.0", "4.0"])
    ]
    result = _evaluate(
        hist,
        current_per=None,
        current_pbr=Decimal("2.5"),
        current_pbr_basis=ValuationBasis.TRAILING,
        config=_config(full_confidence_data_points=4, coverage_high_threshold=0.5),
    )
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    # coverage=0.5はcoverage_high_threshold=0.5を満たすため、近似PBRの制約が
    # 無ければHIGHになるはずの条件で、実際にはMEDIUMへ制約されることを確認する。
    assert result.coverage >= 0.5
    assert result.confidence == ConfidenceLevel.MEDIUM


def test_approximate_pbr_flag_recorded_in_result_and_reason_codes() -> None:
    hist = [
        _hv(pbr=Decimal(str(v)), date=dt.date(2020 + i, 3, 31), pbr_is_approximate=True)
        for i, v in enumerate(["1.0", "2.0"])
    ]
    result = _evaluate(
        hist,
        current_per=None,
        current_pbr=Decimal("1.5"),
        current_pbr_basis=ValuationBasis.TRAILING,
    )
    assert result.pbr_is_approximate is True
    assert "HISTORICAL_PBR_SHARE_COUNT_APPROXIMATED" in result.reason_codes


def test_available_at_aware_utc_is_accepted() -> None:
    _hv(per=Decimal("10"), available_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))


def test_available_at_aware_jst_is_accepted() -> None:
    jst = dt.timezone(dt.timedelta(hours=9))
    _hv(per=Decimal("10"), available_at=dt.datetime(2026, 1, 1, tzinfo=jst))


def test_available_at_naive_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _hv(per=Decimal("10"), available_at=dt.datetime(2026, 1, 1))  # noqa: DTZ001


def test_approximate_pbr_flag_has_no_effect_when_pbr_not_evaluated() -> None:
    """PBRが評価に使われない(current_pbr未指定)場合、過去データに近似PBR行が
    存在してもpbr_is_approximate/理由コードへ影響しない。"""
    hist = [
        _hv(per=Decimal("10"), date=dt.date(2024, 3, 31)),
        _hv(per=Decimal("20"), date=dt.date(2025, 3, 31)),
        _hv(pbr=Decimal("1.0"), date=dt.date(2024, 3, 31), pbr_is_approximate=True),
    ]
    result = _evaluate(hist, current_per=Decimal("15"), current_pbr=None)
    assert result.state == HistoricalValuationEvaluationState.EVALUATED
    assert result.pbr_is_approximate is False
    assert "HISTORICAL_PBR_SHARE_COUNT_APPROXIMATED" not in result.reason_codes
