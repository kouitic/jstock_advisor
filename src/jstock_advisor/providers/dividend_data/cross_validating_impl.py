"""dividend_data_provider のクロスバリデーション実装。

yfinanceを主データ源、EDINETを副データ源として配当額を突き合わせる。実測により、
配当額の乖離は多くの場合「株式分割・併合の調整基準の違い」が原因であることが
分かっているため、まず無調整で比較し、一致しなければ直近の株式分割比率で
補正しても一致するかを試す。それでも一致しない場合は、どちらが正しいか判断できない
ため配当データを「取得不可」として扱い(推測で片方を採用しない)、警告ログを出す。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from jstock_advisor.config.models import DataValidationRulesConfig
from jstock_advisor.interfaces.corporate_action import CorporateActionProvider
from jstock_advisor.interfaces.dividend_data import DividendDataProvider
from jstock_advisor.interfaces.types import CorporateActionEvent, DividendInfo

logger = logging.getLogger(__name__)


def _representative_value(info: DividendInfo) -> Decimal | None:
    return info.actual_annual_dividend_per_share or info.forecast_annual_dividend_per_share


def _within_threshold(a: Decimal, b: Decimal, threshold_pct: float) -> bool:
    if b == 0:
        return a == 0
    return abs(float(a / b - 1)) * 100 <= threshold_pct


class CrossValidatingDividendDataProvider:
    def __init__(
        self,
        primary: DividendDataProvider,
        secondary: DividendDataProvider,
        corporate_action_provider: CorporateActionProvider,
        config: DataValidationRulesConfig,
        now: dt.datetime | None = None,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._corporate_action = corporate_action_provider
        self._config = config
        self._now = now or dt.datetime.now(dt.UTC)

    def get_dividend_info(self, stock_code: str) -> DividendInfo | None:
        primary_info = self._primary.get_dividend_info(stock_code)
        if primary_info is None:
            return None

        primary_value = _representative_value(primary_info)
        if primary_value is None:
            return primary_info

        secondary_info = self._secondary.get_dividend_info(stock_code)
        secondary_value = (
            secondary_info.actual_annual_dividend_per_share if secondary_info else None
        )
        if secondary_value is None:
            # 副データ源が使えない場合は主データ源をそのまま信頼する
            return primary_info

        threshold = self._config.discrepancy_threshold_pct
        if _within_threshold(primary_value, secondary_value, threshold):
            return primary_info

        since = self._now.date() - dt.timedelta(days=self._config.split_adjustment_lookback_days)
        splits = self._corporate_action.get_corporate_actions(stock_code, since)
        if self._reconcilable_with_splits(primary_value, secondary_value, splits, threshold):
            logger.info(
                "配当データの乖離は株式分割調整で解消(銘柄=%s, primary=%s, secondary=%s)",
                stock_code,
                primary_value,
                secondary_value,
            )
            return primary_info

        logger.warning(
            "配当データがソース間で乖離しており解消できないため除外(銘柄=%s, "
            "primary(%s)=%s, secondary(%s)=%s)",
            stock_code,
            primary_info.source.provider,
            primary_value,
            secondary_info.source.provider if secondary_info else "?",
            secondary_value,
        )
        return None

    @staticmethod
    def _reconcilable_with_splits(
        primary_value: Decimal,
        secondary_value: Decimal,
        splits: list[CorporateActionEvent],
        threshold_pct: float,
    ) -> bool:
        """乖離が株式分割で説明できるかを判定する。

        ルックバック期間内に複数回の分割・併合があった場合、単一の分割比率だけでは
        不十分(例: 3分割の2年後に5分割が起きていれば、通算15倍のずれが生じる)なため、
        個別の比率に加えて全比率の累積(通算)倍率でも照合する。
        """
        ratios = [event.ratio for event in splits if event.ratio is not None and event.ratio > 0]
        if not ratios:
            return False

        cumulative = Decimal(1)
        for ratio in ratios:
            cumulative *= ratio
        candidates = [*ratios, cumulative]

        for ratio in candidates:
            if _within_threshold(secondary_value / ratio, primary_value, threshold_pct):
                return True
            if _within_threshold(secondary_value * ratio, primary_value, threshold_pct):
                return True
        return False
