"""ウォッチリスト自動追加機能向けのスクリーニングデータ取得抽象化。

BUY判定パイプラインが使う`stock_snapshot_service.build_stock_snapshot()`は
適正価格算出等スクリーニングには不要な計算も含む重い処理だが、既存データ取得
経路を再利用するという方針(要求仕様§2)に基づき、v1ではこれをそのまま利用する
(`StockSnapshotScreeningDataProvider`)。

ウォッチリスト自動運用の改善(高速化、計画Part B-2)で、`LightweightScreeningDataProvider`
を追加した。`multi_style_monitoring`Policyが実際に参照する項目のみを取得する
(株価・財務・配当・平均売買代金20日・開示情報30日の5回のみ。株価ヒストリー・
ベンチマークヒストリー・Historical Valuation・Fair Value/DCF・Entry/Exit Price
Range・Market/Sector/Environment・Earnings Surprise/Trend・次回決算日は取得しない)。
判定ロジックの二重実装を避けるため、`WatchlistScreeningInput`の構築自体は
両Providerが共通の`_build_screening_input()`を呼ぶ形にし、`classify_stock_type()`・
`to_seasonally_adjusted_series()`・`has_severe_earnings_decline()`・
`detect_disclosure_risk_keywords()`・`compute_dividend_yield_pct`/
`compute_annual_benefit_value`/`compute_benefit_yield_pct`という既存ドメイン層の
純粋関数(StockSnapshot非依存)をそのまま呼ぶ。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.classification.stock_type import classify_stock_type
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.common import BenefitUtilityCoefficients, DataSourceReference
from jstock_advisor.domain.financial_series import to_seasonally_adjusted_series
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.screening.rules import detect_disclosure_risk_keywords
from jstock_advisor.domain.signals.buy_signal import has_severe_earnings_decline
from jstock_advisor.domain.valuation.yield_calc import (
    compute_annual_benefit_value,
    compute_benefit_yield_pct,
    compute_dividend_yield_pct,
)
from jstock_advisor.interfaces.disclosure import DisclosureAvailability
from jstock_advisor.interfaces.types import DividendInfo, FinancialSummary, ShareholderBenefit
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot
from jstock_advisor.services.yfinance_rate_limit import call_with_rate_limit_retry

# WatchlistScreeningInputの必須項目・スコア項目の分類(要求仕様§5・§8)。
# 必須条件用フィールド(is_debt_excess等)はStockSnapshot取得できた時点で常にbool値
# (デフォルトFalse)を持つため、実際に欠損しうるのはこの2つのみ。
REQUIRED_FIELD_NAMES = ("shares_outstanding", "operating_cashflow")
SCORING_FIELD_NAMES = (
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
    # --- ウォッチリスト自動追加基準の再設計(2026-08)で追加。multi_style_monitoring
    # Policy専用。既存のBUY一次スクリーニング(domain/screening/rules.py)・
    # 銘柄タイプ分類(domain/classification/stock_type.py)がStockSnapshot上で
    # 既に算出済みの値をそのまま伝播するだけで、ここで新たな判定ロジックは
    # 実装しない(高配当条件に偏らない5タイプ判定・ハード除外の共通化のため)。
    stock_type_classification: StockTypeClassification
    avg_trading_value: Decimal | None
    disclosure_risk_keywords_found: list[str]
    severe_earnings_decline: bool


# Issue #53 Phase B2: 開示情報を調査できなかった場合に必須項目欠損として扱う名前。
# 既存のmissing_required_fields → ExclusionReason.DATA_INSUFFICIENT 経路を再利用し、
# 新しい除外理由を増やさない(「開示リスク検出」とは別物として扱うため、
# DISCLOSURE_RISKには決して倒さない)。
DISCLOSURE_AVAILABILITY_FIELD_NAME = "disclosure_availability"


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
    # --- 候補ユニバース本格対応(2026-08、5節)で追加、運用ハードニング3節で
    # 429以外(403/5xx/タイムアウト/接続切断/yfinance固有例外等)へ一般化。
    # DATA_ERRORがデータ提供元障害の疑いによるものかどうかを区別し、バッチ集計で
    # 「障害疑い件数/率」を算出できるようにする(10節のABORTED判定に使う)。
    # 提供元障害以外の理由によるDATA_ERRORでは常にFalse。
    is_provider_failure_suspected: bool = False


class ScreeningDataProvider(Protocol):
    def get_screening_input(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult: ...


def _build_screening_input(
    *,
    stock_code: str,
    financial: FinancialSummary,
    current_price: Decimal,
    dividend: DividendInfo,
    benefit: ShareholderBenefit | None,
    dividend_yield_pct: float | None,
    benefit_yield_pct: float | None,
    next_earnings_date: dt.date | None,
    stock_type_classification: StockTypeClassification,
    avg_trading_value: Decimal | None,
    disclosure_risk_keywords_found: list[str],
    severe_earnings_decline: bool,
    disclosure_available: bool,
) -> WatchlistScreeningInput:
    """`StockSnapshotScreeningDataProvider`/`LightweightScreeningDataProvider`が
    共通で使う、WatchlistScreeningInput組み立てロジック本体(ロジックの二重実装を
    避けるため、ここに1箇所だけ存在する)。
    """
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
        for name in REQUIRED_FIELD_NAMES
        if getattr(financial, name, None) is None
    ]
    if not disclosure_available:
        # 開示情報を調査できていない = 重大リスク開示の有無が不明。評価不能
        # (DATA_INSUFFICIENT)として扱い、候補へ通さない(Issue #53 Phase B2)。
        missing_required.append(DISCLOSURE_AVAILABILITY_FIELD_NAME)

    consecutive_increase_years = dividend.consecutive_dividend_increase_years
    scoring_values = {
        "dividend_yield_pct": dividend_yield_pct,
        "equity_ratio_pct": financial.equity_ratio_pct,
        "payout_ratio_pct": financial.payout_ratio_pct,
        "consecutive_dividend_increase_years": consecutive_increase_years,
        "shareholder_benefit_yield_pct": benefit_yield_pct,
    }
    # 運用ハードニング第2弾4節: 株主優待制度自体が無い銘柄(benefit is None、
    # 市場の大多数を占める)のshareholder_benefit_yield_pct=Noneは「正常に値が
    # 存在しない」ケースであり、データ品質低下(欠損)として数えない。優待はあるが
    # 利回りが算出できない場合(benefit is not None)のみ欠損として扱う。
    # この区別が無いと、優待の無い銘柄が他に1項目でも欠損した場合に
    # max_missing_fields(既定1)を超えてDATA_INSUFFICIENT除外されてしまう
    # (HighDividendFinancialHealthPolicyの除外判定に影響する既知の不具合の修正)。
    missing_scoring = [
        name
        for name in SCORING_FIELD_NAMES
        if scoring_values[name] is None
        and not (name == "shareholder_benefit_yield_pct" and benefit is None)
    ]

    return WatchlistScreeningInput(
        stock_code=stock_code,
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
        dividend_yield_pct=dividend_yield_pct,
        shareholder_benefit_exists=benefit is not None,
        shareholder_benefit_yield_pct=benefit_yield_pct,
        is_dividend_cut_announced=dividend.is_dividend_cut_announced,
        is_dividend_omission_announced=dividend.is_dividend_omission_announced,
        is_debt_excess=financial.is_debt_excess,
        is_deficit=financial.is_deficit,
        is_going_concern_doubt=financial.is_going_concern_doubt,
        next_earnings_date=next_earnings_date,
        missing_required_fields=missing_required,
        missing_scoring_fields=missing_scoring,
        stock_type_classification=stock_type_classification,
        avg_trading_value=avg_trading_value,
        disclosure_risk_keywords_found=disclosure_risk_keywords_found,
        severe_earnings_decline=severe_earnings_decline,
    )


def _to_screening_input(snapshot: StockSnapshot) -> WatchlistScreeningInput:
    return _build_screening_input(
        stock_code=snapshot.stock_code,
        financial=snapshot.financial,
        current_price=snapshot.current_price,
        dividend=snapshot.dividend,
        benefit=snapshot.benefit,
        dividend_yield_pct=snapshot.dividend_yield_pct,
        benefit_yield_pct=snapshot.benefit_yield_pct,
        next_earnings_date=snapshot.next_earnings_date,
        stock_type_classification=snapshot.stock_type_classification,
        avg_trading_value=snapshot.avg_trading_value,
        disclosure_risk_keywords_found=snapshot.disclosure_risk_keywords_found,
        severe_earnings_decline=snapshot.severe_earnings_decline,
        disclosure_available=(
            snapshot.disclosure_availability is DisclosureAvailability.AVAILABLE
        ),
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
                is_provider_failure_suspected=retry_result.is_provider_failure_suspected,
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


class LightweightScreeningDataProvider:
    """計画Part B-2: `multi_style_monitoring`Policyが実際に参照する項目のみを
    取得する高速版。呼び出しは5回のみ(株価・財務・配当・平均売買代金20日・
    開示情報30日)+株主優待のローカルレジストリ参照。株価ヒストリー・
    ベンチマークヒストリー等、build_stock_snapshot()が算出する他の値は
    一切取得・計算しない。next_earnings_dateは判定に使われないため常にNone。
    """

    def __init__(self, providers: ProviderBundle, config: AppConfig) -> None:
        self._providers = providers
        self._config = config

    def get_screening_input(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        try:
            retry_result = call_with_rate_limit_retry(
                lambda: self._fetch_and_build(stock_code, now)
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
                is_provider_failure_suspected=retry_result.is_provider_failure_suspected,
            )
        assert retry_result.value is not None
        input_dto, error = retry_result.value

        if input_dto is None:
            return ScreeningDataResult(
                status=ScreeningDataStatus.NOT_FOUND,
                input=None,
                missing_fields=[],
                error_message=error,
            )

        return ScreeningDataResult(
            status=ScreeningDataStatus.OK,
            input=input_dto,
            missing_fields=input_dto.missing_required_fields + input_dto.missing_scoring_fields,
            error_message=None,
        )

    def _fetch_and_build(
        self, stock_code: str, now: dt.datetime
    ) -> tuple[WatchlistScreeningInput | None, str | None]:
        snap = self._providers.market_data.get_latest_price(stock_code)
        if snap is None:
            return None, "株価データを取得できません"

        financial = self._providers.financial_data.get_financial_summary(stock_code)
        if financial is None:
            return None, "財務データを取得できません"

        dividend = self._providers.dividend_data.get_dividend_info(
            stock_code, fiscal_year_end_month=financial.fiscal_year_end_month
        )
        if dividend is None:
            return None, (
                "配当データを取得できません"
                "(データ提供元(yfinance)から取得できなかったか、yfinanceとEDINETの配当額が"
                "株式分割等で説明できない水準まで乖離しており自動判定できないため除外しています。"
                "後者の場合、詳細はCloudWatch Logsの該当銘柄のwarningログをご確認ください)"
            )

        benefit = self._providers.shareholder_benefit.get_shareholder_benefit(stock_code)
        current_price = snap.close_price
        avg_trading_value = self._providers.market_data.get_average_trading_value(stock_code, 20)
        disclosure_result = self._providers.disclosure.get_disclosures(
            stock_code, evaluation_date_jst(now) - dt.timedelta(days=30)
        )
        disclosures = disclosure_result.disclosures

        coefficients = BenefitUtilityCoefficients(
            **self._config.scoring.shareholder_benefit_value.utility_coefficients_default.model_dump()
        )
        dividend_yield_pct = compute_dividend_yield_pct(
            dividend.forecast_annual_dividend_per_share, current_price
        )
        annual_benefit_value = compute_annual_benefit_value(benefit, coefficients)
        min_shares_required = benefit.min_shares_required if benefit is not None else 100
        benefit_yield_pct = compute_benefit_yield_pct(
            annual_benefit_value, min_shares_required, current_price
        )

        # multi_style_monitoringのGROWTH判定・severe_earnings_declineが必要とする
        # 直近四半期営業利益のみを季節調整する(営業CFの季節調整・DCF等、
        # build_stock_snapshot()の他の用途は不要なため計算しない)。
        period_ends = [q.quarter_end for q in financial.recent_quarters]
        raw_operating_incomes = [q.operating_income for q in financial.recent_quarters]
        adjusted_operating_incomes = to_seasonally_adjusted_series(
            raw_operating_incomes, period_ends
        )
        quarterly_operating_incomes = [v for v in adjusted_operating_incomes if v is not None]
        severe_earnings_decline = has_severe_earnings_decline(quarterly_operating_incomes)

        data_sources: list[DataSourceReference] = [snap.source, financial.source, dividend.source]
        if benefit is not None:
            data_sources.append(benefit.source)

        keywords_found = detect_disclosure_risk_keywords(
            disclosures, self._config.sell.disclosure_risk_keywords
        )

        stock_type_classification = classify_stock_type(
            financial=financial,
            dividend_yield_pct=dividend_yield_pct,
            current_price=current_price,
            quarterly_operating_incomes=quarterly_operating_incomes,
            disclosures=disclosures,
            now=now,
            config=self._config.stock_classification,
            data_sources=data_sources,
            dividend=dividend,
        )

        input_dto = _build_screening_input(
            stock_code=stock_code,
            financial=financial,
            current_price=current_price,
            dividend=dividend,
            benefit=benefit,
            dividend_yield_pct=dividend_yield_pct,
            benefit_yield_pct=benefit_yield_pct,
            next_earnings_date=None,
            stock_type_classification=stock_type_classification,
            avg_trading_value=avg_trading_value,
            disclosure_risk_keywords_found=keywords_found,
            severe_earnings_decline=severe_earnings_decline,
            disclosure_available=disclosure_result.is_available,
        )
        return input_dto, None


def build_screening_data_provider(
    providers: ProviderBundle, config: AppConfig
) -> ScreeningDataProvider:
    """`config.watchlist_screening.screening_data_provider`に基づき、実際に
    使うProviderを生成する(計画Part B-2)。Dispatcher/Worker/CLIの3箇所の
    生成ロジックをここへ集約する。
    """
    provider_name = config.watchlist_screening.screening_data_provider
    if provider_name == "lightweight":
        return LightweightScreeningDataProvider(providers, config)
    return StockSnapshotScreeningDataProvider(providers, config)
