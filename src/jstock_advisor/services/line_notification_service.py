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
import logging
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

logger = logging.getLogger(__name__)

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
    # --- 売却判定エンジンの再設計(2026-07仕様)で追加 ---
    RecommendationType.REVIEW: NotificationType.SELL_SIGNAL,
    RecommendationType.MANUAL_REVIEW_REQUIRED: NotificationType.MANUAL_REVIEW_REQUIRED,
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

# データ品質アラートの対応内容(要求仕様18節): check_nameごとに「何を確認・実施すべきか」を明示する
_RECOMMENDED_ACTION_BY_CHECK: dict[str, str] = {
    "full_take_extreme_margin": (
        "全株利確検討価格が現在値から極端に乖離しています。"
        "適正価格算出の入力データ(企業行動調整・財務データ等)に誤りがないか確認してください。"
    ),
    "full_take_no_price_guidance": (
        "全株利確判定なのに指値候補が算出されていません。データ取得状況を確認してください。"
    ),
    "watch_recommends_immediate_sell": (
        "監視判定なのに即時執行目安の価格が提示されています。判定ロジックの不整合の"
        "可能性があるため、該当銘柄の他の通知履歴と照合してください。"
    ),
    "three_or_more_equal_prices": (
        "3つ以上の価格フィールドが同一値になっています。適正価格・利確価格の"
        "算出元データを確認してください。"
    ),
    "reevaluation_unreasonably_above_full_take": (
        "再評価価格が全株利確検討価格より不合理に高くなっています。"
        "適正価格レンジの算出結果を確認してください。"
    ),
    "low_fair_value_confidence_full_take": (
        "適正価格の信頼度がLOWのまま全株利確判定が出ています。"
        "適正価格算出に使われた手法数・データ鮮度を確認してください。"
    ),
    "full_take_with_insufficient_gain_and_reasons": (
        "含み益率が全利確閾値未満なのに根拠が少数です。判定ロジックの条件充足状況を"
        "確認してください。"
    ),
    "sufficient_yield_full_take_on_yield_alone": (
        "総合利回りが基準以上なのに利回り低下のみを根拠に全株利確が出ています。"
        "配当・優待データを確認してください。"
    ),
    "price_equals_current_with_target_basis": (
        "価格フィールドが現在値と一致していますが、目標価格として扱われています。"
        "basis(算出根拠の種別)の設定を確認してください。"
    ),
    "fair_value_out_of_plausible_range": (
        "適正価格が現在株価から大きく外れた倍率になっています。"
        "財務データ(EPS/BPS等)の取得元・算出ロジックを確認してください。"
    ),
    "fair_value_changed_sharply": (
        "前回分析から適正価格が大きく変動しています。決算発表・企業行動(株式分割等)・"
        "データ取得エラーのいずれかが原因である可能性があります。CloudWatch Logsで"
        "直近の分析ログを確認してください。"
    ),
    "price_change_resembles_split_ratio": (
        "株価が前回から典型的な分割比率に近い倍率で変化しています。未反映の株式分割が"
        "ないか確認し、必要であれば企業行動レジストリに手動登録してください。"
    ),
    "full_profit_take_with_unrealized_loss": (
        "全株利確判定なのに含み損になっています。判定ロジックに重大な誤りがある"
        "可能性が高いため、優先的に確認してください。"
    ),
}
_DEFAULT_RECOMMENDED_ACTION = (
    "検出内容を確認し、必要に応じてデータの再取得や手動確認を行ってください。"
    "原因が分からない場合はCloudWatch Logsで該当銘柄のログを確認してください。"
)


def _build_recommended_action(check_names: list[str]) -> str:
    actions = dict.fromkeys(
        _RECOMMENDED_ACTION_BY_CHECK.get(name, _DEFAULT_RECOMMENDED_ACTION) for name in check_names
    )
    return " / ".join(actions) if actions else _DEFAULT_RECOMMENDED_ACTION


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
        lines.append("反対材料: " + " / ".join(recommendation.counter_factors))
    if recommendation.recommended_action_summary:
        lines.append("判定内容: " + recommendation.recommended_action_summary)
    sp = recommendation.sell_prices
    if sp is not None and sp.immediate_execution_price:
        lines.append(f"即時執行目安価格: {sp.immediate_execution_price.price}円")
    if sp is not None and sp.stop_review_price:
        lines.append(f"売却目安価格: {sp.stop_review_price.price}円")
    if recommendation.next_review_conditions:
        lines.append("次の判断条件: " + " / ".join(recommendation.next_review_conditions))
    if recommendation.holding_risks:
        lines.append("保有を継続する場合のリスク: " + " / ".join(recommendation.holding_risks))
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

        alert, requires_manual_review = self._check_data_quality(
            recommendation, previous, notification_type, now
        )
        if alert is not None:
            if requires_manual_review:
                return self.notify_manual_review_required(recommendation, alert, now)
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
    ) -> tuple[DataQualityAlert | None, bool]:
        del notification_type  # 将来process名の出し分けに使う可能性があるため引数として保持
        contradictions: list[str] = []
        suppressed_values: dict[str, str] = {}
        check_names: list[str] = []

        consistency_result = validate_recommendation(
            recommendation, self._config.data_validation.consistency_validation
        )
        for violation in consistency_result.violations:
            contradictions.append(f"[{violation.check_name}] {violation.description}")
            check_names.append(violation.check_name)

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
            check_names.append(issue.check_name)

        if not contradictions:
            return None, False

        alert = DataQualityAlert(
            stock_code=recommendation.stock_code,
            stock_name=recommendation.stock_name,
            detected_at=now,
            process="notify_recommendation",
            contradictions=contradictions,
            suppressed_values=suppressed_values,
            recalculation_result=None,
            action_required=True,
            recommended_action=_build_recommended_action(check_names),
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
            notification_values={
                "suppressed_values": suppressed_values,
                "recommended_action": alert.recommended_action,
            },
        )
        return alert, consistency_result.requires_manual_review

    def notify_data_quality_alert(self, alert: DataQualityAlert, now: dt.datetime) -> bool:
        """判定/価格の矛盾または異常値を検出した際、通常の推奨通知の代わりに呼ばれる。

        LINEへは個別送信しない(要求仕様: 個別のデータ品質アラートはチャットへ
        配信せず、バッチ全体のサマリー通知(notify_batch_summary)に集約する)。
        検出内容・対応内容はCloudWatch Logsおよび監査ログ(_check_data_quality内で
        AuditServiceへ記録済み)で追跡できる。LINEへは何も送信していないためFalseを返す
        (戻り値は「メッセージを送信したか」を表す既存の意味を保つ)。
        """
        del now
        name_part = f" {alert.stock_name}" if alert.stock_name else ""
        logger.warning(
            "data_quality_alert stock_code=%s%s process=%s contradictions=%s recommended_action=%s",
            alert.stock_code,
            name_part,
            alert.process,
            alert.contradictions,
            alert.recommended_action,
        )
        return False

    def notify_manual_review_required(
        self, recommendation: Recommendation, alert: DataQualityAlert, now: dt.datetime
    ) -> bool:
        """SELL/URGENT_REVIEW等の自動判定が安全条件(独立根拠件数・一次情報確認等)を
        満たさない場合、自動通知の代わりに手動確認を促すメッセージを送信する
        (要求仕様§15・§16)。DATA_QUALITY_ALERTと異なり、これは実際にLINEへ配信する
        (根拠不足のSELL/URGENT_REVIEWは自動で確定させず、必ず人間の確認を経由させるため)。
        """
        lines = [
            f"【要手動確認】{recommendation.stock_code} {recommendation.stock_name}",
            "自動売却判定の根拠に、業種別評価未対応・独立根拠不足・一次情報未確認の"
            "いずれかの項目が含まれています。",
            "検出内容:",
            *[f"・{c}" for c in alert.contradictions],
            f"自動判定結果: {recommendation.recommendation_type.value}",
            "自動売却推奨: 停止(手動確認が完了するまで自動での売却推奨は行いません)",
            "確認事項:",
        ]
        if recommendation.reasons:
            lines.append(f"・検出された懸念事項: {' / '.join(recommendation.reasons)}")
        lines.append("・一次情報(EDINET/TDnet等)での事実確認")
        if recommendation.next_earnings_date:
            lines.append(f"・次回決算({recommendation.next_earnings_date})の内容")
        lines.append(_DISCLAIMER)
        self._client.push_message("\n".join(lines))

        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=NotificationType.MANUAL_REVIEW_REQUIRED,
                stock_code=recommendation.stock_code,
                content_hash=_compute_content_hash(recommendation.recommendation_type),
                sent_at=now,
                related_recommendation_id=recommendation.recommendation_id,
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
        stock_name: str | None = None,
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

        name_part = f" {stock_name}" if stock_name else ""
        lines = [
            f"【重要開示検知】{stock_code}{name_part}",
            f"検出キーワード: {', '.join(matched_keywords)}",
            f"開示タイトル: {disclosure_title}",
        ]
        if disclosure_summary:
            lines.append(f"概要: {disclosure_summary[:300]}")
        lines.append("対応内容: 開示内容を確認し、投資前提に影響がないか確認してください。")
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

    def notify_data_error(
        self, stock_code: str, message: str, now: dt.datetime, stock_name: str | None = None
    ) -> bool:
        """データ取得エラーが発生した際に呼ばれる。

        LINEへは個別送信しない(要求仕様: 個別のデータ取得エラーはチャットへ配信せず、
        バッチ全体のサマリー通知(notify_batch_summary)に集約する)。詳細はCloudWatch
        Logsで追跡できる。LINEへは何も送信していないためFalseを返す。
        """
        del now
        name_part = f" {stock_name}" if stock_name else ""
        logger.warning("data_error stock_code=%s%s message=%s", stock_code, name_part, message)
        return False

    def notify_batch_summary(
        self, process_name: str, total: int, succeeded: int, failed: int, now: dt.datetime
    ) -> bool:
        """銘柄単位ファンアウト(lambda_handlers/_fanout.py)の全件処理完了後に1回だけ送る、
        全体件数・正常件数・異常件数のサマリー通知。個別のデータ取得エラー・データ品質
        アラートはこれに集約され、個別には送信しない。

        ファンアウトの起動元(スケジューラ・手動実行)が何らかの理由で二重ディスパッチ
        された場合、独立した2つのbatch_idがそれぞれ完了を検知してこのメソッドを
        呼び出しうる。それぞれのbatch_idの完了自体は正しい検知だが、結果として
        まったく同一内容のサマリーがLINEへ二重送信されることを防ぐため、同一日付・
        同一内容(件数)の通知が既に送信済みの場合は送信をスキップする。
        """
        pseudo_stock_code = f"__batch__:{process_name}"
        content_hash = hashlib.sha256(
            f"{process_name}|{now.date().isoformat()}|{total}|{succeeded}|{failed}".encode()
        ).hexdigest()[:16]
        latest = self._log_repo.latest_by_stock_and_type(
            pseudo_stock_code, NotificationType.BATCH_SUMMARY
        )
        if latest is not None and latest.content_hash == content_hash:
            logger.info(
                "batch_summary duplicate suppressed process_name=%s total=%d succeeded=%d "
                "failed=%d",
                process_name,
                total,
                succeeded,
                failed,
            )
            return False

        lines = [
            f"【処理完了】{process_name}",
            f"全体処理件数: {total}",
            f"正常件数: {succeeded}",
            f"異常件数: {failed}",
            f"完了日時: {format_jst(now)}",
            _DISCLAIMER,
        ]
        self._client.push_message("\n".join(lines))
        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=NotificationType.BATCH_SUMMARY,
                stock_code=pseudo_stock_code,
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
