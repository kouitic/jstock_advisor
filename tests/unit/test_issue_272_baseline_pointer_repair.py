"""Issue #272: baseline pointer 不整合の検出と修復 CLI。

なぜこの経路が危ないか:
  pointer と baseline 履歴の不整合は `get_active_baseline()` が integrity_error を
  返し、保有判断がその時点で打ち切られる。通常経路で pointer を作る
  `activate_baseline()` は integrity_error の early return より後にあるため、
  **何度実行しても自力では復旧しない**(永久停止)。

各テストは「検出されたこと」だけでなく **整合している保有を誤検出しないこと**、
**dry-run が書き込まないこと**、**自動で baseline を選ばないこと** も確認する
(#254 の観点: 否定形の assert だけで完結させない)。

fixture は架空値のみ。銘柄コードは JPX に割り当てのない "0000" 系を使い、
実在の上場コード・所有者名・保有数量は使用しない。store_dir は tmp_path で隔離する
(TEST_STORE_ISOLATION)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.cli.baseline_repair import RepairReason, _detect, _resolve_baseline
from jstock_advisor.domain.entities.enums import (
    AccountType,
    BaselineOrigin,
    BaselineStatus,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    BaselineValueSnapshot,
    InvestmentThesisBaseline,
)
from jstock_advisor.domain.entities.owner import build_holding_id, log_ref
from jstock_advisor.infrastructure.aws.baseline_pointer import create_pointer, get_pointer
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.investment_thesis_baseline_repository import (
    InvestmentThesisBaselineRepository,
)

_OWNER = "owner-a"
_NOW = dt.datetime(2026, 9, 9, 6, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def _holding(store_dir: Path, stock_code: str) -> Holding:
    holding = Holding(
        owner=_OWNER,
        holding_id=build_holding_id(_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2025, 1, 1),
        last_purchase_date=dt.date(2025, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )
    HoldingRepository(store_dir).upsert(holding)
    return holding


def _baseline(store_dir: Path, holding_id: str, version: int) -> InvestmentThesisBaseline:
    baseline = InvestmentThesisBaseline(
        baseline_id=f"bl-{holding_id}-{version}",
        holding_id=holding_id,
        stock_code=holding_id.split("#")[-1],
        version=version,
        status=BaselineStatus.APPROVED,
        origin=BaselineOrigin.HOLDING_REGISTRATION_SNAPSHOT,
        baseline_values=BaselineValueSnapshot(),
        created_at=_NOW,
    )
    InvestmentThesisBaselineRepository(store_dir).save(baseline)
    return baseline


def _detect_all(store_dir: Path):
    return _detect(
        HoldingRepository(store_dir),
        InvestmentThesisBaselineRepository(store_dir),
        store_dir,
    )


# --- 検出 ---------------------------------------------------------------------


def test_a_pointer_missing_with_history_is_detected(tmp_path: Path) -> None:
    """(A) pointer が無く履歴だけがある保有が、理由区分つきで検出される。"""
    holding = _holding(tmp_path, "0001")
    _baseline(tmp_path, holding.holding_id, 1)

    targets = _detect_all(tmp_path)

    assert len(targets) == 1
    assert targets[0].reason is RepairReason.POINTER_MISSING
    assert targets[0].holding_id == holding.holding_id
    assert len(targets[0].history) == 1


def test_b_version_mismatch_is_detected(tmp_path: Path) -> None:
    """(B) pointer の version が baseline と食い違う保有が検出される。"""
    holding = _holding(tmp_path, "0002")
    _baseline(tmp_path, holding.holding_id, 1)
    updated = _baseline(tmp_path, holding.holding_id, 2)
    # version 2 の baseline を、version 1 として指す pointer を作る
    create_pointer(holding.holding_id, updated.baseline_id, 1, store_dir=tmp_path)

    targets = _detect_all(tmp_path)

    assert len(targets) == 1
    assert targets[0].reason is RepairReason.VERSION_MISMATCH
    assert targets[0].pointed_version == 2
    assert targets[0].current_pointer_version is not None


def test_b_baseline_not_found_is_detected(tmp_path: Path) -> None:
    """(B) pointer が指す baseline が存在しない保有が検出される。"""
    holding = _holding(tmp_path, "0003")
    _baseline(tmp_path, holding.holding_id, 1)
    create_pointer(holding.holding_id, "bl-does-not-exist", 1, store_dir=tmp_path)

    targets = _detect_all(tmp_path)

    assert len(targets) == 1
    assert targets[0].reason is RepairReason.BASELINE_NOT_FOUND
    assert targets[0].pointed_version is None


# --- 誤検出しないこと ---------------------------------------------------------


def test_consistent_holding_is_not_detected(tmp_path: Path) -> None:
    """pointer と baseline が整合している保有は 1 件も現れない。"""
    holding = _holding(tmp_path, "0004")
    baseline = _baseline(tmp_path, holding.holding_id, 1)
    create_pointer(holding.holding_id, baseline.baseline_id, baseline.version, store_dir=tmp_path)

    assert _detect_all(tmp_path) == []


def test_first_time_holding_without_history_is_not_detected(tmp_path: Path) -> None:
    """pointer も履歴も無い保有(初回)は正常であり、integrity_error ではない。"""
    _holding(tmp_path, "0005")

    assert _detect_all(tmp_path) == []


# --- dry-run が書き込まないこと -----------------------------------------------


def test_detection_writes_nothing(tmp_path: Path) -> None:
    """★ 検出(scan の実体)は pointer を 1 件も作らない。"""
    holding = _holding(tmp_path, "0006")
    _baseline(tmp_path, holding.holding_id, 1)

    _detect_all(tmp_path)

    # 検出しただけで pointer が生えていないこと(肯定形で状態を述べる)
    assert get_pointer(holding.holding_id, tmp_path) is None
    # 2 回目も同じ対象が残っている = 直っていない
    assert len(_detect_all(tmp_path)) == 1


# --- 修復と冪等性 -------------------------------------------------------------


def test_a_repair_restores_pointer_and_clears_integrity_error(tmp_path: Path) -> None:
    """(A) を修復すると pointer が復元され、対象から外れる(判定が再開する)。"""
    holding = _holding(tmp_path, "0007")
    baseline = _baseline(tmp_path, holding.holding_id, 1)

    target = _detect_all(tmp_path)[0]
    chosen = _resolve_baseline(target, None)
    assert chosen is not None
    create_pointer(holding.holding_id, chosen.baseline_id, chosen.version, store_dir=tmp_path)

    pointer = get_pointer(holding.holding_id, tmp_path)
    assert pointer is not None
    assert pointer.active_baseline_id == baseline.baseline_id
    assert pointer.active_baseline_version == baseline.version
    # 冪等: 修復後は対象 0 件
    assert _detect_all(tmp_path) == []


def test_repair_is_idempotent(tmp_path: Path) -> None:
    """修復後にもう一度検出しても対象は 0 件のまま(2 重に作らない)。"""
    holding = _holding(tmp_path, "0008")
    baseline = _baseline(tmp_path, holding.holding_id, 1)
    create_pointer(holding.holding_id, baseline.baseline_id, baseline.version, store_dir=tmp_path)

    assert _detect_all(tmp_path) == []
    assert _detect_all(tmp_path) == []


# --- baseline の選び方 --------------------------------------------------------


def test_b_never_selects_a_baseline_automatically(tmp_path: Path) -> None:
    """★ (B) は --baseline-id が無ければ候補を決めない(自動で最新を選ばない)。

    pointer は意図的に古い baseline を指している場合があり、自動で上書きすると
    判定基準が黙って入れ替わる。
    """
    holding = _holding(tmp_path, "0009")
    _baseline(tmp_path, holding.holding_id, 1)
    newer = _baseline(tmp_path, holding.holding_id, 2)
    create_pointer(holding.holding_id, newer.baseline_id, 1, store_dir=tmp_path)

    target = _detect_all(tmp_path)[0]

    assert target.requires_explicit_baseline is True
    assert _resolve_baseline(target, None) is None


def test_a_default_candidate_is_the_latest_version(tmp_path: Path) -> None:
    """(A) の既定候補は履歴の最新 version。"""
    holding = _holding(tmp_path, "0010")
    _baseline(tmp_path, holding.holding_id, 1)
    _baseline(tmp_path, holding.holding_id, 3)
    _baseline(tmp_path, holding.holding_id, 2)

    target = _detect_all(tmp_path)[0]
    chosen = _resolve_baseline(target, None)

    assert chosen is not None
    assert chosen.version == 3


def test_explicit_baseline_version_overrides_the_default(tmp_path: Path) -> None:
    """--baseline-version を指定すると、その version が採用される。"""
    holding = _holding(tmp_path, "0011")
    _baseline(tmp_path, holding.holding_id, 1)
    _baseline(tmp_path, holding.holding_id, 2)

    target = _detect_all(tmp_path)[0]
    chosen = _resolve_baseline(target, 1)

    assert chosen is not None
    assert chosen.version == 1


def test_unknown_baseline_version_is_rejected(tmp_path: Path) -> None:
    """履歴に無い version を指定しても採用しない。"""
    holding = _holding(tmp_path, "0012")
    _baseline(tmp_path, holding.holding_id, 1)

    target = _detect_all(tmp_path)[0]

    assert _resolve_baseline(target, 99) is None


# --- 出力に PII を含めないこと -------------------------------------------------


def test_holding_ref_does_not_expose_owner_or_stock_code(tmp_path: Path) -> None:
    """★ 表示に使う holding_ref から所有者名・銘柄コードが復元できないこと。"""
    holding = _holding(tmp_path, "0013")
    _baseline(tmp_path, holding.holding_id, 1)

    ref = log_ref(_detect_all(tmp_path)[0].holding_id)

    assert ref.startswith("sha256:")
    assert _OWNER not in ref
    assert "0013" not in ref


# --- holding_ref の衝突 -------------------------------------------------------


def test_apply_refuses_when_holding_ref_is_not_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ holding_ref が複数の保有に一致したら、どれも書き換えずに中断する。

    holding_ref は sha256 の先頭 8 文字であり、理論上は衝突しうる。1 件目を
    黙って採用すると **別の保有の pointer を書き換える**ことになる。
    """
    from typer.testing import CliRunner

    from jstock_advisor.cli import baseline_repair

    first = _holding(tmp_path, "0014")
    second = _holding(tmp_path, "0015")
    _baseline(tmp_path, first.holding_id, 1)
    _baseline(tmp_path, second.holding_id, 1)

    # 2 つの保有が同じ holding_ref を返す状況を作る(衝突の模擬)
    monkeypatch.setattr(baseline_repair, "log_ref", lambda _value: "sha256:collide")

    result = CliRunner().invoke(
        baseline_repair.app,
        ["apply", "--holding-ref", "sha256:collide", "--store-dir", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "一意ではありません" in result.stdout
    # ★ どちらの pointer も作られていないこと(肯定形で状態を述べる)
    assert get_pointer(first.holding_id, tmp_path) is None
    assert get_pointer(second.holding_id, tmp_path) is None


# --- 出力に baseline_id を含めないこと ----------------------------------------


def test_scan_output_does_not_contain_baseline_id(tmp_path: Path) -> None:
    """★ baseline_id は `<所有者>#<銘柄コード>:v<version>` であり PII を含む。

    scan の出力に現れてはならない(version だけで holding 内は一意に特定できる)。
    """
    from typer.testing import CliRunner

    from jstock_advisor.cli import baseline_repair

    holding = _holding(tmp_path, "0016")
    baseline = _baseline(tmp_path, holding.holding_id, 1)

    result = CliRunner().invoke(baseline_repair.app, ["scan", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "version=1" in result.stdout
    assert baseline.baseline_id not in result.stdout
    assert _OWNER not in result.stdout
    assert "0016" not in result.stdout
