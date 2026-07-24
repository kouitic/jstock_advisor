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
from jstock_advisor.domain.entities.enums import NotificationType, RecommendationType
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

_RECOMMENDATION_TO_NOTIFICATION_TYPE: dict[RecommendationType, NotificationType] = {
    RecommendationType.BUY: NotificationType.DAILY_BUY_CANDIDATES,
    RecommendationType.WATCH_BUY: NotificationType.WATCHLIST_BUY_SIGNAL,
    RecommendationType.WATCH: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.PARTIAL_PROFIT_TAKE: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.FULL_PROFIT_TAKE: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.SELL: NotificationType.SELL_SIGNAL,
    RecommendationType.URGENT_REVIEW: NotificationType.SELL_SIGNAL,
}

_DISCLAIMER = "※最終的な投資判断は利用者が行ってください。"


def _representative_price(recommendation: Recommendation) -> Decimal | None:
    if recommendation.buy_prices is not None and recommendation.buy_prices.standard is not None:
        return recommendation.buy_prices.standard.price
    if recommendation.sell_prices is not None:
        for level in (
            recommendation.sell_prices.profit_take_recommended,
            recommendation.sell_prices.premise_deterioration_target,
            recommendation.sell_prices.partial_take_start,
        ):
            if level is not None:
                return level.price
    return None


def _compute_content_hash(recommendation_type: RecommendationType) -> str:
    return hashlib.sha256(recommendation_type.value.encode()).hexdigest()[:16]


def _record_months(recommendation: Recommendation) -> str:
    months = sorted(
        {
            d.month
            for d in (recommendation.dividend_record_date, recommendation.benefit_record_date)
            if d
        }
    )
    return "・".join(f"{m}月" for m in months) if months else "不明"


def _format_buy_message(recommendation: Recommendation, notification_type: NotificationType) -> str:
    title = (
        "買い候補"
        if notification_type == NotificationType.DAILY_BUY_CANDIDATES
        else "ウォッチリスト買い時"
    )
    lines = [
        f"【{title}】{recommendation.stock_code} {recommendation.stock_name}",
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
    lines.append(f"総合スコア: {recommendation.total_score}")
    if recommendation.reasons:
        lines.append("推奨理由: " + " / ".join(recommendation.reasons))
    if recommendation.key_risks:
        lines.append("主なリスク: " + " / ".join(recommendation.key_risks))
    if recommendation.next_earnings_date:
        lines.append(f"次回決算予定日: {recommendation.next_earnings_date}")
    lines.append(f"権利確定月: {_record_months(recommendation)}")
    if recommendation.data_sources:
        fetched_at = min(s.fetched_at for s in recommendation.data_sources)
        lines.append(f"データ取得日時: {format_jst(fetched_at)}")
    lines.append(f"判定の信頼度: {recommendation.confidence.value}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_profit_taking_message(recommendation: Recommendation) -> str:
    lines = [
        f"【利確検討】{recommendation.stock_code} {recommendation.stock_name}",
        f"保有: {recommendation.shares_at_recommendation}株 / "
        f"平均取得 {recommendation.average_purchase_price_at_recommendation}円 → "
        f"現在 {recommendation.price_at_recommendation}円",
        f"判定: {recommendation.recommendation_type.value}",
    ]
    if recommendation.reasons:
        lines.append("利確を検討する理由: " + " / ".join(recommendation.reasons))
    if recommendation.counter_factors:
        lines.append("保有継続を支持する要因: " + " / ".join(recommendation.counter_factors))
    sp = recommendation.sell_prices
    if sp is not None:
        if sp.partial_take_start:
            lines.append(f"一部利確開始価格: {sp.partial_take_start.price}円")
        if sp.profit_take_recommended:
            lines.append(f"利確推奨価格: {sp.profit_take_recommended.price}円")
        if sp.full_take_consider:
            lines.append(f"全株利確検討価格: {sp.full_take_consider.price}円")
        if sp.reassessment_price:
            lines.append(f"再評価価格: {sp.reassessment_price.price}円")
    if recommendation.next_earnings_date:
        lines.append(f"次回決算予定日: {recommendation.next_earnings_date}")
    lines.append(f"権利確定月: {_record_months(recommendation)}")
    if recommendation.data_sources:
        fetched_at = min(s.fetched_at for s in recommendation.data_sources)
        lines.append(f"データ取得日時: {format_jst(fetched_at)}")
    lines.append(f"判定の信頼度: {recommendation.confidence.value}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_sell_message(recommendation: Recommendation) -> str:
    lines = [
        f"【{recommendation.recommendation_type.value}】{recommendation.stock_code} "
        f"{recommendation.stock_name}(投資前提悪化の可能性)",
        f"保有: {recommendation.shares_at_recommendation}株 / "
        f"平均取得 {recommendation.average_purchase_price_at_recommendation}円 → "
        f"現在 {recommendation.price_at_recommendation}円",
    ]
    if recommendation.reasons:
        lines.append("投資前提が悪化した理由: " + " / ".join(recommendation.reasons))
    sp = recommendation.sell_prices
    if sp is not None and sp.premise_deterioration_target:
        lines.append(f"売却目安価格: {sp.premise_deterioration_target.price}円")
    lines.append("保有を継続する場合のリスク: 投資前提の悪化が是正されない可能性があります")
    if recommendation.data_sources:
        fetched_at = min(s.fetched_at for s in recommendation.data_sources)
        lines.append(f"データ取得日時: {format_jst(fetched_at)}")
    lines.append(f"判定の信頼度: {recommendation.confidence.value}")
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


class LineNotificationService:
    def __init__(
        self,
        line_client: LineClient,
        notification_log_repository: NotificationLogRepository,
        recommendation_repository: RecommendationRepository,
        config: AppConfig,
    ) -> None:
        self._client = line_client
        self._log_repo = notification_log_repository
        self._recommendation_repo = recommendation_repository
        self._config = config

    def notify_recommendation(self, recommendation: Recommendation, now: dt.datetime) -> bool:
        """再通知条件を満たす場合のみLINEへ送信する。送信した場合Trueを返す。"""
        notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]

        if not self._should_send(recommendation, notification_type, now):
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

    def _should_send(
        self,
        recommendation: Recommendation,
        notification_type: NotificationType,
        now: dt.datetime,
    ) -> bool:
        latest_log = self._log_repo.latest_by_stock_and_type(
            recommendation.stock_code, notification_type
        )
        if latest_log is None:
            return True

        previous = (
            self._recommendation_repo.get(latest_log.related_recommendation_id)
            if latest_log.related_recommendation_id
            else None
        )
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
