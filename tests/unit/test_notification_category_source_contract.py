"""通知カテゴリ・attention_origin の正本(domain 層)の契約テスト(F5。#604 P-b4 の前提)。

`resolve_notification_category()` / `resolve_attention_origin_for_recommendation()` は
services/line_notification_service.py から domain/notification/notification_category.py へ
判定ロジックを変えずに移した。資金回転の観測 Lambda(P)が attention_origin を正本で再計算するとき、
LINE 通知の service を import すると LINE 送信・永続化の closure を引き込み、
#606 の接続ゲートに反する。

固定するもの:
  C1  正本の module の import closure が domain 層だけ(services / infrastructure / config /
      lambda_handlers / cli / providers を含まない)= P の Lambda が安全に import できる
  C2  2 つの関数の定義が src 全体で 1 か所ずつ(重複実装の禁止。移した後に service 側へ戻らない)
  C3  service・handler が使う関数は正本と同一(別の写しを持たない)
  C4  判定は『カテゴリ → 通知意図 → attention_origin』の合成と一致し、全 RecommendationType ×
      Profit Protection のシグナルで成立する(空振りでない: None と非 None の両方が現れる)
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    NotificationCategory,
    ProfitProtectionSignal,
    RecommendationType,
    WatchType,
)
from jstock_advisor.domain.notification import notification_category as source
from jstock_advisor.domain.notification.notification_intent import resolve_attention_origin
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler
from jstock_advisor.services import line_notification_service as service
from tests.factories import build_recommendation
from tests.unit.test_issue_606_production_connection_gate import (
    _module_files,
    _reachable,
)

_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_SOURCE_MODULE = "jstock_advisor.domain.notification.notification_category"
_NOW = dt.datetime(2026, 10, 16, 8, 0, tzinfo=dt.UTC)
_FUNCTIONS = ("resolve_notification_category", "resolve_attention_origin_for_recommendation")


def _recommendation(
    recommendation_type: RecommendationType,
    *,
    signal: str | None = None,
    buy_action: BuyAction | None = None,
    watch_type: WatchType | None = None,
):  # noqa: ANN202 - Recommendation(tests.factories の戻り値の型)
    return build_recommendation(
        recommendation_id=f"f5-{recommendation_type.value}-{signal}-{buy_action}",
        stock_code="1234",
        stock_name="テスト銘柄",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        profit_protection_signal=signal,
        buy_action=buy_action,
        watch_type=watch_type,
    )


# --- C1 import closure ---------------------------------------------------------------------


def test_c1_the_source_module_reaches_only_the_domain_layer() -> None:
    known = _module_files()
    assert _SOURCE_MODULE in known  # 正本の module が実在する(空振りでない)
    closure = _reachable({_SOURCE_MODULE}, known)
    assert len(closure) > 5  # closure を実際に辿れている
    assert "jstock_advisor.domain.entities.enums" in closure
    assert "jstock_advisor.domain.notification.notification_intent" in closure
    outside = sorted(
        m for m in closure if m != "jstock_advisor" and not m.startswith("jstock_advisor.domain")
    )
    assert outside == [], f"domain 層の外へ到達している: {outside}"


def test_c1_the_closure_check_is_not_vacuous() -> None:
    """service を根にすると domain の外へ到達する(C1 は『何も到達しない』で通っていない)。"""
    known = _module_files()
    closure = _reachable({"jstock_advisor.services.line_notification_service"}, known)
    assert any(m.startswith("jstock_advisor.infrastructure") for m in closure)
    assert any(m.startswith("jstock_advisor.config") for m in closure)


# --- C2 定義は 1 か所 -------------------------------------------------------------------------


def _definitions(name: str) -> list[str]:
    found: list[str] = []
    for path in _PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                found.append(path.relative_to(_PACKAGE).as_posix())
    return found


@pytest.mark.parametrize("name", _FUNCTIONS)
def test_c2_each_function_is_defined_exactly_once_in_the_source_module(name: str) -> None:
    assert _definitions(name) == ["domain/notification/notification_category.py"]


# --- C3 使う側は正本と同一 ---------------------------------------------------------------------


def test_c3_the_service_and_the_handler_use_the_source_functions() -> None:
    assert service.resolve_notification_category is source.resolve_notification_category
    assert (
        service.resolve_attention_origin_for_recommendation
        is source.resolve_attention_origin_for_recommendation
    )
    assert handler.resolve_notification_category is source.resolve_notification_category
    assert (
        handler.resolve_attention_origin_for_recommendation
        is source.resolve_attention_origin_for_recommendation
    )


# --- C4 判定の一致 ----------------------------------------------------------------------------

_SIGNALS: tuple[str | None, ...] = (None, *(s.value for s in ProfitProtectionSignal))


def test_c4_the_origin_is_the_composition_of_category_and_intent_for_every_combination() -> None:
    origins: set[str | None] = set()
    categories: set[NotificationCategory] = set()
    for recommendation_type in RecommendationType:
        for signal in _SIGNALS:
            recommendation = _recommendation(recommendation_type, signal=signal)
            category = source.resolve_notification_category(recommendation)
            expected = resolve_attention_origin(category, signal)
            assert source.resolve_attention_origin_for_recommendation(recommendation) == expected
            origins.add(expected)
            categories.add(category)
    # 空振りでない: attention でない(None)と 2 種類の attention_origin が現れ、複数のカテゴリを通る
    assert origins == {
        None,
        "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE",
        "PROFIT_PROTECTION_CANDIDATE",
    }
    assert len(categories) >= 5


@pytest.mark.parametrize(
    ("recommendation_type", "signal", "expected"),
    [
        (
            RecommendationType.WATCH,
            ProfitProtectionSignal.STRONG,
            "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE",
        ),
        (RecommendationType.WATCH, ProfitProtectionSignal.CANDIDATE, "PROFIT_PROTECTION_CANDIDATE"),
        (RecommendationType.WATCH, None, None),
        (RecommendationType.FULL_PROFIT_TAKE, ProfitProtectionSignal.STRONG, None),
        (RecommendationType.PARTIAL_PROFIT_TAKE, ProfitProtectionSignal.CANDIDATE, None),
    ],
)
def test_c4_known_origins(
    recommendation_type: RecommendationType, signal: str | None, expected: str | None
) -> None:
    recommendation = _recommendation(recommendation_type, signal=signal)
    assert source.resolve_attention_origin_for_recommendation(recommendation) == expected


def test_c4_the_buy_side_categories_are_unchanged() -> None:
    near_buy = _recommendation(
        RecommendationType.HOLD,
        buy_action=BuyAction.WATCH_FOR_PRICE,
        watch_type=WatchType.NEAR_BUY,
    )
    assert source.resolve_notification_category(near_buy) is NotificationCategory.NEAR_BUY
    before_earnings = _recommendation(
        RecommendationType.HOLD, buy_action=BuyAction.WATCH_BEFORE_EARNINGS
    )
    assert (
        source.resolve_notification_category(before_earnings)
        is NotificationCategory.WATCH_BEFORE_EARNINGS
    )
    # 通常の WATCH_FOR_PRICE(NEAR BUY 非該当)は通知対象外
    plain_watch = _recommendation(RecommendationType.HOLD, buy_action=BuyAction.WATCH_FOR_PRICE)
    assert source.resolve_notification_category(plain_watch) is NotificationCategory.NOT_NOTIFIABLE


# 移す前(main)の実装と全組合せ(RecommendationType × buy_action × watch_type × signal = 1800 通り)を
# 突き合わせて差が 0 であることを確認した上で、buy_action なしの分類を表として固定する。
_GOLDEN_CATEGORY: dict[str, str] = {
    "BUY": "OTHER",
    "WATCH_BUY": "OTHER",
    "HOLD": "OTHER",
    "WATCH": "WATCH",
    "PARTIAL_PROFIT_TAKE": "PARTIAL_SELL",
    "FULL_PROFIT_TAKE": "SELL",
    "SELL": "SELL",
    "URGENT_REVIEW": "CRITICAL_RISK",
    "WATCH_BEFORE_EARNINGS": "WATCH",
    "PARTIAL_RISK_REDUCTION": "PARTIAL_SELL",
    "REVIEW_AFTER_EARNINGS": "WATCH",
    "REVIEW": "MANUAL_REVIEW",
    "MANUAL_REVIEW_REQUIRED": "OTHER",
    "REVIEW_BEFORE_EARNINGS": "WATCH",
    "PORTFOLIO_CONCENTRATION_REVIEW": "WATCH",
    "SELL_CONSIDERATION": "SELL",
    "STRONG_SELL_CONSIDERATION": "SELL",
    "URGENT_HOLDING_REVIEW": "CRITICAL_RISK",
}


def test_c4_the_category_of_every_recommendation_type_is_frozen() -> None:
    # 表が全メンバーを覆う(新しい RecommendationType を足したら、分類を意図して決めて表を更新する)
    assert set(_GOLDEN_CATEGORY) == {t.value for t in RecommendationType}
    actual = {
        t.value: source.resolve_notification_category(_recommendation(t)).value
        for t in RecommendationType
    }
    assert actual == _GOLDEN_CATEGORY
