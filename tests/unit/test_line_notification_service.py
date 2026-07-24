import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.line_notification_service import LineNotificationService

_CONFIG = load_config()
_NOW = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.UTC)


class _FakeLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)

    def reply_message(self, reply_token: str, text: str) -> None:
        self.sent.append(text)


def _make_recommendation(
    *, recommendation_id: str, recommendation_type: RecommendationType, standard_price: str
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        buy_prices=BuyPriceLevels(
            tentative=PriceWithRationale(price=Decimal("3500"), rationale="x"),
            standard=PriceWithRationale(price=Decimal(standard_price), rationale="x"),
            aggressive=PriceWithRationale(price=Decimal("3100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        dividend_yield_pct_at_recommendation=4.5,
        total_yield_pct_at_recommendation=4.5,
        total_score=60.0,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


@pytest.fixture
def service_and_repos(
    tmp_path: Path,
) -> tuple[LineNotificationService, RecommendationRepository, _FakeLineClient]:
    store_dir = tmp_path / "local_store"
    recommendation_repo = RecommendationRepository(store_dir=store_dir)
    notification_log_repo = NotificationLogRepository(store_dir=store_dir)
    client = _FakeLineClient()
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=notification_log_repo,
        recommendation_repository=recommendation_repo,
        config=_CONFIG,
    )
    return service, recommendation_repo, client


def test_first_notification_is_sent(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)
    assert sent is True
    assert len(client.sent) == 1
    assert "2914" in client.sent[0]
    assert "最終的な投資判断は利用者が行って" in client.sent[0]


def test_duplicate_same_day_is_suppressed(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec1 = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    rec2 = _make_recommendation(
        recommendation_id="rec-2", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(hours=1))

    assert sent is False
    assert len(client.sent) == 1


def test_resend_when_judgment_type_changes(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec1 = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    rec2 = _make_recommendation(
        recommendation_id="rec-2",
        recommendation_type=RecommendationType.WATCH_BUY,
        standard_price="3359",
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(hours=1))

    assert sent is True
    assert len(client.sent) == 2


def test_resend_when_price_changes_beyond_threshold(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec1 = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3000"
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    # 標準買い価格が3000 -> 3200円(+6.7%)。閾値3.0%を超えるため再通知される
    rec2 = _make_recommendation(
        recommendation_id="rec-2", recommendation_type=RecommendationType.BUY, standard_price="3200"
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(hours=1))

    assert sent is True
    assert len(client.sent) == 2


def test_no_resend_when_price_change_within_threshold(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec1 = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3000"
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    # +1%程度の変化は閾値未満なので抑止される
    rec2 = _make_recommendation(
        recommendation_id="rec-2", recommendation_type=RecommendationType.BUY, standard_price="3030"
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(hours=1))

    assert sent is False
    assert len(client.sent) == 1


def test_resend_after_days_elapsed(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec1 = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    rec2 = _make_recommendation(
        recommendation_id="rec-2", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec2)
    later = _NOW + dt.timedelta(days=_CONFIG.notification.resend_after_days)
    sent = service.notify_recommendation(rec2, later)

    assert sent is True
    assert len(client.sent) == 2


def test_data_error_notification_dedup(service_and_repos) -> None:
    service, _repo, client = service_and_repos
    sent1 = service.notify_data_error("9999", "株価データを取得できません", _NOW)
    sent2 = service.notify_data_error(
        "9999", "株価データを取得できません", _NOW + dt.timedelta(hours=1)
    )
    assert sent1 is True
    assert sent2 is False
    assert len(client.sent) == 1


def test_data_error_notification_resends_for_different_message(service_and_repos) -> None:
    service, _repo, client = service_and_repos
    service.notify_data_error("9999", "株価データを取得できません", _NOW)
    sent = service.notify_data_error(
        "9999", "財務データを取得できません", _NOW + dt.timedelta(hours=1)
    )
    assert sent is True
    assert len(client.sent) == 2


def test_disclosure_risk_notification_is_sent(service_and_repos) -> None:
    service, _repo, client = service_and_repos
    sent = service.notify_disclosure_risk(
        stock_code="2914",
        disclosure_title="臨時報告書",
        disclosure_summary="特別調査委員会の設置について",
        matched_keywords=["特別調査委員会"],
        published_at=_NOW,
        now=_NOW,
    )
    assert sent is True
    assert len(client.sent) == 1
    assert "2914" in client.sent[0]
    assert "特別調査委員会" in client.sent[0]


def test_disclosure_risk_notification_dedup_for_same_disclosure(service_and_repos) -> None:
    service, _repo, client = service_and_repos
    service.notify_disclosure_risk(
        stock_code="2914",
        disclosure_title="臨時報告書",
        disclosure_summary="特別調査委員会の設置について",
        matched_keywords=["特別調査委員会"],
        published_at=_NOW,
        now=_NOW,
    )
    sent = service.notify_disclosure_risk(
        stock_code="2914",
        disclosure_title="臨時報告書",
        disclosure_summary="特別調査委員会の設置について",
        matched_keywords=["特別調査委員会"],
        published_at=_NOW,
        now=_NOW + dt.timedelta(hours=1),
    )
    assert sent is False
    assert len(client.sent) == 1


def test_disclosure_risk_notification_resends_for_different_disclosure(service_and_repos) -> None:
    service, _repo, client = service_and_repos
    service.notify_disclosure_risk(
        stock_code="2914",
        disclosure_title="臨時報告書",
        disclosure_summary="特別調査委員会の設置について",
        matched_keywords=["特別調査委員会"],
        published_at=_NOW,
        now=_NOW,
    )
    sent = service.notify_disclosure_risk(
        stock_code="2914",
        disclosure_title="訂正臨時報告書",
        disclosure_summary="継続企業の前提に関する重要事象",
        matched_keywords=["継続企業の前提に関する重要事象"],
        published_at=_NOW + dt.timedelta(days=1),
        now=_NOW + dt.timedelta(days=1),
    )
    assert sent is True
    assert len(client.sent) == 2
