"""実際の売買記録サービス(要求仕様3節 transaction_history_service、27〜28節)。

システムの推奨とは独立に、ユーザーが実際に行った売買を記録する。保有銘柄の
管理(portfolio_service/jstock holdings)とは別の関心事であり、ここでは
「推奨の妥当性を事後検証できるようにする」ための履歴記録に徹する
(推奨に従ったかどうか、価格差はいくらだったか等)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from jstock_advisor.domain.entities.enums import AccountType, SkipReason, TransactionType
from jstock_advisor.domain.entities.owner import build_holding_id, normalize_and_validate_owner
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.transaction import SkippedRecommendation, Transaction
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    SkippedRecommendationRepository,
    TransactionRepository,
)

_BUY_TYPES = (TransactionType.BUY, TransactionType.ADDITIONAL_BUY)
_SELL_TYPES = (TransactionType.PARTIAL_SELL, TransactionType.FULL_SELL)


def _reference_price(
    recommendation: Recommendation | None, transaction_type: TransactionType
) -> Decimal | None:
    if recommendation is None:
        return None
    if transaction_type in _BUY_TYPES:
        if recommendation.buy_prices is not None and recommendation.buy_prices.standard is not None:
            return recommendation.buy_prices.standard.price
        return None
    if transaction_type in _SELL_TYPES:
        sp = recommendation.sell_prices
        if sp is None:
            return None
        if transaction_type == TransactionType.PARTIAL_SELL:
            candidate = sp.partial_profit_start_price or sp.recommended_limit_price
        else:
            candidate = (
                sp.full_profit_consideration_price
                or sp.stop_review_price
                or sp.recommended_limit_price
            )
        return candidate.price if candidate is not None else None
    return None


class TransactionHistoryService:
    def __init__(
        self,
        transaction_repository: TransactionRepository | None = None,
        skipped_repository: SkippedRecommendationRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
    ) -> None:
        self._transactions = transaction_repository or TransactionRepository()
        self._skipped = skipped_repository or SkippedRecommendationRepository()
        self._recommendations = recommendation_repository or RecommendationRepository()

    def record_execution(
        self,
        owner: str,
        stock_code: str,
        transaction_type: TransactionType,
        shares: int,
        execution_price: Decimal,
        execution_date: dt.date,
        recommendation_id: str | None = None,
        fee: Decimal = Decimal("0"),
        tax: Decimal = Decimal("0"),
        account_type: AccountType | None = None,
        reason: str | None = None,
        memo: str | None = None,
        now: dt.datetime | None = None,
    ) -> Transaction:
        """build_execution_plan()を呼び出した直後にその場で永続化する薄い
        ラッパー(LINEボタン起点会話型UI・実装プランv2 3節。挙動・戻り値は
        従来と完全に同じ)。"""
        transaction = self.build_execution_plan(
            transaction_id=str(uuid.uuid4()),
            owner=owner,
            stock_code=stock_code,
            transaction_type=transaction_type,
            shares=shares,
            execution_price=execution_price,
            execution_date=execution_date,
            recommendation_id=recommendation_id,
            fee=fee,
            tax=tax,
            account_type=account_type,
            reason=reason,
            memo=memo,
            now=now,
        )
        self._transactions.save(transaction)
        return transaction

    def record_execution_if_absent(
        self,
        transaction_id: str,
        owner: str,
        stock_code: str,
        transaction_type: TransactionType,
        shares: int,
        execution_price: Decimal,
        execution_date: dt.date,
        recommendation_id: str | None = None,
        fee: Decimal = Decimal("0"),
        tax: Decimal = Decimal("0"),
        account_type: AccountType | None = None,
        reason: str | None = None,
        memo: str | None = None,
        now: dt.datetime | None = None,
    ) -> bool:
        """指定したtransaction_idが未登録のときだけ保存する(Issue #61 Phase B3)。

        保存できたらTrue、既に同じtransaction_idが存在すればFalse(何も書かない)。
        CSV取込が「同じ行を何度取り込んでも1回だけ登録される」ことを、
        **永続データそのもの**で保証するために使う。

        `record_execution()`と違い`transaction_id`を呼び出し側が決める。
        書き込みは`save_if_absent`(DynamoDB実装では条件付き書き込み)で原子的に
        行うため、呼び出し側でexists()→save()というcheck-then-actを書かないこと。

        **既存Transactionの内容は上書きしない。** 同じidが既にある場合は
        「取込済み」とみなす。

        ## duplicate fast-path と race safety の分担

        既に同じtransaction_idが保存済みの場合、**計画の構築より前に**Falseを返す。
        `build_execution_plan()`は`recommendation_id`の実在をRecommendation
        リポジトリへ問い合わせるため、これを先に通すと「取込時点では存在したが
        その後に削除された推奨」を参照する再取込がエラーになり、
        「同一バイト列のCSVの再取込は正常なno-opである」という契約に違反する。
        取込済みかどうかの判定を、**現在の可変状態へ依存させない**。

        この事前readには`get_consistent()`(DynamoDB実装ではConsistentRead=True)を
        使う。通常の`get()`は結果整合性読み取りであり、保存済みのTransactionが
        一時的に見えないことがある。その場合に`build_execution_plan()`へ進むと、
        取込後に削除された推奨を参照する再取込がProductionでだけERRORになり、
        上記の契約を満たせない。

        ただし事前readは強い整合性で読んでも**あくまで最適化(fast-path)**であり、
        一意性の権威ではない。事前readと書き込みの間に他プロセスが同じidを保存する
        余地は残るが、最終的な書き込みは`save_if_absent`(DynamoDB実装では条件付き
        書き込み)であるため、その競合は書き込み側で必ず検出されFalseになる。
        **事前readで分岐して無条件saveを行うcheck-then-actにはしない。**
        """
        existing = self._transactions.get_consistent(transaction_id)
        if existing is not None:
            return False

        transaction = self.build_execution_plan(
            transaction_id=transaction_id,
            owner=owner,
            stock_code=stock_code,
            transaction_type=transaction_type,
            shares=shares,
            execution_price=execution_price,
            execution_date=execution_date,
            recommendation_id=recommendation_id,
            fee=fee,
            tax=tax,
            account_type=account_type,
            reason=reason,
            memo=memo,
            now=now,
        )
        return self._transactions.save_if_absent(transaction)

    def build_execution_plan(
        self,
        transaction_id: str,
        owner: str,
        stock_code: str,
        transaction_type: TransactionType,
        shares: int,
        execution_price: Decimal,
        execution_date: dt.date,
        recommendation_id: str | None = None,
        fee: Decimal = Decimal("0"),
        tax: Decimal = Decimal("0"),
        account_type: AccountType | None = None,
        reason: str | None = None,
        memo: str | None = None,
        now: dt.datetime | None = None,
    ) -> Transaction:
        """record_execution()と同じ計算を行うが、一切の永続化を行わず
        「計画」のみを返す(LINEボタン起点会話型UI・実装プランv2 3節)。

        transaction_idを呼び出し側から指定できるのは、LINEボタン起点会話型
        UIがConversationStateのoperation_idをそのままtransaction_idとして
        使い(実装プランv2 3節「決定的ID化」)、TransactWriteItemsが
        クラッシュ後に同一postbackで再試行された場合も同一内容で上書きされる
        だけの安全な冪等操作にするため。

        M3(保有銘柄オーナー機能): Transactionは常にholding-scopeのため、
        owner/holding_idを必ず設定する(holding_idはowner×stock_codeから
        決定的に導出し、外部からは受け取らない)。
        """
        if shares <= 0:
            raise ValueError("shares must be positive")
        if execution_price <= 0:
            raise ValueError("execution_price must be positive")

        recommendation = self._recommendations.get(recommendation_id) if recommendation_id else None
        if recommendation_id and recommendation is None:
            raise ValueError(f"recommendation_id={recommendation_id} が見つかりません")

        reference_price = _reference_price(recommendation, transaction_type)
        price_diff = execution_price - reference_price if reference_price is not None else None

        normalized_owner = normalize_and_validate_owner(owner)
        holding_id = build_holding_id(normalized_owner, stock_code)

        return Transaction(
            transaction_id=transaction_id,
            recommendation_id=recommendation_id,
            stock_code=stock_code,
            transaction_type=transaction_type,
            execution_date=execution_date,
            shares=shares,
            execution_price=execution_price,
            fee=fee,
            tax=tax,
            account_type=account_type,
            followed_recommendation=recommendation_id is not None,
            price_diff_from_recommendation=price_diff,
            reason=reason,
            memo=memo,
            created_at=now or dt.datetime.now(dt.UTC),
            owner=normalized_owner,
            holding_id=holding_id,
        )

    def record_skip(
        self,
        recommendation_id: str,
        skip_reason: SkipReason,
        reason_detail: str | None = None,
        now: dt.datetime | None = None,
    ) -> SkippedRecommendation:
        if self._recommendations.get(recommendation_id) is None:
            raise ValueError(f"recommendation_id={recommendation_id} が見つかりません")

        skipped = SkippedRecommendation(
            recommendation_id=recommendation_id,
            skip_reason=skip_reason,
            reason_detail=reason_detail,
            created_at=now or dt.datetime.now(dt.UTC),
        )
        self._skipped.save(skipped)
        return skipped

    def list_transactions(self, stock_code: str | None = None) -> list[Transaction]:
        if stock_code:
            return self._transactions.list_by_stock(stock_code)
        return self._transactions.list_all()
