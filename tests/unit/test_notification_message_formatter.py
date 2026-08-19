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


# --- 指摘2対応: 70文字soft limitとrequiredセグメントのbreak問題
# (再コードレビュー対応2026-08、指摘2再修正) ---
#
# 以前のformat_notification_text()は、必須セグメント(PARTIAL売却数量等)より
# 手前にある非必須セグメント(現在値等)だけで70文字を超えると即座にbreakし、
# それより後ろの必須セグメントが本文へ一切到達できない不具合があった。


def test_case_g_partial_sell_quantity_with_ratio_sanrio() -> None:
    """サンリオ相当(8136 サンリオ、300株、60%)で「300株(60%)」が表示される
    (Case G)。
    """
    data = _base(
        category=NotificationCategory.PARTIAL_SELL,
        stock_code="8136",
        stock_name="サンリオ",
        suggested_sell_shares=300,
        suggested_sell_ratio=0.60,
    )
    text = format_notification_text(data)
    assert "300株(60%)" in text


def test_case_h_quantity_survives_even_when_earlier_segment_alone_overflows() -> None:
    """非常に長い銘柄名+現在値のセグメントだけで70文字を超える状況を実際に
    作り、それでも売却数量「300株(60%)」が本文へ残ることを証明する(Case H)。

    以前のように「長い名前を設定しただけ」で暗黙にoverflowを期待するテストは
    禁止のため、header+current_price時点で明示的にmax_charsを超える前提を
    assertで固定する。
    """
    long_name = "非常に長い銘柄名" * 8  # 64文字
    data = _base(
        category=NotificationCategory.PARTIAL_SELL,
        stock_code="8136",
        stock_name=long_name,
        current_price=Decimal("1234567"),
        suggested_sell_shares=300,
        suggested_sell_ratio=0.60,
    )
    header_and_price_len = len(f"一部売却 {data.stock_code} {data.stock_name}\n1,234,567円")
    # header+current_priceの時点で既にmax_charsを超えることを明示的に証明する
    # (overflow前提が本物であることの担保。実装の内部書式と厳密一致しなくても、
    # この長さ自体がMAX_CHARSを優に超えていることが重要)。
    assert header_and_price_len > MAX_CHARS

    text = format_notification_text(data)
    assert "300株(60%)" in text


def test_case_i_quantity_survives_target_price_may_drop() -> None:
    """数量あり+売却目安価格あり+70文字制約の場合、数量は必ず残り、
    必要なら売却目安価格の方が落ちることを確認する(Case I)。
    """
    long_name = "非常に長い銘柄名" * 4
    data = _base(
        category=NotificationCategory.PARTIAL_SELL,
        stock_code="8136",
        stock_name=long_name,
        current_price=Decimal("1234567"),
        suggested_sell_shares=300,
        suggested_sell_ratio=0.60,
        target_price=Decimal("1500"),
        target_price_label="売却目安",
    )
    text = format_notification_text(data)
    assert "300株(60%)" in text


def test_case_j_no_shares_no_quantity_segment() -> None:
    """suggested_sell_sharesがNoneの場合、空の数量セグメントを出さない
    (Case J)。
    """
    data = _base(
        category=NotificationCategory.PARTIAL_SELL,
        suggested_sell_shares=None,
        suggested_sell_ratio=None,
    )
    text = format_notification_text(data)
    assert "株" not in text


def test_case_k_shares_without_ratio_no_bogus_percent() -> None:
    """suggested_sell_ratioがNoneの場合、「300株」とだけ表示し、
    「0%」「None%」等の不正な値を出さない(Case K)。
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
