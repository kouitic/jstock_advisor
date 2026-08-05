"""保有判断スコアのバックテスト/リプレイのテスト(実装プラン修正5)。

live比較モード(--start-date未指定)とreplayモード(--start-date指定)の
両方について、指定銘柄・全銘柄・CSV出力・空データ時の挙動を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.holding_decision_backtest_service import (
    resolve_target_stock_codes,
    run_history_replay,
    run_live_comparison,
    write_backtest_csv,
)
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle

_CFG = load_config()
_NOW = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
_PROVIDERS = build_mock_provider_bundle(_NOW)


def _holding(stock_code: str) -> Holding:
    return Holding(
        stock_code=stock_code,
        stock_name="x",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _recommendation(stock_code: str, recommended_at: dt.datetime) -> Recommendation:
    return Recommendation(
        recommendation_id=f"rec-{stock_code}-{recommended_at.isoformat()}",
        stock_code=stock_code,
        stock_name="test",
        recommended_at=recommended_at,
        recommendation_type=RecommendationType.SELL,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
    )


# ===== resolve_target_stock_codes =====


def test_resolve_target_stock_codes_uses_explicit_list_when_given(store_dir: Path):
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    result = resolve_target_stock_codes(["2914", "9861"], portfolio_service=portfolio)
    assert result == ["2914", "9861"]


def test_resolve_target_stock_codes_deduplicates_while_preserving_order(store_dir: Path):
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    result = resolve_target_stock_codes(["2914", "9861", "2914"], portfolio_service=portfolio)
    assert result == ["2914", "9861"]


def test_resolve_target_stock_codes_falls_back_to_all_holdings(store_dir: Path):
    holding_repo = HoldingRepository(store_dir=store_dir)
    portfolio = PortfolioService(holding_repository=holding_repo)
    holding_repo.upsert(_holding("2914"))
    holding_repo.upsert(_holding("9861"))
    result = resolve_target_stock_codes([], portfolio_service=portfolio)
    assert set(result) == {"2914", "9861"}


def test_resolve_target_stock_codes_empty_when_no_holdings_and_no_explicit_codes(store_dir: Path):
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    assert resolve_target_stock_codes([], portfolio_service=portfolio) == []


# ===== run_live_comparison =====


def test_live_comparison_returns_one_row_per_stock_code():
    rows = run_live_comparison(["2914", "9861"], _PROVIDERS, _CFG, _NOW)
    assert [r.stock_code for r in rows] == ["2914", "9861"]
    assert all(r.source == "live" for r in rows)


def test_live_comparison_row_has_legacy_and_new_engine_fields():
    rows = run_live_comparison(["2914"], _PROVIDERS, _CFG, _NOW)
    row = rows[0]
    assert row.legacy_recommendation_type is not None
    assert row.new_score is not None
    assert row.new_category is not None
    assert isinstance(row.new_notified, bool)
    assert isinstance(row.legacy_notified, bool)


def test_live_comparison_works_for_stock_not_in_portfolio(store_dir: Path):
    """保有していない銘柄でも、ダミー保有データでスコア計算自体は行える
    (保有判断スコアは取得単価・株数を入力に使わないため)。"""
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    rows = run_live_comparison(["2914"], _PROVIDERS, _CFG, _NOW, portfolio_service=portfolio)
    assert len(rows) == 1
    assert rows[0].new_score is not None


# ===== run_history_replay =====


def test_history_replay_returns_empty_when_no_data(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rows = run_history_replay(
        ["2914"],
        dt.date(2020, 1, 1),
        dt.date(2020, 12, 31),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows == []


def test_history_replay_includes_recommendations_within_range(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rec_repo.save(_recommendation("2914", dt.datetime(2026, 6, 15, tzinfo=dt.UTC)))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert len(rows) == 1
    assert rows[0].legacy_notified is True
    assert rows[0].legacy_recommendation_type == "SELL"
    assert rows[0].new_score is None  # このケースでは新方式の記録は無い


def test_history_replay_excludes_data_outside_range(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rec_repo.save(_recommendation("2914", dt.datetime(2025, 1, 1, tzinfo=dt.UTC)))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 1, 1),
        dt.date(2026, 12, 31),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows == []


def test_history_replay_filters_by_stock_code(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rec_repo.save(_recommendation("2914", dt.datetime(2026, 6, 15, tzinfo=dt.UTC)))
    rec_repo.save(_recommendation("9861", dt.datetime(2026, 6, 15, tzinfo=dt.UTC)))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert [r.stock_code for r in rows] == ["2914"]


def test_history_replay_no_stock_filter_includes_all(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rec_repo.save(_recommendation("2914", dt.datetime(2026, 6, 15, tzinfo=dt.UTC)))
    rec_repo.save(_recommendation("9861", dt.datetime(2026, 6, 16, tzinfo=dt.UTC)))

    rows = run_history_replay(
        [],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert {r.stock_code for r in rows} == {"2914", "9861"}


# ===== CSV出力 =====


def test_write_backtest_csv_round_trips_live_rows(tmp_path: Path):
    rows = run_live_comparison(["2914"], _PROVIDERS, _CFG, _NOW)
    csv_path = tmp_path / "backtest.csv"
    write_backtest_csv(rows, csv_path)

    content = csv_path.read_text(encoding="utf-8-sig")
    lines = content.strip().splitlines()
    assert lines[0] == (
        "date,stock_code,source,legacy_recommendation_type,legacy_notified,"
        "new_score,new_category,new_notified"
    )
    assert len(lines) == 2
    assert "2914" in lines[1]


def test_write_backtest_csv_handles_empty_rows(tmp_path: Path):
    csv_path = tmp_path / "empty.csv"
    write_backtest_csv([], csv_path)
    content = csv_path.read_text(encoding="utf-8-sig")
    assert content.strip().splitlines() == [
        "date,stock_code,source,legacy_recommendation_type,legacy_notified,"
        "new_score,new_category,new_notified"
    ]
