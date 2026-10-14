"""Issue #603 PR-F2-2a: 購入側 Shadow の計算の組み立て(配線なし・dormant)の契約テスト。

固定するもの:
  - バッチ共通の入力: 保有の Scan 1 回・推奨の一括取得 1 回(候補 ∪ 保有の ID。重複を除く)。
    上限超過は読まずに INPUT_LIMIT_EXCEEDED。読取の失敗・期限・係数の縮退は理由コードに畳み、
    例外を送出しない
  - 候補: 推奨 → G1 の RAER → CandidateRecord(HARD_STOP の価格は使わない・推奨が無い候補は除く)
  - 保有: 株数は owner 別・価格・業種・印は推奨から。推奨が無い保有 = RiskNotRecorded、
    旧い記録 = 全語が未観測(= high_risk_exposure は算出不能。0・中立値で補わない)
  - owner の計算: 買付余力の各状態(未登録・IAM・期限・失敗)→ header だけの記録。計算できた日は
    F2-1 の記録が allowlist を通り、replay が一致する。owner の平文・鍵は記録に出ない
  - 構造: 実在の AWS・repository・時計に触れない(注入だけ)/ どこからも import されない
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import json
import threading
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.domain.entities.enums import (
    AccountType,
    AvailableCashUpdateType,
    BuyAction,
    BuyIndustrySector,
    ConfidenceLevel,
    EarningsTrendEvaluationState,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.identifier_ref import (
    KeyedOwnerRef,
    KeyMaterial,
    keyed_owner_ref,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.allocation_policy_config import (
    AllocationPolicyBundle,
    load_allocation_policy,
)
from jstock_advisor.domain.signals.allocation_shadow_record import (
    replay_items_record,
    validate_items_record,
)
from jstock_advisor.domain.valuation.raer_coefficients_config import load_raer_coefficients
from jstock_advisor.domain.valuation.raer_composition import TrendWeights
from jstock_advisor.domain.valuation.raer_inputs import (
    RAER_FULL_COMPARISON_KEY,
    AssemblerPolicy,
    facts_from_recommendation,
)
from jstock_advisor.services import allocation_shadow_compute as compute
from jstock_advisor.services.allocation_shadow_compute import (
    MAX_RECOMMENDATIONS,
    BatchInputs,
    BatchSpec,
    BatchUnavailable,
    ComputeConfig,
    OwnerComputation,
    compute_owner,
    prepare_batch_inputs,
)
from jstock_advisor.services.allocation_shadow_service import (
    Deadline,
    ShadowOutcome,
    ShadowSkipReason,
)
from tests.factories import build_recommendation

D = Decimal
_JST = dt.timezone(dt.timedelta(hours=9))
_NOW = dt.datetime(2026, 10, 14, 10, 0, tzinfo=_JST)
_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO / "src" / "jstock_advisor" / "services" / "allocation_shadow_compute.py"
_PACKAGE = _REPO / "src" / "jstock_advisor"
_TARGET = "jstock_advisor.services.allocation_shadow_compute."
_KEY = KeyMaterial(key_version=1, key=b"t" * 32)  # テスト用の鍵(秘密ではない)
_BATCH = "buy-candidates-20261014"
_SENTINEL_OWNER = "sentinel-owner-zz"
_FRESH = dt.date(2026, 10, 13)  # 直近の営業日の終値(NORMAL)
_STALE = dt.date(2026, 9, 1)


def _ref(owner: str = _SENTINEL_OWNER) -> KeyedOwnerRef:
    return keyed_owner_ref(owner, _KEY)


def _rec_id(code: str) -> str:
    return f"{_BATCH}:{code}"


def _quality() -> dict[str, object]:
    return {"shadow_state": "COMPUTED", "score": 30.0, "coverage_ratio": 0.8}


def _trend() -> dict[str, object]:
    return {
        "state": EarningsTrendEvaluationState.EVALUATED.value
        if hasattr(EarningsTrendEvaluationState, "EVALUATED")
        else "EVALUATED",
        "operating_income_trend_component": 37.5,
        "operating_cashflow_trend_component": -20.0,
    }


_DETECTION_OBSERVED = {
    "version": 1,
    "flags": [],
    "observed": {"earnings_date": True, "liquidity": True, "industry": True, "eps": True},
}


def rec(code: str, **overrides: Any) -> Recommendation:
    values: dict[str, Any] = {
        "recommendation_id": _rec_id(code),
        "stock_code": code,
        "buy_action": BuyAction.BUY,
        "price_at_recommendation": D("1000"),
        "price_as_of_date": _FRESH,
        "fair_value_neutral": D("1400"),
        "fair_value_overall_confidence": ConfidenceLevel.MEDIUM,
        "fair_value_usable_for_trading_judgment": True,
        "dividend_yield_pct_at_recommendation": 3.0,
        "total_yield_pct_at_recommendation": 3.0,
        "required_margin_of_safety_entry": D("0.2"),
        "buy_industry_sector": BuyIndustrySector.GENERAL,
        "buy_score_input_facts": {
            "common_quality_shadow": _quality(),
            "raer_risk_detection": _DETECTION_OBSERVED,
        },
        "earnings_trend_metrics": _trend(),
    }
    values.update(overrides)
    return build_recommendation(**values)


def holding(owner: str, code: str, shares: int = 100) -> Holding:
    return Holding(
        owner=owner,
        holding_id=f"{owner}#{code}",
        stock_code=code,
        stock_name="テスト銘柄",
        shares=shares,
        average_purchase_price=D("900"),
        total_purchase_amount=D("900") * shares,
        first_purchase_date=dt.date(2026, 1, 5),
        last_purchase_date=dt.date(2026, 1, 5),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def cash(owner: str, amount: str = "5000000") -> AvailableCash:
    return AvailableCash(
        owner=owner,
        available_cash=D(amount),
        updated_at=_NOW - dt.timedelta(days=1),
        last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
        last_reconciled_at=_NOW - dt.timedelta(days=3),
    )


class FakeReader:
    def __init__(
        self,
        holdings: Sequence[Holding] = (),
        recs: Mapping[str, Recommendation] | None = None,
        cashes: Mapping[str, AvailableCash | BaseException] | None = None,
        holdings_error: BaseException | None = None,
        recs_error: BaseException | None = None,
        block: threading.Event | None = None,
    ) -> None:
        self.holdings = list(holdings)
        self.recs = dict(recs or {})
        self.cashes = dict(cashes or {})
        self.holdings_error = holdings_error
        self.recs_error = recs_error
        self.block = block
        self.calls: list[tuple[str, Any]] = []

    def read_holdings(self) -> Sequence[Holding]:
        self.calls.append(("holdings", None))
        if self.block is not None:
            self.block.wait(5)
        if self.holdings_error is not None:
            raise self.holdings_error
        return self.holdings

    def read_recommendations(self, ids: Sequence[str]) -> Mapping[str, Recommendation]:
        self.calls.append(("recommendations", tuple(ids)))
        if self.recs_error is not None:
            raise self.recs_error
        return {i: self.recs[i] for i in ids if i in self.recs}

    def read_available_cash(self, owner: str) -> AvailableCash | None:
        self.calls.append(("cash", owner))
        value = self.cashes.get(owner)
        if isinstance(value, BaseException):
            raise value
        return value


def _bundle(tmp_path: Path) -> AllocationPolicyBundle:
    text = (_REPO / "config" / "allocation_policy.yaml").read_text(encoding="utf-8")
    text = text.replace("comparison_key: null", f'comparison_key: "{RAER_FULL_COMPARISON_KEY}"')
    (tmp_path / "allocation_policy.yaml").write_text(text, encoding="utf-8")
    return load_allocation_policy(tmp_path, minimum_trading_unit=100)


def _config(tmp_path: Path, *, coefficients: Any = "shipped") -> ComputeConfig:
    bundle = load_raer_coefficients()
    return ComputeConfig(
        bundle=_bundle(tmp_path),
        coefficients=bundle.coefficients if coefficients == "shipped" else coefficients,
        assembler_policy=AssemblerPolicy(
            company_quality_minimum=D("0.6"),
            trend_weights=TrendWeights(D("1.0"), D("0.75"), D("0.5"), D("0.25")),
            trend_min_coverage_required=D("0.3"),
            trend_model_version="earnings_trend_v3",
        ),
        calendar=BusinessCalendar(frozenset(), frozenset()),
    )


def spec(codes: Sequence[str] = ("0001", "0002")) -> BatchSpec:
    return BatchSpec(
        batch_id=_BATCH,
        now=_NOW,
        candidate_ids={code: _rec_id(code) for code in codes},
        holding_recommendation_id=_rec_id,
    )


def _prepare(
    tmp_path: Path,
    reader: FakeReader,
    *,
    codes: Sequence[str] = ("0001", "0002"),
    deadline: Deadline | None = None,
    io_timeout: float = 1.0,
    config: ComputeConfig | None = None,
) -> BatchInputs | BatchUnavailable:
    return prepare_batch_inputs(
        spec(codes),
        config or _config(tmp_path),
        reader,
        deadline=deadline or Deadline(30.0),
        io_timeout_seconds=io_timeout,
    )


def _ready(tmp_path: Path, reader: FakeReader, **kwargs: Any) -> BatchInputs:
    result = _prepare(tmp_path, reader, **kwargs)
    assert isinstance(result, BatchInputs), result
    return result


def _standard_reader(**overrides: Any) -> FakeReader:
    recs = {_rec_id(code): rec(code, stock_name="テスト銘柄") for code in ("0001", "0002")}
    # 保有の銘柄は別の業種(候補の業種の上限に当たらない)
    recs[_rec_id("9001")] = rec("9001", buy_industry_sector=BuyIndustrySector.BANK)
    values: dict[str, Any] = {
        "holdings": [holding(_SENTINEL_OWNER, "9001", 100000)],
        "recs": recs,
        "cashes": {_SENTINEL_OWNER: cash(_SENTINEL_OWNER)},
    }
    values.update(overrides)
    return FakeReader(**values)


def _owner(
    inputs: BatchInputs,
    reader: FakeReader,
    owner: str = _SENTINEL_OWNER,
    *,
    deadline: Deadline | None = None,
) -> OwnerComputation:
    return compute_owner(
        inputs,
        owner,
        _ref(owner),
        reader=reader,
        deadline=deadline or Deadline(30.0),
        io_timeout_seconds=1.0,
        elapsed_ms=12,
    )


# =================================================================================================
# バッチ共通の入力
# =================================================================================================


def test_the_batch_inputs_are_built_with_one_scan_and_one_recommendation_read(
    tmp_path: Path,
) -> None:
    reader = _standard_reader()

    inputs = _ready(tmp_path, reader)

    assert [name for name, _ in reader.calls] == ["holdings", "recommendations"]
    assert inputs.owners == (_SENTINEL_OWNER,)
    assert [c.stock_code for c in inputs.candidates] == ["0001", "0002"]
    assert set(inputs.details) == {"0001", "0002"}
    assert dict(inputs.read_states) == {"holdings": "OK", "recommendations": "OK"}
    assert inputs.business_date == dt.date(2026, 10, 14)


def test_the_ids_are_the_union_of_candidates_and_holdings_without_duplicates(
    tmp_path: Path,
) -> None:
    reader = _standard_reader(holdings=[holding("owner-a", "0001"), holding("owner-b", "9001")])

    _ready(tmp_path, reader)

    ids = [value for name, value in reader.calls if name == "recommendations"][0]
    assert ids == (_rec_id("0001"), _rec_id("0002"), _rec_id("9001"))


def test_a_candidate_that_is_also_held_keeps_the_candidate_recommendation_id(
    tmp_path: Path,
) -> None:
    reader = _standard_reader(holdings=[holding("owner-a", "0001")])
    reader.recs["cand-0001"] = reader.recs.pop(_rec_id("0001"))
    candidate_spec = BatchSpec(
        batch_id=_BATCH,
        now=_NOW,
        candidate_ids={"0001": "cand-0001", "0002": _rec_id("0002")},
        holding_recommendation_id=_rec_id,
    )

    result = prepare_batch_inputs(
        candidate_spec,
        _config(tmp_path),
        reader,
        deadline=Deadline(30.0),
        io_timeout_seconds=1.0,
    )

    assert isinstance(result, BatchInputs)
    ids = [value for name, value in reader.calls if name == "recommendations"][0]
    assert ids == (_rec_id("0002"), "cand-0001")  # 同じ銘柄の ID を 2 通り読まない(昇順)
    assert "0001" in result.recommendations  # 保有の事実は候補と同じ推奨から取る


def test_the_recommendation_ids_are_sorted_whatever_the_candidate_order(tmp_path: Path) -> None:
    reader = _standard_reader()

    _ready(tmp_path, reader, codes=("0002", "0001"))

    ids = [value for name, value in reader.calls if name == "recommendations"][0]
    assert list(ids) == sorted(ids)


def test_the_business_date_is_the_jst_calendar_day(tmp_path: Path) -> None:
    """JST 00:30(UTC では前日)でも業務日は JST の日付。"""
    early = BatchSpec(
        _BATCH, dt.datetime(2026, 10, 14, 0, 30, tzinfo=_JST), {"0001": _rec_id("0001")}, _rec_id
    )
    result = prepare_batch_inputs(
        early,
        _config(tmp_path),
        _standard_reader(),
        deadline=Deadline(30.0),
        io_timeout_seconds=1.0,
    )

    assert isinstance(result, BatchInputs)
    assert result.business_date == dt.date(2026, 10, 14)


def test_owners_are_the_distinct_owners_of_the_scan_in_order(tmp_path: Path) -> None:
    reader = _standard_reader(
        holdings=[
            holding("owner-b", "9001"),
            holding("owner-a", "9001"),
            holding("owner-b", "0001"),
        ]
    )

    assert _ready(tmp_path, reader).owners == ("owner-a", "owner-b")


def test_a_holding_with_no_shares_is_not_a_holding(tmp_path: Path) -> None:
    reader = _standard_reader(holdings=[holding("owner-a", "9001", 0)])

    inputs = _ready(tmp_path, reader)

    assert inputs.owners == ()


def _held_codes(count: int) -> list[Holding]:
    return [holding("owner-a", f"H{n:04d}") for n in range(count)]  # 候補(0001・0002)と重ならない


def test_more_than_the_limit_is_not_read(tmp_path: Path) -> None:
    reader = _standard_reader(holdings=_held_codes(MAX_RECOMMENDATIONS - 1))  # 候補 2 + 499 = 501

    result = _prepare(tmp_path, reader)

    assert isinstance(result, BatchUnavailable)
    assert result.reason is ShadowSkipReason.INPUT_LIMIT_EXCEEDED
    assert [name for name, _ in reader.calls] == ["holdings"]  # 推奨は読まない


def test_exactly_the_limit_is_read(tmp_path: Path) -> None:
    reader = _standard_reader(holdings=_held_codes(MAX_RECOMMENDATIONS - 2))  # 候補 2 + 498 = 500

    assert isinstance(_prepare(tmp_path, reader), BatchInputs)
    ids = [value for name, value in reader.calls if name == "recommendations"][0]
    assert len(ids) == MAX_RECOMMENDATIONS


def test_the_limit_constant_is_the_five_batch_get_requests() -> None:
    assert MAX_RECOMMENDATIONS == 500


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"holdings_error": RuntimeError("boom")}, ShadowSkipReason.HOLDINGS_READ_FAILED),
        ({"recs_error": OSError("net")}, ShadowSkipReason.RECOMMENDATIONS_READ_FAILED),
    ],
)
def test_a_failed_read_is_folded_into_a_reason_and_never_raised(
    tmp_path: Path, kwargs: dict[str, Any], reason: ShadowSkipReason
) -> None:
    result = _prepare(tmp_path, _standard_reader(**kwargs))

    assert isinstance(result, BatchUnavailable)
    assert result.reason is reason


def test_a_slow_read_is_cut_off_by_the_io_timeout(tmp_path: Path) -> None:
    gate = threading.Event()
    try:
        result = _prepare(tmp_path, _standard_reader(block=gate), io_timeout=0.05)
    finally:
        gate.set()

    assert isinstance(result, BatchUnavailable)
    assert result.reason is ShadowSkipReason.TIME_LIMIT


def test_an_exhausted_budget_reads_nothing(tmp_path: Path) -> None:
    reader = _standard_reader()
    expired = Deadline(0.0)

    result = _prepare(tmp_path, reader, deadline=expired)

    assert isinstance(result, BatchUnavailable)
    assert result.reason is ShadowSkipReason.TIME_LIMIT
    assert reader.calls == []


def test_missing_coefficients_read_nothing(tmp_path: Path) -> None:
    reader = _standard_reader()

    result = _prepare(tmp_path, reader, config=_config(tmp_path, coefficients=None))

    assert isinstance(result, BatchUnavailable)
    assert result.reason is ShadowSkipReason.COEFFICIENTS_UNAVAILABLE
    assert reader.calls == []


def test_a_naive_clock_is_a_failure_reason_not_an_exception(tmp_path: Path) -> None:
    naive = BatchSpec(_BATCH, dt.datetime(2026, 10, 14, 10, 0), {}, _rec_id)

    result = prepare_batch_inputs(
        naive,
        _config(tmp_path),
        _standard_reader(),
        deadline=Deadline(30.0),
        io_timeout_seconds=1.0,
    )

    assert isinstance(result, BatchUnavailable)
    assert result.reason is ShadowSkipReason.COMPUTATION_FAILED


# =================================================================================================
# 候補
# =================================================================================================


def test_a_missing_candidate_recommendation_is_counted_and_left_out_not_filled(
    tmp_path: Path,
) -> None:
    reader = _standard_reader()
    del reader.recs[_rec_id("0002")]

    inputs = _ready(tmp_path, reader)

    assert [c.stock_code for c in inputs.candidates] == ["0001"]
    assert inputs.read_states["recommendations"] == "PARTIAL"


def test_a_stale_candidate_price_is_not_used(tmp_path: Path) -> None:
    reader = _standard_reader()
    reader.recs[_rec_id("0001")] = rec("0001", price_as_of_date=_STALE)

    inputs = _ready(tmp_path, reader)

    by_code = {c.stock_code: c for c in inputs.candidates}
    assert by_code["0001"].current_price is None  # HARD_STOP の価格は使わない(補わない)
    assert by_code["0002"].current_price == D("1000")


def test_a_candidate_that_fails_to_assemble_is_skipped_without_stopping_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = facts_from_recommendation

    def flaky(recommendation: Recommendation) -> Any:
        if recommendation.stock_code == "0001":
            raise ValueError("sentinel-failure-text")
        return real(recommendation)

    monkeypatch.setattr(_TARGET + "facts_from_recommendation", flaky)

    inputs = _ready(tmp_path, _standard_reader())

    assert [c.stock_code for c in inputs.candidates] == ["0002"]
    assert inputs.read_states["recommendations"] == "PARTIAL"


def test_the_candidate_raer_is_the_g1_result_and_the_detail_is_recorded(tmp_path: Path) -> None:
    inputs = _ready(tmp_path, _standard_reader())

    candidate = inputs.candidates[0]
    detail = inputs.details["0001"]
    assert candidate.raer.value is not None
    assert candidate.raer.comparison_key == RAER_FULL_COMPARISON_KEY
    assert detail.freshness_verdict == "NORMAL"
    assert detail.raer_in["fair_value_confidence"] == "MEDIUM"
    assert detail.raer_in["risk_flags_observed"] is True
    assert detail.raer_in["quality_score"] == "30.0"
    assert detail.raer_extra["coefficient_set_id"]
    assert set(detail.provenance) >= {"UPSIDE", "INCOME", "COMPANY_QUALITY", "BUSINESS_RISK"}


def test_a_candidate_without_risk_observation_and_quality_says_so_in_the_record(
    tmp_path: Path,
) -> None:
    reader = _standard_reader()
    reader.recs[_rec_id("0001")] = rec(
        "0001",
        required_margin_of_safety_entry=None,
        buy_score_input_facts={},  # 検出の記録も充足度つきの品質も無い
    )

    inputs = _ready(tmp_path, reader)

    detail = inputs.details["0001"]
    assert detail.raer_in["risk_flags_observed"] is False
    assert detail.raer_in["quality_score"] is None
    assert detail.provenance["COMPANY_QUALITY"] != "DERIVED"  # 導出できなかった理由の語
    assert detail.provenance["UPSIDE"] == "DERIVED"
    assert inputs.details["0002"].raer_in["risk_flags_observed"] is True


# =================================================================================================
# owner の計算
# =================================================================================================


def test_a_computed_owner_gives_a_valid_record_that_replays_and_leaks_nothing(
    tmp_path: Path,
) -> None:
    reader = _standard_reader(cashes={_SENTINEL_OWNER: cash(_SENTINEL_OWNER, "20000000")})
    inputs = _ready(tmp_path, reader)

    computed = _owner(inputs, reader)

    assert computed.result.outcome is ShadowOutcome.COMPUTED
    record = computed.record
    assert record is not None
    validate_items_record(record)
    assert json.loads(json.dumps(record)) == record
    assert replay_items_record(record).matched is True
    assert record["owner_ref"] == _ref()
    assert record["run"]["outcome"] == "COMPUTED"
    assert record["counts"]["candidates"] == 2
    allocated_in_record = sum(
        1 for row in record["candidates"] if row["allocation"]["decision"] == "ALLOCATED"
    )
    assert (
        computed.result.facts["allocated"] == allocated_in_record >= 1
    )  # 実際に配分が起きる日である(NO_ACTION でない)
    assert record["run"]["status"] == "ALLOCATED_SOME"
    assert record["cash"]["available_cash"] == "20000000"
    assert record["run"]["elapsed_ms"] == 12
    assert record["model"]["shadow_mode"] == "SHADOW"
    assert record["model"]["raer_model_version"].startswith("raer-combined-v1")
    assert record["cash"]["last_reconciled_at"] is not None  # 最終棚卸しを記録へ写す
    assert record["counts"]["cash_age_days"] == 3
    blob = json.dumps(record, ensure_ascii=False)
    assert _SENTINEL_OWNER not in blob  # owner の平文は出ない
    assert "テスト銘柄" not in blob  # 銘柄名は出ない
    assert set(computed.result.facts) == {"status", "candidates", "allocated"}


def test_the_record_has_no_holding_identifier_or_holding_stock_name(tmp_path: Path) -> None:
    """R1 の品質条件: 保有 ID(owner#銘柄)・保有の銘柄の名前・株数を記録に出さない。"""
    reader = _standard_reader()
    inputs = _ready(tmp_path, reader)

    record = _owner(inputs, reader).record

    assert record is not None
    blob = json.dumps(record, ensure_ascii=False)
    assert f"{_SENTINEL_OWNER}#" not in blob
    assert "holding_id" not in blob
    assert "9001" not in blob  # 保有の銘柄コードは記録に出ない(集計金額だけ)
    assert "100000" not in blob.replace("100000000", "")  # 株数(100,000 株)は出ない


def test_the_record_carries_the_candidate_rows_with_raer_inputs_and_provenance(
    tmp_path: Path,
) -> None:
    reader = _standard_reader()
    record = _owner(_ready(tmp_path, reader), reader).record

    assert record is not None
    row = record["candidates"][0]
    assert row["raer_in"]["upside_pct"] is not None
    assert row["raer_out"]["extra"]["coefficient_set_id"]
    assert row["provenance"]["UPSIDE"] == "DERIVED"
    assert row["price"]["freshness_verdict"] == "NORMAL"


def test_two_owners_holding_the_same_stock_do_not_mix_their_shares(tmp_path: Path) -> None:
    reader = _standard_reader(
        holdings=[holding("owner-a", "9001", 1000), holding("owner-b", "9001", 3000)],
        cashes={"owner-a": cash("owner-a"), "owner-b": cash("owner-b")},
    )
    inputs = _ready(tmp_path, reader)

    a = _owner(inputs, reader, "owner-a").record
    b = _owner(inputs, reader, "owner-b").record

    assert a is not None and b is not None
    assert a["portfolio"]["portfolio_value"] == "1000000"
    assert b["portfolio"]["portfolio_value"] == "3000000"
    assert a["owner_ref"] != b["owner_ref"]


def test_an_owner_without_holdings_still_computes_with_no_holdings_basis(
    tmp_path: Path,
) -> None:
    reader = _standard_reader(cashes={"owner-x": cash("owner-x")})
    inputs = _ready(tmp_path, reader)

    computed = _owner(inputs, reader, "owner-x")

    assert computed.result.outcome is ShadowOutcome.COMPUTED
    assert computed.record is not None
    assert computed.record["counts"]["portfolio_value_basis"] == "NO_HOLDINGS"


def test_a_holding_without_a_recommendation_makes_the_exposure_unavailable_not_zero(
    tmp_path: Path,
) -> None:
    reader = _standard_reader(holdings=[holding(_SENTINEL_OWNER, "7777", 100)])
    inputs = _ready(tmp_path, reader)

    record = _owner(inputs, reader).record

    assert record is not None
    assert record["portfolio"]["portfolio_value"] is None  # 価格が無い(補わない)
    assert record["portfolio"]["high_risk_exposure"] is None
    assert record["counts"]["holdings_price_missing"] == 1
    assert record["counts"]["holdings_risk_unknown"] == 1  # 推奨が無い = 印を観測できていない


def test_a_stale_holding_price_is_not_used(tmp_path: Path) -> None:
    reader = _standard_reader()
    reader.recs[_rec_id("9001")] = rec("9001", price_as_of_date=_STALE)
    inputs = _ready(tmp_path, reader)

    record = _owner(inputs, reader).record

    assert record is not None
    assert record["portfolio"]["portfolio_value"] is None
    assert record["counts"]["holdings_price_missing"] == 1


def test_an_old_style_risk_record_leaves_every_word_unobserved(tmp_path: Path) -> None:
    """検出の記録が無い旧い推奨 = 観測できなかった語を区別できない → high-risk は算出不能。"""
    reader = _standard_reader()
    reader.recs[_rec_id("9001")] = rec(
        "9001", buy_score_input_facts={"common_quality_shadow": _quality()}
    )
    inputs = _ready(tmp_path, reader)

    record = _owner(inputs, reader).record

    assert record is not None
    assert record["portfolio"]["portfolio_value"] == "100000000"
    assert record["portfolio"]["high_risk_exposure"] is None
    assert record["counts"]["holdings_risk_unknown"] == 1


def test_an_observed_detection_record_gives_a_high_risk_exposure(tmp_path: Path) -> None:
    reader = _standard_reader()
    inputs = _ready(tmp_path, reader)

    record = _owner(inputs, reader).record

    assert record is not None
    assert record["portfolio"]["high_risk_exposure"] == "0"
    assert record["counts"]["holdings_risk_unknown"] == 0


def test_a_detected_high_risk_word_counts_the_holding_value(tmp_path: Path) -> None:
    reader = _standard_reader()
    flagged = {
        "version": 1,
        "flags": ["small_cap_or_low_liquidity"],
        "observed": {"earnings_date": True, "liquidity": True, "industry": True, "eps": True},
    }
    reader.recs[_rec_id("9001")] = rec(
        "9001",
        buy_score_input_facts={"common_quality_shadow": _quality(), "raer_risk_detection": flagged},
    )
    inputs = _ready(tmp_path, reader)

    record = _owner(inputs, reader).record

    assert record is not None
    assert record["portfolio"]["high_risk_exposure"] == "100000000"


# --- 買付余力の各状態 -----------------------------------------------------------------------------


def _denied() -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetItem")


@pytest.mark.parametrize(
    ("cashes", "reason", "state"),
    [
        ({}, ShadowSkipReason.CASH_NOT_REGISTERED, "NOT_REGISTERED"),
        ({_SENTINEL_OWNER: _denied()}, ShadowSkipReason.IAM_MISSING, "IAM_MISSING"),
        ({_SENTINEL_OWNER: RuntimeError("boom")}, ShadowSkipReason.COMPUTATION_FAILED, "ERROR"),
    ],
)
def test_an_unavailable_cash_gives_a_header_only_record_with_the_reason(
    tmp_path: Path, cashes: dict[str, Any], reason: ShadowSkipReason, state: str
) -> None:
    reader = _standard_reader(cashes=cashes)
    inputs = _ready(tmp_path, reader)

    computed = _owner(inputs, reader)

    assert computed.result.outcome is ShadowOutcome.SKIPPED
    assert computed.result.reason is reason
    record = computed.record
    assert record is not None
    validate_items_record(record)
    assert record["run"]["outcome"] == "SKIPPED"
    assert record["run"]["skip_reason"] == reason.value
    assert record["run"]["read_states"]["cash"] == state
    assert record["candidates"] == []
    assert record["cash"]["available_cash"] is None  # 0 円にしない


def test_a_slow_cash_read_is_cut_off(tmp_path: Path) -> None:
    gate = threading.Event()

    class SlowCash(FakeReader):
        def read_available_cash(self, owner: str) -> AvailableCash | None:
            gate.wait(5)
            return super().read_available_cash(owner)

    reader = SlowCash(
        holdings=[holding(_SENTINEL_OWNER, "9001")],
        recs=_standard_reader().recs,
        cashes={_SENTINEL_OWNER: cash(_SENTINEL_OWNER)},
    )
    inputs = _ready(tmp_path, reader)
    try:
        computed = compute_owner(
            inputs,
            _SENTINEL_OWNER,
            _ref(),
            reader=reader,
            deadline=Deadline(30.0),
            io_timeout_seconds=0.05,
        )
    finally:
        gate.set()

    assert computed.result.reason is ShadowSkipReason.TIME_LIMIT
    assert computed.record is not None
    assert computed.record["run"]["skip_reason"] == "TIME_LIMIT"


def test_an_exhausted_budget_does_not_read_the_cash(tmp_path: Path) -> None:
    reader = _standard_reader()
    inputs = _ready(tmp_path, reader)
    before = len(reader.calls)

    computed = _owner(inputs, reader, deadline=Deadline(0.0))

    assert computed.result.reason is ShadowSkipReason.TIME_LIMIT
    assert len(reader.calls) == before
    assert computed.record is not None
    assert computed.record["run"]["read_states"]["cash"] == "TIMEOUT"


def test_a_failure_inside_the_computation_is_a_failed_header_with_the_type_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _standard_reader()
    inputs = _ready(tmp_path, reader)

    def boom(**_kwargs: Any) -> Any:
        raise ValueError("sentinel-failure-text")

    monkeypatch.setattr(_TARGET + "assemble_allocation_inputs", boom)

    computed = _owner(inputs, reader)

    assert computed.result.outcome is ShadowOutcome.FAILED
    assert computed.result.reason is ShadowSkipReason.COMPUTATION_FAILED
    assert dict(computed.result.facts) == {"error_type": "ValueError"}
    assert computed.record is not None
    assert computed.record["run"]["outcome"] == "FAILED"
    assert "sentinel-failure-text" not in json.dumps(computed.record)
    # 補完した買付余力は記録に残る(読めていた事実。失敗は計算側)
    assert computed.record["cash"]["available_cash"] == "5000000"


def test_if_even_the_header_cannot_be_built_only_the_frame_result_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _standard_reader()
    inputs = _ready(tmp_path, reader)

    def boom(**_kwargs: Any) -> Any:
        raise ValueError("x")

    monkeypatch.setattr(_TARGET + "assemble_allocation_inputs", boom)
    monkeypatch.setattr(_TARGET + "build_header_record", boom)

    computed = _owner(inputs, reader)  # 例外を送出しない

    assert computed.record is None
    assert computed.result.outcome is ShadowOutcome.FAILED


# =================================================================================================
# 構造(注入だけ・時計なし・dormant)
# =================================================================================================


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
    return names


def test_the_module_touches_no_repository_aws_or_clock() -> None:
    imported = _imported_modules(_MODULE_PATH)
    assert not [m for m in imported if "repository" in m or "infrastructure" in m]
    assert not imported & {"boto3", "os", "pathlib", "requests", "time"}
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"now", "today", "utcnow", "getenv", "open"}
        if isinstance(node, ast.Name):
            assert node.id not in {"open", "print"}


def test_the_module_imports_only_the_keyed_reference_type_from_the_identifier_module() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "jstock_advisor.domain.entities.identifier_ref"
        for alias in node.names
    }
    assert imported == {"KeyedOwnerRef"}  # 符号は呼び出し側(F2-2b)が作って渡す。鍵・kv0 に触れない


def test_the_module_is_imported_only_by_the_loop_and_the_wiring() -> None:
    """F2-2b の配線後: 観測のループ(型)と配線(prepare / compute の束縛)だけが import する。"""
    importers = []
    for path in _PACKAGE.rglob("*.py"):
        if path == _MODULE_PATH:
            continue
        if any(m.endswith("allocation_shadow_compute") for m in _imported_modules(path)):
            importers.append(path.relative_to(_PACKAGE).as_posix())
    assert sorted(importers) == [
        "lambda_handlers/allocation_shadow_wiring.py",
        "services/allocation_shadow_runner.py",
    ]


def test_the_reader_protocol_has_exactly_the_three_reads() -> None:
    methods = {name for name in vars(compute.ShadowInputReader) if not name.startswith("_")} - {
        "read_holdings",
        "read_recommendations",
        "read_available_cash",
    }
    assert methods == set()


# =================================================================================================
# coefficient_set_id(#602 …6159406045 の USER 条件 4): モデル版と係数の束の整合
# =================================================================================================


def _set_id_of(config: ComputeConfig) -> str:
    assert config.coefficients is not None
    return config.coefficients.coefficient_set_id


def _candidate_set_ids(record: dict[str, Any]) -> set[str]:
    return {row["raer_out"]["extra"]["coefficient_set_id"] for row in record["candidates"]}


def test_the_record_carries_the_coefficient_set_id_of_the_same_bundle_as_the_model_version(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    reader = _standard_reader(cashes={_SENTINEL_OWNER: cash(_SENTINEL_OWNER, "20000000")})
    inputs = _ready(tmp_path, reader, config=config)

    record = _owner(inputs, reader).record

    assert record is not None
    assert record["model"]["coefficient_set_id"] == _set_id_of(config)
    assert record["model"]["raer_model_version"].startswith("raer-combined-v1")
    # 候補ごとの RAER の extra も同じ束の id(モデル版と束の id が食い違う記録を作らない)
    assert _candidate_set_ids(record) == {_set_id_of(config)}


def test_changing_one_coefficient_changes_both_the_record_id_and_every_candidate_id(
    tmp_path: Path,
) -> None:
    shipped = _config(tmp_path)
    assert shipped.coefficients is not None
    changed_coefficients = dataclasses.replace(
        shipped.coefficients,
        quality_deduction_max=shipped.coefficients.quality_deduction_max - D("0.01"),
    )
    changed = _config(tmp_path, coefficients=changed_coefficients)
    reader = _standard_reader(cashes={_SENTINEL_OWNER: cash(_SENTINEL_OWNER, "20000000")})

    before = _owner(_ready(tmp_path, reader, config=shipped), reader).record
    after = _owner(_ready(tmp_path, reader, config=changed), reader).record

    assert before is not None and after is not None
    assert _set_id_of(shipped) != _set_id_of(changed)
    assert before["model"]["coefficient_set_id"] != after["model"]["coefficient_set_id"]
    assert after["model"]["coefficient_set_id"] == _set_id_of(changed)
    assert _candidate_set_ids(after) == {_set_id_of(changed)}
    assert _candidate_set_ids(before).isdisjoint(_candidate_set_ids(after))
    # モデル版は係数だけでは変わらない(束の id が変わるのは係数の値による)
    assert before["model"]["raer_model_version"] == after["model"]["raer_model_version"]


@pytest.mark.parametrize(
    "cashes",
    [
        {},  # 買付余力が未登録
        {_SENTINEL_OWNER: RuntimeError("boom")},  # 読取の失敗
    ],
)
def test_a_header_only_day_keeps_the_coefficient_set_id(
    tmp_path: Path, cashes: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    reader = _standard_reader(cashes=cashes)

    computed = _owner(_ready(tmp_path, reader, config=config), reader)

    assert computed.result.outcome is ShadowOutcome.SKIPPED
    assert computed.record is not None
    assert computed.record["candidates"] == []
    assert computed.record["model"]["coefficient_set_id"] == _set_id_of(config)
