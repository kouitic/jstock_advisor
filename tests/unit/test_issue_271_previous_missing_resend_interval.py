"""Issue #271(横断監査 N-03): 参照先Recommendationが消えても再送間隔を守る。

```
問題  `_notification_status_for_send()` は
        latest_log is None -> SENT   （真の「未送信」。ここで処理済み）
        previous is None   -> SENT   ★ ここが本 Issue
      と並んでいた。

      ★ 2 行目に到達した時点で latest_log は**必ず存在する** = 過去に送信済みである。
        したがって previous is None は「未送信」ではなく「**比較できない**」であり、
        意味しうるのは次の 2 つだけ。
          (1) latest_log.related_recommendation_id が None（参照IDを持たない古いログ）
          (2) 参照先の Recommendation が**消えている**（purge / #63 の隔離等）

      -> 修正前は resend_after_days を待たずに送っていた = **重複送信**。
```

```
方式 O-1（管理者の第一候補）
  early return を外し、previous を必要とする比較（種別一致 / 価格変化 /
  決算待ち状態）だけを skip して**日数判定へ進む**。

★ 日数判定は `latest_log.sent_at` だけで計算でき、previous を 1 つも参照しない。
  だから「previous が無いから判定できない」という事実は存在しない。
★ 挙動が変わる向きは**抑止側**（過剰通知が減る）。新しく送るケースは 1 つも増えない。
```

```
★ 値はすべて架空値。銘柄コードは実在しない "0000" 系を使う。
  所有者名・保有数量・取得単価は 1 つも含まない。
★ Production への注入は行わない（tmp_path のストアへ書くだけ）。
```
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    ExecutionMode,
    NotificationStatus,
    NotificationType,
    RecommendationType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.notification import NotificationLog
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
from jstock_advisor.services.line_notification_service import LineNotificationService

_CONFIG = load_config()
#: config/notification_rules.yaml の実値。ここを変えると本テストの境界も動く。
_RESEND_AFTER_DAYS = _CONFIG.notification.resend_after_days

_STOCK = "0000"
_NOW = dt.datetime(2026, 9, 9, 8, 0, tzinfo=dt.UTC)  # = JST 2026-09-09 17:00
_LOG_ID = "11111111-1111-4111-8111-111111111111"
_PRESENT_REC_ID = "22222222-2222-4222-8222-222222222222"
_MISSING_REC_ID = "33333333-3333-4333-8333-333333333333"


class _FakeLineClient(LineClient):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


def _service(
    store_dir: Path, execution_context: ExecutionContext | None = None
) -> LineNotificationService:
    kwargs = {}
    if execution_context is not None:
        kwargs["execution_context"] = execution_context
    return LineNotificationService(
        line_client=_FakeLineClient(),
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
        **kwargs,
    )


def _sell_recommendation(
    recommendation_id: str, price: Decimal | None = None
) -> Recommendation:
    """stock-scope（holding_id なし）の SELL。SELL_SIGNAL へ対応する。

    ★ `price` を渡すと `_representative_price()` が拾える形で設定する。
      SELL（= 全部売却系ではない通常の売却）の代表価格は
      `immediate_execution_price` が最優先である（実測）。
      渡さない場合は代表価格を持たない = 価格比較ができない状態になる。
    """
    sell_prices = SellPriceLevels()
    if price is not None:
        sell_prices = SellPriceLevels(
            immediate_execution_price=PriceWithRationale(price=price, rationale="架空の根拠")
        )
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK,
        stock_name="銘柄 X",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        sell_prices=sell_prices,
        price_at_recommendation=price if price is not None else Decimal("1000"),
        reasons=["架空の理由"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def _seed_log(store_dir: Path, sent_at: dt.datetime, related_recommendation_id: str | None) -> None:
    """過去に 1 回送信した実績を置く。"""
    NotificationLogRepository(store_dir=store_dir).save(
        NotificationLog(
            notification_id=_LOG_ID,
            notification_type=NotificationType.SELL_SIGNAL,
            stock_code=_STOCK,
            content_hash="hash-0001",
            sent_at=sent_at,
            related_recommendation_id=related_recommendation_id,
        )
    )


def _days_ago(days: int) -> dt.datetime:
    return _NOW - dt.timedelta(days=days)


# =============================================================================
# A) 本 Issue の目的 — 参照先が消えていても再送間隔を守る
# =============================================================================


def test_t1_previous_missing_and_interval_not_reached_is_suppressed(tmp_path: Path) -> None:
    """★ T-1: 参照先が消えていて、かつ**日数が未経過**なら抑止されること。

    **修正前はここで送信されていた**（resend_after_days を待たずに再送 = 重複送信）。
    本 Issue の回帰の本体である。
    """
    _seed_log(tmp_path, _days_ago(1), _MISSING_REC_ID)  # 参照先は保存しない = 消えている
    service = _service(tmp_path)
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    previous = service._previous_recommendation(recommendation, NotificationType.SELL_SIGNAL)
    assert previous is None  # 前提: 参照先が引けない

    status = service._notification_status_for_send(recommendation, previous, _NOW)

    assert status is NotificationStatus.RESEND_INTERVAL_NOT_REACHED


def test_t2_previous_missing_and_interval_reached_is_sent(tmp_path: Path) -> None:
    """T-2: 参照先が消えていても、**日数が経過していれば送る**こと。

    ★ 抑止しっぱなしにしない（通知の欠落が固定化する O-2 は採らない）。
    """
    _seed_log(tmp_path, _days_ago(_RESEND_AFTER_DAYS), _MISSING_REC_ID)
    service = _service(tmp_path)
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    status = service._notification_status_for_send(recommendation, None, _NOW)

    assert status is NotificationStatus.SENT


def test_t2b_missing_related_id_is_treated_the_same(tmp_path: Path) -> None:
    """★ previous が None になるもう 1 つの経路（参照IDを持たない古いログ）。

    参照先が消えた場合と**同じ扱い**であること（片方だけ直すと片方が残る）。
    """
    _seed_log(tmp_path, _days_ago(1), None)
    service = _service(tmp_path)
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    previous = service._previous_recommendation(recommendation, NotificationType.SELL_SIGNAL)
    assert previous is None

    status = service._notification_status_for_send(recommendation, previous, _NOW)

    assert status is NotificationStatus.RESEND_INTERVAL_NOT_REACHED


def test_t3_no_log_at_all_is_still_sent(tmp_path: Path) -> None:
    """T-3: **真の未送信**（ログが 1 件も無い）は従来どおり送ること。

    ★ 「参照先が消えた」と「一度も送っていない」を**別状態として区別する**
      （受入条件 2）。ここを混ぜると初回通知が出なくなる。
    """
    service = _service(tmp_path)
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    status = service._notification_status_for_send(recommendation, None, _NOW)

    assert status is NotificationStatus.SENT


# =============================================================================
# B) 境界 — resend_after_days ちょうど / 1 日手前
# =============================================================================


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (_RESEND_AFTER_DAYS - 1, NotificationStatus.RESEND_INTERVAL_NOT_REACHED),
        (_RESEND_AFTER_DAYS, NotificationStatus.SENT),
        (_RESEND_AFTER_DAYS + 1, NotificationStatus.SENT),
    ],
    ids=["one_day_before", "exactly_on_the_threshold", "one_day_after"],
)
def test_t4_boundary_of_resend_after_days(
    tmp_path: Path, days: int, expected: NotificationStatus
) -> None:
    """境界: `days_elapsed >= resend_after_days` のちょうど / 手前 / 後。

    ★ 参照先が消えている状態で、**日数判定そのものが効いている**ことの確認。
    """
    _seed_log(tmp_path, _days_ago(days), _MISSING_REC_ID)
    service = _service(tmp_path)
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    status = service._notification_status_for_send(recommendation, None, _NOW)

    assert status is expected


def test_t4b_utc_day_boundary_is_counted_in_jst(tmp_path: Path) -> None:
    """★ 経過日数は **JST 暦日**で数えること（Issue #23 の既存契約が不変であること）。

    UTC 暦日で数えると、同一 JST 日の中で UTC 日付境界（JST 09:00）を跨いだだけで
    1 日経過と誤判定する。参照先が消えた経路でも同じ数え方であることを固定する。

        sent_at 2026-09-08T23:30Z = JST 09-09 08:30
        now     2026-09-09T00:30Z = JST 09-09 09:30
        -> UTC 暦日では 1 日、**JST 暦日では 0 日**。抑止側であること。
    """
    _seed_log(tmp_path, dt.datetime(2026, 9, 8, 23, 30, tzinfo=dt.UTC), _MISSING_REC_ID)
    service = _service(tmp_path)
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    status = service._notification_status_for_send(
        recommendation, None, dt.datetime(2026, 9, 9, 0, 30, tzinfo=dt.UTC)
    )

    assert status is NotificationStatus.RESEND_INTERVAL_NOT_REACHED


# =============================================================================
# C) previous がある場合は 1 つも変わらない（回帰）
# =============================================================================


def test_t5_previous_present_type_change_is_sent(tmp_path: Path) -> None:
    """回帰: 推奨種別が変わったら日数に関係なく送る。"""
    _seed_log(tmp_path, _days_ago(1), _PRESENT_REC_ID)
    service = _service(tmp_path)
    previous = _sell_recommendation(_PRESENT_REC_ID)
    previous = previous.model_copy(update={"recommendation_type": RecommendationType.URGENT_REVIEW})
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    status = service._notification_status_for_send(recommendation, previous, _NOW)

    assert status is NotificationStatus.SENT


def test_t5b_previous_present_price_move_is_sent(tmp_path: Path) -> None:
    """回帰: 価格が閾値以上動いたら日数に関係なく送る。"""
    _seed_log(tmp_path, _days_ago(1), _PRESENT_REC_ID)
    service = _service(tmp_path)
    threshold = _CONFIG.notification.price_change_resend_threshold_pct
    previous = _sell_recommendation(_PRESENT_REC_ID, price=Decimal("1000"))
    moved = Decimal("1000") * (1 + Decimal(str(threshold)) / 100 + Decimal("0.01"))
    recommendation = _sell_recommendation(
        "44444444-4444-4444-8444-444444444444", price=moved
    )

    status = service._notification_status_for_send(recommendation, previous, _NOW)

    assert status is NotificationStatus.SENT


def test_t5c_previous_present_small_move_is_suppressed_with_price_reason(
    tmp_path: Path,
) -> None:
    """★ 回帰: previous があるときの**抑止理由**が変わらないこと。

    価格を比較できた場合は PRICE_CHANGE_BELOW_THRESHOLD であり、
    本 Issue で追加した RESEND_INTERVAL_NOT_REACHED の分岐へ**落ちない**。
    """
    _seed_log(tmp_path, _days_ago(1), _PRESENT_REC_ID)
    service = _service(tmp_path)
    previous = _sell_recommendation(_PRESENT_REC_ID, price=Decimal("1000"))
    recommendation = _sell_recommendation(
        "44444444-4444-4444-8444-444444444444", price=Decimal("1001")
    )

    status = service._notification_status_for_send(recommendation, previous, _NOW)

    assert status is NotificationStatus.PRICE_CHANGE_BELOW_THRESHOLD


def test_t5d_previous_present_interval_reached_is_sent(tmp_path: Path) -> None:
    """回帰: previous があり日数が経過していれば従来どおり送る。"""
    _seed_log(tmp_path, _days_ago(_RESEND_AFTER_DAYS), _PRESENT_REC_ID)
    service = _service(tmp_path)
    previous = _sell_recommendation(_PRESENT_REC_ID, price=Decimal("1000"))
    recommendation = _sell_recommendation(
        "44444444-4444-4444-8444-444444444444", price=Decimal("1001")
    )

    status = service._notification_status_for_send(recommendation, previous, _NOW)

    assert status is NotificationStatus.SENT


# =============================================================================
# D) 他の抑止機構と衝突しないこと
# =============================================================================


def test_t6_validation_still_bypasses_resend_suppression(tmp_path: Path) -> None:
    """T-5(計画): VALIDATION では従来どおり再送防止をバイパスすること。

    ★ 本 Issue の分岐は VALIDATION の早期 return より**後ろ**にあるため、
      検証モードの挙動は 1 つも変わらない。
    """
    _seed_log(tmp_path, _days_ago(1), _MISSING_REC_ID)
    service = _service(tmp_path, ExecutionContext(mode=ExecutionMode.VALIDATION))
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    status = service._notification_status_for_send(recommendation, None, _NOW)

    assert status is NotificationStatus.SENT


def test_t7_undecidable_still_wins_over_the_new_branch(tmp_path: Path) -> None:
    """★ T-7: #279 の抑止（判定不能）が本変更で緩んでいないこと。

    読めなかった場合は `previous is None` へ到達する**前に**
    DATA_INSUFFICIENT で返る。順序が入れ替わると、
    「読めなかったのに日数だけで送る」という #279 の欠陥が復活する。
    """
    store = tmp_path / "notification_log.json"
    store.write_text(
        json.dumps(
            [
                {
                    "notification_id": _LOG_ID,
                    "notification_type": NotificationType.SELL_SIGNAL.value,
                    "stock_code": _STOCK,
                    "content_hash": "hash-0001",
                    "sent_at": "not-a-timestamp",  # decode できない架空レコード
                    "related_recommendation_id": _MISSING_REC_ID,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = _service(tmp_path)
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    status = service._notification_status_for_send(recommendation, None, _NOW)

    assert status is NotificationStatus.DATA_INSUFFICIENT


# =============================================================================
# E) 実経路（check_resend_eligibility）でも直っていること
# =============================================================================


def test_t8_end_to_end_through_check_resend_eligibility(tmp_path: Path) -> None:
    """★ 実際の呼び出し経路で確認する。

    `_previous_recommendation()` を fake で置き換えず、**本物の repository を引いて
    参照先が見つからない状態**を作る。ここが通らなければ本 Issue は直っていない。
    """
    _seed_log(tmp_path, _days_ago(1), _MISSING_REC_ID)
    service = _service(tmp_path)
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    eligibility = service.check_resend_eligibility(recommendation, _NOW)

    assert eligibility.eligible is False
    assert eligibility.block_reason == NotificationStatus.RESEND_INTERVAL_NOT_REACHED.value


def test_t8b_end_to_end_sends_when_the_interval_has_passed(tmp_path: Path) -> None:
    """逆側: 参照先が消えていても日数が経てば eligible になること。"""
    _seed_log(tmp_path, _days_ago(_RESEND_AFTER_DAYS), _MISSING_REC_ID)
    service = _service(tmp_path)
    recommendation = _sell_recommendation("44444444-4444-4444-8444-444444444444")

    assert service.check_resend_eligibility(recommendation, _NOW).eligible is True


# =============================================================================
# F) negative check — 修正が戻ったら落ちること
# =============================================================================


def test_t9_the_early_return_is_not_reintroduced() -> None:
    """★ `previous is None -> SENT` の early return が復活していないこと。

    T-1 が壊れれば落ちるが、実装が別の形で同じ穴を開けた場合
    （例: 分岐を上へ移す）に気づけるよう、**ソース上でも**固定する。
    """
    import inspect

    source = inspect.getsource(LineNotificationService._notification_status_for_send)

    assert "if previous is None:\n            return NotificationStatus.SENT" not in source, (
        "previous is None を『送信してよい』へ倒す early return が復活している(#271)"
    )
    # 日数判定が previous を参照しないままであること（O-1 の前提）
    assert "evaluation_date_jst(latest_log.sent_at)" in source
