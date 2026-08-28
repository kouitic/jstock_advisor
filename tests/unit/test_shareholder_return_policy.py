"""Issue #30 Phase 1: 株主還元方針レジストリ・DividendInfo 3状態化・scoreのテスト。

- レジストリ(config/shareholder_return_policies.yaml)のschema validation
- enrichment provider(手動レジストリが唯一のTrue/False正本)
- score_dividend_sustainabilityの3状態(True/False/None)処理と式の不変性
- input_facts / component_statesへの観測情報記録
- 過去実績(連続増配等)からpolicyを推測しないこと
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import (
    ShareholderReturnPoliciesConfig,
    ShareholderReturnPolicyEntry,
)
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ShareholderReturnPolicyType
from jstock_advisor.domain.scoring.score import (
    UndervaluationSignals,
    compute_score,
    score_dividend_sustainability,
)
from jstock_advisor.interfaces.types import DividendInfo, FinancialSummary
from jstock_advisor.providers.dividend_data.policy_enrichment_impl import (
    ShareholderReturnPolicyEnrichingDividendDataProvider,
)

_NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)
_CONFIG = load_config()


def _entry(**overrides: object) -> ShareholderReturnPolicyEntry:
    base = dict(
        stock_code="9433",
        policy_type="PROGRESSIVE",
        status="CONFIRMED",
        source_type="COMPANY_IR",
        source_reference="https://example.com/ir/policy",
        source_date=dt.date(2026, 5, 1),
        evidence_text="累進配当を基本方針とする",
        checked_at=dt.date(2026, 8, 28),
    )
    base.update(overrides)
    return ShareholderReturnPolicyEntry(**base)  # type: ignore[arg-type]


def _registry(*entries: ShareholderReturnPolicyEntry) -> ShareholderReturnPoliciesConfig:
    return ShareholderReturnPoliciesConfig(version=1, policies=list(entries))


def _dividend(**overrides: object) -> DividendInfo:
    base = dict(stock_code="9433", fiscal_year="2026", source=_SOURCE)
    base.update(overrides)
    return DividendInfo(**base)  # type: ignore[arg-type]


def _financial(**overrides: object) -> FinancialSummary:
    base = dict(
        stock_code="9433",
        fiscal_period_end=_NOW.date(),
        equity_ratio_pct=60.0,
        payout_ratio_pct=45.0,
        source=_SOURCE,
    )
    base.update(overrides)
    return FinancialSummary(**base)  # type: ignore[arg-type]


class _FakeInnerProvider:
    def __init__(self, info: DividendInfo | None) -> None:
        self._info = info

    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        return self._info


# --- registry schema validation ----------------------------------------------


def test_shipped_registry_file_is_valid_and_empty() -> None:
    path = Path(__file__).resolve().parents[2] / "config" / "shareholder_return_policies.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = ShareholderReturnPoliciesConfig.model_validate(data)
    # 初期データは登録しない(実装確認はテストfixtureのみ。Issue #30 Phase 1)
    assert config.policies == []
    # load_config()経由でも読み込まれる(AppConfigへの組み込み確認)
    assert _CONFIG.shareholder_return_policies.policies == []


def test_invalid_stock_code_fails_fast() -> None:
    with pytest.raises(ValidationError):
        _entry(stock_code="12")
    with pytest.raises(ValidationError):
        _entry(stock_code="abcd")


def test_invalid_policy_type_fails_fast() -> None:
    with pytest.raises(ValidationError):
        _entry(policy_type="PROGRESSIVE_MAYBE")


def test_invalid_status_and_source_type_fail_fast() -> None:
    with pytest.raises(ValidationError):
        _entry(status="GUESSED")
    with pytest.raises(ValidationError):
        _entry(source_type="LLM_OUTPUT")


def test_empty_source_reference_or_evidence_fails_fast() -> None:
    with pytest.raises(ValidationError):
        _entry(source_reference="")
    with pytest.raises(ValidationError):
        _entry(evidence_text="")


def test_duplicate_stock_code_fails_fast() -> None:
    with pytest.raises(ValidationError):
        _registry(_entry(), _entry(policy_type="DOE"))


# --- enrichment provider(True/False認定の唯一の経路) -------------------------


def test_registry_progressive_yields_true_with_audit_snapshot() -> None:
    provider = ShareholderReturnPolicyEnrichingDividendDataProvider(
        inner=_FakeInnerProvider(_dividend()), policies=_registry(_entry())
    )
    info = provider.get_dividend_info("9433")
    assert info is not None
    assert info.is_progressive_or_doe_policy is True
    assert info.shareholder_return_policy_type == ShareholderReturnPolicyType.PROGRESSIVE
    assert info.shareholder_return_policy_source_type == "COMPANY_IR"
    assert info.shareholder_return_policy_source_reference == "https://example.com/ir/policy"
    assert info.shareholder_return_policy_checked_at == dt.date(2026, 8, 28)


def test_registry_doe_and_both_yield_true() -> None:
    for policy_type in ("DOE", "BOTH"):
        provider = ShareholderReturnPolicyEnrichingDividendDataProvider(
            inner=_FakeInnerProvider(_dividend()),
            policies=_registry(_entry(policy_type=policy_type)),
        )
        info = provider.get_dividend_info("9433")
        assert info is not None
        assert info.is_progressive_or_doe_policy is True


def test_registry_none_yields_confirmed_false() -> None:
    provider = ShareholderReturnPolicyEnrichingDividendDataProvider(
        inner=_FakeInnerProvider(_dividend()),
        policies=_registry(_entry(policy_type="NONE")),
    )
    info = provider.get_dividend_info("9433")
    assert info is not None
    assert info.is_progressive_or_doe_policy is False
    assert info.shareholder_return_policy_type == ShareholderReturnPolicyType.NONE


def test_absent_from_registry_stays_unknown() -> None:
    provider = ShareholderReturnPolicyEnrichingDividendDataProvider(
        inner=_FakeInnerProvider(_dividend()), policies=_registry()
    )
    info = provider.get_dividend_info("9433")
    assert info is not None
    assert info.is_progressive_or_doe_policy is None
    assert info.shareholder_return_policy_type is None


def test_inner_none_passes_through() -> None:
    provider = ShareholderReturnPolicyEnrichingDividendDataProvider(
        inner=_FakeInnerProvider(None), policies=_registry(_entry())
    )
    assert provider.get_dividend_info("9433") is None


def test_dividend_info_default_is_unknown() -> None:
    """DividendInfoの既定値はNone(UNKNOWN)。False(方針なし確認済み)ではない。"""
    assert _dividend().is_progressive_or_doe_policy is None


# --- score 3状態(式は不変: 0.4/0.4/0.2、weight 20) ---------------------------


def _sustainability(dividend: DividendInfo) -> float:
    score, _ = score_dividend_sustainability(
        dividend, _financial(payout_ratio_pct=None), max_payout_ratio_pct=70.0, weight=20.0
    )
    return score


def test_score_true_gets_policy_8_points() -> None:
    assert _sustainability(_dividend(is_progressive_or_doe_policy=True)) == pytest.approx(8.0)


def test_score_both_policy_is_still_8_points_not_16() -> None:
    provider = ShareholderReturnPolicyEnrichingDividendDataProvider(
        inner=_FakeInnerProvider(_dividend()),
        policies=_registry(_entry(policy_type="BOTH")),
    )
    info = provider.get_dividend_info("9433")
    assert info is not None
    assert _sustainability(info) == pytest.approx(8.0)


def test_score_false_and_none_are_zero_and_identical() -> None:
    false_score = _sustainability(_dividend(is_progressive_or_doe_policy=False))
    none_score = _sustainability(_dividend(is_progressive_or_doe_policy=None))
    assert false_score == pytest.approx(0.0)
    assert none_score == pytest.approx(0.0)  # UNKNOWNへの中立加点・再正規化はしない


def test_score_streak_and_payout_parts_unchanged_by_policy_state() -> None:
    """連続増配(0.4)・配当性向(0.2)のfactorは方針の3状態と独立に従来どおり。"""
    for policy in (False, None):
        score, formula = score_dividend_sustainability(
            _dividend(is_progressive_or_doe_policy=policy, consecutive_dividend_increase_years=5),
            _financial(payout_ratio_pct=0.0),
            max_payout_ratio_pct=70.0,
            weight=20.0,
        )
        # 0.4(連続増配5年満点) + 0.2(配当性向余力満点) = 0.6 -> 12点
        assert score == pytest.approx(12.0)
        assert "連続増配5年" in formula


def test_five_year_streak_does_not_infer_policy() -> None:
    """5年連続増配・レジストリ未登録 -> policyはNoneのまま(実績から推測しない)。
    方針分の8点は加点されない(J/K基準)。"""
    provider = ShareholderReturnPolicyEnrichingDividendDataProvider(
        inner=_FakeInnerProvider(_dividend(consecutive_dividend_increase_years=5)),
        policies=_registry(),
    )
    info = provider.get_dividend_info("9433")
    assert info is not None
    assert info.is_progressive_or_doe_policy is None
    score, _ = score_dividend_sustainability(
        info, _financial(payout_ratio_pct=None), max_payout_ratio_pct=70.0, weight=20.0
    )
    assert score == pytest.approx(8.0)  # 連続増配0.4のみ(方針0.4は加点なし)


# --- input_facts / component_states ------------------------------------------


def _compute(dividend: DividendInfo):
    return compute_score(
        total_yield_pct=5.0,
        dividend=dividend,
        financial=_financial(),
        undervaluation_signals=UndervaluationSignals(),
        benefit_yield_pct=None,
        quarterly_operating_incomes=[Decimal("100"), Decimal("110"), Decimal("120")],
        price_bars=[],
        min_equity_ratio_pct=30.0,
        max_payout_ratio_pct=70.0,
        config=_CONFIG.scoring,
        undervaluation_category_caps=_CONFIG.buy_decision.undervaluation_category_caps,
    )


def test_input_facts_snapshot_unknown() -> None:
    result = _compute(_dividend())
    facts = result.input_facts
    assert facts["is_progressive_or_doe_policy"] is None
    assert facts["shareholder_return_policy_status"] == "UNKNOWN"
    assert facts["shareholder_return_policy_type"] is None
    assert facts["shareholder_return_policy_source"] is None
    assert facts["shareholder_return_policy_checked_at"] is None
    state = result.component_states["dividend_sustainability"]
    assert isinstance(state, dict)
    assert "POLICY_STATUS_UNKNOWN" in state["reason_codes"]  # type: ignore[index]


def test_input_facts_snapshot_confirmed_true() -> None:
    provider = ShareholderReturnPolicyEnrichingDividendDataProvider(
        inner=_FakeInnerProvider(_dividend()), policies=_registry(_entry())
    )
    info = provider.get_dividend_info("9433")
    assert info is not None
    result = _compute(info)
    facts = result.input_facts
    assert facts["is_progressive_or_doe_policy"] is True
    assert facts["shareholder_return_policy_status"] == "CONFIRMED"
    assert facts["shareholder_return_policy_type"] == ShareholderReturnPolicyType.PROGRESSIVE
    assert facts["shareholder_return_policy_source"] == "https://example.com/ir/policy"
    assert facts["shareholder_return_policy_checked_at"] == "2026-08-28"
    state = result.component_states["dividend_sustainability"]
    assert isinstance(state, dict)
    reason_codes = state["reason_codes"]  # type: ignore[index]
    assert "POLICY_STATUS_UNKNOWN" not in reason_codes
    assert "POLICY_NONE_CONFIRMED" not in reason_codes


def test_component_state_confirmed_none_reason() -> None:
    result = _compute(_dividend(is_progressive_or_doe_policy=False))
    state = result.component_states["dividend_sustainability"]
    assert isinstance(state, dict)
    assert "POLICY_NONE_CONFIRMED" in state["reason_codes"]  # type: ignore[index]
    # 方針以外のfactorは評価可能なため、component全体はEVALUATEDのまま
    assert str(state["state"]) == "EVALUATED"


def test_component_state_stays_evaluated_when_policy_unknown() -> None:
    result = _compute(_dividend())
    state = result.component_states["dividend_sustainability"]
    assert isinstance(state, dict)
    assert str(state["state"]) == "EVALUATED"
