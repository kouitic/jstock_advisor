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

★ サブちゃんレビュー#624 F1対応: 既存の冪等キー(CSV importの
`csv:{sha256}:{row}`等)はすべて内容由来で取り違えが構造的に起こらないが、
本Issueで初めて「人が手で打つ自由入力のキー」を導入したため、同一キーを
別の取引(所有者・銘柄・株数・単価・約定日・BUY/SELL区分のいずれか)へ誤って
使うと、その取引が黙って消える(`already_registered=True`が返るだけで、
実際には何も登録されない)欠陥があった。既存Transactionと要求内容が一致するかを
fast-path・raceのいずれの経路でも検証し、不一致なら
`IdempotencyKeyReusedForDifferentTradeError`で明示的に拒否する
(F6でownerを、F1'でexecution_dateを照合対象へ追加済み)。

★ サブちゃんレビュー#624 F7対応: `register_buy()`はaccount_type(既定GENERAL)を
`build_purchase_write_plan()`(Lot/Holding)へは渡していたが、
`build_execution_plan()`(Transaction)へは渡しておらず、Transaction側だけ
account_type=Noneになっていた。サービスAPIとしてaccount_typeを公開している
以上、同一取引から生成される永続データ間でaccount_typeも一致させるべき
(USER判断: 決定A)であるため、Transactionへも渡すよう修正し、あわせて
「所有者・銘柄・株数・単価・約定日・BUY/SELL区分が同一でaccount_typeだけが
異なる」場合も別取引として扱い、idempotency照合対象へaccount_typeを追加した。
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


def _is_buy_type(transaction_type: TransactionType) -> bool:
    return transaction_type in (TransactionType.BUY, TransactionType.ADDITIONAL_BUY)


class IdempotencyKeyReusedForDifferentTradeError(ValueError):
    """同一`idempotency_key`が、既存の登録内容と異なる取引(所有者・銘柄・
    株数・単価・約定日・BUY/SELL区分のいずれか)に対して指定された
    (Issue #619 サブちゃんレビューF1、F6でowner照合を追加)。

    CSV importの既存冪等キー(`csv:{sha256}:{row}`等)はすべて内容由来で
    取り違えが構造的に起こらないが、本Issueで初めて導入した「人が手で打つ
    自由入力のキー」にはその保証が無い。取り違えたまま黙って進めると、
    対象取引がno-op扱いで消え、保有株数・買付余力が実態より多いまま残る
    (利確判定・買い候補判定・余力不足判定の入力が狂う)ため、明示的に
    検出して拒否する。owner不一致も同じ失敗モード(所有者Aの1回目登録に
    使ったキーを所有者Bの2回目登録へ誤って使い回すと、Bの取引が黙って
    消える)であるため同様に扱う。
    """

    def __init__(
        self,
        existing: Transaction,
        *,
        owner: str,
        stock_code: str,
        shares: int,
        price: Decimal,
        trade_date: dt.date,
        is_buy: bool,
        account_type: AccountType | None,
    ) -> None:
        super().__init__(
            f"idempotency-key={existing.transaction_id}は既に別の取引"
            f"(owner={existing.owner} {existing.stock_code} {existing.shares}株 "
            f"@{existing.execution_price}円 {existing.execution_date} "
            f"[{existing.transaction_type.value}] account_type={existing.account_type})"
            f"として登録済みのため、今回の取引(owner={owner} {stock_code} {shares}株 "
            f"@{price}円 {trade_date} [{'BUY' if is_buy else 'SELL'}] "
            f"account_type={account_type})には使用できません。"
            "別のidempotency-keyを指定してください。"
        )


def _verify_idempotency_key_matches(
    existing: Transaction,
    *,
    owner: str,
    stock_code: str,
    shares: int,
    price: Decimal,
    trade_date: dt.date,
    is_buy: bool,
    account_type: AccountType | None = None,
) -> None:
    """既存Transactionと要求内容を照合する(Issue #619 サブちゃんレビュー
    F1・F1'・F6・F7対応)。

    所有者(`owner`)も照合対象に含める(サブちゃんレビュー#624 F6)。
    Transactionはowner-scopeであり、CLIには`--owner`があるため、
    銘柄・株数・単価・約定日・BUY/SELL区分が全て同一でownerだけが異なる
    場合も、F1と同じ失敗モード(所有者Bの取引が黙って消える)が起こる。

    約定日(`execution_date`)も照合対象に含める。同一キーで銘柄・株数・
    単価・区分が全て一致し約定日だけが異なる場合も、日付違いの別取引が
    黙って消える(F1と同じ失敗モード)ため区別する必要があるという判断
    (サブちゃんレビュー#624 F1'。1回目が実は成功していたのに翌日
    `--date`省略で再実行すると拒否され、別キーで打ち直すと二重登録に
    なりうるtrade-offは認識したうえで、「exit 0で登録済みと報告した
    まま取引が消える」実害の方が大きいためfail-closedを優先する)。

    account_type(SELLは概念が無いため常にNone)も照合対象に含める
    (サブちゃんレビュー#624 F7。USER判断: 決定A。所有者・銘柄・株数・
    単価・約定日・BUY/SELL区分が同一でも、口座種別(特定/NISA/一般)が
    異なれば税務上別の取引として扱うべきであり、他の項目と同じ
    fail-closed方針を適用する)。
    """
    mismatch = (
        existing.owner != owner
        or existing.stock_code != stock_code
        or existing.shares != shares
        or existing.execution_price != price
        or existing.execution_date != trade_date
        or _is_buy_type(existing.transaction_type) != is_buy
        or existing.account_type != account_type
    )
    if mismatch:
        raise IdempotencyKeyReusedForDifferentTradeError(
            existing,
            owner=owner,
            stock_code=stock_code,
            shares=shares,
            price=price,
            trade_date=trade_date,
            is_buy=is_buy,
            account_type=account_type,
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
        fast_path = self._fast_path_if_already_registered(
            owner,
            stock_code,
            idempotency_key,
            shares=shares,
            price=price,
            trade_date=trade_date,
            is_buy=True,
            account_type=account_type,
        )
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
            account_type=account_type,
            now=now,
        )
        available_cash_put = self._available_cash.build_trade_update_plan(
            owner, -(price * shares), now
        )
        return self._commit_locally(
            owner=owner,
            stock_code=stock_code,
            shares=shares,
            price=price,
            trade_date=trade_date,
            is_buy=True,
            account_type=account_type,
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
        fast_path = self._fast_path_if_already_registered(
            owner,
            stock_code,
            idempotency_key,
            shares=shares,
            price=price,
            trade_date=trade_date,
            is_buy=False,
            account_type=None,
        )
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
            shares=shares,
            price=price,
            trade_date=trade_date,
            is_buy=False,
            account_type=None,
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
        self,
        owner: str,
        stock_code: str,
        idempotency_key: str,
        *,
        shares: int,
        price: Decimal,
        trade_date: dt.date,
        is_buy: bool,
        account_type: AccountType | None = None,
    ) -> TradeRegistrationResult | None:
        existing = self._transaction_repo.get_consistent(idempotency_key)
        if existing is None:
            return None
        _verify_idempotency_key_matches(
            existing,
            owner=owner,
            stock_code=stock_code,
            shares=shares,
            price=price,
            trade_date=trade_date,
            is_buy=is_buy,
            account_type=account_type,
        )
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
        shares: int,
        price: Decimal,
        trade_date: dt.date,
        is_buy: bool,
        account_type: AccountType | None = None,
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
            #
            # 申し送り(サブちゃんレビュー#624、今回対応不要): `get()`
            # (結果整合性読み取り)が空を返した場合`transaction`(自分自身)へ
            # フォールバックするため、その場合`_verify_idempotency_key_matches()`
            # は自分自身と照合することになりguardを素通りする。CLIはローカル
            # storeで即時読めるため現状は到達しないが、将来Lambda呼び出し元を
            # 追加する場合は`get_consistent()`へ揃えると同じ保証になる。
            raced_existing = self._transaction_repo.get(idempotency_key) or transaction
            _verify_idempotency_key_matches(
                raced_existing,
                owner=owner,
                stock_code=stock_code,
                shares=shares,
                price=price,
                trade_date=trade_date,
                is_buy=is_buy,
                account_type=account_type,
            )
            return TradeRegistrationResult(
                transaction=raced_existing,
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
