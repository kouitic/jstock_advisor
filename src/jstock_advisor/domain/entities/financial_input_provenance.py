"""判定入力financial dataのprovenance(Issue #20 Phase B2-A)。

Recommendation生成時にdecision pipelineが実際に使用したfinancial dataについて
「どの期間・どのprovider・いつ観測した・どの種別の値だったか」を
decision-time historical factとして保存する。

【保証対象】pipelineが実際に使用したStockSnapshot上の情報のみ。
「その時点でprovider上に存在していた全情報」や「現在APIから再構築した過去」は
保証しない(現在値からのbackfill・推測は禁止)。

【fp1の明示的な非保証事項】publication(公開)時刻はfp1ではcapture/保証しない。
現行provider(yfinance等)は財務値の公開時刻を提供せず、取得できない値のために
固定UNKNOWNフィールドを置くことはしない(実値の取得はB2-C候補。
Disclosure.published_atから財務値への推測リンクも行わない)。

【欠損semantics】
- NOT_CAPTURED: Recommendation.financial_input_provenance自体がNone
  (fp1導入前の旧レコード)。
- NOT_AVAILABLE: 判定時点でproviderが当該値を提供しなかった
  (FinancialValueProvenance.available=False)。
- UNKNOWN: 値は存在するがsource semantics(会社予想か推定か等)を
  判別できない(FinancialValueSourceType.UNKNOWN)。
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import RecentPeriodsSource

FINANCIAL_INPUT_PROVENANCE_SCHEMA_VERSION = "fp1"


class FinancialValueSourceType(StrEnum):
    """値の種別。予想値について根拠なくCOMPANY_FORECASTとしないこと
    (providerが予想の出所を明示しない場合はPROVIDER_FORECAST_UNSPECIFIED)。"""

    ACTUAL = "ACTUAL"
    COMPANY_FORECAST = "COMPANY_FORECAST"
    PROVIDER_FORECAST_UNSPECIFIED = "PROVIDER_FORECAST_UNSPECIFIED"
    ANALYST_ESTIMATE = "ANALYST_ESTIMATE"
    UNKNOWN = "UNKNOWN"


class FinancialValueProvenance(ImmutableSnapshot):
    """1つの入力値のprovenance。値そのものは保持しない(値の正本は既存の
    保存フィールド。対象はFinancialInputProvenance側のフィールド名で
    機械的に一意化される)。

    observed_atは「システムがproviderからこのsnapshotを取得した時刻」
    (DataSourceReference.fetched_at)であり、providerが値を初めて公開した
    時刻ではない。
    """

    source_type: FinancialValueSourceType
    provider: str | None = None
    observed_at: dt.datetime | None = None
    # False = 判定時点でproviderが当該値を提供しなかった(NOT_AVAILABLE)。
    # その場合でもsource_type/provider/observed_atは「取得を試みた先」の
    # 事実として保持する。
    available: bool = True


class FinancialInputProvenance(ImmutableSnapshot):
    """Recommendation 1件に対する判定入力financial dataのprovenance
    (Issue #20 Phase B2-A、fp1)。

    値とprovenanceの対応(fp1で固定):
    - forecast_eps_source → Recommendation.forecast_eps /
      buy_score_input_facts["forecast_eps"](予想EPS。PER法・スコアの入力)
    - forecast_bps_source → buy_score_input_facts["forecast_bps"]
      (予想BPS。PBR法の入力。実態はprovider提供のtrailing bookValueで
      あることが既知のため、source_typeで種別を正直に記録する)
    - forecast_dividend_source →
      DividendInfo.forecast_annual_dividend_per_share
      (予想年間配当。target_yield法・配当利回りの入力)
    - actual_dividend_source →
      DividendInfo.actual_annual_dividend_per_share(実績年間配当)

    BUY/SELL両パイプラインが同一のStockSnapshotを消費するため、本provenanceは
    「そのsnapshotに含まれ両パイプラインへ提供された入力」の事実を表す
    (パイプライン別の消費有無までは主張しない)。
    """

    provenance_schema_version: str = FINANCIAL_INPUT_PROVENANCE_SCHEMA_VERSION

    # --- 財務諸表の期間(StockSnapshot上の事実のみ。新たな推定を追加しない) ---
    # 直近開示期間末(年次フォールバック時は年次期末)。
    fiscal_period_end: dt.date | None = None
    # 企業の正式な決算期末月(周期推定用の既存フィールドの転記)。
    fiscal_year_end_month: int | None = None
    # recent_quarters先頭(=判定が決算反映確認に優先使用する四半期期末)。
    latest_quarter_end: dt.date | None = None
    # recent_quartersの由来(四半期実績由来か年次フォールバックか)。
    recent_periods_source: RecentPeriodsSource = RecentPeriodsSource.UNAVAILABLE

    # --- 財務summaryの観測情報 ---
    financial_provider: str | None = None
    # システムがproviderから財務summaryを取得した時刻(公開時刻ではない)。
    financial_observed_at: dt.datetime | None = None

    # --- 値別provenance(値とfieldの対応は上記docstringでfp1として固定) ---
    forecast_eps_source: FinancialValueProvenance | None = None
    forecast_bps_source: FinancialValueProvenance | None = None
    forecast_dividend_source: FinancialValueProvenance | None = None
    actual_dividend_source: FinancialValueProvenance | None = None
