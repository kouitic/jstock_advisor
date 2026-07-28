"""適時開示チェックサービス(schedule.yaml disclosure_check、要求仕様16節)。

保有銘柄について新規の適時開示を取得し、リスクキーワードを検出する。analyzeコマンド
実行時にも同じ開示データはstock_snapshot_service経由で取得されるが、判定処理の完了を
待たず速報として検知したい場合に単独で実行する(通知の送信・重複抑止は呼び出し側の
line_notification_service.notify_disclosure_riskが担当し、本サービスは検知のみを行う)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.screening.rules import detect_disclosure_risk_keywords
from jstock_advisor.interfaces.disclosure import DisclosureProvider
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.services.portfolio_service import PortfolioService

_LOOKBACK_DAYS = 7  # 直近何日分の開示を確認対象とするか


@dataclass(frozen=True)
class DisclosureRiskAlert:
    stock_code: str
    stock_name: str
    disclosure: Disclosure
    matched_keywords: list[str]


class DisclosureCheckService:
    def __init__(
        self,
        disclosure_provider: DisclosureProvider,
        config: AppConfig,
        portfolio_service: PortfolioService | None = None,
    ) -> None:
        self._disclosure = disclosure_provider
        self._config = config
        self._portfolio = portfolio_service or PortfolioService()

    def check_holdings(self, now: dt.datetime) -> list[DisclosureRiskAlert]:
        alerts: list[DisclosureRiskAlert] = []
        since = now.date() - dt.timedelta(days=_LOOKBACK_DAYS)
        for holding in self._portfolio.list_holdings():
            disclosures = self._disclosure.get_disclosures(holding.stock_code, since)
            for disclosure in disclosures:
                matched = detect_disclosure_risk_keywords(
                    [disclosure], self._config.sell.disclosure_risk_keywords
                )
                if matched:
                    alerts.append(
                        DisclosureRiskAlert(
                            holding.stock_code, holding.stock_name, disclosure, matched
                        )
                    )
        return alerts
