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
    BuyAction,
    ConfidenceLevel,
    DividendComparisonOutcome,
    EarningsDateStatus,
    EarningsReleaseConfirmationState,
    ExecutionMode,
    NotificationStatus,
    RecommendationType,
    RecordDateUnknownReason,
    SourceType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.daily_notification_priority_repository import (
    DailyNotificationPriorityRepository,
)
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services import line_notification_service as line_notification_service_module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.line_notification_service import (
    LineNotificationService,
    compute_watchlist_addition_content_hash,
    render_notification_preview,
    render_watchlist_addition_message,
)
from jstock_advisor.services.watchlist_addition_summary_builder import (
    EvaluationHighlight,
    WatchlistAdditionItemView,
    WatchlistAdditionSummary,
)

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
            tentative=PriceWithRationale(price=Decimal("3600"), rationale="x"),
            standard=PriceWithRationale(price=Decimal(standard_price), rationale="x"),
            aggressive=PriceWithRationale(price=Decimal("2900"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        dividend_yield_pct_at_recommendation=4.5,
        total_yield_pct_at_recommendation=4.5,
        total_score=60.0,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _make_earnings_review_recommendation(
    *,
    recommendation_id: str,
    next_review_conditions: list[str],
    earnings_release_confirmation_state: EarningsReleaseConfirmationState = (
        EarningsReleaseConfirmationState.AWAITING_CONFIRMATION
    ),
    earnings_date_raw: dt.date = dt.date(2026, 8, 5),
) -> Recommendation:
    """決算発表確認待ち通知(REVIEW_AFTER_EARNINGS)用のRecommendation(コードレビュー
    対応: 明治HD事例・デプロイ前対応)。sell_pricesが空のため価格比較による再送判定
    ができず、構造化フィールド(earnings_release_confirmation_state等)の変化のみが
    再送のシグナルになる(next_review_conditionsは表示文言のみで、dedup判定には
    使われない)。
    """
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.REVIEW_AFTER_EARNINGS,
        sell_prices=SellPriceLevels(),
        price_at_recommendation=Decimal("4200"),
        average_purchase_price_at_recommendation=Decimal("4000"),
        shares_at_recommendation=100,
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        next_review_conditions=next_review_conditions,
        earnings_date_status=EarningsDateStatus.STALE_PAST_DATE,
        earnings_date_raw=earnings_date_raw,
        earnings_release_confirmation_state=earnings_release_confirmation_state,
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
        # BUY候補裾野拡大機能(2026-08): クールダウン判定用リポジトリも
        # 他のリポジトリと同じtmp_pathで分離し、実データへ影響しないようにする。
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        # cross-pipeline重複抑止(コードレビュー対応2026-08、指摘5)用リポジトリも
        # 同様にtmp_pathで分離する(未分離のまま実データを汚染した不備の再発防止)。
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )
    return service, recommendation_repo, client


@pytest.fixture
def validation_service_and_repos(
    tmp_path: Path,
) -> tuple[LineNotificationService, RecommendationRepository, _FakeLineClient]:
    """通知検証モード機能(2026-08追加)。execution_context=VALIDATIONで構築した
    サービス版(service_and_reposと同じ流儀)。"""
    store_dir = tmp_path / "local_store"
    recommendation_repo = RecommendationRepository(store_dir=store_dir)
    notification_log_repo = NotificationLogRepository(store_dir=store_dir)
    client = _FakeLineClient()
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=notification_log_repo,
        recommendation_repository=recommendation_repo,
        config=_CONFIG,
        execution_context=ExecutionContext(mode=ExecutionMode.VALIDATION),
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
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


def test_evaluate_notification_status_does_not_send(service_and_repos) -> None:
    """買い候補の優先度付け通知(2026-07仕様追加): evaluate_notification_statusは
    判定のみ行い、実際の送信(push_message)は一切行わないことを確認する。
    """
    service, repo, client = service_and_repos
    rec = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec)

    from jstock_advisor.domain.entities.enums import NotificationStatus

    outcome = service.evaluate_notification_status(rec, _NOW)
    assert outcome.status == NotificationStatus.SENT
    assert outcome.sent is False
    assert client.sent == []


def test_send_recommendation_notification_sends_unconditionally(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec)

    service.send_recommendation_notification(rec, _NOW)
    assert len(client.sent) == 1
    assert "2914" in client.sent[0]


def test_evaluate_then_send_matches_notify_recommendation_with_status(service_and_repos) -> None:
    """evaluate_notification_status→send_recommendation_notificationの2段階呼び出しが、
    従来のnotify_recommendation_with_status一括呼び出しと同じ結果(送信内容・
    通知ログ記録)になることを確認する回帰テスト。
    """
    service, repo, client = service_and_repos
    rec_a = _make_recommendation(
        recommendation_id="rec-a", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    rec_a = rec_a.model_copy(update={"stock_code": "1111"})
    repo.save(rec_a)
    rec_b = _make_recommendation(
        recommendation_id="rec-b", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    rec_b = rec_b.model_copy(update={"stock_code": "2222"})
    repo.save(rec_b)

    # rec_a: 一括呼び出し
    combined_outcome = service.notify_recommendation_with_status(rec_a, _NOW)
    # rec_b: 2段階呼び出し
    outcome = service.evaluate_notification_status(rec_b, _NOW)
    assert outcome.status == combined_outcome.status
    service.send_recommendation_notification(rec_b, _NOW)

    assert len(client.sent) == 2
    assert "1111" in client.sent[0]
    assert "2222" in client.sent[1]


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


def test_earnings_review_pending_notification_is_sent_with_expected_content(
    service_and_repos,
) -> None:
    """REVIEW_AFTER_EARNINGSの初回通知が正しくフォーマットされ送信されることの確認
    (コードレビュー対応: 明治HD事例)。"決算未発表"/"決算発表済み"と断定しない。
    """
    service, repo, client = service_and_repos
    rec = _make_earnings_review_recommendation(
        recommendation_id="rec-1",
        next_review_conditions=["決算発表予定日を経過していますが、無償データから..."],
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    assert len(client.sent) == 1
    assert "決算発表状況確認待ち" in client.sent[0]
    assert "決算未発表" not in client.sent[0]
    assert "決算発表済み" not in client.sent[0]


def test_earnings_review_pending_notification_not_resent_for_same_state(
    service_and_repos,
) -> None:
    """同一のnext_review_conditions(=同一の確認待ち状態)が続く間は再送しない。"""
    service, repo, client = service_and_repos
    conditions = ["決算発表予定日を経過していますが、無償データから実際の発表状況を確認できて"]
    rec1 = _make_earnings_review_recommendation(
        recommendation_id="rec-1", next_review_conditions=conditions
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    rec2 = _make_earnings_review_recommendation(
        recommendation_id="rec-2", next_review_conditions=conditions
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(hours=1))

    assert sent is False
    assert len(client.sent) == 1


def test_earnings_review_pending_notification_resent_when_state_transitions_to_delayed(
    service_and_repos,
) -> None:
    """AWAITING_CONFIRMATION→DELAYEDのような状態変化は、構造化フィールド
    (earnings_release_confirmation_state)の変化として検知され、価格情報が
    無くても再送資格ありとみなす(デプロイ前対応: 自由文比較から構造化キー
    比較へ変更)。
    """
    service, repo, client = service_and_repos
    rec1 = _make_earnings_review_recommendation(
        recommendation_id="rec-1",
        next_review_conditions=["決算発表予定日を経過していますが、無償データから..."],
        earnings_release_confirmation_state=EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    rec2 = _make_earnings_review_recommendation(
        recommendation_id="rec-2",
        next_review_conditions=["決算発表予定日を経過し、最新財務データの反映確認が長引いています。"],
        earnings_release_confirmation_state=EarningsReleaseConfirmationState.DELAYED,
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(hours=1))

    assert sent is True
    assert len(client.sent) == 2


def test_earnings_review_pending_notification_not_resent_for_same_delayed_state(
    service_and_repos,
) -> None:
    """DELAYED→DELAYEDのように状態が変わらない場合は、最小再通知時間
    (resend_after_days)を経過するまで再送しない。"""
    service, repo, client = service_and_repos
    rec1 = _make_earnings_review_recommendation(
        recommendation_id="rec-1",
        next_review_conditions=["決算発表予定日を経過し、最新財務データの反映確認が長引いています。"],
        earnings_release_confirmation_state=EarningsReleaseConfirmationState.DELAYED,
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    rec2 = _make_earnings_review_recommendation(
        recommendation_id="rec-2",
        next_review_conditions=["決算発表予定日を経過し、最新財務データの反映確認が長引いています。"],
        earnings_release_confirmation_state=EarningsReleaseConfirmationState.DELAYED,
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(hours=1))

    assert sent is False
    assert len(client.sent) == 1


def test_earnings_review_pending_notification_resent_when_earnings_date_changes(
    service_and_repos,
) -> None:
    """対象の決算予定日自体が変わった(=別の決算イベントに対する待機)場合は、
    状態ラベルが同じでも再送資格ありとみなす。"""
    service, repo, client = service_and_repos
    rec1 = _make_earnings_review_recommendation(
        recommendation_id="rec-1",
        next_review_conditions=["決算発表予定日を経過していますが、無償データから..."],
        earnings_date_raw=dt.date(2026, 8, 5),
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    rec2 = _make_earnings_review_recommendation(
        recommendation_id="rec-2",
        next_review_conditions=["決算発表予定日を経過していますが、無償データから..."],
        earnings_date_raw=dt.date(2026, 11, 5),
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(hours=1))

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
    assert "全株利確目標" in client.sent[0]
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
    *,
    recommendation_id: str,
    reasons: list[str],
    evidence_details: list[dict] | None = None,
    independent_evidence_group_count: int = 2,
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
        independent_evidence_group_count=independent_evidence_group_count,
    )


def test_sell_message_with_insufficient_evidence_routes_to_manual_review(
    service_and_repos,
) -> None:
    # 独立根拠グループが1件のみのSELLは、自動確定させず手動確認へ回す(要求仕様§15・§16)。
    service, repo, client = service_and_repos
    rec = _make_sell_recommendation(
        recommendation_id="rec-1", reasons=["減配(major)"], independent_evidence_group_count=1
    )
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

    # 通知簡潔化(コードレビュー対応2026-08)により実送信本文は50/70文字ルールへ
    # 短縮される(判定内容・保有継続時リスク等の詳細はRecommendation/監査ログの
    # みに保持され、LINE本文には含まれなくなった)。ここでは「抑止されず実際に
    # 送信されたこと」と、短縮本文が銘柄・第一理由を含むことのみを検証する。
    message = client.sent[0]
    assert "売却検討" in message
    assert rec.stock_code in message
    assert "減配(major)" in message
    assert len(message) <= 70


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


def _counts(
    sent=0,
    hold=0,
    review=0,
    data_insufficient=0,
    suppressed=0,
    failed=0,
    candidate_not_ranked=0,
    watch_not_ranked=0,
) -> dict[str, int]:
    return {
        "sent": sent,
        "hold": hold,
        "review": review,
        "data_insufficient": data_insufficient,
        "suppressed": suppressed,
        "failed": failed,
        "candidate_not_ranked": candidate_not_ranked,
        "watch_not_ranked": watch_not_ranked,
    }


def test_notify_batch_summary_sends_counts(service_and_repos) -> None:
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts=_counts(sent=6, hold=18, data_insufficient=1, suppressed=2),
        now=_NOW,
    )

    assert sent is True
    assert len(client.sent) == 1
    message = client.sent[0]
    assert "処理結果:" in message
    assert "対象銘柄：27件" in message
    assert "・個別通知送信：6件" in message
    assert "・通知不要（保有継続）：18件" in message
    assert "・要確認：0件" in message
    assert "・データ不足：1件" in message
    assert "・再通知抑止：2件" in message
    assert "・処理失敗：0件" in message
    assert "内訳合計" not in message  # 6+18+0+1+2+0=27で一致するため不整合の注記は出ない


def test_notify_batch_summary_flags_inconsistent_counts(service_and_repos) -> None:
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts=_counts(sent=6, hold=18),  # 合計24 != 27
        now=_NOW,
    )

    message = client.sent[0]
    assert "内訳合計(24件)が対象銘柄数と一致していません" in message


def test_notify_batch_summary_lists_data_insufficient_and_failed_stock_codes(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=2,
        category_counts=_counts(data_insufficient=1, failed=1),
        now=_NOW,
        data_insufficient_stock_codes=["7042"],
        failed_stock_codes=["1234"],
    )

    message = client.sent[0]
    assert "データ不足：\n・7042" in message
    assert "処理失敗：\n・1234" in message


def test_notify_batch_summary_suppresses_duplicate_same_day_same_content(
    service_and_repos,
) -> None:
    # ディスパッチが二重化され、2つの独立したbatch_idが同一内容で完了を検知した場合でも、
    # 同一日付・同一件数のサマリーは1通しか送らない。
    service, _repo, client = service_and_repos

    first = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts=_counts(sent=6, hold=18, data_insufficient=1, suppressed=2),
        now=_NOW,
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts=_counts(sent=6, hold=18, data_insufficient=1, suppressed=2),
        now=_NOW + dt.timedelta(seconds=15),
    )

    assert first is True
    assert second is False
    assert len(client.sent) == 1


def test_notify_batch_summary_sends_again_when_content_differs(service_and_repos) -> None:
    # 同日でも件数が異なる(=新しい情報がある)場合は改めて送信する。
    service, _repo, client = service_and_repos

    first = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts=_counts(sent=6, hold=18, data_insufficient=1, suppressed=2),
        now=_NOW,
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts=_counts(sent=9, hold=18),
        now=_NOW + dt.timedelta(hours=1),
    )

    assert first is True
    assert second is True
    assert len(client.sent) == 2


def test_prices_are_rounded_to_whole_yen_in_notification() -> None:
    # 要求仕様レビュー対応: 金額は小数点以下を表示せず、整数円(カンマ区切り)で表示する。
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600.5"
    ).model_copy(
        update={
            "fair_value_bear": Decimal("390.0262389877913247479315874"),
            "fair_value_neutral": Decimal("498"),
            "fair_value_bull": Decimal("657.3426438760979267386731305"),
        }
    )

    message = render_notification_preview(rec)

    assert "4,601円" in message
    assert "4600.5" not in message
    assert "390円" in message
    assert "390.0262389877913247479315874" not in message
    assert "657円" in message


def test_yen_amount_with_scientific_notation_decimal_is_not_shown_in_exponent_form() -> None:
    # Decimal('5.5E+2')のように指数を内部保持する値は、str()するとそのまま
    # "5.5E+2"と表示されてしまう(to_integral_value()だけでは解消しない)ため回帰確認する。
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(
        update={
            "fair_value_neutral": Decimal("550"),
            "fair_value_methods": [
                {"method": "target_yield", "fair_value": str(Decimal("5.5E+2"))},
            ],
        }
    )

    message = render_notification_preview(rec)

    assert "550円" in message
    assert "E+2" not in message


def test_recommendation_type_shown_as_japanese_label_not_raw_enum(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec = _make_full_profit_take_recommendation(recommendation_id="rec-1", full_take_price="4600")
    repo.save(rec)

    service.notify_recommendation(rec, _NOW)

    message = client.sent[0]
    assert "全部売却を検討" in message
    assert "PARTIAL_PROFIT_TAKE" not in message
    assert "FULL_PROFIT_TAKE" not in message


def test_watch_recommendation_type_shown_as_japanese_label() -> None:
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(update={"recommendation_type": RecommendationType.WATCH})

    message = render_notification_preview(rec)

    assert "保有継続(監視)" in message
    assert "WATCH" not in message


# --- 2026-07仕様レビュー対応: 基準日情報区分・信頼度分離・暫定判定・見出し出し分け ---


def test_watch_message_shows_five_distinct_record_date_categories() -> None:
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(
        update={
            "recommendation_type": RecommendationType.WATCH,
            "dividend_record_date": None,
            "dividend_record_date_recurring_label": "毎年3月末(登録済みの権利確定周期に基づく)",
            "dividend_record_date_source_type": SourceType.COMPANY_IR,
            "benefit_record_date": None,
            "benefit_record_date_recurring_label": "毎年3月末(登録済みの権利確定周期に基づく)",
            "benefit_record_date_source_type": SourceType.MANUAL_REGISTRY,
        }
    )

    message = render_notification_preview(rec)

    assert "情報区分：会社公式情報" in message
    assert "情報区分：手動登録データ" in message


def test_watch_message_shows_data_provider_and_inferred_categories_separately() -> None:
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(
        update={
            "recommendation_type": RecommendationType.WATCH,
            "dividend_record_date": None,
            "dividend_record_date_recurring_label": (
                "毎年3月末(決算期末を基準とした一般的な慣行からの推定、確定情報ではない)"
            ),
            "dividend_record_date_source_type": None,
            "benefit_record_date": None,
            "benefit_record_date_recurring_label": "毎年3月末(登録済みの権利確定周期に基づく)",
            "benefit_record_date_source_type": SourceType.CONTRACTED_PROVIDER,
        }
    )

    message = render_notification_preview(rec)

    assert "情報区分：決算期末等からの推定" in message
    assert "情報区分：データ提供元" in message
    # 統合ラベルにはならず、5区分が別々に扱われていることの確認
    assert "情報区分：会社公式情報" not in message


def test_watch_before_earnings_shows_provisional_judgment_and_reason() -> None:
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(
        update={
            "recommendation_type": RecommendationType.WATCH_BEFORE_EARNINGS,
            "business_days_to_earnings": 6,
        }
    )

    message = render_notification_preview(rec)

    assert "暫定判定:" in message
    assert "判断保留理由: 次回決算まで6営業日のため、決算内容確認後に再評価" in message
    assert "【適正価格超過・決算後に再評価】" in message


def test_watch_normal_does_not_show_provisional_judgment() -> None:
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(update={"recommendation_type": RecommendationType.WATCH})

    message = render_notification_preview(rec)

    assert "暫定判定:" not in message
    assert "判断保留理由" not in message
    assert "判定:" in message


def test_watch_message_separates_fair_value_and_holding_confidence_labels() -> None:
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(
        update={
            "recommendation_type": RecommendationType.WATCH,
            "fair_value_bear": Decimal("3000"),
            "fair_value_neutral": Decimal("3300"),
            "fair_value_bull": Decimal("3600"),
            "fair_value_overall_confidence": ConfidenceLevel.HIGH,
            "confidence": ConfidenceLevel.MEDIUM,
        }
    )

    message = render_notification_preview(rec)

    assert "適正価格算出の信頼度: HIGH" in message
    assert "保有継続判定の信頼度: MEDIUM" in message
    # ラベル無しの単独「信頼度:」行が残っていないことを確認
    assert "\n信頼度:\n" not in message


def test_watch_message_suppresses_bullish_scenario_note_when_dispersion_large() -> None:
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    ).model_copy(
        update={
            "recommendation_type": RecommendationType.WATCH,
            "price_at_recommendation": Decimal("3200"),
            "fair_value_bear": Decimal("3000"),
            "fair_value_neutral": Decimal("3300"),
            "fair_value_bull": Decimal("4500"),
            "fair_value_overall_confidence": ConfidenceLevel.LOW,
            "fair_value_methods": [
                {"method": "PER", "fair_value": Decimal("3100")},
                {"method": "PBR", "fair_value": Decimal("3200")},
                {"method": "DCF", "fair_value": Decimal("4500")},
            ],
        }
    )

    message = render_notification_preview(rec)

    assert "強気シナリオの想定範囲内" not in message
    assert "適正価格に関する注意:" in message
    assert "DCFを除く適正価格は" in message
    assert "【適正価格のばらつき大・継続監視】" in message


# --- BUYパイプライン再設計(2026-07)の通知フォーマット ---------------------------


def _make_buy_pipeline_recommendation(
    *, buy_action: BuyAction, recommendation_type: RecommendationType = RecommendationType.BUY
) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-buy-1",
        stock_code="4516",
        stock_name="日本新薬",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("3440"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3225"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("3010"), rationale="x"),
        ),
        price_at_recommendation=Decimal("3495"),
        total_yield_pct_at_recommendation=3.55,
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=buy_action,
        company_quality_score=48.67,
        purchase_attractiveness_score=30.0,
        valuation_anchor=Decimal("3620"),
        valuation_min=Decimal("3400"),
        valuation_max=Decimal("3900"),
        decision_valuation_min=Decimal("3400"),
        decision_valuation_max=Decimal("3900"),
        valuation_dispersion_ratio=Decimal("1.15"),
        current_vs_entry_price_pct=Decimal("1.6"),
        required_decline_to_entry_pct=Decimal("1.6"),
        reasons=["財務健全性が高評価"],
    )


def test_buy_candidate_message_includes_valuation_anchor_and_price_levels() -> None:
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.SMALL_ENTRY)
    message = render_notification_preview(rec)

    assert "打診購入候補" in message
    assert "適正価格レンジ: 3,400円〜3,900円" in message
    assert "購入判断基準価格: 3,620円" in message
    assert "打診買い:3,440円 標準買い:3,225円 積極買い:3,010円" in message
    assert "企業魅力度: 48.7点" in message
    assert "購入魅力度: 30.0点" in message


def test_watch_for_price_message_distinct_from_buy_message() -> None:
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.WATCH_FOR_PRICE)
    message = render_notification_preview(rec)

    assert "監視継続(価格待ち)" in message
    assert "打診買い価格まで" in message
    assert "今回の購入候補には含めません" in message
    assert "購入魅力度" not in message  # 価格待ち通知には購入魅力度は表示しない


def test_watch_before_earnings_buy_action_does_not_collide_with_profit_taking_type() -> None:
    """RecommendationType.WATCH_BEFORE_EARNINGSは利確判定エンジン専用のため、
    BUYパイプラインはrecommendation_typeを書き換えず、buy_actionのみで
    「監視継続(決算待ち)」表示に分岐する(通知フォーマットの衝突回帰テスト)。
    """
    rec = _make_buy_pipeline_recommendation(
        buy_action=BuyAction.WATCH_BEFORE_EARNINGS,
        recommendation_type=RecommendationType.BUY,
    )
    message = render_notification_preview(rec)

    assert "監視継続(決算待ち)" in message
    assert "決算内容を確認してから再評価" in message


def test_buy_action_label_used_instead_of_recommendation_type_label() -> None:
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)
    message = render_notification_preview(rec)
    assert "積極購入候補" in message


def test_batch_summary_no_buy_candidates_renders_none_found(service_and_repos) -> None:
    # 通知本文に「該当なし」が表示されることを確認する。
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=10,
        category_counts=_counts(hold=5, watch_not_ranked=5),
        now=_NOW,
        buy_candidates_sent_count=0,
    )

    message = client.sent[0]
    assert "該当なし" in message
    assert "現在価格で安全余裕を満たす銘柄はありませんでした" in message


def test_batch_summary_shows_buy_and_watch_breakdown_separately(service_and_repos) -> None:
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=68,
        category_counts=_counts(
            sent=6, hold=40, data_insufficient=5, candidate_not_ranked=17, watch_not_ranked=0
        ),
        now=_NOW,
        buy_candidates_sent_count=1,
    )

    message = client.sent[0]
    assert "買い候補(通知上限により見送り)：17件" in message
    assert "価格待ち(通知上限により見送り)：" not in message  # 0件なので非表示


def test_batch_summary_never_says_top_priority_n_stocks(service_and_repos) -> None:
    # 「優先順位の高い5件」という表現が通知本文に含まれないことを確認する。
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=68,
        category_counts=_counts(sent=5, candidate_not_ranked=22, watch_not_ranked=3),
        now=_NOW,
        buy_candidates_sent_count=5,
    )

    message = client.sent[0]
    assert "優先順位の高い" not in message


# --- BUYパイプライン第2次修正(2026-07)で追加 ---


@pytest.mark.parametrize(
    "buy_action",
    [
        # BUY候補裾野拡大機能(2026-08): WATCH_BEFORE_EARNINGSはNOT_NOTIFIABLEでは
        # なくなったため、このリストから除外した(下のtest_watch_before_earnings_*
        # で個別に検証する)。通常のWATCH_FOR_PRICE(watch_type未設定=NEAR_BUY非該当)
        # は引き続き通知対象外のまま。
        BuyAction.WATCH_FOR_PRICE,
        BuyAction.MANUAL_REVIEW,
        BuyAction.NOT_ATTRACTIVE,
        BuyAction.EXCLUDED,
        BuyAction.DATA_INSUFFICIENT,
    ],
)
def test_evaluate_notification_status_never_sends_non_buy_family_actions(
    service_and_repos, buy_action: BuyAction
) -> None:
    service, _repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=buy_action)

    outcome = service.evaluate_notification_status(rec, _NOW)

    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.sent is False
    assert client.sent == []


def test_watch_before_earnings_reaches_evaluate_notification_status(service_and_repos) -> None:
    """BUY候補裾野拡大機能(2026-08、指摘1): 旧ゲート(buy_action not in
    BUY_FAMILY_ACTIONS)ではWATCH_BEFORE_EARNINGSが誤って抑止されていた。
    resolve_notification_category()経由の新ゲートでは通知評価まで到達し、
    notification_policy.watch_before_earnings.notify_every_business_day=trueの
    ため毎営業日SENTになる。"""
    service, _repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.WATCH_BEFORE_EARNINGS)

    outcome = service.evaluate_notification_status(rec, _NOW)

    assert outcome.status == NotificationStatus.SENT
    assert outcome.sent is False  # evaluate_notification_status自体は送信しない(呼び出し側の責務)


@pytest.mark.parametrize(
    "buy_action", [BuyAction.STRONG_BUY, BuyAction.BUY, BuyAction.SMALL_ENTRY]
)
def test_evaluate_notification_status_allows_buy_family_actions(
    service_and_repos, buy_action: BuyAction
) -> None:
    service, _repo, _client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=buy_action)

    outcome = service.evaluate_notification_status(rec, _NOW)

    assert outcome.status == NotificationStatus.SENT


def _alert_stub(rec: Recommendation):
    from jstock_advisor.domain.entities.data_quality_alert import DataQualityAlert

    return DataQualityAlert(
        stock_code=rec.stock_code,
        stock_name=rec.stock_name,
        detected_at=_NOW,
        process="notify_recommendation",
        contradictions=["[test] テスト用の矛盾"],
        suppressed_values={},
        recalculation_result=None,
        action_required=True,
        recommended_action="要確認",
    )


def test_evaluate_notification_status_buy_candidate_batch_suppresses_manual_review_line(
    service_and_repos, monkeypatch
) -> None:
    """BUY_CANDIDATE_BATCHコンテキストでは、データ品質アラートがrequires_manual_review
    相当であっても、notify_manual_review_requiredによるLINE送信は行わない
    (BUYパイプライン第3次修正2026-07)。data_quality_blocked=Trueは維持する
    (異常自体は_check_data_quality内で監査ログへ記録済み)。
    """
    from jstock_advisor.domain.entities.enums import NotificationContext

    service, repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.BUY)
    repo.save(rec)
    monkeypatch.setattr(
        service, "_check_data_quality", lambda *a, **kw: (_alert_stub(rec), True)
    )

    outcome = service.evaluate_notification_status(
        rec, _NOW, context=NotificationContext.BUY_CANDIDATE_BATCH
    )

    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.sent is False
    assert outcome.data_quality_blocked is True
    assert client.sent == []


def test_evaluate_notification_status_default_context_still_sends_manual_review_line(
    service_and_repos, monkeypatch
) -> None:
    """DEFAULT/HOLDING_REVIEWコンテキスト(SELL・保有銘柄レビュー系)では、
    要手動確認LINEの安全弁は従来通り動作する(抑止対象はBUY_CANDIDATE_BATCHのみ)。
    """
    service, repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.BUY)
    repo.save(rec)
    monkeypatch.setattr(
        service, "_check_data_quality", lambda *a, **kw: (_alert_stub(rec), True)
    )

    outcome = service.evaluate_notification_status(rec, _NOW)

    assert outcome.data_quality_blocked is True
    assert outcome.sent is True
    assert len(client.sent) == 1
    assert "【要手動確認】" in client.sent[0]


def test_notify_buy_candidates_digest_sends_one_message_for_multiple_winners(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos
    winners = [
        _make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY),
        _make_buy_pipeline_recommendation(buy_action=BuyAction.SMALL_ENTRY).model_copy(
            update={"stock_code": "1111", "recommendation_id": "rec-buy-2"}
        ),
    ]

    results = service.notify_buy_candidates_digest(winners, _NOW)

    assert results == {"4516": "SENT_AND_RECORDED", "1111": "SENT_AND_RECORDED"}
    assert len(client.sent) == 1
    message = client.sent[0]
    assert "【本日の購入候補】" in message
    # 通知簡潔化(コードレビュー対応2026-08)により、1銘柄分のブロックは
    # 「順位」ではなくformat_notification_text()の短縮形式(判定 銘柄コード
    # 銘柄名)になった(送信順序自体が優先度順を表す)。
    assert "買い 4516 日本新薬" in message
    assert "買い 1111 日本新薬" in message
    assert "対象: 最大2銘柄" in message
    blocks = [line for line in message.split("\n\n") if line.startswith("買い ")]
    assert all(len(block) <= 70 for block in blocks)


def test_notify_buy_candidates_digest_block_omits_long_form_detail(
    service_and_repos,
) -> None:
    """通知簡潔化(コードレビュー対応2026-08、指摘1)。ダイジェストの1銘柄
    ブロック(notify_buy_candidates_digest → _buy_candidate_digest_block)は
    保有種別・保有株数・平均取得単価・評価損益等の長文詳細をLINE本文には
    含めない(詳細情報はRecommendation本体・監査ログに保持され続ける。
    旧来の項目網羅型ブロックの再発防止)。保有銘柄由来・気になる銘柄由来の
    いずれでも短縮形式であることを確認する。
    """
    from decimal import Decimal

    from jstock_advisor.domain.entities.enums import CandidateSource

    service, _repo, client = service_and_repos
    winner = _make_buy_pipeline_recommendation(buy_action=BuyAction.BUY).model_copy(
        update={
            "candidate_source": CandidateSource.HOLDING,
            "holding_quantity": 300,
            "average_acquisition_price": Decimal("2100"),
            "unrealized_profit_loss": Decimal("75000"),
            "unrealized_profit_loss_pct": Decimal("11.9"),
        }
    )

    service.notify_buy_candidates_digest([winner], _NOW)

    message = client.sent[0]
    assert "種別" not in message
    assert "現在の保有" not in message
    assert "平均取得単価" not in message
    assert "評価損益" not in message


def test_notify_buy_candidates_digest_records_notification_log_per_stock(
    tmp_path: Path,
) -> None:
    store_dir = tmp_path / "local_store"
    recommendation_repo = RecommendationRepository(store_dir=store_dir)
    notification_log_repo = NotificationLogRepository(store_dir=store_dir)
    client = _FakeLineClient()
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=notification_log_repo,
        recommendation_repository=recommendation_repo,
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )
    winners = [_make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)]

    service.notify_buy_candidates_digest(winners, _NOW)

    from jstock_advisor.domain.entities.enums import NotificationType

    latest = notification_log_repo.latest_by_stock_and_type(
        "4516", NotificationType.DAILY_BUY_CANDIDATES
    )
    assert latest is not None
    assert latest.related_recommendation_id == "rec-buy-1"


def test_notify_buy_candidates_digest_no_winners_sends_nothing(service_and_repos) -> None:
    service, _repo, client = service_and_repos

    results = service.notify_buy_candidates_digest([], _NOW)

    assert results == {}
    assert client.sent == []


def test_notify_buy_candidates_digest_line_send_failure_marks_send_failed(
    service_and_repos, monkeypatch
) -> None:
    """LINE送信(push_message)自体が失敗した場合、SENT_AND_RECORDEDにはならず
    SEND_FAILEDとして扱われ、NotificationLogは保存されない
    (統合BUY候補パイプライン2026-07)。"""
    service, _repo, client = service_and_repos
    winners = [_make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)]

    def _raise(_message: str) -> None:
        raise RuntimeError("LINE API down")

    monkeypatch.setattr(client, "push_message", _raise)

    results = service.notify_buy_candidates_digest(winners, _NOW)

    assert results == {"4516": "SEND_FAILED"}


def test_notify_buy_candidates_digest_log_save_failure_marks_sent_log_failed(
    service_and_repos, monkeypatch
) -> None:
    """push_messageは成功したがNotificationLog保存が失敗した場合、二重送信を
    避けるため未送信扱いにせず、SENT_LOG_FAILEDとして区別する
    (統合BUY候補パイプライン2026-07)。"""
    service, _repo, client = service_and_repos
    winners = [_make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)]

    def _raise(_entry: object) -> None:
        raise RuntimeError("DynamoDB write failed")

    monkeypatch.setattr(service._log_repo, "save", _raise)

    results = service.notify_buy_candidates_digest(winners, _NOW)

    assert results == {"4516": "SENT_LOG_FAILED"}
    assert len(client.sent) == 1  # LINEへは実際に送信されている


def test_notify_batch_summary_suppressed_when_empty_and_send_empty_summary_false(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "買い候補分析",
        total=68,
        category_counts=_counts(hold=68),
        now=_NOW,
        buy_candidates_sent_count=0,
        send_empty_summary=False,
    )

    assert sent is False
    assert client.sent == []


def test_notify_batch_summary_sent_when_empty_and_send_empty_summary_true(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "買い候補分析",
        total=68,
        category_counts=_counts(hold=68),
        now=_NOW,
        buy_candidates_sent_count=0,
        send_empty_summary=True,
    )

    assert sent is True
    assert len(client.sent) == 1
    assert "該当なし" in client.sent[0]


def test_notify_batch_summary_default_still_sends_for_non_buy_callers(
    service_and_repos,
) -> None:
    # buy_candidates_sent_count=None(保有銘柄バッチ等)の場合、send_empty_summaryの
    # 既定値(True)は既存呼び出し元の挙動に一切影響しない。
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "保有銘柄評価",
        total=10,
        category_counts=_counts(hold=10),
        now=_NOW,
    )

    assert sent is True
    assert len(client.sent) == 1


# --- BUYパイプライン第3次修正(2026-07): 採用済み安全余裕理由のみ表示 ------------


def test_buy_candidate_message_shows_only_adopted_margin_adjustments() -> None:
    """カテゴリ内で不採用(superseded_by設定あり)になった安全余裕調整は、LINE本文の
    「必要安全余裕を拡大した理由」には表示しない(BUYパイプライン第3次修正2026-07)。
    採用済み(superseded_by=None)のもののみ表示する。
    """
    from decimal import Decimal as _Decimal

    from jstock_advisor.domain.entities.common import MarginAdjustment
    from jstock_advisor.domain.entities.enums import MarginRiskCategory

    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.BUY).model_copy(
        update={
            "margin_adjustments": [
                MarginAdjustment(
                    code="cyclical_industry",
                    adjustment=_Decimal("0.05"),
                    reason="景気循環業種のため",
                    category=MarginRiskCategory.INDUSTRY_AND_BUSINESS,
                    superseded_by=None,
                ),
                MarginAdjustment(
                    code="major_customer_dependency",
                    adjustment=_Decimal("0.03"),
                    reason="主要顧客への依存度が高いため",
                    category=MarginRiskCategory.INDUSTRY_AND_BUSINESS,
                    superseded_by="cyclical_industry",
                ),
                MarginAdjustment(
                    code="data_quality_warning",
                    adjustment=_Decimal("0.05"),
                    reason="データ品質に懸念があるため",
                    category=MarginRiskCategory.DATA_QUALITY,
                    superseded_by=None,
                ),
            ]
        }
    )

    message = render_notification_preview(rec)

    assert "必要安全余裕を拡大した理由" in message
    assert "・景気循環業種のため" in message
    assert "・データ品質に懸念があるため" in message
    assert "・主要顧客への依存度が高いため" not in message


def test_buy_candidate_message_omits_margin_line_when_all_adjustments_superseded() -> None:
    """全ての調整が不採用(superseded_by設定あり)の場合、「必要安全余裕を拡大した
    理由」の行自体を表示しない(空の見出しだけ出すことを避ける)。
    """
    from decimal import Decimal as _Decimal

    from jstock_advisor.domain.entities.common import MarginAdjustment
    from jstock_advisor.domain.entities.enums import MarginRiskCategory

    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.BUY).model_copy(
        update={
            "margin_adjustments": [
                MarginAdjustment(
                    code="major_customer_dependency",
                    adjustment=_Decimal("0.03"),
                    reason="主要顧客への依存度が高いため",
                    category=MarginRiskCategory.INDUSTRY_AND_BUSINESS,
                    superseded_by="cyclical_industry",
                ),
            ]
        }
    )

    message = render_notification_preview(rec)

    assert "必要安全余裕を拡大した理由" not in message


# --- 統合BUY候補パイプライン(2026-07): check_data_quality_eligibility /
# check_resend_eligibilityの読み取り専用性・判定内容のテスト ---


def test_check_data_quality_eligibility_is_eligible_for_clean_data(service_and_repos) -> None:
    # _make_buy_pipeline_recommendationは価格帯が意図的に不整合(現在値が全買付
    # 価格帯を上回る)なテストフィクスチャのため、ここでは整合性検証を通過する
    # _make_recommendation(buy_action未設定)を使う。
    service, repo, client = service_and_repos
    rec = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec)

    eligibility = service.check_data_quality_eligibility(rec, _NOW)

    assert eligibility.eligible is True
    assert eligibility.block_category is None


def test_check_data_quality_eligibility_blocks_with_data_quality_category(
    service_and_repos, monkeypatch
) -> None:
    from jstock_advisor.domain.entities.enums import EligibilityBlockCategory

    service, repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)
    repo.save(rec)
    monkeypatch.setattr(service, "_check_data_quality", lambda *a, **kw: (_alert_stub(rec), False))

    eligibility = service.check_data_quality_eligibility(rec, _NOW)

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.DATA_QUALITY


def test_check_data_quality_eligibility_does_not_write_notification_log(
    service_and_repos, monkeypatch
) -> None:
    """データ品質チェックはNotificationLogへ一切書き込まない(読み取り専用)。
    ブロックされるケース・されないケースの両方で、呼び出し前後で件数が変化しない
    ことを確認する。
    """
    service, repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)
    repo.save(rec)
    monkeypatch.setattr(service, "_check_data_quality", lambda *a, **kw: (_alert_stub(rec), False))

    before = len(service._log_repo.list_all())
    service.check_data_quality_eligibility(rec, _NOW)
    after = len(service._log_repo.list_all())

    assert before == after == 0
    assert client.sent == []  # notify_data_quality_alertはログのみ、LINE送信もしない


def test_check_data_quality_eligibility_buy_candidate_batch_suppresses_manual_review_line(
    service_and_repos, monkeypatch
) -> None:
    """BUY_CANDIDATE_BATCHコンテキストでは要手動確認LINEを送らない
    (evaluate_notification_statusと同じ規約)。"""
    from jstock_advisor.domain.entities.enums import NotificationContext

    service, repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)
    repo.save(rec)
    monkeypatch.setattr(service, "_check_data_quality", lambda *a, **kw: (_alert_stub(rec), True))

    eligibility = service.check_data_quality_eligibility(
        rec, _NOW, context=NotificationContext.BUY_CANDIDATE_BATCH
    )

    assert eligibility.eligible is False
    assert client.sent == []


def test_check_resend_eligibility_is_eligible_for_first_notification(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)
    repo.save(rec)

    eligibility = service.check_resend_eligibility(rec, _NOW)

    assert eligibility.eligible is True


def test_check_resend_eligibility_blocks_recently_notified_with_correct_category(
    service_and_repos,
) -> None:
    from jstock_advisor.domain.entities.enums import EligibilityBlockCategory, NotificationType

    service, repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)
    repo.save(rec)
    # 直近に同一銘柄・同一種別の通知を送信済みという状態を作る
    service._log_repo.save(
        NotificationLog(
            notification_id="log-1",
            notification_type=NotificationType.DAILY_BUY_CANDIDATES,
            stock_code=rec.stock_code,
            content_hash="dummy",
            sent_at=_NOW,
            related_recommendation_id=rec.recommendation_id,
        )
    )

    eligibility = service.check_resend_eligibility(rec, _NOW)

    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.RECENTLY_NOTIFIED


def test_check_resend_eligibility_does_not_write_notification_log(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)
    repo.save(rec)

    before = len(service._log_repo.list_all())
    service.check_resend_eligibility(rec, _NOW)
    after = len(service._log_repo.list_all())

    assert before == after == 0


# --- notify_watchlist_additions(ウォッチリスト自動追加機能) --------------------


def _hash_for(
    stock_codes: list[str],
    policy_name: str = "high_dividend_financial_health",
    evaluation_date: dt.date | None = None,
    batch_id: str = "test-batch",
) -> str:
    return compute_watchlist_addition_content_hash(
        batch_id, stock_codes, policy_name, evaluation_date or _NOW.date()
    )


def _summary_item(
    stock_code: str,
    display_name: str | None,
    rank: int,
    total_score: float,
    highlights: list[EvaluationHighlight] | None = None,
) -> WatchlistAdditionItemView:
    return WatchlistAdditionItemView(
        stock_code=stock_code,
        display_name=display_name or stock_code,
        rank=rank,
        total_score=total_score,
        highlights=(
            highlights
            if highlights is not None
            else [
                EvaluationHighlight(label="配当利回り", detail="4.2%", score=total_score),
                EvaluationHighlight(label="自己資本比率", detail="55.0%", score=total_score),
            ]
        ),
    )


def _summary(
    items: list[WatchlistAdditionItemView],
    *,
    policy_name: str = "high_dividend_financial_health",
    policy_label: str | None = None,
    policy_conditions: list[str] | None = None,
    total_target_count: int | None = None,
    ranked_count: int | None = None,
    data_unavailable_count: int = 0,
    evaluated_at: dt.datetime = _NOW,
) -> WatchlistAdditionSummary:
    added_count = len(items)
    target = total_target_count if total_target_count is not None else added_count
    return WatchlistAdditionSummary(
        policy_name=policy_name,
        policy_label=policy_label if policy_label is not None else policy_name,
        policy_conditions=(
            policy_conditions if policy_conditions is not None else ["配当利回り6.0%以上(満点)"]
        ),
        total_target_count=target,
        ranked_count=ranked_count if ranked_count is not None else added_count,
        data_unavailable_count=data_unavailable_count,
        added_count=added_count,
        addition_rate_pct=(added_count / target * 100) if target else 0.0,
        evaluated_at=evaluated_at,
        items=items,
    )


def test_notify_watchlist_additions_returns_false_and_sends_nothing_when_empty(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos

    sent = service.notify_watchlist_additions(_summary([]), _hash_for([]))

    assert sent is False
    assert client.sent == []


def test_notify_watchlist_additions_sends_and_shows_rank_score_and_highlights(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos
    summary = _summary(
        [_summary_item("1234", "テスト株式会社", rank=1, total_score=87.0)],
        policy_label="高配当・財務健全性",
        total_target_count=1,
        ranked_count=1,
    )

    sent = service.notify_watchlist_additions(summary, _hash_for(["1234"]))

    assert sent is True
    message = client.sent[0]
    assert "【ウォッチリスト追加】" in message
    assert "新たに1銘柄を追加しました。" in message
    assert "高配当・財務健全性" in message
    assert "対象：1銘柄" in message
    assert "評価可能：1銘柄" in message
    assert "追加率：100.0%" in message
    assert "1. テスト株式会社（1234）" in message
    assert "・総合スコア：87点" in message
    assert "・順位：評価可能1銘柄中1位" in message
    assert "・高評価項目：" in message
    assert "配当利回り 4.2%" in message
    assert "自己資本比率 55.0%" in message


def test_notify_watchlist_additions_only_shows_actually_added_items(
    service_and_repos,
) -> None:
    """summary.itemsに含まれる銘柄のみが表示されることを確認する
    (上限超過/既登録だった銘柄はbuild_watchlist_addition_summary側で
    既に除外されている前提)。"""
    service, _repo, client = service_and_repos
    summary = _summary([_summary_item("1234", None, rank=1, total_score=80.0)])

    service.notify_watchlist_additions(summary, _hash_for(["1234"]))

    message = client.sent[0]
    assert "1234" in message
    assert "新たに1銘柄を追加しました" in message


def test_notify_watchlist_additions_renders_items_in_summary_order(service_and_repos) -> None:
    """順位付け(スコア降順への並べ替え)はbuild_watchlist_addition_summary()側の
    責務であり、レンダリング側はsummary.itemsの並び順をそのまま表示する
    (通知チャネルとPresentation生成の責務分離)。"""
    service, _repo, client = service_and_repos
    high = _summary_item("2222", "ハイ", rank=1, total_score=90.0)
    low = _summary_item("1111", "ロー", rank=2, total_score=60.0)
    summary = _summary([high, low], total_target_count=2, ranked_count=2)

    service.notify_watchlist_additions(summary, _hash_for(["1111", "2222"]))

    message = client.sent[0]
    assert message.index("1. ハイ") < message.index("2. ロー")


def test_notify_watchlist_additions_shows_only_top_ten_with_remainder_summary(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos
    items = [
        _summary_item(f"{1000 + i}", None, rank=i + 1, total_score=100.0 - i) for i in range(13)
    ]
    summary = _summary(items, total_target_count=13, ranked_count=13)

    service.notify_watchlist_additions(
        summary, _hash_for([item.stock_code for item in items])
    )

    message = client.sent[0]
    assert "11." not in message
    assert "ほか3銘柄を追加しました。" in message


def test_notify_watchlist_additions_suppresses_duplicate_same_day_same_content(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos
    summary = _summary([_summary_item("1234", None, rank=1, total_score=80.0)])

    content_hash = _hash_for(["1234"])
    first = service.notify_watchlist_additions(summary, content_hash)
    second = service.notify_watchlist_additions(
        _summary(
            [_summary_item("1234", None, rank=1, total_score=80.0)],
            evaluated_at=_NOW + dt.timedelta(minutes=5),
        ),
        content_hash,
    )

    assert first is True
    assert second is False
    assert len(client.sent) == 1


def test_notify_watchlist_additions_unknown_policy_name_falls_back_to_raw_value(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos
    summary = _summary(
        [_summary_item("1234", None, rank=1, total_score=80.0)],
        policy_name="some_future_policy",
        policy_label="some_future_policy",
    )

    service.notify_watchlist_additions(
        summary, _hash_for(["1234"], policy_name="some_future_policy")
    )

    assert "some_future_policy" in client.sent[0]


# --- render_watchlist_addition_message: 文字数予算(LINE通知品質改善、修正⑩) -----


def test_render_watchlist_addition_message_normal_case_fits_in_one_message() -> None:
    summary = _summary(
        [_summary_item("1234", "テスト株式会社", rank=1, total_score=80.0)],
        total_target_count=1,
        ranked_count=1,
    )

    message = render_watchlist_addition_message(summary)

    assert len(message) <= line_notification_service_module._LINE_ADDITION_MESSAGE_CHAR_BUDGET
    assert "1. テスト株式会社（1234）" in message


def test_render_watchlist_addition_message_zero_items_returns_placeholder_call() -> None:
    """summary.itemsが空の場合はnotify_watchlist_additions側で早期returnするため
    render自体は呼ばれない設計だが、render関数単体としても予算内に収まること。"""
    summary = _summary([])

    message = render_watchlist_addition_message(summary)

    assert len(message) <= line_notification_service_module._LINE_ADDITION_MESSAGE_CHAR_BUDGET


def test_render_watchlist_addition_message_at_max_additions_stays_within_budget() -> None:
    """max_watchlist_additions_per_run既定値(20件)まで追加が発生した極端な
    ケースでも、最終本文が文字数予算に収まること。"""
    items = [
        _summary_item(f"{1000 + i}", f"テスト株式会社{i:02d}", rank=i + 1, total_score=100.0 - i)
        for i in range(20)
    ]
    summary = _summary(items, total_target_count=100, ranked_count=90)

    message = render_watchlist_addition_message(summary)

    assert len(message) <= line_notification_service_module._LINE_ADDITION_MESSAGE_CHAR_BUDGET


def test_render_watchlist_addition_message_truncates_beyond_detail_limit() -> None:
    items = [
        _summary_item(f"{1000 + i}", None, rank=i + 1, total_score=100.0 - i) for i in range(13)
    ]
    summary = _summary(items, total_target_count=13, ranked_count=13)

    message = render_watchlist_addition_message(summary)

    assert "11." not in message
    assert "ほか3銘柄を追加しました。" in message


def test_render_watchlist_addition_message_excludes_item_that_would_exceed_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """次の銘柄を詳細表示に含めると最終本文が予算を超えるフィクスチャで、
    その銘柄が詳細表示から除外され省略件数へ計上されること。"""
    monkeypatch.setattr(line_notification_service_module, "_LINE_ADDITION_MESSAGE_CHAR_BUDGET", 600)
    items = [
        _summary_item(f"{1000 + i}", "とても長い会社名" * 3, rank=i + 1, total_score=100.0 - i)
        for i in range(5)
    ]
    summary = _summary(items, total_target_count=5, ranked_count=5)

    message = render_watchlist_addition_message(summary)

    assert len(message) <= 600
    assert "ほか" in message


def test_render_watchlist_addition_message_handles_full_width_characters() -> None:
    """日本語の全角文字を含む場合でも文字数(Python文字列長)ベースで正しく
    予算判定されること(UTF-8バイト数との混同がないこと)。"""
    items = [
        _summary_item(f"{1000 + i}", "全角銘柄名株式会社", rank=i + 1, total_score=100.0 - i)
        for i in range(10)
    ]
    summary = _summary(items, total_target_count=10, ranked_count=10)

    message = render_watchlist_addition_message(summary)

    assert len(message) <= line_notification_service_module._LINE_ADDITION_MESSAGE_CHAR_BUDGET
    assert "全角銘柄名株式会社" in message


def test_render_watchlist_addition_message_minimal_fallback_when_required_parts_exceed_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """タイトル・件数サマリー・評価ポリシーだけで予算を超過する異常ケースでは、
    短縮フォーマットへ切り替わり、それでも件数サマリー・評価日時は残ること。"""
    monkeypatch.setattr(line_notification_service_module, "_LINE_ADDITION_MESSAGE_CHAR_BUDGET", 100)
    long_conditions = [f"非常に長い評価ポリシー条件文その{i}" * 5 for i in range(10)]
    summary = _summary(
        [_summary_item("1234", None, rank=1, total_score=80.0)],
        policy_conditions=long_conditions,
        total_target_count=1,
        ranked_count=1,
    )

    message = render_watchlist_addition_message(summary)

    assert len(message) <= 100
    assert "対象" in message
    assert "評価日時" in message


def test_render_watchlist_addition_message_logs_error_on_minimal_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    monkeypatch.setattr(line_notification_service_module, "_LINE_ADDITION_MESSAGE_CHAR_BUDGET", 50)
    long_conditions = [f"非常に長い評価ポリシー条件文その{i}" * 5 for i in range(10)]
    summary = _summary(
        [_summary_item("1234", None, rank=1, total_score=80.0)],
        policy_conditions=long_conditions,
        total_target_count=1,
        ranked_count=1,
    )

    with caplog.at_level(logging.ERROR):
        render_watchlist_addition_message(summary)

    assert any("exceeds char budget" in r.message for r in caplog.records)


def test_render_watchlist_addition_message_always_within_budget_across_sizes() -> None:
    """様々な追加件数・文言長の組み合わせで、戻り値が常に文字数予算以内に
    収まることを確認する。"""
    for count in (0, 1, 5, 10, 15, 20):
        items = [
            _summary_item(
                f"{2000 + i}", f"銘柄{i}" * (i % 5 + 1), rank=i + 1, total_score=100.0 - i
            )
            for i in range(count)
        ]
        summary = _summary(items, total_target_count=max(count, 1), ranked_count=count)
        message = render_watchlist_addition_message(summary)
        assert len(message) <= line_notification_service_module._LINE_ADDITION_MESSAGE_CHAR_BUDGET


# --- 通知検証モード機能(2026-08追加) -------------------------------------


def test_validation_mode_bypasses_resend_suppression(
    validation_service_and_repos,
) -> None:
    """NORMALなら再送防止で抑止される条件(直近同一内容)でも、VALIDATIONでは
    LINE送信されること(_notification_status_for_sendのバイパス)。"""
    service, repo, client = validation_service_and_repos
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

    assert sent is True
    assert len(client.sent) == 2


def test_validation_mode_does_not_save_notification_log(
    validation_service_and_repos,
) -> None:
    service, repo, client = validation_service_and_repos
    rec = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    assert len(client.sent) == 1
    assert service._log_repo.list_all() == []


def test_validation_mode_prepends_banner_to_line_body(
    validation_service_and_repos,
) -> None:
    service, repo, client = validation_service_and_repos
    rec = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec)

    service.notify_recommendation(rec, _NOW)

    assert client.sent[0].startswith("【🧪 検証モードで送信】")


def test_normal_mode_does_not_prepend_banner(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec)

    service.notify_recommendation(rec, _NOW)

    assert "【🧪 検証モードで送信】" not in client.sent[0]


def test_validation_mode_notify_batch_summary_bypasses_own_dedup(
    validation_service_and_repos,
) -> None:
    """notify_batch_summaryが独自に持つ同日・同内容dedupも、VALIDATIONでは
    バイパスされ、直近と同一内容でも送信されること。"""
    service, _repo, client = validation_service_and_repos

    first = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts=_counts(sent=6, hold=18, data_insufficient=1, suppressed=2),
        now=_NOW,
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts=_counts(sent=6, hold=18, data_insufficient=1, suppressed=2),
        now=_NOW + dt.timedelta(seconds=15),
    )

    assert first is True
    assert second is True
    assert len(client.sent) == 2
    assert all(msg.startswith("【🧪 検証モードで送信】") for msg in client.sent)


def test_validation_mode_buy_candidates_digest_returns_sent_validation(
    validation_service_and_repos,
) -> None:
    """VALIDATIONでは常にSENT_VALIDATIONを返し、SENT_AND_RECORDED/SENT_LOG_FAILED
    は発生しない(NotificationLogを保存しないため)。"""
    service, _repo, client = validation_service_and_repos
    winners = [_make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)]

    results = service.notify_buy_candidates_digest(winners, _NOW)

    assert results == {"4516": "SENT_VALIDATION"}
    assert len(client.sent) == 1
    assert client.sent[0].startswith("【🧪 検証モードで送信】")
    assert service._log_repo.list_all() == []


def test_validation_mode_buy_candidates_digest_never_marks_sent_log_failed(
    validation_service_and_repos, monkeypatch
) -> None:
    """VALIDATIONではNotificationLog.save自体を呼ばないため、その保存が例外を
    投げるよう仕込んでもSENT_LOG_FAILEDには絶対にならない。"""
    service, _repo, _client = validation_service_and_repos
    winners = [_make_buy_pipeline_recommendation(buy_action=BuyAction.STRONG_BUY)]

    def _raise(_entry: object) -> None:
        raise RuntimeError("DynamoDB write failed")

    monkeypatch.setattr(service._log_repo, "save", _raise)

    results = service.notify_buy_candidates_digest(winners, _NOW)

    assert results == {"4516": "SENT_VALIDATION"}


def test_validation_mode_preserves_manual_review_diversion(
    validation_service_and_repos, monkeypatch
) -> None:
    """データ品質チェックで人的確認が必要と判定された場合、VALIDATIONでも
    通常の売却等通知を強制送信せず、NORMAL同様notify_manual_review_requiredへ
    分岐すること(検証banner付きで実送信される)。「VALIDATIONだから元の判定
    通知を強制送信する」動作になっていないことを保証する回帰テスト。
    """
    service, repo, client = validation_service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.BUY)
    repo.save(rec)
    monkeypatch.setattr(service, "_check_data_quality", lambda *a, **kw: (_alert_stub(rec), True))

    outcome = service.evaluate_notification_status(rec, _NOW)

    assert outcome.data_quality_blocked is True
    assert outcome.sent is True
    assert len(client.sent) == 1
    assert "【要手動確認】" in client.sent[0]
    assert client.sent[0].startswith("【🧪 検証モードで送信】")


def test_validation_manual_review_does_not_grow_production_audit_log(tmp_path: Path) -> None:
    """通知検証モード コードレビュー対応(Issue 2): データ品質チェックで人的確認が
    必要と判定されnotify_manual_review_requiredへ分岐した場合でも、_check_data_quality
    内のself._audit.record()(実物のAuditService/AuditLogRepository、保存先のみ
    tmp_pathへ差し替え)がVALIDATIONでは本番AuditLogへ一切保存しないことを、
    _check_data_qualityをモックせず実ロジックを経由させて検証する。
    """
    store_dir = tmp_path / "local_store"
    audit_repo = AuditLogRepository(store_dir=store_dir)
    recommendation_repo = RecommendationRepository(store_dir=store_dir)
    notification_log_repo = NotificationLogRepository(store_dir=store_dir)
    client = _FakeLineClient()
    validation_context = ExecutionContext(mode=ExecutionMode.VALIDATION)
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=notification_log_repo,
        recommendation_repository=recommendation_repo,
        config=_CONFIG,
        audit_service=AuditService(audit_repo, execution_context=validation_context),
        execution_context=validation_context,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )
    # 独立根拠グループが1件のみのSELLは自動確定させず手動確認へ回る
    # (_check_data_qualityの実ロジックがrequires_manual_review=Trueを返す)。
    rec = _make_sell_recommendation(
        recommendation_id="rec-1", reasons=["減配(major)"], independent_evidence_group_count=1
    )
    recommendation_repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    message = client.sent[0]
    assert "【要手動確認】4631 ＤＩＣ" in message
    assert message.startswith("【🧪 検証モードで送信】")
    assert audit_repo.list_all() == []


def test_normal_manual_review_still_grows_audit_log(tmp_path: Path) -> None:
    """NORMAL回帰確認: 同じ経路でもNORMALでは従来どおり監査ログが記録される。"""
    store_dir = tmp_path / "local_store"
    audit_repo = AuditLogRepository(store_dir=store_dir)
    recommendation_repo = RecommendationRepository(store_dir=store_dir)
    notification_log_repo = NotificationLogRepository(store_dir=store_dir)
    client = _FakeLineClient()
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=notification_log_repo,
        recommendation_repository=recommendation_repo,
        config=_CONFIG,
        audit_service=AuditService(audit_repo),
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )
    rec = _make_sell_recommendation(
        recommendation_id="rec-1", reasons=["減配(major)"], independent_evidence_group_count=1
    )
    recommendation_repo.save(rec)

    service.notify_recommendation(rec, _NOW)

    assert len(audit_repo.list_all()) == 1
    assert "【要手動確認】" in client.sent[0]
