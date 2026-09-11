"""Issue #71 F-C13: WatchState の無条件 upsert による更新喪失を固定する。

## なぜこのテストが要るか

`evaluate_and_update()` は `get_active()` で読んだ `existing` を土台に
`model_copy(update={...})` して書き戻すが、その update 辞書に
`ended_at` / `end_reason` が**含まれていない**。したがって、読んでから書くまでの
間に別実行(売買検知による `end_for_trade_events()`)が終了させていても、
「終了していない状態」がそのまま復活していた。

★ 競合の窓は毎営業日 08:00 に開いている。評価側は buy-candidates、終了側は
holdings-watchlist で、両者の EventBridge Schedule の cron が**完全に同一**
(`cron(0 8 ? * MON-FRI *)`) であるため。

## このテストが固定すること

    (i)   終了が先・評価が後 -> ★ 終端が**維持される**(復活しない)
    (ii)  評価が先・終了が後 -> 終了が成立する
    (iii) 競合が無い通常経路 -> 従来どおり動く(挙動を変えていない)
    (iv)  ★ consecutive_business_days が失われない
          (この値は BUY 到達通知の本文「N 日監視後」へ到達する)

★ (i) は**是正前のコードでは必ず失敗する**。復活してしまうためである。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.models import NearBuyConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    TransactionType,
    WatchTransitionType,
    WatchType,
)
from jstock_advisor.domain.entities.watch_state import WatchState, build_watch_id
from jstock_advisor.domain.signals.trade_event_detection import TradeEvent
from jstock_advisor.infrastructure.local_repository.watch_state_repository import (
    WatchStateRepository,
)
from jstock_advisor.services.watch_state_service import (
    END_REASON_TRADE_EVENT,
    WatchStateService,
)

# 架空の銘柄コード(実在の保有銘柄を使わない)。
_STOCK = "0000"
_TODAY = dt.date(2026, 9, 11)  # 金曜(営業日)
_YESTERDAY = dt.date(2026, 9, 10)  # 木曜(営業日)


def _config() -> NearBuyConfig:
    return NearBuyConfig(
        start_required_decline_pct=10.0,
        continue_required_decline_pct=15.0,
        min_company_quality_score=0.0,
        daily_max_notifications=10,
        max_stale_business_days=5,
    )


def _repo(tmp_path: Path) -> WatchStateRepository:
    return WatchStateRepository(store_dir=tmp_path)


def _service(tmp_path: Path, repo: WatchStateRepository) -> WatchStateService:
    return WatchStateService(
        business_calendar=BusinessCalendar(
            extra_closure_mm_dd=frozenset(), additional_closure_dates=frozenset()
        ),
        repository=repo,
    )


def _seed_active(repo: WatchStateRepository, *, consecutive: int = 4) -> WatchState:
    """監視中(未終了)の WatchState を 1 件置く。"""
    state = WatchState(
        watch_id=build_watch_id(_STOCK, WatchType.NEAR_BUY),
        stock_code=_STOCK,
        watch_type=WatchType.NEAR_BUY,
        started_at=dt.date(2026, 9, 4),
        last_matched_at=_YESTERDAY,
        last_evaluated_at=_YESTERDAY,
        consecutive_business_days=consecutive,
        last_current_price=Decimal("1000"),
        last_entry_price=Decimal("900"),
        best_distance_pct=Decimal("0.11"),
    )
    repo.upsert(state)
    return state


def _evaluate_continue(service: WatchStateService) -> object:
    """継続条件を満たす評価(CONTINUED になる入力)。"""
    return service.evaluate_and_update(
        stock_code=_STOCK,
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=80.0,
        required_decline_to_entry_pct=Decimal("12"),
        current_price=Decimal("1010"),
        entry_price=Decimal("900"),
        today=_TODAY,
        config=_config(),
    )


class _EndingDuringReadRepository(WatchStateRepository):
    """`get_active_with_raw()` の直後に、別実行が終了させた状況を作る。

    ★ 実運用での競合(評価側が読んだ後、終了側が書く)を決定的に再現するための
    テスト用リポジトリ。読み取り自体は本物を使い、**読んだ直後に**終了を
    差し込む。以降は素の振る舞いに戻す。
    """

    def __init__(self, store_dir: Path, today: dt.date) -> None:
        super().__init__(store_dir=store_dir)
        self._today = today
        self._injected = False

    def get_active_with_raw(
        self, stock_code: str, watch_type: WatchType
    ) -> tuple[WatchState, str] | None:
        fetched = super().get_active_with_raw(stock_code, watch_type)
        if fetched is not None and not self._injected:
            self._injected = True
            state, _ = fetched
            # 別実行(end_for_trade_events)が終了させた、という状況を作る。
            super().upsert(
                state.model_copy(
                    update={
                        "ended_at": self._today,
                        "end_reason": END_REASON_TRADE_EVENT,
                        "last_evaluated_at": self._today,
                    }
                )
            )
        return fetched


# --- (i) ★ 終了が先・評価が後 -> 終端が維持される(本 Issue の核心) ------------


def test_terminated_watch_is_not_revived_by_a_concurrent_evaluation(tmp_path: Path) -> None:
    """★ 是正前はここで**復活**していた。終端が維持されることを固定する。"""
    repo = _EndingDuringReadRepository(tmp_path, _TODAY)
    _seed_active(repo)
    service = _service(tmp_path, repo)

    _evaluate_continue(service)

    stored = repo.get_with_raw(build_watch_id(_STOCK, WatchType.NEAR_BUY))
    assert stored is not None
    state, _ = stored
    assert state.ended_at == _TODAY, "★ 終了が評価によって巻き戻ってはならない"
    assert state.end_reason == END_REASON_TRADE_EVENT, "★ 終了理由も維持されること"


def test_evaluation_reports_no_transition_when_the_watch_was_ended_concurrently(
    tmp_path: Path,
) -> None:
    """★ 復活させない代わりに、その評価は「遷移なし」として扱う。

    CONTINUED を返すと「まだ監視中」という誤った情報が通知層へ渡るため。
    """
    repo = _EndingDuringReadRepository(tmp_path, _TODAY)
    _seed_active(repo)
    service = _service(tmp_path, repo)

    result = _evaluate_continue(service)

    assert result.transition_type == WatchTransitionType.NONE
    assert result.watch_type is None


def test_consecutive_business_days_is_not_resurrected(tmp_path: Path) -> None:
    """★ (iv) 復活すると、売買前の連続日数がそのまま引き継がれてしまう。

    この値は BUY 到達通知の本文「N 日監視後」へ到達するため、
    終了した監視の日数が生き残ってはならない。
    """
    repo = _EndingDuringReadRepository(tmp_path, _TODAY)
    _seed_active(repo, consecutive=4)
    service = _service(tmp_path, repo)

    _evaluate_continue(service)

    stored = repo.get_with_raw(build_watch_id(_STOCK, WatchType.NEAR_BUY))
    assert stored is not None
    state, _ = stored
    # 終了済みのまま = この監視はもう続かない。日数が 5 へ進んでいないこと。
    assert state.ended_at is not None
    assert state.consecutive_business_days == 4, "★ 終了後に日数が進んではならない"


# --- (ii) 評価が先・終了が後 -> 終了が成立する --------------------------------


def test_end_after_evaluation_still_terminates(tmp_path: Path) -> None:
    """順序が逆(評価が先)なら、終了は従来どおり成立する。"""
    repo = _repo(tmp_path)
    _seed_active(repo)
    service = _service(tmp_path, repo)

    result = _evaluate_continue(service)
    assert result.transition_type == WatchTransitionType.CONTINUED

    active = repo.get_active(_STOCK, WatchType.NEAR_BUY)
    assert active is not None
    service.end_for_trade_events(
        events=[_trade_event()],
        today=_TODAY,
    )

    stored = repo.get_with_raw(build_watch_id(_STOCK, WatchType.NEAR_BUY))
    assert stored is not None
    state, _ = stored
    assert state.ended_at == _TODAY
    assert state.end_reason == END_REASON_TRADE_EVENT


def _trade_event() -> TradeEvent:
    """架空の売買イベント(★ 実在の所有者・数量は使わない)。"""
    return TradeEvent(
        holding_id="owner-a#0000",
        owner="owner-a",
        stock_code=_STOCK,
        event_type=TransactionType.BUY,
        detected_at=_TODAY,
        shares=100,
        average_purchase_price=Decimal("900"),
    )


# --- (iii) 競合が無い通常経路は従来どおり ------------------------------------


def test_normal_path_without_conflict_is_unchanged(tmp_path: Path) -> None:
    """★ 競合が無ければ、CAS 化しても結果は 1 つも変わらない。"""
    repo = _repo(tmp_path)
    _seed_active(repo, consecutive=4)
    service = _service(tmp_path, repo)

    result = _evaluate_continue(service)

    assert result.transition_type == WatchTransitionType.CONTINUED
    assert result.consecutive_business_days == 5, "営業日が 1 日進んだので +1"
    assert result.previous_consecutive_business_days == 4

    stored = repo.get_active(_STOCK, WatchType.NEAR_BUY)
    assert stored is not None
    assert stored.ended_at is None
    assert stored.consecutive_business_days == 5
    assert stored.last_evaluated_at == _TODAY


def test_end_is_idempotent_when_already_ended(tmp_path: Path) -> None:
    """★ 既に終了している状態へ終了を重ねても、結果が変わらないこと。

    終了は単調であり、CAS が失敗しても「既に終了済み」なら目的は達成されている。
    """
    repo = _repo(tmp_path)
    _seed_active(repo)
    service = _service(tmp_path, repo)

    service.end_for_trade_events(events=[_trade_event()], today=_TODAY)
    first = repo.get_with_raw(build_watch_id(_STOCK, WatchType.NEAR_BUY))
    assert first is not None

    service.end_for_trade_events(events=[_trade_event()], today=_TODAY)
    second = repo.get_with_raw(build_watch_id(_STOCK, WatchType.NEAR_BUY))
    assert second is not None
    assert second[0].ended_at == first[0].ended_at
    assert second[0].end_reason == first[0].end_reason
