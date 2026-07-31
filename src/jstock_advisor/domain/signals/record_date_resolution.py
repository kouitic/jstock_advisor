"""配当・優待の権利確定日(基準日)表示の解決ロジック(2026-07仕様レビュー対応)。

profit_taking_service.py・buy_signal_service.pyの両方から共通で使うため、
サービス層から独立したこのモジュールへ切り出した。

正確な次回日付が不明でも、決算期末等の一次情報から基準月・基準日の周期
パターンが分かる場合は、単なる「不明」ではなく推定ラベルを表示する
(要求仕様16節)。あわせて、その表示が実データ(確定日または登録済み周期)に
基づくのか、システムによる自己推定なのかを区別できるよう、SourceType
(既存enum)を返す解決関数も提供する。表示ラベルへの変換は通知層
(services/line_notification_service.py)に閉じ込め、ここではSourceTypeを
そのまま返すにとどめる(Recommendationの既存カテゴリカルフィールドと
同じ設計方針)。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import RecordDateUnknownReason, SourceType
from jstock_advisor.interfaces.types import DividendInfo, ShareholderBenefit

_MONTH_END_FISCAL_LABEL = "{month}月末"


def resolve_dividend_record_date_recurring_label(
    dividend: DividendInfo, fiscal_year_end_month: int | None
) -> str | None:
    """配当基準日の正確な次回日付が不明でも、決算期末から推定される周期パターンを
    ラベル化する(要求仕様レビュー対応: 単なる「不明」表示を避ける)。

    日本企業の多くは期末配当(決算期末基準)と中間配当(決算期末の6ヶ月前基準)の
    年2回、または期末配当のみ年1回の構成であるという一般的な慣行に基づく推定
    であり、当該銘柄固有の確定情報ではないことを明示する。

    直近開示期間末(四半期の場合がある)ではなく、必ず企業の正式な決算期末月
    (fiscal_year_end_month)を使う。
    """
    if (
        dividend.dividend_record_date_unknown_reason
        != RecordDateUnknownReason.DATA_PROVIDER_MISSING
    ):
        return None
    if fiscal_year_end_month is None:
        return None
    interim_month = (fiscal_year_end_month - 6 - 1) % 12 + 1
    months = sorted({interim_month, fiscal_year_end_month})
    labels = "・".join(_MONTH_END_FISCAL_LABEL.format(month=m) for m in months)
    return f"毎年{labels}(決算期末を基準とした一般的な慣行からの推定、確定情報ではない)"


def resolve_benefit_record_date_recurring_label(
    benefit: ShareholderBenefit | None, fiscal_year_end_month: int | None
) -> str | None:
    """優待基準日の推定・登録済み周期ラベルを、優先順位に従って解決する
    (2026-07修正: 登録済みの権利確定周期(benefit_record_date_recurrence_months)を
    最優先する。以前はunknown_reason==DATA_PROVIDER_MISSINGのときしか推定ラベルを
    出さない設計だったため、手動登録(CSV取込含む)でbenefit_record_datesが空だが
    recurrence_monthsだけが登録されているケース(unknown_reason=SOURCE_NOT_FOUND)で
    「不明(未登録)」という事実と異なる表示になっていた不具合を修正)。

    優先順位: 確定日(呼び出し元でNoneが返る) > 登録済み周期 > 決算期末からの
    一般的推定 > 不明。低い優先順位の理由コードが高い優先順位の登録済みデータを
    覆い隠さないようにする。
    """
    if benefit is None:
        return None
    if benefit.benefit_record_dates:
        return None  # 確定日があるので推定不要
    if benefit.benefit_record_date_recurrence_months:
        months = sorted(set(benefit.benefit_record_date_recurrence_months))
        labels = "・".join(_MONTH_END_FISCAL_LABEL.format(month=m) for m in months)
        return f"毎年{labels}(登録済みの権利確定周期に基づく)"
    if (
        benefit.benefit_record_date_unknown_reason == RecordDateUnknownReason.DATA_PROVIDER_MISSING
        and fiscal_year_end_month is not None
    ):
        if benefit.frequency_per_year >= 2:
            interim_month = (fiscal_year_end_month - 6 - 1) % 12 + 1
            months = sorted({interim_month, fiscal_year_end_month})
        else:
            months = [fiscal_year_end_month]
        labels = "・".join(_MONTH_END_FISCAL_LABEL.format(month=m) for m in months)
        return f"毎年{labels}(決算期末を基準とした一般的な慣行からの推定、確定情報ではない)"
    return None


def resolve_dividend_record_date_source_type(dividend: DividendInfo) -> SourceType | None:
    """確定日(実データ)がある場合のみsource_typeを返す。決算期末等からの
    自己推定にはsource_typeを付与しない(通知層での「データ提供元」との
    誤表示を防ぐため)。配当には手動登録の仕組みが無いため、現行のデータ
    ソース(yfinance/EDINET)では常にNoneになる(確定日を提供しないため)。
    """
    if dividend.dividend_record_dates:
        return dividend.source.source_type
    return None


def resolve_benefit_record_date_source_type(
    benefit: ShareholderBenefit | None,
) -> SourceType | None:
    """確定日または登録済み周期(=実データ)がある場合のみsource_typeを返す。
    決算期末等からの自己推定にはsource_typeを付与しない。
    """
    if benefit is None:
        return None
    if benefit.benefit_record_dates or benefit.benefit_record_date_recurrence_months:
        return benefit.source.source_type
    return None
