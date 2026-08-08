"""判定精度向上機能Phase A: DecisionSnapshot保存の安全なラッパー。

domain層(decision_snapshot_builder.py)はinfrastructure層に依存しないため、
Repositoryを扱うこの薄いラッパーはservices層に置く。

コードレビュー対応(insert-only保証): DecisionSnapshotは一度保存されたら後から
絶対に上書きしない。同一decision_idの再保存が発生した場合、(a)内容が完全に
同一なら正常な冪等再実行として何もしない、(b)内容が異なるなら既存の記録を
正として保持し、データ不整合の可能性としてWARNINGログを残す(内容不一致を
検知しても既存Recommendation保存・LINE通知は一切ブロックしない)。
"""

from __future__ import annotations

import logging

from jstock_advisor.domain.decision_snapshot_builder import build_decision_snapshot
from jstock_advisor.domain.entities.enums import DecisionType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)

# CloudWatch Logsで固定文字列として検索・メトリクスフィルタ可能にするための
# イベントキー(コードレビュー対応: 自己評価基盤が長期間壊れていても既存の
# Recommendation保存・LINE通知は正常に動き続けるため、専用の監視手段が無いと
# 気づけない)。ログ末尾のkey=value群には秘匿情報・巨大オブジェクトを含めない。
# save_failed(ストレージ障害等の予期しない例外)とconflict(同一decision_idだが
# 内容が異なるデータ不整合)は原因が異なるため、イベントキーを分けて検知する。
DECISION_SNAPSHOT_SAVE_FAILED_EVENT = "decision_snapshot_save_failed"
DECISION_SNAPSHOT_CONFLICT_EVENT = "decision_snapshot_conflict"


def save_decision_snapshot_safely(
    repo: DecisionSnapshotRepository,
    recommendation: Recommendation,
    decision_type: DecisionType,
    logger: logging.Logger,
) -> None:
    """DecisionSnapshotの構築・保存失敗が既存のRecommendation保存・通知フローを
    絶対にブロックしないためのラッパー。例外はWARNINGログのみに留め、呼び出し元へ
    伝播させない。

    insert-only保証: 真正な重複防止の正はrepo.insert_if_absent()の条件付き
    書き込みとする(get→insertのcheck-then-actを排他制御として信用しない)。
    get()は既存値との内容比較のためだけに使う。insert_if_absentがFalseを返した
    場合(既に存在、または並行実行によるrace)は、再度get()して内容を比較する。
    """
    try:
        new_snapshot = build_decision_snapshot(recommendation, decision_type)
        existing = repo.get(new_snapshot.decision_id)
        if existing is None:
            if repo.insert_if_absent(new_snapshot):
                return
            existing = repo.get(new_snapshot.decision_id)
        if existing == new_snapshot:
            # 同一内容の正常な冪等再実行(warning不要)。
            return
        logger.warning(
            "%s stock_code=%s recommendation_id=%s decision_id=%s decision_type=%s",
            DECISION_SNAPSHOT_CONFLICT_EVENT,
            recommendation.stock_code,
            recommendation.recommendation_id,
            new_snapshot.decision_id,
            decision_type.value,
        )
    except Exception:
        logger.warning(
            "%s stock_code=%s recommendation_id=%s decision_type=%s",
            DECISION_SNAPSHOT_SAVE_FAILED_EVENT,
            recommendation.stock_code,
            recommendation.recommendation_id,
            decision_type.value,
            exc_info=True,
        )
