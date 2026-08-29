"""LINE protocol上限の送信境界保証(Issue #50)。

責務分離の設計:
  Layer A(business formatter): 意味を保持して内部予算内へ要約する。
      代表N件+「ほかX件」でヘッダ・件数・評価日時を保持し、
      通常要約でも収まらない異常時は最小形へfallbackする。
  Layer B(LineNotificationService._push): VALIDATION banner付与後の
      「最終本文」がprotocol hard limitを超えていないことを検証する。
      自動truncateはせず、logger.error + 専用例外でfail-fastする
      (formatterの不具合を隠さない)。
  Layer C(LineClient): protocol hard limit(5000/5/13)を機械的に保証する。
      違反時は専用例外。業務文面の切断・要約は行わない。

本ファイルはLayer B/Cの境界値と、Layer Aのbatch summary要約を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ExecutionMode, NotificationMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.line.client import (
    LINE_MAX_MESSAGES_PER_REQUEST,
    LINE_MAX_QUICK_REPLY_ITEMS,
    LINE_MAX_TEXT_CHARS,
    ConsoleLineClient,
    LineMessageLimitError,
    LiveLineClient,
    QuickReplyButton,
)
from jstock_advisor.infrastructure.local_repository.daily_notification_priority_repository import (
    DailyNotificationPriorityRepository,
)
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.line_notification_service import (
    _VALIDATION_BANNER,
    NOTIFICATION_TEXT_CHAR_BUDGET,
    LineNotificationService,
)

_NOW = dt.datetime(2026, 8, 20, 4, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


class _FakeLineClient:
    """protocol検証を持たないテスト用クライアント。

    Layer B(_push)の検証がLayer Cに依存せず単独で機能することを確かめるため、
    あえて検証なしの実装を使う。
    """

    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)

    def reply_message(self, reply_token: str, text: str) -> None:
        self.sent.append(text)


def _purchase_judgment_counts(**kwargs: int) -> dict[str, int]:
    base = {
        "buy_candidate": 0,
        "near_buy": 0,
        "watch_wait": 0,
        "not_attractive": 0,
        "manual_review": 0,
        "data_insufficient": 0,
        "failed": 0,
    }
    base.update(kwargs)
    return base


def _build_service(
    tmp_path: Path,
    *,
    client: object | None = None,
    execution_context: ExecutionContext | None = None,
) -> tuple[LineNotificationService, _FakeLineClient]:
    store_dir = tmp_path / "local_store"
    line_client = client or _FakeLineClient()
    service = LineNotificationService(
        line_client=line_client,  # type: ignore[arg-type]
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
        notification_claim_repository=NotificationClaimRepository(store_dir=store_dir),
        # execution_context=None を明示的に渡すと既定値(normal)が上書きされるため、
        # 指定があるときだけ渡す。
        **({"execution_context": execution_context} if execution_context else {}),
    )
    return service, line_client  # type: ignore[return-value]


# --- Layer C: LineClient protocol validation ---------------------------------


@pytest.mark.parametrize("length", [1, LINE_MAX_TEXT_CHARS - 1, LINE_MAX_TEXT_CHARS])
def test_live_client_accepts_text_up_to_hard_limit(monkeypatch, length: int) -> None:
    """4999文字・5000文字ちょうどは通す(5000は合法な最大値)。"""
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "jstock_advisor.infrastructure.line.client._post_messages",
        lambda token, endpoint, payload: posted.append(payload),
    )
    client = LiveLineClient(channel_access_token="t", user_id="u")

    client.push_message("あ" * length)

    assert len(posted) == 1


def test_live_client_rejects_text_over_hard_limit(monkeypatch) -> None:
    """5001文字は送信前に拒否する(LINE APIの400を待たない)。"""
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "jstock_advisor.infrastructure.line.client._post_messages",
        lambda token, endpoint, payload: posted.append(payload),
    )
    client = LiveLineClient(channel_access_token="t", user_id="u")

    with pytest.raises(LineMessageLimitError):
        client.push_message("あ" * (LINE_MAX_TEXT_CHARS + 1))

    assert posted == []  # 送信を試みない


def test_live_client_accepts_five_reply_messages(monkeypatch) -> None:
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "jstock_advisor.infrastructure.line.client._post_messages",
        lambda token, endpoint, payload: posted.append(payload),
    )
    client = LiveLineClient(channel_access_token="t", user_id="u")

    client.reply_messages("token", ["a"] * LINE_MAX_MESSAGES_PER_REQUEST)

    assert len(posted) == 1


def test_live_client_rejects_six_reply_messages(monkeypatch) -> None:
    """docstringが「最大5件」と契約していたが強制が無かった(Issue #50)。"""
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "jstock_advisor.infrastructure.line.client._post_messages",
        lambda token, endpoint, payload: posted.append(payload),
    )
    client = LiveLineClient(channel_access_token="t", user_id="u")

    with pytest.raises(LineMessageLimitError):
        client.reply_messages("token", ["a"] * (LINE_MAX_MESSAGES_PER_REQUEST + 1))

    assert posted == []


def _buttons(n: int) -> list[QuickReplyButton]:
    return [QuickReplyButton(label=f"l{i}", postback_data=f"d{i}") for i in range(n)]


def test_live_client_accepts_thirteen_quick_replies(monkeypatch) -> None:
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "jstock_advisor.infrastructure.line.client._post_messages",
        lambda token, endpoint, payload: posted.append(payload),
    )
    client = LiveLineClient(channel_access_token="t", user_id="u")

    client.reply_message("token", "hi", _buttons(LINE_MAX_QUICK_REPLY_ITEMS))

    assert len(posted) == 1


def test_live_client_rejects_fourteen_quick_replies(monkeypatch) -> None:
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "jstock_advisor.infrastructure.line.client._post_messages",
        lambda token, endpoint, payload: posted.append(payload),
    )
    client = LiveLineClient(channel_access_token="t", user_id="u")

    with pytest.raises(LineMessageLimitError):
        client.reply_message("token", "hi", _buttons(LINE_MAX_QUICK_REPLY_ITEMS + 1))

    assert posted == []


def test_console_client_applies_same_protocol_validation() -> None:
    """ドライラン実装もLiveと同じ検証を行う(本番でしか気づけない状態を作らない)。"""
    client = ConsoleLineClient()

    with pytest.raises(LineMessageLimitError):
        client.push_message("あ" * (LINE_MAX_TEXT_CHARS + 1))
    with pytest.raises(LineMessageLimitError):
        client.reply_messages("token", ["a"] * (LINE_MAX_MESSAGES_PER_REQUEST + 1))
    with pytest.raises(LineMessageLimitError):
        client.reply_message("token", "hi", _buttons(LINE_MAX_QUICK_REPLY_ITEMS + 1))

    assert client.sent_messages == []

    client.push_message("あ" * LINE_MAX_TEXT_CHARS)
    assert len(client.sent_messages) == 1


# --- Layer B: _push final validation -----------------------------------------


def test_push_rejects_over_limit_without_truncating(tmp_path: Path, caplog) -> None:
    """_pushは自動truncateせず、logger.error + 例外でfail-fastする。

    ここで黙って切り詰めるとformatter層の不具合を隠してしまうため。
    """
    service, client = _build_service(tmp_path)

    with caplog.at_level("ERROR"), pytest.raises(LineMessageLimitError):
        service._push("あ" * (LINE_MAX_TEXT_CHARS + 1))

    assert client.sent == []  # 切り詰めた本文を送ってしまわない
    assert any("上限を超えました" in r.message for r in caplog.records)


def test_push_accepts_exactly_hard_limit(tmp_path: Path) -> None:
    service, client = _build_service(tmp_path)

    service._push("あ" * LINE_MAX_TEXT_CHARS)

    assert len(client.sent) == 1


def test_push_validates_after_validation_banner(tmp_path: Path) -> None:
    """VALIDATION bannerは_pushが本文組み立て後に前置するため、
    最終長を見られるのはこの層だけである。banner分を含めて判定する。"""
    context = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.SEND
    )
    service, client = _build_service(tmp_path, execution_context=context)

    # banner分を足すと上限をちょうど1文字超える長さ
    over_by_one = LINE_MAX_TEXT_CHARS - len(_VALIDATION_BANNER) + 1
    with pytest.raises(LineMessageLimitError):
        service._push("あ" * over_by_one)
    assert client.sent == []

    # formatterの予算(4500)内であればbanner付与後も上限に収まる
    service._push("あ" * NOTIFICATION_TEXT_CHAR_BUDGET)
    assert len(client.sent) == 1
    assert len(client.sent[0]) <= LINE_MAX_TEXT_CHARS


def test_validation_banner_budget_has_headroom() -> None:
    """内部予算とprotocol上限は別概念であり、余白がbannerを吸収する。"""
    assert NOTIFICATION_TEXT_CHAR_BUDGET < LINE_MAX_TEXT_CHARS


# --- Layer A: batch summary の要約 --------------------------------------------


def _codes(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:04d}" for i in range(n)]


@pytest.mark.parametrize("count", [100, 500, 1000])
def test_batch_summary_stays_within_budget_for_large_failure_counts(
    tmp_path: Path, count: int
) -> None:
    """provider全体障害等で失敗銘柄が大量になっても通知が失われない(Issue #50)。

    従来は銘柄コードを無制限に列挙しており、約766件で5000文字を超えて
    LINEが400を返し、バッチサマリー全体が送信できなくなっていた。
    """
    service, client = _build_service(tmp_path)

    sent = service.notify_batch_summary(
        "買い候補分析",
        total=count,
        category_counts={},
        now=_NOW,
        data_insufficient_stock_codes=[],
        failed_stock_codes=_codes("9", count),
        purchase_judgment_counts=_purchase_judgment_counts(failed=count),
    )

    assert sent is True
    message = client.sent[0]
    assert len(message) <= NOTIFICATION_TEXT_CHAR_BUDGET
    assert len(message) <= LINE_MAX_TEXT_CHARS
    # ヘッダ(必須部分)が保持されている
    assert "【買い候補分析完了】" in message
    assert "購入判定:" in message
    assert f"・処理失敗：{count}件" in message
    assert f"対象銘柄：{count}件" in message
    assert "評価日時：" in message


@pytest.mark.parametrize("count", [500, 1000])
def test_batch_summary_preserves_omitted_count(tmp_path: Path, count: int) -> None:
    """省略しても総件数が復元できること(「何件失敗したか」を失わない)。"""
    service, client = _build_service(tmp_path)

    service.notify_batch_summary(
        "買い候補分析",
        total=count,
        category_counts={},
        now=_NOW,
        failed_stock_codes=_codes("9", count),
        purchase_judgment_counts=_purchase_judgment_counts(failed=count),
    )

    message = client.sent[0]
    shown = sum(1 for line in message.splitlines() if line.startswith("・9"))
    omitted_lines = [line for line in message.splitlines() if line.startswith("・ほか")]
    omitted = (
        int(omitted_lines[0].removeprefix("・ほか").split("件")[0]) if omitted_lines else 0
    )
    # 省略が起きても起きなくても、表示件数+省略件数=総件数が常に成立する
    assert shown + omitted == count
    assert len(message) <= NOTIFICATION_TEXT_CHAR_BUDGET


def test_batch_summary_keeps_both_sections_when_both_overflow(tmp_path: Path) -> None:
    """データ不足と処理失敗の双方が大量でも、両区分の見出しが残る。"""
    service, client = _build_service(tmp_path)

    service.notify_batch_summary(
        "買い候補分析",
        total=1000,
        category_counts={},
        now=_NOW,
        data_insufficient_stock_codes=_codes("7", 500),
        failed_stock_codes=_codes("9", 500),
        purchase_judgment_counts=_purchase_judgment_counts(
            data_insufficient=500, failed=500
        ),
    )

    message = client.sent[0]
    assert len(message) <= NOTIFICATION_TEXT_CHAR_BUDGET
    assert "データ不足：" in message
    assert "処理失敗：" in message
    assert message.count("・ほか") == 2  # 両区分とも省略件数を持つ
    assert "・データ不足：500件" in message
    assert "・処理失敗：500件" in message


def test_batch_summary_falls_back_to_minimal_form(tmp_path: Path, monkeypatch, caplog) -> None:
    """ヘッダだけで予算を超える異常系でも、件数サマリーと評価日時は必ず残す。"""
    service, client = _build_service(tmp_path)
    # ヘッダ自体が予算を超える状況を、予算を極端に小さくして再現する
    monkeypatch.setattr(
        "jstock_advisor.services.line_notification_service.NOTIFICATION_TEXT_CHAR_BUDGET",
        10,
    )

    with caplog.at_level("ERROR"):
        service.notify_batch_summary(
            "買い候補分析",
            total=100,
            category_counts={},
            now=_NOW,
            failed_stock_codes=_codes("9", 100),
            purchase_judgment_counts=_purchase_judgment_counts(failed=100),
        )

    message = client.sent[0]
    assert "【買い候補分析完了】" in message
    assert "対象銘柄：100件" in message
    assert "処理失敗100件" in message
    assert "評価日時：" in message
    assert "要約形式で送信しました" in message
    assert any("最小形へ切り替え" in r.message for r in caplog.records)


def test_batch_summary_small_lists_unchanged(tmp_path: Path) -> None:
    """従来どおり、少数の銘柄コードはそのまま全件列挙する(既存挙動の回帰)。"""
    service, client = _build_service(tmp_path)

    service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=2,
        category_counts={},
        now=_NOW,
        data_insufficient_stock_codes=["7042"],
        failed_stock_codes=["1234"],
        purchase_judgment_counts=_purchase_judgment_counts(data_insufficient=1, failed=1),
    )

    message = client.sent[0]
    assert "データ不足：\n・7042" in message
    assert "処理失敗：\n・1234" in message
    assert "ほか" not in message


def test_batch_summary_without_code_lists_has_no_sections(tmp_path: Path) -> None:
    """0件・None のときは列挙ブロック自体を出さない(既存挙動の回帰)。"""
    service, client = _build_service(tmp_path)

    service.notify_batch_summary(
        "買い候補分析",
        total=5,
        category_counts={},
        now=_NOW,
        purchase_judgment_counts=_purchase_judgment_counts(not_attractive=5),
    )

    message = client.sent[0]
    assert "データ不足：\n" not in message
    assert "処理失敗：\n" not in message


# --- Layer A: buy digest の header/footer 算入(off-by-one 修正)-----------------


def test_buy_digest_chunks_fit_budget_including_header_and_footer(tmp_path: Path) -> None:
    """header/footerはチャンク境界を決めた後で連結されるため、従来は予算に
    算入されておらず実効上限が予算を超えていた(Issue #50、off-by-one)。
    完成形が予算内に収まることを検証する。"""
    from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
    from jstock_advisor.domain.entities.recommendation import Recommendation

    service, client = _build_service(tmp_path)
    winners = [
        Recommendation(
            recommendation_id=f"rec-{i}",
            stock_code=f"{1000 + i}",
            stock_name="テスト銘柄名称" * 5,
            recommended_at=_NOW,
            recommendation_type=RecommendationType.BUY,
            price_at_recommendation=Decimal("1000"),
            confidence=ConfidenceLevel.HIGH,
            rule_version="v1",
            reasons=["利回りが基準を満たす" * 3],
        )
        for i in range(60)
    ]

    service.notify_buy_candidates_digest(winners, _NOW, batch_id="batch-limit")

    assert client.sent, "digestが送信されていない"
    for message in client.sent:
        assert len(message) <= NOTIFICATION_TEXT_CHAR_BUDGET
        assert len(message) <= LINE_MAX_TEXT_CHARS
    # footerは最終チャンクにのみ付く(既存挙動の回帰)
    assert sum(1 for m in client.sent if "評価日時: " in m) == 1
    assert "評価日時: " in client.sent[-1]


def test_buy_digest_single_chunk_keeps_footer(tmp_path: Path) -> None:
    """1チャンクに収まる通常ケースの既存挙動を変えない。"""
    from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
    from jstock_advisor.domain.entities.recommendation import Recommendation

    service, client = _build_service(tmp_path)
    winners = [
        Recommendation(
            recommendation_id="rec-1",
            stock_code="1234",
            stock_name="テスト",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.BUY,
            price_at_recommendation=Decimal("1000"),
            confidence=ConfidenceLevel.HIGH,
            rule_version="v1",
        )
    ]

    service.notify_buy_candidates_digest(winners, _NOW, batch_id="batch-single")

    assert len(client.sent) == 1
    assert "【本日の購入候補】" in client.sent[0]
    assert "(1/1)" not in client.sent[0]  # 単一チャンクでは連番を付けない
    assert "評価日時: " in client.sent[0]
