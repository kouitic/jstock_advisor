"""services/watchlist_score_detail.py: build_notification_detail()のテスト
(LINE通知品質改善)。

SCORE_CRITERION_DEFINITIONSがラベル・実測値抽出・config条件文生成を一元管理し、
呼び出し側にcriterion_keyによるif分岐が発生しないことを確認する。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.services.screening_data_provider import WatchlistScreeningInput
from jstock_advisor.services.watchlist_score_detail import (
    SCORE_CRITERION_DEFINITIONS,
    build_notification_detail,
)

_CONFIG = load_config()
_SCORING = _CONFIG.watchlist_screening.scoring


def _input(**overrides: object) -> WatchlistScreeningInput:
    defaults: dict[str, object] = dict(
        stock_code="1234",
        stock_name="テスト株式会社",
        security_type="STOCK",
        sector=None,
        industry=None,
        current_price=Decimal("3000"),
        shares_outstanding=Decimal("1000000"),
        market_cap=Decimal("3000000000"),
        forecast_eps=None,
        forecast_bps=None,
        current_per=None,
        current_pbr=None,
        equity_ratio_pct=65.0,
        operating_cashflow=Decimal("1000000"),
        payout_ratio_pct=45.2,
        consecutive_dividend_increase_years=5,
        dividend_yield_pct=6.6,
        shareholder_benefit_exists=False,
        shareholder_benefit_yield_pct=None,
        is_dividend_cut_announced=False,
        is_dividend_omission_announced=False,
        is_debt_excess=False,
        is_deficit=False,
        is_going_concern_doubt=False,
        next_earnings_date=None,
        missing_required_fields=[],
        missing_scoring_fields=[],
    )
    defaults.update(overrides)
    return WatchlistScreeningInput(**defaults)  # type: ignore[arg-type]


def test_build_notification_detail_maps_score_breakdown_to_criteria() -> None:
    score_breakdown = {
        "dividend_yield": 30.0,
        "equity_ratio": 20.0,
        "payout_ratio": 15.0,
        "dividend_growth": 7.5,
        "shareholder_benefit": 0.0,
    }
    detail = build_notification_detail("1234", score_breakdown, _input())

    assert detail is not None
    assert detail.stock_code == "1234"
    by_key = {c.criterion_key: c for c in detail.criteria}
    assert by_key["dividend_yield"].score == 30.0
    assert by_key["dividend_yield"].metric_value == "6.6%"
    assert by_key["equity_ratio"].metric_value == "65.0%"
    assert by_key["payout_ratio"].metric_value == "45.2%"
    assert by_key["dividend_growth"].metric_value == "5年連続"


def test_build_notification_detail_missing_metric_value_shows_none_not_guessed() -> None:
    """実測値が無い場合、値を推測せずmetric_value=Noneのまま返す
    (呼び出し側がスコア文字列で表示する)。"""
    score_breakdown = {"shareholder_benefit": 0.0}
    detail = build_notification_detail(
        "1234", score_breakdown, _input(shareholder_benefit_exists=False)
    )

    assert detail is not None
    by_key = {c.criterion_key: c for c in detail.criteria}
    assert by_key["shareholder_benefit"].metric_value is None
    assert by_key["shareholder_benefit"].score == 0.0


def test_build_notification_detail_covers_all_criterion_definitions() -> None:
    detail = build_notification_detail("1234", {}, _input())

    assert detail is not None
    assert {c.criterion_key for c in detail.criteria} == {
        d.criterion_key for d in SCORE_CRITERION_DEFINITIONS
    }


def test_describe_condition_follows_config_values() -> None:
    """describe_conditionがconfig値の変更に追随することを確認する
    (固定文言のハードコードでないこと)。"""
    dividend_yield_definition = next(
        d for d in SCORE_CRITERION_DEFINITIONS if d.criterion_key == "dividend_yield"
    )
    text = dividend_yield_definition.describe_condition(_SCORING)
    assert f"{_SCORING.dividend_yield.full_at_pct:.1f}" in text


def test_no_criterion_key_branching_in_definitions_iteration() -> None:
    """SCORE_CRITERION_DEFINITIONSは各criterionのlabel/extract_metric/
    describe_conditionをすべて保持しており、呼び出し側でcriterion_keyごとの
    分岐が不要であることを確認する(全定義が呼び出し可能であること)。"""
    for definition in SCORE_CRITERION_DEFINITIONS:
        assert callable(definition.extract_metric)
        assert callable(definition.describe_condition)
        assert definition.describe_condition(_SCORING)
