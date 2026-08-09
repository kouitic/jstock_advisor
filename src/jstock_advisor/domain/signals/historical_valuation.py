"""判定精度向上機能Phase B: Historical Valuation Score(自己過去比較スコア)。

銘柄自身の過去PER/PBR水準に対して、現在の値がどの位置にあるかを
-100(過去最高値=最も割高)〜+100(過去最安値=最も割安)のランクベース
スコアで表す。同業他社・市場平均とは比較しない(自己過去比較のみ、
docs/functional_spec.md §15の既存制約を踏襲)。

yfinance等から取得できる過去バリュエーションデータは実質年次数点程度と
少ないため、平均・標準偏差ベースの手法(外れ値・少数データに弱い)ではなく、
mid-rank percentile(タイをそのまま50%点として扱うランクベース手法)を採用
する。algorithm自体は外部I/Oを一切行わない純関数(domain/signals/momentum.py
と同じパターン)。

コードレビュー対応(basis整合性): 現在値と過去値のPER/PBRは、同一の
ValuationBasis(TRAILING/FORWARD)である場合のみ比較する。basisが不一致・
不明な組み合わせは推測で補完せず、その指標をスコア対象から除外する。

コードレビュー対応(look-ahead bias防止): evaluation_atより後の日付を持つ
過去データは評価対象から除外する(将来のバックテストで過去時点評価を行う
場合にも同じ関数がそのまま使えるようにするため)。

コードレビュー対応(Shadow計測): この評価結果はDecisionSnapshot(判定精度
向上機能Phase Aの自己評価基盤)へ記録する専用のものであり、BUY候補判定・
保有判断スコア・旧売却判定・ProfitTaking判定・LINE通知など既存の判定
ロジックからは一切参照されない。
"""

from __future__ import annotations

import datetime as dt
import statistics
from decimal import Decimal
from typing import Any

from jstock_advisor.config.models import HistoricalValuationRulesConfig
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    HistoricalValuationCategory,
    HistoricalValuationEvaluationState,
    ValuationBasis,
)
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware
from jstock_advisor.interfaces.types import HistoricalValuation

# データ品質フィルタの除外理由コード(CloudWatch等で検索可能な固定文字列)。
REASON_NONE_OR_NON_POSITIVE_EXCLUDED = "NONE_OR_NON_POSITIVE_EXCLUDED"
REASON_BASIS_MISMATCH_EXCLUDED = "BASIS_MISMATCH_EXCLUDED"
REASON_FUTURE_DATE_EXCLUDED = "FUTURE_DATE_EXCLUDED"
REASON_STOCK_CODE_MISMATCH_EXCLUDED = "STOCK_CODE_MISMATCH_EXCLUDED"
REASON_ABSOLUTE_RANGE_EXCLUDED = "ABSOLUTE_RANGE_EXCLUDED"
REASON_DUPLICATE_DATE_EXCLUDED = "DUPLICATE_DATE_EXCLUDED"
REASON_OUTLIER_EXCLUDED = "OUTLIER_EXCLUDED"


class _FilteredSeries:
    __slots__ = ("values", "data_count_raw", "data_count_used", "excluded_reasons")

    def __init__(
        self,
        values: list[Decimal],
        data_count_raw: int,
        data_count_used: int,
        excluded_reasons: set[str],
    ) -> None:
        self.values = values
        self.data_count_raw = data_count_raw
        self.data_count_used = data_count_used
        self.excluded_reasons = excluded_reasons


def _filter_series(
    historical_valuations: list[HistoricalValuation],
    stock_code: str,
    evaluation_date: dt.date,
    current_basis: ValuationBasis,
    value_field: str,
    basis_field: str,
    absolute_min: float,
    absolute_max: float,
    config: HistoricalValuationRulesConfig,
) -> _FilteredSeries:
    """1指標(PERまたはPBR)分の過去データ品質フィルタを適用する。

    除外は複数条件が重複しうるため、1行につき最初に該当した理由のみを
    記録する(reason_codes全体としては複数理由が集まりうる)。
    """
    raw_count = 0
    excluded_reasons: set[str] = set()
    candidates: list[tuple[dt.date, Decimal]] = []

    for row in historical_valuations:
        value = getattr(row, value_field)
        basis = getattr(row, basis_field)
        if value is None:
            continue
        raw_count += 1
        if row.stock_code != stock_code:
            excluded_reasons.add(REASON_STOCK_CODE_MISMATCH_EXCLUDED)
            continue
        if row.date > evaluation_date:
            excluded_reasons.add(REASON_FUTURE_DATE_EXCLUDED)
            continue
        if value <= 0:
            excluded_reasons.add(REASON_NONE_OR_NON_POSITIVE_EXCLUDED)
            continue
        if current_basis == ValuationBasis.UNKNOWN or basis != current_basis:
            excluded_reasons.add(REASON_BASIS_MISMATCH_EXCLUDED)
            continue
        if not (absolute_min < float(value) < absolute_max):
            excluded_reasons.add(REASON_ABSOLUTE_RANGE_EXCLUDED)
            continue
        candidates.append((row.date, value))

    # 重複日付は正が判定できないため、該当日付の行を全除外する。
    date_counts: dict[dt.date, int] = {}
    for row_date, _ in candidates:
        date_counts[row_date] = date_counts.get(row_date, 0) + 1
    deduped = [(d, v) for d, v in candidates if date_counts[d] == 1]
    if len(deduped) < len(candidates):
        excluded_reasons.add(REASON_DUPLICATE_DATE_EXCLUDED)

    values = [v for _, v in deduped]

    if len(values) >= config.outlier_detection_min_data_points:
        values, had_outliers = _exclude_outliers_mad(values, config.outlier_mad_threshold)
        if had_outliers:
            excluded_reasons.add(REASON_OUTLIER_EXCLUDED)

    return _FilteredSeries(
        values=values,
        data_count_raw=raw_count,
        data_count_used=len(values),
        excluded_reasons=excluded_reasons,
    )


def _exclude_outliers_mad(
    values: list[Decimal], mad_threshold: float
) -> tuple[list[Decimal], bool]:
    """MAD(median absolute deviation)ベースの外れ値除外。

    少数データでも平均・標準偏差より頑健とされる手法。MAD=0(全値が同一等)の
    場合は判定不能のため除外を行わない。
    """
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations)
    if mad == 0:
        return values, False
    kept: list[Decimal] = []
    excluded_any = False
    for v in values:
        modified_z = Decimal("0.6745") * abs(v - median) / mad
        if modified_z > Decimal(str(mad_threshold)):
            excluded_any = True
        else:
            kept.append(v)
    if not kept:
        # 全件が外れ値判定されてしまう場合は判定を信用せず、除外しない
        # (少数データでの誤判定リスクを避ける)。
        return values, False
    return kept, excluded_any


def _percentile_rank_score(current: Decimal, historical: list[Decimal]) -> tuple[float, float]:
    """mid-rank percentileベースのスコアを算出する。戻り値は(score, percentile)。

    historicalのうちcurrent未満の件数をlower_count、current と等しい件数を
    equal_countとし、percentile = (lower_count + 0.5*equal_count) / n とする。
    percentileが高い(=過去の多くの値が現在値以上)ほど、現在値は過去と比べて
    割安と判断しscoreは+100に近づく。逆にpercentileが低いほど割高と判断し
    scoreは-100に近づく。tieが存在する場合もmid-rankにより安定した値になる。
    """
    n = len(historical)
    lower_count = sum(1 for h in historical if h < current)
    equal_count = sum(1 for h in historical if h == current)
    percentile = (lower_count + 0.5 * equal_count) / n
    score = 100.0 - 200.0 * percentile
    score = max(-100.0, min(100.0, score))
    return score, percentile


def _classify_category(
    score: float, thresholds: HistoricalValuationRulesConfig
) -> HistoricalValuationCategory:
    t = thresholds.category_thresholds
    if score >= t.very_cheap:
        return HistoricalValuationCategory.HISTORICALLY_VERY_CHEAP
    if score >= t.cheap:
        return HistoricalValuationCategory.CHEAP
    if score <= t.very_expensive:
        return HistoricalValuationCategory.VERY_EXPENSIVE
    if score <= t.expensive:
        return HistoricalValuationCategory.EXPENSIVE
    return HistoricalValuationCategory.NORMAL


def evaluate_historical_valuation(
    historical_valuations: list[HistoricalValuation],
    stock_code: str,
    current_per: Decimal | None,
    current_per_basis: ValuationBasis,
    current_pbr: Decimal | None,
    current_pbr_basis: ValuationBasis,
    evaluation_at: dt.datetime,
    config: HistoricalValuationRulesConfig,
) -> HistoricalValuationResult:
    """銘柄自身の過去PER/PBR水準に対する現在値のランクベース評価結果を算出する。

    PER・PBRそれぞれについて、現在値が存在し、basisが過去データと一致し、
    かつ有効な過去データ点数が`config.min_data_points_required`以上ある場合
    のみコンポーネントを評価する。利用可能なコンポーネントのみを
    per_weight/pbr_weightで加重平均し(片方しか無ければその重みだけで
    正規化する)、両方とも算出不可の場合はstate=NOT_EVALUATEDを返す
    (推測で補完しない)。
    """
    require_timezone_aware(evaluation_at)
    evaluation_date = evaluation_date_jst(evaluation_at)

    per_series = _filter_series(
        historical_valuations,
        stock_code,
        evaluation_date,
        current_per_basis,
        "per",
        "per_basis",
        config.per_absolute_min,
        config.per_absolute_max,
        config,
    )
    pbr_series = _filter_series(
        historical_valuations,
        stock_code,
        evaluation_date,
        current_pbr_basis,
        "pbr",
        "pbr_basis",
        config.pbr_absolute_min,
        config.pbr_absolute_max,
        config,
    )

    components: list[tuple[float, float]] = []  # (score, weight)
    per_score: float | None = None
    per_percentile: float | None = None
    pbr_score: float | None = None
    pbr_percentile: float | None = None
    reason_codes: set[str] = set()
    excluded_data_reasons: set[str] = set()

    per_evaluated = (
        current_per is not None
        and current_per > 0
        and per_series.data_count_used >= config.min_data_points_required
    )
    if per_evaluated:
        assert current_per is not None  # noqa: S101 型絞り込み用(mypy対応)
        per_score, per_percentile = _percentile_rank_score(current_per, per_series.values)
        components.append((per_score, config.per_weight))
    excluded_data_reasons |= per_series.excluded_reasons

    pbr_evaluated = (
        current_pbr is not None
        and current_pbr > 0
        and pbr_series.data_count_used >= config.min_data_points_required
    )
    if pbr_evaluated:
        assert current_pbr is not None  # noqa: S101 型絞り込み用(mypy対応)
        pbr_score, pbr_percentile = _percentile_rank_score(current_pbr, pbr_series.values)
        components.append((pbr_score, config.pbr_weight))
    excluded_data_reasons |= pbr_series.excluded_reasons

    if not per_evaluated and current_per is not None:
        reason_codes.add("PER_INSUFFICIENT_DATA_OR_BASIS_MISMATCH")
    if not pbr_evaluated and current_pbr is not None:
        reason_codes.add("PBR_INSUFFICIENT_DATA_OR_BASIS_MISMATCH")

    if not components:
        return HistoricalValuationResult(
            state=HistoricalValuationEvaluationState.NOT_EVALUATED,
            per_percentile=per_percentile,
            pbr_percentile=pbr_percentile,
            current_per=current_per,
            current_pbr=current_pbr,
            current_per_basis=current_per_basis,
            current_pbr_basis=current_pbr_basis,
            per_data_count_raw=per_series.data_count_raw,
            per_data_count_used=per_series.data_count_used,
            pbr_data_count_raw=pbr_series.data_count_raw,
            pbr_data_count_used=pbr_series.data_count_used,
            excluded_data_reasons=tuple(sorted(excluded_data_reasons)),
            reason_codes=tuple(sorted(reason_codes)),
            evaluated_at=evaluation_at,
            model_version=config.model_version,
        )

    total_weight = sum(weight for _, weight in components)
    score = sum(s * weight for s, weight in components) / total_weight
    category = _classify_category(score, config)

    per_sufficiency = (
        min(1.0, per_series.data_count_used / config.full_confidence_data_points)
        if per_evaluated
        else 0.0
    )
    pbr_sufficiency = (
        min(1.0, pbr_series.data_count_used / config.full_confidence_data_points)
        if pbr_evaluated
        else 0.0
    )
    total_config_weight = config.per_weight + config.pbr_weight
    coverage = (
        per_sufficiency * config.per_weight + pbr_sufficiency * config.pbr_weight
    ) / total_config_weight

    if coverage >= config.coverage_high_threshold and not excluded_data_reasons:
        confidence = ConfidenceLevel.HIGH
    elif coverage >= config.coverage_medium_threshold:
        confidence = ConfidenceLevel.MEDIUM
    else:
        confidence = ConfidenceLevel.LOW

    return HistoricalValuationResult(
        state=HistoricalValuationEvaluationState.EVALUATED,
        score=score,
        category=category,
        confidence=confidence,
        coverage=coverage,
        per_score=per_score,
        pbr_score=pbr_score,
        per_percentile=per_percentile,
        pbr_percentile=pbr_percentile,
        current_per=current_per,
        current_pbr=current_pbr,
        current_per_basis=current_per_basis,
        current_pbr_basis=current_pbr_basis,
        per_data_count_raw=per_series.data_count_raw,
        per_data_count_used=per_series.data_count_used,
        pbr_data_count_raw=pbr_series.data_count_raw,
        pbr_data_count_used=pbr_series.data_count_used,
        excluded_data_reasons=tuple(sorted(excluded_data_reasons)),
        reason_codes=tuple(sorted(reason_codes)),
        evaluated_at=evaluation_at,
        model_version=config.model_version,
    )


def historical_valuation_result_to_metrics(result: HistoricalValuationResult) -> dict[str, Any]:
    """HistoricalValuationResultを、Recommendation.historical_valuation_metrics
    (延いてはDecisionSnapshot.historical_valuation_metrics)へ保存する監査用
    dictへ変換する(コードレビュー対応: 後から「なぜこの点数だったか」を
    再現できるようにするため、raw metricsを保存する)。"""
    return {
        "state": result.state.value,
        "category": result.category.value if result.category is not None else None,
        "per_score": result.per_score,
        "pbr_score": result.pbr_score,
        "per_percentile": result.per_percentile,
        "pbr_percentile": result.pbr_percentile,
        "current_per": str(result.current_per) if result.current_per is not None else None,
        "current_pbr": str(result.current_pbr) if result.current_pbr is not None else None,
        "current_per_basis": (
            result.current_per_basis.value if result.current_per_basis is not None else None
        ),
        "current_pbr_basis": (
            result.current_pbr_basis.value if result.current_pbr_basis is not None else None
        ),
        "per_data_count_raw": result.per_data_count_raw,
        "per_data_count_used": result.per_data_count_used,
        "pbr_data_count_raw": result.pbr_data_count_raw,
        "pbr_data_count_used": result.pbr_data_count_used,
        "excluded_data_reasons": list(result.excluded_data_reasons),
        "model_version": result.model_version,
    }


def historical_valuation_config_values(config: HistoricalValuationRulesConfig) -> dict[str, Any]:
    """判定当時に実際に使用したHistorical Valuation Score設定値
    (Recommendation.config_values_used["historical_valuation"]として保存する。
    Phase Aの既存原則: 判定当時に実際に使用された設定値を保存する)。"""
    return {
        "model_version": config.model_version,
        "min_data_points_required": config.min_data_points_required,
        "per_weight": config.per_weight,
        "pbr_weight": config.pbr_weight,
        "outlier_detection_min_data_points": config.outlier_detection_min_data_points,
        "outlier_mad_threshold": config.outlier_mad_threshold,
        "per_absolute_min": config.per_absolute_min,
        "per_absolute_max": config.per_absolute_max,
        "pbr_absolute_min": config.pbr_absolute_min,
        "pbr_absolute_max": config.pbr_absolute_max,
        "full_confidence_data_points": config.full_confidence_data_points,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
    }
