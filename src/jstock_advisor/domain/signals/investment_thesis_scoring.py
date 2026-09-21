"""投資ストーリー維持スコア(0-50点)の算出(実装プラン3節)。

「長期保有する理由が今も維持されているか」を評価する。baseline比較が必要な
項目(優待条件維持・利益CF前提維持・財務前提維持)は、比較不能な場合
(呼び出し側がNoneを渡す。SYSTEM_INITIALIZED baselineの初回評価等)は
NOT_EVALUATEDとし、current=baselineで自動的に満点を付与しない。

## NOT_EVALUATED には性質の違う2つが同居する(Issue #249)

    a  データ不足で算出不能        total_yield / custom_conditions
    b  baselineが無く比較不能      上記3項目(入力がNone)

bは「比較する相手がまだ無い」だけであり、悪い状態が観測されたわけではない。
これを0点として分母に数えると、**買った直後の初回評価だけが不当に低く出る**
(Issue #249。実測で最大18.75点の下振れ)。そのためbのみをスコアの分母から
除く。aは従来どおり分母に残す(**Issue #55 Phase A Decision 3は変更しない**。
欠測は「不足として計上する」という別の意図的な決定であり、
tests/unit/test_missing_yield_semantics.py が契約として固定している)。

## 優待条件の状態は1つの純関数で導出する(Issue #470)

優待条件(benefit_condition)は、以前は「現在の優待の有無」
(`benefit is not None and not is_abolished`)だけで NOT_APPLICABLE かどうかを決めていた。
そのため**baseline時に優待があった保有で、現在の台帳に登録が無くなった(取得不能・台帳の削除・移行漏れ)場合**も「優待非保有銘柄」と区別できず、減点も
不評価もされなかった。状態は`derive_benefit_condition_state`で、**baselineの値**と現在の入力から導く。

    baselineに優待なし                          -> NOT_APPLICABLE(現在が廃止登録でも同じ。U-B)
    baselineの値が不明(None)/ 初回評価          -> BASELINE_NOT_COMPARABLE(不評価。分母から外す)
    baselineに優待あり かつ 現在の登録なし      -> DATA_MISSING(不評価。★廃止とみなさない)
    baselineに優待あり かつ 明示的な改悪        -> DOWNGRADED(評価・0点)
    baselineに優待あり かつ 維持                -> MAINTAINED(評価・満点)

★ 明示的な**廃止**(`is_abolished`)は、従来どおり NOT_APPLICABLE のまま変えていない(Issue #476 が、
  同じ関数の状態として「評価・0点」へ改める。本Issueの範囲外)。

DATA_MISSINGは、不評価の理由コード`BENEFIT_DATA_MISSING`で BASELINE_NOT_COMPARABLE と区別する。
`status`には新しい値を足さない(共通enum S-16は変えず、保存形式も変わらない)。スコアの分母からは
外し(悪い状態が観測されたわけではないため)、coverage_ratioの分母は据え置く(確認できていない
事実はcoverageに残る。coverageが下がればcoverage gateが働く)。

## coverage_ratioの分母は変えない

「評価できなかった」事実はcoverage_ratioに残し続ける。スコアだけを直し、
確認できていないことを隠さない。coverageが下がればcoverage gateが働く。
"""

from __future__ import annotations

import datetime as dt
import enum
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

_BASELINE_NOT_COMPARABLE = "BASELINE_NOT_COMPARABLE"
"""baseline比較が必要な項目で、比較対象がまだ無いことを表す理由(Issue #249)。

`status`には新しい値を足さずNOT_EVALUATEDのままとし、既存の`reason`フィールドで
区別する。enumを増やすと共通enum(S-16)の変更となり、保存済みレコードの
読み手すべてに波及するため。**保存形式は変わらない。**
"""


_BENEFIT_DATA_MISSING = "BENEFIT_DATA_MISSING"
"""baselineに優待があったのに、現在は台帳に登録が無い(Issue #470)ことを表す理由。

`status`は既存のNOT_EVALUATEDのまま、`reason`で区別する(共通enum S-16は変えない)。
"""

#: スコアの分母から外す不評価の理由(悪い状態が観測されたわけではない項目)。
#: total_yield等のデータ欠測(理由なし)は、Issue #55 Phase A Decision 3により分母に残す(変更しない)。
_EXCLUDED_FROM_SCORE_DENOMINATOR: frozenset[str] = frozenset(
    {_BASELINE_NOT_COMPARABLE, _BENEFIT_DATA_MISSING}
)


class BenefitConditionState(enum.StrEnum):
    """優待条件(benefit_condition)の状態(Issue #470)。"""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    BASELINE_NOT_COMPARABLE = "BASELINE_NOT_COMPARABLE"
    DATA_MISSING = "DATA_MISSING"
    DOWNGRADED = "DOWNGRADED"
    MAINTAINED = "MAINTAINED"


def derive_benefit_condition_state(
    *,
    baseline_has_benefit: bool | None,
    is_first_evaluation: bool,
    benefit_registered: bool,
    benefit_is_abolished: bool,
    benefit_is_major_downgrade: bool,
) -> BenefitConditionState:
    """優待条件の状態を導く(純関数。I/Oなし)。

    * baselineに優待なし(False)は、現在が廃止登録でも NOT_APPLICABLE(U-B)。
    * baselineの値が不明(None。テスト・repair経路のみ)は、優待なしにも維持にも倒さず不評価。
    * 初回評価(baselineを今作った)は比較不能(Issue #249の既存挙動)。
    * baselineに優待ありで現在の登録が無い場合は、DATA_MISSING(廃止とみなさない)。
    * ★ 明示的な廃止は従来どおり NOT_APPLICABLE(Issue #476 で「評価・0点」へ改める)。
    """
    if baseline_has_benefit is False:
        return BenefitConditionState.NOT_APPLICABLE
    if baseline_has_benefit is None or is_first_evaluation:
        return BenefitConditionState.BASELINE_NOT_COMPARABLE
    if not benefit_registered:
        return BenefitConditionState.DATA_MISSING
    if benefit_is_abolished:
        # 従来の挙動を保つ(has_benefit = not is_abolished が False → NOT_APPLICABLE)。#476で改める。
        return BenefitConditionState.NOT_APPLICABLE
    if benefit_is_major_downgrade:
        return BenefitConditionState.DOWNGRADED
    return BenefitConditionState.MAINTAINED


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class InvestmentThesisInputs:
    current_total_yield_pct: float | None
    # 優待条件の状態(Issue #470。`derive_benefit_condition_state`で導く)。
    benefit_state: BenefitConditionState
    dividend_cut_or_omission_confirmed: bool
    # None = baseline比較不能(SYSTEM_INITIALIZED初回評価等)。以下2項目共通。
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

    # 3. 優待条件の維持(baseline比較が必要。状態は derive_benefit_condition_state で導く)
    state = inputs.benefit_state
    if state is BenefitConditionState.NOT_APPLICABLE:
        items.append(
            ScoreItemDetail(
                item_code="benefit_condition",
                axis="benefit_condition",
                weight=weights.benefit_condition,
                status=EvidenceCoverageStatus.NOT_APPLICABLE,
                reason="優待非保有銘柄",
            )
        )
    elif state in (
        BenefitConditionState.BASELINE_NOT_COMPARABLE,
        BenefitConditionState.DATA_MISSING,
    ):
        items.append(
            ScoreItemDetail(
                item_code="benefit_condition",
                axis="benefit_condition",
                weight=weights.benefit_condition,
                status=EvidenceCoverageStatus.NOT_EVALUATED,
                reason=(
                    _BENEFIT_DATA_MISSING
                    if state is BenefitConditionState.DATA_MISSING
                    else _BASELINE_NOT_COMPARABLE
                ),
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
                    0.0 if state is BenefitConditionState.DOWNGRADED else weights.benefit_condition
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
                reason=_BASELINE_NOT_COMPARABLE,
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
                reason=_BASELINE_NOT_COMPARABLE,
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

    evaluated_weight = sum(i.weight for i in items if i.status == EvidenceCoverageStatus.EVALUATED)
    available_weight = sum(
        i.weight for i in items if i.status != EvidenceCoverageStatus.NOT_APPLICABLE
    )
    # Issue #249: baseline比較不能の項目はスコアの分母から外す。
    # Issue #470: 優待のデータ欠落(BENEFIT_DATA_MISSING)も同じ扱い(廃止とみなさない)。
    # NOT_APPLICABLE(評価対象外)と同じ扱いであり、「悪い」わけではないため。
    # ★ available_weight自体は減らさない。coverage_ratioの分母は据え置き、
    #   「評価できなかった」事実をcoverageに残すため(下のcoverage_ratio参照)。
    not_comparable_weight = sum(
        i.weight
        for i in items
        if i.status == EvidenceCoverageStatus.NOT_EVALUATED
        and i.reason in _EXCLUDED_FROM_SCORE_DENOMINATOR
    )
    score_weight = available_weight - not_comparable_weight
    raw_points = sum(i.points_earned for i in items)

    # score_weight <= 0 はfail-closed。現行configでは到達しない
    # (dividend_policyはbaseline不要で常にEVALUATEDのため分母に残る)が、
    # weightsのvalidatorは合計50点しか検査しないため構造的には起こりうる。
    # そのときscoreだけを0.0にするとcoverageが高いまま残りgateをすり抜けるため、
    # **coverage_ratioも0.0にして「評価できていない」ことを示す。**
    score = (raw_points / score_weight * 50.0) if score_weight > 0 else 0.0
    coverage_ratio = (
        (evaluated_weight / available_weight) if available_weight > 0 and score_weight > 0 else 0.0
    )

    return InvestmentThesisScore(
        score=score,
        coverage_ratio=coverage_ratio,
        items=tuple(items),
        baseline_id=baseline_id,
        baseline_version=baseline_version,
        baseline_origin=baseline_origin,
    )
