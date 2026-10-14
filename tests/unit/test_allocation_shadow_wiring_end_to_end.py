"""購入側 Shadow の配線 `build_shadow_runner` の結合テスト(Issue #603 PR-F2-2b)。

実際の計算(F2-2a)・記録(F2-1)・観測のループ・CLI の画面モデルをつなぎ、fake にするのは
『入力の読取元(reader)』『鍵の provider』『AuditLog(in-memory)』だけ。固定するもの:

  * 実際の計算で作った記録が allowlist を通り、索引・項目つきの記録・枠の記録として保存され、
    CLI が『完全な日』として読み戻せる(書込側と読み手が噛み合う)
  * 係数が読めない日・保有の読取の失敗の日は、理由つきの日単位の skip(索引・owner 単位の記録は 0 件)
  * 記録に owner の平文・保有 ID・銘柄名・保有の銘柄が出ない
  * 出荷の設定(YAML)から実際に ComputeConfig を組み立てられる(AWS・repository に触れない)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.identifier_ref import KeyUnavailable, KeyUnavailableReason
from jstock_advisor.domain.notification.allocation_shadow_cli_renderer import (
    render_json,
    render_table,
)
from jstock_advisor.domain.notification.allocation_shadow_cli_view import DayState
from jstock_advisor.domain.signals.allocation_shadow_record import (
    day_skip_audit_id,
    index_audit_id,
    items_audit_id,
    validate_index_record,
    validate_items_record,
)
from jstock_advisor.lambda_handlers import allocation_shadow_wiring as wiring
from jstock_advisor.services.allocation_shadow_service import Deadline, ShadowRunContext
from tests.unit import test_allocation_shadow_compute_contract as ct
from tests.unit import test_allocation_shadow_runner_contract as rt


def _runner(
    tmp_path: Path, reader: Any, *, provider: Any = None, coefficients: Any = "shipped"
) -> Any:
    return wiring.build_shadow_runner(
        config=None,  # type: ignore[arg-type]  # compute_config を渡すので config は使わない
        batch_id=ct._BATCH,
        now=ct._NOW,
        candidate_ids={code: ct._rec_id(code) for code in ("0001", "0002")},
        holding_recommendation_id=ct._rec_id,
        recommendation_repo=None,
        reader=reader,
        key_provider=provider if provider is not None else rt._Provider(),
        compute_config=ct._config(tmp_path, coefficients=coefficients),
    )


def _ctx(audit: Any) -> ShadowRunContext:
    return ShadowRunContext(
        batch_id=ct._BATCH,
        now=ct._NOW,
        audit_service=audit,
        deadline=Deadline(30.0),
        io_timeout_seconds=1.0,
        shadow_mode="SHADOW",
        precheck=lambda: None,
    )


def _date() -> Any:
    return ct._NOW.date()  # JST 2026-10-14 10:00


def test_a_real_computation_is_stored_and_read_back_as_a_complete_day(tmp_path: Path) -> None:
    reader = ct._standard_reader(
        cashes={ct._SENTINEL_OWNER: ct.cash(ct._SENTINEL_OWNER, "20000000")}
    )
    audit = rt._Audit()

    recorded = _runner(tmp_path, reader)(_ctx(audit))

    assert recorded is True
    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    validate_index_record(index)
    [items] = [r["output_values"] for r in audit.of("allocation_shadow_items")]
    validate_items_record(items)
    assert items["run"]["outcome"] == "COMPUTED"
    assert items["model"]["coefficient_set_id"]  # 係数の束の識別子が記録される
    assert items["candidates"], "候補が記録されている(header だけの日ではない)"
    assert index["owner_refs"] == [items["owner_ref"]]
    assert index["unrecorded_reasons"] == {}

    screen = rt._read_back(audit, index["owner_refs"])
    assert screen.state is DayState.COMPLETE
    assert screen.complete is True
    assert screen.unparsed == 0
    assert screen.owners[0].rows  # 配分の表が読める


def test_nothing_that_identifies_an_owner_or_a_holding_is_stored(tmp_path: Path) -> None:
    reader = ct._standard_reader(
        cashes={ct._SENTINEL_OWNER: ct.cash(ct._SENTINEL_OWNER, "20000000")}
    )
    audit = rt._Audit()

    _runner(tmp_path, reader)(_ctx(audit))

    stored = json.dumps(list(audit.records.items()), default=str, ensure_ascii=False)
    assert ct._SENTINEL_OWNER not in stored  # owner の平文
    assert "テスト銘柄" not in stored  # 銘柄名
    assert "9001" not in stored  # 保有の銘柄(候補ではない)
    assert "holding_id" not in stored and "owner-a#" not in stored  # 保有 ID の欄・owner#銘柄
    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    screen = rt._read_back(audit, index["owner_refs"])
    assert ct._SENTINEL_OWNER not in render_table(screen) + render_json(screen)


def test_unavailable_coefficients_write_a_day_skip_and_nothing_else(tmp_path: Path) -> None:
    audit = rt._Audit()
    provider = rt._Provider()

    recorded = _runner(tmp_path, ct._standard_reader(), provider=provider, coefficients=None)(
        _ctx(audit)
    )

    assert recorded is True
    assert list(audit.records) == [day_skip_audit_id(_date())]
    assert audit.records[day_skip_audit_id(_date())]["output_values"]["reason"] == (
        "COEFFICIENTS_UNAVAILABLE"
    )
    assert provider.calls == 0  # 入力が作れない日は、鍵も取らない


def test_a_failed_holdings_read_writes_a_day_skip_with_the_reason(tmp_path: Path) -> None:
    reader = ct._standard_reader()

    def broken() -> Any:
        raise RuntimeError("scan failed")

    reader.read_holdings = broken  # type: ignore[method-assign]
    audit = rt._Audit()

    _runner(tmp_path, reader)(_ctx(audit))

    [skip] = [r["output_values"] for r in audit.of("allocation_shadow_day_skip")]
    assert skip["reason"] == "HOLDINGS_READ_FAILED"
    assert audit.of("allocation_shadow_index") == []
    screen = rt._read_back(audit)
    assert screen.state is DayState.NO_INDEX_DAY_SKIP


@pytest.mark.parametrize("reason", list(KeyUnavailableReason))
def test_an_unavailable_key_after_a_real_preparation_records_only_the_day_skip(
    tmp_path: Path, reason: KeyUnavailableReason
) -> None:
    audit = rt._Audit()
    provider = rt._Provider(KeyUnavailable(reason))

    _runner(tmp_path, ct._standard_reader(), provider=provider)(_ctx(audit))

    assert list(audit.records) == [day_skip_audit_id(_date())]
    record = audit.records[day_skip_audit_id(_date())]["output_values"]
    assert record["key_unavailable_reason"] == reason.value
    assert record["unrecorded_owner_count"] == 1


def test_the_record_key_is_the_business_day_and_the_kv1_reference(tmp_path: Path) -> None:
    reader = ct._standard_reader(
        cashes={ct._SENTINEL_OWNER: ct.cash(ct._SENTINEL_OWNER, "20000000")}
    )
    audit = rt._Audit()

    _runner(tmp_path, reader)(_ctx(audit))

    ref = ct._ref()
    assert items_audit_id(_date(), ref) in audit.records
    assert index_audit_id(_date()) in audit.records


def test_the_shipped_configuration_builds_a_compute_config_without_touching_aws() -> None:
    """出荷の YAML から ComputeConfig を組み立てられる(係数・方針・assembler・営業日)。"""
    config = load_config()

    compute_config = wiring._compute_config(config)

    assert compute_config.coefficients is not None
    assert compute_config.bundle is not None
    assert compute_config.assembler_policy.company_quality_minimum > 0
    assert compute_config.shadow_mode == "SHADOW"


def test_the_default_provider_is_not_configured_without_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """環境変数が無ければ、provider は NOT_CONFIGURED(AWS を呼ばない)= 記録しない側へ倒れる。"""
    monkeypatch.delenv(wiring.KEY_SECRET_ARN_ENV, raising=False)

    result = wiring._key_provider().get()

    assert isinstance(result, KeyUnavailable)
    assert result.reason is KeyUnavailableReason.NOT_CONFIGURED
