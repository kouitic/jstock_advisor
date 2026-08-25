"""買い候補分析1銘柄1回の評価・通知結果レコード(2026-08、買い候補サマリー表示改修)。

将来のLINE詳細理由照会機能に向けた、銘柄コード横断で安定して参照可能な
構造化レコード(要求仕様: 判定理由・通知抑止理由を内部では十分な粒度で保持する)。

既存のRecommendation(判定時点の詳細、buy_decision_reasons等)・DecisionSnapshot・
NotificationLog・AuditLogTable(unified_buy_candidate_evaluation/
unified_buy_candidate_notification_outcome)はいずれも変更しない。本エンティティは
それらを置き換えるものではなく、「通知判定結果(ランク・ブロック理由・送信結果)が
Auditにしか存在せず、銘柄コードからの安定した参照に向かない」というギャップを
埋めるために追加する最小限の構造。

1バッチ実行につき評価対象1銘柄ごとに1件。判定時点(_process_single_candidate)で
作成し、finalize時点(_finalize_batch、ランキングループ通過対象のみ)で同じ行を
更新する。1バッチのfinalizeは1回のみ実行されるため、更新に楽観ロック等の排他制御は
不要(単純なupsertで安全)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import BuyAction, CandidateSource, PurchaseCategory


class BuyCandidateEvaluationRecord(Entity):
    # PK: f"{batch_id}:{stock_code}"(1バッチ内で銘柄コードごとに1回だけ評価
    # されるため決定的・衝突しない)。
    evaluation_id: str
    batch_id: str
    stock_code: str
    evaluated_at: dt.datetime
    rule_version: str
    candidate_source: CandidateSource | None = None
    purchase_category: PurchaseCategory
    final_buy_action: BuyAction | None = None
    raw_buy_action: BuyAction | None = None
    # 判定時点でRecommendationが作成された場合のみ設定(EXCLUDED/
    # DATA_INSUFFICIENTの場合はNoneのまま)。
    recommendation_id: str | None = None
    # Phase 2-B「銘柄分析」向け(2026-08): screen_investment_universe()が返す
    # 除外理由(ScreeningOutcome.exclusion_reasons)。purchase_category=EXCLUDEDの
    # 場合のみ設定する。以前はBuyAnalysisOutcomeに乗ったまま監査ログにしか
    # 記録されず、銘柄コードから安定して参照できなかった(調査で確認済みの
    # ギャップ)。
    exclusion_reasons: tuple[str, ...] | None = None

    # --- finalize時(ランキングループ通過対象のみ)に埋まる。判定時点では
    # 全てNoneのまま ---
    unified_rank: int | None = None
    notification_rank: int | None = None
    notification_eligible: bool | None = None
    # EligibilityBlockCategory.value(既存enumをそのまま機械可読コードとして
    # 再利用する。独自コードは発明しない)。
    notification_block_category: str | None = None
    notification_block_reason: str | None = None
    # evaluate_add_on_eligibility()が返す詳細理由(複数件ありうる)。
    add_on_block_reasons: tuple[str, ...] = ()
    # SENT_AND_RECORDED / SENT_VALIDATION / SENT_LOG_FAILED / SEND_FAILED。
    send_outcome: str | None = None


def build_evaluation_id(batch_id: str, stock_code: str) -> str:
    return f"{batch_id}:{stock_code}"
