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
    policy_name: str | None = None


def build_maintenance_screening_summary(
    result: WatchlistScreeningResult,
) -> MaintenanceScreeningSummary:
    hard_exclusion_reasons: list[str] = []
    for policy_result in result.policy_results:
        hard_exclusion_reasons.extend(policy_result.hard_exclusion_reasons)
    return MaintenanceScreeningSummary(
        passed=result.passed,
        total_score=result.total_score,
        matched_target_types=[c.value for c in result.matched_criteria],
        hard_exclusion_reasons=hard_exclusion_reasons,
        policy_name=result.policy_results[0].policy_name if result.policy_results else None,
    )

# 即時削除(Aルート)対象の理由文字列プレフィックス。
# domain/signals/watchlist_screening.py::_evaluate_hard_exclusions()が返す
# 人間可読な文言(ScreeningPolicyResult.hard_exclusion_reasons)と一致させる。
# 流動性不足("平均売買代金...")・業種未対応("業種(...)は個別評価ルール未実装")・
# 重大業績悪化("直近決算で重大な業績悪化")・開示リスク("開示情報にリスク
# キーワードを検出")は意図的に含めない(Bルートで扱う、レビュー修正3)。
_IMMEDIATE_REMOVAL_REASON_PREFIXES = (
    "REITは対象外です",
    "ETFは対象外です",
    "債務超過",
    "継続企業の前提に重大な疑義",
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


def _immediate_removal_reason(hard_exclusion_reasons: list[str]) -> str | None:
    for reason in hard_exclusion_reasons:
        if any(reason.startswith(prefix) for prefix in _IMMEDIATE_REMOVAL_REASON_PREFIXES):
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
        updated = item.model_copy(update={"last_screened_at": now})
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
            }
        )
        return MaintenanceDecision(MaintenanceOutcome.KEEP, updated)

    # 非該当(passed=False)。まずAルート(即時削除)を判定する。
    hard_exclusion_reasons = screening_summary.hard_exclusion_reasons
    immediate_reason = _immediate_removal_reason(hard_exclusion_reasons)

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
