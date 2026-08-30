"""通知簡潔化・cross-pipeline重複抑止の結合テスト(コードレビュー対応2026-08)。

単体formatter(format_notification_text())を直接呼んだ出力ではなく、
LineNotificationService → FakeLineClient.push_message という実際の送信経路を
通った本文を検証する(指摘1・指摘5)。
"""

from __future__ import annotations

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
from jstock_advisor.domain.entities.daily_notification_priority import (
    DailyNotificationPriorityRecord,
    build_daily_notification_priority_id,
)
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    IndustryClassification,
    NotificationStatus,
    RecommendationType,
    StockType,
    WatchTransitionType,
    WatchType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    evaluate_profit_taking,
)
from jstock_advisor.infrastructure.line.client import LineClient
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
from jstock_advisor.services.line_notification_service import LineNotificationService

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_CONFIG = load_config()
_MAX_CHARS = 70


class _FakeLineClient(LineClient):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def service(tmp_path: Path) -> tuple[LineNotificationService, _FakeLineClient]:
    store_dir = tmp_path / "local_store"
    client = _FakeLineClient()
    svc = LineNotificationService(
        line_client=client,
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )
    return svc, client


def _buy_recommendation(
    stock_code: str = "4516", recommendation_id: str = "rec-buy-1"
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="日本新薬",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("3440"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3300"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("3100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("3395"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=BuyAction.BUY,
        company_quality_score=72.0,
        stock_types=[StockType.INCOME, StockType.QUALITY],
        reasons=["財務健全性が高評価", "連続増配実績あり"],
    )


def _near_buy_recommendation(
    stock_code: str = "9432", recommendation_id: str = "rec-nb-1"
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="NTT",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
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
        near_buy_consecutive_business_days=4,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("5.1"),
        reasons=["財務健全性が高評価"],
    )


def _watch_before_earnings_recommendation(stock_code: str = "7203") -> Recommendation:
    return Recommendation(
        recommendation_id="rec-wbe-1",
        stock_code=stock_code,
        stock_name="トヨタ自動車",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(),
        price_at_recommendation=Decimal("2800"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=BuyAction.WATCH_BEFORE_EARNINGS,
    )


def _sell_recommendation(
    stock_code: str = "4631", recommendation_id: str = "rec-sell-1"
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="ＤＩＣ",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        sell_prices=SellPriceLevels(
            stop_review_price=PriceWithRationale(price=Decimal("4000"), rationale="x")
        ),
        price_at_recommendation=Decimal("4384"),
        average_purchase_price_at_recommendation=Decimal("3745"),
        shares_at_recommendation=100,
        reasons=["減配(major)", "営業利益の継続悪化(major)"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def _partial_sell_recommendation(
    stock_code: str = "4631", recommendation_id: str = "rec-partial-1"
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="ＤＩＣ",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            recommended_limit_price=PriceWithRationale(price=Decimal("4600"), rationale="x")
        ),
        price_at_recommendation=Decimal("4384"),
        average_purchase_price_at_recommendation=Decimal("3745"),
        shares_at_recommendation=100,
        reasons=["含み益が閾値を超過"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def _daily_priority_record(svc: LineNotificationService, recommendation: Recommendation):
    """当該Recommendationのscopeに対応する当日の優先度記録を返す。"""
    record_id = build_daily_notification_priority_id(
        recommendation.stock_code,
        evaluation_date_jst(_NOW),
        holding_id=recommendation.holding_id,
    )
    return svc._daily_priority_repo.get(record_id)


def _put_daily_priority_record(
    svc: LineNotificationService, stock_code: str, holding_id: str | None, category: str
) -> None:
    """既送信状態を直接作る(通知経路を通さずに読み取り側の分岐だけを検証するため)。

    `category` には未知の文字列も渡せる(fail-close の検証に使う)。
    """
    business_date = evaluation_date_jst(_NOW)
    svc._daily_priority_repo.upsert(
        DailyNotificationPriorityRecord(
            record_id=build_daily_notification_priority_id(
                stock_code, business_date, holding_id=holding_id
            ),
            stock_code=stock_code,
            holding_id=holding_id,
            business_date=business_date,
            priority=4,
            category=category,
        )
    )


def _critical_risk_recommendation(stock_code: str = "1234") -> Recommendation:
    long_reason = (
        "継続企業の前提に重大な疑義が生じたため、緊急に保有内容の見直しを検討してください。"
        "詳細はIR資料をご確認ください。"
    )
    return Recommendation(
        recommendation_id="rec-critical-1",
        stock_code=stock_code,
        stock_name="サンプル株式会社",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.URGENT_HOLDING_REVIEW,
        price_at_recommendation=Decimal("500"),
        shares_at_recommendation=100,
        reasons=[long_reason],
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


# --- A: Recommendation → LineNotificationService → FakeLineClient → 実本文 ---


def test_buy_notification_actual_pushed_body_within_70_chars(service) -> None:
    svc, client = service
    rec = _buy_recommendation()

    # send_recommendation_notification()を直接呼ぶ(データ品質・整合性検証
    # ゲートは別の既存テストで検証済みのため、ここでは実送信経路を通った
    # 本文の簡潔化のみを検証する。notify_recommendation_with_status経由の
    # 送信可否判定はtest_line_notification_service.pyで別途カバーされている)。
    svc.send_recommendation_notification(rec, _NOW)

    assert len(client.sent) == 1
    body = client.sent[0]
    assert rec.stock_code in body
    # G: BUYでは打診買い価格の「打診」表現が従来どおり使われる
    # (コードレビュー対応2026-08、指摘3)。
    assert "打診" in body
    assert len(body) <= _MAX_CHARS
    # 旧長文formatterが誤って使われていないことの確認(旧専用の見出し文言)。
    assert "算出手法間のばらつき" not in body
    assert "通知ID" not in body


def test_near_buy_notification_actual_pushed_body_within_70_chars(service) -> None:
    """コードレビュー対応(2026-08、LINE通知アクション限定化): NEAR BUYは
    evaluate_notification_status経由ではもはや送信されない(NON_ACTIONABLE)。
    本テストの主眼は実送信の可否ではなく、send_recommendation_notification()を
    直接呼んだ場合の本文フォーマット自体(短文・「接近」「打診」表現)であるため、
    そちらは従来どおり直接呼び出して検証する。
    """
    svc, client = service
    rec = _near_buy_recommendation()

    outcome = svc.evaluate_notification_status(rec, _NOW)
    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category is not None and outcome.block_category.value == "NON_ACTIONABLE"
    svc.send_recommendation_notification(rec, _NOW)

    body = client.sent[0]
    assert "接近" in body
    assert rec.stock_code in body
    assert len(body) <= _MAX_CHARS
    # G: NEAR BUYでも打診買い価格の「打診」表現が従来どおり使われる
    # (コードレビュー対応2026-08、指摘3)。
    assert "打診" in body


def test_watch_before_earnings_notification_actual_pushed_body_within_70_chars(
    service,
) -> None:
    svc, client = service
    rec = _watch_before_earnings_recommendation()

    svc.send_recommendation_notification(rec, _NOW)

    body = client.sent[0]
    assert "決算待ち" in body
    assert rec.stock_code in body
    assert len(body) <= _MAX_CHARS


def test_sell_notification_actual_pushed_body_within_70_chars(service) -> None:
    svc, client = service
    rec = _sell_recommendation()

    svc.notify_recommendation(rec, _NOW)

    body = client.sent[0]
    assert rec.stock_code in body
    assert len(body) <= _MAX_CHARS
    assert "投資前提悪化の可能性" not in body  # 旧_format_sell_messageのタイトル文言
    # F: SELLでは「打診」を使わず、価格フィールドの業務的意味に応じた
    # ラベル(このfixtureはstop_review_price設定のため「見直し」)を使う
    # (コードレビュー対応2026-08、指摘3)。
    assert "打診" not in body
    assert "見直し" in body


def test_critical_risk_notification_keeps_reason_even_if_over_70_chars(service) -> None:
    svc, client = service
    rec = _critical_risk_recommendation()

    svc.notify_recommendation(rec, _NOW)

    body = client.sent[0]
    assert "継続企業の前提に重大な疑義が生じたため" in body
    assert "IR資料をご確認ください" in body


# --- B: WatchState day4 → BUY昇格 → Recommendation → 通知 → 「4日監視後」 ---


def test_promoted_to_buy_notification_shows_reached_label_and_days(service) -> None:
    svc, client = service
    rec = _buy_recommendation(stock_code="9432", recommendation_id="rec-promoted-1").model_copy(
        update={
            "watch_transition_type": WatchTransitionType.PROMOTED_TO_BUY.value,
            "watch_previous_consecutive_business_days": 4,
        }
    )

    svc.send_recommendation_notification(rec, _NOW)

    body = client.sent[0]
    assert body.startswith("到達")
    assert "4日監視後" in body
    assert rec.stock_code in body
    assert len(body) <= _MAX_CHARS


# --- E/F: cross-pipeline優先度(同一銘柄・同一日) ---


def test_near_buy_sent_then_sell_still_sent_higher_priority(service) -> None:
    """E: 同日NEAR BUY通知済み → SELL発生 → SELLは送信される(高優先度は必ず貫通)。"""
    svc, client = service
    stock_code = "9432"
    near_buy = _near_buy_recommendation(stock_code=stock_code, recommendation_id="rec-nb-e")
    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-e")

    svc.send_recommendation_notification(near_buy, _NOW)
    assert len(client.sent) == 1

    priority = svc.check_cross_pipeline_priority_eligibility(sell, _NOW)
    assert priority.eligible is True

    svc.send_recommendation_notification(sell, _NOW)
    assert len(client.sent) == 2
    assert "売却検討" in client.sent[1]


def test_sell_sent_then_near_buy_suppressed_lower_priority(service) -> None:
    """F: 同日SELL通知済み → NEAR BUY発生 → NEAR BUYは送信されない。

    コードレビュー対応(2026-08、LINE通知アクション限定化): NEAR_BUYはもはや
    LINE送信されない(NON_ACTIONABLE)カテゴリのため、cross-pipeline重複抑止
    (_NOTIFICATION_PRIORITY)の対象から外れた(priority=0扱い、eligible=True)。
    抑止の理由がLOW_PRIORITYからNON_ACTIONABLEへ変わっただけで、「NEAR BUYが
    追加送信されない」という結果自体は変わらない。
    """
    svc, client = service
    stock_code = "9432"
    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-f")
    near_buy = _near_buy_recommendation(stock_code=stock_code, recommendation_id="rec-nb-f")

    svc.send_recommendation_notification(sell, _NOW)
    assert len(client.sent) == 1

    priority = svc.check_cross_pipeline_priority_eligibility(near_buy, _NOW)
    assert priority.eligible is True  # NEAR_BUYはcross-pipeline優先度表の対象外(priority=0)

    # evaluate_notification_status経由でNOT_REQUIRED(NON_ACTIONABLE)となり、
    # 実送信されないこと。
    outcome = svc.evaluate_notification_status(near_buy, _NOW)
    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category is not None and outcome.block_category.value == "NON_ACTIONABLE"
    assert len(client.sent) == 1  # SELLの1件のみ、NEAR BUYは追加送信されない


# --- G: PARTIAL_SELLのcross-pipeline優先度統合(横断整合性レビュー対応2026-08、指摘7) ---


def test_partial_sell_and_sell_are_same_tier_second_one_suppressed(service) -> None:
    """G1: 同日SELL通知済み → 同一銘柄のPARTIAL_SELLは同tier(priority 4)の
    ためDUPLICATE_STOCK_NOTIFICATIONとして抑止される(先着優先)。"""
    svc, client = service
    stock_code = "4631"
    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g1")
    partial = _partial_sell_recommendation(
        stock_code=stock_code, recommendation_id="rec-partial-g1"
    )

    svc.send_recommendation_notification(sell, _NOW)
    assert len(client.sent) == 1

    eligibility = svc.check_cross_pipeline_priority_eligibility(partial, _NOW)
    assert eligibility.eligible is False
    assert eligibility.block_category is not None
    assert eligibility.block_category.value == "DUPLICATE_STOCK_NOTIFICATION"


def test_sell_after_partial_sell_same_day_is_allowed_as_escalation(service) -> None:
    """G2(Issue #60 A1で方針転換): 同日PARTIAL_SELL通知済み → 同一銘柄のSELLは
    **エスカレーションとして送信される**。

    旧契約は「sell-side通知は方向を問わず1日1回で十分」という業務判断で、
    同tier(priority 4)のため抑止していた。しかし一部売却と全部売却は利用者へ
    求める行動が異なり、強まった側が届かないのは投資判断に直結するため、
    この組み合わせだけを例外として通す(F-M5)。

    G1(`SELL → PARTIAL_SELL`、逆方向)は従来どおり抑止する。
    """
    svc, client = service
    stock_code = "4631"
    partial = _partial_sell_recommendation(
        stock_code=stock_code, recommendation_id="rec-partial-g2"
    )
    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g2")

    svc.send_recommendation_notification(partial, _NOW)
    assert len(client.sent) == 1

    eligibility = svc.check_cross_pipeline_priority_eligibility(sell, _NOW)
    assert eligibility.eligible is True
    assert eligibility.block_category is None


def test_partial_sell_after_partial_sell_same_day_is_suppressed(service) -> None:
    """G2-a: 同一アクションの再通知は従来どおり抑止する(エスカレーションではない)。"""
    svc, client = service
    stock_code = "4631"
    first = _partial_sell_recommendation(
        stock_code=stock_code, recommendation_id="rec-partial-g2a-1"
    )
    second = _partial_sell_recommendation(
        stock_code=stock_code, recommendation_id="rec-partial-g2a-2"
    )

    svc.send_recommendation_notification(first, _NOW)
    assert len(client.sent) == 1

    eligibility = svc.check_cross_pipeline_priority_eligibility(second, _NOW)
    assert eligibility.eligible is False
    assert eligibility.block_category is not None
    assert eligibility.block_category.value == "DUPLICATE_STOCK_NOTIFICATION"


def test_sell_after_sell_same_day_is_suppressed(service) -> None:
    """G2-b: SELLの再通知も従来どおり抑止する。"""
    svc, client = service
    stock_code = "4631"
    first = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g2b-1")
    second = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g2b-2")

    svc.send_recommendation_notification(first, _NOW)
    assert len(client.sent) == 1

    eligibility = svc.check_cross_pipeline_priority_eligibility(second, _NOW)
    assert eligibility.eligible is False
    assert eligibility.block_category is not None
    assert eligibility.block_category.value == "DUPLICATE_STOCK_NOTIFICATION"


def test_escalation_advances_recorded_category_to_sell(service) -> None:
    """G2-c(Issue #60 A1): エスカレーションで実際にSELLを送ったら、当日の記録の
    categoryをSELLへ前進させる。priorityは4のまま変えない。

    これが無いと記録はPARTIAL_SELLのまま残り、同日2通目以降のSELLも
    「PARTIAL_SELLからのエスカレーション」と誤判定されて送信され続ける。
    """
    svc, client = service
    stock_code = "4631"
    partial = _partial_sell_recommendation(
        stock_code=stock_code, recommendation_id="rec-partial-g2c"
    )
    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g2c")

    svc.send_recommendation_notification(partial, _NOW)
    record = _daily_priority_record(svc, partial)
    assert record is not None
    assert (record.priority, record.category) == (4, "PARTIAL_SELL")

    svc.send_recommendation_notification(sell, _NOW)
    assert len(client.sent) == 2

    record = _daily_priority_record(svc, sell)
    assert record is not None
    assert record.category == "SELL"
    assert record.priority == 4, "priority階層は変更しない"


def test_second_sell_after_escalation_same_day_is_suppressed(service) -> None:
    """G2-d(Issue #60 A1): エスカレーションを許すのは1回だけ。

    PARTIAL_SELL → SELL を通した後、同日さらにSELLが発生しても抑止する
    (エスカレーションの連鎖でSELLが何通も届くことを防ぐ)。
    """
    svc, client = service
    stock_code = "4631"
    svc.send_recommendation_notification(
        _partial_sell_recommendation(stock_code=stock_code, recommendation_id="rec-partial-g2d"),
        _NOW,
    )
    svc.send_recommendation_notification(
        _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g2d-1"), _NOW
    )
    assert len(client.sent) == 2

    third = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g2d-2")
    eligibility = svc.check_cross_pipeline_priority_eligibility(third, _NOW)
    assert eligibility.eligible is False
    assert eligibility.block_category is not None
    assert eligibility.block_category.value == "DUPLICATE_STOCK_NOTIFICATION"


def test_escalation_on_next_jst_day_is_a_normal_send(service) -> None:
    """G2-e: 別JST日のPARTIAL_SELL → SELLは、エスカレーション判定を経ずに
    通常送信される(記録は日付を含むキーで分離されている)。"""
    svc, client = service
    stock_code = "4631"
    svc.send_recommendation_notification(
        _partial_sell_recommendation(stock_code=stock_code, recommendation_id="rec-partial-g2e"),
        _NOW,
    )

    next_day = _NOW + dt.timedelta(days=1)
    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g2e")
    assert svc.check_cross_pipeline_priority_eligibility(sell, next_day).eligible is True


def test_escalation_does_not_leak_to_other_stock(service) -> None:
    """G2-f: 別銘柄のPARTIAL_SELL記録はエスカレーション判定に干渉しない。"""
    svc, client = service
    svc.send_recommendation_notification(
        _partial_sell_recommendation(stock_code="4631", recommendation_id="rec-partial-g2f"), _NOW
    )

    other_sell = _sell_recommendation(stock_code="9999", recommendation_id="rec-sell-g2f")
    assert svc.check_cross_pipeline_priority_eligibility(other_sell, _NOW).eligible is True


def test_escalation_blocked_when_any_same_priority_record_is_not_escalation(service) -> None:
    """G2-g: stock-scopeとholding-scopeの両方が同priorityで存在する場合、
    **1件でもエスカレーションに該当しなければ抑止**する(安全側)。

    holding側がPARTIAL_SELLでも、stock側が既にSELLならSELLを通さない。
    """
    svc, _ = service
    stock_code = "4631"
    holding_id = "owner-a#4631"

    # stock-scope: SELL(エスカレーション元にならない)
    _put_daily_priority_record(svc, stock_code, None, "SELL")
    # holding-scope: PARTIAL_SELL(単独ならエスカレーション元になる)
    _put_daily_priority_record(svc, stock_code, holding_id, "PARTIAL_SELL")

    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g2g")
    sell = sell.model_copy(update={"holding_id": holding_id})

    eligibility = svc.check_cross_pipeline_priority_eligibility(sell, _NOW)
    assert eligibility.eligible is False
    assert eligibility.block_category is not None
    assert eligibility.block_category.value == "DUPLICATE_STOCK_NOTIFICATION"


def test_unknown_recorded_category_does_not_allow_escalation(service) -> None:
    """G2-h: 記録のcategoryを解釈できない場合はエスカレーションを許可しない
    (fail-close。判断できないときに通知を増やす方向へ倒さない)。"""
    svc, _ = service
    stock_code = "4631"
    _put_daily_priority_record(svc, stock_code, None, "SOME_UNKNOWN_CATEGORY")

    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-g2h")
    eligibility = svc.check_cross_pipeline_priority_eligibility(sell, _NOW)
    assert eligibility.eligible is False
    assert eligibility.block_category is not None
    assert eligibility.block_category.value == "DUPLICATE_STOCK_NOTIFICATION"


def test_partial_sell_outranks_buy_and_is_sent(service) -> None:
    """G3: 同日BUY通知済み(priority 3) → 同一銘柄のPARTIAL_SELL(priority 4)
    は高優先度のため貫通・送信される。"""
    svc, client = service
    stock_code = "9432"
    buy = _buy_recommendation(stock_code=stock_code, recommendation_id="rec-buy-g3")
    partial = _partial_sell_recommendation(
        stock_code=stock_code, recommendation_id="rec-partial-g3"
    )

    svc.send_recommendation_notification(buy, _NOW)
    assert len(client.sent) == 1

    eligibility = svc.check_cross_pipeline_priority_eligibility(partial, _NOW)
    assert eligibility.eligible is True

    svc.send_recommendation_notification(partial, _NOW)
    assert len(client.sent) == 2


def test_buy_after_partial_sell_suppressed_as_low_priority(service) -> None:
    """G4: G3の対称ケース。同日PARTIAL_SELL通知済み(priority 4) → 同一銘柄の
    BUY(priority 3)は低優先度のためLOW_PRIORITYとして抑止される。"""
    svc, client = service
    stock_code = "9432"
    partial = _partial_sell_recommendation(
        stock_code=stock_code, recommendation_id="rec-partial-g4"
    )
    buy = _buy_recommendation(stock_code=stock_code, recommendation_id="rec-buy-g4")

    svc.send_recommendation_notification(partial, _NOW)
    assert len(client.sent) == 1

    eligibility = svc.check_cross_pipeline_priority_eligibility(buy, _NOW)
    assert eligibility.eligible is False
    assert eligibility.block_category is not None
    assert eligibility.block_category.value == "LOW_PRIORITY"


def test_critical_risk_always_passes_even_after_partial_sell(service) -> None:
    """G5: 同日PARTIAL_SELL通知済みでも、CRITICAL_RISKは優先度比較自体を
    スキップして必ず貫通する(既存のクールダウン同様の方針、指摘7でも回帰
    しないことを確認)。"""
    svc, client = service
    stock_code = "1234"
    partial = _partial_sell_recommendation(
        stock_code=stock_code, recommendation_id="rec-partial-g5"
    )
    critical = _critical_risk_recommendation(stock_code=stock_code)

    svc.send_recommendation_notification(partial, _NOW)
    assert len(client.sent) == 1

    eligibility = svc.check_cross_pipeline_priority_eligibility(critical, _NOW)
    assert eligibility.eligible is True

    svc.send_recommendation_notification(critical, _NOW)
    assert len(client.sent) == 2


# --- C: 上値余地マトリクスとの整合性(再コードレビュー対応2026-08) ---------------


def _fv_range(
    *, neutral: Decimal, bull: Decimal, bear: Decimal, method_count: int = 3
) -> FairValueRange:
    methods = [
        FairValueMethodResult(
            method=f"method{i}", fair_value=neutral, confidence=ConfidenceLevel.MEDIUM
        )
        for i in range(method_count)
    ]
    return FairValueRange(
        bear=bear,
        neutral=neutral,
        bull=bull,
        overall_confidence=ConfidenceLevel.MEDIUM,
        methods_used=methods,
        methods_excluded=[],
        usable_for_trading_judgment=True,
    )


def _profit_taking_recommendation(
    *,
    stock_code: str,
    current_price: Decimal,
    average_purchase_price: Decimal,
    fair_value_range: FairValueRange,
    recommendation_id: str,
) -> Recommendation:
    """evaluate_profit_taking()の実結果からRecommendationを組み立てる
    (profit_taking_service.pyの本番配線を模した最小限のフィクスチャ)。
    reasons/sell_prices/profit_taking_origin等の構造化フィールドをprofit_taking.py
    の実際の出力から取るため、通知直前の整合性検証(recommendation_consistency_
    validator.py)を実データに近い形で検証できる。
    """
    result = evaluate_profit_taking(
        current_price=current_price,
        average_purchase_price=average_purchase_price,
        shares=100,
        total_purchase_amount=average_purchase_price * 100,
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=fair_value_range,
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="テスト水産",
        recommended_at=_NOW,
        recommendation_type=result.final_action,
        sell_prices=result.sell_prices,
        price_at_recommendation=current_price,
        average_purchase_price_at_recommendation=average_purchase_price,
        shares_at_recommendation=100,
        reasons=result.triggered_reasons,
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        fair_value_overall_confidence=fair_value_range.overall_confidence,
        profit_taking_origin=result.origin,
        profit_taking_ceiling_price=result.ceiling_price,
        profit_taking_upside_pct=result.upside_pct,
    )


def test_full_profit_take_gain28_upside3_reaches_actual_send(service) -> None:
    """再コードレビュー対応(2026-08、指摘1・回帰テスト): 含み益28%×上値余地約3%は
    他の独立条件なしで単独でFULLへ到達する(origin=PRICE_POSITION)。
    通知直前の整合性検証(旧: 含み益率50%未満なら根拠不足とみなす固定閾値)が
    誤ってブロックせず、実際にLINE送信経路まで到達することを確認する。
    """
    svc, client = service
    rec = _profit_taking_recommendation(
        stock_code="1301",
        current_price=Decimal("1280"),  # +28%
        average_purchase_price=Decimal("1000"),
        fair_value_range=_fv_range(
            neutral=Decimal("1250"), bull=Decimal("1318"), bear=Decimal("1200")
        ),
        recommendation_id="rec-full-gain28",
    )
    assert rec.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert rec.profit_taking_origin == "PRICE_POSITION"

    outcome = svc.notify_recommendation_with_status(rec, _NOW)

    assert outcome.status == NotificationStatus.SENT
    assert outcome.sent is True
    assert outcome.data_quality_blocked is False
    assert len(client.sent) == 1
    body = client.sent[0]
    assert rec.stock_code in body
    assert "全部売却検討" in body


def test_partial_profit_take_price_matrix_body_never_exceeds_ceiling(service) -> None:
    """再コードレビュー対応(2026-08、指摘2・回帰テストA・D): origin=PRICE_POSITION
    由来のPARTIALでは、実送信本文の売却目安価格がceiling_price(1,426円)を
    超えず、旧gain+50%の目安値(1,500円)が使われないことを確認する。
    """
    svc, client = service
    rec = _profit_taking_recommendation(
        stock_code="1301",
        current_price=Decimal("1320"),  # +32%
        average_purchase_price=Decimal("1000"),
        fair_value_range=_fv_range(
            neutral=Decimal("1300"), bull=Decimal("1426"), bear=Decimal("1200")
        ),
        recommendation_id="rec-partial-ceiling",
    )
    assert rec.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert rec.profit_taking_origin == "PRICE_POSITION"

    outcome = svc.notify_recommendation_with_status(rec, _NOW)

    assert outcome.status == NotificationStatus.SENT
    assert len(client.sent) == 1
    body = client.sent[0]
    assert "1,320円" in body
    assert "1,500円" not in body


def test_fundamental_critical_risk_body_uses_current_price_not_future_target(
    service,
) -> None:
    """再コードレビュー対応(2026-08、指摘2・回帰テストC): FUNDAMENTAL_CRITICAL_
    RISK由来のFULLは、現在値が適正価格レンジを超過していても、実送信本文が
    旧bull+40%等の遠い未来値ではなく現在値付近を示すことを確認する。
    """
    svc, client = service
    result = evaluate_profit_taking(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CONFIG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_fv_range(
                neutral=Decimal("1500"), bull=Decimal("1500"), bear=Decimal("1500")
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
            investment_premise_broken=True,
        ),
    )
    assert result.final_action == RecommendationType.FULL_PROFIT_TAKE
    assert result.origin == "FUNDAMENTAL_CRITICAL_RISK"
    rec = Recommendation(
        recommendation_id="rec-fundamental-critical",
        stock_code="1301",
        stock_name="テスト水産",
        recommended_at=_NOW,
        recommendation_type=result.final_action,
        sell_prices=result.sell_prices,
        price_at_recommendation=Decimal("1600"),
        average_purchase_price_at_recommendation=Decimal("1000"),
        shares_at_recommendation=100,
        reasons=result.triggered_reasons,
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        profit_taking_origin=result.origin,
        profit_taking_ceiling_price=result.ceiling_price,
        profit_taking_upside_pct=result.upside_pct,
    )

    outcome = svc.notify_recommendation_with_status(rec, _NOW)

    assert outcome.status == NotificationStatus.SENT
    assert len(client.sent) == 1
    body = client.sent[0]
    assert "1,600円" in body
    assert "2,100円" not in body
