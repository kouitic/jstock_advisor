"""Issue #603 / PR-F2-1: 項目つきの監査記録 `allocation_shadow_items` の契約テスト。

組み立て・allowlist・replay を固定する。純粋関数だけで、時計・I/O・永続化・配線に触れない。
どこからも import されない(dormant)。

## 何を固定するのか

```
・記録の形: 全ての入れ子のキーが allowlist と完全一致(未知・不足を再帰で拒否)。値は ASCII の
  識別子・コード・10 進数の文字列・真偽・有界の int だけ(float・日本語・空白・平文の owner を拒否)
・保存範囲(docs #930 の USER 承認): 保有由来は集計金額だけ。個別の保有 ID・取得単価・損益・
  所有者の実名・保有の銘柄別の株数・保有の一覧は、builder の引数にもキーにも値にも現れない
  (sentinel を入れて再帰で確認)
・replay: 記録の入力部から配分を再計算し、記録の出力部と一致する。改ざん・欠落は項目の位置だけで報告
・大きさ: 候補の記録は上限(超過は件数だけ。配分した候補を優先)。全体の大きさも検証
・SKIPPED・FAILED の日は header だけの記録(なぜ算出されなかったかが残る)
```
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import datetime as dt
import inspect
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import BuyAction, BuyIndustrySector, ConfidenceLevel
from jstock_advisor.domain.entities.identifier_ref import (
    KeyedReferenceRequiredError,
    KeyMaterial,
    keyed_owner_ref,
    owner_ref,
)
from jstock_advisor.domain.signals import allocation_shadow_record as rec
from jstock_advisor.domain.signals import portfolio_allocation_types as pt
from jstock_advisor.domain.signals.allocation_inputs import (
    AllocationInputs,
    CandidateRecord,
    CashInput,
    HoldingRecord,
    RiskObserved,
    assemble_allocation_inputs,
)
from jstock_advisor.domain.signals.allocation_policy_config import (
    AllocationPolicyBundle,
    load_allocation_policy,
)
from jstock_advisor.domain.signals.allocation_shadow_record import (
    CandidateDetail,
    CashSnapshot,
    RecordMeta,
    RecordSchemaError,
    ReplayNotPossibleError,
    build_header_record,
    build_items_record,
    replay_items_record,
    validate_items_record,
)
from jstock_advisor.domain.valuation.raer import (
    MODEL_VERSION,
    AxisCode,
    RaerResult,
    RaerState,
    make_comparison_key,
)

D = Decimal
S = BuyIndustrySector
_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "domain" / "signals" / "allocation_shadow_record.py"
_JST = dt.timezone(dt.timedelta(hours=9))
_NOW = dt.datetime(2026, 10, 14, 9, 5, tzinfo=_JST)
_ALL_AXES = frozenset(a.value for a in AxisCode)
KEY = make_comparison_key(MODEL_VERSION, _ALL_AXES)
OWNER = "owner-a"
UNIT = 100


def raer(value: str = "20") -> RaerResult:
    return RaerResult(
        state=RaerState.AVAILABLE,
        value=D(value),
        confidence=ConfidenceLevel.MEDIUM,
        risk_flags=frozenset(),
        risk_flags_available=True,
        model_version=MODEL_VERSION,
        comparison_key=KEY,
        axes_applied=_ALL_AXES,
        axes_not_applied=frozenset(),
        reason_codes=(),
    )


def _policy_with_key(directory: Path, comparison_key: str | None) -> AllocationPolicyBundle:
    text = (_REPO / "config" / "allocation_policy.yaml").read_text(encoding="utf-8")
    value = "null" if comparison_key is None else f'"{comparison_key}"'
    text, count = re.subn(r"(?m)^comparison_key:.*$", f"comparison_key: {value}", text)
    assert count == 1
    (directory / "allocation_policy.yaml").write_text(text, encoding="utf-8")
    return load_allocation_policy(directory, minimum_trading_unit=UNIT)


def decided_bundle(tmp_path: Path) -> AllocationPolicyBundle:
    return _policy_with_key(tmp_path, KEY)


_TEST_KEY = KeyMaterial(key_version=1, key=b"t" * 32)  # テスト用の鍵(秘密ではない)


def _keyed(owner: str):  # type: ignore[no-untyped-def]
    return keyed_owner_ref(owner, _TEST_KEY)


COEFFICIENT_SET_ID = "cset-test-0123456789abcdef-v1"
META = RecordMeta(
    batch_id="buy-candidates-20261014",
    business_date=dt.date(2026, 10, 14),
    owner_ref=_keyed(OWNER),
    shadow_mode="SHADOW",
    raer_model_version=MODEL_VERSION,
    coefficient_set_id=COEFFICIENT_SET_ID,
    elapsed_ms=1234,
    read_states={"holdings": "OK", "recommendations": "OK", "cash": "AVAILABLE"},
)
CASH = CashSnapshot(D("5000000"), _NOW - dt.timedelta(days=3), _NOW - dt.timedelta(days=1))


def scenario(
    tmp_path: Path,
    codes: list[tuple[str, str]] | None = None,
    holdings: list[HoldingRecord] | None = None,
) -> tuple[AllocationPolicyBundle, AllocationInputs, pt.AllocationResult]:
    bundle = decided_bundle(tmp_path)
    codes = codes if codes is not None else [("0001", "30"), ("0002", "25"), ("0003", "9")]
    candidates = [
        CandidateRecord(
            stock_code=code,
            buy_action=BuyAction.BUY,
            current_price=D("1000"),
            sector=S.GENERAL,
            trading_unit=None,
            raer=raer(value),
        )
        for code, value in codes
    ]
    held = (
        holdings
        if holdings is not None
        else [
            HoldingRecord(
                owner=OWNER,
                stock_code="9001",
                shares=10000,
                current_price=D("1000"),
                sector=S.BANK,
                risk=RiskObserved(flags=frozenset(), unobserved=frozenset()),
            )
        ]
    )
    inputs = assemble_allocation_inputs(
        owner=OWNER,
        now=_NOW,
        candidates=candidates,
        holdings=held,
        cash=CashInput(CASH.amount, CASH.last_reconciled_at),
        bundle=bundle,
    )
    return bundle, inputs, inputs.optimize(bundle)


def build(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    bundle, inputs, result = scenario(tmp_path, **kwargs)
    return build_items_record(
        meta=META, bundle=bundle, cash=CASH, inputs=inputs, result=result, details={}
    )


# --- 形と自己検証 ---------------------------------------------------------------------------


def test_a_computed_day_builds_a_valid_json_serializable_record(tmp_path: Path) -> None:
    record = build(tmp_path)

    validate_items_record(record)
    assert json.loads(json.dumps(record)) == record
    assert record["schema_version"] == rec.SCHEMA_VERSION == 1
    assert record["record_kind"] == "allocation_shadow_items"
    assert record["business_date"] == "2026-10-14"
    assert record["owner_ref"] == _keyed(OWNER)
    assert record["run"]["outcome"] == "COMPUTED"
    assert record["run"]["status"] == "ALLOCATED_SOME"


def test_the_record_is_deterministic(tmp_path: Path) -> None:
    first = build(tmp_path)
    second = build(tmp_path)

    assert first == second
    assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)


def test_the_record_does_not_depend_on_the_input_order(tmp_path: Path) -> None:
    forward = build(tmp_path, codes=[("0001", "30"), ("0002", "25"), ("0003", "9")])
    backward = build(tmp_path, codes=[("0003", "9"), ("0002", "25"), ("0001", "30")])

    assert forward == backward


def test_the_recorded_allocation_numbers_are_the_results_numbers(tmp_path: Path) -> None:
    bundle, inputs, result = scenario(tmp_path)
    record = build_items_record(
        meta=META, bundle=bundle, cash=CASH, inputs=inputs, result=result, details={}
    )

    by_code = {c["stock_code"]: c for c in record["candidates"]}
    for item in result.allocations:
        section = by_code[item.stock_code]["allocation"]
        assert section["shares"] == item.shares
        assert section["amount"] == format(item.amount, "f")
        assert section["reason_code"] == item.reason_code.value
        assert section["rank"] == item.rank
    assert record["portfolio"]["portfolio_value"] == "10000000"
    assert record["portfolio"]["sector_exposure"] == {"BANK": "10000000"}
    assert record["portfolio"]["high_risk_exposure"] == "0"
    assert record["cash"]["available_cash"] == "5000000"


def test_the_recording_order_is_allocated_first_then_by_rank(tmp_path: Path) -> None:
    record = build(tmp_path)

    allocated_flags = [
        c["allocation"] is not None and c["allocation"]["decision"] == "ALLOCATED"
        for c in record["candidates"]
    ]
    assert allocated_flags == sorted(allocated_flags, reverse=True)
    assert [c["seq"] for c in record["candidates"]] == list(range(1, len(record["candidates"]) + 1))


def test_money_and_ratios_are_exact_decimal_strings_without_exponents(tmp_path: Path) -> None:
    assert rec._dec(D("1E+3")) == "1000"
    assert rec._dec(D("0.150")) == "0.150"
    assert rec._dec(None) is None
    with pytest.raises(RecordSchemaError):
        rec._dec(D("NaN"))
    record = build(tmp_path)
    assert record["policy"]["max_single_stock_ratio"] == "0.15"
    assert record["policy"]["tier_quantity_caps"] == {
        "ENTRY": "0.33",
        "STANDARD": "0.67",
        "STRONG": "1.0",
    }


# --- allowlist: 全ての入れ子のキーを再帰で固定 -------------------------------------------------


def _dict_paths(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """辞書(入れ子・リストの中を含む)の場所を全て返す。"""
    found: list[tuple[Any, ...]] = []
    if isinstance(value, dict):
        found.append(path)
        for key, inner in value.items():
            found.extend(_dict_paths(inner, (*path, key)))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            found.extend(_dict_paths(inner, (*path, index)))
    return found


def _at(record: Any, path: tuple[Any, ...]) -> Any:
    for step in path:
        record = record[step]
    return record


def test_an_unknown_key_is_rejected_at_every_object_level(tmp_path: Path) -> None:
    record = build(tmp_path)
    paths = _dict_paths(record)
    assert len(paths) >= 12  # 最上位・各節・候補・入れ子(raer_out.extra・attrs ほか)を覆う

    for path in paths:
        mutated = copy.deepcopy(record)
        _at(mutated, path)["unknown_key"] = "x"
        with pytest.raises(RecordSchemaError):
            validate_items_record(mutated)


def test_a_missing_key_is_rejected_at_every_object_level_except_maps(tmp_path: Path) -> None:
    record = build(tmp_path)
    map_paths = {
        ("run", "read_states"),
        ("counts", "exclusions"),
        ("portfolio", "sector_exposure"),
        ("policy", "tier_quantity_caps"),
        ("candidates", 0, "provenance"),
    }
    for path in _dict_paths(record):
        if path in map_paths:
            continue
        for key in list(_at(record, path)):
            mutated = copy.deepcopy(record)
            del _at(mutated, path)[key]
            with pytest.raises(RecordSchemaError):
                validate_items_record(mutated)


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        (("business_date",), "2026/10/14"),
        (("business_date",), 20261014),
        (("batch_id",), "日本語のバッチ"),
        (("batch_id",), "has space"),
        (("batch_id",), "x" * 300),
        (("owner_ref",), "owner-a"),
        (("owner_ref",), "sha256:abcd1234"),
        (("owner_ref",), owner_ref("owner-a")),  # kv0(鍵なし)は書かない
        (("owner_ref",), "kv1:" + "a" * 24),  # 鍵の版が無い
        (("owner_ref",), "kv1.0:" + "a" * 24),
        (("model", "config_hash"), "ハッシュ"),
        (("cash", "available_cash"), 5000000),
        (("cash", "available_cash"), 5000000.5),
        (("cash", "available_cash"), "5,000,000"),
        (("cash", "available_cash"), "1e6"),
        (("cash", "last_reconciled_at"), "2026-10-11T09:05:00"),
        (("run", "elapsed_ms"), 1.5),
        (("run", "elapsed_ms"), True),
        (("run", "elapsed_ms"), 10**15),
        (("run", "outcome"), "DONE"),
        (("run", "status"), "OK"),
        (("run", "read_states"), {"holdings": "良好"}),
        (("run", "read_states"), {"Holdings Read": "OK"}),
        (("counts", "add_on_gate_applied"), "false"),
        (("counts", "exclusions"), {"PRICE_UNAVAILABLE": 1.0}),
        (("portfolio", "portfolio_value"), 10000000.0),
        (("portfolio", "sector_exposure"), {"銀行": "1"}),
        (("candidates", 0, "stock_code"), "トヨタ"),
        (("candidates", 0, "stock_code"), "7203 トヨタ自動車"),
        (("candidates", 0, "attrs", "buy_action"), "AGGRESSIVE_BUY"),
        (("candidates", 0, "attrs", "sector"), "銀行業"),
        (("candidates", 0, "price", "current_price"), 1000.0),
        (("candidates", 0, "raer_out", "state"), "UNKNOWN"),
        (("candidates", 0, "raer_out", "value"), 20.0),
        (("candidates", 0, "raer_out", "reason_codes"), ["説明文です"]),
        (("candidates", 0, "provenance"), {"UPSIDE": "free text"}),
    ],
)
def test_a_value_of_the_wrong_form_is_rejected(
    tmp_path: Path, path: tuple[Any, ...], bad: Any
) -> None:
    record = build(tmp_path)
    parent = _at(record, path[:-1])
    if path == ("candidates", 0, "provenance"):
        parent[path[-1]] = bad
    else:
        parent[path[-1]] = bad

    with pytest.raises(RecordSchemaError):
        validate_items_record(record)


def test_an_error_names_only_the_place_never_the_value(tmp_path: Path) -> None:
    record = build(tmp_path)
    record["candidates"][0]["stock_code"] = "SENTINEL-日本語-12345"

    with pytest.raises(RecordSchemaError) as caught:
        validate_items_record(record)

    assert "SENTINEL" not in str(caught.value) and "12345" not in str(caught.value)
    assert "candidates[0].stock_code" in str(caught.value)


def test_the_candidate_list_has_a_hard_limit_in_the_schema(tmp_path: Path) -> None:
    record = build(tmp_path)
    record["candidates"] = [copy.deepcopy(record["candidates"][0])] * (
        rec.MAX_CANDIDATES_RECORDED + 1
    )

    with pytest.raises(RecordSchemaError):
        validate_items_record(record)


def test_the_whole_record_has_a_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = build(tmp_path)
    size = rec.record_size_bytes(record)
    assert size < rec.MAX_RECORD_BYTES

    monkeypatch.setattr(rec, "MAX_RECORD_BYTES", size - 1)
    with pytest.raises(RecordSchemaError):
        validate_items_record(record)
    monkeypatch.setattr(rec, "MAX_RECORD_BYTES", size)
    validate_items_record(record)


# --- 保存範囲(USER 承認: 保有由来は集計金額だけ)-----------------------------------------------


_FORBIDDEN_KEY_WORDS = (
    "holding_id",
    "holding_ids",
    "acquisition",
    "average_purchase",
    "purchase_price",
    "profit",
    "pnl",
    "loss",
    "shares_held",
    "held_shares",
    "owner",
    "name",
    "holdings_list",
)


def _all_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            keys.append(str(key))
            keys.extend(_all_keys(inner))
    elif isinstance(value, list):
        for inner in value:
            keys.extend(_all_keys(inner))
    return keys


def test_no_key_names_an_individual_holding_acquisition_price_profit_or_owner(
    tmp_path: Path,
) -> None:
    keys = {k.lower() for k in _all_keys(build(tmp_path))}

    offenders = sorted(k for k in keys if any(word in k for word in _FORBIDDEN_KEY_WORDS))
    # owner を含む名前は『符号』(owner_ref)と『owner が保有する候補の件数』(集計の件数)だけ
    assert offenders == ["owner_holds_candidate", "owner_ref"]


def test_sentinel_holdings_never_reach_the_record(tmp_path: Path) -> None:
    """保有の銘柄コード・銘柄別の株数・owner の実名が、記録のどこにも現れない(再帰で全文を走査)。"""
    sentinel_code = "SENTCODE9999"
    sentinel_owner = "sentinel-owner-zz"
    holdings = [
        HoldingRecord(
            owner=sentinel_owner,
            stock_code=sentinel_code,
            shares=7771,
            current_price=D("1003"),
            sector=S.BANK,
            risk=RiskObserved(flags=frozenset(), unobserved=frozenset()),
        )
    ]
    bundle = decided_bundle(tmp_path)
    inputs = assemble_allocation_inputs(
        owner=sentinel_owner,
        now=_NOW,
        candidates=[
            CandidateRecord(
                stock_code="0001",
                buy_action=BuyAction.BUY,
                current_price=D("1000"),
                sector=S.GENERAL,
                trading_unit=None,
                raer=raer(),
            )
        ],
        holdings=holdings,
        cash=CashInput(CASH.amount, CASH.last_reconciled_at),
        bundle=bundle,
    )
    meta = RecordMeta(
        batch_id=META.batch_id,
        business_date=META.business_date,
        owner_ref=_keyed(sentinel_owner),
        shadow_mode="SHADOW",
        raer_model_version=MODEL_VERSION,
        coefficient_set_id=COEFFICIENT_SET_ID,
        elapsed_ms=1,
        read_states=META.read_states,
    )

    record = build_items_record(
        meta=meta, bundle=bundle, cash=CASH, inputs=inputs, result=inputs.optimize(bundle)
    )

    text = json.dumps(record, ensure_ascii=False)
    assert sentinel_code not in text
    assert sentinel_owner not in text
    assert "7771" not in text  # 銘柄別の株数
    # 集計金額(総額・業種別)は許可された範囲として現れる
    assert record["portfolio"]["portfolio_value"] == str(7771 * 1003)
    assert record["portfolio"]["sector_exposure"] == {"BANK": str(7771 * 1003)}


def test_the_builder_has_no_parameter_that_could_carry_a_holding_detail() -> None:
    allowed = {"meta", "bundle", "cash", "inputs", "result", "details"}
    assert set(inspect.signature(build_items_record).parameters) == allowed
    assert set(inspect.signature(build_header_record).parameters) == {
        "meta",
        "bundle",
        "cash",
        "outcome",
        "skip_reason",
    }
    meta_fields = set(inspect.signature(RecordMeta).parameters)
    assert meta_fields == {
        "batch_id",
        "business_date",
        "owner_ref",
        "shadow_mode",
        "raer_model_version",
        "coefficient_set_id",
        "elapsed_ms",
        "read_states",
        "add_on_gate_applied",
    }


def test_the_owner_parameter_is_the_reference_type_not_a_plain_str() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    annotations = {
        node.target.id: ast.unparse(node.annotation)
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "RecordMeta"
        for node in cls.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert annotations["owner_ref"] == "KeyedOwnerRef"


@pytest.mark.parametrize("bad_ref", [OWNER, owner_ref(OWNER), "kv1:" + "a" * 24])
def test_a_plain_or_unkeyed_owner_is_rejected_by_the_builder(tmp_path: Path, bad_ref: str) -> None:
    """平文の owner・kv0(鍵なし)・鍵の版のない符号は、実行時にも書かせない(USER 決定 #925)。"""
    bundle, inputs, result = scenario(tmp_path)
    bad = RecordMeta(
        batch_id=META.batch_id,
        business_date=META.business_date,
        owner_ref=bad_ref,  # type: ignore[arg-type]
        shadow_mode="SHADOW",
        raer_model_version=MODEL_VERSION,
        coefficient_set_id=COEFFICIENT_SET_ID,
        elapsed_ms=1,
        read_states=META.read_states,
    )

    with pytest.raises(KeyedReferenceRequiredError):
        build_items_record(meta=bad, bundle=bundle, cash=CASH, inputs=inputs, result=result)
    with pytest.raises(KeyedReferenceRequiredError):
        build_header_record(
            meta=bad, bundle=bundle, cash=CASH, outcome="SKIPPED", skip_reason="X_REASON"
        )


def test_the_raer_extras_and_inputs_are_allowlisted(tmp_path: Path) -> None:
    bundle, inputs, result = scenario(tmp_path)
    good = CandidateDetail(
        raer_in={"upside_pct": "12.5", "risk_flags_observed": True, "trend_weights": {"a": "0.5"}},
        raer_extra={
            "contributions": {"FAIR_VALUE_CONFIDENCE": "-1.2"},
            "spec_flags": ["TIME_MISMATCH"],
        },
        provenance={"UPSIDE": "DERIVED", "BUSINESS_RISK": "MARGIN_NOT_COMPUTED"},
    )
    record = build_items_record(
        meta=META, bundle=bundle, cash=CASH, inputs=inputs, result=result, details={"0001": good}
    )
    section = next(c for c in record["candidates"] if c["stock_code"] == "0001")
    assert section["raer_in"]["upside_pct"] == "12.5"
    assert section["raer_out"]["extra"]["contributions"] == {"FAIR_VALUE_CONFIDENCE": "-1.2"}
    assert section["provenance"] == {"UPSIDE": "DERIVED", "BUSINESS_RISK": "MARGIN_NOT_COMPUTED"}

    for bad in (
        CandidateDetail(raer_in={"company_name": "x"}),
        CandidateDetail(raer_extra={"note": "x"}),
        CandidateDetail(raer_in={"upside_pct": 12.5}),  # float
        CandidateDetail(raer_extra={"contributions": {"x": "かなり大きい"}}),
        CandidateDetail(provenance={"UPSIDE": "説明"}),
    ):
        with pytest.raises(RecordSchemaError):
            build_items_record(
                meta=META,
                bundle=bundle,
                cash=CASH,
                inputs=inputs,
                result=result,
                details={"0001": bad},
            )


# --- 切り詰め(大きさ)-----------------------------------------------------------------------


def test_candidates_beyond_the_limit_are_counted_not_recorded_and_allocated_ones_stay(
    tmp_path: Path,
) -> None:
    many = [(f"{i:04d}", str(5 + (i % 40))) for i in range(1, rec.MAX_CANDIDATES_RECORDED + 40)]
    record = build(tmp_path, codes=many)

    counts = record["counts"]
    assert counts["candidates"] == len(many)
    assert counts["candidates_recorded"] == rec.MAX_CANDIDATES_RECORDED == len(record["candidates"])
    assert counts["candidates_truncated"] == len(many) - rec.MAX_CANDIDATES_RECORDED
    allocated_total = sum(
        1
        for c in record["candidates"]
        if c["allocation"] is not None and c["allocation"]["decision"] == "ALLOCATED"
    )
    assert allocated_total >= 1
    assert rec.record_size_bytes(record) <= rec.MAX_RECORD_BYTES
    validate_items_record(record)
    with pytest.raises(ReplayNotPossibleError):
        replay_items_record(record)


def test_the_limits_are_below_the_dynamodb_item_limit() -> None:
    assert rec.MAX_RECORD_BYTES < 400 * 1024
    assert rec.MAX_CANDIDATES_RECORDED > 0


# --- header だけの記録(SKIPPED・FAILED)---------------------------------------------------------


@pytest.mark.parametrize("outcome", ["SKIPPED", "FAILED"])
def test_a_skipped_or_failed_day_records_a_header_with_the_reason(
    tmp_path: Path, outcome: str
) -> None:
    bundle = decided_bundle(tmp_path)
    record = build_header_record(
        meta=META,
        bundle=bundle,
        cash=CashSnapshot(None, None, None),
        outcome=outcome,
        skip_reason="CASH_NOT_REGISTERED",
    )

    validate_items_record(record)
    assert record["run"]["outcome"] == outcome
    assert record["run"]["skip_reason"] == "CASH_NOT_REGISTERED"
    assert record["candidates"] == []
    assert record["cash"]["available_cash"] is None
    assert record["counts"]["candidates_recorded"] == 0
    with pytest.raises(ReplayNotPossibleError):
        replay_items_record(record)


def test_a_header_cannot_claim_a_computed_outcome(tmp_path: Path) -> None:
    with pytest.raises(RecordSchemaError):
        build_header_record(
            meta=META,
            bundle=decided_bundle(tmp_path),
            cash=CashSnapshot(None, None, None),
            outcome="COMPUTED",
            skip_reason="X",
        )


def test_a_skip_reason_must_be_a_code_not_free_text(tmp_path: Path) -> None:
    with pytest.raises(RecordSchemaError):
        build_header_record(
            meta=META,
            bundle=decided_bundle(tmp_path),
            cash=CashSnapshot(None, None, None),
            outcome="SKIPPED",
            skip_reason="買付余力が未登録でした",
        )


def test_an_undecided_key_records_a_ranking_only_unavailable_day(tmp_path: Path) -> None:
    """比較の鍵が未決(comparison_key = null)の日は、算出不能(POLICY_INCOMPLETE)として記録される。"""
    shipped = _policy_with_key(tmp_path, None)
    inputs = assemble_allocation_inputs(
        owner=OWNER,
        now=_NOW,
        candidates=[
            CandidateRecord(
                stock_code="0001",
                buy_action=BuyAction.BUY,
                current_price=D("1000"),
                sector=S.GENERAL,
                trading_unit=None,
                raer=raer(),
            )
        ],
        holdings=[
            HoldingRecord(
                owner=OWNER,
                stock_code="9001",
                shares=10000,
                current_price=D("1000"),
                sector=S.BANK,
                risk=RiskObserved(flags=frozenset(), unobserved=frozenset()),
            )
        ],
        cash=CashInput(D("100000"), _NOW),
        bundle=shipped,
    )

    record = build_items_record(
        meta=META, bundle=shipped, cash=CASH, inputs=inputs, result=inputs.optimize(shipped)
    )

    assert record["run"]["status"] == "UNAVAILABLE"
    assert record["run"]["unavailable_reason"] == "POLICY_INCOMPLETE"
    assert record["run"]["policy_missing"] == ["comparison_key"]
    assert record["policy"]["comparison_key"] is None
    assert replay_items_record(record).matched is True


# --- replay ------------------------------------------------------------------------------------


def test_replay_reproduces_the_recorded_result(tmp_path: Path) -> None:
    report = replay_items_record(build(tmp_path))

    assert report.matched is True
    assert report.mismatches == ()


def test_replay_reproduces_random_scenarios(tmp_path: Path) -> None:
    import random

    rng = random.Random(6032)
    for round_index in range(40):
        codes = [(f"{i:04d}", str(rng.randint(5, 60))) for i in range(1, rng.randint(1, 12))]
        holdings = [
            HoldingRecord(
                owner=OWNER,
                stock_code=f"9{i:03d}",
                shares=rng.randint(1, 80) * 100,
                current_price=D(rng.randint(200, 5000)),
                sector=rng.choice([S.BANK, S.FOOD, S.GENERAL]),
                risk=RiskObserved(flags=frozenset(), unobserved=frozenset()),
            )
            for i in range(rng.randint(1, 5))
        ]
        sub = tmp_path / f"r{round_index}"
        sub.mkdir()
        record = build(sub, codes=codes, holdings=holdings)
        assert replay_items_record(record).matched is True, round_index


def _tamper(record: dict[str, Any], path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    mutated = copy.deepcopy(record)
    _at(mutated, path[:-1])[path[-1]] = value
    return mutated


def test_replay_detects_a_tampered_output_by_position_only(tmp_path: Path) -> None:
    record = build(tmp_path)
    allocated = next(
        i
        for i, c in enumerate(record["candidates"])
        if c["allocation"] and c["allocation"]["decision"] == "ALLOCATED"
    )

    report = replay_items_record(
        _tamper(record, ("candidates", allocated, "allocation", "shares"), 1)
    )

    assert report.matched is False
    assert report.mismatches == (f"candidates[{allocated}].allocation",)
    assert all(not any(ch in m for ch in "0123456789") or "[" in m for m in report.mismatches)


@pytest.mark.parametrize(
    "path, value",
    [
        (("cash", "available_cash"), "1000"),
        (("portfolio", "portfolio_value"), "1000"),
        (("policy", "tier_quantity_caps"), {"ENTRY": "0.33", "STANDARD": "0.10", "STRONG": "1.0"}),
        (("policy", "max_single_stock_ratio"), "0.01"),
        (("policy", "min_raer_to_allocate"), "99"),
        (("run", "status"), "NO_ACTION"),
    ],
)
def test_replay_detects_a_changed_input_or_status(
    tmp_path: Path, path: tuple[Any, ...], value: Any
) -> None:
    report = replay_items_record(_tamper(build(tmp_path), path, value))

    assert report.matched is False
    assert report.mismatches


def test_replay_needs_a_valid_record(tmp_path: Path) -> None:
    record = build(tmp_path)
    record["candidates"][0]["unknown"] = 1

    with pytest.raises(RecordSchemaError):
        replay_items_record(record)


# --- 構造(純粋・dormant)------------------------------------------------------------------------


def test_the_module_is_pure_and_reads_no_clock() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    for name in imported:
        root = name.split(".")[0]
        if root == "jstock_advisor":
            assert name.startswith("jstock_advisor.domain."), name
        else:
            assert root in {
                "__future__",
                "datetime",
                "json",
                "re",
                "collections",
                "dataclasses",
                "decimal",
                "typing",
            }, name
    forbidden = {"now", "today", "utcnow", "open", "read_text", "write_text", "getenv", "sleep"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in forbidden, name
        assert not (isinstance(node, ast.Constant) and isinstance(node.value, float))
        assert not (isinstance(node, ast.Name) and node.id in {"logger", "float"})


def test_the_error_classes_are_built_in_one_place_each() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    calls: dict[str, int] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"RecordSchemaError", "ReplayNotPossibleError"}
        ):
            calls[node.func.id] = calls.get(node.func.id, 0) + 1
    assert calls["RecordSchemaError"] == 1
    assert calls["ReplayNotPossibleError"] == 2  # 切り詰めと header の 2 通り(固定の文言)


def test_nothing_in_src_imports_the_record_builder() -> None:
    """importer は、計算の組み立て(F2-2a)と観測のループ(F2-2b。記録を書く)だけ。"""
    importers = sorted(
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if path != _MODULE_PATH and "allocation_shadow_record" in path.read_text(encoding="utf-8")
    )
    # 本文の文字列での走査なので、docstring での言及も拾う。CLI の画面モデルは
    # 『なぜ import しないか』を docstring に書いている(import ではない)。実効の網は
    # #606 の test_the_cli_does_not_reach_the_heavy_record_or_compute_modules(AST の推移閉包)。
    assert importers == [
        "domain/notification/allocation_shadow_cli_view.py",
        "services/allocation_shadow_compute.py",
        "services/allocation_shadow_runner.py",
    ]


# --- 追加の契約(変異テストで弱かった箇所を固定する)-------------------------------------------


class _ReversedSet(frozenset[str]):
    """反復の順序が降順の frozenset。順序を整えない実装を決定的に検出する。"""

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(sorted(frozenset.__iter__(self), reverse=True))


def _raer_with(
    *, value: str = "20", flags: frozenset[str] = frozenset(), observed: bool = True
) -> RaerResult:
    applied = _ALL_AXES if observed else frozenset(_ALL_AXES - {AxisCode.BUSINESS_RISK.value})
    return RaerResult(
        state=RaerState.AVAILABLE if observed else RaerState.PROVISIONAL,
        value=D(value),
        confidence=ConfidenceLevel.MEDIUM,
        risk_flags=flags if observed else frozenset(),
        risk_flags_available=observed,
        model_version=MODEL_VERSION,
        comparison_key=make_comparison_key(MODEL_VERSION, applied),
        axes_applied=_ReversedSet(applied),
        axes_not_applied=_ReversedSet(_ALL_AXES - applied),
        reason_codes=() if observed else ("AXIS_NOT_IMPLEMENTED",),
    )


def _held_bank() -> list[HoldingRecord]:
    return [
        HoldingRecord(
            owner=OWNER,
            stock_code="9001",
            shares=10000,
            current_price=D("1000"),
            sector=S.BANK,
            risk=RiskObserved(flags=frozenset(), unobserved=frozenset()),
        )
    ]


def _build_with(
    tmp_path: Path,
    candidates: list[CandidateRecord],
    *,
    cash: str = "5000000",
    holdings: list[HoldingRecord] | None = None,
) -> tuple[dict[str, Any], pt.AllocationResult]:
    bundle = decided_bundle(tmp_path)
    held = holdings if holdings is not None else _held_bank()
    inputs = assemble_allocation_inputs(
        owner=OWNER,
        now=_NOW,
        candidates=candidates,
        holdings=held,
        cash=CashInput(D(cash), CASH.last_reconciled_at),
        bundle=bundle,
    )
    result = inputs.optimize(bundle)
    record = build_items_record(
        meta=META,
        bundle=bundle,
        cash=CashSnapshot(D(cash), CASH.last_reconciled_at, CASH.updated_at),
        inputs=inputs,
        result=result,
    )
    return record, result


def _cand(code: str, raer_result: RaerResult, **over: Any) -> CandidateRecord:
    base: dict[str, Any] = {
        "stock_code": code,
        "buy_action": BuyAction.BUY,
        "current_price": D("1000"),
        "sector": S.GENERAL,
        "trading_unit": None,
        "raer": raer_result,
    }
    base.update(over)
    return CandidateRecord(**base)


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        (("run",), []),
        (("run",), "x"),
        (("model",), None),
        (("candidates", 0, "attrs"), []),
        (("candidates", 0, "raer_out", "extra"), "x"),
        (("candidates",), {}),
        (("candidates",), "x"),
        (("candidates", 0, "raer_out", "applied_axes"), "BUSINESS_RISK"),
        (("run", "policy_missing"), {"a": "b"}),
        (("candidates", 0, "provenance"), ["UPSIDE"]),
    ],
)
def test_a_section_of_the_wrong_container_type_is_a_schema_error(
    tmp_path: Path, path: tuple[Any, ...], bad: Any
) -> None:
    record = build(tmp_path)
    _at(record, path[:-1])[path[-1]] = bad

    with pytest.raises(RecordSchemaError):
        validate_items_record(record)


@pytest.mark.parametrize("bad", [[], "x", None, 5])
def test_a_record_that_is_not_an_object_is_a_schema_error(bad: Any) -> None:
    with pytest.raises(RecordSchemaError):
        validate_items_record(bad)


def test_a_record_of_another_kind_or_version_is_rejected(tmp_path: Path) -> None:
    record = build(tmp_path)
    for key, value in (
        ("schema_version", 2),
        ("schema_version", 0),
        ("record_kind", "allocation_shadow"),
    ):
        mutated = copy.deepcopy(record)
        mutated[key] = value
        with pytest.raises(RecordSchemaError):
            validate_items_record(mutated)


def test_every_map_has_an_entry_limit(tmp_path: Path) -> None:
    record = build(tmp_path)
    record["run"]["read_states"] = {f"k{i}": "OK" for i in range(9)}
    with pytest.raises(RecordSchemaError):
        validate_items_record(record)
    record = build(tmp_path)
    record["portfolio"]["sector_exposure"] = {f"S{i}": "1" for i in range(33)}
    with pytest.raises(RecordSchemaError):
        validate_items_record(record)
    record = build(tmp_path)
    record["counts"]["exclusions"] = {f"E{i}": 1 for i in range(17)}
    with pytest.raises(RecordSchemaError):
        validate_items_record(record)


def test_the_contract_version_is_the_allocation_contracts_own(tmp_path: Path) -> None:
    assert build(tmp_path)["model"]["contract_version"] == pt.CONTRACT_VERSION


def test_sets_are_recorded_in_a_sorted_order_whatever_their_iteration_order(
    tmp_path: Path,
) -> None:
    flags = _ReversedSet(
        {"small_cap_or_low_liquidity", "major_customer_dependency", "cyclical_industry"}
    )
    record, _ = _build_with(
        tmp_path, [_cand("0001", _raer_with(flags=flags)), _cand("0002", _raer_with(value="9"))]
    )

    section = next(c for c in record["candidates"] if c["stock_code"] == "0001")
    assert section["raer_out"]["risk_flags"] == sorted(flags)
    assert section["raer_out"]["applied_axes"] == sorted(_ALL_AXES)
    assert record["policy"]["high_risk_flags"] == sorted(record["policy"]["high_risk_flags"])


def test_the_high_risk_attribute_distinguishes_flagged_unflagged_and_unobserved(
    tmp_path: Path,
) -> None:
    flagged = _raer_with(flags=frozenset({"major_customer_dependency"}))
    clean = _raer_with(value="25")
    unobserved = _raer_with(value="9", observed=False)
    record, _ = _build_with(
        tmp_path, [_cand("0001", flagged), _cand("0002", clean), _cand("0003", unobserved)]
    )

    by = {c["stock_code"]: c["attrs"]["high_risk"] for c in record["candidates"]}
    assert by == {"0001": True, "0002": False, "0003": None}


def test_the_recorded_allocation_carries_every_field_of_the_result(tmp_path: Path) -> None:
    """買付余力が足りない候補の不足額・上限株数・段階も、結果と同じ値で残る。"""
    record, result = _build_with(
        tmp_path,
        [_cand(f"000{i}", _raer_with(value=str(30 - i))) for i in range(1, 4)],
        cash="300000",
    )

    by_code = {c["stock_code"]: c["allocation"] for c in record["candidates"]}
    shortfalls = []
    for item in result.allocations:
        section = by_code[item.stock_code]
        assert section == {
            "rank": item.rank,
            "decision": item.decision.value,
            "reason_code": item.reason_code.value,
            "shares": item.shares,
            "amount": format(item.amount, "f"),
            "cap_limited_shares": item.cap_limited_shares,
            "cash_shortfall": format(item.cash_shortfall, "f"),
            "price_tier_used": item.price_tier_used.value if item.price_tier_used else None,
        }
        shortfalls.append(item.cash_shortfall)
    assert any(s > 0 for s in shortfalls)  # 資金不足の候補が実際に含まれる


def test_the_recording_order_puts_allocated_first_then_unallocated_by_rank(
    tmp_path: Path,
) -> None:
    """順位 1 の候補が配分されず(業種不明)、順位 2 が配分される入力で、順序の規則を固定する。"""
    record, _ = _build_with(
        tmp_path,
        [
            _cand("0001", _raer_with(value="9")),
            _cand("0002", _raer_with(value="25")),
            _cand("0003", _raer_with(value="30"), sector=S.UNKNOWN),
        ],
    )

    allocations = {c["stock_code"]: c["allocation"] for c in record["candidates"]}
    assert allocations["0003"]["decision"] == "NOT_ALLOCATED" and allocations["0003"]["rank"] == 1
    assert allocations["0002"]["decision"] == "ALLOCATED"
    assert [c["stock_code"] for c in record["candidates"]] == ["0002", "0003", "0001"]


def test_the_exclusions_and_counts_are_recorded(tmp_path: Path) -> None:
    record, _ = _build_with(
        tmp_path,
        [_cand("0001", _raer_with()), _cand("0002", _raer_with(), current_price=None)],
    )

    assert record["counts"]["exclusions"] == {"PRICE_UNAVAILABLE": 1}
    assert record["counts"]["candidate_records"] == 2
    assert record["counts"]["candidates"] == 1
    assert record["counts"]["portfolio_value_basis"] == "COMPLETE"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("run", "unavailable_reason"), "CASH_NOT_REGISTERED"),
        (("run", "policy_missing"), ["comparison_key"]),
    ],
)
def test_replay_compares_the_unavailable_reason_and_the_missing_items(
    tmp_path: Path, path: tuple[Any, ...], value: Any
) -> None:
    report = replay_items_record(_tamper(build(tmp_path), path, value))

    assert report.matched is False
    assert any(m.startswith("run.") for m in report.mismatches)


def test_replay_uses_the_position_value_of_an_already_held_candidate(tmp_path: Path) -> None:
    """既に大きく保有する銘柄の候補は 1 銘柄の上限で止まる。replay も同じ評価額で再計算する。"""
    record, _ = _build_with(tmp_path, [_cand("9001", _raer_with(), sector=S.BANK)])

    assert record["candidates"][0]["kind"] == "ADDITIONAL_BUY"
    assert record["candidates"][0]["current_position_value"] == "10000000"
    assert record["candidates"][0]["allocation"]["decision"] == "NOT_ALLOCATED"
    assert replay_items_record(record).matched is True


def test_a_naive_reconciliation_time_cannot_be_recorded(tmp_path: Path) -> None:
    bundle, inputs, result = scenario(tmp_path)
    naive = CashSnapshot(D("1"), dt.datetime(2026, 10, 11, 9, 5), None)

    with pytest.raises(RecordSchemaError):
        build_items_record(meta=META, bundle=bundle, cash=naive, inputs=inputs, result=result)


def test_a_datetime_cannot_stand_in_for_the_business_date(tmp_path: Path) -> None:
    bundle, inputs, result = scenario(tmp_path)
    bad = RecordMeta(
        batch_id=META.batch_id,
        business_date=dt.datetime(2026, 10, 14, 9, 5),
        owner_ref=META.owner_ref,
        shadow_mode="SHADOW",
        raer_model_version=MODEL_VERSION,
        coefficient_set_id=COEFFICIENT_SET_ID,
        elapsed_ms=1,
        read_states=META.read_states,
    )

    with pytest.raises(RecordSchemaError):
        build_items_record(meta=bad, bundle=bundle, cash=CASH, inputs=inputs, result=result)


def test_the_raer_axis_reason_codes_with_a_colon_are_accepted(tmp_path: Path) -> None:
    """RaerResult.reason_codes は全体の `CODE` と軸ごとの `AXIS:CODE`(to_raer_result の形)を持つ。

    F2-2a の結合で、許可する形が `CODE` だけだと実際の RAER の記録を組み立てられないことが分かった。
    """
    record = build(tmp_path)
    record["candidates"][0]["raer_out"]["reason_codes"] = [
        "AXES_BELOW_MINIMUM",
        "COMPANY_QUALITY:COVERAGE_BELOW_MINIMUM",
    ]

    validate_items_record(record)  # 通る
    for bad in ("COMPANY_QUALITY:", ":X", "a:b", "X:Y:Z", "X Y", "日本語:X", "X:Y\n"):
        record["candidates"][0]["raer_out"]["reason_codes"] = [bad]
        with pytest.raises(RecordSchemaError):
            validate_items_record(record)


# --- model.coefficient_set_id(#602 …6159406045 の USER 条件 4)---------------------------------


def _header(tmp_path: Path) -> dict[str, Any]:
    return build_header_record(
        meta=META,
        bundle=decided_bundle(tmp_path),
        cash=CashSnapshot(None, None, None),
        outcome="SKIPPED",
        skip_reason="CASH_NOT_REGISTERED",
    )


def test_both_builders_record_the_coefficient_set_id_in_the_model_section(tmp_path: Path) -> None:
    bundle, inputs, result = scenario(tmp_path)
    items = build_items_record(
        meta=META, bundle=bundle, cash=CASH, inputs=inputs, result=result, details=None
    )

    assert items["model"]["coefficient_set_id"] == COEFFICIENT_SET_ID
    assert _header(tmp_path)["model"]["coefficient_set_id"] == COEFFICIENT_SET_ID


def test_the_coefficient_set_id_key_is_required_but_its_value_may_be_null(tmp_path: Path) -> None:
    record = _header(tmp_path)

    validate_items_record(record)
    validate_items_record({**record, "model": {**record["model"], "coefficient_set_id": None}})
    without = {k: v for k, v in record["model"].items() if k != "coefficient_set_id"}
    with pytest.raises(RecordSchemaError):
        validate_items_record({**record, "model": without})


@pytest.mark.parametrize("bad", ["", " ", "a b", "x" * 300, 7, {"a": 1}, ["x"]])
def test_a_coefficient_set_id_that_is_not_an_identifier_is_rejected(
    tmp_path: Path, bad: object
) -> None:
    record = _header(tmp_path)

    with pytest.raises(RecordSchemaError):
        validate_items_record({**record, "model": {**record["model"], "coefficient_set_id": bad}})


# --- 縮退した日(係数の束が無い)の整合・schema_version を据え置く理由(#603 …6161266213)-------


def _record_with_a_raer_value(tmp_path: Path) -> dict[str, Any]:
    bundle, inputs, result = scenario(tmp_path)
    record = build_items_record(
        meta=META, bundle=bundle, cash=CASH, inputs=inputs, result=result, details={}
    )
    assert any(row["raer_out"]["value"] is not None for row in record["candidates"])
    return record


def test_a_record_without_a_coefficient_set_cannot_carry_a_raer_value(tmp_path: Path) -> None:
    """版はあるが係数の束の id が無い記録は、候補に RAER の値を 1 つも持てない(縮退の側の固定)。"""
    record = _record_with_a_raer_value(tmp_path)
    validate_items_record(record)  # 束の id がある記録は値を持てる

    degraded = {**record, "model": {**record["model"], "coefficient_set_id": None}}
    with pytest.raises(RecordSchemaError):
        validate_items_record(degraded)

    for row in degraded["candidates"]:  # 値が無ければ(縮退の実態)通る
        row["raer_out"] = {**row["raer_out"], "value": None}
    validate_items_record(degraded)


def test_a_header_only_record_without_a_coefficient_set_is_valid(tmp_path: Path) -> None:
    record = build_header_record(
        meta=dataclasses.replace(META, coefficient_set_id=None),
        bundle=decided_bundle(tmp_path),
        cash=CashSnapshot(None, None, None),
        outcome="SKIPPED",
        skip_reason="COEFFICIENTS_UNAVAILABLE",
    )

    validate_items_record(record)
    assert record["model"]["coefficient_set_id"] is None


def test_the_schema_version_rule_is_written_in_the_module_docstring() -> None:
    doc = rec.__doc__ or ""

    assert "旧い形の記録が存在し得るか" in doc
    assert "配線後は、キーの追加・削除は必ず SCHEMA_VERSION を上げる" in doc
