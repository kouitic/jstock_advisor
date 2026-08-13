"""NEAR BUY専用ランキング→finalizeループの結合テスト(BUY候補裾野拡大機能2026-08、指摘1)。

NEAR BUY該当銘柄(buy_action=WATCH_FOR_PRICE, watch_type=NEAR_BUY)が
_finalize_batch()の新設ループを通じて実際にsend_recommendation_notification()
まで到達することを確認する(単にゲート判定の戻り値を見るだけでなく、
BUY候補Lambda側の経路そのものが新設されていることの回帰テスト)。
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel, WatchType
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module
from jstock_advisor.services.audit_service import AuditService as RealAuditService

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


def _patch_audit(monkeypatch, tmp_path: Path) -> None:
    repo = AuditLogRepository(store_dir=tmp_path)
    monkeypatch.setattr(
        handler_module, "AuditService", lambda *a, **kw: RealAuditService(repository=repo)
    )


def _near_buy_recommendation(
    stock_code: str, recommendation_id: str, distance_pct: str, quality_score: float
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        recommended_at=_NOW,
        recommendation_type=handler_module.RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("150"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("140"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("130"), rationale="x"),
        ),
        price_at_recommendation=Decimal("158"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        watch_type=WatchType.NEAR_BUY,
        company_quality_score=quality_score,
        required_decline_to_entry_pct=Decimal(distance_pct),
    )


class _FakeNearBuyNotificationService:
    def __init__(self) -> None:
        self.sent: list[Recommendation] = []
        self.batch_summary_calls: list[dict[str, object]] = []

    def check_data_quality_eligibility(self, recommendation, now, context=None):
        return NotificationEligibility(eligible=True)

    def check_trade_cooldown_eligibility(self, recommendation, now):
        return NotificationEligibility(eligible=True)

    def check_cross_pipeline_priority_eligibility(self, recommendation, now):
        return NotificationEligibility(eligible=True)

    def check_resend_eligibility(self, recommendation, now):
        return NotificationEligibility(eligible=True)

    def send_recommendation_notification(self, recommendation, now) -> None:
        self.sent.append(recommendation)

    def notify_buy_candidates_digest(self, winners, now):
        return {}

    def notify_batch_summary(self, process_name, total, category_counts, now, **kwargs):
        self.batch_summary_calls.append({"near_buy_sent_count": kwargs.get("near_buy_sent_count")})
        return True


def test_near_buy_candidate_reaches_send_recommendation_notification(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_audit(monkeypatch, tmp_path)
    repo = RecommendationRepository(store_dir=tmp_path)

    rec = _near_buy_recommendation("9432", "near-1", "5.1", 65.0)
    repo.save(rec)
    near_buy_entries = [handler_module._encode_near_buy_ranking_entry(rec)]

    progress = handler_module.BatchProgress(
        total=1,
        completed=1,
        category_counts={"watch_not_ranked": 1},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
        near_buy_ranking_entries=near_buy_entries,
    )
    fake_service = _FakeNearBuyNotificationService()

    handler_module._finalize_batch(progress, _CONFIG, _NOW, repo, fake_service)

    assert len(fake_service.sent) == 1
    assert fake_service.sent[0].stock_code == "9432"
    assert fake_service.batch_summary_calls[0]["near_buy_sent_count"] == 1


def test_near_buy_daily_limit_stops_further_sends(monkeypatch, tmp_path: Path) -> None:
    """NEAR BUYの日次上限(既定5件)を超える分は送信されないが、監査には記録される
    (WatchState自体の継続はwatch_state_service側の責務のためここでは検証しない)。"""
    _patch_audit(monkeypatch, tmp_path)
    repo = RecommendationRepository(store_dir=tmp_path)

    limited_config = _CONFIG.model_copy(
        update={
            "buy_decision": _CONFIG.buy_decision.model_copy(
                update={
                    "near_buy": _CONFIG.buy_decision.near_buy.model_copy(
                        update={"daily_max_notifications": 2}
                    )
                }
            )
        }
    )

    near_buy_entries = []
    for i in range(4):
        rec = _near_buy_recommendation(f"100{i}", f"near-{i}", f"{i + 1}.0", 65.0)
        repo.save(rec)
        near_buy_entries.append(handler_module._encode_near_buy_ranking_entry(rec))

    progress = handler_module.BatchProgress(
        total=4,
        completed=4,
        category_counts={"watch_not_ranked": 4},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
        near_buy_ranking_entries=near_buy_entries,
    )
    fake_service = _FakeNearBuyNotificationService()

    handler_module._finalize_batch(progress, limited_config, _NOW, repo, fake_service)

    assert len(fake_service.sent) == 2
    # distance_pct昇順(近い順)に送信されるため、最も近い2件が選ばれる
    assert {r.stock_code for r in fake_service.sent} == {"1000", "1001"}
