"""LINE通知サービス(要求仕様3節 line_notification_service、16〜19節)。

推奨種別ごとにメッセージを整形し、以下のいずれかに該当する場合のみLINEへ送信する
(要求仕様16節の再通知条件のうち機械的に判定可能なものを実装。決算発表・価格到達・
重要度上昇による再通知は将来の拡張ポイント)。
  - 当該銘柄・通知種別について過去に通知履歴が無い
  - 前回通知時から判定区分(recommendation_type)が変化した
  - 前回通知時から代表価格が設定閾値(%)以上変化した
  - 前回通知からresend_after_days日(暦日)以上経過した
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from decimal import Decimal

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.data_quality_alert import DataQualityAlert
from jstock_advisor.domain.entities.enums import (
    DividendComparisonOutcome,
    NotificationType,
    RecommendationType,
    RecordDateUnknownReason,
)
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.jst import format_jst
from jstock_advisor.infrastructure.line.client import LineClient
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.data_quality_service import DataQualityIssueSeverity, detect_anomalies
from jstock_advisor.services.recommendation_consistency_validator import validate_recommendation

_RECOMMENDATION_TO_NOTIFICATION_TYPE: dict[RecommendationType, NotificationType] = {
    RecommendationType.BUY: NotificationType.DAILY_BUY_CANDIDATES,
    RecommendationType.WATCH_BUY: NotificationType.WATCHLIST_BUY_SIGNAL,
    RecommendationType.WATCH: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.PARTIAL_PROFIT_TAKE: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.FULL_PROFIT_TAKE: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.SELL: NotificationType.SELL_SIGNAL,
    RecommendationType.URGENT_REVIEW: NotificationType.SELL_SIGNAL,
    # --- 決算直前・直後ルール(要求仕様14節)で追加 ---
    RecommendationType.WATCH_BEFORE_EARNINGS: NotificationType.WATCHLIST_BUY_SIGNAL,
    RecommendationType.PARTIAL_RISK_REDUCTION: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.REVIEW_AFTER_EARNINGS: NotificationType.PROFIT_TAKING_SIGNAL,
}

_DISCLAIMER = "※最終的な投資判断は利用者が行ってください。"

_RECORD_DATE_UNKNOWN_REASON_LABELS: dict[RecordDateUnknownReason, str] = {
    RecordDateUnknownReason.SOURCE_NOT_FOUND: "未登録(要ユーザー登録)",
    RecordDateUnknownReason.PARSE_ERROR: "取得データの解析に失敗",
    RecordDateUnknownReason.CORPORATE_ACTION_UNRESOLVED: "企業行動未反映のため保留",
    RecordDateUnknownReason.DATA_PROVIDER_MISSING: "データ提供元が非対応(恒久的)",
    RecordDateUnknownReason.NOT_APPLICABLE: "該当なし",
}

_DIVIDEND_COMPARISON_OUTCOME_LABELS: dict[DividendComparisonOutcome, str] = {
    DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT: "減配(実績確定)",
    DividendComparisonOutcome.FORECAST_DIVIDEND_CUT: "予想減配(未確定)",
    DividendComparisonOutcome.SPLIT_ADJUSTMENT_ONLY: "分割調整のみ(実質的な減配ではない)",
    DividendComparisonOutcome.DIVIDEND_MAINTAINED: "配当維持",
    DividendComparisonOutcome.DIVIDEND_INCREASE: "増配",
    DividendComparisonOutcome.COMPARISON_NOT_POSSIBLE: "比較不可",
}


def _representative_price(recommendation: Recommendation) -> Decimal | None:
    if recommendation.buy_prices is not None and recommendation.buy_prices.standard is not None:
        return recommendation.buy_prices.standard.price
    if recommendation.sell_prices is not None:
        for level in (
            recommendation.sell_prices.recommended_limit_price,
            recommendation.sell_prices.stop_review_price,
            recommendation.sell_prices.partial_profit_start_price,
        ):
            if level is not None:
                return level.price
    return None


def _compute_content_hash(recommendation_type: RecommendationType) -> str:
    return hashlib.sha256(recommendation_type.value.encode()).hexdigest()[:16]


def _record_date_display(date: dt.date | None, reason: RecordDateUnknownReason | None) -> str:
    if date is not None:
        return date.isoformat()
    if reason is not None:
        return f"不明({_RECORD_DATE_UNKNOWN_REASON_LABELS[reason]})"
    return "不明"


def _confirmation_lines(recommendation: Recommendation) -> list[str]:
    """確認事項(要求仕様16節): 権利確定情報は理由コード付き、配当比較は比較年度付きで表示する。"""
    dividend_record = _record_date_display(
        recommendation.dividend_record_date, recommendation.dividend_record_date_unknown_reason
    )
    benefit_record = _record_date_display(
        recommendation.benefit_record_date, recommendation.benefit_record_date_unknown_reason
    )
    lines = [
        "【確認事項】",
        f"配当権利確定日: {dividend_record}",
        f"優待権利確定日: {benefit_record}",
    ]
    outcome = recommendation.dividend_comparison_outcome
    if outcome is not None:
        source_year = recommendation.dividend_comparison_source_fiscal_year or "不明"
        target_year = recommendation.dividend_comparison_target_fiscal_year or "不明"
        lines.append(
            f"配当比較({source_year} → {target_year}): "
            f"{_DIVIDEND_COMPARISON_OUTCOME_LABELS[outcome]}"
        )
    return lines


def _format_buy_message(recommendation: Recommendation, notification_type: NotificationType) -> str:
    title = (
        "買い候補"
        if notification_type == NotificationType.DAILY_BUY_CANDIDATES
        else "ウォッチリスト買い時"
    )
    lines = [
        f"【{title}】{recommendation.stock_code} {recommendation.stock_name}",
        f"判定: {recommendation.recommendation_type.value}",
        f"現在株価: {recommendation.price_at_recommendation}円",
    ]
    if recommendation.dividend_yield_pct_at_recommendation is not None:
        lines.append(f"予想配当利回り: {recommendation.dividend_yield_pct_at_recommendation:.2f}%")
    if recommendation.shareholder_benefit_yield_pct_at_recommendation is not None:
        lines.append(
            f"株主優待利回り: {recommendation.shareholder_benefit_yield_pct_at_recommendation:.2f}%"
        )
    lines.append(f"総合利回り: {recommendation.total_yield_pct_at_recommendation:.2f}%")
    bp = recommendation.buy_prices
    if bp is not None and bp.tentative and bp.standard and bp.aggressive:
        lines.append(
            f"打診買い:{bp.tentative.price}円 標準買い:{bp.standard.price}円 "
            f"積極買い:{bp.aggressive.price}円"
        )
        lines.append(f"次の判断条件: 標準買い価格({bp.standard.price}円)到達時に再検討")
    lines.append(f"総合スコア: {recommendation.total_score}")
    if recommendation.reasons:
        lines.append("推奨理由: " + " / ".join(recommendation.reasons))
    if recommendation.key_risks:
        lines.append("主なリスク: " + " / ".join(recommendation.key_risks))
    if recommendation.next_earnings_date:
        lines.append(f"次回決算予定日: {recommendation.next_earnings_date}")
    lines.extend(_confirmation_lines(recommendation))
    if recommendation.data_sources:
        fetched_at = min(s.fetched_at for s in recommendation.data_sources)
        lines.append(f"データ取得日時: {format_jst(fetched_at)}")
    lines.append(f"判定の信頼度: {recommendation.confidence.value}")
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_profit_taking_message(recommendation: Recommendation) -> str:
    lines = [
        f"【利確検討】{recommendation.stock_code} {recommendation.stock_name}",
        f"【保有状況】{recommendation.shares_at_recommendation}株 / "
        f"平均取得 {recommendation.average_purchase_price_at_recommendation}円 → "
        f"現在 {recommendation.price_at_recommendation}円",
        f"判定: {recommendation.recommendation_type.value}",
    ]
    if recommendation.reasons:
        lines.append("利確を検討する理由: " + " / ".join(recommendation.reasons))
    if recommendation.counter_factors:
        lines.append("直ちに売却としない理由: " + " / ".join(recommendation.counter_factors))
    sp = recommendation.sell_prices
    next_decision_lines: list[str] = []
    if sp is not None:
        if sp.partial_profit_start_price:
            p = sp.partial_profit_start_price
            suffix = "(即時執行目安)" if p.basis.name == "IMMEDIATE_EXECUTION_REFERENCE" else ""
            lines.append(f"一部利確開始価格: {p.price}円{suffix}")
        if sp.recommended_limit_price:
            p = sp.recommended_limit_price
            suffix = "(即時執行目安)" if p.basis.name == "IMMEDIATE_EXECUTION_REFERENCE" else ""
            lines.append(f"利確推奨価格(指値候補): {p.price}円{suffix}")
        elif recommendation.recommendation_type in (
            RecommendationType.PARTIAL_PROFIT_TAKE,
            RecommendationType.FULL_PROFIT_TAKE,
        ):
            lines.append("利確推奨価格(指値候補): 算出不能(総合利回り低下のみが根拠のため)")
        if sp.full_profit_consideration_price:
            price = sp.full_profit_consideration_price.price
            lines.append(f"全株利確検討価格(参考水準): {price}円")
            next_decision_lines.append(f"全株利確検討価格({price}円)到達時に再検討")
        if sp.reevaluation_price_upside:
            price = sp.reevaluation_price_upside.price
            lines.append(f"再評価価格(上昇時): {price}円")
            next_decision_lines.append(f"上昇時再評価価格({price}円)到達時に再検討")
    if next_decision_lines:
        lines.append("次の判断条件: " + " / ".join(next_decision_lines))
    if recommendation.next_earnings_date:
        lines.append(f"次回決算予定日: {recommendation.next_earnings_date}")
    lines.extend(_confirmation_lines(recommendation))
    if recommendation.data_sources:
        fetched_at = min(s.fetched_at for s in recommendation.data_sources)
        lines.append(f"データ取得日時: {format_jst(fetched_at)}")
    lines.append(f"判定の信頼度: {recommendation.confidence.value}")
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_sell_message(recommendation: Recommendation) -> str:
    lines = [
        f"【{recommendation.recommendation_type.value}】{recommendation.stock_code} "
        f"{recommendation.stock_name}(投資前提悪化の可能性)",
        f"判定: {recommendation.recommendation_type.value}",
        f"【保有状況】{recommendation.shares_at_recommendation}株 / "
        f"平均取得 {recommendation.average_purchase_price_at_recommendation}円 → "
        f"現在 {recommendation.price_at_recommendation}円",
    ]
    if recommendation.reasons:
        lines.append("悪化懸念(投資前提が悪化した理由): " + " / ".join(recommendation.reasons))
    if recommendation.counter_factors:
        lines.append("直ちに売却としない理由: " + " / ".join(recommendation.counter_factors))
    else:
        lines.append(
            "直ちに売却としない理由: "
            "本通知は投資前提悪化の可能性を示すものであり、執行タイミングの判断は利用者が行います"
        )
    sp = recommendation.sell_prices
    if sp is not None and sp.stop_review_price:
        lines.append(f"売却目安価格: {sp.stop_review_price.price}円")
        lines.append(f"次の判断条件: 売却目安価格({sp.stop_review_price.price}円)到達時に再検討")
    lines.append("保有を継続する場合のリスク: 投資前提の悪化が是正されない可能性があります")
    if recommendation.data_sources:
        fetched_at = min(s.fetched_at for s in recommendation.data_sources)
        lines.append(f"データ取得日時: {format_jst(fetched_at)}")
    lines.append(f"判定の信頼度: {recommendation.confidence.value}")
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_message(recommendation: Recommendation, notification_type: NotificationType) -> str:
    if notification_type in (
        NotificationType.DAILY_BUY_CANDIDATES,
        NotificationType.WATCHLIST_BUY_SIGNAL,
    ):
        return _format_buy_message(recommendation, notification_type)
    if notification_type == NotificationType.PROFIT_TAKING_SIGNAL:
        return _format_profit_taking_message(recommendation)
    return _format_sell_message(recommendation)


def render_notification_preview(recommendation: Recommendation) -> str:
    """実際に送信されるLINE通知本文と同じ内容を、送信せずに生成する(before/afterレポート用)。"""
    notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]
    return _format_message(recommendation, notification_type)


class LineNotificationService:
    def __init__(
        self,
        line_client: LineClient,
        notification_log_repository: NotificationLogRepository,
        recommendation_repository: RecommendationRepository,
        config: AppConfig,
        audit_service: AuditService | None = None,
    ) -> None:
        self._client = line_client
        self._log_repo = notification_log_repository
        self._recommendation_repo = recommendation_repository
        self._config = config
        self._audit = audit_service or AuditService()

    def notify_recommendation(self, recommendation: Recommendation, now: dt.datetime) -> bool:
        """再通知条件を満たす場合のみLINEへ送信する。送信した場合Trueを返す。

        単一の合流点(要求仕様11節・17節): 送信前に判定/価格の整合性検証と異常値検知を
        必ず実行し、いずれかでBLOCKING相当の問題を検出した場合は通常の推奨通知を送らず
        DATA_QUALITY_ALERTへ切り替える。
        """
        notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]
        previous = self._previous_recommendation(recommendation.stock_code, notification_type)

        alert = self._check_data_quality(recommendation, previous, notification_type, now)
        if alert is not None:
            return self.notify_data_quality_alert(alert, now)

        if not self._should_send(recommendation, previous, now):
            return False

        message = _format_message(recommendation, notification_type)
        self._client.push_message(message)
        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=notification_type,
                stock_code=recommendation.stock_code,
                content_hash=_compute_content_hash(recommendation.recommendation_type),
                sent_at=now,
                related_recommendation_id=recommendation.recommendation_id,
            )
        )
        return True

    def _check_data_quality(
        self,
        recommendation: Recommendation,
        previous: Recommendation | None,
        notification_type: NotificationType,
        now: dt.datetime,
    ) -> DataQualityAlert | None:
        del notification_type  # 将来process名の出し分けに使う可能性があるため引数として保持
        contradictions: list[str] = []
        suppressed_values: dict[str, str] = {}

        consistency_result = validate_recommendation(
            recommendation, self._config.data_validation.consistency_validation
        )
        for violation in consistency_result.violations:
            contradictions.append(f"[{violation.check_name}] {violation.description}")

        anomalies = detect_anomalies(
            recommendation.stock_code,
            recommendation,
            previous,
            self._config.data_validation.anomaly_detection,
        )
        for issue in anomalies:
            if issue.severity != DataQualityIssueSeverity.BLOCKING:
                continue
            contradictions.append(f"[{issue.check_name}] {issue.description}")
            suppressed_values.update(issue.suppressed_values)

        if not contradictions:
            return None

        alert = DataQualityAlert(
            stock_code=recommendation.stock_code,
            detected_at=now,
            process="notify_recommendation",
            contradictions=contradictions,
            suppressed_values=suppressed_values,
            recalculation_result=None,
            action_required=True,
        )

        self._audit.record(
            decision_type="notify_recommendation_quality_alert",
            stock_code=recommendation.stock_code,
            input_values={"recommendation_id": recommendation.recommendation_id},
            calculation_formulas={},
            output_values={"contradictions": contradictions},
            data_sources=list(recommendation.data_sources),
            rule_version=recommendation.rule_version,
            timestamp=now,
            consistency_validation_result={
                "passed": consistency_result.passed,
                "violations": [
                    {"check_name": v.check_name, "description": v.description}
                    for v in consistency_result.violations
                ],
            },
            suppressed_rules=[issue.check_name for issue in anomalies],
            notification_values={"suppressed_values": suppressed_values},
        )
        return alert

    def notify_data_quality_alert(self, alert: DataQualityAlert, now: dt.datetime) -> bool:
        """判定/価格の矛盾または異常値を検出した際、通常の推奨通知の代わりに送信する。

        同一銘柄・同一内容(矛盾リストが同一)のアラートは再送しない。
        """
        content_hash = hashlib.sha256(
            f"{alert.stock_code}|{'|'.join(alert.contradictions)}".encode()
        ).hexdigest()[:16]
        latest = self._log_repo.latest_by_stock_and_type(
            alert.stock_code, NotificationType.DATA_QUALITY_ALERT
        )
        if latest is not None and latest.content_hash == content_hash:
            return False

        lines = [
            f"【データ品質アラート】{alert.stock_code}",
            f"検出元処理: {alert.process}",
            "検出した矛盾・異常: " + " / ".join(alert.contradictions),
        ]
        if alert.suppressed_values:
            values = ", ".join(f"{k}={v}" for k, v in alert.suppressed_values.items())
            lines.append(f"使用を停止した値: {values}")
        if alert.recalculation_result:
            lines.append(f"再計算結果: {alert.recalculation_result}")
        lines.append(f"対応要否: {'要対応' if alert.action_required else '参考情報'}")
        lines.append(f"検出日時: {format_jst(alert.detected_at)}")
        lines.append("この銘柄の通常の売買推奨通知は、問題が解消されるまで抑止されます。")
        lines.append(_DISCLAIMER)
        self._client.push_message("\n".join(lines))

        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=NotificationType.DATA_QUALITY_ALERT,
                stock_code=alert.stock_code,
                content_hash=content_hash,
                sent_at=now,
                related_recommendation_id=None,
            )
        )
        return True

    def notify_disclosure_risk(
        self,
        stock_code: str,
        disclosure_title: str,
        disclosure_summary: str | None,
        matched_keywords: list[str],
        published_at: dt.datetime,
        now: dt.datetime,
    ) -> bool:
        """適時開示からリスクキーワードが検出された場合に速報として送信する。

        同一開示(published_at+タイトルで識別)は再送しない。
        """
        content_hash = hashlib.sha256(
            f"{stock_code}|{published_at.isoformat()}|{disclosure_title}".encode()
        ).hexdigest()[:16]
        latest = self._log_repo.latest_by_stock_and_type(
            stock_code, NotificationType.IMPORTANT_DISCLOSURE
        )
        if latest is not None and latest.content_hash == content_hash:
            return False

        lines = [
            f"【重要開示検知】{stock_code}",
            f"検出キーワード: {', '.join(matched_keywords)}",
            f"開示タイトル: {disclosure_title}",
        ]
        if disclosure_summary:
            lines.append(f"概要: {disclosure_summary[:300]}")
        lines.append(f"開示日時: {format_jst(published_at)}")
        lines.append(_DISCLAIMER)
        self._client.push_message("\n".join(lines))

        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=NotificationType.IMPORTANT_DISCLOSURE,
                stock_code=stock_code,
                content_hash=content_hash,
                sent_at=now,
                related_recommendation_id=None,
            )
        )
        return True

    def notify_data_error(self, stock_code: str, message: str, now: dt.datetime) -> bool:
        content_hash = hashlib.sha256(message.encode()).hexdigest()[:16]
        latest = self._log_repo.latest_by_stock_and_type(stock_code, NotificationType.DATA_ERROR)
        if latest is not None and latest.content_hash == content_hash:
            days_elapsed = (now.date() - latest.sent_at.date()).days
            if days_elapsed < self._config.notification.resend_after_days:
                return False

        text = (
            f"【データ取得エラー】{stock_code}\n{message}\n"
            f"データ取得日時: {format_jst(now)}\n{_DISCLAIMER}"
        )
        self._client.push_message(text)
        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=NotificationType.DATA_ERROR,
                stock_code=stock_code,
                content_hash=content_hash,
                sent_at=now,
                related_recommendation_id=None,
            )
        )
        return True

    def _previous_recommendation(
        self, stock_code: str, notification_type: NotificationType
    ) -> Recommendation | None:
        latest_log = self._log_repo.latest_by_stock_and_type(stock_code, notification_type)
        if latest_log is None or latest_log.related_recommendation_id is None:
            return None
        return self._recommendation_repo.get(latest_log.related_recommendation_id)

    def _should_send(
        self,
        recommendation: Recommendation,
        previous: Recommendation | None,
        now: dt.datetime,
    ) -> bool:
        notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]
        latest_log = self._log_repo.latest_by_stock_and_type(
            recommendation.stock_code, notification_type
        )
        if latest_log is None:
            return True
        if previous is None:
            return True

        if previous.recommendation_type != recommendation.recommendation_type:
            return True

        prev_price = _representative_price(previous)
        new_price = _representative_price(recommendation)
        if prev_price is not None and new_price is not None and prev_price > 0:
            change_pct = abs(float(new_price / prev_price - 1) * 100)
            if change_pct >= self._config.notification.price_change_resend_threshold_pct:
                return True

        days_elapsed = (now.date() - latest_log.sent_at.date()).days
        return days_elapsed >= self._config.notification.resend_after_days
