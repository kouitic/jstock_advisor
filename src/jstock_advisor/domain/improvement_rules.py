"""週次改善レビューの純粋ロジック(振り返り機能改修)。

candidate_keyの生成規則のみを扱う。判定閾値そのもの(min_success_rate_pct等)は
config/review_improvement.yaml・ReviewImprovementConfigに置き、Candidate判定・
Issue化条件はweekly_improvement_review_serviceが担う。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import RecommendationType


def build_candidate_key(
    recommendation_type: RecommendationType,
    rule_version: str,
    segment_key: str | None,
    problem_category: str,
) -> str:
    """複数週を跨ぐ同一問題の決定的な識別子を生成する。

    problem_categoryは固定値("PERFORMANCE_DEGRADED"/
    "EVALUATION_CRITERIA_UNDEFINED")のみを取るため、reason_codesの配列順に
    影響されない(reason_codesはこのkeyの構成要素に含めない)。
    """
    return (
        f"{recommendation_type.value}|{rule_version}|{segment_key or 'ALL'}|{problem_category}"
    )
