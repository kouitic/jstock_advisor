import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.daily_notification_priority import (
    STOCK_SCOPE_SUFFIX,
    build_daily_notification_priority_id,
)
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    DividendComparisonOutcome,
    EarningsDateStatus,
    EarningsReleaseConfirmationState,
    ExecutionMode,
    NotificationCategory,
    NotificationIntent,
    NotificationStatus,
    NotificationType,
    RecommendationType,
    RecordDateUnknownReason,
    SourceType,
    WatchType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.jst import evaluation_date_jst
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
    """再コードレビュー対応(2026-08、NotificationIntent fail-closed化):
    recommendation_type=BUYの場合はbuy_actionも設定し、実本番のBUY候補
    パイプライン(統合BUY候補パイプラインが必ずbuy_actionを設定する)と同じ
    形状にする。fail-closed化前は、buy_action未設定のRecommendationType.BUYが
    resolve_notification_category()でOTHER(たまたまACTIONABLEのdenylist漏れで
    送信されていた)に分類されており、本番に存在しない形状へテストが依存していた。

    buy_action=BUYを設定すると、通知直前の整合性検証(_check_buy_consistency、
    recommendation_consistency_validator.py)がentry_buy_price(打診買い価格)の
    設定・current_price<=entry_buy_priceを要求するため、あわせて設定する
    (buy_pricesのtentative/standard/aggressiveはこの検証が実際に参照する
    フィールドではない、entry_buy_price/standard_buy_price/strong_buy_priceが
    正本)。
    """
    is_buy = recommendation_type == RecommendationType.BUY
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        buy_action=BuyAction.BUY if is_buy else None,
        entry_buy_price=Decimal("4200") if is_buy else None,
        standard_buy_price=Decimal(standard_price) if is_buy else None,
        strong_buy_price=Decimal("2900") if is_buy else None,
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
    """再コードレビュー対応(2026-08、NotificationIntent fail-closed化): buy_action
    設定後はNotificationCategory.BUY(SHORT_TEXT_CATEGORIES)の短文フォーマットで
    送信されるため、旧来の長文フォーマット専用だったdisclaimer文言のassertは
    削除した(短文フォーマットにdisclaimerは含まれない、既存仕様どおり)。
    """
    service, repo, client = service_and_repos
    rec = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)
    assert sent is True
    assert len(client.sent) == 1
    assert "2914" in client.sent[0]


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
    """再コードレビュー対応(2026-08、NotificationIntent fail-closed化): 以前は
    RecommendationType.WATCH_BUY(buy_action未設定、fail-closed化前はOTHER経由で
    たまたまACTIONABLEだった廃止済みレガシー型)への「型変化」を使っていたが、
    fail-closed化後はWATCH_BUYが常にINTERNAL_ONLYとなり、この組み合わせでは
    そもそも「型が変わったから再送する」ロジック自体を検証できなくなった
    (WATCH_BUY→WATCHLIST_BUY_SIGNALはBUY→DAILY_BUY_CANDIDATESと異なる
    notification_typeのため、旧テストは実際には_notification_status_for_send()の
    型変化比較を一度も通っていなかった)。同一notification_type(SELL_SIGNAL)を
    共有し、かつ両方ともACTIONABLEなSELL→URGENT_REVIEWの組み合わせに差し替える。
    URGENT_REVIEW(重大リスク)はcheck_cross_pipeline_priority_eligibility()の
    is_critical_risk早期リターンにより優先度比較自体をスキップするため、同日内の
    再評価でもCross Pipeline Priorityに阻まれない。
    """
    service, repo, client = service_and_repos
    rec1 = Recommendation(
        recommendation_id="rec-1",
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    rec2 = Recommendation(
        recommendation_id="rec-2",
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.URGENT_REVIEW,
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(hours=1))

    assert sent is True
    assert len(client.sent) == 2


def test_resend_when_price_changes_beyond_threshold(service_and_repos) -> None:
    """再コードレビュー対応(2026-08、NotificationIntent fail-closed化): fail-closed
    化前はbuy_action未設定のためBUYがOTHER category(cross-pipeline priority対象外、
    priority<=0で早期リターン)扱いだったが、fail-closed化後は正しくBUY category
    (priority=3)として扱われるようになった。そのため同日内の2回目評価はCross
    Pipeline Priority(DUPLICATE_STOCK_NOTIFICATION、同一優先度は同格の重複とみなす)
    に先に捕まってしまい、本テストが検証したい価格変化閾値ロジック
    (_notification_status_for_send())まで到達できなくなった。Cross Pipeline
    Priorityの重複排除は営業日単位のキー(build_daily_notification_priority_id)の
    ため、2回目の評価日を翌日にずらして価格変化閾値ロジックを独立して検証する
    (resend_after_days=5より短いため「日数経過による再送」ではなく「価格変化に
    よる再送」を検証できている)。
    """
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
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(days=1))

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


def _seed_previous_notification(service, repo, rec, sent_at: dt.datetime) -> None:
    """コードレビュー対応(2026-08、LINE通知アクション限定化): REVIEW_AFTER_EARNINGS
    (NotificationCategory.WATCH)はもはやnotify_recommendation経由で実送信されない
    (NON_ACTIONABLE)ため、再送判定ロジック自体の回帰テストでは「直前に送信済み」
    状態をNotificationLog/Recommendationへ直接投入して再現する
    (test_notification_outcome_suppression_reason.pyの既存パターンと同じ)。
    """
    repo.save(rec)
    service._log_repo.save(
        NotificationLog(
            notification_id=str(uuid.uuid4()),
            notification_type=NotificationType.PROFIT_TAKING_SIGNAL,
            stock_code=rec.stock_code,
            content_hash="dummy-hash-not-used-by-earnings-waiting-state-key",
            sent_at=sent_at,
            related_recommendation_id=rec.recommendation_id,
        )
    )


def test_earnings_review_pending_notification_is_non_actionable_but_has_expected_content(
    service_and_repos,
) -> None:
    """REVIEW_AFTER_EARNINGSはNotificationCategory.WATCHに分類されるため、
    コードレビュー対応(2026-08、LINE通知アクション限定化)によりもはや
    notify_recommendation経由では送信されない(NON_ACTIONABLE、明治HD事例の
    決算発表確認待ち通知自体は引き続きAudit/Recommendationへ記録される)。
    メッセージフォーマット自体("決算発表状況確認待ち"・"決算未発表"/"決算発表済み"
    と断定しない表現)は、send_recommendation_notification()を直接呼んで確認する。
    """
    service, repo, client = service_and_repos
    rec = _make_earnings_review_recommendation(
        recommendation_id="rec-1",
        next_review_conditions=["決算発表予定日を経過していますが、無償データから..."],
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)
    assert sent is False
    assert client.sent == []

    service.send_recommendation_notification(rec, _NOW)
    assert len(client.sent) == 1
    assert "決算発表状況確認待ち" in client.sent[0]
    assert "決算未発表" not in client.sent[0]
    assert "決算発表済み" not in client.sent[0]


def test_earnings_review_pending_notification_not_resent_for_same_state(
    service_and_repos,
) -> None:
    """同一のnext_review_conditions(=同一の確認待ち状態)が続く間は再送資格なし。"""
    service, repo, _client = service_and_repos
    conditions = ["決算発表予定日を経過していますが、無償データから実際の発表状況を確認できて"]
    rec1 = _make_earnings_review_recommendation(
        recommendation_id="rec-1", next_review_conditions=conditions
    )
    _seed_previous_notification(service, repo, rec1, _NOW)

    rec2 = _make_earnings_review_recommendation(
        recommendation_id="rec-2", next_review_conditions=conditions
    )
    repo.save(rec2)
    eligibility = service.check_resend_eligibility(rec2, _NOW + dt.timedelta(hours=1))

    assert eligibility.eligible is False


def test_earnings_review_pending_notification_resent_when_state_transitions_to_delayed(
    service_and_repos,
) -> None:
    """AWAITING_CONFIRMATION→DELAYEDのような状態変化は、構造化フィールド
    (earnings_release_confirmation_state)の変化として検知され、価格情報が
    無くても再送資格ありとみなす(デプロイ前対応: 自由文比較から構造化キー
    比較へ変更)。
    """
    service, repo, _client = service_and_repos
    rec1 = _make_earnings_review_recommendation(
        recommendation_id="rec-1",
        next_review_conditions=["決算発表予定日を経過していますが、無償データから..."],
        earnings_release_confirmation_state=EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
    )
    _seed_previous_notification(service, repo, rec1, _NOW)

    rec2 = _make_earnings_review_recommendation(
        recommendation_id="rec-2",
        next_review_conditions=["決算発表予定日を経過し、最新財務データの反映確認が長引いています。"],
        earnings_release_confirmation_state=EarningsReleaseConfirmationState.DELAYED,
    )
    repo.save(rec2)
    eligibility = service.check_resend_eligibility(rec2, _NOW + dt.timedelta(hours=1))

    assert eligibility.eligible is True


def test_earnings_review_pending_notification_not_resent_for_same_delayed_state(
    service_and_repos,
) -> None:
    """DELAYED→DELAYEDのように状態が変わらない場合は、最小再通知時間
    (resend_after_days)を経過するまで再送資格なし。"""
    service, repo, _client = service_and_repos
    rec1 = _make_earnings_review_recommendation(
        recommendation_id="rec-1",
        next_review_conditions=["決算発表予定日を経過し、最新財務データの反映確認が長引いています。"],
        earnings_release_confirmation_state=EarningsReleaseConfirmationState.DELAYED,
    )
    _seed_previous_notification(service, repo, rec1, _NOW)

    rec2 = _make_earnings_review_recommendation(
        recommendation_id="rec-2",
        next_review_conditions=["決算発表予定日を経過し、最新財務データの反映確認が長引いています。"],
        earnings_release_confirmation_state=EarningsReleaseConfirmationState.DELAYED,
    )
    repo.save(rec2)
    eligibility = service.check_resend_eligibility(rec2, _NOW + dt.timedelta(hours=1))

    assert eligibility.eligible is False


def test_earnings_review_pending_notification_resent_when_earnings_date_changes(
    service_and_repos,
) -> None:
    """対象の決算予定日自体が変わった(=別の決算イベントに対する待機)場合は、
    状態ラベルが同じでも再送資格ありとみなす。"""
    service, repo, _client = service_and_repos
    rec1 = _make_earnings_review_recommendation(
        recommendation_id="rec-1",
        next_review_conditions=["決算発表予定日を経過していますが、無償データから..."],
        earnings_date_raw=dt.date(2026, 8, 5),
    )
    _seed_previous_notification(service, repo, rec1, _NOW)

    rec2 = _make_earnings_review_recommendation(
        recommendation_id="rec-2",
        next_review_conditions=["決算発表予定日を経過していますが、無償データから..."],
        earnings_date_raw=dt.date(2026, 11, 5),
    )
    repo.save(rec2)
    eligibility = service.check_resend_eligibility(rec2, _NOW + dt.timedelta(hours=1))

    assert eligibility.eligible is True


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


def test_recommendation_with_consistency_violation_sends_manual_review_alert(
    service_and_repos,
) -> None:
    """コードレビュー対応(2026-08、full_take_extreme_marginの挙動変更): 全株利確
    検討価格が現在値から極端に乖離している場合、内部計算異常(価格算出ロジックの
    不整合)の疑いがあるため、通常の推奨通知の代わりに要確認LINEメッセージを
    安全弁として送信するようになった(以前はmanual_review_required=Falseの
    ままログ記録のみで、通常通知だけを黙って抑止していた)。この経路は
    notify_manual_review_required()を通るため、_check_data_quality内の
    data_quality_alertログ(notify_data_quality_alert専用)は出ない
    (検出内容自体はAuditServiceへ別途記録済み、_check_data_quality参照)。
    """
    service, repo, client = service_and_repos
    # 全株利確検討価格が現在値の100%以上高く、極端な乖離(full_take_extreme_margin)。
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="9000"
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    assert len(client.sent) == 1
    assert "要確認" in client.sent[0]


def test_clean_full_profit_take_is_sent_normally(service_and_repos) -> None:
    """コードレビュー対応(2026-08、LINE通知/監査分離): FULL_PROFIT_TAKEは
    SELLカテゴリ+label_override="全部売却検討"の短文経路で送信される。
    旧長文フォーマット固有の文言(全株利確目標・通知ID等)はもう出ない
    (Recommendation自体には引き続き保持される)。
    """
    service, repo, client = service_and_repos
    # 現在値+10%程度の穏当な価格なので、整合性検証・異常値検知いずれも問題を検出しない
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="4600"
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    assert len(client.sent) == 1
    message = client.sent[0]
    assert "データ品質アラート" not in message
    assert "全部売却検討" in message
    assert rec.stock_code in message
    assert "全株利確目標" not in message
    assert f"通知ID: {rec.recommendation_id}" not in message
    assert len(message) <= 70


def test_message_does_not_show_record_date_unknown_reason(
    service_and_repos,
) -> None:
    """コードレビュー対応(2026-08、LINE通知/監査分離): 配当基準日不明の
    詳細理由はLINE本文から削除され、Recommendation側にのみ保持される。
    """
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

    assert "不明(データ提供元が非対応(恒久的))" not in client.sent[0]
    assert rec.dividend_record_date_unknown_reason == RecordDateUnknownReason.DATA_PROVIDER_MISSING


def test_message_does_not_show_dividend_comparison_with_fiscal_years(
    service_and_repos,
) -> None:
    """コードレビュー対応(2026-08、LINE通知/監査分離): 配当比較(期別)の
    詳細はLINE本文から削除され、Recommendation側にのみ保持される。
    """
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

    assert "配当比較(2025 → 2026): 減配(実績確定)" not in client.sent[0]
    assert rec.dividend_comparison_outcome == DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT


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


def test_sell_message_with_insufficient_evidence_is_logged_not_sent(
    service_and_repos,
) -> None:
    """独立根拠グループが1件のみのSELLは、要求仕様§15・§16により自動確定させない。

    コードレビュー対応(2026-08、LINE通知アクション限定化): 証拠不足
    (独立根拠グループ不足)は内部論理矛盾ではなく証拠の情報源品質の問題である
    ため、もはや要確認LINEの安全弁(notify_manual_review_required)を発火させ
    ない(is_evidence_quality_issue=True・manual_review_required=False)。
    ログへは記録されるが、LINEへは何も送信されない。
    """
    service, repo, client = service_and_repos
    rec = _make_sell_recommendation(
        recommendation_id="rec-1", reasons=["減配(major)"], independent_evidence_group_count=1
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is False
    assert client.sent == []


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


def test_data_quality_alert_logs_stock_name_and_recommended_action(
    service_and_repos, caplog
) -> None:
    """証拠品質系(manual_review_required=False)のデータ品質アラートは
    notify_data_quality_alert()経由でログのみに記録され、LINEへは送信されない
    (stock_name・recommended_actionを含む)。

    コードレビュー対応(2026-08、LINE通知アクション限定化): 以前はこのログ
    経路をfull_take_extreme_marginのシナリオで確認していたが、そちらは
    manual_review_required=Trueへ挙動変更されnotify_manual_review_required()
    (別のログを出さない経路)を通るようになったため、代わりに証拠品質系のまま
    据え置かれたsell_based_on_single_evidenceのシナリオで確認する。
    """
    service, repo, client = service_and_repos
    rec = _make_sell_recommendation(
        recommendation_id="rec-1", reasons=["減配(major)"], independent_evidence_group_count=1
    )
    repo.save(rec)

    with caplog.at_level("WARNING"):
        service.notify_recommendation(rec, _NOW)

    assert client.sent == []
    assert f"stock_code={rec.stock_code} {rec.stock_name}" in caplog.text
    assert "sell_based_on_single_evidence" in caplog.text


# --- 再コードレビュー対応(2026-08、指摘3): NON_ACTIONABLEゲートより内部論理矛盾の
# 安全弁を先に評価する ------------------------------------------------------


def _make_review_recommendation_with_immediate_execution_price() -> Recommendation:
    return Recommendation(
        recommendation_id="rec-review-contradiction",
        stock_code="4631",
        stock_name="ＤＩＣ",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.REVIEW,
        sell_prices=SellPriceLevels(
            immediate_execution_price=PriceWithRationale(price=Decimal("4200"), rationale="x")
        ),
        price_at_recommendation=Decimal("4200"),
        reasons=["適正価格レンジ上限を超過"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def test_review_with_immediate_execution_price_contradiction_sends_safety_valve(
    service_and_repos,
) -> None:
    """再コードレビュー対応(2026-08、指摘3): REVIEW判定はNotificationCategory.
    MANUAL_REVIEW(NON_ACTIONABLE対象)だが、即時執行価格が残存している
    (_check_review_retains_immediate_execution_price、内部論理矛盾)場合は
    NON_ACTIONABLEゲートより先に評価され、要確認LINEの安全弁が送信される
    (以前はNON_ACTIONABLEゲートがデータ品質チェックより前段にあったため、
    この安全弁自体が構造的に到達不能だった)。
    """
    service, repo, client = service_and_repos
    rec = _make_review_recommendation_with_immediate_execution_price()
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    assert len(client.sent) == 1
    assert "要確認" in client.sent[0]


def test_watch_with_immediate_execution_price_contradiction_sends_safety_valve(
    service_and_repos,
) -> None:
    """再コードレビュー対応(2026-08、指摘3): WATCH判定でも、即時執行価格が
    残存している内部論理矛盾(_check_watch_immediate_execution、今回
    manual_review_required=Trueへ挙動変更)はNON_ACTIONABLEゲートより先に
    評価され、要確認LINEの安全弁が送信される。
    """
    service, repo, client = service_and_repos
    rec = Recommendation(
        recommendation_id="rec-watch-contradiction",
        stock_code="4631",
        stock_name="ＤＩＣ",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH,
        sell_prices=SellPriceLevels(
            immediate_execution_price=PriceWithRationale(price=Decimal("4200"), rationale="x")
        ),
        price_at_recommendation=Decimal("4200"),
        reasons=["適正価格レンジ上限に接近"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    assert len(client.sent) == 1
    assert "要確認" in client.sent[0]


def test_ordinary_review_with_evidence_quality_issue_only_is_not_sent(
    service_and_repos,
) -> None:
    """通常のREVIEW(証拠不足のみが理由で、内部論理矛盾を伴わない)は、
    NON_ACTIONABLEゲート順序変更後も引き続きLINE送信されない(Auditのみ)。
    """
    service, repo, client = service_and_repos
    rec = _make_sell_recommendation(
        recommendation_id="rec-review-evidence-only",
        reasons=["減配(major)"],
        independent_evidence_group_count=1,
    ).model_copy(update={"recommendation_type": RecommendationType.REVIEW})
    repo.save(rec)

    outcome = service.notify_recommendation_with_status(rec, _NOW)

    assert outcome.sent is False
    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category is not None and outcome.block_category.value == "NON_ACTIONABLE"
    assert client.sent == []


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


def _purchase_judgment_counts(
    buy_candidate=0,
    near_buy=0,
    watch_wait=0,
    not_attractive=0,
    manual_review=0,
    data_insufficient=0,
    failed=0,
) -> dict[str, int]:
    return {
        "buy_candidate": buy_candidate,
        "near_buy": near_buy,
        "watch_wait": watch_wait,
        "not_attractive": not_attractive,
        "manual_review": manual_review,
        "data_insufficient": data_insufficient,
        "failed": failed,
    }


def _notification_result_counts(
    sent=0,
    notification_limit=0,
    resend_suppressed=0,
    addon_blocked=0,
    other_suppressed=0,
    send_failed=0,
    other_error=0,
) -> dict[str, int]:
    return {
        "sent": sent,
        "notification_limit": notification_limit,
        "resend_suppressed": resend_suppressed,
        "addon_blocked": addon_blocked,
        "other_suppressed": other_suppressed,
        "send_failed": send_failed,
        "other_error": other_error,
    }


def test_notify_batch_summary_sends_counts(service_and_repos) -> None:
    """買い候補サマリー表示改修(2026-08): 「保有銘柄・ウォッチリスト分析」という
    process_nameを渡していても、holdings専用の4分類kwargsを渡さない呼び出しは
    唯一の実呼び出し元であるbuy_candidates_handler.py用の書式(購入判定/買い候補
    の通知結果)で描画される(is_holdings_call判定はprocess_nameではなくkwargsの
    有無で行われる)。"""
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts={},
        now=_NOW,
        purchase_judgment_counts=_purchase_judgment_counts(
            buy_candidate=6, not_attractive=20, data_insufficient=1
        ),
        notification_result_counts=_notification_result_counts(sent=4, resend_suppressed=2),
    )

    assert sent is True
    assert len(client.sent) == 1
    message = client.sent[0]
    assert "購入判定:" in message
    assert "対象銘柄：27件" in message
    assert "・買い候補：6件" in message
    assert "・買い対象外：20件" in message
    assert "・要確認：0件" in message
    assert "・データ不足：1件" in message
    assert "・処理失敗：0件" in message
    assert "買い候補の通知結果:" in message
    assert "・通知済み：4件" in message
    assert "・再通知抑止：2件" in message
    assert "内訳合計" not in message  # 6+0+0+20+0+1+0=27で一致するため不整合の注記は出ない
    assert "通知結果内訳合計" not in message  # 4+2=6=buy_candidateで一致する


def test_notify_batch_summary_flags_inconsistent_counts(service_and_repos) -> None:
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts={},
        now=_NOW,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=6, not_attractive=18),
        notification_result_counts=_notification_result_counts(sent=6),  # 合計24 != 27
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
        category_counts={},
        now=_NOW,
        data_insufficient_stock_codes=["7042"],
        failed_stock_codes=["1234"],
        purchase_judgment_counts=_purchase_judgment_counts(data_insufficient=1, failed=1),
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
        category_counts={},
        now=_NOW,
        purchase_judgment_counts=_purchase_judgment_counts(
            buy_candidate=6, not_attractive=20, data_insufficient=1
        ),
        notification_result_counts=_notification_result_counts(sent=4, resend_suppressed=2),
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts={},
        now=_NOW + dt.timedelta(seconds=15),
        purchase_judgment_counts=_purchase_judgment_counts(
            buy_candidate=6, not_attractive=20, data_insufficient=1
        ),
        notification_result_counts=_notification_result_counts(sent=4, resend_suppressed=2),
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
        category_counts={},
        now=_NOW,
        purchase_judgment_counts=_purchase_judgment_counts(
            buy_candidate=6, not_attractive=20, data_insufficient=1
        ),
        notification_result_counts=_notification_result_counts(sent=4, resend_suppressed=2),
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=27,
        category_counts={},
        now=_NOW + dt.timedelta(hours=1),
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=9, not_attractive=18),
        notification_result_counts=_notification_result_counts(sent=9),
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
    assert "全部売却検討" in message
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
    """買い候補サマリー表示改修(2026-08)により、旧来の「買い候補(通知上限により
    見送り)」「価格待ち(通知上限により見送り)」という文言・区分は廃止された。
    購入判定(買い候補の総数)と、その通知結果内訳(通知上限等)は別ブロックへ
    明確に分離して表示する。"""
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=68,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=1,
        purchase_judgment_counts=_purchase_judgment_counts(
            buy_candidate=18, not_attractive=40, data_insufficient=5, watch_wait=5
        ),
        notification_result_counts=_notification_result_counts(sent=1, notification_limit=17),
    )

    message = client.sent[0]
    assert "・買い候補：18件" in message
    assert "・通知上限：17件" in message
    assert "買い候補(通知上限により見送り)" not in message
    assert "価格待ち(通知上限により見送り)" not in message


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
    resolve_notification_category()経由の新ゲートでは正しくWATCH_BEFORE_EARNINGS
    カテゴリとして分類される(NOT_NOTIFIABLEには落ちない)ことが確認できる。

    コードレビュー対応(2026-08、LINE通知アクション限定化): ただし
    WATCH_BEFORE_EARNINGS自体はユーザーに明確な売買アクションを促さない
    カテゴリのため、もはやLINE送信はしない(NON_ACTIONABLE、旧
    notify_every_business_day=trueによる毎営業日送信は廃止)。
    """
    service, _repo, client = service_and_repos
    rec = _make_buy_pipeline_recommendation(buy_action=BuyAction.WATCH_BEFORE_EARNINGS)

    outcome = service.evaluate_notification_status(rec, _NOW)

    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category is not None and outcome.block_category.value == "NON_ACTIONABLE"
    assert outcome.sent is False


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
    assert "要確認" in client.sent[0]


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


# --- 買い候補サマリー表示改修(2026-08): 購入判定/通知結果の分離レンダリング ------


def test_notify_batch_summary_shows_all_notification_result_categories_when_nonzero(
    service_and_repos,
) -> None:
    """買い候補の通知結果ブロックは、0件でない区分のみ表示する。ここでは6区分
    (通知済み以外の5区分)すべてを非ゼロにし、全て表示されることを確認する。"""
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=20,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=1,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=19, not_attractive=1),
        notification_result_counts=_notification_result_counts(
            sent=1,
            notification_limit=2,
            resend_suppressed=3,
            addon_blocked=4,
            other_suppressed=5,
            send_failed=4,
        ),
    )

    message = client.sent[0]
    assert "・通知済み：1件" in message
    assert "・通知上限：2件" in message
    assert "・再通知抑止：3件" in message
    assert "・買い増し見送り：4件" in message
    assert "・その他抑止：5件" in message
    assert "・送信失敗：4件" in message


def test_notify_batch_summary_hides_zero_notification_result_categories(
    service_and_repos,
) -> None:
    """買い候補の通知結果ブロックでは、0件の区分は表示しない(「通知済み」のみ
    非ゼロで残り5区分が0件のケース)。"""
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=5,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=5,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=5),
        notification_result_counts=_notification_result_counts(sent=5),
    )

    message = client.sent[0]
    assert "・通知済み：5件" in message
    assert "・通知上限：" not in message
    assert "・再通知抑止：" not in message
    assert "・買い増し見送り：" not in message
    assert "・その他抑止：" not in message
    assert "・送信失敗：" not in message


def test_notify_batch_summary_omits_notification_result_block_when_no_buy_candidates(
    service_and_repos,
) -> None:
    """買い候補が1件も無い日は、通知結果ブロック自体を表示しない
    (通知済み0件を毎回表示するとノイズになるため)。"""
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=3,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=0,
        purchase_judgment_counts=_purchase_judgment_counts(
            near_buy=1, watch_wait=1, not_attractive=1
        ),
    )

    message = client.sent[0]
    assert "買い候補の通知結果:" not in message


def test_notify_batch_summary_no_candidates_block_only_when_all_three_categories_zero(
    service_and_repos,
) -> None:
    """「該当なし」は買い候補・買い間近・買い待ちがすべて0件の日のみ表示する。
    買い候補が存在するが全件抑止された(通知結果は0件)ケースでは、判定は成立して
    いるため「該当なし」を出してはならない(以前の不具合の再発防止)。"""
    service, _repo, client = service_and_repos

    all_zero_message = service.notify_batch_summary(
        "買い候補分析",
        total=3,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=0,
        purchase_judgment_counts=_purchase_judgment_counts(not_attractive=3),
    )
    assert all_zero_message is True
    assert "【今回の購入候補】" in client.sent[0]
    assert "該当なし" in client.sent[0]

    client.sent.clear()
    service.notify_batch_summary(
        "買い候補分析",
        total=3,
        category_counts={},
        now=_NOW + dt.timedelta(hours=1),
        buy_candidates_sent_count=0,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=3),
        notification_result_counts=_notification_result_counts(resend_suppressed=3),
    )
    assert "【今回の購入候補】" not in client.sent[0]
    assert "該当なし" not in client.sent[0]


def test_notify_batch_summary_never_shows_removed_holding_continuation_wording(
    service_and_repos,
) -> None:
    """廃止済みの「通知不要（保有継続）」という文言は、買い候補分析のサマリーに
    一切出現しない(保有銘柄もウォッチリスト銘柄と同じ購入判定区分へ統合された)。"""
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=10,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=2,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=2, not_attractive=8),
        notification_result_counts=_notification_result_counts(sent=2),
    )

    message = client.sent[0]
    assert "通知不要（保有継続）" not in message


def test_notify_batch_summary_never_shows_old_notification_limit_wording(
    service_and_repos,
) -> None:
    """旧来の通知上限見送り文言(買い候補/価格待ち/NEAR BUY監視、廃止済み)は
    完全に消えている(リネームではなく削除)。"""
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=10,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=2,
        purchase_judgment_counts=_purchase_judgment_counts(
            buy_candidate=3, near_buy=2, not_attractive=5
        ),
        notification_result_counts=_notification_result_counts(sent=2, notification_limit=1),
    )

    message = client.sent[0]
    assert "買い候補(通知上限により見送り)" not in message
    assert "価格待ち(通知上限により見送り)" not in message
    assert "NEAR BUY監視(LINE通知なし)" not in message


def test_notify_batch_summary_notification_result_mismatch_warning_shown_only_when_inconsistent(
    service_and_repos,
) -> None:
    """通知結果内訳合計が買い候補件数と一致しない場合のみ警告行を出す。"""
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "買い候補分析",
        total=5,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=3,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=5),
        notification_result_counts=_notification_result_counts(sent=3),  # 3 != 5
    )

    message = client.sent[0]
    assert "通知結果内訳合計(3件)が買い候補件数と一致していません" in message

    client.sent.clear()
    service.notify_batch_summary(
        "買い候補分析",
        total=5,
        category_counts={},
        now=_NOW + dt.timedelta(hours=1),
        buy_candidates_sent_count=5,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=5),
        notification_result_counts=_notification_result_counts(sent=5),  # 一致
    )
    assert "通知結果内訳合計" not in client.sent[0]


def test_notify_batch_summary_content_hash_changes_with_purchase_judgment_and_notification_result(
    service_and_repos,
) -> None:
    """content_hashはpurchase_judgment_counts/notification_result_countsの差異も
    反映する(既存のcategory_counts/total/near_buy等が完全に同一でも、この2つの
    辞書だけが異なれば別内容として扱い、同日dedupで誤って抑止しない)。"""
    service, _repo, client = service_and_repos

    first = service.notify_batch_summary(
        "買い候補分析",
        total=5,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=3,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=5),
        notification_result_counts=_notification_result_counts(sent=3, resend_suppressed=2),
    )
    second = service.notify_batch_summary(
        "買い候補分析",
        total=5,
        category_counts={},
        now=_NOW + dt.timedelta(seconds=10),
        buy_candidates_sent_count=3,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=5),
        # notification_result_countsの内訳だけが異なる(合計は同じ3件)。
        notification_result_counts=_notification_result_counts(sent=3, addon_blocked=2),
    )

    assert first is True
    assert second is True  # content_hashが変わるためdedup抑止されない
    assert len(client.sent) == 2


def test_notify_batch_summary_normal_and_validation_bodies_match_except_title(
    service_and_repos, validation_service_and_repos
) -> None:
    """NORMAL/VALIDATIONで同一件数・同一process_nameを渡した場合、本文は完全に
    一致し、VALIDATIONでは_push()が本文冒頭に検証banner(🧪検証｜)を付与する
    だけである(通知検証モード機能の既存の仕組み。買い候補サマリー表示改修で
    別ロジックを分岐させていないことの回帰確認)。"""
    from jstock_advisor.services.line_notification_service import _VALIDATION_BANNER

    normal_service, _repo, normal_client = service_and_repos
    validation_service, _validation_repo, validation_client = validation_service_and_repos

    kwargs = dict(
        total=10,
        category_counts={},
        now=_NOW,
        buy_candidates_sent_count=3,
        purchase_judgment_counts=_purchase_judgment_counts(buy_candidate=3, not_attractive=7),
        notification_result_counts=_notification_result_counts(sent=3),
    )

    normal_service.notify_batch_summary("買い候補分析", **kwargs)
    validation_service.notify_batch_summary("買い候補分析", **kwargs)

    normal_body = normal_client.sent[0]
    validation_body = validation_client.sent[0]
    assert normal_body.startswith("【買い候補分析完了】")
    assert validation_body == _VALIDATION_BANNER + normal_body


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
    LINE送信されること(_notification_status_for_sendのバイパス)。

    再コードレビュー対応(2026-08、NotificationIntent fail-closed化): buy_action
    設定後はBUY categoryがCross Pipeline Priority対象(priority=3)になるため、
    同日内の2回目評価はそちらの重複排除に先に捕まってしまう(Cross Pipeline
    PriorityはVALIDATIONを特別扱いしない)。2回目の評価日を翌日にずらし、
    本テストが検証したい_notification_status_for_sendのバイパスを独立して
    検証する。
    """
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
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(days=1))

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

    assert client.sent[0].startswith("🧪検証｜")


def test_normal_mode_does_not_prepend_banner(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec = _make_recommendation(
        recommendation_id="rec-1", recommendation_type=RecommendationType.BUY, standard_price="3359"
    )
    repo.save(rec)

    service.notify_recommendation(rec, _NOW)

    assert "🧪検証｜" not in client.sent[0]


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
    assert all(msg.startswith("🧪検証｜") for msg in client.sent)


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
    assert client.sent[0].startswith("🧪検証｜")
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
    assert "要確認" in client.sent[0]
    assert client.sent[0].startswith("🧪検証｜")


def test_validation_manual_review_does_not_grow_production_audit_log(tmp_path: Path) -> None:
    """通知検証モード コードレビュー対応(Issue 2): データ品質チェックで人的確認が
    必要と判定されnotify_manual_review_requiredへ分岐した場合でも、_check_data_quality
    内のself._audit.record()(実物のAuditService/AuditLogRepository、保存先のみ
    tmp_pathへ差し替え)がVALIDATIONでは本番AuditLogへ一切保存しないことを、
    _check_data_qualityをモックせず実ロジックを経由させて検証する。

    コードレビュー対応(2026-08、証拠品質系の分離): sell_based_on_single_evidence
    はもはやmanual_review_requiredを発火させない(is_evidence_quality_issue=True)
    ため、引き続きmanual_review_required=Trueのfull_take_extreme_marginシナリオ
    (全株利確検討価格が現在値から極端に乖離)を使う。
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
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="9000"
    )
    recommendation_repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)

    assert sent is True
    message = client.sent[0]
    assert "要確認" in message
    assert "2914" in message
    assert message.startswith("🧪検証｜")
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
    rec = _make_full_profit_take_recommendation(
        recommendation_id="rec-1", full_take_price="9000"
    )
    recommendation_repo.save(rec)

    service.notify_recommendation(rec, _NOW)

    assert len(audit_repo.list_all()) == 1
    assert "要確認" in client.sent[0]


# --- コードレビュー対応(2026-08、LINE通知/監査分離)の回帰テスト ---

_BLACKLISTED_RATIONALE_MARKERS = (
    "適正価格レンジ",
    "信頼度",
    "通知ID",
    "保有継続を支持する要因",
    "直ちに利確しない理由",
    "監視条件",
)


def _rationale_heavy_fields() -> dict:
    """LINE本文に出てはいけない判断根拠(旧長文フォーマットでのみ表示される
    フィールド群)をまとめて注入するための共通kwargs。"""
    return {
        "counter_factors": ["保有継続を支持する要因: 一時的な悪材料"],
        "not_yet_action_reasons": ["直ちに利確しない理由: 業績が堅調"],
        "next_review_conditions": ["監視条件: 株価が3,900円を割り込む"],
        "fair_value_neutral": Decimal("3800"),
        "fair_value_bull": Decimal("4100"),
        "fair_value_bear": Decimal("3500"),
        "confidence": ConfidenceLevel.HIGH,
    }


def _blacklist_test_recommendations() -> list[Recommendation]:
    common = _rationale_heavy_fields()
    return [
        Recommendation(
            recommendation_id="bl-buy",
            stock_code="1001",
            stock_name="テスト買い",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.BUY,
            buy_action=BuyAction.BUY,
            buy_prices=BuyPriceLevels(
                tentative=PriceWithRationale(price=Decimal("3600"), rationale="x"),
                standard=PriceWithRationale(price=Decimal("3400"), rationale="x"),
                aggressive=PriceWithRationale(price=Decimal("2900"), rationale="x"),
            ),
            price_at_recommendation=Decimal("3550"),
            reasons=["株価が打診水準に到達"],
            rule_version="v1-mvp",
            **common,
        ),
        Recommendation(
            recommendation_id="bl-near-buy",
            stock_code="1002",
            stock_name="テスト打診接近",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.WATCH_BUY,
            buy_action=BuyAction.WATCH_FOR_PRICE,
            watch_type=WatchType.NEAR_BUY,
            buy_prices=BuyPriceLevels(
                tentative=PriceWithRationale(price=Decimal("3600"), rationale="x")
            ),
            price_at_recommendation=Decimal("3650"),
            required_decline_to_entry_pct=1.4,
            rule_version="v1-mvp",
            **common,
        ),
        Recommendation(
            recommendation_id="bl-watch-before-earnings",
            stock_code="1003",
            stock_name="テスト決算前監視",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.WATCH_BUY,
            buy_action=BuyAction.WATCH_BEFORE_EARNINGS,
            price_at_recommendation=Decimal("2000"),
            rule_version="v1-mvp",
            **common,
        ),
        Recommendation(
            recommendation_id="bl-sell",
            stock_code="1004",
            stock_name="テスト売却検討",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.SELL_CONSIDERATION,
            sell_prices=SellPriceLevels(
                stop_review_price=PriceWithRationale(price=Decimal("4000"), rationale="x")
            ),
            price_at_recommendation=Decimal("4384"),
            reasons=["含み益が閾値を超過"],
            rule_version="v1-mvp",
            **common,
        ),
        Recommendation(
            recommendation_id="bl-full-sell",
            stock_code="1005",
            stock_name="テスト全部売却検討",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.STRONG_SELL_CONSIDERATION,
            sell_prices=SellPriceLevels(
                full_profit_consideration_price=PriceWithRationale(
                    price=Decimal("4600"), rationale="x"
                )
            ),
            price_at_recommendation=Decimal("4200"),
            reasons=["含み益が閾値を超過"],
            rule_version="v1-mvp",
            **common,
        ),
        Recommendation(
            recommendation_id="bl-critical",
            stock_code="1006",
            stock_name="テスト緊急確認",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.URGENT_HOLDING_REVIEW,
            sell_prices=SellPriceLevels(
                immediate_execution_price=PriceWithRationale(price=Decimal("1500"), rationale="x")
            ),
            price_at_recommendation=Decimal("1500"),
            reasons=["重大な悪材料を検知"],
            rule_version="v1-mvp",
            **common,
        ),
        Recommendation(
            recommendation_id="bl-watch",
            stock_code="1007",
            stock_name="テスト監視",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.WATCH,
            sell_prices=SellPriceLevels(
                partial_profit_start_price=PriceWithRationale(price=Decimal("2200"), rationale="x")
            ),
            price_at_recommendation=Decimal("2100"),
            rule_version="v1-mvp",
            **common,
        ),
        Recommendation(
            recommendation_id="bl-partial-sell",
            stock_code="1008",
            stock_name="テスト一部売却",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
            sell_prices=SellPriceLevels(
                recommended_limit_price=PriceWithRationale(price=Decimal("2600"), rationale="x")
            ),
            price_at_recommendation=Decimal("2400"),
            rule_version="v1-mvp",
            **common,
        ),
        Recommendation(
            recommendation_id="bl-manual-review",
            stock_code="1009",
            stock_name="テスト要確認",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.REVIEW,
            price_at_recommendation=Decimal("1800"),
            reasons=["根拠が単一のため要確認"],
            rule_version="v1-mvp",
            **common,
        ),
    ]


def test_all_short_form_categories_never_leak_judgment_rationale() -> None:
    """コードレビュー対応(2026-08、LINE通知/監査分離)の受入テスト。
    counter_factors・not_yet_action_reasons・next_review_conditions・
    fair_value_neutral/bull/bear・confidence・recommendation_idを埋めても、
    実送信経路(_render_notification_body)が生成する本文にはそれらの内部判断
    根拠が一切出ないことを確認する(Audit/Recommendation側には引き続き
    フィールドとして保持される)。
    """
    for rec in _blacklist_test_recommendations():
        message = line_notification_service_module._render_notification_body(rec)
        for marker in _BLACKLISTED_RATIONALE_MARKERS:
            assert marker not in message, (
                f"{rec.recommendation_type}: '{marker}' leaked into LINE body: {message!r}"
            )
        assert rec.recommendation_id not in message
        assert rec.stock_code in message


def test_audit_separation_acceptance_cases_a_to_e(service_and_repos) -> None:
    """監査分離の受入テスト(計画§8、A〜E)。LINE本文からは判断根拠を除いても、
    Recommendation(RecommendationRepository経由で永続化された後も)には引き続き
    判断根拠が残っていることを、ケースごとに対で確認する。

    ケースDの「FairValueRange.usable_for_trading_judgment=Falseとその理由」は、
    Recommendationエンティティ自体には持たせていない(FairValueRangeそのものは
    判定計算時にのみ使う一時オブジェクトで、AuditLogEntry.fair_value_resultsへ
    upstream(profit_taking_service.py等)が記録する)ため、本テストの対象外。
    そちらはtest_profit_taking.py::test_full_profit_take_price_excludes_unusable_fair_value
    とtest_sell_price_recommendation_service.pyで「LINEへ捏造された目安価格を
    出さない」側から既に回帰確認済み。

    コードレビュー対応(2026-08、LINE通知アクション限定化): ケースA(WATCH)・
    C(REVIEW)はNotificationCategory.WATCH/MANUAL_REVIEWに分類され、もはや
    notify_recommendation経由では送信されない(NON_ACTIONABLE)。本文の
    フォーマット自体(判断根拠が漏れないこと)はカテゴリの送信可否とは独立した
    性質のため、send_recommendation_notification()を直接呼んで確認する
    (ケースB・Eは実送信経路のまま)。
    """
    service, repo, client = service_and_repos

    # A. ProfitTaking WATCH: 適正価格手法の内訳・confidence・生の適正価格レンジは
    # LINEに出ないが、Recommendationには引き続き保持される。
    rec_a = Recommendation(
        recommendation_id="audit-a-watch",
        stock_code="2001",
        stock_name="ケースA監視",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH,
        sell_prices=SellPriceLevels(
            partial_profit_start_price=PriceWithRationale(price=Decimal("2200"), rationale="x")
        ),
        price_at_recommendation=Decimal("2100"),
        fair_value_neutral=Decimal("1900"),
        fair_value_bull=Decimal("2300"),
        fair_value_bear=Decimal("1600"),
        fair_value_methods=[
            {"method": "DCF", "fair_value": "2300", "confidence": "HIGH"},
            {"method": "PER倍率", "fair_value": "2000", "confidence": "MEDIUM"},
        ],
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )
    repo.save(rec_a)
    service.send_recommendation_notification(rec_a, _NOW)
    message_a = client.sent[-1]
    assert "DCF" not in message_a
    assert "PER倍率" not in message_a
    assert "1,900" not in message_a and "2,300" not in message_a and "1,600" not in message_a
    saved_a = repo.get("audit-a-watch")
    assert saved_a is not None
    assert saved_a.fair_value_methods == rec_a.fair_value_methods
    assert saved_a.fair_value_bull == Decimal("2300")

    # B. PARTIAL_PROFIT_TAKE: 反対材料・監視条件の長文はLINEに出ないが、
    # Recommendationには保持される。
    rec_b = Recommendation(
        recommendation_id="audit-b-partial",
        stock_code="2002",
        stock_name="ケースB一部利確",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            recommended_limit_price=PriceWithRationale(price=Decimal("2600"), rationale="x")
        ),
        price_at_recommendation=Decimal("2400"),
        counter_factors=["業績の一時的な悪化が懸念材料"],
        not_yet_action_reasons=["配当利回りはまだ許容範囲内"],
        next_review_conditions=["株価が2,700円を超過した場合に再評価"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )
    repo.save(rec_b)
    service.notify_recommendation(rec_b, _NOW)
    message_b = client.sent[-1]
    assert "業績の一時的な悪化が懸念材料" not in message_b
    assert "配当利回りはまだ許容範囲内" not in message_b
    assert "株価が2,700円を超過した場合に再評価" not in message_b
    saved_b = repo.get("audit-b-partial")
    assert saved_b is not None
    assert saved_b.counter_factors == rec_b.counter_factors
    assert saved_b.not_yet_action_reasons == rec_b.not_yet_action_reasons
    assert saved_b.next_review_conditions == rec_b.next_review_conditions

    # C. REVIEW(要確認): 検出内容の技術的な詳細(evidence_details)はLINEに
    # 出ないが、Recommendationには保持される。
    rec_c = Recommendation(
        recommendation_id="audit-c-review",
        stock_code="2003",
        stock_name="ケースC要確認",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.REVIEW,
        price_at_recommendation=Decimal("1800"),
        evidence_details=[{"code": "SINGLE_EVIDENCE_ONLY", "detail": "独立根拠数1件のみ"}],
        confidence=ConfidenceLevel.LOW,
        rule_version="v1-mvp",
    )
    repo.save(rec_c)
    service.send_recommendation_notification(rec_c, _NOW)
    message_c = client.sent[-1]
    assert "SINGLE_EVIDENCE_ONLY" not in message_c
    assert "独立根拠数1件のみ" not in message_c
    saved_c = repo.get("audit-c-review")
    assert saved_c is not None
    assert saved_c.evidence_details == rec_c.evidence_details

    # E. 業種モデル未対応: 未対応である旨の技術説明はLINEに出ないが、
    # Recommendationには保持される。
    rec_e = Recommendation(
        recommendation_id="audit-e-industry",
        stock_code="2005",
        stock_name="ケースE業種未対応",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH,
        sell_prices=SellPriceLevels(
            partial_profit_start_price=PriceWithRationale(price=Decimal("2200"), rationale="x")
        ),
        price_at_recommendation=Decimal("2100"),
        industry_model_applied=False,
        industry_model_missing_reason="対象業種の評価モデルが未整備のため標準モデルを使用",
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )
    repo.save(rec_e)
    service.send_recommendation_notification(rec_e, _NOW)
    message_e = client.sent[-1]
    assert "対象業種の評価モデルが未整備のため標準モデルを使用" not in message_e
    assert "業種モデル" not in message_e
    saved_e = repo.get("audit-e-industry")
    assert saved_e is not None
    assert saved_e.industry_model_applied is False
    assert saved_e.industry_model_missing_reason == rec_e.industry_model_missing_reason


def test_notify_batch_summary_new_format_excludes_critical_risk_from_sell_count(
    service_and_repos,
) -> None:
    """コードレビュー対応(2026-08、LINE通知アクション限定化)。バッチサマリー新
    仕様は実際にLINE送信したアクション3分類(一部売却・全部売却・売却)+緊急確認
    で出力し、CRITICAL_RISKは「売却」件数に含めない。WATCH/MANUAL_REVIEWは
    もはやLINE送信されないため、サマリーからも除外された(旧「監視」「要確認」
    分類は廃止)。"""
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=5, hold=5),
        now=_NOW,
        partial_sell_detected_count=2,
        full_sell_detected_count=1,
        sell_detected_count=3,
        critical_risk_detected_count=1,
    )

    assert sent is True
    message = client.sent[0]
    assert "一部売却：2件" in message
    assert "全部売却：1件" in message
    assert "売却：3件" in message
    assert "緊急確認：1件" in message


def test_notify_batch_summary_new_format_omits_critical_risk_segment_when_zero(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos

    service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=5, hold=5),
        now=_NOW,
        partial_sell_detected_count=2,
        full_sell_detected_count=1,
        sell_detected_count=3,
        critical_risk_detected_count=0,
    )

    message = client.sent[0]
    assert "緊急" not in message


# ===== 通知意図3段階化(2026-08)対応: JST営業日1回保証・常時送信への変更 =====
#
# 以前は(1) category_counts+action countsを含むcontent_hashが同一日内で異なれば
# 別内容として再送を許し、(2) holdings4分類がすべて0件の日はサマリー自体を
# 送信しなかった。今回の再設計(修正6・Part 7)で、保有株チェック完了サマリーは
# 【買い候補分析完了】と対称的に「内容に関わらずJST暦日1回のみ」「0件でも必ず
# 送信」という方針へ変更したため、以下のテストは新方針に合わせて書き換えている。


def test_notify_batch_summary_same_jst_business_date_suppresses_even_when_actions_differ(
    service_and_repos,
) -> None:
    """JST暦日が同じであれば、action構成(一部売却/全部売却/売却/緊急確認/
    利益保全注意)が異なっていても2通目は送信しない(修正6: content_hash一致
    だけでは、同日内に件数が変わる形で複数回実行された場合に複数回送信されて
    しまう不備があったため、判定基準をJST暦日一致へ変更した)。"""
    service, _repo, client = service_and_repos

    first = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=2, hold=8),
        now=_NOW,
        partial_sell_detected_count=2,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=2, hold=8),
        now=_NOW + dt.timedelta(seconds=15),
        partial_sell_detected_count=0,
        full_sell_detected_count=0,
        sell_detected_count=2,
        critical_risk_detected_count=0,
    )

    assert first is True
    assert second is False
    assert len(client.sent) == 1


def test_notify_batch_summary_next_jst_business_date_allows_resend(
    service_and_repos,
) -> None:
    """JST暦日が翌日に変われば、内容が同一でも新しいサマリーとして送信できる。"""
    service, _repo, client = service_and_repos

    first = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=2, hold=8),
        now=_NOW,
        partial_sell_detected_count=2,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=2, hold=8),
        now=_NOW + dt.timedelta(days=1),
        partial_sell_detected_count=2,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )

    assert first is True
    assert second is True
    assert len(client.sent) == 2


def test_notify_batch_summary_dedup_boundary_utc_same_day_jst_next_day(
    service_and_repos,
) -> None:
    """23:00 UTC(=翌08:00 JST)をまたぐ境界: UTC暦日は同じでもJST暦日が翌日に
    変わっていれば新しいサマリーとして送信できる(UTC日付でdedupすると本番の
    バッチ実行時刻(23:00 UTC)付近で誤って同日抑止してしまう回帰を防ぐ)。"""
    service, _repo, client = service_and_repos

    first_now = dt.datetime(2026, 7, 24, 14, 0, tzinfo=dt.UTC)  # 2026-07-24 23:00 JST
    second_now = dt.datetime(2026, 7, 24, 16, 0, tzinfo=dt.UTC)  # 2026-07-25 01:00 JST
    assert first_now.date() == second_now.date()  # UTC暦日は同一(2026-07-24)

    first = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=2, hold=8),
        now=first_now,
        partial_sell_detected_count=2,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=2, hold=8),
        now=second_now,
        partial_sell_detected_count=2,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )

    assert first is True
    assert second is True
    assert len(client.sent) == 2


def test_notify_batch_summary_dedup_boundary_jst_same_day_utc_different_day(
    service_and_repos,
) -> None:
    """UTC暦日をまたいでもJST暦日が同じであれば同日扱いとして抑止する
    (JST 00:00〜09:00の間はUTC上前日日付になる境界の回帰確認)。"""
    service, _repo, client = service_and_repos

    first_now = dt.datetime(2026, 7, 24, 23, 30, tzinfo=dt.UTC)  # 2026-07-25 08:30 JST
    second_now = dt.datetime(2026, 7, 25, 0, 30, tzinfo=dt.UTC)  # 2026-07-25 09:30 JST
    assert first_now.date() != second_now.date()  # UTC暦日は異なる

    first = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=2, hold=8),
        now=first_now,
        partial_sell_detected_count=2,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=2, hold=8),
        now=second_now,
        partial_sell_detected_count=2,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )

    assert first is True
    assert second is False
    assert len(client.sent) == 1


def test_notify_batch_summary_holdings_always_sends_even_when_all_action_counts_are_zero(
    service_and_repos,
) -> None:
    """全区分(一部売却/全部売却/売却/緊急確認/利益保全注意)が0件でも、
    【買い候補分析完了】と対称的に常にサマリーを送信する(修正5・Part 7、
    以前は4分類すべて0件の日はサマリー自体を送信しなかった)。"""
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(hold=10),
        now=_NOW,
        partial_sell_detected_count=0,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
        attention_detected_count=0,
    )

    assert sent is True
    message = client.sent[0]
    assert "該当なし" in message
    assert "特に対応が必要な銘柄はありませんでした" in message


def test_notify_batch_summary_sends_when_only_partial_sell_is_nonzero(
    service_and_repos,
) -> None:
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=1, hold=9),
        now=_NOW,
        partial_sell_detected_count=1,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )

    assert sent is True
    assert len(client.sent) == 1


def test_notify_batch_summary_sends_when_only_critical_risk_is_nonzero(
    service_and_repos,
) -> None:
    """緊急確認のみ1件でも、他が0であればサマリー自体は抑止しない。"""
    service, _repo, client = service_and_repos

    sent = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=1, hold=9),
        now=_NOW,
        partial_sell_detected_count=0,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=1,
    )

    assert sent is True
    assert "緊急確認：1件" in client.sent[0]


def test_notify_batch_summary_holdings_send_is_unaffected_by_send_empty_summary_flag(
    service_and_repos,
) -> None:
    """send_empty_summaryはBUY専用のガード(購入候補0件でも「該当なし」を明示
    送信する設計)であり、holdings側の常時送信方針とは無関係な別ロジックである
    ことを確認する。send_empty_summary=True(既定)・False いずれを渡しても、
    holdings呼び出し(action countsのいずれかを渡す)では常に送信される。"""
    service, _repo, client = service_and_repos

    for i, send_empty_summary in enumerate((True, False)):
        sent = service.notify_batch_summary(
            "保有銘柄・ウォッチリスト分析",
            total=10,
            category_counts=_counts(hold=10),
            now=_NOW + dt.timedelta(days=i),
            send_empty_summary=send_empty_summary,
            partial_sell_detected_count=0,
            full_sell_detected_count=0,
            sell_detected_count=0,
            critical_risk_detected_count=0,
        )
        assert sent is True, send_empty_summary
    assert len(client.sent) == 2


def test_notify_batch_summary_attention_detected_count_is_shown_and_reused_for_dedup(
    service_and_repos,
) -> None:
    """利益保全注意はattention_detected_count(個別送信の成否を問わない検出件数)
    をそのまま表示する。ATTENTIONのみ非0件の日も送信され、JST暦日dedupの対象に
    なることを確認する。"""
    service, _repo, client = service_and_repos

    first = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=0, hold=10),
        now=_NOW,
        partial_sell_detected_count=0,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
        attention_detected_count=2,
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=0, hold=10),
        now=_NOW + dt.timedelta(minutes=5),
        partial_sell_detected_count=0,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
        # 個別送信がすべてdedupで抑止されても、検出件数(2)は変わらず表示される
        # 想定を示すため、意図的に同じ値を渡す。
        attention_detected_count=2,
    )

    assert first is True
    assert "利益保全注意：2件" in client.sent[0]
    assert second is False  # 同一JST暦日のため2通目は抑止


def test_notify_batch_summary_suppression_does_not_write_notification_log(
    service_and_repos,
) -> None:
    """同一JST暦日での抑止時はLINE送信もNotificationLog保存も行わない
    (1通目のログのみが残り、2通目の抑止によって上書き・追記されない)。"""
    service, repo, _client = service_and_repos

    service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(hold=10),
        now=_NOW,
        partial_sell_detected_count=0,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )
    first_log = service._log_repo.latest_by_stock_and_type(
        "__batch__:保有銘柄・ウォッチリスト分析", NotificationType.BATCH_SUMMARY
    )
    assert first_log is not None

    service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        total=10,
        category_counts=_counts(sent=5, hold=5),
        now=_NOW + dt.timedelta(minutes=10),
        partial_sell_detected_count=3,
        full_sell_detected_count=0,
        sell_detected_count=0,
        critical_risk_detected_count=0,
    )
    second_log = service._log_repo.latest_by_stock_and_type(
        "__batch__:保有銘柄・ウォッチリスト分析", NotificationType.BATCH_SUMMARY
    )
    assert second_log is not None
    assert second_log.notification_id == first_log.notification_id


def test_normal_and_validation_bodies_differ_only_by_prefix() -> None:
    """コードレビュー対応(2026-08、LINE通知/監査分離、指摘7の訂正版)。同一の
    Recommendationをrenderした場合、NORMAL/VALIDATIONの本文差はバナー
    prefix("🧪検証｜")のみであること(送信件数の一致は本テストの対象外)。
    """
    for rec in _blacklist_test_recommendations():
        body = line_notification_service_module._render_notification_body(rec)
        normal_text = body
        validation_text = line_notification_service_module._VALIDATION_BANNER + body
        assert validation_text == "🧪検証｜" + normal_text
        assert validation_text.removeprefix("🧪検証｜") == normal_text


# --- 再コードレビュー対応(2026-08-14、実装漏れ・回帰2件の修正) ---


def test_resolve_notification_category_profit_taking_watch_before_earnings() -> None:
    """RecommendationType.WATCH_BEFORE_EARNINGS(利確判定エンジンのWATCH抑制
    専用、buy_action=None)がNotificationCategory.WATCHへ分類されること
    (以前はどの分岐にも一致せずOTHERへ落ちていた)。買い候補側の
    BuyAction.WATCH_BEFORE_EARNINGS→NotificationCategory.WATCH_BEFORE_EARNINGS
    という既存経路とは独立していることも確認する。
    """
    rec = Recommendation(
        recommendation_id="cat-watch-before-earnings",
        stock_code="9434",
        stock_name="ソフトバンク",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH_BEFORE_EARNINGS,
        price_at_recommendation=Decimal("235"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )
    assert (
        line_notification_service_module.resolve_notification_category(rec)
        is NotificationCategory.WATCH
    )


def test_profit_taking_watch_before_earnings_routes_to_short_watch_category(
    service_and_repos,
) -> None:
    """再コードレビュー対応(2026-08-14、実装漏れ修正)。上記の分類漏れにより、
    実送信時に旧長文_format_message()が使われ、判断根拠(適正価格レンジ等)が
    LINE本文へ漏れる可能性があった不具合の回帰テスト。

    コードレビュー対応(2026-08、LINE通知アクション限定化): RecommendationType.
    WATCH_BEFORE_EARNINGS(利確判定エンジンのWATCH抑制専用、buy_action=None)は
    NotificationCategory.WATCHに分類され、もはやnotify_recommendation経由では
    送信されない(NON_ACTIONABLE)。短文フォーマット自体の回帰確認(長文漏れが
    無いこと)は、send_recommendation_notification()を直接呼んで検証する。
    """
    service, repo, client = service_and_repos
    rec = Recommendation(
        recommendation_id="watch-before-earnings-1",
        stock_code="9434",
        stock_name="ソフトバンク",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH_BEFORE_EARNINGS,
        sell_prices=SellPriceLevels(
            partial_profit_start_price=PriceWithRationale(price=Decimal("240"), rationale="x")
        ),
        price_at_recommendation=Decimal("235"),
        fair_value_neutral=Decimal("200"),
        fair_value_bull=Decimal("260"),
        fair_value_bear=Decimal("180"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )
    repo.save(rec)

    sent = service.notify_recommendation(rec, _NOW)
    assert sent is False
    assert client.sent == []

    service.send_recommendation_notification(rec, _NOW)
    message = client.sent[0]
    assert "決算発表接近のため様子見" in message
    assert "適正価格レンジ" not in message
    assert "通知ID" not in message
    assert "信頼度" not in message
    assert rec.recommendation_id not in message


def _make_strong_sell_recommendation(
    *, recommendation_id: str, full_price: str, stop_review_price: str
) -> Recommendation:
    # price_at_recommendationはstop_review_priceと意図的に1円ずらす。実際の
    # 生成経路(sell_price_recommendation_service.py)ではstop_review_price=
    # 現在値だが、両者が完全一致すると_check_sell_price_equals_current_as_
    # future_condition()(要求仕様§8、成立済みの現在値を将来条件として提示
    # しない)に引っかかり、要手動確認へ切り替わって本テストの対象(通常の
    # 再送抑止判定)を検証できなくなるため。
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="1010",
        stock_name="テスト全部売却",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.STRONG_SELL_CONSIDERATION,
        sell_prices=SellPriceLevels(
            full_profit_consideration_price=PriceWithRationale(
                price=Decimal(full_price), rationale="x"
            ),
            stop_review_price=PriceWithRationale(
                price=Decimal(stop_review_price), rationale="x"
            ),
        ),
        price_at_recommendation=Decimal(stop_review_price) + Decimal("1"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def test_representative_price_prefers_full_profit_consideration_for_strong_sell() -> None:
    # A. STRONG_SELL_CONSIDERATIONの代表価格はfull_profit_consideration_price
    # (ユーザーへ提示する全部売却目安)を優先し、stop_review_price(現在値の
    # 監視専用フィールド)は見ない。
    rec = _make_strong_sell_recommendation(
        recommendation_id="rp-a", full_price="4000", stop_review_price="5600"
    )
    assert line_notification_service_module._representative_price(rec) == Decimal("4000")


def test_no_resend_when_only_stop_review_price_moves_for_strong_sell(service_and_repos) -> None:
    """B. 再コードレビュー対応(2026-08-14、実装漏れ修正)。全部売却検討の代表
    価格にstop_review_price(常に現在値の監視専用フィールド)を使っていたため、
    実際にユーザーへ提示する全部売却目安(full_profit_consideration_price)が
    変わっていなくても、現在値の変動だけで不要な再送が発生していた不具合の
    回帰テスト。
    """
    service, repo, client = service_and_repos
    rec1 = _make_strong_sell_recommendation(
        recommendation_id="rp-b1", full_price="4000", stop_review_price="5600"
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    # 全部売却目安(4000円)は不変、現在値(stop_review_price)だけ5600→5900(+5.4%)。
    # 翌日にずらすことで、cross-pipeline重複抑止(同日・同優先度カテゴリの
    # 重複送信抑止、resolve_notification_category()参照)ではなく、価格ベース
    # の再送閾値判定(_representative_price())自体を検証する。
    rec2 = _make_strong_sell_recommendation(
        recommendation_id="rp-b2", full_price="4000", stop_review_price="5900"
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(days=1))

    assert sent is False
    assert len(client.sent) == 1


def test_resend_when_full_profit_consideration_price_changes(service_and_repos) -> None:
    # C. 全部売却目安そのものが変化(4000→4300円、+7.5%、閾値3.0%を超過)した
    # 場合は既存のprice_change_resend_threshold_pctの条件どおり再送される
    # (Bと同様、翌日にずらしてcross-pipeline重複抑止の影響を避ける)。
    service, repo, client = service_and_repos
    rec1 = _make_strong_sell_recommendation(
        recommendation_id="rp-c1", full_price="4000", stop_review_price="5600"
    )
    repo.save(rec1)
    service.notify_recommendation(rec1, _NOW)

    rec2 = _make_strong_sell_recommendation(
        recommendation_id="rp-c2", full_price="4300", stop_review_price="5600"
    )
    repo.save(rec2)
    sent = service.notify_recommendation(rec2, _NOW + dt.timedelta(days=1))

    assert sent is True
    assert len(client.sent) == 2


def test_representative_price_selection_unchanged_for_partial_watch_and_normal_sell() -> None:
    # D. PARTIAL_PROFIT_TAKE/WATCH/通常SELL_CONSIDERATIONの既存の代表価格
    # 選択(recommendation_adapter.pyの表示価格選択と同じ優先順位)を壊さない。
    rec_partial = Recommendation(
        recommendation_id="rp-partial",
        stock_code="1011",
        stock_name="テスト一部売却",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            recommended_limit_price=PriceWithRationale(price=Decimal("2600"), rationale="x"),
            partial_profit_start_price=PriceWithRationale(price=Decimal("2300"), rationale="x"),
        ),
        price_at_recommendation=Decimal("2400"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )
    assert line_notification_service_module._representative_price(rec_partial) == Decimal("2600")

    rec_watch = Recommendation(
        recommendation_id="rp-watch",
        stock_code="1012",
        stock_name="テスト監視",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH,
        sell_prices=SellPriceLevels(
            partial_profit_start_price=PriceWithRationale(price=Decimal("2200"), rationale="x")
        ),
        price_at_recommendation=Decimal("2100"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )
    assert line_notification_service_module._representative_price(rec_watch) == Decimal("2200")

    rec_sell = Recommendation(
        recommendation_id="rp-sell",
        stock_code="1013",
        stock_name="テスト売却検討",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL_CONSIDERATION,
        sell_prices=SellPriceLevels(
            stop_review_price=PriceWithRationale(price=Decimal("4000"), rationale="x")
        ),
        price_at_recommendation=Decimal("4384"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )
    assert line_notification_service_module._representative_price(rec_sell) == Decimal("4000")


# ===== 通知意図3段階化(2026-08): ATTENTION(Profit Protection candidate/strong) =====


def _make_attention_watch_recommendation(
    *,
    recommendation_id: str,
    signal: str = "CANDIDATE",
    basis_date: dt.date = dt.date(2026, 6, 1),
    peak_date: dt.date = dt.date(2026, 6, 10),
    peak_price: Decimal = Decimal("1500"),
    stock_code: str = "8136",
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="テスト利益保全",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH,
        price_at_recommendation=Decimal("1400"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        profit_protection_signal=signal,
        profit_protection_basis_date=basis_date,
        profit_protection_peak_price=peak_price,
        profit_protection_peak_date=peak_date,
        profit_protection_peak_gain_pct=58.1,
        profit_protection_current_gain_pct=33.4,
        profit_protection_drawdown_from_peak_pct=15.6,
        profit_protection_gain_giveback_ratio_pct=42.5,
    )


def test_watch_candidate_signal_resolves_to_attention_intent(service_and_repos) -> None:
    service, _repo, _client = service_and_repos
    rec = _make_attention_watch_recommendation(recommendation_id="att-1")

    outcome = service.evaluate_notification_status(rec, _NOW)

    assert outcome.status == NotificationStatus.SENT
    assert outcome.notification_intent is NotificationIntent.ATTENTION
    assert outcome.block_category is None


def test_watch_strong_signal_resolves_to_attention_intent(service_and_repos) -> None:
    service, _repo, _client = service_and_repos
    rec = _make_attention_watch_recommendation(recommendation_id="att-strong", signal="STRONG")

    outcome = service.evaluate_notification_status(rec, _NOW)

    assert outcome.notification_intent is NotificationIntent.ATTENTION


def test_watch_without_profit_protection_signal_stays_internal_only(service_and_repos) -> None:
    service, _repo, _client = service_and_repos
    rec = _make_attention_watch_recommendation(recommendation_id="att-none", signal="NONE")

    outcome = service.evaluate_notification_status(rec, _NOW)

    assert outcome.notification_intent is NotificationIntent.INTERNAL_ONLY
    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_reason == "NON_ACTIONABLE"


def test_attention_notification_first_send_via_with_status(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec = _make_attention_watch_recommendation(recommendation_id="att-send-1")
    repo.save(rec)

    outcome = service.notify_recommendation_with_status(rec, _NOW)

    assert outcome.sent is True
    assert outcome.notification_intent is NotificationIntent.ATTENTION
    assert len(client.sent) == 1
    log = service._log_repo.latest_by_stock_and_type(
        rec.stock_code, NotificationType.PROFIT_PROTECTION_ATTENTION
    )
    assert log is not None
    assert log.related_recommendation_id == rec.recommendation_id


def test_attention_message_never_shows_sell_quantity_fields(service_and_repos) -> None:
    """ATTENTIONはPARTIAL_PROFIT_TAKEではないため、そもそもsuggested_sell_shares/
    ratioがRecommendationに設定されないが、念のため本文にも一切現れないことを
    確認する(build_attention_text_input()がcategory=WATCHを使い、format_
    notification_text()がPARTIAL_SELL以外で売却数量セグメントを構造上生成
    しないことの統合確認)。"""
    service, repo, client = service_and_repos
    rec = _make_attention_watch_recommendation(recommendation_id="att-no-shares", signal="STRONG")
    repo.save(rec)

    service.send_attention_notification(rec, _NOW)

    message = client.sent[0]
    assert "株" not in message
    assert "利益保全注意" in message


def test_attention_origin_differs_between_candidate_and_strong(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec_candidate = _make_attention_watch_recommendation(
        recommendation_id="att-origin-candidate", signal="CANDIDATE", stock_code="8136"
    )
    rec_strong = _make_attention_watch_recommendation(
        recommendation_id="att-origin-strong", signal="STRONG", stock_code="9101"
    )
    repo.save(rec_candidate)
    repo.save(rec_strong)

    service.send_attention_notification(rec_candidate, _NOW)
    service.send_attention_notification(rec_strong, _NOW)

    assert "(一部売却見送り)" not in client.sent[0]
    assert "(一部売却見送り)" in client.sent[1]


def test_attention_same_event_identity_is_deduplicated(service_and_repos) -> None:
    service, repo, client = service_and_repos
    rec_day1 = _make_attention_watch_recommendation(recommendation_id="att-dedup-1")
    repo.save(rec_day1)
    first = service.notify_recommendation_with_status(rec_day1, _NOW)
    assert first.sent is True

    rec_day2 = _make_attention_watch_recommendation(recommendation_id="att-dedup-2")
    repo.save(rec_day2)
    second = service.notify_recommendation_with_status(rec_day2, _NOW + dt.timedelta(days=1))

    assert second.sent is False
    assert second.status == NotificationStatus.DUPLICATE_SUPPRESSED
    assert len(client.sent) == 1


def test_attention_new_peak_allows_resend(service_and_repos) -> None:
    """同じbasis_dateのまま高値が更新された(peak_price上昇)場合は新eventとして
    再送する。"""
    service, repo, client = service_and_repos
    rec_day1 = _make_attention_watch_recommendation(
        recommendation_id="att-newpeak-1", peak_price=Decimal("1500")
    )
    repo.save(rec_day1)
    service.notify_recommendation_with_status(rec_day1, _NOW)

    rec_day2 = _make_attention_watch_recommendation(
        recommendation_id="att-newpeak-2",
        peak_price=Decimal("1600"),
        peak_date=dt.date(2026, 6, 20),
    )
    repo.save(rec_day2)
    second = service.notify_recommendation_with_status(rec_day2, _NOW + dt.timedelta(days=1))

    assert second.sent is True
    assert len(client.sent) == 2


def test_attention_peak_date_only_change_allows_resend(service_and_repos) -> None:
    """再コードレビュー対応(2026-08、指摘7・追加確認): basis_date・peak_priceが
    同一のまま、peak_dateだけが変化した場合(同値の高値が別日に再形成された)も
    新eventとして再送する。event identityが(basis_date, peak_date, peak_price)の
    3要素すべてを見ていることの直接確認(peak_price/peak_dateいずれか一方だけの
    変化テストとは別に、peak_date単独の変化を明示的に検証する)。"""
    service, repo, client = service_and_repos
    rec_day1 = _make_attention_watch_recommendation(
        recommendation_id="att-peakdate-1",
        basis_date=dt.date(2026, 6, 1),
        peak_price=Decimal("1500"),
        peak_date=dt.date(2026, 6, 10),
    )
    repo.save(rec_day1)
    first = service.notify_recommendation_with_status(rec_day1, _NOW)
    assert first.sent is True

    rec_day2 = _make_attention_watch_recommendation(
        recommendation_id="att-peakdate-2",
        basis_date=dt.date(2026, 6, 1),
        peak_price=Decimal("1500"),
        peak_date=dt.date(2026, 6, 20),
    )
    repo.save(rec_day2)
    second = service.notify_recommendation_with_status(rec_day2, _NOW + dt.timedelta(days=1))

    assert second.sent is True
    assert len(client.sent) == 2


def test_attention_all_three_identical_is_deduplicated(service_and_repos) -> None:
    """basis_date・peak_date・peak_priceの3要素すべてが完全一致する場合は
    同一eventとして再送抑止する(new event判定3ケースと対になる回帰確認)。"""
    service, repo, client = service_and_repos
    rec_day1 = _make_attention_watch_recommendation(
        recommendation_id="att-allsame-1",
        basis_date=dt.date(2026, 6, 1),
        peak_price=Decimal("1500"),
        peak_date=dt.date(2026, 6, 10),
    )
    repo.save(rec_day1)
    first = service.notify_recommendation_with_status(rec_day1, _NOW)
    assert first.sent is True

    rec_day2 = _make_attention_watch_recommendation(
        recommendation_id="att-allsame-2",
        basis_date=dt.date(2026, 6, 1),
        peak_price=Decimal("1500"),
        peak_date=dt.date(2026, 6, 10),
    )
    repo.save(rec_day2)
    second = service.notify_recommendation_with_status(rec_day2, _NOW + dt.timedelta(days=1))

    assert second.sent is False
    assert second.status == NotificationStatus.DUPLICATE_SUPPRESSED
    assert len(client.sent) == 1


def test_attention_new_basis_date_allows_resend(service_and_repos) -> None:
    """買い増し・実売却でbasis_dateが進んだ場合は新eventとして再送する
    (peak_price/peak_dateが偶然同じ値であっても)。"""
    service, repo, client = service_and_repos
    rec_day1 = _make_attention_watch_recommendation(recommendation_id="att-newbasis-1")
    repo.save(rec_day1)
    service.notify_recommendation_with_status(rec_day1, _NOW)

    rec_day2 = _make_attention_watch_recommendation(
        recommendation_id="att-newbasis-2", basis_date=dt.date(2026, 7, 1)
    )
    repo.save(rec_day2)
    second = service.notify_recommendation_with_status(rec_day2, _NOW + dt.timedelta(days=1))

    assert second.sent is True
    assert len(client.sent) == 2


def test_attention_missing_basis_date_and_peak_date_does_not_crash(service_and_repos) -> None:
    """basis_date/peak_dateが欠損している(旧Recommendation・後方互換)場合でも
    例外を出さない。欠損値は固定文字列"NONE"としてハッシュ化されるため、残りの
    値(peak_price)が同じであれば従来どおり決定的に同一eventとしてdedupされる
    (欠損時に無条件で再送を許すと同じ局面を繰り返し通知してしまうため、安全側の
    デフォルトはdedup継続)。"""
    service, repo, client = service_and_repos
    rec_day1 = _make_attention_watch_recommendation(recommendation_id="att-missing-1").model_copy(
        update={"profit_protection_basis_date": None, "profit_protection_peak_date": None}
    )
    repo.save(rec_day1)
    first = service.notify_recommendation_with_status(rec_day1, _NOW)
    assert first.sent is True

    # peak_priceも欠損値も同じ→同一eventとして扱われ再送しない。
    rec_day2_same = _make_attention_watch_recommendation(
        recommendation_id="att-missing-2"
    ).model_copy(update={"profit_protection_basis_date": None, "profit_protection_peak_date": None})
    repo.save(rec_day2_same)
    second = service.notify_recommendation_with_status(rec_day2_same, _NOW + dt.timedelta(days=1))
    assert second.sent is False
    assert second.status == NotificationStatus.DUPLICATE_SUPPRESSED

    # peak_priceが変われば、日付が両方とも欠損したままでも新eventとして再送する。
    rec_day3_diff = _make_attention_watch_recommendation(
        recommendation_id="att-missing-3", peak_price=Decimal("1700")
    ).model_copy(update={"profit_protection_basis_date": None, "profit_protection_peak_date": None})
    repo.save(rec_day3_diff)
    third = service.notify_recommendation_with_status(rec_day3_diff, _NOW + dt.timedelta(days=2))
    assert third.sent is True
    assert len(client.sent) == 2


def test_partial_profit_take_is_actionable_not_attention(service_and_repos) -> None:
    """PARTIAL_PROFIT_TAKE(strong_signal成立かつpartial_sale_executable=True)は
    ATTENTIONへ昇格せずACTIONABLEのまま通常経路(send_recommendation_notification)
    で送信される(ATTENTION→PARTIAL昇格時、ATTENTION専用ロジックには一切触れない
    ことの確認)。"""
    service, repo, client = service_and_repos
    rec = Recommendation(
        recommendation_id="att-upgraded-partial",
        stock_code="8136",
        stock_name="テスト利益保全",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            recommended_limit_price=PriceWithRationale(price=Decimal("1450"), rationale="x")
        ),
        price_at_recommendation=Decimal("1400"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        profit_protection_signal="STRONG",
        profit_protection_basis_date=dt.date(2026, 6, 1),
        profit_protection_peak_date=dt.date(2026, 6, 10),
        profit_protection_peak_price=Decimal("1500"),
        suggested_sell_shares=300,
        suggested_sell_ratio=0.5,
    )
    repo.save(rec)

    outcome = service.notify_recommendation_with_status(rec, _NOW)

    assert outcome.sent is True
    assert outcome.notification_intent is NotificationIntent.ACTIONABLE
    assert service._log_repo.latest_by_stock_and_type(
        rec.stock_code, NotificationType.PROFIT_PROTECTION_ATTENTION
    ) is None
    log = service._log_repo.latest_by_stock_and_type(
        rec.stock_code, NotificationType.PROFIT_TAKING_SIGNAL
    )
    assert log is not None


# ===== 再コードレビュー対応(2026-08): ATTENTIONのCross Pipeline Priority参加 =====


def test_notification_priority_attention_is_two(service_and_repos) -> None:
    """指摘10-N: ATTENTION(WATCH+Profit Protection candidate/strong)のpriorityは
    2(CRITICAL_RISK=6 > PROMOTED_TO_BUY=5 > SELL/PARTIAL_SELL=4 > BUY=3 >
    ATTENTION=2 > その他=0)。"""
    rec = _make_attention_watch_recommendation(recommendation_id="prio-attention")
    assert line_notification_service_module.notification_priority_for_recommendation(rec) == 2


def test_notification_priority_normal_watch_is_zero(service_and_repos) -> None:
    """指摘10-O: Profit Protectionシグナルの無い通常WATCHはpriority=0のまま
    (Cross Pipeline Priority対象外)。"""
    rec = _make_attention_watch_recommendation(recommendation_id="prio-watch-normal", signal="NONE")
    assert line_notification_service_module.notification_priority_for_recommendation(rec) == 0


def test_attention_sent_then_buy_is_not_blocked(service_and_repos) -> None:
    """指摘10-P: ATTENTION送信後にBUY → BUYはpriorityが高いため送信可能。"""
    service, repo, client = service_and_repos
    attention_rec = _make_attention_watch_recommendation(recommendation_id="prio-p-attention")
    repo.save(attention_rec)
    first = service.notify_recommendation_with_status(attention_rec, _NOW)
    assert first.sent is True

    buy_rec = _make_recommendation(
        recommendation_id="prio-p-buy", recommendation_type=RecommendationType.BUY,
        standard_price="3359",
    ).model_copy(update={"stock_code": attention_rec.stock_code})
    repo.save(buy_rec)
    second = service.notify_recommendation_with_status(buy_rec, _NOW + dt.timedelta(minutes=5))

    assert second.sent is True
    assert len(client.sent) == 2


def test_buy_sent_then_attention_is_blocked(service_and_repos) -> None:
    """指摘10-Q: BUY送信後にATTENTION → ATTENTIONはpriorityが低いため抑止される
    (LOW_PRIORITY)。"""
    service, repo, client = service_and_repos
    buy_rec = _make_recommendation(
        recommendation_id="prio-q-buy", recommendation_type=RecommendationType.BUY,
        standard_price="3359",
    )
    repo.save(buy_rec)
    first = service.notify_recommendation_with_status(buy_rec, _NOW)
    assert first.sent is True

    attention_rec = _make_attention_watch_recommendation(
        recommendation_id="prio-q-attention", stock_code=buy_rec.stock_code
    )
    repo.save(attention_rec)
    second = service.notify_recommendation_with_status(
        attention_rec, _NOW + dt.timedelta(minutes=5)
    )

    assert second.sent is False
    assert second.block_reason == "LOW_PRIORITY"


def test_attention_sent_then_sell_is_not_blocked(service_and_repos) -> None:
    """指摘10-R: ATTENTION送信後にSELL/一部売却 → priorityが高いため送信可能。"""
    service, repo, client = service_and_repos
    attention_rec = _make_attention_watch_recommendation(recommendation_id="prio-r-attention")
    repo.save(attention_rec)
    first = service.notify_recommendation_with_status(attention_rec, _NOW)
    assert first.sent is True

    sell_rec = Recommendation(
        recommendation_id="prio-r-sell",
        stock_code=attention_rec.stock_code,
        stock_name=attention_rec.stock_name,
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        price_at_recommendation=Decimal("1400"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )
    repo.save(sell_rec)
    second = service.notify_recommendation_with_status(sell_rec, _NOW + dt.timedelta(minutes=5))

    assert second.sent is True
    assert len(client.sent) == 2


def test_sell_sent_then_attention_is_blocked(service_and_repos) -> None:
    """指摘10-S: SELL/一部売却送信後にATTENTION → priorityが低いため抑止される。"""
    service, repo, client = service_and_repos
    sell_rec = Recommendation(
        recommendation_id="prio-s-sell",
        stock_code="8136",
        stock_name="テスト利益保全",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        price_at_recommendation=Decimal("1400"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )
    repo.save(sell_rec)
    first = service.notify_recommendation_with_status(sell_rec, _NOW)
    assert first.sent is True

    attention_rec = _make_attention_watch_recommendation(
        recommendation_id="prio-s-attention", stock_code=sell_rec.stock_code
    )
    repo.save(attention_rec)
    second = service.notify_recommendation_with_status(
        attention_rec, _NOW + dt.timedelta(minutes=5)
    )

    assert second.sent is False
    assert second.block_reason == "LOW_PRIORITY"


def test_attention_sent_then_critical_is_not_blocked(service_and_repos) -> None:
    """指摘10-T: ATTENTION送信後にCRITICAL(至急確認) → 重大リスクは
    Cross Pipeline Priorityの比較自体をスキップし常に送信可能。"""
    service, repo, client = service_and_repos
    attention_rec = _make_attention_watch_recommendation(recommendation_id="prio-t-attention")
    repo.save(attention_rec)
    first = service.notify_recommendation_with_status(attention_rec, _NOW)
    assert first.sent is True

    critical_rec = Recommendation(
        recommendation_id="prio-t-critical",
        stock_code=attention_rec.stock_code,
        stock_name=attention_rec.stock_name,
        recommended_at=_NOW,
        recommendation_type=RecommendationType.URGENT_HOLDING_REVIEW,
        price_at_recommendation=Decimal("1400"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )
    repo.save(critical_rec)
    second = service.notify_recommendation_with_status(
        critical_rec, _NOW + dt.timedelta(minutes=5)
    )

    assert second.sent is True
    assert len(client.sent) == 2


# ===== 再コードレビュー対応(2026-08、JST暦日境界修正) =====
#
# Cross Pipeline Priority(check_cross_pipeline_priority_eligibility/
# _record_daily_priority)・TradeCooldown(check_trade_cooldown_eligibility)の
# 「当日」判定をUTC暦日からJST暦日(evaluation_date_jst)へ統一したことの回帰。
# CP-D〜HはATTENTION⇄BUY/SELL/CRITICALの優先度テストとして既に上記
# test_notification_priority_attention_is_two等でカバー済みのため重複追加しない。


def _sell_recommendation_for_priority(recommendation_id: str, now: dt.datetime) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=now,
        recommendation_type=RecommendationType.SELL,
        price_at_recommendation=Decimal("1400"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def test_cp_a_same_jst_day_different_utc_date_shares_priority_record(service_and_repos) -> None:
    """CP-A: 08:30 JST相当(前日23:30 UTC)と09:10 JST相当(当日00:10 UTC)は
    UTC暦日が異なるが、同一のJST暦日のためCross Pipeline Priorityの同一
    record_idを参照する(同じpriority判定になる)。"""
    service, repo, _client = service_and_repos
    first_now = dt.datetime(2026, 8, 20, 23, 30, tzinfo=dt.UTC)  # 2026-08-21 08:30 JST
    second_now = dt.datetime(2026, 8, 21, 0, 10, tzinfo=dt.UTC)  # 2026-08-21 09:10 JST
    assert first_now.date() != second_now.date()  # UTC暦日は異なることを前提として確認
    assert evaluation_date_jst(first_now) == evaluation_date_jst(second_now)

    sell_rec = _sell_recommendation_for_priority("cp-a-sell", first_now)
    repo.save(sell_rec)
    first_outcome = service.notify_recommendation_with_status(sell_rec, first_now)
    assert first_outcome.sent is True

    attention_rec = _make_attention_watch_recommendation(
        recommendation_id="cp-a-attention", stock_code=sell_rec.stock_code
    )
    repo.save(attention_rec)
    second_outcome = service.notify_recommendation_with_status(attention_rec, second_now)

    # SELL(priority=4)が既に記録されているため、同一JST日である限りATTENTION
    # (priority=2)は抑止される(=同一record_idを参照している証拠)。
    assert second_outcome.sent is False
    assert second_outcome.block_reason == "LOW_PRIORITY"


def test_cp_b_next_jst_day_gets_a_separate_priority_record(service_and_repos) -> None:
    """CP-B: 翌JST日になれば別のrecord_idとなり、前日の優先度記録の影響を
    受けない(ATTENTIONが正常に送信できる)。"""
    service, repo, _client = service_and_repos
    first_now = dt.datetime(2026, 8, 21, 0, 10, tzinfo=dt.UTC)  # 2026-08-21 09:10 JST
    next_day_now = first_now + dt.timedelta(days=1)  # 2026-08-22 09:10 JST
    assert evaluation_date_jst(first_now) != evaluation_date_jst(next_day_now)

    sell_rec = _sell_recommendation_for_priority("cp-b-sell", first_now)
    repo.save(sell_rec)
    first_outcome = service.notify_recommendation_with_status(sell_rec, first_now)
    assert first_outcome.sent is True

    attention_rec = _make_attention_watch_recommendation(
        recommendation_id="cp-b-attention", stock_code=sell_rec.stock_code
    )
    repo.save(attention_rec)
    second_outcome = service.notify_recommendation_with_status(attention_rec, next_day_now)

    assert second_outcome.sent is True


def test_cp_c_record_id_date_matches_business_date_field(service_and_repos) -> None:
    """CP-C: build_daily_notification_priority_id()に渡した日付と、実際に
    upsertされたDailyNotificationPriorityRecord.business_dateが必ず一致する
    (_record_daily_priority()がevaluation_date_jst(now)を1回だけ算出し、
    record_id生成・business_dateフィールドの両方へ同じ値を使うことの確認)。"""
    service, repo, _client = service_and_repos
    now = dt.datetime(2026, 8, 21, 0, 10, tzinfo=dt.UTC)  # 2026-08-21 09:10 JST
    business_date = evaluation_date_jst(now)

    sell_rec = _sell_recommendation_for_priority("cp-c-sell", now)
    repo.save(sell_rec)
    outcome = service.notify_recommendation_with_status(sell_rec, now)
    assert outcome.sent is True

    record_id = build_daily_notification_priority_id(sell_rec.stock_code, business_date)
    stored = service._daily_priority_repo.get(record_id)
    assert stored is not None
    assert stored.business_date == business_date
    assert (
        record_id
        == f"{stored.business_date.isoformat()}:{sell_rec.stock_code}:{STOCK_SCOPE_SUFFIX}"
    )


# ===== TradeCooldown JST暦日境界修正(追加修正1) =====


def _service_with_cooldown_entry(
    tmp_path: Path, cooldown_until_date: dt.date
) -> LineNotificationService:
    store_dir = tmp_path / "local_store"
    HoldingsSnapshotRepository(store_dir=store_dir).upsert(
        HoldingsSnapshotEntry(
            owner=DEFAULT_OWNER,
            holding_id=build_holding_id(DEFAULT_OWNER, "4631"),
            stock_code="4631",
            shares=100,
            average_purchase_price=Decimal("1000"),
            recorded_at=cooldown_until_date - dt.timedelta(days=5),
            cooldown_until_date=cooldown_until_date,
            active_holding=True,
        )
    )
    return LineNotificationService(
        line_client=_FakeLineClient(),
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )


def test_tc_a_cooldown_until_date_boundary_still_suppresses_on_jst_business_date(
    tmp_path: Path,
) -> None:
    """TC-A: cooldown_until_date=2026-08-20、評価時刻が2026-08-20 08:00 JST相当
    → クールダウン中(抑止される)。"""
    service = _service_with_cooldown_entry(tmp_path, dt.date(2026, 8, 20))
    now = dt.datetime(2026, 8, 19, 23, 0, tzinfo=dt.UTC)  # 2026-08-20 08:00 JST
    rec = _sell_recommendation_for_priority("tc-a-sell", now).model_copy(
        update={"stock_code": "4631"}
    )

    eligibility = service.check_trade_cooldown_eligibility(rec, now)

    assert eligibility.eligible is False
    assert eligibility.block_reason == "TRADE_COOLDOWN"


def test_tc_b_next_jst_business_date_releases_cooldown(tmp_path: Path) -> None:
    """TC-B: cooldown_until_date=2026-08-20、評価時刻が2026-08-21 08:00 JST相当
    → クールダウン解除。"""
    service = _service_with_cooldown_entry(tmp_path, dt.date(2026, 8, 20))
    now = dt.datetime(2026, 8, 20, 23, 0, tzinfo=dt.UTC)  # 2026-08-21 08:00 JST
    rec = _sell_recommendation_for_priority("tc-b-sell", now).model_copy(
        update={"stock_code": "4631"}
    )

    eligibility = service.check_trade_cooldown_eligibility(rec, now)

    assert eligibility.eligible is True


def test_tc_c_same_jst_day_different_utc_time_gives_same_cooldown_verdict(
    tmp_path: Path,
) -> None:
    """TC-C: 2026-08-21 08:30 JST相当と2026-08-21 09:10 JST相当(UTC暦日は異なる)
    で、同一のクールダウン判定になる(境界のcooldown_until_date=2026-08-20に対し、
    どちらもJST暦日は2026-08-21のため解除済み)。"""
    service = _service_with_cooldown_entry(tmp_path, dt.date(2026, 8, 20))
    first_now = dt.datetime(2026, 8, 20, 23, 30, tzinfo=dt.UTC)  # 2026-08-21 08:30 JST
    second_now = dt.datetime(2026, 8, 21, 0, 10, tzinfo=dt.UTC)  # 2026-08-21 09:10 JST
    assert first_now.date() != second_now.date()
    rec = _sell_recommendation_for_priority("tc-c-sell", first_now).model_copy(
        update={"stock_code": "4631"}
    )

    first_eligibility = service.check_trade_cooldown_eligibility(rec, first_now)
    second_eligibility = service.check_trade_cooldown_eligibility(rec, second_now)

    assert first_eligibility.eligible is True
    assert second_eligibility.eligible is True


def test_tc_d_existing_cooldown_business_days_config_is_unchanged() -> None:
    """TC-D: 既存のTradeCooldown日数設定(買い増し/一部売却=partial、新規購入・
    全部売却=buy/sell)は今回のJST暦日境界修正では変更されていないことの回帰確認
    (設定値自体の確認)。"""
    assert _CONFIG.notification.trade_cooldown.enabled is not None
    assert _CONFIG.notification.trade_cooldown.partial_trade_business_days >= 1
    assert _CONFIG.notification.trade_cooldown.buy_business_days >= 1
    assert _CONFIG.notification.trade_cooldown.sell_business_days >= 1


# --- Issue #23(2026-08-28): UTC/JST境界の回帰テスト -------------------------
# いずれも「修正前(UTC暦日基準)なら誤り、修正後(JST暦日基準)なら正しい」
# 業務ケースを固定する。


def test_issue23_resend_not_triggered_when_only_utc_date_advanced(
    service_and_repos,
) -> None:
    """resend_after_daysはJST暦日で数える。sent_at=JST 08:50(UTC前日23:50)の
    ように送信がUTC日付境界の手前にあると、UTC暦日差はJST暦日差より1大きく
    なる。JST暦日差がresend_after_days未満(n-1日)のうちは、UTC暦日差が
    ちょうどnに達していても再送してはならない(修正前はUTC差で再送していた)。"""
    service, repo, client = service_and_repos
    n = _CONFIG.notification.resend_after_days

    sent_at = dt.datetime(2026, 7, 23, 23, 50, tzinfo=dt.UTC)  # JST 07-24 08:50
    rec1 = _make_recommendation(
        recommendation_id="rec-jst-1",
        recommendation_type=RecommendationType.BUY,
        standard_price="3359",
    )
    repo.save(rec1)
    assert service.notify_recommendation(rec1, sent_at) is True

    # JST 09:10(UTC 00:10)なのでUTC暦日はsent_atの翌日。そこから(n-1)日後:
    # UTC暦日差 = n(修正前は再送条件成立)、JST暦日差 = n-1(未達)。
    now_not_yet = dt.datetime(2026, 7, 24, 0, 10, tzinfo=dt.UTC) + dt.timedelta(days=n - 1)
    rec2 = _make_recommendation(
        recommendation_id="rec-jst-2",
        recommendation_type=RecommendationType.BUY,
        standard_price="3359",
    )
    repo.save(rec2)
    assert service.notify_recommendation(rec2, now_not_yet) is False
    assert len(client.sent) == 1

    # さらに1日進めるとJST暦日差もnに達し、正しく再送される。
    now_due = dt.datetime(2026, 7, 24, 0, 10, tzinfo=dt.UTC) + dt.timedelta(days=n)
    rec3 = _make_recommendation(
        recommendation_id="rec-jst-3",
        recommendation_type=RecommendationType.BUY,
        standard_price="3359",
    )
    repo.save(rec3)
    assert service.notify_recommendation(rec3, now_due) is True
    assert len(client.sent) == 2


def test_issue23_batch_summary_dedup_stable_across_utc_boundary_same_jst_day(
    service_and_repos,
) -> None:
    """batch summaryのcontent_hashに含める日付はJST暦日。同一JST日・同一内容の
    2回目呼び出し(JST 08:55 → 09:05、UTC日付だけが跨る)はdedup identityが
    同一となり重複抑止される(修正前はUTC日付が変わりhash不一致→二重送信)。"""
    service, _repo, client = service_and_repos
    kwargs = dict(
        total=27,
        category_counts={},
        purchase_judgment_counts=_purchase_judgment_counts(
            buy_candidate=6, not_attractive=20, data_insufficient=1
        ),
        notification_result_counts=_notification_result_counts(sent=4, resend_suppressed=2),
    )
    first = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        now=dt.datetime(2026, 7, 23, 23, 55, tzinfo=dt.UTC),  # JST 07-24 08:55
        **kwargs,
    )
    second = service.notify_batch_summary(
        "保有銘柄・ウォッチリスト分析",
        now=dt.datetime(2026, 7, 24, 0, 5, tzinfo=dt.UTC),  # JST 07-24 09:05
        **kwargs,
    )
    assert first is True
    assert second is False  # 同一JST日・同一内容 -> content_hash一致で抑止
    assert len(client.sent) == 1
