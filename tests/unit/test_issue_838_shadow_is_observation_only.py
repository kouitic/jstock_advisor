"""Issue #838(#582): valuation_confidence の shadow を出荷値 SHADOW にしても、観測専用であること。

USER 承認(2026-10-08): SHADOW_ACTIVATION = APPROVED、初期の観測期間 = 20 営業日。
**shadow は観測専用**で、Production の BUY / SELL / confidence の判定・通知・保存された業務結果へ
接続しない(接続は別の USER 判断)。出荷 config を OFF -> SHADOW にしたことで、これが崩れないこと
を、handler の合流点(`_process_single_candidate`)で固定する。

固定するもの:
  (a) 出荷 config は SHADOW で、fallback を経ずに読める(引用符つき)
  (b) 出荷 SHADOW のとき、handler の戻り値・保存された Recommendation・通知 service の記録・
      監査記録(shadow 以外)が、OFF のときと完全に同一。差は
      decision_type = valuation_confidence_shadow の監査記録 1 件だけ
  (c) shadow の記録・再計算が失敗しても、(b) の比較対象は OFF のときと同一(本流へ伝播しない)
  (d) VALIDATION モードでは shadow を実行しない(既存の配線。出荷 SHADOW でも崩れない)
  (e) shadow の記録は allowlist の項目のみで、owner / holding_id / 銘柄名を含まない

★ 銘柄コードは実在しない 0000 系・架空値のみ。Production へは一切アクセスしない。
TIME_SEMANTICS_IMPACT = NO(時刻・営業日の扱いは変えない)。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import BuyAction, CandidateSource
from jstock_advisor.domain.entities.execution_context import ExecutionContext, ExecutionMode
from jstock_advisor.domain.signals import valuation_confidence_shadow_config as shadow_cfg
from jstock_advisor.domain.signals.valuation_confidence_shadow_config import (
    ShadowMode,
    ValuationConfidenceShadowConfig,
    load_valuation_confidence_shadow_config,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module
from jstock_advisor.services import valuation_confidence_shadow_service as shadow_module
from jstock_advisor.services.valuation_confidence_shadow_service import DECISION_TYPE
from tests.unit.test_buy_candidates_handler import (
    _CONFIG,
    _NOW,
    _FakeNotificationServiceForRanking,
    _make_recommendation,
    _outcome,
    _patch_snapshot,
    _RecordingAuditService,
)
from tests.unit.test_valuation_confidence_shadow_service import _inputs

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OFF = ValuationConfidenceShadowConfig(mode=ShadowMode.OFF)
_STOCK = "2914"
# 出荷 config を読む本物の loader(同じ test の中で OFF の run のあとに戻せるよう、先に控えておく)
_REAL_LOADER = shadow_module.load_valuation_confidence_shadow_config


class _AuditThatFailsOnShadow(_RecordingAuditService):
    """shadow の記録だけが例外を出す(他の監査記録は通常どおり)。"""

    def record_if_absent(self, audit_id: str, decision_type: str, **kwargs: Any) -> Any:
        if decision_type == DECISION_TYPE:
            raise RuntimeError("shadow recording exploded")
        return super().record_if_absent(audit_id, decision_type, **kwargs)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    shadow_config: ValuationConfidenceShadowConfig | None,
    audit: _RecordingAuditService | None = None,
    execution_context: ExecutionContext | None = None,
) -> dict[str, Any]:
    """_process_single_candidate を 1 回走らせ、外から観測できる結果をまとめて返す。

    shadow_config=None のときは出荷 config(config/valuation_confidence_shadow.yaml)をそのまま読む。
    """
    _patch_snapshot(monkeypatch)
    audit = audit or _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    recommendation = _make_recommendation(
        _STOCK, company_quality_score=72.5, recommendation_id="rec-838", buy_action=BuyAction.BUY
    )
    outcome = dataclasses.replace(
        _outcome(recommendation, ranking_group="buy_candidate"),
        valuation_confidence_shadow_inputs=_inputs(),
    )
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    monkeypatch.setattr(
        shadow_module,
        "load_valuation_confidence_shadow_config",
        _REAL_LOADER if shadow_config is None else (lambda *a, **kw: shadow_config),
    )
    repo = RecommendationRepository(store_dir=tmp_path)
    notification = _FakeNotificationServiceForRanking()

    kwargs: dict[str, Any] = {}
    if execution_context is not None:
        kwargs["execution_context"] = execution_context
    result = handler_module._process_single_candidate(
        _STOCK,
        CandidateSource.WATCHLIST,
        None,
        None,
        None,
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        notification,
        **kwargs,
    )
    return {
        "result": result,
        "saved": sorted(r.model_dump_json() for r in repo.list_all()),
        "notification": {k: repr(v) for k, v in vars(notification).items()},
        "audit_non_shadow": [r for r in audit.records if r["decision_type"] != DECISION_TYPE],
        "audit_shadow": [r for r in audit.records if r["decision_type"] == DECISION_TYPE],
    }


# --- (a) 出荷 config ----------------------------------------------------------------


def test_shipped_config_is_shadow_and_loads_without_falling_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.DEBUG, logger=shadow_cfg.__name__)

    cfg = load_valuation_confidence_shadow_config(_REPO_ROOT / "config")

    assert cfg.mode is ShadowMode.SHADOW
    assert cfg.enabled is True
    assert [r for r in caplog.records if r.name == shadow_cfg.__name__] == []


# --- (b) 観測専用: OFF と出荷 SHADOW で、本流の結果が完全に同一 -------------------------------


def test_shipped_shadow_changes_nothing_but_one_shadow_audit_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    off = _run(monkeypatch, tmp_path / "off", shadow_config=_OFF)
    shadow = _run(monkeypatch, tmp_path / "shadow", shadow_config=None)  # 出荷 config を読む

    assert off["audit_shadow"] == []
    assert len(shadow["audit_shadow"]) == 1  # 差は shadow の監査記録 1 件だけ
    # 監査記録の stock_code 欄(既存の監査と同じ)
    assert shadow["audit_shadow"][0]["stock_code"] == _STOCK

    assert shadow["result"] == off["result"]
    assert shadow["saved"] == off["saved"]
    assert shadow["notification"] == off["notification"]
    assert shadow["audit_non_shadow"] == off["audit_non_shadow"]
    assert len(shadow["saved"]) >= 1  # 比較が空集合どうしの一致になっていないこと


# --- (c) shadow の失敗は本流へ伝播しない ------------------------------------------------


def test_shadow_recording_failure_leaves_the_main_flow_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    off = _run(monkeypatch, tmp_path / "off", shadow_config=_OFF)
    failing = _run(
        monkeypatch, tmp_path / "failing", shadow_config=None, audit=_AuditThatFailsOnShadow()
    )

    assert failing["audit_shadow"] == []  # 記録に失敗した
    assert failing["result"] == off["result"]
    assert failing["saved"] == off["saved"]
    assert failing["notification"] == off["notification"]
    assert failing["audit_non_shadow"] == off["audit_non_shadow"]


def test_shadow_recomputation_failure_leaves_the_main_flow_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    off = _run(monkeypatch, tmp_path / "off", shadow_config=_OFF)

    def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("candidate recompute exploded")

    monkeypatch.setattr(shadow_module, "_run_candidate_chain", _boom)
    failing = _run(monkeypatch, tmp_path / "failing", shadow_config=None)

    assert failing["audit_shadow"] == []
    assert failing["result"] == off["result"]
    assert failing["saved"] == off["saved"]
    assert failing["notification"] == off["notification"]
    assert failing["audit_non_shadow"] == off["audit_non_shadow"]


# --- (d) VALIDATION モードでは実行しない --------------------------------------------------


def test_validation_mode_runs_no_shadow_even_with_shipped_shadow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    validation = _run(
        monkeypatch,
        tmp_path,
        shadow_config=None,
        execution_context=ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert validation["audit_shadow"] == []


# --- (e) 記録の中身は allowlist のみ --------------------------------------------------------


def test_shadow_record_does_not_leak_owner_holding_or_stock_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shadow = _run(monkeypatch, tmp_path, shadow_config=None)

    record = shadow["audit_shadow"][0]
    text = repr(record)
    assert "owner" not in text.lower()
    assert "holding_id" not in text
    assert f"銘柄{_STOCK}" not in text  # _make_recommendation の stock_name
    # 銘柄コードは監査記録の stock_code 欄にだけ入る(既存の監査と同じ)。
    # input_values / output_values には入れない
    assert _STOCK not in repr(record["input_values"])
    assert _STOCK not in repr(record["output_values"])
