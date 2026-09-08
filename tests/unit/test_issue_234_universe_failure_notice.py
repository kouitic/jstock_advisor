"""Issue #234 PR-1b: 候補一覧の取得に失敗した日に、その事実を利用者への
要約通知で知らせる（方式 A）の検証。

中心は **「知らせたい日ほど届かない」構造を実際に潰せているか**である。
取得に失敗した日は候補一覧が凍結して追加 0 件になりやすく、
「追加が無い日は送らない」という従来の条件のままでは通知が出ない。
その 0 件判定は **finalizer と通知 service の 2 か所**にあり、
片方だけ直しても finalizer で止まって通知に到達しない。両方を直接検査する。

実データ・実在の銘柄名は使用しない（架空の銘柄コードと名称のみ）。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.infrastructure.aws.batch_tracker import (
    UNIVERSE_SOURCE_CACHE,
    UNIVERSE_SOURCE_DOWNLOADED,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLookup,
)
from jstock_advisor.services import watchlist_batch_finalizer
from jstock_advisor.services.line_notification_service import (
    compute_watchlist_addition_content_hash,
    render_watchlist_addition_message,
)
from jstock_advisor.services.watchlist_addition_summary_builder import (
    WatchlistAdditionItemView,
    WatchlistAdditionSummary,
)

_EVALUATED_AT = dt.datetime(2026, 9, 10, 21, 0, tzinfo=dt.UTC)
_NOTICE_HEAD = "候補一覧の取得に失敗しました。"


def _summary(
    *,
    item_count: int = 0,
    universe_fetch_failed: bool = False,
    universe_source_date: str | None = None,
) -> WatchlistAdditionSummary:
    items = [
        WatchlistAdditionItemView(
            stock_code=f"{9000 + i}",
            display_name=f"架空銘柄{i}",
            rank=i + 1,
            total_score=10.0 - i,
            highlights=[],
        )
        for i in range(item_count)
    ]
    return WatchlistAdditionSummary(
        policy_name="multi_style_monitoring",
        policy_label="複合スタイル監視",
        policy_conditions=["架空の条件"],
        total_target_count=300,
        ranked_count=280,
        data_unavailable_count=20,
        added_count=len(items),
        addition_rate_pct=len(items) / 300 * 100,
        evaluated_at=_EVALUATED_AT,
        items=items,
        universe_fetch_failed=universe_fetch_failed,
        universe_source_date=universe_source_date,
    )


# --- 本文の 1 行 -------------------------------------------------------------------


def test_failure_day_puts_the_notice_on_the_first_line() -> None:
    message = render_watchlist_addition_message(
        _summary(item_count=0, universe_fetch_failed=True, universe_source_date="2026-07-31")
    )
    assert message.splitlines()[0] == (
        "候補一覧の取得に失敗しました。前回取得(2026-07-31時点)の内容で継続しています。"
    )


def test_failure_day_with_additions_keeps_the_existing_body_after_the_notice() -> None:
    """★ 1 行足すだけで、既存の本文を壊していないこと。"""
    failed = render_watchlist_addition_message(
        _summary(item_count=3, universe_fetch_failed=True, universe_source_date="2026-07-31")
    )
    normal = render_watchlist_addition_message(_summary(item_count=3))

    assert failed.startswith(_NOTICE_HEAD)
    # 先頭の 2 行（1 行 + 空行）を除けば、成功日の本文と完全に一致する。
    assert "\n".join(failed.splitlines()[2:]) == normal


def test_success_day_body_is_byte_for_byte_unchanged() -> None:
    """取得に成功した日の通知内容は 1 文字も変わらないこと。"""
    message = render_watchlist_addition_message(_summary(item_count=3))
    assert _NOTICE_HEAD not in message
    assert message.splitlines()[0] == "【ウォッチリスト追加】"


def test_notice_shows_unknown_when_source_date_is_missing() -> None:
    message = render_watchlist_addition_message(
        _summary(item_count=0, universe_fetch_failed=True, universe_source_date=None)
    )
    assert message.splitlines()[0] == (
        "候補一覧の取得に失敗しました。前回取得(不明時点)の内容で継続しています。"
    )


def test_notice_does_not_leak_remaining_days_url_or_stock_identifiers() -> None:
    """文言に残り日数・URL・銘柄を出さないこと（#223 の管理者判断 H-1）。"""
    notice = render_watchlist_addition_message(
        _summary(item_count=0, universe_fetch_failed=True, universe_source_date="2026-07-31")
    ).splitlines()[0]
    for forbidden in ("http", "残り", "日後", "上限", "9000", "架空銘柄"):
        assert forbidden not in notice


# --- 送信条件（通知 service 側の 0 件判定） -----------------------------------------


class _FakeLog:
    def __init__(self) -> None:
        self.saved: list[object] = []
        self.latest: object | None = None

    def latest_by_stock_and_type(self, stock_code: str, notification_type: object) -> object:
        """Issue #279: 戻り値は NotificationLookup になった。

        この fake は「壊れたレコードは無い」正常系を模すため、
        undecidable = False / skipped = 0 を返す。
        """
        return NotificationLookup(
            records=[self.latest] if self.latest is not None else [],  # type: ignore[list-item]
            undecidable=False,
            skipped=0,
        )

    def save(self, entry: object) -> None:
        self.saved.append(entry)
        self.latest = entry


class _FakeLine:
    def __init__(self) -> None:
        self.pushed: list[str] = []

    def push_message(self, text: str) -> None:
        self.pushed.append(text)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch):
    from jstock_advisor.config.loader import load_config
    from jstock_advisor.services.line_notification_service import LineNotificationService

    line = _FakeLine()
    log = _FakeLog()
    svc = LineNotificationService(
        line_client=line,  # type: ignore[arg-type]
        notification_log_repository=log,  # type: ignore[arg-type]
        notification_claim_repository=None,
        recommendation_repository=None,  # type: ignore[arg-type]
        config=load_config(),
    )
    return svc, line, log


def test_failure_day_is_sent_even_with_zero_additions(service) -> None:
    """★ 本 Issue の目的そのもの。"""
    svc, line, _log = service
    sent = svc.notify_watchlist_additions(
        _summary(item_count=0, universe_fetch_failed=True, universe_source_date="2026-07-31"),
        "hash-fail-day",
    )
    assert sent is True
    assert len(line.pushed) == 1
    assert line.pushed[0].startswith(_NOTICE_HEAD)


def test_success_day_with_zero_additions_is_still_not_sent(service) -> None:
    """回帰: 取得に成功した日の「0 件なら送らない」は従来どおり。"""
    svc, line, _log = service
    sent = svc.notify_watchlist_additions(_summary(item_count=0), "hash-success-empty")
    assert sent is False
    assert line.pushed == []


def test_success_day_with_additions_is_sent_as_before(service) -> None:
    svc, line, _log = service
    sent = svc.notify_watchlist_additions(_summary(item_count=2), "hash-success-two")
    assert sent is True
    assert len(line.pushed) == 1
    assert _NOTICE_HEAD not in line.pushed[0]


def test_same_batch_rerun_is_suppressed_and_next_day_is_sent(service) -> None:
    """dedup を変えていないこと。

    content_hash は batch_id を含むため実行ごとに一意になる。
    同じ batch の再実行は同じ hash で抑止され、翌日は別 batch_id で送られる。
    """
    svc, line, _log = service
    summary = _summary(item_count=0, universe_fetch_failed=True, universe_source_date="2026-07-31")

    today = compute_watchlist_addition_content_hash(
        "watchlist-20260910T060000-aaaaaaaa", [], "multi_style_monitoring", dt.date(2026, 9, 10)
    )
    tomorrow = compute_watchlist_addition_content_hash(
        "watchlist-20260911T060000-bbbbbbbb", [], "multi_style_monitoring", dt.date(2026, 9, 11)
    )
    assert today != tomorrow

    assert svc.notify_watchlist_additions(summary, today) is True
    assert svc.notify_watchlist_additions(summary, today) is False  # 同一 batch の再実行
    assert svc.notify_watchlist_additions(summary, tomorrow) is True  # 翌日
    assert len(line.pushed) == 2


# --- 失敗日の判定（finalizer 側） ---------------------------------------------------


@pytest.mark.parametrize(
    ("batch_item", "expected"),
    [
        ({"universe_source": UNIVERSE_SOURCE_CACHE, "universe_promoted": False}, True),
        ({"universe_source": UNIVERSE_SOURCE_DOWNLOADED, "universe_promoted": True}, False),
        # Downloader を走らせていない回（maintenance / provider != "jpx"）は
        # いずれも None であり、失敗日と取り違えてはならない。
        ({"universe_source": None, "universe_promoted": None}, False),
        ({}, False),
        # 片方だけでは判定しない（PR-1a より前の古い batch 行が混ざった場合の保険）。
        ({"universe_source": UNIVERSE_SOURCE_CACHE}, False),
        ({"universe_promoted": False}, False),
    ],
)
def test_failure_day_detection_from_the_batch_row(batch_item: dict, expected: bool) -> None:
    detected = (
        batch_item.get("universe_source") == UNIVERSE_SOURCE_CACHE
        and batch_item.get("universe_promoted") is False
    )
    assert detected is expected


def test_finalizer_zero_addition_guard_also_considers_the_failure_day() -> None:
    """★ 0 件判定は 2 か所あり、finalizer 側も直っていること。

    finalizer 側だけ従来のままだと NOTIFICATION_OUTCOME_NOT_REQUIRED で
    打ち切られ、通知 service に到達しない（= 通知 service を直しても効かない）。
    """
    source = watchlist_batch_finalizer.__file__
    with open(source, encoding="utf-8") as f:
        text = f.read()
    assert "if not pending_notification_codes and not universe_fetch_failed:" in text
    assert "universe_fetch_failed=universe_fetch_failed," in text
    assert "universe_source_date=universe_source_date," in text


def test_cli_path_is_left_unchanged() -> None:
    """CLI の同経路は挙動を変えないこと（指示 2-d）。

    CLI は `if added_items and ...` で囲われており追加 0 件では通知に入らない。
    失敗日フラグを渡さない = 既定値 False のままであることを固定する。
    """
    from jstock_advisor.cli import watchlist_screening

    with open(watchlist_screening.__file__, encoding="utf-8") as f:
        text = f.read()
    assert "if added_items and wc.notification_enabled:" in text
    assert "universe_fetch_failed" not in text
    assert "universe_source_date" not in text
