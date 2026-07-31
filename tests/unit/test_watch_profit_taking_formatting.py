"""WATCH(監視)判定通知の純関数群のテスト(2026-07仕様レビュー対応、要求仕様§4・§7)。

見出しの状態別出し分け(_resolve_watch_profit_taking_title)と、手法間乖離が
大きい場合の注意書き(_fair_value_dispersion_warning_lines)を、通知全文の
組み立てから切り離して単体で検証する。
"""

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.services.line_notification_service import (
    _fair_value_dispersion_warning_lines,
    _is_fair_value_dispersion_large,
    _resolve_watch_profit_taking_title,
)

_NOW = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.UTC)
_THRESHOLD = 2.0


def _make_watch_recommendation(**overrides: object) -> Recommendation:
    defaults: dict[str, object] = {
        "recommendation_id": "rec-1",
        "stock_code": "2269",
        "stock_name": "明治ホールディングス",
        "recommended_at": _NOW,
        "recommendation_type": RecommendationType.WATCH,
        "price_at_recommendation": Decimal("3500"),
        "confidence": ConfidenceLevel.MEDIUM,
        "rule_version": "v1-mvp",
        "fair_value_bear": Decimal("3000"),
        "fair_value_neutral": Decimal("3300"),
        "fair_value_bull": Decimal("3600"),
        "fair_value_overall_confidence": ConfidenceLevel.MEDIUM,
        "fair_value_spread_ratio": 1.2,
    }
    defaults.update(overrides)
    return Recommendation(**defaults)


def test_title_earnings_pending_overrides_other_conditions() -> None:
    rec = _make_watch_recommendation(
        recommendation_type=RecommendationType.WATCH_BEFORE_EARNINGS,
        fair_value_overall_confidence=ConfidenceLevel.LOW,
    )
    assert _resolve_watch_profit_taking_title(rec, _THRESHOLD) == "適正価格超過・決算後に再評価"


def test_title_data_insufficient_when_no_fair_value_at_all() -> None:
    rec = _make_watch_recommendation(
        fair_value_bear=None, fair_value_neutral=None, fair_value_bull=None
    )
    assert _resolve_watch_profit_taking_title(rec, _THRESHOLD) == "保有継続・データ確認待ち"


def test_title_dispersion_large_when_confidence_low() -> None:
    rec = _make_watch_recommendation(fair_value_overall_confidence=ConfidenceLevel.LOW)
    assert _resolve_watch_profit_taking_title(rec, _THRESHOLD) == "適正価格のばらつき大・継続監視"


def test_title_dispersion_large_when_spread_ratio_exceeds_threshold() -> None:
    rec = _make_watch_recommendation(fair_value_spread_ratio=2.5)
    assert _resolve_watch_profit_taking_title(rec, _THRESHOLD) == "適正価格のばらつき大・継続監視"


def test_title_default_when_confidence_ok_and_spread_small() -> None:
    rec = _make_watch_recommendation()
    assert _resolve_watch_profit_taking_title(rec, _THRESHOLD) == "割高水準を監視"


def test_is_dispersion_large_false_for_medium_confidence_and_small_spread() -> None:
    rec = _make_watch_recommendation()
    assert _is_fair_value_dispersion_large(rec, _THRESHOLD) is False


def _methods(*pairs: tuple[str, str]) -> list[dict[str, object]]:
    return [{"method": name, "fair_value": Decimal(value)} for name, value in pairs]


def test_dispersion_warning_identifies_outlier_method_dynamically() -> None:
    rec = _make_watch_recommendation(
        fair_value_overall_confidence=ConfidenceLevel.LOW,
        fair_value_methods=_methods(
            ("PER", "3100"), ("PBR", "3200"), ("DCF", "4500")
        ),
        price_at_recommendation=Decimal("3500"),
    )
    lines = _fair_value_dispersion_warning_lines(rec, _THRESHOLD)
    assert lines
    assert "DCFを除く適正価格は3,100円〜3,200円で、現在値3,500円はその上回っています" in "\n".join(
        lines
    )


def test_dispersion_warning_does_not_hardcode_method_name() -> None:
    # 最大値を持つ手法がDCF以外(例: 独自の残余利益モデル)でも、その手法名を動的に検出する
    rec = _make_watch_recommendation(
        fair_value_overall_confidence=ConfidenceLevel.LOW,
        fair_value_methods=_methods(
            ("PER", "3100"), ("PBR", "3200"), ("残余利益モデル", "4500")
        ),
        price_at_recommendation=Decimal("3500"),
    )
    lines = _fair_value_dispersion_warning_lines(rec, _THRESHOLD)
    assert any("残余利益モデルを除く" in line for line in lines)


def test_dispersion_warning_empty_when_confidence_ok_and_spread_small() -> None:
    rec = _make_watch_recommendation(
        fair_value_methods=_methods(("PER", "3100"), ("PBR", "3200"), ("DCF", "3300"))
    )
    assert _fair_value_dispersion_warning_lines(rec, _THRESHOLD) == []


def test_dispersion_warning_empty_when_fewer_than_three_methods() -> None:
    rec = _make_watch_recommendation(
        fair_value_overall_confidence=ConfidenceLevel.LOW,
        fair_value_methods=_methods(("PER", "3100"), ("DCF", "4500")),
    )
    assert _fair_value_dispersion_warning_lines(rec, _THRESHOLD) == []


def test_dispersion_warning_empty_when_max_value_not_a_true_outlier() -> None:
    # 最大値(3210)が他の手法(3100・3200)からほとんど突出していない場合、
    # 「除外して見る」意味が薄いため表示しない、という仕様ではなく、この関数は
    # 最大値が僅かでも他の最大を上回れば表示する。ここでは同値(突出なし)を検証する。
    rec = _make_watch_recommendation(
        fair_value_overall_confidence=ConfidenceLevel.LOW,
        fair_value_methods=_methods(("PER", "3200"), ("PBR", "3200"), ("DCF", "3200")),
    )
    assert _fair_value_dispersion_warning_lines(rec, _THRESHOLD) == []


def test_dispersion_warning_price_within_range_direction() -> None:
    rec = _make_watch_recommendation(
        fair_value_overall_confidence=ConfidenceLevel.LOW,
        fair_value_methods=_methods(("PER", "3100"), ("PBR", "3200"), ("DCF", "4500")),
        price_at_recommendation=Decimal("3150"),
    )
    lines = _fair_value_dispersion_warning_lines(rec, _THRESHOLD)
    assert any("範囲内です" in line for line in lines)


def test_dispersion_warning_price_below_range_direction() -> None:
    rec = _make_watch_recommendation(
        fair_value_overall_confidence=ConfidenceLevel.LOW,
        fair_value_methods=_methods(("PER", "3100"), ("PBR", "3200"), ("DCF", "4500")),
        price_at_recommendation=Decimal("3000"),
    )
    lines = _fair_value_dispersion_warning_lines(rec, _THRESHOLD)
    assert any("下回っています" in line for line in lines)
