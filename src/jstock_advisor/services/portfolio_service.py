"""保有銘柄管理サービス(要求仕様3節 portfolio_service、4節・5節)。

PurchaseLotを正データとし、Holdingは平均購入単価等を再計算したキャッシュとして
upsertする(要求仕様5節: 平均購入単価 = 各ロットの購入金額合計 ÷ 保有株数合計)。

M3(保有銘柄オーナー機能アプリ切替): Holdingを一意に識別する単位は
stock_codeではなくholding_id(= owner + "#" + stock_code、domain/entities/
owner.py参照)。本サービスの全公開APIはownerを必須引数とし、holding_idは
外部から直接受け取らず、常にowner×stock_codeから決定的に導出する。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot, summarize_lots
from jstock_advisor.domain.entities.owner import build_holding_id, normalize_and_validate_owner
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.services.corporate_action_service import CorporateActionService
from jstock_advisor.services.write_plan import (
    ConditionalDelete,
    ConditionalPut,
    PurchaseWritePlan,
    SaleWritePlan,
)


class PortfolioService:
    def __init__(
        self,
        holding_repository: HoldingRepository | None = None,
        lot_repository: PurchaseLotRepository | None = None,
        corporate_action_service: CorporateActionService | None = None,
    ) -> None:
        """corporate_action_serviceを渡すと、shares/average_purchase_priceを
        購入日時点からの累積分割係数で調整して集計する(要求仕様2節)。
        渡さない場合(既定)は従来通り無調整で集計する — 後方互換のための既定値。
        """
        self._holdings = holding_repository or HoldingRepository()
        self._lots = lot_repository or PurchaseLotRepository()
        self._corporate_action = corporate_action_service

    def list_holdings(self) -> list[Holding]:
        """全owner・全銘柄のHoldingを返す(owner横断)。"""
        return self._holdings.list_all()

    def get_holding(self, owner: str, stock_code: str) -> Holding | None:
        holding_id = build_holding_id(normalize_and_validate_owner(owner), stock_code)
        return self._holdings.get(holding_id)

    def list_lots(self, owner: str, stock_code: str) -> list[PurchaseLot]:
        holding_id = build_holding_id(normalize_and_validate_owner(owner), stock_code)
        return self._lots.list_by_holding(holding_id)

    def register_purchase(
        self,
        owner: str,
        stock_code: str,
        stock_name: str | None,
        shares: int,
        purchase_price: Decimal,
        purchase_date: dt.date,
        account_type: AccountType,
        fee: Decimal = Decimal("0"),
        investment_purpose: str | None = None,
        sell_policy: str | None = None,
        profit_target_rate: float | None = None,
        memo: str | None = None,
    ) -> Holding:
        """build_purchase_write_plan()を呼び出した直後にその場で適用する薄い
        ラッパー(LINEボタン起点会話型UI・実装プランv2 3節。挙動・戻り値は
        従来と完全に同じ)。"""
        plan = self.build_purchase_write_plan(
            owner,
            stock_code,
            stock_name,
            shares,
            purchase_price,
            purchase_date,
            account_type,
            fee=fee,
            investment_purpose=investment_purpose,
            sell_policy=sell_policy,
            profit_target_rate=profit_target_rate,
            memo=memo,
        )
        self._lots.upsert(plan.lot_put.model)  # type: ignore[arg-type]
        self._holdings.upsert(plan.holding_put.model)  # type: ignore[arg-type]
        return plan.resulting_holding

    def build_purchase_write_plan(
        self,
        owner: str,
        stock_code: str,
        stock_name: str | None,
        shares: int,
        purchase_price: Decimal,
        purchase_date: dt.date,
        account_type: AccountType,
        fee: Decimal = Decimal("0"),
        investment_purpose: str | None = None,
        sell_policy: str | None = None,
        profit_target_rate: float | None = None,
        memo: str | None = None,
        now: dt.datetime | None = None,
    ) -> PurchaseWritePlan:
        """register_purchase()と同じ計算を行うが、一切の永続化を行わず
        「計画」のみを返す(LINEボタン起点会話型UI・実装プランv2 3節・
        追加条件1)。TransactWriteItemsのConditionExpression用に、
        計画構築時点で読み取った既存Holdingの生data文字列(新規追加なら
        None)をexpected_dataへ含める。
        """
        now = now or dt.datetime.now(dt.UTC)
        normalized_owner = normalize_and_validate_owner(owner)
        holding_id = build_holding_id(normalized_owner, stock_code)
        lot = PurchaseLot(
            lot_id=str(uuid.uuid4()),
            owner=normalized_owner,
            holding_id=holding_id,
            stock_code=stock_code,
            purchase_date=purchase_date,
            shares=shares,
            purchase_price=purchase_price,
            fee=fee,
            account_type=account_type,
        )
        existing_lots = self._lots.list_by_holding(holding_id)
        existing_holding = self._holdings.get(holding_id)
        existing_holding_raw = self._holdings.get_raw_data(holding_id)
        holding = self._compute_holding(
            normalized_owner,
            holding_id,
            stock_code,
            [*existing_lots, lot],
            existing_holding,
            now,
            stock_name=stock_name,
            account_type=account_type,
            investment_purpose=investment_purpose,
            sell_policy=sell_policy,
            profit_target_rate=profit_target_rate,
            memo=memo,
        )
        return PurchaseWritePlan(
            lot_put=ConditionalPut(model=lot, id_field="lot_id", expected_data=None),
            holding_put=ConditionalPut(
                model=holding, id_field="holding_id", expected_data=existing_holding_raw
            ),
            resulting_holding=holding,
        )

    def _compute_holding(
        self,
        owner: str,
        holding_id: str,
        stock_code: str,
        lots: list[PurchaseLot],
        existing: Holding | None,
        now: dt.datetime,
        *,
        stock_name: str | None = None,
        account_type: AccountType | None = None,
        investment_purpose: str | None = None,
        sell_policy: str | None = None,
        profit_target_rate: float | None = None,
        memo: str | None = None,
        is_sale: bool = False,
    ) -> Holding:
        """ロット一覧からHoldingを再計算する純粋な計算部分(永続化を行わない)。
        _recompute_holding()・build_purchase_write_plan()・build_sale_write_plan()
        から共通で呼ばれる(実装プランv2: 計画構築と実際の適用を分離するための
        リファクタ。計算内容は従来のprivate `_recompute_holding()`と同一)。

        is_sale=True(build_sale_write_plan()からのみ指定、コードレビュー対応
        2026-08)の場合、last_sale_dateを本呼び出しの評価日(JST)へ更新する。
        購入・メタ情報更新では更新しない(既存値を保持する)。
        """
        if not lots:
            raise ValueError(f"holding_id{holding_id}の購入ロットがありません")

        _, _, total_amount, first_date, last_date = summarize_lots(lots)

        adjustment_basis_date: dt.date | None = None
        if self._corporate_action is not None:
            total_shares, avg_price = self._split_adjusted_summary(stock_code, lots, now)
            adjustment_basis_date = evaluation_date_jst(now)
        else:
            total_shares, avg_price, _, _, _ = summarize_lots(lots)

        return Holding(
            owner=owner,
            holding_id=holding_id,
            stock_code=stock_code,
            stock_name=stock_name or (existing.stock_name if existing else stock_code),
            market_segment=existing.market_segment if existing else None,
            industry=existing.industry if existing else None,
            shares=total_shares,
            average_purchase_price=avg_price,
            total_purchase_amount=total_amount,
            first_purchase_date=first_date,
            last_purchase_date=last_date,
            shares_and_price_adjustment_basis_date=adjustment_basis_date,
            last_sale_date=(
                evaluation_date_jst(now)
                if is_sale
                else (existing.last_sale_date if existing else None)
            ),
            account_type=account_type
            or (existing.account_type if existing else AccountType.GENERAL),
            investment_purpose=investment_purpose
            or (existing.investment_purpose if existing else None),
            sell_policy=sell_policy or (existing.sell_policy if existing else None),
            cumulative_dividend_received=(
                existing.cumulative_dividend_received if existing else Decimal("0")
            ),
            cumulative_benefit_value_received=(
                existing.cumulative_benefit_value_received if existing else Decimal("0")
            ),
            profit_target_price=existing.profit_target_price if existing else None,
            profit_target_rate=(
                profit_target_rate
                if profit_target_rate is not None
                else (existing.profit_target_rate if existing else None)
            ),
            memo=memo or (existing.memo if existing else None),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )

    def _recompute_holding(
        self,
        owner: str,
        stock_code: str,
        *,
        stock_name: str | None = None,
        account_type: AccountType | None = None,
        investment_purpose: str | None = None,
        sell_policy: str | None = None,
        profit_target_rate: float | None = None,
        memo: str | None = None,
    ) -> Holding:
        normalized_owner = normalize_and_validate_owner(owner)
        holding_id = build_holding_id(normalized_owner, stock_code)
        lots = self._lots.list_by_holding(holding_id)
        existing = self._holdings.get(holding_id)
        now = dt.datetime.now(dt.UTC)
        holding = self._compute_holding(
            normalized_owner,
            holding_id,
            stock_code,
            lots,
            existing,
            now,
            stock_name=stock_name,
            account_type=account_type,
            investment_purpose=investment_purpose,
            sell_policy=sell_policy,
            profit_target_rate=profit_target_rate,
            memo=memo,
        )
        self._holdings.upsert(holding)
        return holding

    def _split_adjusted_summary(
        self, stock_code: str, lots: list[PurchaseLot], now: dt.datetime
    ) -> tuple[int, Decimal]:
        """各ロットの購入日時点からの累積分割係数で保有株数・平均取得単価を調整する。

        購入金額(total_purchase_amount、支出した円の総額)は分割の影響を受けないため
        調整不要(summarize_lotsの値をそのまま使う)。株数のみraw*factor、
        平均取得単価は「調整後総株数」で購入総額を割り直すことで導出する。
        """
        assert self._corporate_action is not None
        basis_date = evaluation_date_jst(now)
        source = DataSourceReference(provider="corporate_action_service", fetched_at=now)
        events = self._corporate_action.get_effective_events(
            stock_code, min(lot.purchase_date for lot in lots)
        )
        total_adjusted_shares = 0
        total_amount = Decimal("0")
        for lot in lots:
            adjusted = self._corporate_action.adjust_shares(
                lot.shares, stock_code, lot.purchase_date, basis_date, source, events=events
            )
            total_adjusted_shares += adjusted.adjusted_value
            total_amount += lot.amount()
        if total_adjusted_shares <= 0:
            raise ValueError(f"銘柄コード{stock_code}の調整後保有株数が0以下です")
        average_price = total_amount / total_adjusted_shares
        return total_adjusted_shares, average_price

    def recompute_holding(self, owner: str, stock_code: str) -> Holding:
        """既存メタ情報を保持したまま、ロットからshares/average_purchase_priceを
        再計算する(企業行動調整サービスを注入している場合は分割調整も適用)。"""
        return self._recompute_holding(owner, stock_code)

    def update_holding_meta(self, owner: str, stock_code: str, **fields: Any) -> Holding:
        """stock_name/market_segment/industry/investment_purpose/sell_policy/
        cumulative_dividend_received/cumulative_benefit_value_received/
        profit_target_price/profit_target_rate/memo 等、ロットから導出されない
        項目を更新する。"""
        holding_id = build_holding_id(normalize_and_validate_owner(owner), stock_code)
        existing = self._holdings.get(holding_id)
        if existing is None:
            raise ValueError(f"holding_id{holding_id}の保有銘柄が見つかりません")
        merged = {
            **existing.model_dump(mode="python"),
            **fields,
            "updated_at": dt.datetime.now(dt.UTC),
        }
        updated = Holding.model_validate(merged)
        self._holdings.upsert(updated)
        return updated

    def sell_shares(
        self, owner: str, stock_code: str, shares: int, now: dt.datetime | None = None
    ) -> Holding | None:
        """build_sale_write_plan()を呼び出した直後にその場で適用する薄い
        ラッパー(LINEボタン起点会話型UI・実装プランv2 3節。挙動・戻り値は
        従来と完全に同じ)。FIFO(購入日が古いロット順)で消費し、保有株数を
        減らす。全ロットを消費した場合はHoldingも削除しNoneを返す。

        now(省略時はbuild_sale_write_plan()側でdt.datetime.now(dt.UTC)へ
        フォールバック)は、last_sale_date算出(evaluation_date_jst(now))に
        使われる。
        """
        plan = self.build_sale_write_plan(owner, stock_code, shares, now=now)
        for lot_delete in plan.lot_deletes:
            self._lots.delete(lot_delete.id_value)
        for lot_put in plan.lot_puts:
            self._lots.upsert(lot_put.model)  # type: ignore[arg-type]
        if plan.holding_delete is not None:
            self._holdings.delete(plan.holding_delete.id_value)
            return None
        if plan.holding_put is not None:
            self._holdings.upsert(plan.holding_put.model)  # type: ignore[arg-type]
            return plan.resulting_holding
        # 全部売却だが、そもそもHoldingが存在しなかった場合(データ不整合時の
        # 安全側フォールバック)。何も削除操作を行わずNoneを返す。
        return None

    def build_sale_write_plan(
        self, owner: str, stock_code: str, shares: int, now: dt.datetime | None = None
    ) -> SaleWritePlan:
        """sell_shares()と同じ計算(FIFO消費)を行うが、一切の永続化を行わず
        「計画」のみを返す(LINEボタン起点会話型UI・実装プランv2 3節・
        追加条件1)。消費対象ロットのDelete/Update・再計算後Holdingの
        Put/Deleteそれぞれに、計画構築時点で読み取った既存アイテムの生data
        文字列(楽観ロック用のexpected_data)を含める。

        FIFO消費はowner別のholding_idに紐づくロットのみを対象とする
        (list_by_holding()。他ownerの同一stock_codeロットには一切触れない)。
        """
        now = now or dt.datetime.now(dt.UTC)
        normalized_owner = normalize_and_validate_owner(owner)
        holding_id = build_holding_id(normalized_owner, stock_code)
        lots = sorted(self._lots.list_by_holding(holding_id), key=lambda lot: lot.purchase_date)
        if not lots:
            raise ValueError(f"holding_id{holding_id}の購入ロットがありません")

        total_held = sum(lot.shares for lot in lots)
        if shares > total_held:
            raise ValueError(f"保有株数({total_held}株)を超える売却はできません")

        remaining = shares
        lot_deletes: list[ConditionalDelete] = []
        lot_puts: list[ConditionalPut] = []
        remaining_lots: list[PurchaseLot] = []
        for lot in lots:
            if remaining <= 0:
                remaining_lots.append(lot)
                continue
            raw = self._lots.get_raw_data(lot.lot_id)
            if raw is None:
                raise ValueError(f"ロットID{lot.lot_id}のデータ取得に失敗しました")
            if lot.shares <= remaining:
                lot_deletes.append(
                    ConditionalDelete(id_value=lot.lot_id, id_field="lot_id", expected_data=raw)
                )
                remaining -= lot.shares
            else:
                updated_lot = lot.model_copy(update={"shares": lot.shares - remaining})
                lot_puts.append(
                    ConditionalPut(model=updated_lot, id_field="lot_id", expected_data=raw)
                )
                remaining_lots.append(updated_lot)
                remaining = 0

        existing_holding = self._holdings.get(holding_id)
        existing_holding_raw = self._holdings.get_raw_data(holding_id)

        if not remaining_lots:
            holding_delete = (
                ConditionalDelete(
                    id_value=holding_id, id_field="holding_id", expected_data=existing_holding_raw
                )
                if existing_holding_raw is not None
                else None
            )
            return SaleWritePlan(
                lot_deletes=lot_deletes,
                lot_puts=lot_puts,
                holding_put=None,
                holding_delete=holding_delete,
                resulting_holding=None,
            )

        holding = self._compute_holding(
            normalized_owner,
            holding_id,
            stock_code,
            remaining_lots,
            existing_holding,
            now,
            is_sale=True,
        )
        return SaleWritePlan(
            lot_deletes=lot_deletes,
            lot_puts=lot_puts,
            holding_put=ConditionalPut(
                model=holding, id_field="holding_id", expected_data=existing_holding_raw
            ),
            holding_delete=None,
            resulting_holding=holding,
        )

    def delete_lot(self, owner: str, stock_code: str, lot_id: str) -> Holding | None:
        holding_id = build_holding_id(normalize_and_validate_owner(owner), stock_code)
        lot = self._lots.get(lot_id)
        if lot is None or lot.holding_id != holding_id:
            raise ValueError(f"ロットID{lot_id}が見つかりません")
        self._lots.delete(lot_id)
        remaining = self._lots.list_by_holding(holding_id)
        if not remaining:
            self._holdings.delete(holding_id)
            return None
        return self._recompute_holding(owner, stock_code)

    def delete_holding(self, owner: str, stock_code: str) -> bool:
        holding_id = build_holding_id(normalize_and_validate_owner(owner), stock_code)
        self._lots.delete_by_holding(holding_id)
        return self._holdings.delete(holding_id)
