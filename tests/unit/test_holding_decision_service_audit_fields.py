"""HoldingDecisionServiceが保存する監査フィールドのテスト(実装プラン15節・20節)。

evaluation_duration_msが評価ごとに記録されること、legacy_reason_codes/
new_reason_codesが新旧両エンジン実行時のみ両方埋まること、positive_reasons/
negative_reasonsが件数上限を超えずscore_impact降順で並ぶことを検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType, ExecutionMode, ExecutionPlanReason
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.investment_thesis_baseline_repository import (
    InvestmentThesisBaselineRepository,
)
from jstock_advisor.infrastructure.local_repository.investment_thesis_repository import (
    InvestmentThesisRepository,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
)
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_CFG = load_config()
_NOW = dt.datetime.now(dt.UTC)
_PROVIDERS = build_mock_provider_bundle(_NOW)


def _holding(stock_code: str = "2914") -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="x",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _service(store_dir: Path) -> HoldingDecisionService:
    return HoldingDecisionService(
        _PROVIDERS,
        _CFG,
        investment_thesis_service=InvestmentThesisService(store_dir=store_dir),
        runtime_config_service=HoldingDecisionRuntimeConfigService(store_dir=store_dir),
        audit_service=AuditService(AuditLogRepository(store_dir)),
    )


def test_evaluation_duration_ms_is_recorded(store_dir: Path):
    outcome = _service(store_dir).evaluate(_holding(), _NOW, ExecutionPlanReason.NORMAL_SHADOW)
    assert outcome.result is not None
    assert outcome.result.evaluation_duration_ms is not None
    assert outcome.result.evaluation_duration_ms >= 0


def test_legacy_reason_codes_stays_empty_when_not_supplied(store_dir: Path):
    outcome = _service(store_dir).evaluate(_holding(), _NOW, ExecutionPlanReason.NORMAL_ACTIVE)
    assert outcome.result is not None
    assert outcome.result.legacy_reason_codes == ()


def test_legacy_reason_codes_populated_when_both_engines_run(store_dir: Path):
    outcome = _service(store_dir).evaluate(
        _holding(),
        _NOW,
        ExecutionPlanReason.NORMAL_SHADOW,
        legacy_reason_codes=("dividend_cut", "continuous_operating_income_decline"),
    )
    assert outcome.result is not None
    assert outcome.result.legacy_reason_codes == (
        "dividend_cut",
        "continuous_operating_income_decline",
    )


def test_positive_and_negative_reasons_respect_configured_caps(store_dir: Path):
    outcome = _service(store_dir).evaluate(_holding(), _NOW, ExecutionPlanReason.NORMAL_ACTIVE)
    assert outcome.result is not None
    rules = _CFG.holding_decision
    assert len(outcome.result.positive_reasons) <= rules.top_positive_reasons_count
    assert len(outcome.result.negative_reasons) <= rules.top_negative_reasons_count


def test_positive_reasons_sorted_descending_by_score_impact(store_dir: Path):
    outcome = _service(store_dir).evaluate(_holding(), _NOW, ExecutionPlanReason.NORMAL_ACTIVE)
    assert outcome.result is not None
    impacts = [r.score_impact for r in outcome.result.positive_reasons]
    assert impacts == sorted(impacts, reverse=True)
    assert all(v > 0 for v in impacts)


def test_negative_reasons_sorted_ascending_by_score_impact(store_dir: Path):
    outcome = _service(store_dir).evaluate(_holding(), _NOW, ExecutionPlanReason.NORMAL_ACTIVE)
    assert outcome.result is not None
    impacts = [r.score_impact for r in outcome.result.negative_reasons]
    assert impacts == sorted(impacts)
    assert all(v < 0 for v in impacts)


def test_reason_impacts_have_structured_fields(store_dir: Path):
    outcome = _service(store_dir).evaluate(_holding(), _NOW, ExecutionPlanReason.NORMAL_ACTIVE)
    assert outcome.result is not None
    for reason in (*outcome.result.positive_reasons, *outcome.result.negative_reasons):
        assert reason.reason_code
        assert reason.category
        assert isinstance(reason.score_impact, float)


def test_validation_mode_still_produces_same_result_without_persisting(
    store_dir: Path, tmp_path: Path
):
    """通知検証モード コードレビュー対応: baseline未存在・InvestmentThesis未存在の
    保有銘柄をVALIDATIONで評価しても、NORMALと同一ロジックでHoldingDecisionResultを
    生成できる(=検証banner付きLINE通知本文も同じ内容で組み立てられる)一方、
    本番InvestmentThesisBaseline/InvestmentThesis/AuditLogのいずれへも一切
    保存されないことを検証する。
    """
    # mockプロバイダは呼び出しごとに一部の値をランダム生成するため、NORMAL/VALIDATION
    # 両方に同一のsnapshotを渡すことで「モードの違いだけ」を検証対象にする。
    snapshot, error = build_stock_snapshot(_PROVIDERS, "2914", _NOW, _CFG)
    assert error is None and snapshot is not None

    normal_outcome = _service(store_dir).evaluate(
        _holding(), _NOW, ExecutionPlanReason.NORMAL_ACTIVE, snapshot=snapshot
    )
    assert normal_outcome.result is not None

    validation_store_dir = tmp_path / "validation_store"
    validation_context = ExecutionContext(mode=ExecutionMode.VALIDATION)
    validation_service = HoldingDecisionService(
        _PROVIDERS,
        _CFG,
        investment_thesis_service=InvestmentThesisService(
            store_dir=validation_store_dir, execution_context=validation_context
        ),
        runtime_config_service=HoldingDecisionRuntimeConfigService(store_dir=validation_store_dir),
        audit_service=AuditService(
            AuditLogRepository(validation_store_dir), execution_context=validation_context
        ),
        execution_context=validation_context,
    )
    validation_outcome = validation_service.evaluate(
        _holding(), _NOW, ExecutionPlanReason.NORMAL_ACTIVE, snapshot=snapshot
    )

    assert validation_outcome.result is not None
    # 本番と全く同じ判定ロジックで同一の結果が生成できる
    assert validation_outcome.result.final_score == normal_outcome.result.final_score
    assert validation_outcome.result.should_notify == normal_outcome.result.should_notify
    assert validation_outcome.result.category == normal_outcome.result.category

    # 本番baseline/thesis/AuditLogのいずれへも保存されない
    assert InvestmentThesisBaselineRepository(validation_store_dir).list_by_holding("2914") == []
    assert InvestmentThesisRepository(validation_store_dir).get_by_holding("2914") is None
    assert AuditLogRepository(validation_store_dir).list_all() == []
