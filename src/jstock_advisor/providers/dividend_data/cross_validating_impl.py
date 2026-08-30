"""dividend_data_provider のクロスバリデーション実装(配当データクロスバリデーション根本修正)。

yfinanceを主データ源、EDINETを副データ源として配当額を突き合わせる。単純に
「両ソースの最新値同士」を比較するのではなく、両ソースが共通して保持する
最新の決算期(period_end)を探し、その決算期についてのみ検証する。EDINETの
有価証券報告書は決算後・提出まで数ヶ月のタイムラグが恒常的に存在するため、
「最新決算期がyfinanceにはあるがEDINETにはまだ無い」状態は日常的に発生する
正常な状態であり、これをもって配当データを取得不可にはしない
(validation_status=NOT_YET_VALIDATABLEとして扱い、最新のyfinance値はそのまま
利用可能とする。「最新値の利用」と「検証済みかどうか」を分離する)。

共通決算期が見つかった場合も、その決算期の"期間内"に株式分割・併合等が発生して
いる場合はEDINETの年間合計値(中間配当・期末配当の内訳を持たない単一の合計)を
単一倍率で一意に正規化できないため、これもNOT_YET_VALIDATABLEとして扱う
(真の乖離とは断定しない)。期間内に分割が無い場合のみ、CorporateActionServiceで
EDINET生値を決定論的にyfinanceと同一の基準日へ正規化してから比較する。

共通決算期(EDINET当期=REPORTED)で、期間内分割が無く、正規化後もなお閾値を
超えて乖離する場合のみ、真の乖離として配当データを「取得不可」として扱う
(推測で片方を採用しない)。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from jstock_advisor.config.models import DataValidationRulesConfig
from jstock_advisor.domain.entities.corporate_action import AdjustedDecimal
from jstock_advisor.domain.entities.enums import DividendPeriodEndBasis, DividendValidationStatus
from jstock_advisor.interfaces.dividend_data import DividendDataProvider
from jstock_advisor.interfaces.types import AnnualDividendActual, DividendInfo
from jstock_advisor.services.corporate_action_service import CorporateActionService

logger = logging.getLogger(__name__)


def _representative_value(info: DividendInfo) -> Decimal | None:
    """クロスバリデーションを実施できるかの判定に使う代表値(Issue #59 Phase B3)。

    契約:

    - `actual_annual_dividend_per_share` が **None でなければ actual を採用**する
    - **`Decimal("0")` は「無配」という正当な実値**であり、欠測ではない
    - `forecast_annual_dividend_per_share` へフォールバックするのは
      **actual が None(=不明)の場合だけ**

    以前は `actual or forecast` としていたため、`Decimal("0")` が falsy 扱いされ
    forecast へフォールバックしていた。その結果、無配企業について
    「配当実績0円(確定)」と「配当実績が不明」が同じ扱いになり、
    予想配当が無い銘柄ではクロスバリデーション自体が
    「代表値なし」として早期returnで実施されなくなっていた
    (#59 の provider failure semantics と同じ「0とNoneの混同」)。
    """
    actual = info.actual_annual_dividend_per_share
    if actual is not None:
        return actual
    return info.forecast_annual_dividend_per_share


def _within_threshold(a: Decimal, b: Decimal, threshold_pct: float) -> bool:
    if b == 0:
        return a == 0
    return abs(float(a / b - 1)) * 100 <= threshold_pct


def _discrepancy_pct(a: Decimal, b: Decimal) -> float | None:
    if b == 0:
        return None
    return abs(float(a / b - 1)) * 100


class CrossValidatingDividendDataProvider:
    def __init__(
        self,
        primary: DividendDataProvider,
        secondary: DividendDataProvider,
        corporate_action_service: CorporateActionService,
        config: DataValidationRulesConfig,
        now: dt.datetime | None = None,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._corporate_action_service = corporate_action_service
        self._config = config
        self._now = now or dt.datetime.now(dt.UTC)

    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        primary_info = self._primary.get_dividend_info(stock_code, fiscal_year_end_month)
        if primary_info is None:
            return None

        if _representative_value(primary_info) is None:
            return primary_info

        if primary_info.calendar_year_fallback_used:
            # fiscal_year_end_month不明で暦年近似した値は、EDINET決算期と偶然一致しても
            # 「同じ決算期」と断定しない(必須修正)。
            logger.info(
                "dividend cross validation skipped: fiscal_year_end_month_unknown "
                "stock_code=%s calendar_year_fallback_used=true "
                "validation_status=NOT_YET_VALIDATABLE",
                stock_code,
            )
            return primary_info.model_copy(
                update={
                    "validation_status": DividendValidationStatus.NOT_YET_VALIDATABLE,
                    "validated_period_end": None,
                }
            )

        secondary_info = self._secondary.get_dividend_info(stock_code, fiscal_year_end_month)
        if secondary_info is None or not secondary_info.annual_dividend_actuals:
            # 副データ源が使えない場合は主データ源をそのまま信頼する(未検証であることは明示する)
            return primary_info.model_copy(
                update={"validation_status": DividendValidationStatus.SECONDARY_UNAVAILABLE}
            )

        primary_by_end = {a.period_end: a for a in primary_info.annual_dividend_actuals}
        secondary_by_end = {a.period_end: a for a in secondary_info.annual_dividend_actuals}

        matched_end = self._select_matched_period(primary_by_end, secondary_by_end, secondary_info)
        if matched_end is None:
            logger.info(
                "dividend cross validation: no common validated period yet "
                "(filing lag or insufficient history) stock_code=%s "
                "validation_status=NOT_YET_VALIDATABLE",
                stock_code,
            )
            return primary_info.model_copy(
                update={"validation_status": DividendValidationStatus.NOT_YET_VALIDATABLE}
            )

        p, s = primary_by_end[matched_end], secondary_by_end[matched_end]
        if p.normalized_dividend_per_share is None:
            # yfinance側が正規化値を持たない(想定外)場合は安全側で検証不能とする
            return primary_info.model_copy(
                update={"validation_status": DividendValidationStatus.NOT_YET_VALIDATABLE}
            )

        period_start = p.period_start or matched_end
        basis_date = p.normalization_basis_date or matched_end

        # 検証対象期間(period_start, matched_end]と、その後(matched_end, basis_date]の
        # 両方をカバーする1回のfetchで済ませる(sinceは事後フィルタのみのためAPIコスト増なし)
        events = self._corporate_action_service.get_effective_events(stock_code, period_start)
        # 1株当たり指標(DPS)の調整対象かどうかの判定はCorporateActionServiceへ一元化する
        # (SPLIT/REVERSE_SPLIT/FREE_ALLOTMENTのみ。MERGER等はratioを持っていても対象外)。
        split_events = self._corporate_action_service.get_ratio_adjustment_events(events)

        split_within_period = any(
            e.effective_date is not None and period_start < e.effective_date <= matched_end
            for e in split_events
        )
        if split_within_period:
            # 決算期"内"の株式分割等はEDINET年間合計(中間配当・期末配当の内訳を持たない
            # 単一の合計値)を単一倍率では一意に正規化できないため、真の乖離とは扱わない。
            logger.info(
                "dividend cross validation: corporate action occurred within the dividend "
                "period; cannot uniquely normalize EDINET annual total stock_code=%s "
                "matched_period_end=%s validation_status=NOT_YET_VALIDATABLE "
                "reason=corporate_action_within_dividend_period",
                stock_code,
                matched_end,
            )
            return primary_info.model_copy(
                update={"validation_status": DividendValidationStatus.NOT_YET_VALIDATABLE}
            )

        # 決算期終了後の分割のみ→EDINET生値をCorporateActionServiceで正式にbasis_dateへ正規化する
        secondary_adjusted = self._corporate_action_service.adjust_per_share_metric(
            raw=s.raw_dividend_per_share,
            stock_code=stock_code,
            value_date=matched_end,
            basis_date=basis_date,
            source=secondary_info.source,
            events=split_events,
        )
        primary_value = p.normalized_dividend_per_share
        primary_adjusted = AdjustedDecimal(
            raw_value=p.raw_dividend_per_share,
            adjusted_value=primary_value,
            adjustment_factor=Decimal(1),
            adjustment_basis_date=basis_date,
            source=primary_info.source,
            source_timestamp=self._now,
        )
        # 異なる基準日の値を直接比較しないことをコード上も強制する
        self._corporate_action_service.require_matching_basis_dates(
            secondary_adjusted, primary_adjusted
        )
        secondary_value = secondary_adjusted.adjusted_value

        threshold = self._config.discrepancy_threshold_pct
        if _within_threshold(primary_value, secondary_value, threshold):
            logger.info(
                "dividend cross validation succeeded stock_code=%s primary_provider=%s "
                "secondary_provider=%s matched_period_end=%s validated_period_basis=%s "
                "primary_raw_value=%s primary_normalized_value=%s secondary_raw_value=%s "
                "secondary_normalized_value=%s normalization_basis_date=%s "
                "applied_split_ratios=%s discrepancy_pct=%s validation_status=VALIDATED",
                stock_code,
                primary_info.source.provider,
                secondary_info.source.provider,
                matched_end,
                s.period_end_basis.value,
                p.raw_dividend_per_share,
                primary_value,
                s.raw_dividend_per_share,
                secondary_value,
                basis_date,
                [e.ratio for e in split_events],
                _discrepancy_pct(primary_value, secondary_value),
            )
            return primary_info.model_copy(
                update={
                    "validation_status": DividendValidationStatus.VALIDATED,
                    "validated_period_end": matched_end,
                    "validated_period_basis": s.period_end_basis,
                }
            )

        if s.period_end_basis != DividendPeriodEndBasis.REPORTED:
            # EDINET側の推定period_end(前期以前)での乖離だけでは真の乖離と断定しない
            logger.info(
                "dividend cross validation: discrepancy on a derived/estimated EDINET period; "
                "treating as not-yet-validatable rather than a confirmed discrepancy "
                "stock_code=%s matched_period_end=%s secondary_period_end_basis=%s "
                "validation_status=NOT_YET_VALIDATABLE",
                stock_code,
                matched_end,
                s.period_end_basis.value,
            )
            return primary_info.model_copy(
                update={"validation_status": DividendValidationStatus.NOT_YET_VALIDATABLE}
            )

        logger.warning(
            "dividend values disagree for same reported fiscal period after normalization "
            "stock_code=%s primary_provider=%s secondary_provider=%s matched_period_end=%s "
            "primary_raw_value=%s primary_normalized_value=%s secondary_raw_value=%s "
            "secondary_normalized_value=%s normalization_basis_date=%s discrepancy_pct=%s",
            stock_code,
            primary_info.source.provider,
            secondary_info.source.provider,
            matched_end,
            p.raw_dividend_per_share,
            primary_value,
            s.raw_dividend_per_share,
            secondary_value,
            basis_date,
            _discrepancy_pct(primary_value, secondary_value),
        )
        return None  # 真の乖離。安全側へ倒す(推測で片方を採用しない)

    @staticmethod
    def _select_matched_period(
        primary_by_end: dict[dt.date, AnnualDividendActual],
        secondary_by_end: dict[dt.date, AnnualDividendActual],
        secondary_info: DividendInfo,
    ) -> dt.date | None:
        """優先順位1: EDINETの当期(REPORTED、書類一覧APIから直接取得した実測period_end)が
        primary側にも存在すればそれを最優先する。無ければ両者の共通period_end集合から
        最新を選ぶ(EDINET側の推定期間(DERIVED_FROM_RELATIVE_PERIOD)同士の一致も含む)。
        """
        edinet_reported = next(
            (
                a
                for a in secondary_info.annual_dividend_actuals
                if a.period_end_basis == DividendPeriodEndBasis.REPORTED
            ),
            None,
        )
        if edinet_reported is not None and edinet_reported.period_end in primary_by_end:
            return edinet_reported.period_end

        common = sorted(set(primary_by_end) & set(secondary_by_end), reverse=True)
        return common[0] if common else None
