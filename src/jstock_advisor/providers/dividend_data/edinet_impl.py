"""dividend_data_provider の EDINET実装(検証・突合用)。

有価証券報告書の「経営指標等の推移」表から1株当たり配当額(過去5期分)を取得する。
この値は株式分割・併合による遡及調整がされていない額面ベースであることに注意
(実測で確認済み。株式分割があった銘柄をyfinance等の分割調整済みデータと比較する
際は分割比率で補正する必要がある)。forecast(予想配当)はこの表には含まれないため
Noneとなる。

各期のperiod_end(決算期末日)は、当期のみEDINET書類一覧APIから取得した実測値
(periodEnd)を使い、前期以前4期は当期のperiod_endから1年刻みで逆算した推定値と
する。EdinetCsvRow(要素ID・項目名・コンテキストID・相対期間ラベル・連結個別区分・
期間/時点区分・単位ID・単位ラベル・値の9列)には各期間の実際の開始日・終了日を
持つ列が含まれておらず、現状のcsv_parser.pyのスコープではこれ以上の精度で
取得できない(配当データクロスバリデーション根本修正で確認)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    DividendPeriodEndBasis,
    RecordDateUnknownReason,
    SourceType,
)
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.csv_parser import EdinetCsvRow, extract_main_document_rows
from jstock_advisor.infrastructure.edinet.document_finder import (
    EdinetFilingCacheRepository,
    find_latest_filings,
)
from jstock_advisor.interfaces.types import AnnualDividendActual, DividendInfo

_PROVIDER_NAME = "edinet"
_DIVIDEND_ELEMENT = "DividendPaidPerShareSummaryOfBusinessResults"
_PERIOD_LABELS_OLDEST_TO_NEWEST = ["四期前", "三期前", "前々期", "前期", "当期"]
# 当期からの遡り年数(経営指標等の推移表は5期分を「四期前〜当期」の相対ラベルで持つ)
_PERIOD_LABEL_OFFSETS_FROM_CURRENT = {"当期": 0, "前期": 1, "前々期": 2, "三期前": 3, "四期前": 4}


def _shift_years(value: dt.date, years: int) -> dt.date:
    """うるう年2/29を跨ぐ場合に備えたreplace(year=...)のフォールバック付き版。"""
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return dt.date(value.year - years, 2, 28)


class EdinetDividendDataProvider:
    def __init__(
        self,
        client: EdinetClient | None = None,
        cache_repository: EdinetFilingCacheRepository | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        self._client = client or EdinetClient()
        self._cache_repo = cache_repository or EdinetFilingCacheRepository()
        self._now = now or dt.datetime.now(dt.UTC)

    def _source(self) -> DataSourceReference:
        return DataSourceReference(
            provider=_PROVIDER_NAME,
            fetched_at=self._now,
            source_type=SourceType.TDNET_EDINET,
            primary_source_flag=True,
        )

    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        del fiscal_year_end_month  # EDINETは当期のperiod_endを書類一覧APIから直接取得するため未使用
        if not self._client.is_configured:
            return None

        filing = find_latest_filings(self._client, self._cache_repo, stock_code, self._now)
        if filing is None or filing.latest_annual_doc_id is None:
            return None

        zip_bytes = self._client.download_document_zip(filing.latest_annual_doc_id)
        if zip_bytes is None:
            return None

        rows = extract_main_document_rows(zip_bytes)
        if rows is None:
            return None

        yearly_values = self._extract_five_year_series(rows)
        if not yearly_values:
            return None

        actual = yearly_values.get("当期")
        previous = yearly_values.get("前期")
        consecutive_increase_years = self._count_consecutive_increases(yearly_values)
        annual_dividend_actuals = self._build_annual_dividend_actuals(
            yearly_values, filing.latest_annual_period_end
        )

        return DividendInfo(
            stock_code=stock_code,
            fiscal_year=filing.latest_annual_period_end or str(self._now.year),
            forecast_annual_dividend_per_share=None,  # 経営指標等の推移表には次期予想が無い
            actual_annual_dividend_per_share=actual,
            previous_fiscal_year_dividend_per_share=previous,
            is_dividend_cut_announced=False,
            is_dividend_omission_announced=False,
            is_progressive_or_doe_policy=False,
            dividend_policy_note=None,
            dividend_record_dates=[],  # EDINETのこの表からは権利確定日を取得できない
            consecutive_dividend_increase_years=consecutive_increase_years,
            source=self._source(),
            dividend_record_date=None,
            dividend_ex_date=None,
            # EDINETの「経営指標等の推移」表も権利確定日・権利落ち日は提供しない(恒久的な制約)
            dividend_record_date_unknown_reason=RecordDateUnknownReason.DATA_PROVIDER_MISSING,
            annual_dividend_actuals=annual_dividend_actuals,
        )

    def _build_annual_dividend_actuals(
        self, yearly_values: dict[str, Decimal], latest_annual_period_end: str | None
    ) -> list[AnnualDividendActual]:
        if latest_annual_period_end is None:
            return []
        try:
            current_period_end = dt.date.fromisoformat(latest_annual_period_end)
        except ValueError:
            return []

        actuals: list[AnnualDividendActual] = []
        for label, offset in _PERIOD_LABEL_OFFSETS_FROM_CURRENT.items():
            value = yearly_values.get(label)
            if value is None:
                continue
            period_end = _shift_years(current_period_end, offset)
            period_start = _shift_years(period_end, 1) + dt.timedelta(days=1)
            actuals.append(
                AnnualDividendActual(
                    period_end=period_end,
                    period_end_basis=(
                        DividendPeriodEndBasis.REPORTED
                        if offset == 0
                        else DividendPeriodEndBasis.DERIVED_FROM_RELATIVE_PERIOD
                    ),
                    period_start=period_start,
                    period_start_is_estimated=True,
                    raw_dividend_per_share=value,
                    normalized_dividend_per_share=None,  # EDINETは自己正規化しない(常に額面のまま)
                    normalization_basis_date=None,
                )
            )
        return actuals

    @staticmethod
    def _extract_five_year_series(rows: list[EdinetCsvRow]) -> dict[str, Decimal]:
        candidates = [r for r in rows if r.element_id.split(":")[-1] == _DIVIDEND_ELEMENT]
        consolidated = [r for r in candidates if "NonConsolidated" not in r.context_id]
        pool = consolidated or candidates

        result: dict[str, Decimal] = {}
        for label in _PERIOD_LABELS_OLDEST_TO_NEWEST:
            matches = [r for r in pool if r.relative_period == label]
            if not matches:
                continue
            try:
                result[label] = Decimal(matches[0].value)
            except (InvalidOperation, ValueError):
                continue
        return result

    @staticmethod
    def _count_consecutive_increases(yearly_values: dict[str, Decimal]) -> int | None:
        ordered = [
            yearly_values[label]
            for label in _PERIOD_LABELS_OLDEST_TO_NEWEST
            if label in yearly_values
        ]
        if len(ordered) < 2:
            return None
        count = 0
        for i in range(len(ordered) - 1, 0, -1):
            if ordered[i] > ordered[i - 1]:
                count += 1
            else:
                break
        return count
