"""保有判断スコア方式のバックテスト/リプレイ(実装プラン修正5)。

このシステムは財務・配当・優待データを「現在値」としてのみ保持しており、
過去の任意時点の財務スナップショットは保存していない(Phase0前提。過去の
株価時系列(HistoricalValuation)はあるが、スコアの入力である財務・配当・
優待データには時系列が無いため、真の意味での過去時点再現はできない)。

そのため本モジュールは2つのモードを提供する。

- **liveモード**(--start-date/--end-date省略時): 指定銘柄(または全保有銘柄)を
  現在のデータで旧方式(SellSignalService)・新方式(HoldingDecisionService)の
  両方にかけ、判定を並べて出力する(「今この瞬間、両エンジンはどう判定するか」の
  比較)。
- **replayモード**(--start-date/--end-date指定時): 過去に実際に保存された
  HoldingDecisionResult/Recommendation(shadow運用等で蓄積された実データ)を
  指定期間で抽出し、そのまま並べて出力する(過去に実際に何が起きたかの再生)。
  蓄積が無い期間を指定した場合は素直に0件と報告する(推測で埋め合わせない)。
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import AccountType, ExecutionPlanReason
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_CSV_HEADER = (
    "date",
    "stock_code",
    "source",
    "legacy_recommendation_type",
    "legacy_notified",
    "new_score",
    "new_category",
    "new_notified",
)


@dataclass(frozen=True)
class BacktestRow:
    stock_code: str
    evaluated_at: dt.datetime
    source: str  # "live" | "history"
    legacy_recommendation_type: str | None
    legacy_notified: bool
    new_score: float | None
    new_category: str | None
    new_notified: bool

    def as_csv_row(self) -> tuple[str, ...]:
        return (
            self.evaluated_at.date().isoformat(),
            self.stock_code,
            self.source,
            self.legacy_recommendation_type or "",
            str(self.legacy_notified),
            "" if self.new_score is None else f"{self.new_score:.2f}",
            self.new_category or "",
            str(self.new_notified),
        )


def placeholder_holding(stock_code: str, now: dt.datetime) -> Holding:
    """保有していない銘柄をbacktest対象にする場合のダミー保有データ。

    保有判断スコアは現在株価・取得単価・含み益率を一切入力に含めないため
    (実装プラン10節)、これらの値がスコア計算結果へ影響することはない。
    """
    return Holding(
        stock_code=stock_code,
        stock_name=stock_code,
        shares=100,
        average_purchase_price=Decimal("1"),
        total_purchase_amount=Decimal("100"),
        first_purchase_date=now.date(),
        last_purchase_date=now.date(),
        account_type=AccountType.GENERAL,
        created_at=now,
        updated_at=now,
    )


def resolve_target_stock_codes(
    explicit_stock_codes: list[str], portfolio_service: PortfolioService | None = None
) -> list[str]:
    """--stock-codeが1件以上指定されていればそれを使い、無指定なら全保有銘柄を使う。"""
    if explicit_stock_codes:
        return list(dict.fromkeys(explicit_stock_codes))  # 重複除去・順序維持
    portfolio = portfolio_service or PortfolioService()
    return [h.stock_code for h in portfolio.list_holdings()]


def run_live_comparison(
    stock_codes: list[str],
    providers: ProviderBundle,
    config: AppConfig,
    now: dt.datetime,
    sell_service: SellSignalService | None = None,
    holding_decision_service: HoldingDecisionService | None = None,
    portfolio_service: PortfolioService | None = None,
) -> list[BacktestRow]:
    """指定銘柄(または保有中の全銘柄)を現在のデータで新旧両エンジンにかける。"""
    sell_service = sell_service or SellSignalService(providers=providers, config=config)
    holding_decision_service = holding_decision_service or HoldingDecisionService(providers, config)
    portfolio = portfolio_service or PortfolioService()

    rows: list[BacktestRow] = []
    for stock_code in stock_codes:
        snapshot, error = build_stock_snapshot(providers, stock_code, now, config)
        if snapshot is None:
            rows.append(
                BacktestRow(
                    stock_code=stock_code,
                    evaluated_at=now,
                    source="live",
                    legacy_recommendation_type=f"DATA_ERROR: {error}",
                    legacy_notified=False,
                    new_score=None,
                    new_category=None,
                    new_notified=False,
                )
            )
            continue

        holding = portfolio.get_holding(stock_code) or placeholder_holding(stock_code, now)

        legacy_outcome = sell_service.analyze(holding, now, snapshot=snapshot)
        legacy_type = (
            legacy_outcome.recommendation.recommendation_type.value
            if legacy_outcome.recommendation is not None
            else "HOLD"
        )

        new_outcome = holding_decision_service.evaluate(
            holding, now, ExecutionPlanReason.NORMAL_SHADOW, snapshot=snapshot
        )
        if new_outcome.integrity_error or new_outcome.result is None:
            new_score: float | None = None
            new_category: str | None = (
                "DATA_INTEGRITY_ERROR" if new_outcome.integrity_error else None
            )
            new_notified = False
        else:
            new_score = new_outcome.result.final_score
            new_category = new_outcome.result.category.value
            new_notified = new_outcome.result.should_notify

        rows.append(
            BacktestRow(
                stock_code=stock_code,
                evaluated_at=now,
                source="live",
                legacy_recommendation_type=legacy_type,
                legacy_notified=legacy_outcome.recommendation is not None,
                new_score=new_score,
                new_category=new_category,
                new_notified=new_notified,
            )
        )
    return rows


def run_history_replay(
    stock_codes: list[str],
    start_date: dt.date,
    end_date: dt.date,
    holding_decision_result_repo: HoldingDecisionResultRepository | None = None,
    recommendation_repo: RecommendationRepository | None = None,
) -> list[BacktestRow]:
    """指定期間に実際に保存されたHoldingDecisionResult/Recommendationを再生する。

    蓄積が無ければ空リストを返す(推測で埋め合わせない)。
    """
    hd_repo = holding_decision_result_repo or HoldingDecisionResultRepository()
    rec_repo = recommendation_repo or RecommendationRepository()

    start_dt = dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.UTC)
    end_dt = dt.datetime.combine(end_date, dt.time.max, tzinfo=dt.UTC)

    stock_code_filter = set(stock_codes) if stock_codes else None

    hd_results = [
        r
        for r in hd_repo.list_between(start_dt, end_dt)
        if stock_code_filter is None or r.stock_code in stock_code_filter
    ]
    recommendations = [
        r
        for r in rec_repo.list_all()
        if start_date <= r.recommended_at.date() <= end_date
        and (stock_code_filter is None or r.stock_code in stock_code_filter)
    ]
    # 同一銘柄・同一日のRecommendationを新方式側の行に対応付ける(shadow運用等で
    # 新旧が同一サイクルで走った場合のみ両方埋まる。日付一致のみによる簡易対応付けの
    # ため、同日に複数回評価された場合は最初に見つかったものを使う)。
    recs_by_stock_and_date: dict[tuple[str, dt.date], Recommendation] = {}
    for rec in recommendations:
        key = (rec.stock_code, rec.recommended_at.date())
        recs_by_stock_and_date.setdefault(key, rec)

    rows: list[BacktestRow] = []
    matched_keys: set[tuple[str, dt.date]] = set()
    for result in hd_results:
        key = (result.stock_code, result.evaluated_at.date())
        legacy_rec = recs_by_stock_and_date.get(key)
        if legacy_rec is not None:
            matched_keys.add(key)
        rows.append(
            BacktestRow(
                stock_code=result.stock_code,
                evaluated_at=result.evaluated_at,
                source="history",
                legacy_recommendation_type=(
                    legacy_rec.recommendation_type.value if legacy_rec is not None else None
                ),
                legacy_notified=legacy_rec is not None,
                new_score=result.final_score,
                new_category=result.category.value,
                new_notified=result.recommendation_id is not None,
            )
        )

    # 新方式の評価が無い(legacy/active一般銘柄で新方式が動いていない)日でも、
    # 旧方式のRecommendationだけは行として残す。
    for (stock_code, rec_date), rec in recs_by_stock_and_date.items():
        if (stock_code, rec_date) in matched_keys:
            continue
        rows.append(
            BacktestRow(
                stock_code=stock_code,
                evaluated_at=rec.recommended_at,
                source="history",
                legacy_recommendation_type=rec.recommendation_type.value,
                legacy_notified=True,
                new_score=None,
                new_category=None,
                new_notified=False,
            )
        )

    return sorted(rows, key=lambda r: (r.evaluated_at, r.stock_code))


def write_backtest_csv(rows: list[BacktestRow], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for row in rows:
            writer.writerow(row.as_csv_row())
