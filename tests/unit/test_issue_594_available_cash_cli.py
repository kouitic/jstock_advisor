"""Issue #594(#128 A5c): CLIから買付余力の参照・棚卸し更新を行えるようにする。

show/reconcileの2コマンドを、既存のAvailableCashService(#589)を通じてのみ
操作する薄いCLI層として実装する。CLI層自体が独自の検証・CAS・正規化ロジックを
持たないことを、この層の観点(未登録/0円表示・上書き・負値拒否・owner分離)で
確認する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import available_cash as available_cash_cli
from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
    AvailableCashRepository,
)
from jstock_advisor.services.available_cash_service import AvailableCashService

runner = CliRunner()


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AvailableCashService:
    """available_cash CLIが生成するAvailableCashServiceをtmp storeへ向ける。"""
    svc = AvailableCashService(repository=AvailableCashRepository(store_dir=tmp_path))
    monkeypatch.setattr(available_cash_cli, "AvailableCashService", lambda: svc)
    return svc


def test_show_unregistered_owner_is_explicit(service: AvailableCashService) -> None:
    """未登録ownerは「未登録」と明示する(0円と混同しない)。"""
    result = runner.invoke(available_cash_cli.app, ["show", "--owner", "owner-a"])
    assert result.exit_code == 0
    assert "未登録です" in result.stdout


def test_reconcile_then_show_displays_amount_and_update_type(
    service: AvailableCashService,
) -> None:
    """棚卸し後は金額とUSER_RECONCILIATIONが表示される。"""
    reconcile_result = runner.invoke(
        available_cash_cli.app, ["reconcile", "--owner", "owner-a", "--amount", "150000"]
    )
    assert reconcile_result.exit_code == 0
    assert "150000" in reconcile_result.stdout

    show_result = runner.invoke(available_cash_cli.app, ["show", "--owner", "owner-a"])
    assert show_result.exit_code == 0
    assert "150000" in show_result.stdout
    assert "USER_RECONCILIATION" in show_result.stdout


def test_reconcile_zero_is_legal_and_distinct_from_unregistered(
    service: AvailableCashService,
) -> None:
    """0円は正当な棚卸し結果であり、未登録とは表示上も区別される。"""
    result = runner.invoke(
        available_cash_cli.app, ["reconcile", "--owner", "owner-a", "--amount", "0"]
    )
    assert result.exit_code == 0

    show_result = runner.invoke(available_cash_cli.app, ["show", "--owner", "owner-a"])
    assert "未登録です" not in show_result.stdout
    assert "0円" in show_result.stdout


def test_reconcile_negative_amount_is_rejected(service: AvailableCashService) -> None:
    """負値は拒否され、理由がユーザーへ表示され、レコードは作成されない。"""
    result = runner.invoke(
        available_cash_cli.app, ["reconcile", "--owner", "owner-a", "--amount", "-1"]
    )
    assert result.exit_code == 1
    assert "0以上である必要があります" in result.stdout

    show_result = runner.invoke(available_cash_cli.app, ["show", "--owner", "owner-a"])
    assert "未登録です" in show_result.stdout


def test_reconcile_overwrites_absolute_value_not_additive(
    service: AvailableCashService,
) -> None:
    """2回目のreconcileは加算ではなく絶対値上書きになる(#589と同じ契約)。"""
    runner.invoke(
        available_cash_cli.app, ["reconcile", "--owner", "owner-a", "--amount", "100000"]
    )
    result = runner.invoke(
        available_cash_cli.app, ["reconcile", "--owner", "owner-a", "--amount", "50000"]
    )
    assert result.exit_code == 0

    show_result = runner.invoke(available_cash_cli.app, ["show", "--owner", "owner-a"])
    assert "50000" in show_result.stdout
    assert "150000" not in show_result.stdout


def test_reconcile_is_isolated_per_owner(service: AvailableCashService) -> None:
    """owner間でavailable_cashが混同されない。"""
    runner.invoke(available_cash_cli.app, ["reconcile", "--owner", "owner-a", "--amount", "100000"])
    runner.invoke(available_cash_cli.app, ["reconcile", "--owner", "owner-b", "--amount", "20000"])

    result_a = runner.invoke(available_cash_cli.app, ["show", "--owner", "owner-a"])
    result_b = runner.invoke(available_cash_cli.app, ["show", "--owner", "owner-b"])
    assert "100000" in result_a.stdout
    assert "20000" in result_b.stdout


def test_reconcile_non_numeric_amount_is_rejected_by_cli(
    service: AvailableCashService,
) -> None:
    """数値変換できないamountはtyper.BadParameterとして拒否される。"""
    result = runner.invoke(
        available_cash_cli.app, ["reconcile", "--owner", "owner-a", "--amount", "abc"]
    )
    assert result.exit_code != 0


def test_show_invalid_owner_is_rejected(service: AvailableCashService) -> None:
    """区切り文字を含む等の不正なownerはInvalidOwnerErrorとして拒否される。

    exit_code==1だけでは、正しくcatchしたうえでの終了と、未処理例外による
    終了(CliRunnerはいずれもexit_code==1を返す)を区別できない。例外が
    catchされ、エラー理由がユーザーへ表示されたことを直接確認する。
    """
    result = runner.invoke(available_cash_cli.app, ["show", "--owner", "owner-a#不正"])
    assert result.exit_code == 1
    assert "区切り文字" in result.stdout


def test_show_displays_updated_at_and_last_reconciled_at(
    service: AvailableCashService,
) -> None:
    """最終更新(updated_at)・最終棚卸し(last_reconciled_at)が表示される。

    #584の契約(未棚卸しはNoneのまま保持し、表示上も区別する)を、CLIが
    利用者へ見せる最初の面として確認する。表示対象のtimestampはCLI自身の
    clock(`dt.datetime.now(dt.UTC)`)ではなく、テストがAvailableCashService.
    reconcile()へ直接渡した固定datetime由来である(CLIコマンド自体に
    clockを注入する変更は行っていない)。したがってdevelopment_workflow.md
    §3.5のT1〜T4(業務時刻の境界判定・provider契約・時刻由来の業務分岐・
    test/mock/fixtureのclock置換)のいずれにも該当しない
    (T1〜T4はいずれも本番コードの時刻判定ロジックを対象とするが、本テストは
    テストデータの構築方法であり、CLI・serviceいずれの時刻判定ロジックも
    変更していない)。
    """
    fixed_now = dt.datetime(2026, 9, 20, 3, 0, tzinfo=dt.UTC)
    service.reconcile("owner-a", Decimal("300000"), fixed_now)

    result = runner.invoke(available_cash_cli.app, ["show", "--owner", "owner-a"])
    assert result.exit_code == 0
    # USER_RECONCILIATIONはupdated_at/last_reconciled_atの両方をnowへ揃える
    # ため(#589の契約)、正しく両方表示されていれば同じISO文字列が2回現れる。
    assert result.stdout.count(fixed_now.isoformat()) == 2
    assert "未棚卸し" not in result.stdout
