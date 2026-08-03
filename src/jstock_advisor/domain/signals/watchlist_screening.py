"""ウォッチリスト自動追加のスクリーニング判定(ウォッチリスト自動追加機能)。

必須条件(1つでも不成立で不合格)とスコア条件(config化した配点、合計が閾値以上で合格)
の組み合わせで判定する。個別銘柄向けの条件分岐は一切行わず、config化された
閾値・配点のみで判定する。

配点方式は既存の domain/scoring/score.py の score_<factor>(...) -> (points, formula)
+ _linear_score ヘルパーと同じパターンをこのモジュール専用に踏襲する
(ScoringWeightsConfig専用のため既存関数はそのまま流用できない)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from jstock_advisor.config.models import (
    DividendGrowthScoringConfig,
    DividendYieldScoringConfig,
    EquityRatioScoringConfig,
    PayoutRatioScoringConfig,
    ShareholderBenefitScoringConfig,
    WatchlistScreeningRulesConfig,
)
from jstock_advisor.services.screening_data_provider import WatchlistScreeningInput


class MatchedCriterion(StrEnum):
    HIGH_DIVIDEND_YIELD = "HIGH_DIVIDEND_YIELD"
    SOLID_EQUITY_RATIO = "SOLID_EQUITY_RATIO"
    HEALTHY_PAYOUT_RATIO = "HEALTHY_PAYOUT_RATIO"
    DIVIDEND_GROWTH_TRACK_RECORD = "DIVIDEND_GROWTH_TRACK_RECORD"
    SHAREHOLDER_BENEFIT = "SHAREHOLDER_BENEFIT"


class ExclusionReason(StrEnum):
    ALREADY_HELD = "ALREADY_HELD"
    ALREADY_WATCHLISTED = "ALREADY_WATCHLISTED"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    MARKET_CAP_BELOW_THRESHOLD = "MARKET_CAP_BELOW_THRESHOLD"
    NEGATIVE_OPERATING_CASHFLOW = "NEGATIVE_OPERATING_CASHFLOW"
    SEVERE_DIVIDEND_CUT = "SEVERE_DIVIDEND_CUT"
    DEBT_EXCESS = "DEBT_EXCESS"
    DEFICIT = "DEFICIT"
    GOING_CONCERN_DOUBT = "GOING_CONCERN_DOUBT"
    EXCLUDED_SECURITY_TYPE = "EXCLUDED_SECURITY_TYPE"
    SCORE_BELOW_THRESHOLD = "SCORE_BELOW_THRESHOLD"
    RANK_OUTSIDE_ADDITION_LIMIT = "RANK_OUTSIDE_ADDITION_LIMIT"


# 必須条件(R1〜R7)由来のExclusionReason。DATA_INSUFFICIENT/SCORE_BELOW_THRESHOLDとは
# 区別してカテゴリ集計する(実装プラン§9)。Lambdaハンドラ・CLIの両方から
# categorize_screening_result()経由で参照する(判定ロジックを分散させない)。
_REQUIRED_CONDITION_REASONS = frozenset(
    {
        ExclusionReason.MARKET_CAP_BELOW_THRESHOLD,
        ExclusionReason.NEGATIVE_OPERATING_CASHFLOW,
        ExclusionReason.SEVERE_DIVIDEND_CUT,
        ExclusionReason.DEBT_EXCESS,
        ExclusionReason.DEFICIT,
        ExclusionReason.GOING_CONCERN_DOUBT,
        ExclusionReason.EXCLUDED_SECURITY_TYPE,
    }
)


def categorize_exclusion_reasons(
    exclusion_reasons: list[ExclusionReason],
) -> tuple[str, str]:
    """(batch_tracker用category, AuditLog用evaluation_result)を返す。

    優先順位: データ不足 > 必須条件不成立 > スコア不足 > 合格。
    """
    reasons = set(exclusion_reasons)
    if ExclusionReason.DATA_INSUFFICIENT in reasons:
        return "data_insufficient", "DATA_INSUFFICIENT"
    if reasons & _REQUIRED_CONDITION_REASONS:
        return "required_condition_failed", "FAILED_REQUIRED"
    if ExclusionReason.SCORE_BELOW_THRESHOLD in reasons:
        return "score_failed", "FAILED_SCORE"
    return "passed", "PASSED"


@dataclass(frozen=True)
class ScreeningPolicyResult:
    policy_name: str
    passed: bool
    score: float
    matched_criteria: list[MatchedCriterion]
    exclusion_reasons: list[ExclusionReason]
    missing_required_fields: list[str]
    missing_scoring_fields: list[str]
    score_breakdown: dict[str, float]


class ScreeningPolicy(Protocol):
    @property
    def policy_name(self) -> str: ...

    def evaluate(
        self, input: WatchlistScreeningInput, config: WatchlistScreeningRulesConfig
    ) -> ScreeningPolicyResult: ...


# 構造化されたmatched_criteriaから通知・ウォッチリスト登録理由の日本語文言を生成する
# 唯一の変換辞書(通知層・Lambdaハンドラの両方がdescribe_matched_criteria()経由で使う。
# 固定文言を複数箇所へ分散させない)。
_MATCHED_CRITERIA_LABELS: dict[MatchedCriterion, str] = {
    MatchedCriterion.HIGH_DIVIDEND_YIELD: "高配当",
    MatchedCriterion.SOLID_EQUITY_RATIO: "財務健全",
    MatchedCriterion.HEALTHY_PAYOUT_RATIO: "配当性向良好",
    MatchedCriterion.DIVIDEND_GROWTH_TRACK_RECORD: "増配実績あり",
    MatchedCriterion.SHAREHOLDER_BENEFIT: "株主優待あり",
}


def describe_matched_criteria(matched_criteria: list[MatchedCriterion]) -> str:
    labels = [_MATCHED_CRITERIA_LABELS[criterion] for criterion in matched_criteria]
    return "、".join(labels) if labels else "スクリーニング条件に合致"


class RankingEntry(BaseModel):
    """fan-out集約用の構造化ランキング情報(第3回レビュー対応)。

    batch_tracker.record_result()のranking_entry(str)引数へは
    model_dump_json()でJSON文字列化して渡し、finalize側はmodel_validate_json()で
    型安全に復元する。"score|stock_code"のような手組み文字列パースは行わない。
    """

    stock_code: str
    total_score: float
    policy_scores: dict[str, float]
    matched_criteria: list[MatchedCriterion]
    main_metrics: dict[str, str]


class ScoreCriterionValue(BaseModel):
    """通知品質改善(2026-08)で追加。1銘柄・1配点項目あたりのスコア根拠。"""

    criterion_key: str
    label: str
    score: float
    metric_value: str | None


class WatchlistScoreDetail(BaseModel):
    """通知品質改善(2026-08)で追加。合格銘柄の通知再構築用スコア詳細。

    infrastructure/aws/batch_tracker.pyのCandidateProgressRecord.notification_detail
    (passed銘柄のみ)へモデルのまま保持し、JSON化はbatch_tracker.py内部でのみ行う。
    RankingEntry(既存、無変更)とは独立したDynamoDB列・バイト予算を持つ。
    """

    stock_code: str
    criteria: list[ScoreCriterionValue]


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _linear_score(value: float, weight: float, zero_at: float, full_at: float) -> float:
    if full_at == zero_at:
        return 0.0
    ratio = (value - zero_at) / (full_at - zero_at)
    return weight * _clip(ratio, 0.0, 1.0)


def _score_dividend_yield(
    dividend_yield_pct: float | None, params: DividendYieldScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    if dividend_yield_pct is None:
        return 0.0, []
    score = _linear_score(dividend_yield_pct, params.weight, params.zero_at_pct, params.full_at_pct)
    matched = (
        [MatchedCriterion.HIGH_DIVIDEND_YIELD] if dividend_yield_pct >= params.zero_at_pct else []
    )
    return score, matched


def _score_equity_ratio(
    equity_ratio_pct: float | None, params: EquityRatioScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    if equity_ratio_pct is None:
        return 0.0, []
    score = _linear_score(equity_ratio_pct, params.weight, params.zero_at_pct, params.full_at_pct)
    matched = (
        [MatchedCriterion.SOLID_EQUITY_RATIO] if equity_ratio_pct >= params.zero_at_pct else []
    )
    return score, matched


def _score_payout_ratio(
    payout_ratio_pct: float | None, params: PayoutRatioScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    """配当性向は低すぎず高すぎない範囲(healthy_min_pct〜healthy_max_pct)を満点とする
    山型の配点。範囲外は健全域からの乖離に応じて線形に逓減する。"""
    if payout_ratio_pct is None:
        return 0.0, []
    healthy_min = params.healthy_min_pct
    healthy_max = params.healthy_max_pct
    weight = params.weight
    if healthy_min <= payout_ratio_pct <= healthy_max:
        return weight, [MatchedCriterion.HEALTHY_PAYOUT_RATIO]
    if payout_ratio_pct < healthy_min:
        ratio = payout_ratio_pct / healthy_min if healthy_min > 0 else 0.0
    else:
        span = healthy_max if healthy_max > 0 else 1.0
        ratio = 1.0 - (payout_ratio_pct - healthy_max) / span
    return weight * _clip(ratio, 0.0, 1.0), []


def _score_dividend_growth(
    consecutive_years: int | None, params: DividendGrowthScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    if consecutive_years is None or consecutive_years <= 0:
        return 0.0, []
    score = _linear_score(
        float(consecutive_years),
        params.weight,
        float(params.zero_at_years),
        float(params.full_at_years),
    )
    return score, [MatchedCriterion.DIVIDEND_GROWTH_TRACK_RECORD]


def _score_shareholder_benefit(
    exists: bool, yield_pct: float | None, params: ShareholderBenefitScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    if not exists:
        return 0.0, []
    weight = params.weight
    if yield_pct is None:
        return weight * params.presence_only_score_ratio, [MatchedCriterion.SHAREHOLDER_BENEFIT]
    score = _linear_score(yield_pct, weight, 0.0, params.yield_full_at_pct)
    return score, [MatchedCriterion.SHAREHOLDER_BENEFIT]


class HighDividendFinancialHealthPolicy:
    """高配当・財務健全性を軸とした初期スクリーニングPolicy(v1で実装する唯一のPolicy)。"""

    policy_name = "high_dividend_financial_health"

    def evaluate(
        self, input: WatchlistScreeningInput, config: WatchlistScreeningRulesConfig
    ) -> ScreeningPolicyResult:
        if input.missing_required_fields:
            return ScreeningPolicyResult(
                policy_name=self.policy_name,
                passed=False,
                score=0.0,
                matched_criteria=[],
                exclusion_reasons=[ExclusionReason.DATA_INSUFFICIENT],
                missing_required_fields=input.missing_required_fields,
                missing_scoring_fields=input.missing_scoring_fields,
                score_breakdown={},
            )

        # missing_required_fieldsが空の時点でmarket_cap/operating_cashflowは非Noneが保証される
        # (services/screening_data_provider.pyの_to_screening_input参照)。
        assert input.market_cap is not None
        assert input.operating_cashflow is not None

        thresholds = config.thresholds
        exclusion_reasons: list[ExclusionReason] = []

        if input.market_cap < thresholds.minimum_market_cap_yen:
            exclusion_reasons.append(ExclusionReason.MARKET_CAP_BELOW_THRESHOLD)
        if thresholds.require_positive_operating_cash_flow and input.operating_cashflow <= 0:
            exclusion_reasons.append(ExclusionReason.NEGATIVE_OPERATING_CASHFLOW)
        if thresholds.exclude_dividend_cut_announced and (
            input.is_dividend_cut_announced or input.is_dividend_omission_announced
        ):
            exclusion_reasons.append(ExclusionReason.SEVERE_DIVIDEND_CUT)
        if thresholds.exclude_debt_excess and input.is_debt_excess:
            exclusion_reasons.append(ExclusionReason.DEBT_EXCESS)
        if thresholds.exclude_deficit and input.is_deficit:
            exclusion_reasons.append(ExclusionReason.DEFICIT)
        if thresholds.exclude_going_concern_doubt and input.is_going_concern_doubt:
            exclusion_reasons.append(ExclusionReason.GOING_CONCERN_DOUBT)
        if (thresholds.exclude_etf and input.security_type == "ETF") or (
            thresholds.exclude_reit and input.security_type == "REIT"
        ):
            exclusion_reasons.append(ExclusionReason.EXCLUDED_SECURITY_TYPE)

        scoring = config.scoring
        score_breakdown: dict[str, float] = {}
        matched_criteria: list[MatchedCriterion] = []

        dy_score, dy_matched = _score_dividend_yield(
            input.dividend_yield_pct, scoring.dividend_yield
        )
        score_breakdown["dividend_yield"] = dy_score
        matched_criteria += dy_matched

        er_score, er_matched = _score_equity_ratio(input.equity_ratio_pct, scoring.equity_ratio)
        score_breakdown["equity_ratio"] = er_score
        matched_criteria += er_matched

        pr_score, pr_matched = _score_payout_ratio(input.payout_ratio_pct, scoring.payout_ratio)
        score_breakdown["payout_ratio"] = pr_score
        matched_criteria += pr_matched

        dg_score, dg_matched = _score_dividend_growth(
            input.consecutive_dividend_increase_years, scoring.dividend_growth
        )
        score_breakdown["dividend_growth"] = dg_score
        matched_criteria += dg_matched

        sb_score, sb_matched = _score_shareholder_benefit(
            input.shareholder_benefit_exists,
            input.shareholder_benefit_yield_pct,
            scoring.shareholder_benefit,
        )
        score_breakdown["shareholder_benefit"] = sb_score
        matched_criteria += sb_matched

        total_score = sum(score_breakdown.values())

        if len(input.missing_scoring_fields) > config.max_missing_fields:
            exclusion_reasons.append(ExclusionReason.DATA_INSUFFICIENT)
        if total_score < scoring.minimum_total_score:
            exclusion_reasons.append(ExclusionReason.SCORE_BELOW_THRESHOLD)

        return ScreeningPolicyResult(
            policy_name=self.policy_name,
            passed=not exclusion_reasons,
            score=total_score,
            matched_criteria=matched_criteria,
            exclusion_reasons=exclusion_reasons,
            missing_required_fields=input.missing_required_fields,
            missing_scoring_fields=input.missing_scoring_fields,
            score_breakdown=score_breakdown,
        )
