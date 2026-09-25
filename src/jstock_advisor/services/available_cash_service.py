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

_DEFAULT_MAX_RETRIES = 3


class AvailableCashReconciliationExhaustedError(RuntimeError):
    """棚卸し更新が競合により max_retries 回失敗した(#589)。"""


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
