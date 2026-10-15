"""利益保全 ATTENTION の局面(anchor)の identity と配信済み段階の復元(Issue #879 PR-1。dormant)。

設計: #879 rev1〜rev4・USER 承認 #122(U-6 / U-7 = 案 (a))。**時計・I/O・乱数・設定に触れない**。
どこからも import されない(配線は PR-2 以降)。段階(rank)は整数で受け取り、
`domain/exit_architecture` へは依存しない(C0 の import guard を保つ。rank の候補は引数)。

## identity の 2 形式

* 旧形式(現行): `sha256(f"{basis}|{peak_date}|{peak_price}")[:16]`(欠損は "NONE")。
  `services.line_notification_service._compute_attention_event_identity` と同値(契約テストで固定)
* 新形式: 旧形式の入力へ段階を織り込む(`v2|...|rank=N`)。段階が違えば別の hash になる

## 復元(U-7 案 (a))

直近の配信ログの `content_hash` から『その局面で配信された段階』を復元する。
* 新形式の hash が一致した段階 → その段階
* 旧形式の hash に一致 → ★ 移行日に今日の段階で配信済みとみなす ★(移行日の再送 0)
* どちらでもない(局面が変わった・読めない)→ None(= その局面の配信の実績なし)
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

#: 欠損の固定文字列(旧形式と同じ)
MISSING: Final = "NONE"
#: hash の長さ(旧形式と同じ)
HASH_LENGTH: Final = 16
#: 新形式の版
IDENTITY_VERSION: Final = "v2"


class IdentityContractError(ValueError):
    """入力の型・値が契約に反する。値を含めない。"""


@dataclass(frozen=True)
class EscalationAnchor:
    """局面を決める 3 値(basis_date, peak_date, peak_price)。欠損は None。"""

    basis_date: dt.date | None = None
    peak_date: dt.date | None = None
    peak_price: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("basis_date", "peak_date"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, dt.date) or isinstance(value, dt.datetime)
            ):
                raise IdentityContractError(f"{name} must be a date or None")
        if self.peak_price is not None and not isinstance(self.peak_price, Decimal):
            raise IdentityContractError("peak_price must be a Decimal or None")

    def key(self) -> tuple[str, str, str]:
        """旧形式と同じ文字列化(日付は isoformat・価格は str・欠損は "NONE")。"""
        return (
            self.basis_date.isoformat() if self.basis_date is not None else MISSING,
            self.peak_date.isoformat() if self.peak_date is not None else MISSING,
            str(self.peak_price) if self.peak_price is not None else MISSING,
        )


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:HASH_LENGTH]


def legacy_content_hash(anchor: EscalationAnchor) -> str:
    """旧形式(現行の `_compute_attention_event_identity` と同値)。"""
    basis, peak_date, peak_price = anchor.key()
    return _digest(f"{basis}|{peak_date}|{peak_price}")


def _check_rank(rank: object) -> int:
    if isinstance(rank, bool) or not isinstance(rank, int):
        raise IdentityContractError("rank must be an int")
    return rank


def severity_content_hash(anchor: EscalationAnchor, rank: int) -> str:
    """新形式(段階を織り込む)。同じ anchor でも段階が違えば別の hash。"""
    basis, peak_date, peak_price = anchor.key()
    return _digest(f"{IDENTITY_VERSION}|{basis}|{peak_date}|{peak_price}|rank={_check_rank(rank)}")


def restore_delivered_rank(
    last_content_hash: str | None,
    anchor: EscalationAnchor,
    *,
    today_rank: int,
    candidate_ranks: Iterable[int],
) -> int | None:
    """直近の配信ログの hash から、その局面で配信された段階を復元する(U-7 案 (a))。

    新形式に一致した段階(複数一致しても最大)→ その段階 / 旧形式に一致 → `today_rank`(移行日に
    今日の段階で配信済み)/ どちらでもない → None。`last_content_hash` が None(ログなし)も None。
    """
    if last_content_hash is None:
        return None
    today = _check_rank(today_rank)
    matched = [r for r in candidate_ranks if severity_content_hash(anchor, r) == last_content_hash]
    if matched:
        return max(matched)
    if last_content_hash == legacy_content_hash(anchor):
        return today
    return None
