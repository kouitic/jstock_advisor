"""Issue #590(#128 A3-LINE): 通常売買登録(BUY/SELL)とAvailable Cash更新を
整合した1更新単位として扱う。`AvailableCashService.build_trade_update_plan()`の
semanticsのみを固定する(実際のTransactWriteItems原子性はtest_conversation_
commit.py側で検証する)。

CLI経路(#619)はscope外。余力不足時の拒否ロジックの具体的な契約
(専用例外の型・境界値・SELL側の非対象化)はIssue #591固有のため
test_issue_591_insufficient_cash_guard.pyで固定する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import AvailableCashUpdateType
from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
    AvailableCashRepository,
)
from jstock_advisor.services.available_cash_service import (
    AvailableCashNotRegisteredError,
    AvailableCashService,
)

_NOW = dt.datetime(2026, 9, 26, 9, 0, tzinfo=dt.UTC)
_LATER = dt.datetime(2026, 9, 26, 10, 0, tzinfo=dt.UTC)


def _seed(
    service: AvailableCashService, owner: str, amount: str, reconciled_at: dt.datetime
) -> None:
    service.reconcile(owner, Decimal(amount), reconciled_at)


# --- BUY(delta負値)・SELL(delta正値)の基本契約 -------------------------------


def test_buy_debit_subtracts_delta_from_current_balance(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000000", _NOW)

    plan = service.build_trade_update_plan("owner-a", Decimal("-150000"), _LATER)

    assert plan.model.available_cash == Decimal("850000")


def test_sell_credit_adds_delta_to_current_balance(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000000", _NOW)

    plan = service.build_trade_update_plan("owner-a", Decimal("150000"), _LATER)

    assert plan.model.available_cash == Decimal("1150000")


def test_trade_update_sets_trade_update_contract_fields(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000000", _NOW)

    plan = service.build_trade_update_plan("owner-a", Decimal("-150000"), _LATER)

    assert plan.model.last_update_type == AvailableCashUpdateType.TRADE_UPDATE
    assert plan.model.updated_at == _LATER


def test_trade_update_does_not_advance_last_reconciled_at(tmp_path) -> None:
    """#584 entity docstringの契約(TRADE_UPDATEでlast_reconciled_atを進めない)
    をここで初めて強制する(#590本文どおり)。"""
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000000", _NOW)

    plan = service.build_trade_update_plan("owner-a", Decimal("-150000"), _LATER)

    assert plan.model.last_reconciled_at == _NOW  # _LATERではない


def test_build_trade_update_plan_does_not_persist_anything(tmp_path) -> None:
    """plan構築のみで一切永続化しない(既存のbuild_reconcile_plan()と同じ規約)。"""
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000000", _NOW)
    repo = AvailableCashRepository(store_dir=tmp_path)

    service.build_trade_update_plan("owner-a", Decimal("-150000"), _LATER)

    assert repo.get("owner-a").available_cash == Decimal("1000000")  # 変化なし


def test_expected_data_reflects_raw_value_read_at_plan_build_time(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000000", _NOW)
    repo = AvailableCashRepository(store_dir=tmp_path)

    plan = service.build_trade_update_plan("owner-a", Decimal("-150000"), _LATER)

    assert plan.expected_data == repo.get_raw("owner-a")


class _SingleReadRepository:
    """delta基準値とCASのexpected_dataを同一の読み取りから導出することを
    固定するためのfake(サブちゃんレビュー#620 F1対応)。

    `get()`が一切呼ばれないこと(二重読み取りの片方を無くしたこと)・
    `get_raw()`がちょうど1回だけ呼ばれることを検証する。`get_raw()`の
    呼び出し中に別経路(#589の棚卸し等)の並行更新を割り込ませることで、
    delta計算とCASのexpected_dataが常に同じ(最新の)値を基準にすることを
    確認する(以前の実装は`get()`→`get_raw()`の2回読みだったため、この間に
    割り込まれると、CASは新しい値に対して成立するにも関わらずdelta計算は
    古い値のまま行われ、更新が黙って失われていた)。
    """

    def __init__(self, inner: AvailableCashRepository, concurrent_update: object) -> None:
        self._inner = inner
        self._concurrent_update = concurrent_update
        self.get_calls = 0
        self.get_raw_calls = 0

    def get(self, owner: str):  # noqa: ANN001, ANN201 - 呼ばれないことのみ検証するfake
        self.get_calls += 1
        return self._inner.get(owner)

    def get_raw(self, owner: str) -> str | None:
        self.get_raw_calls += 1
        if self.get_raw_calls == 1 and self._concurrent_update is not None:
            self._concurrent_update()
        return self._inner.get_raw(owner)

    def initialize(self, record) -> bool:  # noqa: ANN001, ANN201 - fake、未使用
        return self._inner.initialize(record)

    def replace_if_raw_matches(self, owner: str, expected_raw_data: str, record) -> bool:  # noqa: ANN001
        return self._inner.replace_if_raw_matches(owner, expected_raw_data, record)


def test_build_trade_update_plan_reads_the_current_balance_only_once(tmp_path) -> None:
    """delta基準値の取得に`get()`を呼ばない(`get_raw()`1回のみに統合済み)
    ことを固定する。"""
    inner = AvailableCashRepository(store_dir=tmp_path)
    service_for_seed = AvailableCashService(repository=inner)
    _seed(service_for_seed, "owner-a", "1000000", _NOW)
    spy = _SingleReadRepository(inner, concurrent_update=None)
    service = AvailableCashService(repository=spy)

    service.build_trade_update_plan("owner-a", Decimal("-150000"), _LATER)

    assert spy.get_calls == 0
    assert spy.get_raw_calls == 1


def test_concurrent_update_between_reads_no_longer_silently_lost(tmp_path) -> None:
    """サブちゃんレビュー#620 F1の実測プローブと同型の回帰テスト。

    旧実装(get()とget_raw()を別々に読む)では、初期100万円に対し並行して
    別経路が200万円へ棚卸しした場合、delta計算は割り込み前の100万円を
    基準にしてしまい、CASのexpected_dataだけが新しい値になるため
    (CAS自体は成立してしまう)、最終的に古い基準額+deltaという誤った値
    (このケースでは85万円、正しくは185万円)で上書きされていた。

    現在の実装は`get_raw()`1回の読み取り結果からdelta基準値・
    expected_dataの両方を導出するため、この割り込みが起きても常に
    最新値(200万円)を基準にdeltaが計算され、CASのexpected_dataも同じ
    値になる(=書き込み時点でさらに変化していない限りCASは正しく成立し、
    値も正しい)。
    """
    inner = AvailableCashRepository(store_dir=tmp_path)
    service_for_seed = AvailableCashService(repository=inner)
    _seed(service_for_seed, "owner-a", "1000000", _NOW)

    def _concurrent_reconcile() -> None:
        service_for_seed.reconcile("owner-a", Decimal("2000000"), _NOW)

    spy = _SingleReadRepository(inner, concurrent_update=_concurrent_reconcile)
    service = AvailableCashService(repository=spy)

    plan = service.build_trade_update_plan("owner-a", Decimal("-150000"), _LATER)

    # 修正前は850000(100万-15万、古い基準額)になっていた。
    assert plan.model.available_cash == Decimal("1850000")
    # expected_dataも同じ(最新の)読み取りに基づいており、基準値との
    # 不整合が無い。
    assert plan.expected_data == inner.get_raw("owner-a")


# --- D3: 未登録ownerは明示エラーで拒否(自動0円初期化しない) -------------------


def test_unregistered_owner_raises_explicit_error_instead_of_auto_initializing(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)

    with pytest.raises(AvailableCashNotRegisteredError):
        service.build_trade_update_plan("owner-unregistered", Decimal("-150000"), _NOW)

    repo = AvailableCashRepository(store_dir=tmp_path)
    assert repo.get("owner-unregistered") is None  # 自動0円初期化されていない


def test_unregistered_owner_error_does_not_leak_raw_owner_value(tmp_path) -> None:
    """Issue #135と同じ方針: 例外messageに生owner値を含めない。"""
    service = AvailableCashService(store_dir=tmp_path)

    with pytest.raises(AvailableCashNotRegisteredError) as exc_info:
        service.build_trade_update_plan("owner-secret-name", Decimal("-1"), _NOW)

    assert "owner-secret-name" not in str(exc_info.value)


# --- 残高不足はentity側validatorがplan構築フェーズ(I/O前)で拒否する -------------


def test_insufficient_balance_rejected_before_any_write(tmp_path) -> None:
    """余力不足はplan構築時点(I/O前)で拒否され、何も書き込まれない。

    具体的な例外の型(InsufficientAvailableCashError)・境界値・SELL側の
    非対象化はIssue #591固有の契約のため、
    test_issue_591_insufficient_cash_guard.pyで固定する(本テストは
    #590の「plan構築フェーズで拒否され書き込みが発生しない」という
    atomicity契約のみを確認する)。"""
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000", _NOW)
    repo = AvailableCashRepository(store_dir=tmp_path)

    with pytest.raises(ValueError):
        service.build_trade_update_plan("owner-a", Decimal("-1001"), _LATER)

    assert repo.get("owner-a").available_cash == Decimal("1000")  # 変化なし


def test_balance_reaching_exactly_zero_is_legal(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000", _NOW)

    plan = service.build_trade_update_plan("owner-a", Decimal("-1000"), _LATER)

    assert plan.model.available_cash == Decimal("0")


# --- owner正規化(既存normalize_and_validate_owner()を再利用、新規ロジックなし) ---


def test_owner_normalization_matches_reconcile_and_get(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000000", _NOW)  # 半角で登録

    plan = service.build_trade_update_plan("owner-ａ", Decimal("-150000"), _LATER)  # 全角の"a"

    assert plan.model.owner == "owner-a"


def test_trading_owner_a_does_not_affect_owner_b(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "1000000", _NOW)
    _seed(service, "owner-b", "2000000", _NOW)

    service.build_trade_update_plan("owner-a", Decimal("-150000"), _LATER)

    repo = AvailableCashRepository(store_dir=tmp_path)
    assert repo.get("owner-b").available_cash == Decimal("2000000")  # 変化なし(planは未適用)
