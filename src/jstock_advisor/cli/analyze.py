"""分析CLIコマンド(要求仕様3節・23節)。MVPではモックProviderで動作する。"""

from __future__ import annotations

import datetime as dt
import logging

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import DecisionType, RecommendationType, buy_action_label
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.line.client import LineClient, build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.providers.mock_fixtures import MOCK_STOCKS
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.decision_snapshot_service import save_decision_snapshot_safely
from jstock_advisor.services.disclosure_check_service import DisclosureCheckService
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import (
    build_mock_provider_bundle,
    build_real_provider_bundle,
)
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.watchlist_service import WatchlistService

logger = logging.getLogger(__name__)

app = typer.Typer(help="買い候補・保有銘柄・ウォッチリストの分析(--sourceでmock/realを切替)")

_DISCLAIMER = "※最終的な投資判断は利用者が行ってください。"

_NOTIFY_HELP = "LINEへ通知する(LINE_CHANNEL_ACCESS_TOKEN/LINE_USER_ID未設定時は標準出力に表示のみ)"
_SOURCE_HELP = (
    "データ提供元: mock(モックデータ、既定)/ real(yfinance+EDINETの実データ。"
    "株主優待はjstock shareholder-benefitで手動登録した内容を使用。"
    "適時開示はEDINET臨時報告書+yfinance決算予定日を使用。決算短信は取得不可)"
)


def _build_providers(source: str, now: dt.datetime, config: AppConfig) -> ProviderBundle:
    if source == "real":
        return build_real_provider_bundle(now, config)
    return build_mock_provider_bundle(now)


def _build_notification_service(
    line_client: LineClient,
    recommendation_repo: RecommendationRepository,
    config: AppConfig,
) -> LineNotificationService:
    return LineNotificationService(
        line_client=line_client,
        notification_log_repository=NotificationLogRepository(),
        # LINE通知dedupの原子化(Issue #17): NORMAL実行の送信決定を原子的に
        # 一意化するclaimリポジトリ(VALIDATION/DRY_RUNでは使用されない)。
        notification_claim_repository=NotificationClaimRepository(),
        recommendation_repository=recommendation_repo,
        config=config,
    )


@app.command("buy-candidates")
def analyze_buy_candidates(
    stock_codes: list[str] | None = typer.Argument(
        None, help="対象銘柄コード(mock時省略でモックデータの全銘柄。real時は必須)"
    ),
    notify: bool = typer.Option(False, "--notify/--no-notify", help=_NOTIFY_HELP),
    source: str = typer.Option("mock", "--source", help=_SOURCE_HELP),
) -> None:
    """買い候補を分析し、推奨買値とともに一覧表示・保存する。"""
    if source not in ("mock", "real"):
        raise typer.BadParameter("--source は mock または real を指定してください")
    if source == "real" and not stock_codes:
        raise typer.BadParameter("--source real の場合、対象銘柄コードを指定してください")

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = _build_providers(source, now, config)
    service = BuySignalService(providers=providers, config=config, business_calendar=calendar)
    repo = RecommendationRepository()
    notification_service = (
        _build_notification_service(build_line_client_from_env(), repo, config) if notify else None
    )

    codes = stock_codes or list(MOCK_STOCKS.keys())
    found_any = False
    for code in codes:
        outcome = service.analyze(code, now)
        if outcome.data_error:
            typer.echo(f"[DATA_ERROR] {code}: {outcome.data_error}")
            if notification_service is not None:
                notification_service.notify_data_error(code, outcome.data_error, now)
            continue
        if outcome.recommendation is None:
            continue
        found_any = True
        repo.save(outcome.recommendation)
        # 判定精度向上機能Phase A: DecisionSnapshotを記録する(スコア項目はPhase Bまで
        # 全てNone)。失敗しても既存の保存・通知には一切影響しない。
        save_decision_snapshot_safely(
            DecisionSnapshotRepository(), outcome.recommendation, DecisionType.BUY, logger
        )
        _print_buy_recommendation(outcome.recommendation)
        if notification_service is not None:
            sent = notification_service.notify_recommendation(outcome.recommendation, now)
            typer.echo(
                "  → LINE通知しました" if sent else "  → 前回と同内容のため通知をスキップしました"
            )

    if not found_any:
        typer.echo("本日買いを検討すべき銘柄はありませんでした。")
    typer.echo(_DISCLAIMER)


@app.command("watchlist")
def analyze_watchlist(
    notify: bool = typer.Option(False, "--notify/--no-notify", help=_NOTIFY_HELP),
    source: str = typer.Option("mock", "--source", help=_SOURCE_HELP),
) -> None:
    """ウォッチリスト銘柄が買い条件に該当するか分析する。"""
    if source not in ("mock", "real"):
        raise typer.BadParameter("--source は mock または real を指定してください")

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = _build_providers(source, now, config)
    service = BuySignalService(providers=providers, config=config, business_calendar=calendar)
    repo = RecommendationRepository()
    notification_service = (
        _build_notification_service(build_line_client_from_env(), repo, config) if notify else None
    )

    items = WatchlistService().list_items()
    if not items:
        typer.echo("ウォッチリストは登録されていません。")
        return

    found_any = False
    for item in items:
        outcome = service.analyze(
            item.stock_code, now, recommendation_type=RecommendationType.WATCH_BUY
        )
        if outcome.data_error:
            typer.echo(f"[DATA_ERROR] {item.stock_code}: {outcome.data_error}")
            if notification_service is not None:
                notification_service.notify_data_error(item.stock_code, outcome.data_error, now)
            continue
        if outcome.recommendation is None:
            continue
        found_any = True
        repo.save(outcome.recommendation)
        # 判定精度向上機能Phase A: DecisionSnapshotを記録する(スコア項目はPhase Bまで
        # 全てNone)。失敗しても既存の保存・通知には一切影響しない。
        save_decision_snapshot_safely(
            DecisionSnapshotRepository(), outcome.recommendation, DecisionType.BUY, logger
        )
        _print_buy_recommendation(outcome.recommendation)
        if notification_service is not None:
            sent = notification_service.notify_recommendation(outcome.recommendation, now)
            typer.echo(
                "  → LINE通知しました" if sent else "  → 前回と同内容のため通知をスキップしました"
            )

    if not found_any:
        typer.echo("買い条件に該当するウォッチリスト銘柄はありませんでした。")
    typer.echo(_DISCLAIMER)


@app.command("holdings")
def analyze_holdings(
    notify: bool = typer.Option(False, "--notify/--no-notify", help=_NOTIFY_HELP),
    source: str = typer.Option("mock", "--source", help=_SOURCE_HELP),
) -> None:
    """保有銘柄の利確判定・投資前提悪化売却判定を行う。"""
    if source not in ("mock", "real"):
        raise typer.BadParameter("--source は mock または real を指定してください")

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = _build_providers(source, now, config)
    profit_service = ProfitTakingService(providers=providers, config=config)
    sell_service = SellSignalService(providers=providers, config=config)
    repo = RecommendationRepository()
    notification_service = (
        _build_notification_service(build_line_client_from_env(), repo, config) if notify else None
    )

    holdings = PortfolioService().list_holdings()
    if not holdings:
        typer.echo("保有銘柄は登録されていません。")
        return

    found_any = False
    for holding in holdings:
        sell_outcome = sell_service.analyze(holding, now)
        if sell_outcome.data_error:
            typer.echo(f"[DATA_ERROR] {holding.stock_code}: {sell_outcome.data_error}")
            if notification_service is not None:
                notification_service.notify_data_error(
                    holding.stock_code, sell_outcome.data_error, now
                )
            continue
        if sell_outcome.recommendation is not None:
            found_any = True
            repo.save(sell_outcome.recommendation)
            # 判定精度向上機能Phase A: DecisionSnapshotを記録する(スコア項目は
            # Phase Bまで全てNone)。失敗しても既存の保存・通知には一切影響しない。
            save_decision_snapshot_safely(
                DecisionSnapshotRepository(), sell_outcome.recommendation, DecisionType.SELL, logger
            )
            _print_sell_recommendation(sell_outcome.recommendation)
            if notification_service is not None:
                sent = notification_service.notify_recommendation(sell_outcome.recommendation, now)
                typer.echo(
                    "  → LINE通知しました"
                    if sent
                    else "  → 前回と同内容のため通知をスキップしました"
                )
            continue  # 投資前提悪化が検出された場合は利確判定より優先して表示する

        pt_outcome = profit_service.analyze(holding, now)
        if pt_outcome.data_error:
            typer.echo(f"[DATA_ERROR] {holding.stock_code}: {pt_outcome.data_error}")
            if notification_service is not None:
                notification_service.notify_data_error(
                    holding.stock_code, pt_outcome.data_error, now
                )
            continue
        if pt_outcome.recommendation is not None:
            found_any = True
            repo.save(pt_outcome.recommendation)
            # 判定精度向上機能Phase A: DecisionSnapshotを記録する(スコア項目は
            # Phase Bまで全てNone)。失敗しても既存の保存・通知には一切影響しない。
            save_decision_snapshot_safely(
                DecisionSnapshotRepository(),
                pt_outcome.recommendation,
                DecisionType.PROFIT_TAKING,
                logger,
            )
            _print_profit_taking_recommendation(pt_outcome.recommendation)
            if notification_service is not None:
                sent = notification_service.notify_recommendation(pt_outcome.recommendation, now)
                typer.echo(
                    "  → LINE通知しました"
                    if sent
                    else "  → 前回と同内容のため通知をスキップしました"
                )

    if not found_any:
        typer.echo("保有継続(HOLD)以外の判定に該当する銘柄はありませんでした。")
    typer.echo(_DISCLAIMER)


@app.command("disclosure-check")
def analyze_disclosure_check(
    notify: bool = typer.Option(False, "--notify/--no-notify", help=_NOTIFY_HELP),
    source: str = typer.Option("mock", "--source", help=_SOURCE_HELP),
) -> None:
    """保有銘柄の新規適時開示をチェックし、リスクキーワード検出時に速報する。"""
    if source not in ("mock", "real"):
        raise typer.BadParameter("--source は mock または real を指定してください")

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = _build_providers(source, now, config)
    service = DisclosureCheckService(disclosure_provider=providers.disclosure, config=config)
    notification_service = (
        _build_notification_service(
            build_line_client_from_env(), RecommendationRepository(), config
        )
        if notify
        else None
    )

    alerts = service.check_holdings(now)
    if not alerts:
        typer.echo("リスクキーワードを含む新規開示はありませんでした。")
        return

    for alert in alerts:
        typer.echo(f"[重要開示検知] {alert.stock_code}: {alert.disclosure.title}")
        typer.echo(f"  検出キーワード: {', '.join(alert.matched_keywords)}")
        if notification_service is not None:
            sent = notification_service.notify_disclosure_risk(
                stock_code=alert.stock_code,
                disclosure_title=alert.disclosure.title,
                disclosure_summary=alert.disclosure.summary,
                matched_keywords=alert.matched_keywords,
                published_at=alert.disclosure.published_at,
                now=now,
                stock_name=alert.stock_name,
            )
            typer.echo(
                "  → LINE通知しました" if sent else "  → 同一開示のため通知をスキップしました"
            )
    typer.echo(_DISCLAIMER)


def _print_buy_recommendation(r: Recommendation) -> None:
    label = (
        buy_action_label(r.buy_action) if r.buy_action is not None else r.recommendation_type.value
    )
    typer.echo(f"[{label}] {r.stock_code} {r.stock_name}")
    # Issue #55 Phase B-1: 総合利回りは判定時点で確定できないことがある(None)。
    # 0.00%と断定せず「不明」と表示する(:.2fへNoneを渡すとTypeErrorになる)。
    total_yield_text = (
        f"{r.total_yield_pct_at_recommendation:.2f}%"
        if r.total_yield_pct_at_recommendation is not None
        else "不明"
    )
    typer.echo(
        f"  現在株価: {r.price_at_recommendation}円 / 総合利回り: {total_yield_text}"
    )
    if r.buy_prices and r.buy_prices.entry and r.buy_prices.standard and r.buy_prices.strong:
        typer.echo(
            f"  打診買い:{r.buy_prices.entry.price}円 標準買い:{r.buy_prices.standard.price}円 "
            f"積極買い:{r.buy_prices.strong.price}円"
        )
    typer.echo(
        f"  企業魅力度: {r.company_quality_score} / 購入魅力度: {r.purchase_attractiveness_score} "
        f"/ 信頼度: {r.confidence.value}"
    )
    for reason in r.reasons:
        typer.echo(f"  理由: {reason}")
    for risk in r.key_risks:
        typer.echo(f"  リスク: {risk}")


def _print_profit_taking_recommendation(r: Recommendation) -> None:
    typer.echo(f"[{r.recommendation_type.value}] {r.stock_code} {r.stock_name}(利確検討)")
    typer.echo(
        f"  現在株価: {r.price_at_recommendation}円 / "
        f"平均取得単価: {r.average_purchase_price_at_recommendation}円"
    )
    for reason in r.reasons:
        typer.echo(f"  理由: {reason}")
    for factor in r.counter_factors:
        typer.echo(f"  保有継続を支持する要因: {factor}")
    sp = r.sell_prices
    if sp is not None:
        if sp.partial_profit_start_price:
            typer.echo(f"  一部利確開始価格: {sp.partial_profit_start_price.price}円")
        if sp.recommended_limit_price:
            typer.echo(f"  利確推奨価格(指値候補): {sp.recommended_limit_price.price}円")
        if sp.full_profit_consideration_price:
            typer.echo(
                f"  全株利確検討価格(参考水準): {sp.full_profit_consideration_price.price}円"
            )


def _print_sell_recommendation(r: Recommendation) -> None:
    typer.echo(f"[{r.recommendation_type.value}] {r.stock_code} {r.stock_name}(投資前提悪化)")
    for reason in r.reasons:
        typer.echo(f"  理由: {reason}")
    if r.sell_prices is not None and r.sell_prices.stop_review_price:
        typer.echo(f"  売却目安価格: {r.sell_prices.stop_review_price.price}円")
