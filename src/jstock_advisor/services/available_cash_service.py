"""owner単位のavailable_cashの棚卸し更新(Issue #589、#128 A2)。

trade連携によるcash増減(TRADE_UPDATE)はA2のscope外であり、本サービスは
ユーザーが証券会社等の実額と照合して明示的に上書きする経路
(USER_RECONCILIATION)のみを扱う。過去理論値との差額イベントは作らない
(入力値を正とする。#589本文どおり)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.domain.entities.enums import AvailableCashUpdateType
from jstock_advisor.domain.entities.owner import log_ref, normalize_and_validate_owner
from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
    AvailableCashRepository,
)
from jstock_advisor.services.write_plan import ConditionalPut

_DEFAULT_MAX_RETRIES = 3


class AvailableCashReconciliationExhaustedError(RuntimeError):
    """棚卸し更新が競合により max_retries 回失敗した(#589)。"""


class AvailableCashNotRegisteredError(ValueError):
    """未登録ownerに対して売買登録(TRADE_UPDATE)を試みた(#590 D3)。

    #584/#589で確立した「未登録owner != 実際の買付余力0円」契約と整合させる
    ため、trade登録時に未登録ownerを自動0円初期化しない(fail-closedで
    利用者に先に棚卸し登録〔#589 reconcile〕を行わせる)。
    """

    def __init__(self, owner: str) -> None:
        super().__init__(
            f"owner_ref={log_ref(owner)}: 買付余力が未登録のため、売買登録と連動した"
            "更新ができません。先に買付余力の棚卸し登録を行ってください。"
        )
        self.owner = owner


class InsufficientAvailableCashError(ValueError):
    """BUY登録時、購入金額がavailable_cashを超える(Issue #591、#128 A4)。

    呼び出し側(LINE#592/CLI#619)がこの型だけを捕捉して業務メッセージへ
    翻訳できるようにする(entity側の汎用ValidationErrorと型で区別する)。
    SELLはこのチェックの対象外(cashを増やすだけのため、そもそも
    available_cash不足という状態が発生しない)。
    """

    def __init__(self, owner: str, available_cash: Decimal, purchase_amount: Decimal) -> None:
        self.owner = owner
        self.available_cash = available_cash
        self.purchase_amount = purchase_amount
        super().__init__(
            f"owner_ref={log_ref(owner)}: 購入金額({purchase_amount})が"
            f"買付余力({available_cash})を超えています"
        )


class AvailableCashService:
    def __init__(
        self,
        repository: AvailableCashRepository | None = None,
        store_dir: Path | None = None,
        default_max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self._repo = repository or AvailableCashRepository(store_dir)
        self._default_max_retries = default_max_retries

    def get(self, raw_owner: str) -> AvailableCash | None:
        """owner単位の現在のavailable_cashを取得する。

        レコード不存在(未登録owner)はNone(0円とは区別する。#584 T4と同じ契約)。
        """
        owner = normalize_and_validate_owner(raw_owner)
        return self._repo.get(owner)

    def reconcile(
        self,
        raw_owner: str,
        new_amount: Decimal,
        now: dt.datetime,
        max_retries: int | None = None,
    ) -> AvailableCash:
        """ユーザー入力値でavailable_cashを上書きする(USER_RECONCILIATION)。

        契約(#589 AC2):
          - available_cash = 入力値(過去理論値との差額イベントは作らない)
          - last_update_type = USER_RECONCILIATION
          - updated_at = now
          - last_reconciled_at = now
          - available_cash < 0 は拒否(entityの既存validatorへ委譲)

        未登録ownerの場合は新規作成する(initialize)。既存レコードがある場合は
        `replace_if_raw_matches()`によるCASで更新し、競合時は最新値を再取得して
        再試行する(`investment_thesis_service.py::activate_baseline()`と同型の
        bounded retry。新しいCAS方式は作らない)。USER_RECONCILIATIONは前回値に
        依存しない絶対値上書きのため、retryのたびに同じrecordをそのまま再送信
        できる。
        """
        owner = normalize_and_validate_owner(raw_owner)
        record = AvailableCash(
            owner=owner,
            available_cash=new_amount,
            updated_at=now,
            last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
            last_reconciled_at=now,
        )
        retries = max_retries if max_retries is not None else self._default_max_retries
        for _ in range(retries):
            existing_raw = self._repo.get_raw(owner)
            if existing_raw is None:
                if self._repo.initialize(record):
                    return record
                continue  # 他プロセスが先に作成した。再取得してCAS分岐へ回す
            if self._repo.replace_if_raw_matches(owner, existing_raw, record):
                return record

        raise AvailableCashReconciliationExhaustedError(
            f"owner_ref={log_ref(owner)}: 買付余力の棚卸し更新が{retries}回失敗しました。"
            "最新状態を確認し、改めて実行してください。"
        )

    def build_reconcile_plan(
        self, raw_owner: str, new_amount: Decimal, now: dt.datetime
    ) -> ConditionalPut:
        """LINE会話型UI(Issue #592)向け: 確認(「登録する」)実行時、
        TransactWriteItemsへ含める単一Putの計画のみを返す(このメソッド自体は
        一切永続化しない)。

        `reconcile()`(#589、CLI向け)の内蔵bounded retryとは異なり、本メソッドは
        `WatchlistService.build_add_item_plan()`と同型の単発読み取りである
        (会話の確認画面表示から実際の「登録する」押下までの間隔でも競合しうる
        ため、conversation_commit側のTransactWriteItemsが持つ楽観ロック
        〔expected_data不一致で失敗〕へ委ねる。ここで再試行はしない)。
        """
        owner = normalize_and_validate_owner(raw_owner)
        record = AvailableCash(
            owner=owner,
            available_cash=new_amount,
            updated_at=now,
            last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
            last_reconciled_at=now,
        )
        existing_raw = self._repo.get_raw(owner)
        return ConditionalPut(model=record, id_field="owner", expected_data=existing_raw)

    def build_trade_update_plan(
        self, raw_owner: str, delta: Decimal, now: dt.datetime
    ) -> ConditionalPut:
        """通常売買登録(BUY/SELL)確定時、TransactWriteItemsへ含める単一Putの
        計画のみを返す(Issue #590、#128 A3-LINE。このメソッド自体は一切
        永続化しない)。

        `delta`は呼び出し側(conversation_service.py)が符号込みで渡す
        (BUY: -purchase_price*shares、SELL: +sale_price*shares)。本メソッドは
        単純に現在値へ加算するのみで、符号の業務ルールは持たない。

        未登録ownerへのTRADE_UPDATEは`AvailableCashNotRegisteredError`で
        明示的に拒否する(D3。自動0円初期化はしない)。

        購入金額が現在の買付余力を上回る場合(BUY、delta<0の場合のみ該当。
        SELLは常にdelta>=0のためこの条件には該当しない)は
        `InsufficientAvailableCashError`を明示的に送出する(Issue #591、
        #128 A4。呼び出し側〔LINE#592/CLI#619〕がentity側の汎用
        ValidationErrorと型で区別して業務メッセージへ翻訳できるようにする
        ため)。entity側の既存validator(`_check_non_negative`)はこの専用
        チェックが機能している限り理論上到達しないが、削除しない
        (defense-in-depth。将来別の呼び出し元がこのチェックを経由せず
        直接構築した場合の最後の安全網として残す)。この失敗はplan構築
        フェーズ(I/O前)で起きるため、Holding/Transaction/Cashのいずれも
        書き込まれない。

        last_reconciled_atは既存値をそのまま引き継ぐ(TRADE_UPDATEでは進め
        ない。#584 entity docstringの契約をここで強制する)。

        **deltaの基準値(現在残高)とCASの`expected_data`は、同一の読み取り
        (`get_raw()`1回)から導出する。**`get()`と`get_raw()`を別々に呼ぶと、
        両者の間に別経路(#589の棚卸し等)の更新が割り込んだ場合、
        `expected_data`は新しい値になるためCAS自体は成立してしまうにも
        関わらず、delta計算は古い基準値のまま行われ、更新が黙って失われる
        (サブちゃんレビュー#620指摘F1。実測: 初期100万→並行更新で200万→
        旧100万を基準にdelta計算→CAS成立→最終85万、期待値185万との差
        100万円)。`build_reconcile_plan()`は絶対値上書きのため基準値
        自体が不要で単発読み取りで問題にならないが、本メソッドは相対計算
        (delta)であるため、読み取りを1回に統合する必要がある。
        """
        owner = normalize_and_validate_owner(raw_owner)
        existing_raw = self._repo.get_raw(owner)
        if existing_raw is None:
            raise AvailableCashNotRegisteredError(owner)
        existing = AvailableCash.model_validate_json(existing_raw)
        new_amount = existing.available_cash + delta
        if new_amount < 0:
            raise InsufficientAvailableCashError(owner, existing.available_cash, -delta)
        record = AvailableCash(
            owner=owner,
            available_cash=new_amount,
            updated_at=now,
            last_update_type=AvailableCashUpdateType.TRADE_UPDATE,
            last_reconciled_at=existing.last_reconciled_at,
        )
        return ConditionalPut(model=record, id_field="owner", expected_data=existing_raw)
