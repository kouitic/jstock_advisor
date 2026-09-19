"""Issue #413 PR-4: watch_state_service の INFO 出力。

module は module 直下で `logger.setLevel(logging.INFO)` を宣言した。ここでは次を確認する。

    1 宣言が実際に効く: Lambda の root logger の既定(WARNING)のもとでも、INFO が有効になる。
    2 INFO が実際に出力され、識別子(watch_id = 銘柄コード:監視種別)と終了理由を出す。
    3 出力に、出してはならない値(架空の owner / holding_id / 保有数量 / 取得単価 / 価格・距離)を
      含めない。**これらの値は、試験対象が実際に読むデータへ入れて確認する**
      (PR #444 のレビュー指摘 F1。データに値が無ければ「現れない」は常に真で、何も保証しない)。
    4 module が売買イベント(TradeEvent)の owner / holding_id / shares / average_purchase_price を
      参照しないことを、構造(AST)で固定する。

★ 設計時の訂正: `end_for_trade_events()` は `TradeEvent`(owner・holding_id・保有数量・取得単価を
持つ)を受け取る。ただし module が読むのは `stock_code` だけである(4 が固定する)。

宣言があること自体は tests/unit/test_issue_413_logger_level_declared.py(PR #436 の guard)が見る。
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.models import NearBuyConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import BuyAction, TransactionType, WatchType
from jstock_advisor.domain.entities.watch_state import WatchState, build_watch_id
from jstock_advisor.domain.signals.trade_event_detection import TradeEvent
from jstock_advisor.infrastructure.local_repository.watch_state_repository import (
    WatchStateRepository,
)
from jstock_advisor.services import watch_state_service
from jstock_advisor.services.watch_state_service import (
    END_REASON_TRADE_EVENT,
    WatchStateService,
)

_MODULE = watch_state_service.__name__

#: 架空の銘柄コード(実在の保有銘柄を使わない)。watch_id の一部として出力されるのは契約どおり。
_STOCK = "0000"
_TODAY = dt.date(2026, 9, 11)  # 金曜(営業日)
_YESTERDAY = dt.date(2026, 9, 10)  # 木曜(営業日)

#: 実在しない架空値。試験対象が読むデータへ入れ、出力に現れたら、出してはならない値を出している。
_OWNER = "owner-a"
_HOLDING_ID = "owner-a:holding-1"
_SHARES = 987654
_AVERAGE_PRICE = "4321.987"
#: 状態の中身(価格・距離)。他の値と衝突しない、末尾が 0 でない数値にする。
_SEED_CURRENT_PRICE = "7654.321"
_SEED_ENTRY_PRICE = "6543.219"
_SEED_BEST_DISTANCE = "0.4321"
_INPUT_CURRENT_PRICE = "8765.432"
_INPUT_ENTRY_PRICE = "5432.109"
_INPUT_REQUIRED_DECLINE = "12.3456"

_FORBIDDEN_VALUES = [
    _OWNER,
    _HOLDING_ID,
    str(_SHARES),
    _AVERAGE_PRICE,
    _SEED_CURRENT_PRICE,
    _SEED_ENTRY_PRICE,
    _SEED_BEST_DISTANCE,
    _INPUT_CURRENT_PRICE,
    _INPUT_ENTRY_PRICE,
    _INPUT_REQUIRED_DECLINE,
]


def _assert_no_forbidden_value(caplog: pytest.LogCaptureFixture) -> list[str]:
    """対象 module が出した全レベルの記録に、出してはならない値が無いことを確認する。"""
    messages = [r.getMessage() for r in caplog.records if r.name == _MODULE]
    for message in messages:
        for value in _FORBIDDEN_VALUES:
            assert value not in message, (value, message)
    return messages


def test_the_leak_check_fails_when_a_record_contains_a_forbidden_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """検査そのものの確認: 出してはならない値を含む記録があれば、検査は赤になる。"""
    for value in _FORBIDDEN_VALUES:
        caplog.clear()
        logging.getLogger(_MODULE).info("x %s", value)

        with pytest.raises(AssertionError):
            _assert_no_forbidden_value(caplog)


def test_info_is_enabled_even_when_the_root_logger_is_at_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lambda の root 既定(WARNING)に左右されず、INFO が有効になる(宣言が効いている)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger(_MODULE).isEnabledFor(logging.INFO)
    assert not logging.getLogger(_MODULE).isEnabledFor(logging.DEBUG)


def _config() -> NearBuyConfig:
    return NearBuyConfig(
        start_required_decline_pct=10.0,
        continue_required_decline_pct=15.0,
        min_company_quality_score=0.0,
        daily_max_notifications=10,
        max_stale_business_days=5,
    )


def _service(repo: WatchStateRepository) -> WatchStateService:
    return WatchStateService(
        business_calendar=BusinessCalendar(
            extra_closure_mm_dd=frozenset(), additional_closure_dates=frozenset()
        ),
        repository=repo,
    )


def _seed_active(repo: WatchStateRepository) -> None:
    repo.upsert(
        WatchState(
            watch_id=build_watch_id(_STOCK, WatchType.NEAR_BUY),
            stock_code=_STOCK,
            watch_type=WatchType.NEAR_BUY,
            started_at=dt.date(2026, 9, 4),
            last_matched_at=_YESTERDAY,
            last_evaluated_at=_YESTERDAY,
            consecutive_business_days=4,
            last_current_price=Decimal(_SEED_CURRENT_PRICE),
            last_entry_price=Decimal(_SEED_ENTRY_PRICE),
            best_distance_pct=Decimal(_SEED_BEST_DISTANCE),
        )
    )
    stored = repo.get_active(_STOCK, WatchType.NEAR_BUY)
    assert stored is not None
    # 架空値が、試験対象が読むデータへ実際に入っている(入っていなければ以下の検査は何も保証しない)
    assert str(stored.last_current_price) == _SEED_CURRENT_PRICE
    assert str(stored.last_entry_price) == _SEED_ENTRY_PRICE
    assert str(stored.best_distance_pct) == _SEED_BEST_DISTANCE


def _evaluate(service: WatchStateService) -> object:
    return service.evaluate_and_update(
        stock_code=_STOCK,
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=80.0,
        required_decline_to_entry_pct=Decimal(_INPUT_REQUIRED_DECLINE),
        current_price=Decimal(_INPUT_CURRENT_PRICE),
        entry_price=Decimal(_INPUT_ENTRY_PRICE),
        today=_TODAY,
        config=_config(),
    )


class _ConcurrentWriteRepository(WatchStateRepository):
    """`get_active_with_raw()` の直後に、別実行の書き込みを差し込む(競合を決定的に再現する)。

    `end_watch=True` なら別実行が終了させた場合(INFO の経路)、False なら終了以外の更新を
    行った場合(WARNING の経路)。読み取り自体は本物を使う。
    """

    def __init__(self, store_dir: Path, *, end_watch: bool) -> None:
        super().__init__(store_dir=store_dir)
        self._end_watch = end_watch
        self._injected = False

    def get_active_with_raw(
        self, stock_code: str, watch_type: WatchType
    ) -> tuple[WatchState, str] | None:
        fetched = super().get_active_with_raw(stock_code, watch_type)
        if fetched is not None and not self._injected:
            self._injected = True
            state, _ = fetched
            update: dict[str, object] = (
                {
                    "ended_at": _TODAY,
                    "end_reason": END_REASON_TRADE_EVENT,
                    "last_evaluated_at": _TODAY,
                }
                if self._end_watch
                else {"consecutive_business_days": 5}
            )
            super().upsert(state.model_copy(update=update))
        return fetched


def test_concurrent_end_info_names_only_the_watch_id_and_the_end_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    repo = _ConcurrentWriteRepository(tmp_path, end_watch=True)
    _seed_active(repo)

    _evaluate(_service(repo))

    messages = _assert_no_forbidden_value(caplog)
    assert messages == [
        "watch state was ended concurrently; skipping evaluation write "
        f"watch_id={_STOCK}:NEAR_BUY end_reason={END_REASON_TRADE_EVENT}"
    ]


def test_concurrent_update_warning_names_only_the_watch_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    repo = _ConcurrentWriteRepository(tmp_path, end_watch=False)
    _seed_active(repo)

    _evaluate(_service(repo))

    messages = _assert_no_forbidden_value(caplog)
    assert messages == [
        f"watch state changed during evaluation; skipping write watch_id={_STOCK}:NEAR_BUY"
    ]


class _NeverAppliedRepository(WatchStateRepository):
    """終了の書き込みが必ず競合に負ける(再試行しても適用できない)状況を作る。"""

    def replace_if_raw_matches(self, expected_raw_data: str, new_state: WatchState) -> bool:
        return False


def test_trade_event_end_warning_does_not_emit_the_trade_event_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`end_for_trade_events()` へ、架空の owner / holding_id / 数量 / 取得単価を入れて通す。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    repo = _NeverAppliedRepository(store_dir=tmp_path)
    _seed_active(repo)
    event = TradeEvent(
        holding_id=_HOLDING_ID,
        owner=_OWNER,
        stock_code=_STOCK,
        event_type=TransactionType.BUY,
        detected_at=_TODAY,
        shares=_SHARES,
        average_purchase_price=Decimal(_AVERAGE_PRICE),
    )

    _service(repo).end_for_trade_events([event], _TODAY)

    messages = _assert_no_forbidden_value(caplog)
    # 経路が実際に通った(空の検査ではない): 適用できなかった旨の記録が、監視の種別ごとに出る
    assert messages
    assert all("could not be applied after retry" in m for m in messages)
    assert all(f"watch_id={_STOCK}:NEAR_BUY" in m for m in messages)


_TRADE_EVENT_PII_FIELDS = {"owner", "holding_id", "shares", "average_purchase_price"}


def test_the_module_never_reads_the_pii_bearing_fields_of_a_trade_event() -> None:
    """構造の歯止め: module が `TradeEvent` から読むのは `stock_code` だけである。

    `owner` / `holding_id` / `shares` / `average_purchase_price` を属性として参照する変更が入ると、
    赤になる。参照する必要が生じたら、その値を架空値でデータへ入れた漏出テストを同時に足すこと。
    """
    tree = ast.parse(Path(watch_state_service.__file__).read_text(encoding="utf-8"))

    referenced = sorted(
        {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in _TRADE_EVENT_PII_FIELDS
        }
    )

    assert referenced == [], referenced
