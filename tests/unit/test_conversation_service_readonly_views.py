"""ConversationServiceの読み取り専用機能(保有銘柄/ウォッチリスト/対象確認、
LINE UI第二弾、2026-08)のpostbackディスパッチのテスト。

各ビューサービス自体の一覧生成ロジックは`test_holdings_view_service.py`/
`test_watchlist_view_service.py`/`test_buy_candidate_target_view_service.py`
で個別に検証済みのため、ここではConversationServiceが正しいメソッドを呼び、
Quick Reply/文言を正しく組み立てることのみを、フェイクのビューサービスで検証する
(moto等の実インフラは使わない、19節: 読み取り専用機能としての安全性)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.services.buy_candidate_target_view_service import CATEGORY_DISPLAY_LABELS
from jstock_advisor.services.conversation_service import ConversationService
from jstock_advisor.services.latest_batch_records_provider import STILL_PROPAGATING_MESSAGE

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)
_USER = "U1"


class _FakeHoldingsView:
    def __init__(self, owners: list[str], lines_by_owner: dict[str, list[str]]) -> None:
        self._owners = owners
        self._lines_by_owner = lines_by_owner
        self.build_owner_holdings_lines_calls: list[str] = []

    def list_owners(self) -> list[str]:
        return self._owners

    def build_owner_holdings_lines(self, owner: str) -> list[str]:
        self.build_owner_holdings_lines_calls.append(owner)
        return self._lines_by_owner.get(owner, [])


class _FakeWatchlistView:
    def __init__(self, message_groups: list[list[str]] | str) -> None:
        self._message_groups = message_groups

    def build_message_groups(self) -> list[list[str]] | str:
        return self._message_groups


class _FakeTargetView:
    def __init__(self, lines_by_category: dict[str, list[str] | str]) -> None:
        self._lines_by_category = lines_by_category
        self.build_lines_calls: list[str] = []

    def build_lines(self, category_label: str) -> list[str] | str:
        self.build_lines_calls.append(category_label)
        return self._lines_by_category.get(category_label, [])


def _service(
    holdings_view=None, watchlist_view=None, target_view=None
) -> ConversationService:
    return ConversationService(
        holdings_view_service=holdings_view or _FakeHoldingsView([], {}),
        watchlist_view_service=watchlist_view or _FakeWatchlistView([]),
        target_view_service=target_view or _FakeTargetView({}),
    )


# --- 保有銘柄 ---------------------------------------------------------------


def test_show_holdings_without_owner_lists_owners_as_quick_reply() -> None:
    fake = _FakeHoldingsView(owners=["所有者A", "所有者B", "所有者C"], lines_by_owner={})
    service = _service(holdings_view=fake)

    reply = service.handle_postback(_USER, "show_holdings", None, _NOW)

    assert "誰の保有銘柄を確認しますか" in reply.text
    assert reply.quick_reply is not None
    labels = [button.label for button in reply.quick_reply]
    assert labels == ["所有者A", "所有者B", "所有者C"]
    for button in reply.quick_reply:
        assert button.postback_data.startswith("action=show_holdings&owner=")


def test_show_holdings_with_no_owners_registered() -> None:
    fake = _FakeHoldingsView(owners=[], lines_by_owner={})
    service = _service(holdings_view=fake)

    reply = service.handle_postback(_USER, "show_holdings", None, _NOW)

    assert "登録されていません" in reply.text
    assert reply.quick_reply is None


def test_show_holdings_with_owner_lists_holdings() -> None:
    fake = _FakeHoldingsView(
        owners=["所有者A"],
        lines_by_owner={"所有者A": ["NTT（9432）｜4,300株｜平均163円"]},
    )
    service = _service(holdings_view=fake)

    reply = service.handle_postback(_USER, "show_holdings", None, _NOW, owner="所有者A")

    assert fake.build_owner_holdings_lines_calls == ["所有者A"]
    assert "【所有者Aの保有銘柄】" in reply.text
    assert "NTT（9432）｜4,300株｜平均163円" in reply.text


def test_show_holdings_with_owner_having_no_holdings() -> None:
    fake = _FakeHoldingsView(owners=["所有者A"], lines_by_owner={})
    service = _service(holdings_view=fake)

    reply = service.handle_postback(_USER, "show_holdings", None, _NOW, owner="所有者A")

    assert "該当する保有銘柄がありません" in reply.text


def test_show_holdings_does_not_write_anything() -> None:
    """読み取り専用機能としての安全性(19節): Holding等の正データを一切
    書き換えない。フェイクのビューサービスに書き込みメソッド自体が存在しない
    (呼び出しようがない)ことで、この不変条件を構造的に保証する。"""
    fake = _FakeHoldingsView(owners=["所有者A"], lines_by_owner={"所有者A": []})
    assert not hasattr(fake, "upsert")
    assert not hasattr(fake, "delete")
    service = _service(holdings_view=fake)
    service.handle_postback(_USER, "show_holdings", None, _NOW, owner="所有者A")


# --- ウォッチリスト -----------------------------------------------------------


def test_show_watchlist_lists_lines() -> None:
    fake = _FakeWatchlistView([["【買い待ち】", "NTT（9432）｜現在値が買付価格を上回る"]])
    service = _service(watchlist_view=fake)

    reply = service.handle_postback(_USER, "show_watchlist", None, _NOW)

    assert "【ウォッチリスト】" in reply.text
    assert "NTT（9432）｜現在値が買付価格を上回る" in reply.text


def test_show_watchlist_multiple_messages_are_all_carried_in_texts() -> None:
    fake = _FakeWatchlistView([["【買い候補】", "対象なし"], ["【買い対象外】", "対象なし"]])
    service = _service(watchlist_view=fake)

    reply = service.handle_postback(_USER, "show_watchlist", None, _NOW)

    assert reply.texts is not None
    assert len(reply.texts) == 2
    assert "【ウォッチリスト】" in reply.texts[0]
    assert "【買い候補】" in reply.texts[0]
    assert "【買い対象外】" in reply.texts[1]


def test_show_watchlist_empty() -> None:
    fake = _FakeWatchlistView([])
    service = _service(watchlist_view=fake)

    reply = service.handle_postback(_USER, "show_watchlist", None, _NOW)

    assert "空です" in reply.text


def test_show_watchlist_still_propagating_returns_message_as_is() -> None:
    fake = _FakeWatchlistView(STILL_PROPAGATING_MESSAGE)
    service = _service(watchlist_view=fake)

    reply = service.handle_postback(_USER, "show_watchlist", None, _NOW)

    assert reply.text == STILL_PROPAGATING_MESSAGE
    assert reply.quick_reply is None


# --- 対象確認 -----------------------------------------------------------------


def test_show_targets_without_category_lists_seven_categories_as_quick_reply() -> None:
    service = _service()

    reply = service.handle_postback(_USER, "show_targets", None, _NOW)

    assert "確認する対象を選択してください" in reply.text
    assert reply.quick_reply is not None
    labels = [button.label for button in reply.quick_reply]
    assert labels == list(CATEGORY_DISPLAY_LABELS)
    assert len(labels) == 7
    for button in reply.quick_reply:
        assert button.postback_data.startswith("action=show_targets&category=")


def test_show_targets_with_category_lists_stocks_with_count_footer() -> None:
    fake = _FakeTargetView({"買い間近": ["NTT（9432）", "明治HD（2269）"]})
    service = _service(target_view=fake)

    reply = service.handle_postback(_USER, "show_targets", None, _NOW, category="買い間近")

    assert fake.build_lines_calls == ["買い間近"]
    assert "【買い間近｜直近分析】" in reply.text
    assert "NTT（9432）" in reply.text
    assert "明治HD（2269）" in reply.text
    assert reply.text.endswith("2件")


def test_show_targets_with_category_and_no_matches() -> None:
    fake = _FakeTargetView({"買い間近": []})
    service = _service(target_view=fake)

    reply = service.handle_postback(_USER, "show_targets", None, _NOW, category="買い間近")

    assert "該当する銘柄がありません" in reply.text


def test_show_targets_with_unknown_category_is_rejected() -> None:
    service = _service()

    reply = service.handle_postback(_USER, "show_targets", None, _NOW, category="謎のカテゴリ")

    assert "認識できない" in reply.text


def test_show_targets_still_propagating_returns_message_as_is() -> None:
    fake = _FakeTargetView({"買い候補": STILL_PROPAGATING_MESSAGE})
    service = _service(target_view=fake)

    reply = service.handle_postback(_USER, "show_targets", None, _NOW, category="買い候補")

    assert reply.text == STILL_PROPAGATING_MESSAGE


# --- 既存BUY/SELL/WATCHフローと共存できること(回帰) ------------------------------


def test_unknown_postback_action_is_still_rejected() -> None:
    service = _service()
    reply = service.handle_postback(_USER, "not_a_real_action", None, _NOW)
    assert "認識できない操作です" in reply.text
