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
from jstock_advisor.infrastructure.collection_store import running_on_lambda
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.services.corporate_action_service import CorporateActionService
from jstock_advisor.services.write_plan import (
    ConditionalDelete,
    ConditionalPut,
    HoldingReplacementPlan,
    PurchaseWritePlan,
    SaleWritePlan,
)

PURCHASE_PRICE_MUST_BE_POSITIVE = "購入単価は0より大きい値を指定してください"

MAX_LOTS_PER_HOLDING = 90
"""1保有あたりのロット数の**業務上の上限**(supported domain、Issue #61 Phase B2)。

保有の原子的な置換・削除は、既存ロットの削除・Holdingの削除・新ロットのPut・
新HoldingのPutを**単一のDynamoDB TransactWriteItemsへ入れる**ことで実現する。
1リクエストあたりの書き込み項目数は次のとおり。

    overwrite(置換) : 既存ロット削除 N + 新ロットPut 1 + HoldingPut 1 = N + 2
    削除のみ         : 既存ロット削除 N + Holding削除 1                = N + 1

既存Holdingは「削除してから作り直す」のではなく、既存の生JSONを条件とする
1回のConditionalPutで置換する(DynamoDBは同一アイテムへの複数アクションを
許可しないため)。

DynamoDBの物理上限は100項目(`dynamodb_transaction.MAX_TRANSACT_ITEMS`)であり
理論上はさらに多くのロットを扱えるが、条件式の追加・将来の項目追加に対する
余裕を残すため、
**業務上の上限は90**とする。物理上限と業務上限は意図的に別の定数である
(物理上限が変わっても業務契約は変わらない)。

上限を超える保有は、**変更を一切行う前に**`HoldingLotLimitExceededError`で
拒否する。部分適用・非トランザクション経路へのフォールバック・
先頭90件だけの削除・無音の切り詰めはいずれも行わない。リトライしても解消しない
性質のため、非リトライ対象として扱う。
"""


class HoldingLotLimitExceededError(ValueError):
    """1保有のロット数が`MAX_LOTS_PER_HOLDING`を超えており、原子的な置換・削除を
    実行できない(Issue #61 Phase B2)。

    **この例外が送出された時点で、HoldingもPurchaseLotも一切変更されていない**
    (mutation開始前の事前検証で送出する)。リトライでは解消しないため、
    呼び出し側は再試行せず運用者へ提示すること。
    """


def _validate_purchase_price(purchase_price: Decimal) -> None:
    """新規購入ロットの取得単価が正であることを保証する(Issue #75 Phase B2)。

    0以下の取得単価は正当な業務状態ではない(贈与・スピンオフ等を0円取得として
    表す仕様は存在しない)。CSV取込・LINE会話型登録は以前から0以下を拒否しており、
    本検証はその契約を購入書き込みの共通境界へ引き上げるものである。
    """
    if purchase_price <= 0:
        raise ValueError(PURCHASE_PRICE_MUST_BE_POSITIVE)


PURCHASE_SHARES_MUST_BE_POSITIVE = "購入株数は0より大きい値を指定してください"


def _validate_purchase_shares(shares: int) -> None:
    """新規購入ロットの株数が正であることを保証する(Issue #93 Phase B1)。

    `summarize_lots()` は **合計株数** のみを検証するため、既に保有がある銘柄へ
    0以下の株数で追加購入すると、合計が正のまま通過してしまう。とくに負値は
    「購入」でありながら実質的に売却として作用し、**売却記録を伴わずに保有株数と
    平均取得単価を改変する**(Issue #93 で実測)。

    取得単価(#75)とは別のフィールドの検証であり、責務を混ぜない。
    """
    if shares <= 0:
        raise ValueError(PURCHASE_SHARES_MUST_BE_POSITIVE)


SALE_SHARES_MUST_BE_POSITIVE = "売却株数は0より大きい値を指定してください"


def _validate_sale_shares(shares: int) -> None:
    """売却株数が正であることを保証する(Issue #96 Phase B1)。

    以前は0以下でも例外にならず、FIFO消費ループが `remaining <= 0` を先に判定して
    どのロットも消費しないまま**成功として終了**していた。純粋なno-opではなく、
    残ロットから Holding を再計算する経路(`is_sale=True`)へ到達するため、
    **売却していないのに `last_sale_date` が当日へ更新される**。

    `last_sale_date` は利益保全判定の基準日(`basis_date`)として使われるため
    (`profit_taking_service.py` / `domain/signals/profit_protection.py`)、
    これが不当に進むと**それ以前の高値形成が評価対象から外れ、利益保全シグナルが
    抑止されうる**。判定入力データの破壊にあたるため、書き込み前に打ち切る。

    購入株数(#93 `_validate_purchase_shares`)とは文言・意味が異なるため
    helperを共有しない(購入/売却を1つのabstractionへ無理に統合しない)。
    """
    if shares <= 0:
        raise ValueError(SALE_SHARES_MUST_BE_POSITIVE)


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

    def list_holdings_by_stock(self, stock_code: str) -> list[Holding]:
        """owner横断検索用(M3.1: BUY候補側で同一銘柄の全owner Holdingを集約する
        ため。特定ownerだけを見るget_holding()とは異なり、複数owner分をすべて
        返す)。"""
        return self._holdings.list_by_stock(stock_code)

    def lot_exists(self, lot_id: str) -> bool:
        """指定lot_idのロットが既に存在するか(Issue #61 Phase B1)。

        CSV取込が「この行は適用済みか」を**永続データそのもの**で判定するために使う
        (別に置いた台帳と実データがずれる状態を作らないため)。
        """
        return self._lots.get(lot_id) is not None

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
        lot_id: str | None = None,
    ) -> Holding:
        """build_purchase_write_plan()を呼び出した直後にその場で適用する薄い
        ラッパー(LINEボタン起点会話型UI・実装プランv2 3節。挙動・戻り値は
        従来と完全に同じ)。

        `lot_id`(Issue #61 Phase B1)を渡すと、そのlot_idでロットを作る。
        **省略時は従来どおりuuid4を採番する**(既存の呼び出し元の挙動は不変)。
        CSV取込は「同じCSVの同じ行」から決定的なlot_idを作ることで、
        同じ行を再適用しても新しいロットが増えないようにする。
        """
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
            lot_id=lot_id,
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
        lot_id: str | None = None,
    ) -> PurchaseWritePlan:
        """register_purchase()と同じ計算を行うが、一切の永続化を行わず
        「計画」のみを返す(LINEボタン起点会話型UI・実装プランv2 3節・
        追加条件1)。TransactWriteItemsのConditionExpression用に、
        計画構築時点で読み取った既存Holdingの生data文字列(新規追加なら
        None)をexpected_dataへ含める。
        """
        # --- Issue #75 Phase B2(2026-08-30): 取得単価の正値contract ---
        # 購入書き込みの共通境界で検証する。register_purchase()側だけに置くと、
        # 本メソッドを直接呼ぶ経路(conversation_service.py等)や将来のcallerが
        # 素通りしてしまうため。lot repositoryの読み取り・PurchaseLot生成・
        # Holding再計算・永続化のいずれよりも前でfail-fastする
        # (repository stateを一切変化させない)。
        #
        # PurchaseLot entityへvalidatorを置く方式は採らない(取得単価・株数とも)。
        # 読み込み時にも検証が走り、値が不正な既存レコードを読めなくしてしまうため
        # (#63のhistorical compatibilityへ干渉する)。取得単価については、既に
        # そうなっている保有銘柄の判定側の安全弁を #75 Phase B1(利確判定の
        # fail-close)が担う。
        _validate_purchase_price(purchase_price)
        # --- Issue #93 Phase B1(2026-08-30): 購入株数の正値contract ---
        # 検証順序は purchase_price → shares の順を維持する。#75 Phase B2 で
        # 確立した「両方不正なら purchase_price のエラーが先に返る」という
        # error precedence を、本Issueを理由に変更しない。
        _validate_purchase_shares(shares)

        now = now or dt.datetime.now(dt.UTC)
        normalized_owner = normalize_and_validate_owner(owner)
        holding_id = build_holding_id(normalized_owner, stock_code)
        lot = PurchaseLot(
            # Issue #61 Phase B1: 呼び出し元が決定的なlot_idを指定できる。
            # 省略時は従来どおりuuid4(既存の呼び出し元の挙動は不変)。
            lot_id=lot_id or str(uuid.uuid4()),
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
        # Issue #61 Phase B1: 同一lot_idを二重に数えない。決定的lot_idを渡す
        # 呼び出し元(CSV取込)が同じ行を再適用しても、Holdingはロット集合からの
        # 再計算であるため**最終状態が変わらない**(収束する)ことを保証する。
        # uuid4を採番する既存の呼び出し元にとっては何も変わらない(衝突しないため)。
        lots_for_holding = [existing for existing in existing_lots if existing.lot_id != lot.lot_id]
        holding = self._compute_holding(
            normalized_owner,
            holding_id,
            stock_code,
            [*lots_for_holding, lot],
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

    def repair_holding_projection(
        self, owner: str, stock_code: str, *, stock_name: str | None = None
    ) -> bool:
        """Holdingの集計値がロット集合とずれていれば再計算して直す(Issue #61 Phase B1)。

        直した場合はTrue、既に整合していれば**何も書き込まず**False。

        PurchaseLotとHoldingは別々の永続書き込みであり(ロット→Holdingの順)、
        ロット保存後・Holding保存前に失敗すると部分状態が残る。CSV取込は
        「その行のロットが存在するか」で適用済みを判定するため、この修復が
        無いとその状態が永久に残る。部分状態は2種類ある。

        - 既存保有への追加購入 … Holdingが古い集計値のまま残る
        - **新規保有** … Holdingがそもそも存在しない(`existing is None`)

        後者も修復対象に含める。ここで`existing is None`を理由に何もしないと、
        ロットだけが存在して保有が作られない状態が永久に残る。

        ロットが1件も無い場合は何もしない(空のロット集合から0株のHoldingを
        新規作成してしまわないため。売却によりロットが尽きた保有の扱いは
        本メソッドの責務ではない)。

        整合している場合に書き込まないのは、`updated_at`を不必要に進めないため
        (取込のやり直しは正常な操作であり、何も変わらないのに更新日時だけが
        動くと、更新日時を手掛かりにした確認ができなくなる)。
        """
        normalized_owner = normalize_and_validate_owner(owner)
        holding_id = build_holding_id(normalized_owner, stock_code)
        lots = self._lots.list_by_holding(holding_id)
        if not lots:
            return False
        existing = self._holdings.get(holding_id)
        expected = self._compute_holding(
            normalized_owner,
            holding_id,
            stock_code,
            lots,
            existing,
            dt.datetime.now(dt.UTC),
            stock_name=stock_name,
        )
        if existing is None:
            self._holdings.upsert(expected)
            return True
        # ロットから導出される項目だけを比較する(メタ情報は再計算対象外)。
        derived = (
            "shares",
            "average_purchase_price",
            "total_purchase_amount",
            "first_purchase_date",
            "last_purchase_date",
        )
        if all(getattr(existing, field) == getattr(expected, field) for field in derived):
            return False
        self._holdings.upsert(expected)
        return True

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
        # --- Issue #96 Phase B1(2026-08-30): 売却株数の正値contract ---
        # **repository読み取りより前**に打ち切る。0以下の売却は以前
        # 「どのロットも消費しないまま成功」として終わり、しかもHolding再計算経路へ
        # 到達して last_sale_date / updated_at を書き換えていた。
        # owner検証は holding_id 導出の前提のため先のまま維持する
        # (不正ownerは従来どおり先に拒否される)。
        _validate_sale_shares(shares)
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

    # --- 保有の原子的な置換・削除(Issue #61 Phase B2) ---------------------
    #
    # overwrite取込と保有削除は、いずれも「既存の全ロット + Holding を消して、
    # (置換の場合は)新しいロット + Holding を作る」という同一の操作である。
    # これを個別のwriteへ分解すると、途中で失敗したときに
    #   - Holdingだけ旧値でロットが一部欠落
    #   - Holdingが消えてロットだけ残る / その逆
    # といった部分状態が残り、**再実行しても元の保有は復元されない**
    # (削除は既に確定しているため)。Phase B1の冪等化は「新規適用を何度行っても
    # 同じ最終状態へ収束する」ことは保証するが、「削除された既存データを復元する」
    # ことは対象外である。
    #
    # そのため本Phaseでは計画(HoldingReplacementPlan)と適用を分離し、適用を
    # **単一トランザクション**で行う。外から見える結果は
    #   成功 -> 新state(または完全削除)
    #   失敗 -> 旧stateが完全に維持される
    # のどちらか一方だけになる。

    def build_holding_replacement_plan(
        self,
        owner: str,
        stock_code: str,
        *,
        purchase: PurchaseWritePlan | None,
    ) -> HoldingReplacementPlan:
        """既存の全ロットとHoldingを消し、purchaseがあればそれを新stateとする計画。

        purchase=Noneは保有の完全削除(CLIの保有削除)。purchaseを渡すと
        overwrite(置換)になる。

        **一切の永続化を行わない。** 既存アイテムの削除には、読み取った生JSONを
        expected_dataとする楽観ロック条件を必ず付与する(計画構築から適用までの
        間に別経路で変更されていれば、適用時にトランザクション全体が失敗する)。

        ロット数がMAX_LOTS_PER_HOLDINGを超える場合はHoldingLotLimitExceededErrorを
        送出する。**計画構築の時点で送出するため、呼び出し側が適用へ進むことはなく、
        HoldingもPurchaseLotも変更されない。**
        """
        normalized_owner = normalize_and_validate_owner(owner)
        holding_id = build_holding_id(normalized_owner, stock_code)
        existing_lots = self._lots.list_by_holding(holding_id)

        if len(existing_lots) > MAX_LOTS_PER_HOLDING:
            raise HoldingLotLimitExceededError(
                f"保有({stock_code})の購入ロットが{len(existing_lots)}件あり、"
                f"原子的に置き換えられる上限({MAX_LOTS_PER_HOLDING}件)を超えています。"
                "データを変更せず中止しました。"
            )

        lot_deletes: list[ConditionalDelete] = []
        for lot in existing_lots:
            raw = self._lots.get_raw_data(lot.lot_id)
            if raw is None:
                raise ValueError(f"ロットID{lot.lot_id}のデータ取得に失敗しました")
            lot_deletes.append(
                ConditionalDelete(id_value=lot.lot_id, id_field="lot_id", expected_data=raw)
            )

        existing_holding_raw: str | None = None
        if self._holdings.get(holding_id) is not None:
            existing_holding_raw = self._holdings.get_raw_data(holding_id)
            if existing_holding_raw is None:
                raise ValueError(f"保有ID{holding_id}のデータ取得に失敗しました")

        if purchase is None:
            holding_delete = (
                ConditionalDelete(
                    id_value=holding_id,
                    id_field="holding_id",
                    expected_data=existing_holding_raw,
                )
                if existing_holding_raw is not None
                else None
            )
            return HoldingReplacementPlan(
                lot_deletes=lot_deletes,
                holding_delete=holding_delete,
                lot_put=None,
                holding_put=None,
                resulting_holding=None,
            )

        # 置換後のHoldingは「新しいロット1件だけ」から再計算した値でなければならない。
        # build_purchase_write_plan()は既存ロットへの追加として集計するため、
        # ここで新ロット単独の集計へ組み替える。
        new_lot = purchase.lot_put.model
        if not isinstance(new_lot, PurchaseLot):
            raise TypeError("purchase.lot_put.modelはPurchaseLotである必要があります")
        replaced_holding = self._compute_holding(
            normalized_owner,
            holding_id,
            stock_code,
            [new_lot],
            None,
            dt.datetime.now(dt.UTC),
            stock_name=getattr(purchase.holding_put.model, "stock_name", None),
        )

        # 同一アイテムへ Delete と Put を同時に入れない(DynamoDBの制約)。
        # 既存Holdingは「削除してから作り直す」のではなく、既存の生JSONを
        # expected_dataとする**1回のConditionalPut**で置換する(楽観ロックは維持)。
        # 新しいロットIDが既存ロットに含まれる場合も同様に、削除対象から外して
        # Putだけを行う。
        new_lot_id = new_lot.lot_id
        lot_put_expected: str | None = None
        remaining_lot_deletes: list[ConditionalDelete] = []
        for lot_delete in lot_deletes:
            if lot_delete.id_value == new_lot_id:
                lot_put_expected = lot_delete.expected_data
                continue
            remaining_lot_deletes.append(lot_delete)

        return HoldingReplacementPlan(
            lot_deletes=remaining_lot_deletes,
            holding_delete=None,
            lot_put=ConditionalPut(
                model=new_lot, id_field="lot_id", expected_data=lot_put_expected
            ),
            holding_put=ConditionalPut(
                model=replaced_holding,
                id_field="holding_id",
                expected_data=existing_holding_raw,
            ),
            resulting_holding=replaced_holding,
        )

    def apply_holding_replacement_plan(self, plan: HoldingReplacementPlan) -> None:
        """計画を**全部成功か全部不成功か**のどちらかで適用する。

        Lambda環境(DynamoDB)ではTransactWriteItemsで原子的に適用する。
        ローカルJSON実装にはトランザクション機構が無いため、適用前の状態を保持し、
        途中で失敗した場合は保持した状態へ戻したうえで例外を再送出する。
        **呼び出し側から観測できる結果は両実装で同じ**(成功=全置換/全削除、
        失敗=旧stateの完全維持)。
        """
        if plan.write_item_count == 0:
            return
        if running_on_lambda():
            from jstock_advisor.infrastructure.aws.holding_replacement_commit import (
                commit_holding_replacement,
            )

            commit_holding_replacement(plan)
            return
        self._apply_holding_replacement_locally(plan)

    def _apply_holding_replacement_locally(self, plan: HoldingReplacementPlan) -> None:
        """ローカルJSON実装向けの適用。

        ## 保証する範囲(AWS実装との差)

        DynamoDB実装はTransactWriteItemsにより、プロセスの強制終了を含む
        いかなる失敗に対しても「全部成功 or 全部不成功」が保証される。
        ローカルJSON実装はロットと保有が**別ファイル**であり、同じ保証は与えられない。
        本実装が保証するのは次のとおりである。

            LOCAL_NORMAL_EXCEPTION_ROLLBACK        = YES
              書き込み中に例外が送出された場合、旧stateへ戻す
            LOCAL_CRASH_SAFE_CROSS_FILE_ATOMICITY  = NO
              2つのファイル書き込みの間でプロセスが強制終了した場合は保証しない

        ## 実装

        1. **各ファイルへの書き込みを1回にまとめる**(`apply_batch`)。
           一時ファイルへ書いてから`os.replace()`で差し替えるため、
           同一ファイル内では部分適用が起こらない。「ロットを1件ずつ削除して
           途中で失敗する」という状態は構造的に発生しない。
        2. **ロット→保有の順に書く。** ロットの書き込みで失敗した場合は
           どちらのファイルも変更されておらず、旧stateが完全に維持される。
        3. 保有の書き込みで例外が出た場合は、保持しておいた旧ロット・旧保有で
           書き戻す。

        3のロールバック自体も失敗する状況(プロセス強制終了等)で残りうるのは
        「新しいロット + 古い保有」である。これは**再取込で必ず新stateへ収束する**
        (Phase B1の決定的lot_idと`repair_holding_projection`)。overwriteが意図する
        最終状態と一致するため、利用者が指定していない状態が残ることはない。
        """
        delete_lot_ids = [d.id_value for d in plan.lot_deletes]
        saved_lots = [lot for lot in (self._lots.get(i) for i in delete_lot_ids) if lot is not None]

        # 置換(holding_put)と削除(holding_delete)は排他。どちらの場合も
        # 対象のholding_idを特定し、ロールバック用に旧Holdingを保持する。
        holding_id: str | None = None
        if plan.holding_delete is not None:
            holding_id = plan.holding_delete.id_value
        elif plan.holding_put is not None:
            holding_id = str(getattr(plan.holding_put.model, plan.holding_put.id_field))
        saved_holding = self._holdings.get(holding_id) if holding_id is not None else None

        new_lots: list[PurchaseLot] = []
        if plan.lot_put is not None:
            lot_model = plan.lot_put.model
            if not isinstance(lot_model, PurchaseLot):
                raise TypeError("lot_put.modelはPurchaseLotである必要があります")
            new_lots.append(lot_model)

        delete_holding_ids = (
            [plan.holding_delete.id_value] if plan.holding_delete is not None else []
        )
        new_holdings: list[Holding] = []
        if plan.holding_put is not None:
            holding_model = plan.holding_put.model
            if not isinstance(holding_model, Holding):
                raise TypeError("holding_put.modelはHoldingである必要があります")
            new_holdings.append(holding_model)

        # 1. ロット(ここで失敗すれば何も変わっていない)
        self._lots.apply_batch(delete_lot_ids, new_lots)
        # 2. 保有(ここで失敗したらロットと保有を書き戻す)
        try:
            self._holdings.apply_batch(delete_holding_ids, new_holdings)
        except Exception:
            restore_ids = [lot.lot_id for lot in new_lots]
            self._lots.apply_batch(restore_ids, saved_lots)
            if saved_holding is not None:
                self._holdings.apply_batch([], [saved_holding])
            elif holding_id is not None:
                self._holdings.apply_batch([holding_id], [])
            raise


    def replace_holding_with_purchase(self, **purchase_kwargs: Any) -> Holding:
        """既存の保有を破棄し、指定した購入1件だけの状態へ**原子的に**置き換える。

        保有CSVの --on-duplicate overwrite から使う。引数は
        build_purchase_write_plan() と同じ。
        """
        purchase = self.build_purchase_write_plan(**purchase_kwargs)
        plan = self.build_holding_replacement_plan(
            purchase_kwargs["owner"], purchase_kwargs["stock_code"], purchase=purchase
        )
        self.apply_holding_replacement_plan(plan)
        if plan.resulting_holding is None:
            raise RuntimeError("置換計画にHoldingが含まれていません")
        return plan.resulting_holding

    def delete_holding(self, owner: str, stock_code: str) -> bool:
        """保有とその全ロットを**原子的に**削除する(Issue #61 Phase B2)。

        戻り値の意味は従来と同じ(削除対象のHoldingが存在したかどうか)。
        以前はロットを1件ずつ削除してからHoldingを削除していたため、途中で失敗
        すると部分削除が残った。現在は失敗時に旧stateが完全に維持される。

        ロット数がMAX_LOTS_PER_HOLDINGを超える場合は、**何も削除せずに**
        HoldingLotLimitExceededErrorを送出する。
        """
        plan = self.build_holding_replacement_plan(owner, stock_code, purchase=None)
        self.apply_holding_replacement_plan(plan)
        return plan.holding_delete is not None
