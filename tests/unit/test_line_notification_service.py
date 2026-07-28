import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    RecommendationType,
    RecordDateUnknownReason,
)
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


def test_data_error_notification_is_not_sent_to_line(service_and_repos, caplog) -> None:
    # 個別のデータ取得エラーはLINEへ配信せず、バッチサマリーに集約する
    service, _repo, client = service_and_repos
    with caplog.at_level("WARNING"):
        sent = service.notify_data_error("9999", "株価データを取得できません", _NOW)

    assert sent is False
    assert client.sent == []
    assert "data_error stock_code=9999" in caplog.text


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


def _make_full_profit_take_recommendation(
    *, recommendation_id: str, full_take_price: str
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            full_profit_consideration_price=PriceWithRationale(
                price=Decimal(full_take_price), rationale="x"
            )
        ),
        price_at_recommendation=Decimal("4200"),
        reasons=["適正価格レンジ上限を超過"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def test_recommendation_with_consistency_violation_suppresses_normal_notification(
    service_and_repos, caplog
) -> None:
    service, repo, client = service_and_repos
    # 全株利確検討価格が現在値の100%以上高く、極端な乖離(full_take_extreme_margin)。
    # データ品質アラートはLINEへ個別送信せず、通常の推奨通知のみを抑止する
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="9000"
    )
    repo.save(rec)

    with caplog.at_level("WARNING"):
        sent = service.notify_recommendation(rec, _NOW)

    assert sent is False
    assert client.sent == []
    assert "full_take_extreme_margin" in caplog.text
    assert "stock_code=2914" in caplog.text


def test_clean_full_profit_take_is_sent_normally(service_and_repos) -> None:
    service, repo, client = service_and_repos
    # 現在値+10%程度の穏当な価格なので、整合性検証・異常値検知いずれも問題を検出しない
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    assert len(client.sent) == 1
    assert "データ品質アラート" not in client.sent[0]
    assert "全株利確検討価格" in client.sent[0]
    assert f"通知ID: {rec.recommendation_id}" in client.sent[0]


def test_message_shows_record_date_unknown_reason_instead_of_bare_unknown(
    service_and_repos,
) -> None:
    service, repo, client = service_and_repos
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(
        update={
            "dividend_record_date": None,
            "dividend_record_date_unknown_reason": RecordDateUnknownReason.DATA_PROVIDER_MISSING,
        }
    )
    repo.save(rec)

    service.notify_recommendation(rec, _NOW)

    assert "不明(データ提供元が非対応(恒久的))" in client.sent[0]


def test_message_shows_dividend_comparison_with_fiscal_years(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(
        update={
            "dividend_comparison_source_fiscal_year": "2025",
            "dividend_comparison_target_fiscal_year": "2026",
            "dividend_comparison_outcome": DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT,
        }
    )
    repo.save(rec)

    service.notify_recommendation(rec, _NOW)

    assert "配当比較(2025 → 2026): 減配(実績確定)" in client.sent[0]


def _evidence(rule_name: str, group: str, *, primary_source_confirmed: bool = True) -> dict:
    return {
        "rule_name": rule_name,
        "status": "TRIGGERED",
        "severity": "major",
        "evidence_group": group,
        "is_immediate_critical": False,
        "metric_name": None,
        "current_value": None,
        "previous_value": None,
        "threshold": None,
        "comparison_period": None,
        "primary_source_confirmed": primary_source_confirmed,
        "source": "EDINET/TDnet",
        "explanation": f"{rule_name}が検出された",
    }


def _make_sell_recommendation(
    *, recommendation_id: str, reasons: list[str], evidence_details: list[dict] | None = None
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="4631",
        stock_name="ＤＩＣ",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        sell_prices=SellPriceLevels(
            stop_review_price=PriceWithRationale(price=Decimal("4000"), rationale="x")
        ),
        price_at_recommendation=Decimal("4384"),
        average_purchase_price_at_recommendation=Decimal("3745"),
        shares_at_recommendation=100,
        reasons=reasons,
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        evidence_details=evidence_details or [],
        recommended_action_summary="複数の独立した根拠に基づき投資前提の悪化が疑われます。売却を検討してください。",
        holding_risks=["自己資本比率が閾値を下回っている"],
        independent_evidence_group_count=2,
    )


def test_sell_message_with_insufficient_evidence_routes_to_manual_review(
    service_and_repos,
) -> None:
    # 根拠が1件のみ(または未設定)のSELLは、自動確定させず手動確認へ回す(要求仕様§15・§16)。
    service, repo, client = service_and_repos
    rec = _make_sell_recommendation(recommendation_id="rec-1", reasons=["減配(major)"])
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    message = client.sent[0]
    assert "【要手動確認】4631 ＤＩＣ" in message
    assert "自動売却推奨: 停止" in message


def test_sell_message_with_sufficient_independent_evidence_sends_normally(
    service_and_repos,
) -> None:
    service, repo, client = service_and_repos
    rec = _make_sell_recommendation(
        recommendation_id="rec-1",
        reasons=["減配(major)", "営業利益の継続悪化(major)"],
        evidence_details=[
            _evidence("dividend_cut", "DIVIDEND"),
            _evidence("continuous_operating_income_decline", "EARNINGS"),
        ],
    )
    repo.save(rec)

    service.notify_recommendation(rec, _NOW)

    message = client.sent[0]
    assert "判定内容: 複数の独立した根拠に基づき投資前提の悪化が疑われます" in message
    assert "保有を継続する場合のリスク: 自己資本比率が閾値を下回っている" in message
    assert "直ちに売却としない理由" not in message


def test_data_error_notification_logs_stock_name_instead_of_sending(
    service_and_repos, caplog
) -> None:
    service, _repo, client = service_and_repos
    with caplog.at_level("WARNING"):
        service.notify_data_error(
            "9999", "株価データを取得できません", _NOW, stock_name="テスト銘柄"
        )

    assert client.sent == []
    assert "stock_code=9999 テスト銘柄" in caplog.text


def test_data_quality_alert_logs_stock_name_and_recommended_action_instead_of_sending(
    service_and_repos, caplog
) -> None:
    service, repo, client = service_and_repos
    rec = _make_full_profit_take_recommendation(recommendation_id="rec-1", full_take_price="9000")
    repo.save(rec)

    with caplog.at_level("WARNING"):
        service.notify_recommendation(rec, _NOW)

    assert client.sent == []
    assert f"stock_code={rec.stock_code} {rec.stock_name}" in caplog.text
    assert "適正価格算出の入力データ" in caplog.text


def test_notify_batch_summary_sends_counts(service_and_repos) -> None:
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析", total=27, succeeded=24, failed=3, now=_NOW
    )

    assert sent is True
    assert len(client.sent) == 1
    message = client.sent[0]
    assert "全体処理件数: 27" in message
    assert "正常件数: 24" in message
    assert "異常件数: 3" in message
