"""AUTO_SCREENING銘柄の自動メンテナンス(再評価・自動削除、計画Part C)。

既存の`MultiStyleMonitoringPolicy`/`WatchlistScreeningService.evaluate()`を
無改造で再利用する(新規候補評価と同じpure functionを「既存ウォッチリスト
項目の再評価」にもそのまま適用できることを実コード調査で確認済み。両者とも
「新規候補か既存項目か」を一切区別しない)。

3段階の削除ロジック(計画Part C-3、レビュー修正2反映):

A. 即時削除: REIT/ETF化・債務超過・継続企業の前提への重大な疑義、という
   恒久的に投資対象外と言える理由のみ。`minimum_not_qualified_span_days`は
   適用しない。
B. 3回連続非該当+最低継続期間: 上記以外の非該当理由(開示リスクキーワード・
   重大業績悪化・流動性不足・金融業/業種モデル未対応・StockType非該当)は
   すべてこちら。`created_at`から`minimum_age_days`以上、
   `consecutive_not_qualified_count>=consecutive_not_qualified_required`、
   かつ`removal_candidate_since`から`minimum_not_qualified_span_days`以上
   経過、の3条件をすべて満たした場合のみ削除する。週次実行回数だけに依存した
   拙速な削除を防ぐため、件数条件と期間条件を独立したAND条件とする。
C. 長期確認不能: データ取得エラー等で有効な再評価ができないまま
   `maximum_unconfirmed_days`を超えた場合、削除はせず運用警告の対象とする
   (`MaintenanceDecision.stale_unconfirmed`)。

価格タイミング(現在値とBUY価格の乖離等)は判定に一切使わない
(`MultiStyleMonitoringPolicy`自体が価格接近度を参照しないため、追加コード
なしで設計上自動的に満たされる)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from jstock_advisor.config.models import AutoRemovalConfig
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.signals.watchlist_screening import HardExclusionCode
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningResult


class MaintenanceScreeningSummary(BaseModel):
    """1銘柄分の再評価結果のうち、自動削除判定に必要な項目だけを抜き出した
    JSON直列化可能な要約(`WatchlistScreeningResult`本体はJSON往復を想定した
    形をしていないため、Workerが評価直後にこの要約へ変換してDynamoDBへ保存し
    (`CandidateProgressRecord.screening_summary_json`)、finalize時にこれだけを
    読み戻す。`WatchlistScoreDetail`と同じ「モデル型のまま保持、JSON化は
    batch_tracker.py内部にのみ存在する」パターンを踏襲する)。
    """

    passed: bool
    total_score: float
    matched_target_types: list[str] = Field(default_factory=list)
    hard_exclusion_reasons: list[str] = Field(default_factory=list)
    # 横断整合性レビュー対応(2026-08、指摘4)で追加。hard_exclusion_reasonsと
    # 同じ順序・同じ長さで対応する構造化コード。ローリングデプロイ中に旧
    # コードのWorkerが書き込んだJSONにはこのキーが存在しないが、Pydanticの
    # default_factoryにより空リストとして読み戻される(後方互換)。
    hard_exclusion_codes: list[HardExclusionCode] = Field(default_factory=list)
    policy_name: str | None = None


def build_maintenance_screening_summary(
    result: WatchlistScreeningResult,
) -> MaintenanceScreeningSummary:
    hard_exclusion_reasons: list[str] = []
    hard_exclusion_codes: list[HardExclusionCode] = []
    for policy_result in result.policy_results:
        hard_exclusion_reasons.extend(policy_result.hard_exclusion_reasons)
        hard_exclusion_codes.extend(policy_result.hard_exclusion_codes)
    return MaintenanceScreeningSummary(
        passed=result.passed,
        total_score=result.total_score,
        matched_target_types=[c.value for c in result.matched_criteria],
        hard_exclusion_reasons=hard_exclusion_reasons,
        hard_exclusion_codes=hard_exclusion_codes,
        policy_name=result.policy_results[0].policy_name if result.policy_results else None,
    )

# 即時削除(Aルート)対象の構造化コード(横断整合性レビュー対応2026-08、
# 指摘4)。以前はhard_exclusion_reasons(人間可読な日本語文言)への
# str.startswith()で判定しており、文言だけを変更しても判定ロジックが静かに
# 壊れる脆弱性があった。流動性不足・業種未対応・重大業績悪化・開示リスクは
# 意図的に含めない(Bルートで扱う、レビュー修正3)。
_PERMANENT_HARD_EXCLUSION_CODES = frozenset(
    {
        HardExclusionCode.REIT_EXCLUDED,
        HardExclusionCode.ETF_EXCLUDED,
        HardExclusionCode.NEGATIVE_EQUITY,
        HardExclusionCode.GOING_CONCERN_DOUBT,
    }
)


class MaintenanceOutcome(StrEnum):
    KEEP = "KEEP"
    IMMEDIATE_REMOVAL = "IMMEDIATE_REMOVAL"
    CONSECUTIVE_NOT_QUALIFIED_REMOVAL = "CONSECUTIVE_NOT_QUALIFIED_REMOVAL"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


@dataclass(frozen=True)
class MaintenanceDecision:
    outcome: MaintenanceOutcome
    updated_item: WatchlistItem
    removal_reason: str | None = None
    # C. 長期確認不能(計画Part C-3)。DATA_UNAVAILABLEが続き
    # maximum_unconfirmed_daysを超えた場合のみTrue(削除はしない、運用警告用)。
    stale_unconfirmed: bool = False


def _immediate_removal_reason(
    hard_exclusion_codes: list[HardExclusionCode], hard_exclusion_reasons: list[str]
) -> str | None:
    """恒久的な投資対象外理由(HardExclusionCode)による即時削除判定。

    後方互換(横断整合性レビュー対応2026-08、指摘4): hard_exclusion_codesが
    空(ローリングデプロイ中に旧コードのWorkerが書き込んだscreening_summary_
    jsonを新コードのFinalizerが読み戻したケース)の場合、hard_exclusion_
    reasonsに恒久除外理由の文言が含まれていても構造化コードで判定できないため
    即時削除はしない(zip()はcodesが空なら何も反復しない)。この場合でも
    Bルート(3回連続非該当+最低継続期間)は引き続き機能するため、削除自体が
    行われなくなるわけではなく、より安全な遅延削除へフォールバックするだけ。
    """
    for code, reason in zip(hard_exclusion_codes, hard_exclusion_reasons, strict=False):
        if code in _PERMANENT_HARD_EXCLUSION_CODES:
            return reason
    return None


def evaluate_maintenance_decision(
    item: WatchlistItem,
    screening_summary: MaintenanceScreeningSummary | None,
    config: AutoRemovalConfig,
    now: dt.datetime,
) -> MaintenanceDecision:
    """1銘柄分の再評価結果から次の状態を決定する(呼び出し側はDynamoDB書き込み・
    監査記録を一切行わない、純粋な判定のみ)。

    `screening_summary=None`はScreeningDataStatus.DATA_ERROR/NOT_FOUND(データ
    取得自体ができなかった)を表す。この場合`consecutive_not_qualified_count`・
    `removal_candidate_since`は変更せず、`last_screened_at`のみ更新する。
    """
    if screening_summary is None:
        # Issue #58 Phase B2(F-O5): system-owned stateを変更して永続化する経路では
        # `updated_at`も進める。`updated_at`は「そのレコードが最後に実質的に
        # 更新された時刻」であり、利用者の最終編集日時ではない。
        # DATA_UNAVAILABLEでも「確認を試みた事実」を`last_screened_at`へ記録する
        # 実質的な更新であるため、ここも対象に含む。
        updated = item.model_copy(update={"last_screened_at": now, "updated_at": now})
        reference_time = item.last_qualified_at or item.created_at
        stale = (now - reference_time).days > config.maximum_unconfirmed_days
        return MaintenanceDecision(
            MaintenanceOutcome.DATA_UNAVAILABLE, updated, stale_unconfirmed=stale
        )

    if screening_summary.passed:
        updated = item.model_copy(
            update={
                "last_screened_at": now,
                "last_qualified_at": now,
                "consecutive_not_qualified_count": 0,
                "removal_candidate_since": None,
                "last_monitoring_score": screening_summary.total_score,
                "last_matched_target_types": screening_summary.matched_target_types,
                "last_screening_result": "PASSED",
                "last_screening_policy": screening_summary.policy_name,
                # Issue #58 Phase B2(F-O5)
                "updated_at": now,
            }
        )
        return MaintenanceDecision(MaintenanceOutcome.KEEP, updated)

    # 非該当(passed=False)。まずAルート(即時削除)を判定する。
    hard_exclusion_reasons = screening_summary.hard_exclusion_reasons
    immediate_reason = _immediate_removal_reason(
        screening_summary.hard_exclusion_codes, hard_exclusion_reasons
    )

    new_consecutive_count = item.consecutive_not_qualified_count + 1
    new_removal_candidate_since = item.removal_candidate_since or now
    updated = item.model_copy(
        update={
            "last_screened_at": now,
            "consecutive_not_qualified_count": new_consecutive_count,
            "removal_candidate_since": new_removal_candidate_since,
            "last_monitoring_score": screening_summary.total_score,
            "last_matched_target_types": screening_summary.matched_target_types,
            "last_screening_result": "FAILED",
            "last_screening_policy": screening_summary.policy_name,
            # Issue #58 Phase B2(F-O5)。削除系outcomeでは`updated_item`が
            # 永続化されない(finalizerがdelete分岐へ入る)ため、この値は
            # 監査記録の参照にのみ使われる。KEEP(非該当1回目等)では永続化される。
            "updated_at": now,
        }
    )

    if immediate_reason is not None:
        return MaintenanceDecision(
            MaintenanceOutcome.IMMEDIATE_REMOVAL, updated, removal_reason=immediate_reason
        )

    age_days = (now - item.created_at).days
    span_days = (now - new_removal_candidate_since).days
    if (
        age_days >= config.minimum_age_days
        and new_consecutive_count >= config.consecutive_not_qualified_required
        and span_days >= config.minimum_not_qualified_span_days
    ):
        reasons_summary = (
            "、".join(hard_exclusion_reasons)
            if hard_exclusion_reasons
            else "対象5タイプいずれにも非該当"
        )
        return MaintenanceDecision(
            MaintenanceOutcome.CONSECUTIVE_NOT_QUALIFIED_REMOVAL,
            updated,
            removal_reason=reasons_summary,
        )

    return MaintenanceDecision(MaintenanceOutcome.KEEP, updated)
