"""ウォッチリスト自動追加機能向けのスクリーニングデータ取得抽象化。

BUY判定パイプラインが使う`stock_snapshot_service.build_stock_snapshot()`は
適正価格算出等スクリーニングには不要な計算も含む重い処理だが、既存データ取得
経路を再利用するという方針(要求仕様§2)に基づき、v1ではこれをそのまま利用する。
将来より軽量な実装(LightweightScreeningDataProvider)へ差し替える際、この
ファイル内の変更のみで完結するよう、build_stock_snapshot()の呼び出しは
StockSnapshotScreeningDataProviderの1箇所に集約する。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from jstock_advisor.config.models import AppConfig
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot
from jstock_advisor.services.yfinance_rate_limit import call_with_rate_limit_retry

# WatchlistScreeningInputの必須項目・スコア項目の分類(要求仕様§5・§8)。
# 必須条件用フィールド(is_debt_excess等)はStockSnapshot取得できた時点で常にbool値
# (デフォルトFalse)を持つため、実際に欠損しうるのはこの2つのみ。
_REQUIRED_FIELD_NAMES = ("shares_outstanding", "operating_cashflow")
_SCORING_FIELD_NAMES = (
    "dividend_yield_pct",
    "equity_ratio_pct",
    "payout_ratio_pct",
    "consecutive_dividend_increase_years",
    "shareholder_benefit_yield_pct",
)


@dataclass(frozen=True)
class WatchlistScreeningInput:
    stock_code: str
    stock_name: str | None
    security_type: str
    sector: str | None
    industry: str | None
    current_price: Decimal
    shares_outstanding: Decimal | None
    market_cap: Decimal | None
    forecast_eps: Decimal | None
    forecast_bps: Decimal | None
    current_per: Decimal | None
    current_pbr: Decimal | None
    equity_ratio_pct: float | None
    operating_cashflow: Decimal | None
    payout_ratio_pct: float | None
    consecutive_dividend_increase_years: int | None
    dividend_yield_pct: float | None
    shareholder_benefit_exists: bool
    shareholder_benefit_yield_pct: float | None
    is_dividend_cut_announced: bool
    is_dividend_omission_announced: bool
    is_debt_excess: bool
    is_deficit: bool
    is_going_concern_doubt: bool
    next_earnings_date: dt.date | None
    missing_required_fields: list[str]
    missing_scoring_fields: list[str]


class ScreeningDataStatus(StrEnum):
    OK = "OK"
    NOT_FOUND = "NOT_FOUND"
    DATA_ERROR = "DATA_ERROR"


@dataclass(frozen=True)
class ScreeningDataResult:
    status: ScreeningDataStatus
    input: WatchlistScreeningInput | None
    missing_fields: list[str]
    error_message: str | None
    # --- 候補ユニバース本格対応(2026-08、5節)で追加。DATA_ERRORが429疑いによる
    # ものかどうかを区別し、バッチ集計で「429疑い件数/率」を算出できるようにする
    # (10節のABORTED判定に使う)。429以外の理由によるDATA_ERRORでは常にFalse。
    is_rate_limit_suspected: bool = False


class ScreeningDataProvider(Protocol):
    def get_screening_input(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult: ...


def _to_screening_input(snapshot: StockSnapshot) -> WatchlistScreeningInput:
    financial = snapshot.financial
    current_price = snapshot.current_price

    market_cap: Decimal | None = None
    if financial.shares_outstanding is not None:
        market_cap = financial.shares_outstanding * current_price

    current_per: Decimal | None = None
    if financial.forecast_eps is not None and financial.forecast_eps != 0:
        current_per = current_price / financial.forecast_eps

    current_pbr: Decimal | None = None
    if financial.forecast_bps is not None and financial.forecast_bps != 0:
        current_pbr = current_price / financial.forecast_bps

    missing_required = [
        name
        for name in _REQUIRED_FIELD_NAMES
        if getattr(financial, name, None) is None
    ]

    consecutive_increase_years = snapshot.dividend.consecutive_dividend_increase_years
    scoring_values = {
        "dividend_yield_pct": snapshot.dividend_yield_pct,
        "equity_ratio_pct": financial.equity_ratio_pct,
        "payout_ratio_pct": financial.payout_ratio_pct,
        "consecutive_dividend_increase_years": consecutive_increase_years,
        "shareholder_benefit_yield_pct": snapshot.benefit_yield_pct,
    }
    missing_scoring = [name for name in _SCORING_FIELD_NAMES if scoring_values[name] is None]

    return WatchlistScreeningInput(
        stock_code=snapshot.stock_code,
        stock_name=financial.stock_name,
        security_type=financial.security_type,
        sector=financial.sector,
        industry=financial.industry,
        current_price=current_price,
        shares_outstanding=financial.shares_outstanding,
        market_cap=market_cap,
        forecast_eps=financial.forecast_eps,
        forecast_bps=financial.forecast_bps,
        current_per=current_per,
        current_pbr=current_pbr,
        equity_ratio_pct=financial.equity_ratio_pct,
        operating_cashflow=financial.operating_cashflow,
        payout_ratio_pct=financial.payout_ratio_pct,
        consecutive_dividend_increase_years=consecutive_increase_years,
        dividend_yield_pct=snapshot.dividend_yield_pct,
        shareholder_benefit_exists=snapshot.benefit is not None,
        shareholder_benefit_yield_pct=snapshot.benefit_yield_pct,
        is_dividend_cut_announced=snapshot.dividend.is_dividend_cut_announced,
        is_dividend_omission_announced=snapshot.dividend.is_dividend_omission_announced,
        is_debt_excess=financial.is_debt_excess,
        is_deficit=financial.is_deficit,
        is_going_concern_doubt=financial.is_going_concern_doubt,
        next_earnings_date=snapshot.next_earnings_date,
        missing_required_fields=missing_required,
        missing_scoring_fields=missing_scoring,
    )


class StockSnapshotScreeningDataProvider:
    def __init__(self, providers: ProviderBundle, config: AppConfig) -> None:
        self._providers = providers
        self._config = config

    def get_screening_input(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        # 429対応(案B、5節): build_stock_snapshot()全体をcall_with_rate_limit_retry()で
        # 包み、429疑いの例外のみ再試行する。build_stock_snapshot()自体・共有yfinance
        # Provider実装は一切変更しない(欠点は同関数のdocstring参照)。429疑いでない
        # 例外はcall_with_rate_limit_retry()が再送出するため、従来どおりここで
        # 捕捉してDATA_ERRORとして扱う(この層の例外処理契約自体は変更しない)。
        try:
            retry_result = call_with_rate_limit_retry(
                lambda: build_stock_snapshot(self._providers, stock_code, now, self._config)
            )
        except Exception as exc:  # noqa: BLE001 - 将来のretry判定用にstatusで区別するため意図的に捕捉
            return ScreeningDataResult(
                status=ScreeningDataStatus.DATA_ERROR,
                input=None,
                missing_fields=[],
                error_message=str(exc),
            )
        if retry_result.error is not None:
            return ScreeningDataResult(
                status=ScreeningDataStatus.DATA_ERROR,
                input=None,
                missing_fields=[],
                error_message=str(retry_result.error),
                is_rate_limit_suspected=retry_result.is_rate_limit_suspected,
            )
        assert retry_result.value is not None
        snapshot, error = retry_result.value

        if snapshot is None:
            return ScreeningDataResult(
                status=ScreeningDataStatus.NOT_FOUND,
                input=None,
                missing_fields=[],
                error_message=error,
            )

        input_dto = _to_screening_input(snapshot)
        return ScreeningDataResult(
            status=ScreeningDataStatus.OK,
            input=input_dto,
            missing_fields=input_dto.missing_required_fields + input_dto.missing_scoring_fields,
            error_message=None,
        )
