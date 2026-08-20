from decimal import Decimal

import pytest

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
#
# 「Recommendation生成時点でのsuggested_sell_shares/ratio整合性」自体は、
# formatter単体Contract(Case G)と重複しないadapter層のテスト
# (test_recommendation_adapter.py::test_partial_sell_forwards_suggested_shares_
# and_ratio)・生成→adapter→formatterの一気通貫統合テスト
# (test_partial_sell_shares_integration.py::test_case_q_sanrio_500_shares_
# strong_intensity_end_to_end)で別途保証されている(テストコード削減対応
# 2026-08、AAA分析でCase Gとformatter Contractが完全重複と判断し削除)。


# --- 指摘2再修正: formatter defense-in-depth(再コードレビュー対応2026-08) ---
#
# adapter層が壊れていなくても、category!=PARTIAL_SELLへ誤ってsuggested_sell_
# shares/ratioが渡された場合、formatter自身が数量表示を拒否することを確認する。
# Case T(PARTIAL_SELLで従来どおり表示)・Case U(overflow下でも数量が残る)は
# 既存のCase G・Case Hがそのまま満たすため、重複テストは追加しない。


@pytest.mark.parametrize(
    ("category", "is_critical", "base_overrides", "expected_present", "expected_absent"),
    [
        (
            NotificationCategory.SELL,
            False,
            {
                "current_price": Decimal("4200"),
                "target_price": Decimal("4000"),
                "target_price_label": "見直し",
                "reason": "全部売却検討",
            },
            ["4,200円", "見直し4,000円", "全部売却検討"],
            ["300株", "60%"],
        ),
        (
            NotificationCategory.CRITICAL_RISK,
            True,
            {
                "current_price": Decimal("4200"),
                "target_price": Decimal("4200"),
                "target_price_label": "即時執行",
                "reason": "重大な会計問題が確認されたため、緊急に保有内容を確認してください。",
            },
            ["重大な会計問題が確認されたため、緊急に保有内容を確認してください。"],
            ["300株", "60%"],
        ),
    ],
    ids=["sell_ignores_stray_quantity", "critical_risk_ignores_stray_quantity"],
)
def test_non_partial_sell_never_displays_stray_quantity(
    category: NotificationCategory,
    is_critical: bool,
    base_overrides: dict[str, object],
    expected_present: list[str],
    expected_absent: list[str],
) -> None:
    """category!=PARTIAL_SELLへ誤ってsuggested_sell_shares/ratioが設定されても、
    数量セグメントを表示しない(旧Case R・Case Sを統合、テストコード削減対応
    2026-08)。SELL/CRITICAL_RISKいずれも、数量以外の本文情報(現在値・目安価格・
    理由)は従来どおり正常に表示され続けることをあわせて確認する
    (defense-in-depth Regression、再コードレビュー対応2026-08指摘2は維持)。
    """
    data = _base(
        category=category,
        suggested_sell_shares=300,
        suggested_sell_ratio=0.60,
        **base_overrides,
    )
    text = format_notification_text(data, is_critical_risk=is_critical)
    for expected in expected_present:
        assert expected in text
    for unexpected in expected_absent:
        assert unexpected not in text


# test_partial_sell_none_shares_no_segment()は上記test_case_j_no_shares_no_
# quantity_segment()と同一のArrange/Act/Assert(PARTIAL_SELL+shares=None+
# ratio=None→「株」非表示)であったため削除した(テストコード削減対応2026-08)。
# 当該観点はCase Jがそのまま維持している。
