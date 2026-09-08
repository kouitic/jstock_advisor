"""一次スクリーニング(要求仕様8節)。

除外条件を一つでも満たせば買い候補から除外する。判定に使用した理由をすべて記録し、
除外未満だが留意すべき事項はwarningsとして残す。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import ScreeningRulesConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.entities.enums import IndustryClassification
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.price_freshness import (
    PriceFreshnessVerdict,
    evaluate_buy_price_freshness,
)
from jstock_advisor.interfaces.types import Disclosure, DividendInfo, FinancialSummary


@dataclass(frozen=True)
class ScreeningResult:
    passed: bool
    exclusion_reasons: list[str]
    warnings: list[str]


def detect_disclosure_risk_keywords(
    disclosures: list[Disclosure], keywords: list[str]
) -> list[str]:
    """適時開示のタイトル・要約からリスクキーワードを検出する(決定論的な文字列検索)。

    LLMによる高度な抽出は将来の拡張とし、MVPでは数値判定に影響する重大リスクの
    検知漏れを防ぐための機械的なキーワード一致のみを行う(要求仕様sell_rules.yaml参照)。
    """
    found: set[str] = set()
    for disclosure in disclosures:
        text = f"{disclosure.title} {disclosure.summary or ''}"
        for keyword in keywords:
            if keyword in text:
                found.add(keyword)
    return sorted(found)


# 重大事象の確認語(2026-07仕様レビュー対応)。「第三者委員会設置」等のリスク
# キーワード一致のみでは、実際に企業価値へ重大な影響がある事象かどうか
# 確認できないため、これらの語が別途本文中に見つかった場合にのみ
# MATERIAL_EVENT_CONFIRMEDとする(major_scandal/listing_maintenance_riskの
# is_immediate_critical判定に使用)。
MATERIAL_EVENT_KEYWORDS: tuple[str, ...] = (
    "決算訂正",
    "決算発表延期",
    "監査意見",
    "業績予想の大幅修正",
    "上場維持",
    "重大な財務損失",
    "経営陣の責任",
    "不正の事実",
    "継続企業",
)


def detect_material_event_keywords(disclosures: list[Disclosure]) -> list[str]:
    """開示本文から重大事象の確認語を検出する(決定論的な文字列検索)。"""
    return detect_disclosure_risk_keywords(disclosures, list(MATERIAL_EVENT_KEYWORDS))


def evaluate_screening(
    financial: FinancialSummary,
    dividend: DividendInfo | None,
    average_trading_value_yen: Decimal | None,
    disclosure_risk_keywords_found: list[str],
    data_fetched_at: dt.datetime,
    now: dt.datetime,
    business_calendar: BusinessCalendar,
    config: ScreeningRulesConfig,
    price_as_of_date: dt.date | None = None,
) -> ScreeningResult:
    """一次スクリーニング(BUY候補裾野拡大機能2026-08で再整理)。

    全タイプ共通ハード除外(企業存続・実務上の売買可否に関わるもの)のみを
    `reasons`(除外理由)として扱う。配当性向・自己資本比率・営業CF・
    単年度赤字・直近減配は、投資スタイル(StockType)によって許容度が
    異なるためハード除外から外し、`warnings`(留意事項)として記録するに
    留める。実際のタイプ別可否は`classify_stock_type()`側の各条件で判定
    する。総合利回り(旧: 3.5%ハードゲート)は削除し、呼び出し元では一切
    受け取らない(HIGH_DIVIDEND/INCOME分類には配当利回り単体基準を使う
    ため、ここへ引き継がない)。

    `financial_health.min_equity_ratio_pct`/`max_payout_ratio_pct`は
    `domain/scoring/score.py::compute_score()`がスコアリング係数として
    別途参照しているため、値・フィールド名は変更しない(用途をハード除外
    からwarnings生成へ変えるのみ)。
    """
    reasons: list[str] = []
    warnings: list[str] = []

    if config.universe.exclude_reit and financial.security_type == "REIT":
        reasons.append("REITは対象外です")
    if config.universe.exclude_etf and financial.security_type == "ETF":
        reasons.append("ETFは対象外です")

    fh = config.financial_health
    if (
        financial.payout_ratio_pct is not None
        and financial.payout_ratio_pct > fh.max_payout_ratio_pct
    ):
        warnings.append(
            f"配当性向{financial.payout_ratio_pct:.1f}%が基準{fh.max_payout_ratio_pct}%超"
        )

    if (
        financial.equity_ratio_pct is not None
        and financial.equity_ratio_pct < fh.min_equity_ratio_pct
    ):
        warnings.append(
            f"自己資本比率{financial.equity_ratio_pct:.1f}%が基準{fh.min_equity_ratio_pct}%未満"
        )

    if (
        fh.require_positive_operating_cashflow
        and financial.operating_cashflow is not None
        and financial.operating_cashflow <= 0
    ):
        warnings.append("営業キャッシュフローがマイナス")

    if fh.exclude_negative_equity and financial.is_debt_excess:
        reasons.append("債務超過")

    if fh.exclude_deficit_companies and financial.is_deficit:
        warnings.append("今期赤字")

    ce = config.corporate_events
    if ce.exclude_going_concern_doubt and financial.is_going_concern_doubt:
        reasons.append("継続企業の前提に重大な疑義")

    if (
        ce.exclude_recent_dividend_cut_announced
        and dividend is not None
        and (dividend.is_dividend_cut_announced or dividend.is_dividend_omission_announced)
    ):
        warnings.append("直近で減配・無配転落の発表あり")

    if average_trading_value_yen is not None:
        min_value = config.universe.min_avg_trading_value_20d_yen
        if average_trading_value_yen < min_value:
            reasons.append(
                f"平均売買代金{average_trading_value_yen:,.0f}円が基準{min_value:,}円未満"
            )

    # Issue #29(2026-08-28): 以前はconfigの日本語TSE33ラベルとyfinance由来の
    # 英語industry値を直接比較しており一度も一致しなかった(金融業除外が機能して
    # いなかった)。保有判断スコア・SELL側と同じ既存分類器classify_industry()を
    # 唯一の判定ソースとして使う。UNKNOWN(sector欠損・未知値)は金融業と推測して
    # 除外しない(通過)。
    industry_rules = config.industry_specific_rules
    industry_result = classify_industry(financial.sector, financial.industry)
    if (
        industry_result.classification == IndustryClassification.FINANCIAL
        and industry_result.financial_category in industry_rules.target_industry_classification
    ):
        action = industry_rules.financial_sector_action
        message = (
            f"金融業({industry_result.financial_category}: {financial.industry})は"
            "個別評価ルール未実装のため対象外"
        )
        if action == "exclude_with_warning":
            reasons.append(message)
        elif action != "custom_rules":
            warnings.append(message)

    if disclosure_risk_keywords_found:
        message = f"開示情報にリスクキーワードを検出: {', '.join(disclosure_risk_keywords_found)}"
        if ce.scandal_or_delisting_risk_action == "exclude":
            reasons.append(message)
        else:
            warnings.append(message)

    # Issue #23(2026-08-28): JPX BusinessCalendarへ渡すdateは「JPX営業日を表す
    # JST calendar date」とする(JPXの営業日はJST基準)。data_fetched_at/nowは
    # UTC instantとして保持し、営業日計算の直前でのみJST暦日へ変換する。
    # 両端を必ず同一時間概念(JST暦日)へ揃えること — 片端だけJST化すると
    # 新たな基準混在になる。UTC暦日のままだと、JST 00:00〜08:59の実行で
    # 実際より新しいデータとして扱われ、本来stale除外すべき古いデータが
    # 除外を免れる方向へ誤判定する。
    data_age_days = business_calendar.business_days_between(
        evaluation_date_jst(data_fetched_at), evaluation_date_jst(now)
    )
    max_age = config.data_quality.max_data_age_business_days
    if data_age_days > max_age:
        reasons.append(f"データが{data_age_days}営業日前と古く、基準{max_age}営業日を超過")

    # Issue #52 Phase B2: 株価の基準日(as_of_date)による鮮度判定。
    #
    # 上のdata_age判定は「いつ取得したか」(fetched_at)を見ているのに対し、
    # こちらは「その株価がいつの取引によるものか」を見る。yfinance系providerは
    # 常にfetched_at=nowを返すため、上の判定は10営業日前の終値でも発火しない。
    #
    # 判定はmax_data_age_business_daysを**流用しない**。取得鮮度と価格基準日の
    # 鮮度は別概念であり、再び混ぜるとIssue #52の根本原因へ戻る。
    # 閾値は domain/price_freshness.py へ集約する(人間確定値)。
    #
    # price_as_of_dateがNone(未指定)の場合は判定しない。呼び出し側が
    # 価格を持たない文脈(既存テスト等)で挙動を変えないため。
    # **株価の基準日が不明**であること自体を表現したい場合は、
    # 呼び出し側でevaluate_buy_price_freshness()を直接使うこと。
    if price_as_of_date is not None:
        verdict, reason = evaluate_buy_price_freshness(
            price_as_of_date, now, business_calendar
        )
        if verdict is PriceFreshnessVerdict.HARD_STOP and reason is not None:
            reasons.append(reason)
        elif verdict is PriceFreshnessVerdict.WARNING and reason is not None:
            warnings.append(reason)

    return ScreeningResult(passed=not reasons, exclusion_reasons=reasons, warnings=warnings)
