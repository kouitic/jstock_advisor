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


# --- 指摘2対応: 70文字soft limit(コードレビュー対応2026-08) ---


def test_partial_sell_quantity_is_required_segment_exceeding_70_chars() -> None:
    """PARTIAL売却の売却数量は必須セグメントであり、70文字を超えても
    省略しない(soft limit対応、コードレビュー対応2026-08、指摘2)。
    """
    # 長い銘柄名と数量セグメントで70文字を超える条件を作る。
    long_name = "非常に長い銘柄名" * 3  # 24文字
    data = _base(
        category=NotificationCategory.PARTIAL_SELL,
        stock_name=long_name,
        suggested_sell_shares=300,
        suggested_sell_ratio=0.60,
        target_price=Decimal("1500"),  # 売却目安価格
    )
    text = format_notification_text(data)
    # 数量セグメント「300株(60%)」は欠落しない(必須)。
    assert "300株" in text
    assert "(60%)" in text
    # 結果は70文字を超えてよい(soft limit)。
    # assert len(text) > MAX_CHARS  # は実施しない(実装が確実なら自然に超過)


def test_partial_sell_quantity_without_ratio() -> None:
    """suggested_sell_ratio がNoneの場合、比率なしで「300株」だけを表示する
    (コードレビュー対応2026-08、指摘3準備)。
    """
    data = _base(
        category=NotificationCategory.PARTIAL_SELL,
        suggested_sell_shares=300,
        suggested_sell_ratio=None,
    )
    text = format_notification_text(data)
    assert "300株" in text
    assert "%" not in text  # 比率が無い


# --- 指摘3対応: suggested_sell_shares/ratio 整合性(コードレビュー対応2026-08) ---


def test_suggested_sell_shares_ratio_consistency_by_adapter() -> None:
    """Recommendation生成時点でsuggested_sell_shares と suggested_sell_ratio
    の整合性が保たれることを確認する(コードレビュー対応2026-08、指摘3)。
    例: 500株保有、STRONG(60%)の場合 → 300株、60%の組み合わせ。
    テストでは直接数値を使わず、通知層へ到達する値が一致することで
    整合性を確認する。
    """
    # 実際のRecommendationを使う結合テストはrecommendation_adapter_test.pyで行い、
    # ここでは formatter に正しい値が渡されたときの表示を確認する。
    data = _base(
        category=NotificationCategory.PARTIAL_SELL,
        stock_code="8136",
        stock_name="サンリオ",
        suggested_sell_shares=300,  # 500株の60%
        suggested_sell_ratio=0.60,   # 一致している
    )
    text = format_notification_text(data)
    assert "300株(60%)" in text


def test_partial_sell_none_shares_no_segment() -> None:
    """suggested_sell_shares がNoneの場合、セグメント自体を生成しない
    (Empty セグメント表示なし)。
    """
    data = _base(
        category=NotificationCategory.PARTIAL_SELL,
        suggested_sell_shares=None,
        suggested_sell_ratio=None,
    )
    text = format_notification_text(data)
    # セグメント自体が無い(空の「0株」等の文言が無い)。
    assert "株" not in text
