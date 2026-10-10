"""Issue #605 / 605-A: 本日の投資アクション(V605-v1)の表示モデルと、状態を決める純粋関数。

**純粋な型と関数だけ**である。ネットワーク・ファイル・AWS・永続化・時計に触れない。現行の判定・
通知・保存の挙動を変えず、どこからも import されない(dormant)。renderer(605-B / C)・CLI(605-D)・
配分や入替の計算は含まない。入力は、配分(C603)と入替(C604)の「出力の形」を表す型である。

## 何を型で固定するか(USER の必須テストのうち 605-A が負うもの)

```
・NO_ACTION(評価した結果、提案なし)と UNAVAILABLE(算出不能・未評価。理由つき)を型で分ける。
  『見送り』を出せるのは NO_ACTION だけ。方針が未設定・記録なしは UNAVAILABLE である
・売却は sell_action(一部売却 / 全部売却 / 売却〔数量未確定〕)と exit_reason(4 分類)を別にする。
  数量が未確定なら shares / estimated_amount は None のまま(0 にも全株にも置き換えない)
・既存の売却関連の判定(緊急確認・利益保全注意 ほか)は LegacyDecisionItemView に文字列のまま保持し、
  新しい分類(ExitReasonLabel)へ自動変換しない。そのような対応表や分岐をこのモジュールは持たない
・Rotation は sell_leg と buy_leg を対で持つ。数値が未確定の間(numbers_confirmed = False)は、
  改善幅・算入割合・売買数量を持てない。mode = SHADOW では確定した数値を持てない
```

## V605-v1 の補足(設計上の明確化)

```
V605-v1 の不変条件『NO_ACTION => 全ての item が空』は、次のとおり読む:
  ・提案となる項目(リスク回避 / 割安解消 / 利益保全の売却・資金入替・購入〔PURCHASE〕)が空である
  ・見送りの購入候補(SKIP。理由つき)と既存の判定(legacy)は『情報』であり、NO_ACTION・UNAVAILABLE
    でも持てる(落とすと、緊急確認などの既存の情報が見えなくなるため)
```

例外のメッセージには、項目の名前だけを含め、金額・株数などの値を含めない(資産情報をログへ出さない)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from decimal import Decimal
from enum import StrEnum

SCHEMA_VERSION = "V605-v1"
ROTATION_CONDITION_TEXT = "売却しない場合、この購入は提案されません"


class TodayActionsViewError(ValueError):
    """表示モデルの不変条件に反する構築。メッセージに値を含めない。"""


# --- 列挙 -------------------------------------------------------------------------------------


class ViewMode(StrEnum):
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


class ViewStatus(StrEnum):
    ACTIONS = "ACTIONS"
    NO_ACTION = "NO_ACTION"
    UNAVAILABLE = "UNAVAILABLE"


class ViewReason(StrEnum):
    CASH_NOT_REGISTERED = "CASH_NOT_REGISTERED"
    PORTFOLIO_VALUE_UNAVAILABLE = "PORTFOLIO_VALUE_UNAVAILABLE"
    RAER_NOT_COMPARABLE = "RAER_NOT_COMPARABLE"
    POLICY_INCOMPLETE = "POLICY_INCOMPLETE"
    NOT_EVALUATED = "NOT_EVALUATED"
    BATCH_NOT_COMPLETED = "BATCH_NOT_COMPLETED"
    INPUT_CONTRACT_ERROR = "INPUT_CONTRACT_ERROR"
    COMPUTATION_FAILED = "COMPUTATION_FAILED"

    @property
    def label(self) -> str:
        return _REASON_LABELS[self]


class SellAction(StrEnum):
    PARTIAL = "PARTIAL"
    FULL = "FULL"
    UNDETERMINED = "UNDETERMINED"

    @property
    def label(self) -> str:
        return _SELL_ACTION_LABELS[self]


class ExitReasonLabel(StrEnum):
    RISK_EXIT = "RISK_EXIT"
    VALUE_EXIT = "VALUE_EXIT"
    PROFIT_PROTECTION = "PROFIT_PROTECTION"
    CAPITAL_ROTATION = "CAPITAL_ROTATION"

    @property
    def label(self) -> str:
        return _EXIT_REASON_LABELS[self]


class CashReconciliationState(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class BuyFinalAction(StrEnum):
    PURCHASE = "PURCHASE"
    SKIP = "SKIP"


class RemainingCashBasis(StrEnum):
    ESTIMATE = "ESTIMATE"
    ESTIMATE_FROM_UNCONFIRMED_CASH = "ESTIMATE_FROM_UNCONFIRMED_CASH"


class AllocationOutcomeStatus(StrEnum):
    ALLOCATED_SOME = "ALLOCATED_SOME"
    NO_ACTION = "NO_ACTION"
    UNAVAILABLE = "UNAVAILABLE"


class RotationOutcomeStatus(StrEnum):
    ROTATION_PROPOSED = "ROTATION_PROPOSED"
    SHADOW_ONLY = "SHADOW_ONLY"
    NO_ROTATION = "NO_ROTATION"
    UNAVAILABLE = "UNAVAILABLE"


_REASON_LABELS: dict[ViewReason, str] = {
    ViewReason.CASH_NOT_REGISTERED: "買付余力が未登録です",
    ViewReason.PORTFOLIO_VALUE_UNAVAILABLE: "保有の時価が揃わず、評価できません",
    ViewReason.RAER_NOT_COMPARABLE: "期待リターンの算出条件が揃わず、比較できません",
    ViewReason.POLICY_INCOMPLETE: "配分の方針が未設定です",
    ViewReason.NOT_EVALUATED: "該当日の評価記録がありません(未評価)",
    ViewReason.BATCH_NOT_COMPLETED: "対象のバッチが完了していません",
    ViewReason.INPUT_CONTRACT_ERROR: "入力の形式が想定と異なります",
    ViewReason.COMPUTATION_FAILED: "計算に失敗しました",
}
_SELL_ACTION_LABELS: dict[SellAction, str] = {
    SellAction.PARTIAL: "一部売却",
    SellAction.FULL: "全部売却",
    SellAction.UNDETERMINED: "売却(数量未確定)",
}
_EXIT_REASON_LABELS: dict[ExitReasonLabel, str] = {
    ExitReasonLabel.RISK_EXIT: "リスク回避",
    ExitReasonLabel.VALUE_EXIT: "割安の解消",
    ExitReasonLabel.PROFIT_PROTECTION: "利益保全",
    ExitReasonLabel.CAPITAL_ROTATION: "資金入替",
}


# --- 検証の補助 -------------------------------------------------------------------------------


def _fail(name: str, problem: str) -> TodayActionsViewError:
    return TodayActionsViewError(f"{name}: {problem}")


def _text(name: str, value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(name, "must be a str")
    if not allow_empty and not value.strip():
        raise _fail(name, "must not be empty")
    return value


def _decimal(name: str, value: object, *, positive: bool = False) -> Decimal:
    # bool は int のサブクラス、float・int は Decimal ではない。金額・比率に float を混ぜない
    if not isinstance(value, Decimal):
        raise _fail(name, "must be a Decimal")
    if not value.is_finite():
        raise _fail(name, "must be finite")
    if value < 0 or (positive and value == 0):
        raise _fail(name, "is out of range")
    return value


def _optional_decimal(name: str, value: object) -> Decimal | None:
    return None if value is None else _decimal(name, value)


def _shares(name: str, value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(name, "must be an int")
    if value <= 0:
        raise _fail(name, "must be positive")
    return value


def _aware(name: str, value: object) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise _fail(name, "must be a timezone-aware datetime")
    return value


def _enum[E: StrEnum](name: str, value: object, enum_type: type[E]) -> E:
    if not isinstance(value, enum_type):
        raise _fail(name, "has the wrong type")
    return value


def _tuple_of[T](name: str, value: object, item_type: type[T]) -> tuple[T, ...]:
    if not isinstance(value, tuple):
        raise _fail(name, "must be a tuple")
    for item in value:
        if not isinstance(item, item_type):
            raise _fail(name, "has an item of the wrong type")
    return value


# --- 部品 -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CashView:
    """買付余力。未登録(amount = None)と 0 円は別。未棚卸しの残高から確定資金を導かない。"""

    amount: Decimal | None
    registered: bool
    last_reconciled_at: dt.datetime | None
    reconciliation_state: CashReconciliationState

    def __post_init__(self) -> None:
        if not isinstance(self.registered, bool):
            raise _fail("cash.registered", "must be a bool")
        _enum("cash.reconciliation_state", self.reconciliation_state, CashReconciliationState)
        if not self.registered:
            if (
                self.amount is not None
                or self.last_reconciled_at is not None
                or self.reconciliation_state is not CashReconciliationState.UNKNOWN
            ):
                raise _fail("cash", "an unregistered cash has no amount and is unconfirmed")
            return
        _decimal("cash.amount", self.amount)
        if self.reconciliation_state is CashReconciliationState.CONFIRMED:
            _aware("cash.last_reconciled_at", self.last_reconciled_at)
        elif self.last_reconciled_at is not None:
            raise _fail("cash.last_reconciled_at", "an unconfirmed cash has no reconciliation time")


@dataclass(frozen=True)
class PriceBasis:
    """計算に用いた価格と、その基準時点。"""

    price: Decimal
    as_of: dt.datetime

    def __post_init__(self) -> None:
        _decimal("price_basis.price", self.price, positive=True)
        _aware("price_basis.as_of", self.as_of)


@dataclass(frozen=True)
class SellItemView:
    stock_code: str
    stock_name: str
    sell_action: SellAction
    exit_reason: ExitReasonLabel
    shares: int | None
    estimated_amount: Decimal | None
    price_basis: PriceBasis | None
    summary_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text("sell.stock_code", self.stock_code)
        _text("sell.stock_name", self.stock_name, allow_empty=True)
        _enum("sell.sell_action", self.sell_action, SellAction)
        _enum("sell.exit_reason", self.exit_reason, ExitReasonLabel)
        if self.exit_reason is ExitReasonLabel.CAPITAL_ROTATION:
            raise _fail("sell.exit_reason", "a capital rotation is held by a rotation item")
        _shares("sell.shares", self.shares)
        _optional_decimal("sell.estimated_amount", self.estimated_amount)
        if self.price_basis is not None and not isinstance(self.price_basis, PriceBasis):
            raise _fail("sell.price_basis", "has the wrong type")
        _tuple_of("sell.summary_reasons", self.summary_reasons, str)
        if self.sell_action is SellAction.UNDETERMINED:
            # 数量が未確定のとき、0 でも全株でもなく『未確定』のまま持つ
            if self.shares is not None or self.estimated_amount is not None:
                raise _fail("sell", "an undetermined sell holds no quantity")
        elif self.shares is None:
            raise _fail("sell.shares", "is required for a partial or full sell")


@dataclass(frozen=True)
class LegacyDecisionItemView:
    """既存の売却関連の判定。ラベルは既存の文言のまま保持し、新しい分類へ変換しない。"""

    stock_code: str
    stock_name: str
    legacy_label: str
    shares: int | None
    note: str

    def __post_init__(self) -> None:
        _text("legacy.stock_code", self.stock_code)
        _text("legacy.stock_name", self.stock_name, allow_empty=True)
        _text("legacy.legacy_label", self.legacy_label)
        _shares("legacy.shares", self.shares)
        _text("legacy.note", self.note, allow_empty=True)


@dataclass(frozen=True)
class RotationLeg:
    stock_code: str
    stock_name: str
    shares: int | None
    estimated_amount: Decimal | None
    price_basis: PriceBasis | None

    def __post_init__(self) -> None:
        _text("leg.stock_code", self.stock_code)
        _text("leg.stock_name", self.stock_name, allow_empty=True)
        _shares("leg.shares", self.shares)
        _optional_decimal("leg.estimated_amount", self.estimated_amount)
        if self.price_basis is not None and not isinstance(self.price_basis, PriceBasis):
            raise _fail("leg.price_basis", "has the wrong type")


@dataclass(frozen=True)
class RotationItemView:
    """資金入替。売却側と購入側は必ず対で持つ(片方だけの提案は作れない)。"""

    sell_leg: RotationLeg
    buy_leg: RotationLeg
    numbers_confirmed: bool = False
    improvement: Decimal | None = None
    counted_ratio: Decimal | None = None
    condition_text: str = ROTATION_CONDITION_TEXT

    def __post_init__(self) -> None:
        if not isinstance(self.sell_leg, RotationLeg) or not isinstance(self.buy_leg, RotationLeg):
            raise _fail("rotation", "needs both a sell leg and a buy leg")
        if self.condition_text != ROTATION_CONDITION_TEXT:
            raise _fail("rotation.condition_text", "is fixed")
        if not isinstance(self.numbers_confirmed, bool):
            raise _fail("rotation.numbers_confirmed", "must be a bool")
        legs = (self.sell_leg, self.buy_leg)
        if not self.numbers_confirmed:
            # 未確定の数値(改善幅・算入割合・売買数量)は持てない
            if self.improvement is not None or self.counted_ratio is not None:
                raise _fail("rotation", "unconfirmed numbers are not held")
            if any(leg.shares is not None or leg.estimated_amount is not None for leg in legs):
                raise _fail("rotation", "unconfirmed quantities are not held")
            return
        _decimal("rotation.improvement", self.improvement)
        ratio = _decimal("rotation.counted_ratio", self.counted_ratio)
        if ratio > 1:
            raise _fail("rotation.counted_ratio", "is out of range")
        if any(leg.shares is None or leg.estimated_amount is None for leg in legs):
            raise _fail("rotation", "confirmed numbers need both quantities")


@dataclass(frozen=True)
class BuyItemView:
    """購入候補。銘柄単独の判断と最終アクション(購入 / 見送りと理由)を別に持つ。"""

    stock_code: str
    stock_name: str
    standalone_judgement: str
    final_action: BuyFinalAction
    skip_reason_code: str | None
    skip_reason_label: str | None
    shares: int | None
    estimated_amount: Decimal | None
    price_basis: PriceBasis | None
    price_tier: str | None = None

    def __post_init__(self) -> None:
        _text("buy.stock_code", self.stock_code)
        _text("buy.stock_name", self.stock_name, allow_empty=True)
        _text("buy.standalone_judgement", self.standalone_judgement)
        _enum("buy.final_action", self.final_action, BuyFinalAction)
        _shares("buy.shares", self.shares)
        _optional_decimal("buy.estimated_amount", self.estimated_amount)
        if self.price_basis is not None and not isinstance(self.price_basis, PriceBasis):
            raise _fail("buy.price_basis", "has the wrong type")
        if self.price_tier is not None:
            _text("buy.price_tier", self.price_tier)
        if self.final_action is BuyFinalAction.SKIP:
            _text("buy.skip_reason_code", self.skip_reason_code)
            _text("buy.skip_reason_label", self.skip_reason_label)
            if self.shares is not None or self.estimated_amount is not None:
                raise _fail("buy", "a skipped candidate holds no quantity")
        else:
            if self.skip_reason_code is not None or self.skip_reason_label is not None:
                raise _fail("buy", "a purchase has no skip reason")
            if self.shares is None:
                raise _fail("buy.shares", "is required for a purchase")


# --- 配分・入替の出力の形 ---------------------------------------------------------------------


@dataclass(frozen=True)
class AllocationOutcome:
    """C603 の出力の形。UNAVAILABLE は理由つき・購入なし。NO_ACTION は評価済みで購入なし。"""

    status: AllocationOutcomeStatus
    unavailable_reasons: tuple[ViewReason, ...] = ()
    buys: tuple[BuyItemView, ...] = ()

    def __post_init__(self) -> None:
        _enum("allocation.status", self.status, AllocationOutcomeStatus)
        _tuple_of("allocation.unavailable_reasons", self.unavailable_reasons, ViewReason)
        _tuple_of("allocation.buys", self.buys, BuyItemView)
        purchases = [b for b in self.buys if b.final_action is BuyFinalAction.PURCHASE]
        if self.status is AllocationOutcomeStatus.ALLOCATED_SOME:
            if not purchases or self.unavailable_reasons:
                raise _fail(
                    "allocation", "an allocation needs a purchase and no unavailable reason"
                )
        elif self.status is AllocationOutcomeStatus.NO_ACTION:
            if purchases or self.unavailable_reasons:
                raise _fail("allocation", "no action holds no purchase and no unavailable reason")
        elif not self.unavailable_reasons or self.buys:
            raise _fail("allocation", "an unavailable allocation needs reasons and holds no buy")


@dataclass(frozen=True)
class RotationOutcome:
    """C604 の出力の形。"""

    status: RotationOutcomeStatus
    items: tuple[RotationItemView, ...] = ()
    unavailable_reasons: tuple[ViewReason, ...] = ()

    def __post_init__(self) -> None:
        _enum("rotation_outcome.status", self.status, RotationOutcomeStatus)
        _tuple_of("rotation_outcome.items", self.items, RotationItemView)
        _tuple_of("rotation_outcome.unavailable_reasons", self.unavailable_reasons, ViewReason)
        proposed = self.status is RotationOutcomeStatus.ROTATION_PROPOSED
        unavailable = self.status is RotationOutcomeStatus.UNAVAILABLE
        if proposed != bool(self.items):
            raise _fail("rotation_outcome", "items exist only for a proposed rotation")
        if unavailable != bool(self.unavailable_reasons):
            raise _fail("rotation_outcome", "reasons exist only for an unavailable rotation")


# --- 表示モデル -------------------------------------------------------------------------------

_GROUPS: tuple[tuple[str, ExitReasonLabel], ...] = (
    ("risk_exit_items", ExitReasonLabel.RISK_EXIT),
    ("value_exit_items", ExitReasonLabel.VALUE_EXIT),
    ("profit_protection_items", ExitReasonLabel.PROFIT_PROTECTION),
)


@dataclass(frozen=True)
class TodayActionsView:
    mode: ViewMode
    owner: str
    evaluated_at: dt.datetime
    cash: CashView
    status: ViewStatus
    reasons: tuple[ViewReason, ...] = ()
    schema_version: str = SCHEMA_VERSION
    contract_versions: tuple[tuple[str, str], ...] = ()
    model_versions: tuple[str, ...] = ()
    comparison_keys: tuple[str, ...] = ()
    risk_exit_items: tuple[SellItemView, ...] = ()
    value_exit_items: tuple[SellItemView, ...] = ()
    profit_protection_items: tuple[SellItemView, ...] = ()
    legacy_items: tuple[LegacyDecisionItemView, ...] = ()
    rotation_items: tuple[RotationItemView, ...] = ()
    buy_items: tuple[BuyItemView, ...] = ()
    rotation_evaluated: bool = False
    projected_remaining_cash: Decimal | None = None
    remaining_cash_basis: RemainingCashBasis | None = None
    unchanged_holdings_count: int | None = None
    allow_active: InitVar[bool] = False

    def __post_init__(self, allow_active: bool) -> None:
        self._validate_header(allow_active)
        self._validate_items()
        self._validate_status()
        self._validate_remaining_cash()

    def _validate_header(self, allow_active: bool) -> None:
        _enum("view.mode", self.mode, ViewMode)
        if self.mode is ViewMode.ACTIVE and allow_active is not True:
            raise _fail("view.mode", "active mode needs an explicit permission")
        owner = _text("view.owner", self.owner)
        if any(ch.isspace() for ch in owner):
            raise _fail("view.owner", "must not contain whitespace")
        _aware("view.evaluated_at", self.evaluated_at)
        if not isinstance(self.cash, CashView):
            raise _fail("view.cash", "has the wrong type")
        _enum("view.status", self.status, ViewStatus)
        _tuple_of("view.reasons", self.reasons, ViewReason)
        if len(set(self.reasons)) != len(self.reasons):
            raise _fail("view.reasons", "must not repeat")
        if self.schema_version != SCHEMA_VERSION:
            raise _fail("view.schema_version", "is fixed")
        for pair in _tuple_of("view.contract_versions", self.contract_versions, tuple):
            if len(pair) != 2 or not all(isinstance(part, str) for part in pair):
                raise _fail("view.contract_versions", "must hold (name, version) pairs")
        _tuple_of("view.model_versions", self.model_versions, str)
        _tuple_of("view.comparison_keys", self.comparison_keys, str)
        if not isinstance(self.rotation_evaluated, bool):
            raise _fail("view.rotation_evaluated", "must be a bool")

    def _validate_items(self) -> None:
        for field_name, reason in _GROUPS:
            items = _tuple_of(f"view.{field_name}", getattr(self, field_name), SellItemView)
            if any(item.exit_reason is not reason for item in items):
                raise _fail(f"view.{field_name}", "holds an item of another reason")
        _tuple_of("view.legacy_items", self.legacy_items, LegacyDecisionItemView)
        _tuple_of("view.rotation_items", self.rotation_items, RotationItemView)
        _tuple_of("view.buy_items", self.buy_items, BuyItemView)
        if self.rotation_items and not self.rotation_evaluated:
            raise _fail("view.rotation_evaluated", "a rotation item implies an evaluation")
        if self.mode is ViewMode.SHADOW and any(i.numbers_confirmed for i in self.rotation_items):
            raise _fail("view.rotation_items", "a shadow view holds no confirmed numbers")

    def _actionable_count(self) -> int:
        purchases = sum(1 for b in self.buy_items if b.final_action is BuyFinalAction.PURCHASE)
        exits = len(self.risk_exit_items) + len(self.value_exit_items)
        exits += len(self.profit_protection_items)
        return exits + len(self.rotation_items) + purchases

    def _validate_status(self) -> None:
        actionable = self._actionable_count()
        if self.status is ViewStatus.ACTIONS:
            if actionable == 0:
                raise _fail("view.status", "actions need an actionable item")
        elif actionable:
            raise _fail("view.status", "a day without actions holds no proposal")
        elif self.status is ViewStatus.NO_ACTION and self.reasons:
            raise _fail("view.reasons", "no action holds no reason")
        elif self.status is ViewStatus.UNAVAILABLE and not self.reasons:
            raise _fail("view.reasons", "an unavailable view needs a reason")

    def _validate_remaining_cash(self) -> None:
        _optional_decimal("view.projected_remaining_cash", self.projected_remaining_cash)
        if self.unchanged_holdings_count is not None:
            count = self.unchanged_holdings_count
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise _fail("view.unchanged_holdings_count", "must be a non-negative int")
        if (self.projected_remaining_cash is None) != (self.remaining_cash_basis is None):
            raise _fail("view.remaining_cash_basis", "goes with the projected remaining cash")
        if self.remaining_cash_basis is None:
            return
        _enum("view.remaining_cash_basis", self.remaining_cash_basis, RemainingCashBasis)
        unconfirmed = self.cash.reconciliation_state is CashReconciliationState.UNKNOWN
        expected = (
            RemainingCashBasis.ESTIMATE_FROM_UNCONFIRMED_CASH
            if unconfirmed
            else RemainingCashBasis.ESTIMATE
        )
        if self.remaining_cash_basis is not expected:
            raise _fail("view.remaining_cash_basis", "must follow the reconciliation state")


# --- 状態の写像 -------------------------------------------------------------------------------


def _merge_reasons(*groups: tuple[ViewReason, ...]) -> tuple[ViewReason, ...]:
    merged: list[ViewReason] = []
    for group in groups:
        for reason in group:
            if reason not in merged:
                merged.append(reason)
    return tuple(merged)


def build_today_actions_view(
    *,
    mode: ViewMode,
    owner: str,
    evaluated_at: dt.datetime,
    cash: CashView,
    allocation: AllocationOutcome | None,
    rotation: RotationOutcome | None = None,
    risk_exit_items: tuple[SellItemView, ...] = (),
    value_exit_items: tuple[SellItemView, ...] = (),
    profit_protection_items: tuple[SellItemView, ...] = (),
    legacy_items: tuple[LegacyDecisionItemView, ...] = (),
    projected_remaining_cash: Decimal | None = None,
    unchanged_holdings_count: int | None = None,
    contract_versions: Mapping[str, str] | None = None,
    model_versions: tuple[str, ...] = (),
    comparison_keys: tuple[str, ...] = (),
    allow_active: bool = False,
) -> TodayActionsView:
    """配分(C603)と入替(C604)の出力の形から、表示の状態を決める。

    ・評価記録なし(allocation = None)・配分が算出不能・入替が算出不能は、すべて UNAVAILABLE
      (提案が 1 件も無い場合)。『見送り』(NO_ACTION)は、評価済みで提案が無いときだけである
    ・提案となる項目が 1 件でもあれば ACTIONS。部分的に算出不能だった理由は reasons に残す
    ・既存の判定(legacy_items)は、状態に影響させず、そのまま保持する(新しい分類へ変換しない)
    """
    allocation_reasons: tuple[ViewReason, ...] = ()
    buys: tuple[BuyItemView, ...] = ()
    if allocation is None:
        allocation_reasons = (ViewReason.NOT_EVALUATED,)
    else:
        buys = allocation.buys
        if allocation.status is AllocationOutcomeStatus.UNAVAILABLE:
            allocation_reasons = allocation.unavailable_reasons
    rotation_reasons: tuple[ViewReason, ...] = ()
    rotation_items: tuple[RotationItemView, ...] = ()
    if rotation is not None:
        rotation_items = rotation.items
        rotation_reasons = rotation.unavailable_reasons
    reasons = _merge_reasons(allocation_reasons, rotation_reasons)

    actionable = bool(
        risk_exit_items
        or value_exit_items
        or profit_protection_items
        or rotation_items
        or any(b.final_action is BuyFinalAction.PURCHASE for b in buys)
    )
    if actionable:
        status = ViewStatus.ACTIONS
    elif reasons:
        status = ViewStatus.UNAVAILABLE
    else:
        status = ViewStatus.NO_ACTION

    basis: RemainingCashBasis | None = None
    if projected_remaining_cash is not None:
        basis = (
            RemainingCashBasis.ESTIMATE_FROM_UNCONFIRMED_CASH
            if cash.reconciliation_state is CashReconciliationState.UNKNOWN
            else RemainingCashBasis.ESTIMATE
        )
    return TodayActionsView(
        mode=mode,
        owner=owner,
        evaluated_at=evaluated_at,
        cash=cash,
        status=status,
        reasons=reasons,
        contract_versions=tuple(sorted((contract_versions or {}).items())),
        model_versions=model_versions,
        comparison_keys=comparison_keys,
        risk_exit_items=risk_exit_items,
        value_exit_items=value_exit_items,
        profit_protection_items=profit_protection_items,
        legacy_items=legacy_items,
        rotation_items=rotation_items,
        buy_items=buys,
        rotation_evaluated=rotation is not None,
        projected_remaining_cash=projected_remaining_cash,
        remaining_cash_basis=basis,
        unchanged_holdings_count=unchanged_holdings_count,
        allow_active=allow_active,
    )
