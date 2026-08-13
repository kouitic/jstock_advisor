from decimal import Decimal

from jstock_advisor.domain.entities.enums import NotificationCategory, StockType
from jstock_advisor.domain.notification.message_formatter import (
    MAX_CHARS,
    NotificationTextInput,
    format_notification_text,
)


def _base(**overrides: object) -> NotificationTextInput:
    base: dict[str, object] = {
        "category": NotificationCategory.NEAR_BUY,
        "stock_code": "9432",
        "stock_name": "NTT",
    }
    base.update(overrides)
    return NotificationTextInput(**base)  # type: ignore[arg-type]


def test_required_and_price_and_distance_are_included() -> None:
    text = format_notification_text(
        _base(
            current_price=Decimal("158"),
            target_price=Decimal("150"),
            distance_pct=Decimal("5.1"),
        )
    )
    assert "9432" in text
    assert "NTT" in text
    assert "158" in text
    assert "150" in text
    assert len(text) <= MAX_CHARS


def test_text_length_never_exceeds_max_chars_for_non_critical() -> None:
    data = _base(
        current_price=Decimal("158"),
        target_price=Decimal("150"),
        distance_pct=Decimal("5.1"),
        consecutive_business_days=4,
        reason="配当性向の余力評価に基づく非常に長い理由テキストがここに続きます" * 3,
        stock_types=[StockType.INCOME, StockType.QUALITY],
    )
    text = format_notification_text(data)
    assert len(text) <= MAX_CHARS


def test_critical_risk_does_not_truncate_reason() -> None:
    long_reason = "重大な会計問題が確認されたため、緊急に保有内容を確認してください。" * 2
    data = _base(category=NotificationCategory.CRITICAL_RISK, reason=long_reason)
    text = format_notification_text(data, is_critical_risk=True)
    assert long_reason in text
    # 重大リスクはmax_charsを厳密な上限としないため、超えてよい
    assert len(text) > MAX_CHARS


def test_required_fields_never_dropped_even_with_long_stock_name() -> None:
    data = _base(stock_name="非常に長い銘柄名" * 10, reason="理由" * 40)
    text = format_notification_text(data)
    assert "9432" in text
    # 長い銘柄名でも例外を送出せず、文字列として返る
    assert isinstance(text, str)


def test_watch_end_label_used_when_is_watch_end() -> None:
    data = _base(is_watch_end=True, consecutive_business_days=7)
    text = format_notification_text(data)
    assert "監視終了" in text


def test_resumed_after_gap_shows_watch_resumed_not_day_count() -> None:
    data = _base(consecutive_business_days=1, is_resumed_after_gap=True)
    text = format_notification_text(data)
    assert "監視再開" in text
    assert "1日連続" not in text
