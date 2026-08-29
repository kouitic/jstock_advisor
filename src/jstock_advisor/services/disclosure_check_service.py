"""適時開示チェックサービス(schedule.yaml disclosure_check、要求仕様16節)。

保有銘柄について新規の適時開示を取得し、リスクキーワードを検出する。analyzeコマンド
実行時にも同じ開示データはstock_snapshot_service経由で取得されるが、判定処理の完了を
待たず速報として検知したい場合に単独で実行する(通知の送信・重複抑止は呼び出し側の
line_notification_service.notify_disclosure_riskが担当し、本サービスは検知のみを行う)。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.screening.rules import detect_disclosure_risk_keywords
from jstock_advisor.interfaces.disclosure import DisclosureProvider
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.services.portfolio_service import PortfolioService

logger = logging.getLogger(__name__)

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
        """保有銘柄の新規開示からリスクキーワードを検知する。

        Issue #53 Phase B2: 開示情報を取得できなかった銘柄については、
        リスク検知(=通知対象)としては一切扱わない。「取得できなかった」ことを
        「リスク開示あり」と同等に扱うと、EDINET障害がそのままLINEのリスク通知に
        なってしまうため。ただし完全に黙殺せず、運用者が障害を認識できるよう
        WARNINGログへ残す(通知件数は増やさない)。
        """
        alerts: list[DisclosureRiskAlert] = []
        since = evaluation_date_jst(now) - dt.timedelta(days=_LOOKBACK_DAYS)
        for holding in self._portfolio.list_holdings():
            result = self._disclosure.get_disclosures(holding.stock_code, since)
            if not result.is_available:
                logger.warning(
                    "disclosure_check unavailable stock_code=%s reason=%s "
                    "(risk notification is not sent for this stock)",
                    holding.stock_code,
                    result.unavailable_reason,
                )
                continue
            for disclosure in result.disclosures:
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
