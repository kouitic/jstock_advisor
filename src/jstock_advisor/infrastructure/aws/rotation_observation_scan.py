"""資金入替の観測 P の保有 ID の列挙(HoldingsTableV2 の 1 表への Scan。#604 P-b4 A1。dormant)。

P-b2 の reader(`rotation_observation_reader`)は get_item / batch_get_item / query だけを許可し、
★ Scan を呼べるコードを置かない ★ 契約だった。保有の一覧を引く唯一の既存の方法が Scan であり、
その『恒久的な Scan 権限』は USER が承認した(#604 …6195577714。条件は下)。承認された範囲を、
reader とは ★ 別の proxy ★ に閉じ込める(reader へ scan を足さない)。

## 承認の条件(契約テストで固定)

* ★ 1 表だけ ★: 表名は引数で受け、呼ぶのは `scan` だけ(proxy が他の操作を拒否する)。他の表・
  既存の BUY / SELL の Role には足さない(Role は template = A2。ここは コードだけ)
* ★ 件数の上限 ★: 保有が `MAX_HOLDINGS`(200)を超えたら、そこで打ち切って上限超過を返す
* ★ ページ数の上限 ★: `MAX_SCAN_PAGES` ページで読み切れなければ『読めない』
* ★ 時間の上限 ★: 各ページの前に、残り時間が書込の予約を超えているか確かめる。尽きたら『読めない』
* ★ 部分結果から投資判断をしない ★: 失敗・上限・形の崩れは `None`(読めない)で返し、読めた分だけを
  返さない。呼び出し側(`rotation_observation_inputs`)が読めない日として扱う(最後の回に gap が残る)

## 守ること

* 取る属性は保有 ID だけ(`ProjectionExpression`)。保有 ID は『引くキー』としてだけ使い、ログ・例外へ
  出さない。例外は送出せず `None`(値・識別子・例外の文面を持たない)
* client・表名・残り時間は ★ 引数で受ける ★(環境変数・設定・時計を読まない)。boto3 は
  `build_rotation_scan_client()` の中でだけ遅延 import する
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final

from jstock_advisor.domain.signals.rotation_observation import MAX_HOLDINGS
from jstock_advisor.infrastructure.aws.audit_shadow_reader import ReadOnlyViolationError

ALLOWED_SCAN_OPERATIONS: Final[frozenset[str]] = frozenset({"scan"})

#: Scan のページ数の上限(通常は 1 ページ。小さな表で 1 ページに収まる。超えたら『読めない』)
MAX_SCAN_PAGES: Final = 3
#: 1 リクエストで読む件数の上限(上限 + 1 件まで読めば、上限超過かどうかが分かる)
SCAN_PAGE_LIMIT: Final = MAX_HOLDINGS + 1

_KEY: Final = "holding_id"


class RotationScanClient:
    """`scan` だけを通す client の proxy。それ以外は例外(reader の proxy とは別)。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        # __getattr__ はインスタンス属性に無い名前のときだけ呼ばれる。allowlist だけを通す。
        if name not in ALLOWED_SCAN_OPERATIONS:
            raise ReadOnlyViolationError(
                f"Scan 専用の proxy は{sorted(ALLOWED_SCAN_OPERATIONS)}のみ許可する(要求: {name})"
            )
        return getattr(self._client, name)


def build_rotation_scan_client() -> RotationScanClient:
    """呼び出し元の資格情報で Scan 専用の client を作る。boto3 は遅延 import する。"""
    import boto3  # noqa: PLC0415 - 配線(handler)の実行時にだけ必要

    return RotationScanClient(boto3.client("dynamodb"))


def _holding_id(item: Any) -> str | None:
    value = item.get(_KEY) if isinstance(item, dict) else None
    raw = value.get("S") if isinstance(value, dict) else None
    return raw if isinstance(raw, str) and raw else None


class HoldingIdScanner:
    """`HoldingIdsPort` の実装。保有の ID の一覧(上限・ページ・時間の上限つき)。"""

    def __init__(
        self,
        client: RotationScanClient,
        table_name: str,
        *,
        remaining_seconds: Callable[[], float],
        reserve_seconds: float,
    ) -> None:
        self._client = client
        self._table_name = table_name
        self._remaining_seconds = remaining_seconds
        self._reserve_seconds = reserve_seconds

    def holding_ids(self) -> Sequence[str] | None:
        """保有 ID の一覧。`None` = 読めない(失敗・ページ上限・時間切れ・形の崩れ)。

        保有が `MAX_HOLDINGS` を超えたときは、`MAX_HOLDINGS + 1` 件を返して打ち切る
        (呼び出し側が上限超過と判断する。それ以上は読まない)。
        """
        try:
            return self._scan()
        except Exception:  # noqa: BLE001 - 失敗は『読めない』(値・識別子・例外の文面を持たない)
            return None

    def _scan(self) -> Sequence[str] | None:
        ids: list[str] = []
        start_key: Any = None
        for _ in range(MAX_SCAN_PAGES):
            if self._remaining_seconds() <= self._reserve_seconds:
                return None  # 時間切れ。部分結果にしない
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "ProjectionExpression": _KEY,
                "Limit": SCAN_PAGE_LIMIT,
            }
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._client.scan(**kwargs)
            if not isinstance(response, dict):
                return None
            for item in response.get("Items", []):
                holding_id = _holding_id(item)
                if holding_id is None:
                    return None  # 形が崩れた行。推測しない
                ids.append(holding_id)
                if len(ids) > MAX_HOLDINGS:
                    return ids  # 上限超過。これ以上は読まない
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return ids
        return None  # ページの上限を超えた = 読み切れていない
