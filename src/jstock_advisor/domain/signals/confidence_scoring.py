"""信頼度の再設計(要求仕様12節)。

信頼度は「判定の強さ」ではなく「データと分析の信頼性」を表す指標とする。
以下のいずれかに該当する場合、スコアに関わらずHIGHを禁止する。
- 決算発表まで指定営業日数以内である
- 最新四半期データが未取得である
- 株式分割後、調整確認が未完了のまま指定日数以内である
- 権利確定情報が不明である
- 適正価格手法間の差が大きい
- 重要指標の一部がnullである
- 一次情報と二次情報が矛盾している
- 一過性要因を分離できていない
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.config.models import ConfidenceRulesConfig
from jstock_advisor.domain.entities.enums import ConfidenceLevel


@dataclass(frozen=True)
class ConfidenceFactors:
    data_freshness_days: int | None = None
    primary_source_fetch_rate: float | None = None  # 0.0-1.0
    corporate_action_adjustment_consistent: bool | None = None
    financial_period_comparable: bool | None = None
    fair_value_methods_used_count: int = 0
    fair_value_method_spread_ratio: float | None = None  # max/min
    days_to_next_earnings_business_days: int | None = None
    latest_quarter_fetched: bool | None = None
    days_since_last_split: int | None = None
    split_adjustment_confirmed: bool | None = None
    record_date_known: bool | None = None
    key_metric_missing: bool = False
    primary_secondary_conflict: bool = False
    one_time_factors_identified: bool | None = None
    cross_rule_agreement: bool | None = None


@dataclass(frozen=True)
class ConfidenceScoreResult:
    score: float
    level: ConfidenceLevel
    reasons_not_high: list[str]


def _high_confidence_disallow_reasons(
    factors: ConfidenceFactors, config: ConfidenceRulesConfig
) -> list[str]:
    d = config.high_confidence_disallow
    reasons: list[str] = []

    if (
        factors.days_to_next_earnings_business_days is not None
        and factors.days_to_next_earnings_business_days <= d.min_business_days_to_earnings
    ):
        reasons.append("決算発表が近く、確定的な判断ができない")

    if factors.latest_quarter_fetched is not True:
        reasons.append("最新四半期データが未取得または未確認")

    if (
        factors.days_since_last_split is not None
        and factors.days_since_last_split <= d.max_days_since_split_for_unconfirmed_adjustment
        and factors.split_adjustment_confirmed is not True
    ):
        reasons.append("株式分割後の調整確認が未完了")

    if factors.record_date_known is not True:
        reasons.append("権利確定情報が不明または未確認")

    if (
        factors.fair_value_method_spread_ratio is not None
        and factors.fair_value_method_spread_ratio >= d.max_fair_value_method_spread_ratio
    ):
        reasons.append("適正価格手法間の差が大きい")

    if factors.key_metric_missing:
        reasons.append("重要指標の一部が欠損している")

    if factors.primary_secondary_conflict:
        reasons.append("一次情報と二次情報が矛盾している")

    if factors.one_time_factors_identified is not True:
        reasons.append("一過性要因を分離できていない、または未確認")

    return reasons


def compute_confidence(
    factors: ConfidenceFactors, config: ConfidenceRulesConfig
) -> ConfidenceScoreResult:
    w = config.scoring
    score = w.base_score
    reasons: list[str] = []

    if (
        factors.data_freshness_days is not None
        and factors.data_freshness_days > config.max_data_freshness_days
    ):
        score -= w.penalty_stale_data
        reasons.append("データ鮮度が低い")

    if (
        factors.primary_source_fetch_rate is not None
        and factors.primary_source_fetch_rate < config.min_primary_source_rate
    ):
        score -= w.penalty_low_primary_source_rate
        reasons.append("一次情報取得率が低い")

    if factors.corporate_action_adjustment_consistent is False:
        score -= w.penalty_corporate_action_inconsistent
        reasons.append("企業行動調整の整合性に問題がある")

    if factors.financial_period_comparable is False:
        score -= w.penalty_financial_period_incomparable
        reasons.append("財務期間の比較可能性が低い")

    if (
        factors.fair_value_method_spread_ratio is not None
        and factors.fair_value_method_spread_ratio
        >= config.high_confidence_disallow.max_fair_value_method_spread_ratio
    ):
        score -= w.penalty_low_method_agreement
        reasons.append("適正価格手法間の一致度が低い")

    if factors.key_metric_missing:
        score -= w.penalty_missing_data
        reasons.append("重要指標の欠損がある")

    if factors.one_time_factors_identified is False:
        score -= w.penalty_untraced_one_time_factors
        reasons.append("一過性要因を識別できていない")

    if factors.cross_rule_agreement is False:
        score -= w.penalty_cross_rule_disagreement
        reasons.append("判定ルール間の一致度が低い")

    score = max(0.0, score)
    disallow_reasons = _high_confidence_disallow_reasons(factors, config)

    if score >= w.high_threshold and not disallow_reasons:
        level = ConfidenceLevel.HIGH
    elif score >= w.medium_threshold:
        level = ConfidenceLevel.MEDIUM
    else:
        level = ConfidenceLevel.LOW

    all_reasons = reasons + [r for r in disallow_reasons if r not in reasons]
    return ConfidenceScoreResult(score=score, level=level, reasons_not_high=all_reasons)
