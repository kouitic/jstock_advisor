"""CLI経由の通常売買登録(BUY/SELL)を、Holding/Transaction/AvailableCashを
整合した1つの業務更新単位として書き込む(Issue #619、#128 A3-CLI。
#590/#591のLINE経路のCLI版follow-up)。

LINE経路(`infrastructure/aws/conversation_commit.py`)はDynamoDB
TransactWriteItemsで原子性を保証するが、CLIは常に`running_on_lambda()`が
Falseのローカル実行のみ(Lambda呼び出し元は現時点で存在しない)であるため、
本サービスは`PortfolioService._apply_holding_replacement_locally()`と同型の
「適用前スナップショット→例外時ロールバック」パターンを、Transaction/Lot/
Holding/AvailableCashの4対象へ拡張して実装する。★ Lambda環境向けの
TransactWriteItems経路(#619 origin issue〔#590〕のPhase A設計が想定していた
`infrastructure/aws/trade_registration_commit.py`)は、実際の呼び出し元が
存在しないため実装しない(YAGNI。将来Lambda呼び出し元が必要になった場合に
追加することを妨げない)。

idempotency: `--idempotency-key`を明示指定した場合のみ冪等(D2)。
Transactionへの`save_if_absent()`(Issue #61 Phase B3の既存プリミティブを
再利用)を一意性の権威とし、事前の`get_consistent()`はfast-pathの最適化に
過ぎない(`record_execution_if_absent()`と同じ規約。取込済みかどうかの
判定を現在の可変状態に依存させない)。BUY側のPurchaseLotは
`idempotency_key`をそのまま`lot_id`として渡すことで、CSV importが確立した
「同一lot_idの再適用は安全」契約(Issue #61 Phase B1)にそのまま乗る
(新しい冪等機構を作らない)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot
from jstock_advisor.domain.entities.owner import build_holding_id, normalize_and_validate_owner
from jstock_advisor.domain.entities.transaction import Transaction
from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
    AvailableCashRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    TransactionRepository,
)
from jstock_advisor.services.available_cash_service import AvailableCashService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.transaction_history_service import TransactionHistoryService
from jstock_advisor.services.write_plan import (
    ConditionalDelete,
    ConditionalPut,
    apply_conditional_delete,
    apply_conditional_put,
)


@dataclass(frozen=True)
class TradeRegistrationResult:
    """`register_buy()`/`register_sell()`の戻り値。

    `already_registered=True`は、同一`idempotency_key`が既に登録済み
    (今回の呼び出しでは何も書き込んでいない)ことを示す。
    """

    transaction: Transaction
    resulting_holding: Holding | None
    already_registered: bool


class TradeRegistrationService:
    def __init__(
        self,
        portfolio_service: PortfolioService | None = None,
        transaction_history_service: TransactionHistoryService | None = None,
        available_cash_service: AvailableCashService | None = None,
        transaction_repository: TransactionRepository | None = None,
        lot_repository: PurchaseLotRepository | None = None,
        holding_repository: HoldingRepository | None = None,
        available_cash_repository: AvailableCashRepository | None = None,
    ) -> None:
        self._portfolio = portfolio_service or PortfolioService()
        self._transactions = transaction_history_service or TransactionHistoryService()
        self._available_cash = available_cash_service or AvailableCashService()
        self._transaction_repo = transaction_repository or TransactionRepository()
        self._lot_repo = lot_repository or PurchaseLotRepository()
        self._holding_repo = holding_repository or HoldingRepository()
        self._available_cash_repo = available_cash_repository or AvailableCashRepository()

    def register_buy(
        self,
        owner: str,
        stock_code: str,
        shares: int,
        price: Decimal,
        trade_date: dt.date,
        idempotency_key: str,
        now: dt.datetime,
        account_type: AccountType = AccountType.GENERAL,
    ) -> TradeRegistrationResult:
        owner = normalize_and_validate_owner(owner)
        fast_path = self._fast_path_if_already_registered(owner, stock_code, idempotency_key)
        if fast_path is not None:
            return fast_path

        existing_holding = self._portfolio.get_holding(owner, stock_code)
        transaction_type = (
            TransactionType.ADDITIONAL_BUY
            if existing_holding is not None
            else TransactionType.BUY
        )
        plan = self._portfolio.build_purchase_write_plan(
            owner=owner,
            stock_code=stock_code,
            stock_name=None,
            shares=shares,
            purchase_price=price,
            purchase_date=trade_date,
            account_type=account_type,
            now=now,
            lot_id=idempotency_key,
        )
        transaction = self._transactions.build_execution_plan(
            transaction_id=idempotency_key,
            owner=owner,
            stock_code=stock_code,
            transaction_type=transaction_type,
            shares=shares,
            execution_price=price,
            execution_date=trade_date,
            now=now,
        )
        available_cash_put = self._available_cash.build_trade_update_plan(
            owner, -(price * shares), now
        )
        return self._commit_locally(
            owner=owner,
            stock_code=stock_code,
            idempotency_key=idempotency_key,
            transaction=transaction,
            lot_puts=[plan.lot_put],
            lot_deletes=[],
            holding_put=plan.holding_put,
            holding_delete=None,
            available_cash_put=available_cash_put,
            resulting_holding=plan.resulting_holding,
            holding_id=build_holding_id(owner, stock_code),
        )

    def register_sell(
        self,
        owner: str,
        stock_code: str,
        shares: int,
        price: Decimal,
        trade_date: dt.date,
        idempotency_key: str,
        now: dt.datetime,
    ) -> TradeRegistrationResult:
        owner = normalize_and_validate_owner(owner)
        fast_path = self._fast_path_if_already_registered(owner, stock_code, idempotency_key)
        if fast_path is not None:
            return fast_path

        existing_holding = self._portfolio.get_holding(owner, stock_code)
        transaction_type = (
            TransactionType.FULL_SELL
            if existing_holding is not None and shares >= existing_holding.shares
            else TransactionType.PARTIAL_SELL
        )
        plan = self._portfolio.build_sale_write_plan(owner, stock_code, shares, now=now)
        transaction = self._transactions.build_execution_plan(
            transaction_id=idempotency_key,
            owner=owner,
            stock_code=stock_code,
            transaction_type=transaction_type,
            shares=shares,
            execution_price=price,
            execution_date=trade_date,
            now=now,
        )
        available_cash_put = self._available_cash.build_trade_update_plan(
            owner, price * shares, now
        )
        return self._commit_locally(
            owner=owner,
            stock_code=stock_code,
            idempotency_key=idempotency_key,
            transaction=transaction,
            lot_puts=plan.lot_puts,
            lot_deletes=plan.lot_deletes,
            holding_put=plan.holding_put,
            holding_delete=plan.holding_delete,
            available_cash_put=available_cash_put,
            resulting_holding=plan.resulting_holding,
            holding_id=build_holding_id(owner, stock_code),
        )

    def _fast_path_if_already_registered(
        self, owner: str, stock_code: str, idempotency_key: str
    ) -> TradeRegistrationResult | None:
        existing = self._transaction_repo.get_consistent(idempotency_key)
        if existing is None:
            return None
        return TradeRegistrationResult(
            transaction=existing,
            resulting_holding=self._portfolio.get_holding(owner, stock_code),
            already_registered=True,
        )

    def _commit_locally(
        self,
        *,
        owner: str,
        stock_code: str,
        idempotency_key: str,
        transaction: Transaction,
        lot_puts: list[ConditionalPut],
        lot_deletes: list[ConditionalDelete],
        holding_put: ConditionalPut | None,
        holding_delete: ConditionalDelete | None,
        available_cash_put: ConditionalPut,
        resulting_holding: Holding | None,
        holding_id: str,
    ) -> TradeRegistrationResult:
        # ロールバック用に適用前の状態をスナップショットする(#619 origin
        # issue〔#590〕2-1節と同じ理由: 適用途中の失敗で部分適用状態を
        # 残さないため)。
        pre_lot_raw: dict[str, str | None] = {}
        for put in lot_puts:
            lot_id = str(getattr(put.model, put.id_field))
            pre_lot_raw[lot_id] = self._lot_repo.get_raw_data(lot_id)
        for delete in lot_deletes:
            pre_lot_raw[delete.id_value] = self._lot_repo.get_raw_data(delete.id_value)
        pre_holding_raw = self._holding_repo.get_raw_data(holding_id)
        pre_cash_raw = self._available_cash_repo.get_raw(owner)

        if not self._transaction_repo.save_if_absent(transaction):
            # 他プロセスが同一idempotency_keyで先に登録済み(race)。ここまで
            # 一切の書き込みを行っていないため、そのままno-opとして返す
            # (`save_if_absent()`はDynamoDB実装ではattribute_not_exists条件付き
            # 書き込みで原子的にこれを保証するため、check-then-actにならない)。
            return TradeRegistrationResult(
                transaction=self._transaction_repo.get(idempotency_key) or transaction,
                resulting_holding=self._portfolio.get_holding(owner, stock_code),
                already_registered=True,
            )
        try:
            for delete in lot_deletes:
                apply_conditional_delete(self._lot_repo, delete)
            for put in lot_puts:
                apply_conditional_put(self._lot_repo, put)
            if holding_put is not None:
                apply_conditional_put(self._holding_repo, holding_put)
            if holding_delete is not None:
                apply_conditional_delete(self._holding_repo, holding_delete)
            # AvailableCashRepositoryは_CasCapableRepository Protocol全体
            # (insert_if_absent等)を実装しない(#591の設計どおり、新規owner
            # の暗黙initializeを許さないため)。build_trade_update_plan()の
            # expected_dataは常に非None(未登録ownerはそれ以前に
            # AvailableCashNotRegisteredErrorで拒否済み)であるため、
            # apply_conditional_put()は常にreplace_if_raw_matches()分岐のみ
            # 使う(insert_if_absent()分岐には到達しない)。
            apply_conditional_put(self._available_cash_repo, available_cash_put)  # type: ignore[arg-type]
        except Exception:
            self._transaction_repo.delete(idempotency_key)
            for lot_id, raw in pre_lot_raw.items():
                self._restore_lot(self._lot_repo, lot_id, raw)
            self._restore_holding(self._holding_repo, holding_id, pre_holding_raw)
            self._restore_available_cash(owner, pre_cash_raw)
            raise

        return TradeRegistrationResult(
            transaction=transaction,
            resulting_holding=resulting_holding,
            already_registered=False,
        )

    @staticmethod
    def _restore_lot(repo: PurchaseLotRepository, item_id: str, raw: str | None) -> None:
        if raw is None:
            repo.delete(item_id)
        else:
            repo.upsert(PurchaseLot.model_validate_json(raw))

    @staticmethod
    def _restore_holding(repo: HoldingRepository, item_id: str, raw: str | None) -> None:
        if raw is None:
            repo.delete(item_id)
        else:
            repo.upsert(Holding.model_validate_json(raw))

    def _restore_available_cash(self, owner: str, raw: str | None) -> None:
        """AvailableCashは`_commit_locally()`のtry blockで**最後**に適用される
        ため、その適用自体が失敗した場合(CAS不成立)は何も書き込まれておらず、
        本メソッドは事実上no-opになる(restoreする対象がまだ存在しない)。
        それより前のLot/Holding書き込みが失敗した場合はcashへ一切到達しない。
        呼び出し順が将来変わった場合の安全網として、defense-in-depthのまま残す
        (#591のentity側validatorと同じ位置づけ)。"""
        if raw is None:
            # 未登録ownerへのTRADE_UPDATEはbuild_trade_update_plan()が事前に
            # AvailableCashNotRegisteredErrorで拒否するため到達しない。
            return
        current_raw = self._available_cash_repo.get_raw(owner)
        if current_raw is None:
            return
        restored = AvailableCash.model_validate_json(raw)
        self._available_cash_repo.replace_if_raw_matches(owner, current_raw, restored)
