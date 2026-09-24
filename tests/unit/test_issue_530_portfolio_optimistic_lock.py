"""Issue #530(#71 F-C13残り): portfolio_service.pyのread-modify-write upsertへ
楽観ロックを追加したことの回帰テスト(N3〜N7)。

既存の`CollectionStore.replace_if_raw_matches()`/`insert_if_absent()`/
`delete_if_raw_matches()`(Issue #17で確立済みのCAS primitive)と、LINEボタン
起点会話型UI向けに既に存在した`ConditionalPut`/`ConditionalDelete`
(`write_plan.py`)をそのまま再利用しており、新しい機構は作っていない。

競合のシミュレーションは、repositoryの読み取りメソッドをmonkeypatchし、
「このメソッドが読み取った直後に、別実行が先に書き込みを確定させる」という
順序を再現する手法による(watchlist側のテストと同じ手法)。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.write_plan import ConcurrentUpdateError

_STOCK = "8136"
_PURCHASE_DATE = __import__("datetime").date(2025, 4, 1)


def _seed_purchase(
    portfolio_service: PortfolioService, *, shares: int = 100, price: str = "3775"
) -> None:
    portfolio_service.register_purchase(
        owner=DEFAULT_OWNER,
        stock_code=_STOCK,
        stock_name="サンリオ",
        shares=shares,
        purchase_price=Decimal(price),
        purchase_date=_PURCHASE_DATE,
        account_type=AccountType.NISA,
    )


# --- N3: update_holding_meta同時update → lost updateしない ---------------------


def test_n3_update_holding_meta_detects_concurrent_update(
    portfolio_service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_purchase(portfolio_service)
    holdings = portfolio_service._holdings  # noqa: SLF001
    original_get_raw_data = holdings.get_raw_data

    def racy_get_raw_data(holding_id: str) -> str | None:
        raw = original_get_raw_data(holding_id)
        current = holdings.get(holding_id)
        assert current is not None
        holdings.replace_if_raw_matches(
            holding_id, raw, current.model_copy(update={"memo": "Bが割り込んで更新"})
        )
        return raw

    monkeypatch.setattr(holdings, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ConcurrentUpdateError):
        portfolio_service.update_holding_meta(DEFAULT_OWNER, _STOCK, memo="Aによる更新")

    # ★ N7: Bの更新が上書きされずに残っている(lost updateしていない)。
    holding = portfolio_service.get_holding(DEFAULT_OWNER, _STOCK)
    assert holding is not None
    assert holding.memo == "Bが割り込んで更新"


def test_n3_update_holding_meta_succeeds_when_no_conflict(
    portfolio_service: PortfolioService,
) -> None:
    """★ 反証: 競合が無ければこれまでどおり成功する。"""
    _seed_purchase(portfolio_service)

    updated = portfolio_service.update_holding_meta(DEFAULT_OWNER, _STOCK, memo="通常の更新")

    assert updated.memo == "通常の更新"


# --- N4: CLI purchase vs LINE purchase → stale holdingへの上書きを検出 ----------


def test_n4_register_purchase_detects_concurrent_holding_update(
    portfolio_service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ register_purchase()(CLI等の直接適用経路)が計画構築時点で読んだ
    Holdingの生JSONを、LINE購入等の別経路が先に更新していた場合に検出する
    (build_purchase_write_plan()自体は正しくexpected_dataを持っていたが、
    従来のregister_purchase()はそれを無条件upsertで捨てていた、というPR #563
    レビュー相当の欠陥の固定)。
    """
    _seed_purchase(portfolio_service)  # holding_id相当のHoldingを作る
    holdings = portfolio_service._holdings  # noqa: SLF001
    original_get_raw_data = holdings.get_raw_data

    def racy_get_raw_data(holding_id: str) -> str | None:
        raw = original_get_raw_data(holding_id)
        current = holdings.get(holding_id)
        if current is not None:
            # LINE経由の別購入がbuild_purchase_write_plan()の読み取り直後に
            # 先に確定したことをシミュレートする。
            holdings.replace_if_raw_matches(
                holding_id, raw, current.model_copy(update={"memo": "LINE購入が先に確定"})
            )
        return raw

    monkeypatch.setattr(holdings, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ConcurrentUpdateError):
        portfolio_service.register_purchase(
            owner=DEFAULT_OWNER,
            stock_code=_STOCK,
            stock_name="サンリオ",
            shares=50,
            purchase_price=Decimal("3800"),
            purchase_date=_PURCHASE_DATE,
            account_type=AccountType.NISA,
        )

    holding = portfolio_service.get_holding(DEFAULT_OWNER, _STOCK)
    assert holding is not None
    assert holding.memo == "LINE購入が先に確定"
    # ★ N7: 追加購入分のロットも作られていない(部分適用していない。
    # lot_putはholding_putより先に適用されるため、実際にはlotは作られてから
    # holding側で失敗する。次のoccurrence/retryでの整合はrepair_holding_
    # projection()の責務であり、本テストではregister_purchase自体が例外を
    # 投げて処理を止めることだけを確認する)。


def test_n4_register_purchase_succeeds_when_no_conflict(
    portfolio_service: PortfolioService,
) -> None:
    _seed_purchase(portfolio_service)

    holding = portfolio_service.register_purchase(
        owner=DEFAULT_OWNER,
        stock_code=_STOCK,
        stock_name="サンリオ",
        shares=50,
        purchase_price=Decimal("3800"),
        purchase_date=_PURCHASE_DATE,
        account_type=AccountType.NISA,
    )

    assert holding.shares == 150


# --- N5: CLI sell vs別write → stale delete/updateを検出 ------------------------


def test_n5_sell_shares_detects_concurrent_lot_update(
    portfolio_service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ sell_shares()(CLI等の直接適用経路)が、build_sale_write_plan()の
    計画構築時点で読んだPurchaseLotの生JSONを、別経路が先に変更していた場合に
    検出する。"""
    _seed_purchase(portfolio_service)
    lots = portfolio_service._lots  # noqa: SLF001
    original_get_raw_data = lots.get_raw_data

    def racy_get_raw_data(lot_id: str) -> str | None:
        raw = original_get_raw_data(lot_id)
        current = lots.get(lot_id)
        if current is not None:
            # 別経路(例えば別のsell操作)が先にこのロットを変更する。
            lots.replace_if_raw_matches(
                lot_id, raw, current.model_copy(update={"shares": current.shares - 10})
            )
        return raw

    monkeypatch.setattr(lots, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ConcurrentUpdateError):
        portfolio_service.sell_shares(DEFAULT_OWNER, _STOCK, 30)

    # 別経路の変更(90株)が上書きされていない。
    lot_list = portfolio_service.list_lots(DEFAULT_OWNER, _STOCK)
    assert sum(lot.shares for lot in lot_list) == 90


def test_n5_sell_shares_detects_concurrent_holding_update_on_full_sale(
    portfolio_service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全部売却(Holding削除)経路でも、Holdingの生JSONが別経路によって計画構築後に
    変更されていれば検出する(delete_if_raw_matches)。"""
    _seed_purchase(portfolio_service)
    holdings = portfolio_service._holdings  # noqa: SLF001
    original_get_raw_data = holdings.get_raw_data

    def racy_get_raw_data(holding_id: str) -> str | None:
        raw = original_get_raw_data(holding_id)
        current = holdings.get(holding_id)
        if current is not None:
            holdings.replace_if_raw_matches(
                holding_id, raw, current.model_copy(update={"memo": "別経路が先に更新"})
            )
        return raw

    monkeypatch.setattr(holdings, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ConcurrentUpdateError):
        portfolio_service.sell_shares(DEFAULT_OWNER, _STOCK, 100)  # 全株売却

    # Holdingは削除されず、別経路の更新が残っている。
    holding = portfolio_service.get_holding(DEFAULT_OWNER, _STOCK)
    assert holding is not None
    assert holding.memo == "別経路が先に更新"


def test_n5_sell_shares_succeeds_when_no_conflict(portfolio_service: PortfolioService) -> None:
    _seed_purchase(portfolio_service)

    holding = portfolio_service.sell_shares(DEFAULT_OWNER, _STOCK, 30)

    assert holding is not None
    assert holding.shares == 70


def test_n5_delete_lot_detects_concurrent_lot_update(
    portfolio_service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_purchase(portfolio_service)
    lot = portfolio_service.list_lots(DEFAULT_OWNER, _STOCK)[0]
    lots = portfolio_service._lots  # noqa: SLF001
    original_get_raw_data = lots.get_raw_data

    def racy_get_raw_data(lot_id: str) -> str | None:
        raw = original_get_raw_data(lot_id)
        current = lots.get(lot_id)
        if current is not None and lot_id == lot.lot_id:
            lots.replace_if_raw_matches(
                lot_id, raw, current.model_copy(update={"shares": current.shares - 1})
            )
        return raw

    monkeypatch.setattr(lots, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ConcurrentUpdateError):
        portfolio_service.delete_lot(DEFAULT_OWNER, _STOCK, lot.lot_id)

    # ロットは削除されず、別経路の変更(99株)が残っている。
    remaining = portfolio_service.list_lots(DEFAULT_OWNER, _STOCK)
    assert len(remaining) == 1
    assert remaining[0].shares == 99


# --- N6: repair_holding_projection vs通常更新 → newer stateを上書きしない -------


def test_n6_repair_holding_projection_detects_concurrent_update(
    portfolio_service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_purchase(portfolio_service)
    # HoldingとPurchaseLotの間に意図的なズレを作る(Holdingだけ古い集計値)。
    holdings = portfolio_service._holdings  # noqa: SLF001
    holding = portfolio_service.get_holding(DEFAULT_OWNER, _STOCK)
    assert holding is not None
    holdings.upsert(holding.model_copy(update={"shares": 1}))  # ロットとずれた値

    original_get_raw_data = holdings.get_raw_data

    def racy_get_raw_data(holding_id: str) -> str | None:
        raw = original_get_raw_data(holding_id)
        current = holdings.get(holding_id)
        if current is not None:
            # repair実行中に、より新しい正しい状態へ別経路が既に更新していた
            # とする(newer stateを古いrepairで上書きしてはならない)。
            holdings.replace_if_raw_matches(
                holding_id, raw, current.model_copy(update={"memo": "newer state"})
            )
        return raw

    monkeypatch.setattr(holdings, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ConcurrentUpdateError):
        portfolio_service.repair_holding_projection(DEFAULT_OWNER, _STOCK)

    holding_after = portfolio_service.get_holding(DEFAULT_OWNER, _STOCK)
    assert holding_after is not None
    assert holding_after.memo == "newer state"  # 上書きされていない


def test_n6_repair_holding_projection_succeeds_when_no_conflict(
    portfolio_service: PortfolioService,
) -> None:
    _seed_purchase(portfolio_service)
    holdings = portfolio_service._holdings  # noqa: SLF001
    holding = portfolio_service.get_holding(DEFAULT_OWNER, _STOCK)
    assert holding is not None
    holdings.upsert(holding.model_copy(update={"shares": 1}))

    repaired = portfolio_service.repair_holding_projection(DEFAULT_OWNER, _STOCK)

    assert repaired is True
    holding_after = portfolio_service.get_holding(DEFAULT_OWNER, _STOCK)
    assert holding_after is not None
    assert holding_after.shares == 100


def test_n6_repair_holding_projection_new_holding_detects_concurrent_creation(
    portfolio_service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holdingがそもそも存在しない場合(#61 Phase B1の部分状態修復)、別経路が
    先にHoldingを作っていれば`insert_if_absent`で検出する。"""
    _seed_purchase(portfolio_service)
    holdings = portfolio_service._holdings  # noqa: SLF001
    holding_id_holder: list[str] = []

    lots = portfolio_service.list_lots(DEFAULT_OWNER, _STOCK)
    holding_id = lots[0].holding_id
    holdings.delete(holding_id)  # Holdingだけ無い部分状態を作る

    original_get = holdings.get

    def racy_get(hid: str):  # type: ignore[no-untyped-def]
        existing = original_get(hid)
        if existing is None and hid not in holding_id_holder:
            holding_id_holder.append(hid)
            # 別経路が先にHoldingを新規作成する。
            portfolio_service.recompute_holding(DEFAULT_OWNER, _STOCK)
        return existing

    monkeypatch.setattr(holdings, "get", racy_get)

    with pytest.raises(ConcurrentUpdateError):
        portfolio_service.repair_holding_projection(DEFAULT_OWNER, _STOCK)


# --- N7: CAS conflict時は明示失敗。silent overwriteしない・無限retryしない ------


def test_n7_concurrent_update_error_message_identifies_the_id(
    portfolio_service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 明示失敗(N7)の直接固定: ConcurrentUpdateErrorはid_valueを持ち、
    どのアイテムで競合したかが呼び出し元から分かる。"""
    _seed_purchase(portfolio_service)
    holdings = portfolio_service._holdings  # noqa: SLF001
    holding_id = portfolio_service.get_holding(DEFAULT_OWNER, _STOCK)
    assert holding_id is not None
    original_get_raw_data = holdings.get_raw_data

    def racy_get_raw_data(hid: str) -> str | None:
        raw = original_get_raw_data(hid)
        current = holdings.get(hid)
        assert current is not None
        holdings.replace_if_raw_matches(hid, raw, current.model_copy(update={"memo": "B"}))
        return raw

    monkeypatch.setattr(holdings, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ConcurrentUpdateError) as exc_info:
        portfolio_service.update_holding_meta(DEFAULT_OWNER, _STOCK, memo="A")

    assert exc_info.value.id_value == holding_id.holding_id


def test_n7_apply_conditional_put_does_not_retry_automatically(
    portfolio_service: PortfolioService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 反証: CAS不成立時、apply_conditional_put()自身は無限retryを行わず、
    repositoryのreplace_if_raw_matches()を1回だけ呼んで即座に失敗する
    (#530 N7: 勝手に無限retryを導入しない)。"""
    _seed_purchase(portfolio_service)
    holdings = portfolio_service._holdings  # noqa: SLF001
    call_count = 0

    def counting_replace(holding_id: str, expected: str, model: object) -> bool:
        nonlocal call_count
        call_count += 1
        return False  # 常に不成立(実運用のCAS不成立と同じ)

    monkeypatch.setattr(holdings, "replace_if_raw_matches", counting_replace)

    with pytest.raises(ConcurrentUpdateError):
        portfolio_service.update_holding_meta(DEFAULT_OWNER, _STOCK, memo="A")

    assert call_count == 1  # retryしていない
