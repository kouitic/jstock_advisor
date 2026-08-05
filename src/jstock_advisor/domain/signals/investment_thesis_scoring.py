"""投資ストーリー維持スコア(0-50点)の算出(実装プラン3節)。

「長期保有する理由が今も維持されているか」を評価する。baseline比較が必要な
項目(優待条件維持・利益CF前提維持・財務前提維持)は、比較不能な場合
(呼び出し側がNoneを渡す。SYSTEM_INITIALIZED baselineの初回評価等)は
NOT_EVALUATEDとし、current=baselineで自動的に満点を付与しない。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.config.models import InvestmentThesisTemplateConfig, InvestmentThesisWeights
from jstock_advisor.domain.entities.enums import (
    BaselineOrigin,
    EvidenceCoverageStatus,
    ThesisConditionAttestationStatus,
)
from jstock_advisor.domain.entities.holding_decision import (
    InvestmentThesis,
    InvestmentThesisScore,
    ScoreItemDetail,
)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class InvestmentThesisInputs:
    current_total_yield_pct: float | None
    has_shareholder_benefit: bool
    # None = baseline比較不能(SYSTEM_INITIALIZED初回評価等)。以下3項目共通。
    benefit_abolished_or_downgraded: bool | None
    dividend_cut_or_omission_confirmed: bool
    profit_cf_premise_broken: bool | None
    financial_premise_broken: bool | None
    thesis: InvestmentThesis | None


def score_investment_thesis(
    inputs: InvestmentThesisInputs,
    weights: InvestmentThesisWeights,
    template: InvestmentThesisTemplateConfig,
    fresh_within_days: int,
    stale_after_days: int,
    now: dt.datetime,
    baseline_id: str | None = None,
    baseline_version: int | None = None,
    baseline_origin: BaselineOrigin | None = None,
) -> InvestmentThesisScore:
    items: list[ScoreItemDetail] = []

    # 1. 配当方針の維持(公式確認済みの減配・無配転落の有無。絶対条件、baseline不要)
    items.append(
        ScoreItemDetail(
            item_code="dividend_policy",
            axis="dividend_policy",
            weight=weights.dividend_policy,
            status=EvidenceCoverageStatus.EVALUATED,
            points_earned=(
                0.0 if inputs.dividend_cut_or_omission_confirmed else weights.dividend_policy
            ),
        )
    )

    # 2. 配当+優待の総合利回りの維持(絶対条件、baseline不要)
    if inputs.current_total_yield_pct is None:
        items.append(
            ScoreItemDetail(
                item_code="total_yield",
                axis="total_yield",
                weight=weights.total_yield,
                status=EvidenceCoverageStatus.NOT_EVALUATED,
            )
        )
    else:
        ratio = _clip(inputs.current_total_yield_pct / template.min_total_yield_pct, 0.0, 1.0)
        items.append(
            ScoreItemDetail(
                item_code="total_yield",
                axis="total_yield",
                weight=weights.total_yield,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=weights.total_yield * ratio,
            )
        )

    # 3. 優待条件の維持(baseline比較が必要)
    if not inputs.has_shareholder_benefit:
        items.append(
            ScoreItemDetail(
                item_code="benefit_condition",
                axis="benefit_condition",
                weight=weights.benefit_condition,
                status=EvidenceCoverageStatus.NOT_APPLICABLE,
                reason="優待非保有銘柄",
            )
        )
    elif inputs.benefit_abolished_or_downgraded is None:
        items.append(
            ScoreItemDetail(
                item_code="benefit_condition",
                axis="benefit_condition",
                weight=weights.benefit_condition,
                status=EvidenceCoverageStatus.NOT_EVALUATED,
            )
        )
    else:
        items.append(
            ScoreItemDetail(
                item_code="benefit_condition",
                axis="benefit_condition",
                weight=weights.benefit_condition,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=(
                    0.0 if inputs.benefit_abolished_or_downgraded else weights.benefit_condition
                ),
            )
        )

    # 4. 中長期的な利益・CF前提の維持(baseline比較が必要)
    if inputs.profit_cf_premise_broken is None:
        items.append(
            ScoreItemDetail(
                item_code="profit_cf_premise",
                axis="profit_cf_premise",
                weight=weights.profit_cf_premise,
                status=EvidenceCoverageStatus.NOT_EVALUATED,
            )
        )
    else:
        items.append(
            ScoreItemDetail(
                item_code="profit_cf_premise",
                axis="profit_cf_premise",
                weight=weights.profit_cf_premise,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=(
                    0.0 if inputs.profit_cf_premise_broken else weights.profit_cf_premise
                ),
            )
        )

    # 5. 財務健全性に関する投資前提の維持(baseline比較が必要)
    if inputs.financial_premise_broken is None:
        items.append(
            ScoreItemDetail(
                item_code="financial_premise",
                axis="financial_premise",
                weight=weights.financial_premise,
                status=EvidenceCoverageStatus.NOT_EVALUATED,
            )
        )
    else:
        items.append(
            ScoreItemDetail(
                item_code="financial_premise",
                axis="financial_premise",
                weight=weights.financial_premise,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=(
                    0.0 if inputs.financial_premise_broken else weights.financial_premise
                ),
            )
        )

    # 6. 個別に登録された銘柄固有条件(人間のattestationのみで採点。共通テンプレートで代用しない)
    conditions = inputs.thesis.conditions if inputs.thesis is not None else []
    if not conditions:
        items.append(
            ScoreItemDetail(
                item_code="custom_conditions",
                axis="custom_conditions",
                weight=weights.custom_conditions,
                status=EvidenceCoverageStatus.NOT_APPLICABLE,
                reason="銘柄固有条件が未登録",
            )
        )
    else:
        usable = 0
        maintained = 0
        stale_present = False
        for condition in conditions:
            attestation = condition.last_attestation
            if attestation is None:
                continue
            age_days = (now - attestation.attested_at).days
            if age_days > stale_after_days:
                continue
            usable += 1
            if age_days > fresh_within_days:
                stale_present = True
            if attestation.status == ThesisConditionAttestationStatus.MAINTAINED:
                maintained += 1
        if usable == 0:
            items.append(
                ScoreItemDetail(
                    item_code="custom_conditions",
                    axis="custom_conditions",
                    weight=weights.custom_conditions,
                    status=EvidenceCoverageStatus.NOT_EVALUATED,
                    reason="有効なattestationが無い(未申告または鮮度期限超過)",
                )
            )
        else:
            ratio = maintained / usable
            items.append(
                ScoreItemDetail(
                    item_code="custom_conditions",
                    axis="custom_conditions",
                    weight=weights.custom_conditions,
                    status=EvidenceCoverageStatus.EVALUATED,
                    points_earned=weights.custom_conditions * ratio,
                    reason="STALE_ATTESTATION_PRESENT" if stale_present else None,
                )
            )

    evaluated_weight = sum(
        i.weight for i in items if i.status == EvidenceCoverageStatus.EVALUATED
    )
    available_weight = sum(
        i.weight for i in items if i.status != EvidenceCoverageStatus.NOT_APPLICABLE
    )
    raw_points = sum(i.points_earned for i in items)

    score = (raw_points / available_weight * 50.0) if available_weight > 0 else 0.0
    coverage_ratio = (evaluated_weight / available_weight) if available_weight > 0 else 0.0

    return InvestmentThesisScore(
        score=score,
        coverage_ratio=coverage_ratio,
        items=tuple(items),
        baseline_id=baseline_id,
        baseline_version=baseline_version,
        baseline_origin=baseline_origin,
    )
