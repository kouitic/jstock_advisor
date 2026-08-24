"""保有銘柄一覧表示(LINE UI第二弾、読み取り専用、2026-08)のテスト。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import build_holding_id
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.services.holdings_view_service import HoldingsViewService

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)


def _holding(owner: str, stock_code: str, shares: int = 100, price: str = "1000") -> Holding:
    p = Decimal(price)
    return Holding(
        owner=owner,
        holding_id=build_holding_id(owner, stock_code),
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        shares=shares,
        average_purchase_price=p,
        total_purchase_amount=p * shares,
        first_purchase_date=_NOW.date(),
        last_purchase_date=_NOW.date(),
        account_type=AccountType.NISA,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _FakeResolver:
    def resolve(
        self, stock_code: str, fallback_name: str | None = None, fallback_name_provider=None
    ) -> str:
        return {"9432": "NTT", "8306": "三菱UFJ FG"}.get(stock_code, fallback_name or stock_code)


# --- owner一覧取得・重複除去 ----------------------------------------------------


def test_list_owners_returns_empty_when_no_holdings(tmp_path: Path) -> None:
    service = HoldingsViewService(HoldingRepository(store_dir=tmp_path))
    assert service.list_owners() == []


def test_list_owners_returns_single_owner(tmp_path: Path) -> None:
    repo = HoldingRepository(store_dir=tmp_path)
    repo.upsert(_holding("所有者A", "9432"))
    service = HoldingsViewService(repo)
    assert service.list_owners() == ["所有者A"]


def test_list_owners_deduplicates_and_sorts(tmp_path: Path) -> None:
    """同一ownerが複数銘柄を保有していても重複なく1件、複数ownerは安定順で返す。
    owner名はコードにハードコードされておらず、既存Holdingから動的に導出する。"""
    repo = HoldingRepository(store_dir=tmp_path)
    repo.upsert(_holding("所有者C", "9432"))
    repo.upsert(_holding("所有者A", "9432"))
    repo.upsert(_holding("所有者A", "8306"))
    repo.upsert(_holding("所有者B", "8306"))
    service = HoldingsViewService(repo)

    owners = service.list_owners()

    assert owners == sorted({"所有者C", "所有者A", "所有者B"})
    assert len(owners) == 3


# --- 選択ownerの保有銘柄一覧取得 -------------------------------------------------


def test_build_owner_holdings_lines_filters_by_owner(tmp_path: Path) -> None:
    repo = HoldingRepository(store_dir=tmp_path)
    repo.upsert(_holding("所有者A", "9432", shares=4300, price="163"))
    repo.upsert(_holding("所有者B", "8306", shares=100, price="2613"))
    service = HoldingsViewService(repo, display_name_resolver=_FakeResolver())

    lines = service.build_owner_holdings_lines("所有者A")

    assert lines == ["NTT（9432）｜4,300株｜平均163円"]


def test_build_owner_holdings_lines_other_owner_holdings_do_not_leak(tmp_path: Path) -> None:
    """他ownerのHoldingが混ざらないこと。"""
    repo = HoldingRepository(store_dir=tmp_path)
    repo.upsert(_holding("所有者A", "9432"))
    repo.upsert(_holding("所有者B", "8306"))
    repo.upsert(_holding("所有者C", "1234"))
    service = HoldingsViewService(repo, display_name_resolver=_FakeResolver())

    lines = service.build_owner_holdings_lines("所有者A")

    assert len(lines) == 1
    assert "9432" in lines[0]
    assert "8306" not in lines[0]
    assert "1234" not in lines[0]


def test_build_owner_holdings_lines_sorted_by_stock_code(tmp_path: Path) -> None:
    repo = HoldingRepository(store_dir=tmp_path)
    repo.upsert(_holding("所有者A", "9432"))
    repo.upsert(_holding("所有者A", "8306"))
    service = HoldingsViewService(repo, display_name_resolver=_FakeResolver())

    lines = service.build_owner_holdings_lines("所有者A")

    assert [line.split("（")[1][:4] for line in lines] == ["8306", "9432"]


def test_build_owner_holdings_lines_returns_empty_for_unknown_owner(tmp_path: Path) -> None:
    repo = HoldingRepository(store_dir=tmp_path)
    repo.upsert(_holding("所有者A", "9432"))
    service = HoldingsViewService(repo, display_name_resolver=_FakeResolver())

    assert service.build_owner_holdings_lines("存在しない人") == []


def test_build_owner_holdings_lines_falls_back_to_stock_name_when_resolver_unresolved(
    tmp_path: Path,
) -> None:
    """銘柄名解決失敗時のfallback: resolverがstock_codeへフォールバックしても
    表示自体は壊れない(既存Holding.stock_nameがfallback_nameとして渡る)。"""

    class _AlwaysFallbackResolver:
        def resolve(self, stock_code, fallback_name=None, fallback_name_provider=None) -> str:
            return fallback_name or stock_code

    repo = HoldingRepository(store_dir=tmp_path)
    repo.upsert(_holding("所有者A", "9999"))
    service = HoldingsViewService(repo, display_name_resolver=_AlwaysFallbackResolver())

    lines = service.build_owner_holdings_lines("所有者A")

    assert lines == ["銘柄9999（9999）｜100株｜平均1,000円"]


def test_build_owner_holdings_lines_without_resolver_uses_stock_name(tmp_path: Path) -> None:
    repo = HoldingRepository(store_dir=tmp_path)
    repo.upsert(_holding("所有者A", "9432"))
    service = HoldingsViewService(repo, display_name_resolver=None)

    lines = service.build_owner_holdings_lines("所有者A")

    assert lines == ["銘柄9432（9432）｜100株｜平均1,000円"]


# --- 平均取得単価・株数の表示形式 ------------------------------------------------


def test_shares_and_average_price_formatted_with_thousands_separator(tmp_path: Path) -> None:
    repo = HoldingRepository(store_dir=tmp_path)
    repo.upsert(_holding("所有者A", "9432", shares=4300, price="163"))
    service = HoldingsViewService(repo, display_name_resolver=_FakeResolver())

    lines = service.build_owner_holdings_lines("所有者A")

    assert "4,300株" in lines[0]
    assert "平均163円" in lines[0]


# --- 書き込みを一切行わない(19節: 読み取り専用機能としての安全性) -----------------


def test_view_service_does_not_expose_write_methods(tmp_path: Path) -> None:
    repo = HoldingRepository(store_dir=tmp_path)
    service = HoldingsViewService(repo)
    assert not hasattr(service, "upsert")
    assert not hasattr(service, "delete")
