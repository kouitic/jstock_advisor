"""推奨判定の安全制約(要求仕様22節)。

判定の強度: INFO < WATCH < REVIEW < PARTIAL_ACTION < FULL_ACTION < URGENT_REVIEW。
強い判定へ進むほど、必要な根拠数・一次情報率・データ品質スコア・信頼度を
高く要求する。以下のいずれかに該当する場合、FULL_ACTIONまたはURGENT_REVIEWを禁止する。
- データ品質スコアが閾値未満
- 企業行動の未解決事項がある
- 主要データに欠損がある
- 判定と価格に整合性エラーがある
- 一次情報で重大事実を確認できない
- 最新決算から指定日数以上経過している
- 次回決算まで指定営業日数以内である
- 適正価格の信頼度がLOW
- 単一ルールだけで強い判定になっている
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.config.models import ConfidenceRulesConfig
from jstock_advisor.domain.entities.enums import ConfidenceLevel, JudgmentStrength

_STRONG_LEVELS = (JudgmentStrength.FULL_ACTION, JudgmentStrength.URGENT_REVIEW)


@dataclass(frozen=True)
class JudgmentSafetyInputs:
    data_quality_score: float | None = None
    corporate_action_unresolved: bool = False
    key_data_missing: bool = False
    consistency_error: bool = False
    primary_source_confirmed_material_fact: bool | None = None
    latest_earnings_age_days: int | None = None
    days_to_next_earnings_business_days: int | None = None
    fair_value_confidence: ConfidenceLevel | None = None
    single_rule_only: bool = False


def max_allowed_strength(
    inputs: JudgmentSafetyInputs, config: ConfidenceRulesConfig
) -> tuple[JudgmentStrength, list[str]]:
    """このデータ品質・確信度で許容される最大の判定強度と、その制約理由を返す。"""
    ladder = config.judgment_safety_ladder
    reasons: list[str] = []

    if (
        inputs.data_quality_score is not None
        and inputs.data_quality_score < ladder.min_data_quality_score_for_strong_action
    ):
        reasons.append(
            f"データ品質スコア({inputs.data_quality_score:.0f})が"
            f"{ladder.min_data_quality_score_for_strong_action:.0f}未満"
        )

    if inputs.corporate_action_unresolved:
        reasons.append("企業行動の未解決事項がある")

    if inputs.key_data_missing:
        reasons.append("主要データに欠損がある")

    if inputs.consistency_error:
        reasons.append("判定と価格に整合性エラーがある")

    if inputs.primary_source_confirmed_material_fact is not True:
        reasons.append("一次情報で重大事実を確認できない、または未確認")

    if (
        inputs.latest_earnings_age_days is not None
        and inputs.latest_earnings_age_days > ladder.max_latest_earnings_age_days
    ):
        reasons.append(f"最新決算から{inputs.latest_earnings_age_days}日経過している")

    if (
        inputs.days_to_next_earnings_business_days is not None
        and inputs.days_to_next_earnings_business_days
        <= ladder.min_business_days_to_earnings_for_strong_action
    ):
        reasons.append("次回決算が近い")

    if inputs.fair_value_confidence == ConfidenceLevel.LOW:
        reasons.append("適正価格の信頼度がLOW")

    if inputs.single_rule_only:
        reasons.append("単一のルールのみに基づく判定")

    if reasons:
        return JudgmentStrength.PARTIAL_ACTION, reasons
    return JudgmentStrength.URGENT_REVIEW, []


def cap_judgment_strength(
    candidate: JudgmentStrength, inputs: JudgmentSafetyInputs, config: ConfidenceRulesConfig
) -> tuple[JudgmentStrength, list[str]]:
    """候補の判定強度を、安全制約に基づき必要なら引き下げる。"""
    if candidate not in _STRONG_LEVELS:
        return candidate, []
    allowed, reasons = max_allowed_strength(inputs, config)
    if reasons:
        capped = JudgmentStrength.PARTIAL_ACTION
        return (capped, reasons)
    return candidate, []
