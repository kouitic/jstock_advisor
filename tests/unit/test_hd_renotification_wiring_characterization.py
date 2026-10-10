"""通知の「送る / 送らない」の既存の挙動の characterization(Issue #890 PR-3)。

PR-3 は、全種類の通知が通る共通の関数(services/line_notification_service.py)に、保有判断の 3 種類
だけが通る分岐を足す。実装の**前**に、既存の挙動を網羅して固定し、PR-3 が『保有判断の 3 種類
(かつ、比べられる前回の状態があるとき)以外は 1 つも変えない』ことを証明する土台にする。
期待値は、PR-3 の変更を入れる前の main(f6328f67)の出力から生成した golden
(`tests/fixtures/notification_status_characterization.json`)である。

網羅する組み合わせ
  種類     通知の対応表(_RECOMMENDATION_TO_NOTIFICATION_TYPE)の全ての RecommendationType
  前回     なし(参照先が引けない)/ 同じ種類で価格が 0% / +2.9% / +3.0% / +5.0% / 別の種類
  経過日数 1 日 / resend_after_days - 1 / resend_after_days / resend_after_days + 1
  範囲     stock-scope(holding_id なし)。売却系(SELL_SIGNAL に対応する種類)は holding-scope も
  価格     比較に使われる全ての price field(代表価格の選択に使う全て)に同じ値を置く

保有判断の 3 種類(SELL_CONSIDERATION / STRONG_SELL_CONSIDERATION / URGENT_HOLDING_REVIEW)も、
**状態の記録(hd_renotify_state)が無い**場合は従来と同じ結果になること(旧形式・比べられない場合は
従来の判断に任せる)を、この golden で固定する。状態の記録がある場合の新しい挙動は
test_hd_renotification_wiring.py で固定する。
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from itertools import product
from pathlib import Path
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    NotificationType,
    RecommendationType,
)
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
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
from jstock_advisor.services import line_notification_service as line_module
from jstock_advisor.services.line_notification_service import LineNotificationService
from tests.factories import build_recommendation

_GOLDEN = (
    Path(__file__).resolve().parents[1] / "fixtures" / "notification_status_characterization.json"
)
_CONFIG = load_config()
_RESEND_AFTER_DAYS = _CONFIG.notification.resend_after_days
_NOW = dt.datetime(2026, 9, 9, 8, 0, tzinfo=dt.UTC)  # = JST 2026-09-09 17:00
_STOCK = "0000"
_BASE_PRICE = Decimal("1000")

_ALL_TYPES: tuple[RecommendationType, ...] = tuple(line_module._RECOMMENDATION_TO_NOTIFICATION_TYPE)
_SELL_SIGNAL_TYPES = frozenset(
    t
    for t, n in line_module._RECOMMENDATION_TO_NOTIFICATION_TYPE.items()
    if n is NotificationType.SELL_SIGNAL
)

_DAYS = (1, _RESEND_AFTER_DAYS - 1, _RESEND_AFTER_DAYS, _RESEND_AFTER_DAYS + 1)
_PREVIOUS_KINDS = (
    "none",
    "same_0",
    "same_2_9",
    "same_3_0",
    "same_5_0",
    "other_type",
    "same_no_price",  # 今回も前回も代表価格を持たない(価格を比べられない)
)
_RATIO = {
    "same_0": Decimal("1"),
    "same_2_9": Decimal("1.029"),
    "same_3_0": Decimal("1.03"),
    "same_5_0": Decimal("1.05"),
    "other_type": Decimal("1"),
}


class _FakeLineClient(LineClient):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


def _levels(price: Decimal | None) -> SellPriceLevels:
    if price is None:
        return SellPriceLevels()

    def level(value: Decimal) -> PriceWithRationale:
        return PriceWithRationale(price=value, rationale="架空の根拠")

    return SellPriceLevels(
        partial_profit_start_price=level(price),
        recommended_limit_price=level(price),
        full_profit_consideration_price=level(price),
        immediate_execution_price=level(price),
        stop_review_price=level(price),
    )


def make_recommendation(
    recommendation_type: RecommendationType,
    price: Decimal | None,
    *,
    recommendation_id: str,
    holding_id: str | None = None,
    config_values_used: dict[str, Any] | None = None,
) -> Recommendation:
    kwargs: dict[str, Any] = {}
    if config_values_used is not None:
        kwargs["config_values_used"] = config_values_used
    return build_recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK,
        stock_name="銘柄 X",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        sell_prices=_levels(price),
        price_at_recommendation=price if price is not None else _BASE_PRICE,
        reasons=["架空の理由"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        holding_id=holding_id,
        **kwargs,
    )


def make_service(
    store_dir: Path, days_ago: int, *, holding_id: str | None = None
) -> LineNotificationService:
    """通知の種類ごとに『days_ago 日前に送った』実績を置いたストアで、サービスを作る。"""
    log_repo = NotificationLogRepository(store_dir=store_dir)
    sent_at = _NOW - dt.timedelta(days=days_ago)
    for n, notification_type in enumerate(NotificationType):
        log_repo.save(
            NotificationLog(
                notification_id=f"11111111-1111-4111-8111-{n:012d}",
                notification_type=notification_type,
                stock_code=_STOCK,
                content_hash=f"hash-{n:04d}",
                sent_at=sent_at,
                related_recommendation_id=None,
                holding_id=holding_id,
            )
        )
    return LineNotificationService(
        line_client=_FakeLineClient(),
        notification_log_repository=log_repo,
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )


def _other_type(recommendation_type: RecommendationType) -> RecommendationType:
    index = _ALL_TYPES.index(recommendation_type)
    return _ALL_TYPES[(index + 1) % len(_ALL_TYPES)]


def grid_statuses(tmp_root: Path) -> dict[str, str]:
    """全組み合わせの status(値)を返す。キーは『種類|範囲|前回|日数』。"""
    holding_id = build_holding_id(DEFAULT_OWNER, _STOCK)
    out: dict[str, str] = {}
    for scope in ("stock", "holding"):
        for days in _DAYS:
            store = tmp_root / f"{scope}-{days}"
            store.mkdir(parents=True, exist_ok=True)
            hid = holding_id if scope == "holding" else None
            service = make_service(store, days, holding_id=hid)
            for recommendation_type, previous_kind in product(_ALL_TYPES, _PREVIOUS_KINDS):
                if scope == "holding" and recommendation_type not in _SELL_SIGNAL_TYPES:
                    continue
                no_price = previous_kind == "same_no_price"
                recommendation = make_recommendation(
                    recommendation_type,
                    None if no_price else _BASE_PRICE * _RATIO.get(previous_kind, Decimal("1")),
                    recommendation_id="22222222-2222-4222-8222-222222222222",
                    holding_id=hid,
                )
                if previous_kind == "none":
                    previous = None
                else:
                    previous_type = (
                        _other_type(recommendation_type)
                        if previous_kind == "other_type"
                        else recommendation_type
                    )
                    previous = make_recommendation(
                        previous_type,
                        None if no_price else _BASE_PRICE,
                        recommendation_id="33333333-3333-4333-8333-333333333333",
                        holding_id=hid,
                    )
                status = service._notification_status_for_send(recommendation, previous, _NOW)
                out[f"{recommendation_type.value}|{scope}|{previous_kind}|{days}"] = status.value
    return out


def test_golden_covers_every_type_in_the_notification_mapping() -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    covered = {key.split("|")[0] for key in golden}
    assert covered == {t.value for t in _ALL_TYPES}


def test_status_for_every_type_scope_previous_and_elapsed_days_is_unchanged(tmp_path: Path) -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert grid_statuses(tmp_path) == golden
