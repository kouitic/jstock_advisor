"""Issue #273(横断監査 N-05): 通知種別が切り替わっても急変検知が働くこと。

```
問題  急変検知（data_quality_service.detect_anomalies）の比較相手 `previous` を、
      **再送防止と同じ scope**（= 通知種別で絞った直近ログ）から引いていた。

      -> 通知種別が切り替わった直後は新種別のログが無く previous is None となり、
         ★ **急変検知が丸ごと skip** される（検知の欠落）。
```

```
方式 O-2（管理者の第一候補）+ 管理者判断 A-1 / B-1
  ・急変検知には**種別を問わない**直近を使う（A-1: detect_anomalies へ渡す
    previous をまるごと差し替える。signature は変更しない）
  ・比較相手は **NotificationLog 系**から引く（B-1。S-03 には触れない）
  ・**再送防止の `previous`（種別つき）は変更しない**（O-2 の要）

★ 急変検知は推奨種別を 1 つも参照していない（実測）。種別で絞っていたのは
  再送防止の都合であり、急変検知がその scope を借りているのが構造上の誤りだった。
```

```
★ 値はすべて架空値。銘柄コードは実在しない "0000"。
  所有者名・保有数量・取得単価は 1 つも含まない。
★ Production への注入は行わない（tmp_path のストアへ書くだけ）。
```
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    NotificationType,
    RecommendationType,
)
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
from jstock_advisor.services.line_notification_service import (
    _RECOMMENDATION_TO_NOTIFICATION_TYPE,
    LineNotificationService,
)

_CONFIG = load_config()
#: 適正価格の急変とみなす閾値（config/data_validation 由来の実値）。
_FV_THRESHOLD = _CONFIG.data_validation.anomaly_detection.fair_value_change_threshold_pct

_STOCK = "0000"
_NOW = dt.datetime(2026, 9, 9, 8, 0, tzinfo=dt.UTC)

_PREV_REC_ID = "11111111-1111-4111-8111-111111111111"
_CURRENT_REC_ID = "22222222-2222-4222-8222-222222222222"
_LOG_ID = "33333333-3333-4333-8333-333333333333"


class _FakeLineClient(LineClient):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


def _service(store_dir: Path) -> LineNotificationService:
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


def _recommendation(
    recommendation_id: str,
    recommendation_type: RecommendationType,
    fair_value: Decimal,
    recommended_at: dt.datetime = _NOW,
) -> Recommendation:
    """架空の推奨。`fair_value_at_recommendation` だけが急変検知の対象になる。"""
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK,
        stock_name="銘柄 X",
        recommended_at=recommended_at,
        recommendation_type=recommendation_type,
        sell_prices=SellPriceLevels(
            immediate_execution_price=PriceWithRationale(
                price=Decimal("1000"), rationale="架空の根拠"
            )
        ),
        price_at_recommendation=Decimal("1000"),
        fair_value_at_recommendation=fair_value,
        reasons=["架空の理由"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def _seed_previous(
    store_dir: Path,
    logged_notification_type: NotificationType,
    fair_value: Decimal,
    previous_recommendation_type: RecommendationType = RecommendationType.WATCH,
) -> Recommendation:
    """「前回の分析とその通知履歴」を置く。

    ★ `logged_notification_type` が**今回の種別と違う**とき、
      修正前は比較相手が見つからず急変検知が skip されていた。

    ★ `previous_recommendation_type` は**通知種別とは別の軸**である。
      再送防止は「前回と同じ推奨種別か」も見るため、
      再送側を確かめるテストでは今回と揃える必要がある
      （揃えないと「種別が変わったから送ってよい」で先に抜けてしまい、
        再送間隔の判定まで到達しない）。
    """
    previous = _recommendation(
        _PREV_REC_ID,
        previous_recommendation_type,
        fair_value,
        recommended_at=_NOW - dt.timedelta(days=1),
    )
    RecommendationRepository(store_dir=store_dir).save(previous)
    NotificationLogRepository(store_dir=store_dir).save(
        NotificationLog(
            notification_id=_LOG_ID,
            notification_type=logged_notification_type,
            stock_code=_STOCK,
            content_hash="hash-0001",
            sent_at=_NOW - dt.timedelta(days=1),
            related_recommendation_id=_PREV_REC_ID,
        )
    )
    return previous


def _sharply_moved_fair_value(base: Decimal) -> Decimal:
    """閾値を確実に超える適正価格（安全側に 1 割上乗せする）。"""
    return base * (1 + Decimal(str(_FV_THRESHOLD)) / 100 + Decimal("0.10"))


# =============================================================================
# A) 本 Issue の目的 — 種別が切り替わっても急変を検知する
# =============================================================================


def test_t1_anomaly_is_detected_after_a_notification_type_switch(tmp_path: Path) -> None:
    """★ T-1: 通知種別が切り替わった直後でも、適正価格の急変を **BLOCKING** すること。

    **修正前はここで検知されなかった**（新種別のログが無く previous is None）。
    本 Issue の回帰の本体である。
    """
    base = Decimal("1000")
    # 前回は PROFIT_TAKING_SIGNAL として通知した（= WATCH 系）
    _seed_previous(tmp_path, NotificationType.PROFIT_TAKING_SIGNAL, base)
    service = _service(tmp_path)
    # 今回は SELL（= SELL_SIGNAL）。**種別が切り替わった**
    current = _recommendation(
        _CURRENT_REC_ID, RecommendationType.SELL, _sharply_moved_fair_value(base)
    )

    eligibility = service.check_data_quality_eligibility(current, _NOW)

    assert eligibility.eligible is False
    assert eligibility.block_reason == "DATA_QUALITY_BLOCKED"


def test_t2_same_type_still_detects_the_anomaly(tmp_path: Path) -> None:
    """T-2: 種別が同じ場合の挙動が **1 つも変わらない**こと（従来から検知できていた）。"""
    base = Decimal("1000")
    _seed_previous(tmp_path, NotificationType.SELL_SIGNAL, base)
    service = _service(tmp_path)
    current = _recommendation(
        _CURRENT_REC_ID, RecommendationType.SELL, _sharply_moved_fair_value(base)
    )

    assert service.check_data_quality_eligibility(current, _NOW).eligible is False


def test_t2b_small_move_is_not_blocked_after_a_switch(tmp_path: Path) -> None:
    """★ 逆側: 種別が切り替わっても、**動いていなければ BLOCKING しない**こと。

    「切替だけで塞ぐ」ようになっていないことの確認（過剰 BLOCKING の防止）。
    """
    base = Decimal("1000")
    _seed_previous(tmp_path, NotificationType.PROFIT_TAKING_SIGNAL, base)
    service = _service(tmp_path)
    current = _recommendation(_CURRENT_REC_ID, RecommendationType.SELL, base)

    assert service.check_data_quality_eligibility(current, _NOW).eligible is True


def test_t3_no_previous_at_all_is_still_skipped(tmp_path: Path) -> None:
    """T-4(計画): **真の初回**（履歴が 1 件も無い）は従来どおり急変検知を skip すること。

    ★ 「種別が切り替わった」と「一度も分析していない」を**別状態として扱う**。
      ここを混ぜると初回分析が必ず通らなくなる。
    """
    service = _service(tmp_path)
    current = _recommendation(
        _CURRENT_REC_ID, RecommendationType.SELL, _sharply_moved_fair_value(Decimal("1000"))
    )

    assert service.check_data_quality_eligibility(current, _NOW).eligible is True


# =============================================================================
# B) 比較相手の選び方（決定事項 B-1 / C）
# =============================================================================


def test_t4_previous_lookup_crosses_notification_types(tmp_path: Path) -> None:
    """種別を問わない lookup が、**別種別のログから**前回の推奨を引けること。"""
    _seed_previous(tmp_path, NotificationType.PROFIT_TAKING_SIGNAL, Decimal("1000"))
    service = _service(tmp_path)
    current = _recommendation(_CURRENT_REC_ID, RecommendationType.SELL, Decimal("1000"))

    previous = service._previous_recommendation_any_type(current)

    assert previous is not None
    assert previous.recommendation_id == _PREV_REC_ID


def test_t5_previous_is_never_the_current_recommendation(tmp_path: Path) -> None:
    """★ 決定事項 C: **自分自身を比較相手にしない**こと。

    B-2（RecommendationRepository を直接引く）を採ると、保存が品質チェックより
    先に走るため current 自身が返り、変化率が必ず 0 になって
    **直したのに何も検知しない**という結果になる。
    B-1 は「通知履歴を経由する」ため構造的に起きないが、
    **見込みに頼らず固定する**（現に current を保存した状態で確認する）。
    """
    _seed_previous(tmp_path, NotificationType.PROFIT_TAKING_SIGNAL, Decimal("1000"))
    current = _recommendation(
        _CURRENT_REC_ID, RecommendationType.SELL, _sharply_moved_fair_value(Decimal("1000"))
    )
    # ★ 実運用と同じ順序: 保存してから品質チェックを行う
    RecommendationRepository(store_dir=tmp_path).save(current)
    service = _service(tmp_path)

    previous = service._previous_recommendation_any_type(current)

    assert previous is not None
    assert previous.recommendation_id != current.recommendation_id
    assert previous.recommendation_id == _PREV_REC_ID


def test_t5b_the_type_scoped_lookup_still_misses_it(tmp_path: Path) -> None:
    """★ 「種別つきでは引けない」ことを固定する（本 Issue の前提そのもの）。

    これが成り立たなくなったら fixture が「種別の切替」を表さなくなったということで、
    T-1 は**何も守らなくなる**。
    """
    _seed_previous(tmp_path, NotificationType.PROFIT_TAKING_SIGNAL, Decimal("1000"))
    service = _service(tmp_path)
    current = _recommendation(_CURRENT_REC_ID, RecommendationType.SELL, Decimal("1000"))

    assert service._previous_recommendation(current, NotificationType.SELL_SIGNAL) is None


# =============================================================================
# C) 再送防止に影響しないこと（O-2 の要）
# =============================================================================


def test_t6_resend_judgement_is_untouched_by_the_type_switch(tmp_path: Path) -> None:
    """★ T-3(計画): **再送防止の判定が 1 つも変わらない**こと。

    再送防止は「同じ種別の通知を短期間に繰り返さない」ための判定であり、
    種別で絞ることに意味がある。ここまで横断化すると
    「種別が変わったのに前回送ったばかりとして抑止される」= **通知の欠落**になる。
    O-2 はそれを避けるために 2 つの比較相手を分けている。
    """
    _seed_previous(tmp_path, NotificationType.PROFIT_TAKING_SIGNAL, Decimal("1000"))
    service = _service(tmp_path)
    current = _recommendation(_CURRENT_REC_ID, RecommendationType.SELL, Decimal("1000"))

    # 新種別（SELL_SIGNAL）の送信実績は無いため、再送防止は通す（= 送ってよい）
    assert service.check_resend_eligibility(current, _NOW).eligible is True


def test_t6b_resend_is_still_suppressed_within_the_same_type(tmp_path: Path) -> None:
    """逆側: 同じ種別なら従来どおり再送が抑止されること（横断化していない証拠）。"""
    _seed_previous(
        tmp_path,
        NotificationType.SELL_SIGNAL,
        Decimal("1000"),
        previous_recommendation_type=RecommendationType.SELL,
    )
    service = _service(tmp_path)
    current = _recommendation(_CURRENT_REC_ID, RecommendationType.SELL, Decimal("1000"))

    assert service.check_resend_eligibility(current, _NOW).eligible is False


# =============================================================================
# D) repository の追加メソッド
# =============================================================================


def test_t7_repository_lookup_ignores_notification_type(tmp_path: Path) -> None:
    """`list_by_stock()` が種別を問わず引くこと。"""
    _seed_previous(tmp_path, NotificationType.PROFIT_TAKING_SIGNAL, Decimal("1000"))
    repo = NotificationLogRepository(store_dir=tmp_path)

    lookup = repo.list_by_stock(_STOCK)

    assert lookup.latest is not None
    assert lookup.latest.notification_id == _LOG_ID
    assert lookup.undecidable is False
    # 種別つきでは（別種別なので）引けない
    assert repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL).latest is None


def test_t7b_repository_holding_lookup_keeps_the_scope(tmp_path: Path) -> None:
    """★ 種別は問わないが **scope（holding_id）は保つ**こと。

    scope まで広げると別 owner の通知が比較相手になり、#33 が直した
    scope 非対称が復活する。
    """
    repo = NotificationLogRepository(store_dir=tmp_path)
    repo.save(
        NotificationLog(
            notification_id=_LOG_ID,
            notification_type=NotificationType.PROFIT_TAKING_SIGNAL,
            stock_code=_STOCK,
            content_hash="hash-0001",
            sent_at=_NOW - dt.timedelta(days=1),
            related_recommendation_id=_PREV_REC_ID,
            holding_id="所有者A#0000",
        )
    )

    assert repo.list_by_holding("所有者A#0000").latest is not None
    assert repo.list_by_holding("所有者B#0000").latest is None


# =============================================================================
# E) negative check — 修正が戻ったら落ちること
# =============================================================================


def test_t8_data_quality_uses_the_any_type_lookup(tmp_path: Path) -> None:
    """★ データ品質チェックが**種別横断の比較相手**を使っていることをソースで固定する。

    T-1 が壊れれば落ちるが、実装が別の形で同じ穴を開けた場合
    （例: 呼び出しを種別つきへ戻す）に気づけるよう、ソース上でも固定する。
    """
    import inspect

    for method in (
        LineNotificationService.evaluate_notification_status,
        LineNotificationService.check_data_quality_eligibility,
    ):
        source = inspect.getsource(method)
        assert "_check_data_quality(" in source
        assert "previous_any_type" in source, (
            f"{method.__name__}: データ品質チェックが種別横断の比較相手を使っていない(#273)"
        )


def test_t8b_resend_path_still_uses_the_type_scoped_lookup() -> None:
    """★ 逆側: 再送防止が**種別つき**のままであることをソースで固定する（O-2 の要）。"""
    import inspect

    source = inspect.getsource(LineNotificationService.evaluate_notification_status)
    assert "previous = self._previous_recommendation(recommendation, notification_type)" in source
    assert "_notification_status_for_send(recommendation, previous, now)" in source


@pytest.mark.parametrize(
    ("current_type", "logged_type"),
    [
        (RecommendationType.SELL, NotificationType.PROFIT_TAKING_SIGNAL),
        (RecommendationType.WATCH, NotificationType.SELL_SIGNAL),
    ],
    ids=["watch_to_sell", "sell_to_watch"],
)
def test_t9_switch_direction_does_not_matter(
    tmp_path: Path,
    current_type: RecommendationType,
    logged_type: NotificationType,
) -> None:
    """切替の**向き**に依らず比較相手が見つかること（片方向だけ直っていないことの確認）。

    ★ ここで確かめるのは **#273 が変えた機構そのもの**（種別を問わず前回を引けること）
      である。データ品質の最終判定（BLOCKING か否か）で確かめないのは、
      推奨種別ごとに**別の整合性チェック**が先に発火しうるためで、
      それだと「本 Issue の修正で通ったのか、別の理由で塞がれたのか」を
      区別できない（実測でその状態を確認したため、観測点をここへ寄せた）。
      最終判定は T-1 / T-2b が clean な向きで固定している。
    """
    _seed_previous(tmp_path, logged_type, Decimal("1000"))
    service = _service(tmp_path)
    current = _recommendation(
        _CURRENT_REC_ID, current_type, _sharply_moved_fair_value(Decimal("1000"))
    )

    previous = service._previous_recommendation_any_type(current)

    assert previous is not None
    assert previous.recommendation_id == _PREV_REC_ID
    # 種別つき（従来の経路）では引けない = 本当に「切替直後」の状態である
    notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[current.recommendation_type]
    assert service._previous_recommendation(current, notification_type) is None
