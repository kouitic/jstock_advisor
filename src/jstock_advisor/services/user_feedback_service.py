"""ユーザー定性フィードバックサービス(要求仕様47節)。

推奨またはその推奨に基づく売買記録に対して、ユーザーが感じた満足度・納得感等を
記録する。評価ラベル(EvaluationLabel)が機械的な定量評価であるのに対し、
こちらは定性的な補完情報として振り返りレポートに利用する。
"""

from __future__ import annotations

import datetime as dt
import uuid

from jstock_advisor.domain.entities.feedback import UserFeedback
from jstock_advisor.infrastructure.local_repository.feedback_repository import (
    UserFeedbackRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    TransactionRepository,
)

_MIN_SATISFACTION_SCORE = 1
_MAX_SATISFACTION_SCORE = 5


class UserFeedbackService:
    def __init__(
        self,
        feedback_repository: UserFeedbackRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
        transaction_repository: TransactionRepository | None = None,
    ) -> None:
        self._feedback = feedback_repository or UserFeedbackRepository()
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._transactions = transaction_repository or TransactionRepository()

    def submit(
        self,
        recommendation_id: str | None = None,
        transaction_id: str | None = None,
        satisfaction_score: int | None = None,
        risk_explanation_adequate: bool | None = None,
        notification_timing_appropriate: bool | None = None,
        recommended_price_practical: bool | None = None,
        reason_convincing: bool | None = None,
        helpful_for_decision: bool | None = None,
        comment: str | None = None,
        now: dt.datetime | None = None,
    ) -> UserFeedback:
        if (
            satisfaction_score is not None
            and not _MIN_SATISFACTION_SCORE <= satisfaction_score <= _MAX_SATISFACTION_SCORE
        ):
            raise ValueError(
                f"satisfaction_scoreは{_MIN_SATISFACTION_SCORE}〜{_MAX_SATISFACTION_SCORE}"
                "の範囲で指定してください"
            )
        if recommendation_id and self._recommendations.get(recommendation_id) is None:
            raise ValueError(f"recommendation_id={recommendation_id} が見つかりません")
        if transaction_id and self._transactions.get(transaction_id) is None:
            raise ValueError(f"transaction_id={transaction_id} が見つかりません")

        feedback = UserFeedback(
            feedback_id=str(uuid.uuid4()),
            recommendation_id=recommendation_id,
            transaction_id=transaction_id,
            satisfaction_score=satisfaction_score,
            risk_explanation_adequate=risk_explanation_adequate,
            notification_timing_appropriate=notification_timing_appropriate,
            recommended_price_practical=recommended_price_practical,
            reason_convincing=reason_convincing,
            helpful_for_decision=helpful_for_decision,
            comment=comment,
            created_at=now or dt.datetime.now(dt.UTC),
        )
        self._feedback.save(feedback)
        return feedback

    def list_feedback(self, recommendation_id: str | None = None) -> list[UserFeedback]:
        if recommendation_id:
            return self._feedback.list_by_recommendation(recommendation_id)
        return self._feedback.list_all()
