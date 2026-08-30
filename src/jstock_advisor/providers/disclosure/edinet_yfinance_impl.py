"""disclosure_provider の実データ実装。

適時開示(TDnet)は公式APIが無いため、実測検証の結果に基づき以下2つの実データ源を
組み合わせる:

  - get_disclosures: EDINET臨時報告書・訂正臨時報告書(docTypeCode 180/190)。
    代表者異動・特定子会社異動・財務上の特約(コベナンツ)等、金融商品取引法上
    重要とされる会社情報の変更は臨時報告書としてEDINETにも提出義務があるため、
    重大リスクの検知という目的においてはTDnetの適時開示と同等の実効性を持つ。
    ただし決算短信そのものはTDnet専用でEDINETには提出されないため取得不可。
  - get_next_earnings_date: yfinanceのTicker.calendarから取得する。実測検証済み
    (大型株〜中小型株の複数銘柄で取得できることを確認済み。ただし非公式ライブラリの
    ため将来的に取得できなくなる可能性はある)。
"""

from __future__ import annotations

import datetime as dt

import yfinance as yf

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import SourceType
from jstock_advisor.infrastructure.edinet.disclosure_finder import (
    EdinetDisclosureCacheRepository,
    find_extraordinary_reports,
)
from jstock_advisor.infrastructure.edinet.document_list_cache import EdinetDocumentSource
from jstock_advisor.infrastructure.edinet.types import EdinetFailureReason
from jstock_advisor.interfaces.disclosure import (
    DisclosureQueryResult,
    DisclosureUnavailableReason,
)
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.providers._failure import raise_provider_data_error

_EDINET_PROVIDER_NAME = "edinet"

# infrastructure層のEDINET固有失敗種別を、domain契約の3値へ正規化する対応表
# (Issue #53 Phase B2: EdinetFailureReasonをdomainへ漏らさないための境界変換)。
_UNAVAILABLE_REASON_BY_FAILURE: dict[EdinetFailureReason, DisclosureUnavailableReason] = {
    EdinetFailureReason.NOT_CONFIGURED: DisclosureUnavailableReason.NOT_CONFIGURED,
    EdinetFailureReason.TIMEOUT: DisclosureUnavailableReason.TEMPORARY_FAILURE,
    EdinetFailureReason.HTTP_ERROR: DisclosureUnavailableReason.TEMPORARY_FAILURE,
    EdinetFailureReason.DOWNLOAD_ERROR: DisclosureUnavailableReason.TEMPORARY_FAILURE,
    EdinetFailureReason.PARSE_ERROR: DisclosureUnavailableReason.OTHER,
    EdinetFailureReason.OTHER: DisclosureUnavailableReason.OTHER,
}


class EdinetYfinanceDisclosureProvider:
    def __init__(
        self,
        document_source: EdinetDocumentSource | None = None,
        cache_repository: EdinetDisclosureCacheRepository | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        self._source = document_source or EdinetDocumentSource()
        self._cache_repo = cache_repository or EdinetDisclosureCacheRepository()
        self._now = now or dt.datetime.now(dt.UTC)

    def get_disclosures(self, stock_code: str, since: dt.date) -> DisclosureQueryResult:
        scan = find_extraordinary_reports(self._source, self._cache_repo, stock_code, self._now)
        if not scan.complete or scan.cache is None:
            # 今回の実行で対象範囲を最後まで取得できていない。過去の走査結果が
            # cacheに残っていても「開示なし」とは言えないため、取得不能として返す
            # (Issue #53 Phase B2)。
            reason = _UNAVAILABLE_REASON_BY_FAILURE.get(
                scan.failure_reason or EdinetFailureReason.OTHER,
                DisclosureUnavailableReason.OTHER,
            )
            return DisclosureQueryResult.unavailable(reason)
        source = DataSourceReference(
            provider=_EDINET_PROVIDER_NAME,
            fetched_at=self._now,
            source_type=SourceType.TDNET_EDINET,
            primary_source_flag=True,
        )
        return DisclosureQueryResult.available(
            [
                Disclosure(
                    stock_code=stock_code,
                    published_at=dt.datetime.combine(
                        record.submit_date, dt.time.min, tzinfo=dt.UTC
                    ),
                    title="臨時報告書",
                    category="臨時報告書",
                    summary=record.summary,
                    url=None,
                    source=source,
                )
                for record in scan.cache.records
                if record.submit_date >= since
            ]
        )

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        """次回決算発表予定日を取得する(Issue #59 Phase B4)。

        契約: **SUCCESS + date / SUCCESS + missing / FAILURE を混同しない。**

        - 取得できた → `date`
        - **取得自体は成功したが決算予定日が未公表・欠測** → `None`
          (`calendar` が `None` / 空 dict / "Earnings Date" キー無し / 値が空)
        - **外部アクセス失敗・応答の構造が想定外** → `ProviderDataError` を送出

        以前は上記3種をすべて `None` へ潰していたため、provider障害が
        「決算予定なし」と同義になり、再試行も走らないまま
        `EarningsDateStatus.UNAVAILABLE` へロンダリングされていた
        (決算直前のBUY抑制ゲートが無音ですり抜ける経路)。

        パース失敗を `None` + 警告ログに留めると「parse failure = missing」の
        混同が残るため、**failureとして送出**する。新しい例外階層は作らず、
        非再試行の内部例外を `raise_provider_data_error()` へ渡して
        `ProviderDataError` へ統一する(retryable は分類器が1回だけ決める)。
        """
        try:
            ticker = yf.Ticker(f"{stock_code}.T")
            calendar = ticker.calendar
        except Exception as exc:  # noqa: BLE001 - 非公式ライブラリのため例外種別を限定できない
            # 外部アクセス中の例外。429・timeout等はretryableとして分類され、
            # 呼び出し元の既存retry境界(call_with_rate_limit_retry)が再試行する。
            raise_provider_data_error(
                exc, provider_name="yfinance", operation="get_next_earnings_date"
            )

        # --- ここから先はアクセス成功。missing と parse failure を分ける ---
        if calendar is None:
            return None
        if not isinstance(calendar, dict):
            # 想定外の応答構造。「決算予定なし」と解釈してはならない。
            raise_provider_data_error(
                TypeError(f"unexpected calendar type: {type(calendar).__name__}"),
                provider_name="yfinance",
                operation="get_next_earnings_date",
            )

        if "Earnings Date" not in calendar:
            return None
        earnings_dates = calendar["Earnings Date"]
        if earnings_dates is None:
            return None
        if not isinstance(earnings_dates, list | tuple):
            raise_provider_data_error(
                TypeError(
                    f"unexpected Earnings Date type: {type(earnings_dates).__name__}"
                ),
                provider_name="yfinance",
                operation="get_next_earnings_date",
            )
        if not earnings_dates:
            # 空リスト = 予定日が未公表(正常な欠測)。
            return None

        first = earnings_dates[0]
        if isinstance(first, dt.datetime):
            # dt.datetimeはdt.dateのサブクラスであり、素通しすると時刻付きの値が
            # 「次回決算日」として流れる。既存consumerは日付比較
            # (`earnings_date_raw < evaluation_date`)と営業日数算出のみを行うため、
            # 日付へ明示的に正規化する(仕様変更ではなく、既存の日付比較契約の明確化)。
            return first.date()
        if isinstance(first, dt.date):
            return first
        raise_provider_data_error(
            TypeError(f"unexpected Earnings Date element type: {type(first).__name__}"),
            provider_name="yfinance",
            operation="get_next_earnings_date",
        )
