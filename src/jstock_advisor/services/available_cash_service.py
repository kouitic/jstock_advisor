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
        明示的に拒否する(D3。自動0円初期化はしない)。available_cash<0は
        entityの既存validator(`_check_non_negative`)がValidationErrorとして
        送出する(`build_reconcile_plan()`と同じくPydanticの通常のconstructor
        経由で新レコードを構築するため検証が働く。`model_copy(update=...)`は
        フィールド検証を行わないため使わない)。この失敗はplan構築フェーズ
        (I/O前)で起きるため、Holding/Transaction/Cashのいずれも書き込まれ
        ない(#591のinsufficient cash guardが依拠する契約)。

        last_reconciled_atは既存値をそのまま引き継ぐ(TRADE_UPDATEでは進め
        ない。#584 entity docstringの契約をここで強制する)。
        """
        owner = normalize_and_validate_owner(raw_owner)
        existing = self._repo.get(owner)
        if existing is None:
            raise AvailableCashNotRegisteredError(owner)
        existing_raw = self._repo.get_raw(owner)
        record = AvailableCash(
            owner=owner,
            available_cash=existing.available_cash + delta,
            updated_at=now,
            last_update_type=AvailableCashUpdateType.TRADE_UPDATE,
            last_reconciled_at=existing.last_reconciled_at,
        )
        return ConditionalPut(model=record, id_field="owner", expected_data=existing_raw)
