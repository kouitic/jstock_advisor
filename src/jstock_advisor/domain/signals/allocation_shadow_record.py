"""購入側 Shadow の項目つきの監査記録 `allocation_shadow_items` の組み立て・検証・replay(F2-1)。

## 位置づけ

PR-F2 の『記録』の純粋な部分。記録の形(allowlist)・組み立て・自己検証・replay を持つ。
**I/O・時計・ログ・AWS・services / infrastructure に触れない**(業務日・時刻は引数)。
どこからも import されない(dormant。書込は F2-2)。承認の根拠: 判断 C(C-2 段階 2)と
docs #930 の §37.2 / §37.5 / §37.6(保有由来は必要最小限の集計金額だけ。個別の保有 ID・取得単価・
損益・所有者の実名・保有の銘柄別の株数は記録しない)と、#934 の要件表。

## 記録の形(schema_version 1)

```
header      schema_version / record_kind / business_date(JST)/ batch_id / owner_ref
model       raer_model_version / policy_id / config_hash / contract_version / shadow_mode
policy      配分の方針の値(replay に要る。閾値であり、個人の情報ではない)
run         outcome / status / unavailable_reason / skip_reason / policy_missing / elapsed_ms
            / read_states
counts      候補の件数と除外の理由別の件数 / 保有の欠測の件数(件数だけ)
            / 記録した候補数と切り詰めた数
cash        買付余力 / 最終棚卸し / 更新時刻
portfolio   総額 / 業種別 / high-risk の保有額(集計金額だけ)
candidates  銘柄コード・種類・属性・価格と鮮度・RAER の入力と結果・配分の結果
```

## 構造的に拒否するもの(allowlist)

* **キー**: 全ての入れ子を含め、定義したキーだけ。未知・不足のキーは拒否(再帰)
* **値の形**: 文字列は記号を含まない ASCII の短い識別子・コード・10 進数の文字列だけ
  (日本語・空白・自由な文章は拒否 = 銘柄名・所有者名が紛れ込めない)。数値は 10 進数の
  文字列(float にしない)
* **builder の引数の型**: owner は `KeyedOwnerRef`(鍵つき kv1。平文の str・kv0 の符号を型と
  実行時で拒否)。保有の銘柄別の株数・
  取得単価・損益・保有の一覧を受け取る引数が**そもそも無い**
* **大きさ**: 候補の記録に上限があり、超えた分は件数だけ(配分した候補は優先して残す)。
  全体も上限で検証

## replay

記録の入力部(方針・買付余力・ポートフォリオ・候補)から PR-C の `optimize_allocation` で
結果を再計算し、記録の出力部(状態・各候補の配分)と一致するかを返す。切り詰めがある記録は
replay できない(明示)。例外・報告に値を含めない(候補の位置と項目名だけ)。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel
from jstock_advisor.domain.entities.identifier_ref import (
    MAX_KEY_VERSION,
    MIN_KEY_VERSION,
    IdentifierRefError,
    KeyedOwnerRef,
    KeyUnavailableReason,
    parse_keyed_ref,
    require_keyed,
)
from jstock_advisor.domain.signals.allocation_inputs import AllocationInputs
from jstock_advisor.domain.signals.allocation_policy_config import AllocationPolicyBundle
from jstock_advisor.domain.signals.portfolio_allocation import optimize_allocation
from jstock_advisor.domain.signals.portfolio_allocation_types import (
    CONTRACT_VERSION,
    AllocationCandidate,
    AllocationConstraints,
    AllocationPolicy,
    AllocationResult,
    CandidateKind,
    PriceTier,
)
from jstock_advisor.domain.valuation.raer import RaerResult, RaerState

SCHEMA_VERSION: Final = 1
RECORD_KIND: Final = "allocation_shadow_items"
#: 1 件の記録に残す候補の上限(超過分は件数だけ)。実測して見直す(1 件 400KB への安全弁)
MAX_CANDIDATES_RECORDED: Final = 150
#: 記録全体の大きさの上限(バイト)。400KB に余裕を持たせる
MAX_RECORD_BYTES: Final = 300_000


class RecordSchemaError(ValueError):
    """記録が allowlist(キー・値の形・大きさ)に反する。値を含めず、場所(パス)だけを示す。"""


class ReplayNotPossibleError(ValueError):
    """この記録は replay できない(切り詰め・header だけ)。"""


def _schema_fail(path: str, problem: str) -> RecordSchemaError:
    return RecordSchemaError(f"{path}: {problem}")


# --- 値の形(文字列は ASCII の識別子・コード・10 進数だけ)--------------------------------------

_IDENT: Final = re.compile(r"^[A-Za-z0-9_.:|+,\-]{1,200}$")
_CODE: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_WORD: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DEC: Final = re.compile(r"^-?[0-9]{1,30}(\.[0-9]{1,30})?$")
_DATE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_DATETIME: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?[+-][0-9]{2}:[0-9]{2}$"
)
_REF: Final = re.compile(r"^kv1\.[1-9][0-9]{0,2}:[0-9a-f]{24}$")  # 鍵つき(kv1)だけ。kv0 は拒否


@dataclass(frozen=True)
class _Opt:
    inner: Any


@dataclass(frozen=True)
class _List:
    item: Any
    limit: int


@dataclass(frozen=True)
class _Map:
    key: re.Pattern[str]
    value: Any
    limit: int


@dataclass(frozen=True)
class _Enum:
    values: frozenset[str]


def _opt(spec: Any) -> _Opt:
    return _Opt(spec)


def _enum(*values: str) -> _Enum:
    return _Enum(frozenset(values))


_SCALARS: Final[dict[str, re.Pattern[str]]] = {
    "ident": _IDENT,
    "code": _CODE,
    "word": _WORD,
    "dec": _DEC,
    "date": _DATE,
    "datetime": _DATETIME,
    "ref": _REF,
}

_DEC_MAP = _Map(_IDENT, "dec", 64)
_WORD_LIST = _List("word", 16)
_CODE_LIST = _List("code", 32)

#: 候補ごとの RAER の入力(replay・説明用)。キーは固定(値は 10 進数の文字列・真偽・語彙のみ)
RAER_IN_KEYS: Final = (
    "upside_pct",
    "income_pct",
    "fair_value_confidence",
    "quality_score",
    "quality_coverage_ratio",
    "quality_min_coverage",
    "trend_score",
    "trend_partial_coverage",
    "trend_components_used",
    "trend_weights",
    "risk_flags_observed",
    "company_quality_model_version",
    "earnings_trend_model_version",
)
_RAER_IN_SPEC = {
    "upside_pct": _opt("dec"),
    "income_pct": _opt("dec"),
    "fair_value_confidence": _opt("code"),
    "quality_score": _opt("dec"),
    "quality_coverage_ratio": _opt("dec"),
    "quality_min_coverage": _opt("dec"),
    "trend_score": _opt("dec"),
    "trend_partial_coverage": _opt("dec"),
    "trend_components_used": _opt(_WORD_LIST),
    "trend_weights": _opt(_DEC_MAP),
    "risk_flags_observed": _opt("bool"),
    "company_quality_model_version": _opt("ident"),
    "earnings_trend_model_version": _opt("ident"),
}

#: 候補ごとの RAER の結果のうち、RaerResult から自動で作らない付加情報(寄与度・土台・仕様フラグ)
RAER_EXTRA_KEYS: Final = (
    "coefficient_set_id",
    "contributions",
    "risk_contributions",
    "base",
    "spec_flags",
)
_RAER_EXTRA_SPEC = {
    "coefficient_set_id": _opt("ident"),
    "contributions": _opt(_DEC_MAP),
    "risk_contributions": _opt(_DEC_MAP),
    "base": _opt(_DEC_MAP),
    "spec_flags": _opt(_CODE_LIST),
}

_RAER_OUT_SPEC = {
    "state": _enum(*(s.value for s in RaerState)),
    "value": _opt("dec"),
    "confidence": _opt(_enum(*(c.value for c in ConfidenceLevel))),
    "risk_flags": _WORD_LIST,
    "risk_flags_available": "bool",
    "model_version": "ident",
    "comparison_key": _opt("ident"),
    "applied_axes": _List("code", 8),
    "unapplied_axes": _List("code", 8),
    "reason_codes": _CODE_LIST,
    "extra": _RAER_EXTRA_SPEC,
}

_ALLOCATION_SPEC = {
    "rank": _opt("int"),
    "decision": _enum("ALLOCATED", "NOT_ALLOCATED"),
    "reason_code": "code",
    "shares": "int",
    "amount": "dec",
    "cap_limited_shares": "int",
    "cash_shortfall": "dec",
    "price_tier_used": _opt(_enum(*(t.value for t in PriceTier))),
}

_CANDIDATE_SPEC = {
    "seq": "int",
    "stock_code": "ident",
    "kind": _enum(*(k.value for k in CandidateKind)),
    "current_position_value": "dec",
    "attrs": {
        "buy_action": _enum(*(a.value for a in BuyAction)),
        "price_tier": _opt(_enum(*(t.value for t in PriceTier))),
        "fair_value_confidence": _opt("code"),
        "quality_band": _opt(_enum("LOW", "MID", "HIGH", "UNKNOWN")),
        "sector": _opt("code"),
        "high_risk": _opt("bool"),
    },
    "price": {
        "current_price": "dec",
        "price_as_of_date": _opt("date"),
        "freshness_verdict": _opt("code"),
    },
    "raer_in": _RAER_IN_SPEC,
    "raer_out": _RAER_OUT_SPEC,
    "provenance": _Map(_CODE, "code", 16),
    "allocation": _opt(_ALLOCATION_SPEC),
}

_POLICY_SPEC = {
    "min_raer_to_allocate": _opt("dec"),
    "comparison_key": _opt("ident"),
    "max_single_stock_amount": _opt("dec"),
    "max_single_stock_ratio": _opt("dec"),
    "max_sector_ratio": _opt("dec"),
    "max_high_risk_ratio": _opt("dec"),
    "high_risk_flags": _WORD_LIST,
    "tier_quantity_caps": _opt(_Map(_CODE, "dec", 3)),
    "minimum_trading_unit": "int",
}

_RECORD_SPEC = {
    "schema_version": "int",
    "record_kind": "ident",
    "business_date": "date",
    "batch_id": "ident",
    "owner_ref": "ref",
    "model": {
        "raer_model_version": _opt("ident"),
        "policy_id": _opt("ident"),
        "config_hash": "ident",
        "contract_version": "ident",
        "shadow_mode": "code",
    },
    "policy": _POLICY_SPEC,
    "run": {
        "outcome": _enum("COMPUTED", "SKIPPED", "FAILED"),
        "status": _opt(_enum("ALLOCATED_SOME", "NO_ACTION", "UNAVAILABLE")),
        "unavailable_reason": _opt("code"),
        "skip_reason": _opt("code"),
        "policy_missing": _List("word", 16),
        "elapsed_ms": _opt("int"),
        "read_states": _Map(_WORD, "code", 8),
    },
    "counts": {
        "candidate_records": "int",
        "candidates": "int",
        "exclusions": _Map(_CODE, "int", 16),
        "holdings": "int",
        "holdings_price_missing": "int",
        "holdings_sector_unknown": "int",
        "holdings_risk_unknown": "int",
        "owner_holds_candidate": "int",
        "trading_unit_defaulted": "int",
        "incomplete_reasons": _CODE_LIST,
        "portfolio_value_basis": "code",
        "cash_age_days": _opt("int"),
        "candidates_recorded": "int",
        "candidates_truncated": "int",
        "add_on_gate_applied": "bool",
    },
    "cash": {
        "available_cash": _opt("dec"),
        "last_reconciled_at": _opt("datetime"),
        "updated_at": _opt("datetime"),
    },
    "portfolio": {
        "portfolio_value": _opt("dec"),
        "sector_exposure": _opt(_Map(_CODE, "dec", 32)),
        "high_risk_exposure": _opt("dec"),
    },
    "candidates": _List(_CANDIDATE_SPEC, MAX_CANDIDATES_RECORDED),
}

_INT_LIMIT: Final = 10**12


def _check(spec: Any, value: Any, path: str) -> None:
    """spec に値が合うか(再帰)。合わなければ RecordSchemaError(値を含めない)。"""
    if isinstance(spec, _Opt):
        if value is not None:
            _check(spec.inner, value, path)
        return
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise _schema_fail(path, "must be an object")
        extra = set(value) - set(spec)
        missing = set(spec) - set(value)
        if extra:
            raise _schema_fail(path, "has a key outside the allowlist")
        if missing:
            raise _schema_fail(path, "lacks a required key")
        for key, inner in spec.items():
            _check(inner, value[key], f"{path}.{key}")
        return
    if isinstance(spec, _List):
        if not isinstance(value, list):
            raise _schema_fail(path, "must be a list")
        if len(value) > spec.limit:
            raise _schema_fail(path, "has too many items")
        for index, item in enumerate(value):
            _check(spec.item, item, f"{path}[{index}]")
        return
    if isinstance(spec, _Map):
        if not isinstance(value, dict):
            raise _schema_fail(path, "must be a map")
        if len(value) > spec.limit:
            raise _schema_fail(path, "has too many entries")
        for key, item in value.items():
            if not isinstance(key, str) or not spec.key.fullmatch(key):
                raise _schema_fail(path, "has a key of the wrong form")
            _check(spec.value, item, f"{path}.<map>")
        return
    if isinstance(spec, _Enum):
        if not isinstance(value, str) or value not in spec.values:
            raise _schema_fail(path, "is outside the vocabulary")
        return
    if spec == "bool":
        if not isinstance(value, bool):
            raise _schema_fail(path, "must be a bool")
        return
    if spec == "int":
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > _INT_LIMIT:
            raise _schema_fail(path, "must be a bounded int")
        return
    pattern = _SCALARS.get(spec) if isinstance(spec, str) else None
    if pattern is None:
        raise AssertionError("unknown spec")  # スキーマ定義の誤り(実行時の入力ではない)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _schema_fail(path, "has the wrong form")


def validate_items_record(record: Mapping[str, Any]) -> None:
    """記録が allowlist に適合するか(キー・値の形・大きさ)。不適合なら RecordSchemaError。"""
    if not isinstance(record, dict):
        raise _schema_fail("record", "must be an object")
    _check(_RECORD_SPEC, record, "record")
    if record["schema_version"] != SCHEMA_VERSION or record["record_kind"] != RECORD_KIND:
        raise _schema_fail("record", "is not this kind or version")
    if record_size_bytes(record) > MAX_RECORD_BYTES:
        raise _schema_fail("record", "is too large")


def record_size_bytes(record: Mapping[str, Any]) -> int:
    return len(json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


# --- 入力の型(builder が受け取れるものだけ。保有の銘柄別の株数・取得単価・損益・一覧は無い)---


@dataclass(frozen=True)
class CashSnapshot:
    amount: Decimal | None
    last_reconciled_at: dt.datetime | None
    updated_at: dt.datetime | None


@dataclass(frozen=True)
class CandidateDetail:
    """候補ごとの付加情報(F2-2 が RAER の assembler の出力から写す。10 進数文字列・真偽・語彙)。"""

    fair_value_confidence: str | None = None
    quality_band: str | None = None
    high_risk: bool | None = None
    price_as_of_date: dt.date | None = None
    freshness_verdict: str | None = None
    raer_in: Mapping[str, Any] = field(default_factory=dict)
    raer_extra: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordMeta:
    batch_id: str
    business_date: dt.date
    owner_ref: KeyedOwnerRef
    shadow_mode: str
    raer_model_version: str | None
    elapsed_ms: int | None
    read_states: Mapping[str, str]
    add_on_gate_applied: bool = False


def _dec(value: Decimal | None) -> str | None:
    """厳密な 10 進数の文字列(指数表記にしない)。"""
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise _schema_fail("value", "must be a finite Decimal")
    return format(value, "f")


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise _schema_fail("value", "must be timezone-aware")
    return value.isoformat()


def _policy_section(bundle: AllocationPolicyBundle) -> dict[str, Any]:
    c = bundle.constraints
    p = bundle.policy
    caps = p.tier_quantity_caps
    return {
        "min_raer_to_allocate": _dec(p.min_raer_to_allocate),
        "comparison_key": p.comparison_key,
        "max_single_stock_amount": _dec(c.max_single_stock_amount),
        "max_single_stock_ratio": _dec(c.max_single_stock_ratio),
        "max_sector_ratio": _dec(c.max_sector_ratio),
        "max_high_risk_ratio": _dec(c.max_high_risk_ratio),
        "high_risk_flags": sorted(c.high_risk_flags),
        "tier_quantity_caps": (
            None if caps is None else {tier.value: _dec(caps[tier]) for tier in PriceTier}
        ),
        "minimum_trading_unit": c.minimum_trading_unit,
    }


def _raer_out(raer: RaerResult, extra: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(extra) - set(RAER_EXTRA_KEYS)
    if unknown:
        raise _schema_fail("raer_extra", "has a key outside the allowlist")
    return {
        "state": raer.state.value,
        "value": _dec(raer.value),
        "confidence": raer.confidence.value if raer.confidence is not None else None,
        "risk_flags": sorted(raer.risk_flags),
        "risk_flags_available": raer.risk_flags_available,
        "model_version": raer.model_version,
        "comparison_key": raer.comparison_key,
        "applied_axes": sorted(raer.axes_applied),
        "unapplied_axes": sorted(raer.axes_not_applied),
        "reason_codes": list(raer.reason_codes),
        "extra": {key: extra.get(key) for key in RAER_EXTRA_KEYS},
    }


def _candidate_section(
    seq: int,
    candidate: AllocationCandidate,
    detail: CandidateDetail,
    allocation: Mapping[str, Any] | None,
    high_risk_flags: frozenset[str],
) -> dict[str, Any]:
    unknown_in = set(detail.raer_in) - set(RAER_IN_KEYS)
    if unknown_in:
        raise _schema_fail("raer_in", "has a key outside the allowlist")
    high_risk = (
        bool(candidate.raer.risk_flags & high_risk_flags)
        if candidate.raer.risk_flags_available
        else None
    )
    return {
        "seq": seq,
        "stock_code": candidate.stock_code,
        "kind": candidate.kind.value,
        "current_position_value": _dec(candidate.current_position_value),
        "attrs": {
            "buy_action": candidate.buy_action.value,
            "price_tier": candidate.price_tier.value if candidate.price_tier is not None else None,
            "fair_value_confidence": detail.fair_value_confidence,
            "quality_band": detail.quality_band,
            "sector": candidate.sector,
            "high_risk": detail.high_risk if detail.high_risk is not None else high_risk,
        },
        "price": {
            "current_price": _dec(candidate.current_price),
            "price_as_of_date": (
                detail.price_as_of_date.isoformat() if detail.price_as_of_date else None
            ),
            "freshness_verdict": detail.freshness_verdict,
        },
        "raer_in": {key: detail.raer_in.get(key) for key in RAER_IN_KEYS},
        "raer_out": _raer_out(candidate.raer, detail.raer_extra),
        "provenance": dict(detail.provenance),
        "allocation": allocation,
    }


def _allocation_section(item: Any) -> dict[str, Any]:
    return {
        "rank": item.rank,
        "decision": item.decision.value,
        "reason_code": item.reason_code.value,
        "shares": item.shares,
        "amount": _dec(item.amount),
        "cap_limited_shares": item.cap_limited_shares,
        "cash_shortfall": _dec(item.cash_shortfall),
        "price_tier_used": item.price_tier_used.value if item.price_tier_used else None,
    }


def _order_for_recording(
    candidates: Sequence[AllocationCandidate], allocations: Mapping[str, Any]
) -> list[AllocationCandidate]:
    """記録する順: 配分した候補 → 順位つき(順位の昇順)→ それ以外(stock_code 昇順)。"""

    def key(candidate: AllocationCandidate) -> tuple[int, int, str]:
        item = allocations.get(candidate.stock_code)
        allocated = item is not None and item.decision.value == "ALLOCATED"
        rank = item.rank if item is not None and item.rank is not None else 10**9
        return (0 if allocated else 1, rank, candidate.stock_code)

    return sorted(candidates, key=key)


def build_items_record(
    *,
    meta: RecordMeta,
    bundle: AllocationPolicyBundle,
    cash: CashSnapshot,
    inputs: AllocationInputs,
    result: AllocationResult,
    details: Mapping[str, CandidateDetail] | None = None,
) -> dict[str, Any]:
    """計算できた日の記録(COMPUTED)。allowlist に自己検証し、不適合なら RecordSchemaError。"""
    details = details or {}
    report = inputs.report
    allocations = {a.stock_code: a for a in result.allocations}
    ordered = _order_for_recording(inputs.candidates, allocations)
    kept = ordered[:MAX_CANDIDATES_RECORDED]
    sections = [
        _candidate_section(
            seq,
            candidate,
            details.get(candidate.stock_code, CandidateDetail()),
            _allocation_section(allocations[candidate.stock_code])
            if candidate.stock_code in allocations
            else None,
            bundle.constraints.high_risk_flags,
        )
        for seq, candidate in enumerate(kept, start=1)
    ]
    record = _record(meta, bundle, cash)
    record["run"].update(
        {
            "outcome": "COMPUTED",
            "status": result.status.value,
            "unavailable_reason": (
                result.unavailable_reason.value if result.unavailable_reason else None
            ),
            "policy_missing": list(result.policy_missing),
        }
    )
    record["counts"].update(
        {
            "candidate_records": report.candidate_records,
            "candidates": report.candidates,
            "exclusions": {reason.value: count for reason, count in report.exclusions.items()},
            "holdings": report.holdings,
            "holdings_price_missing": report.holdings_price_missing,
            "holdings_sector_unknown": report.holdings_sector_unknown,
            "holdings_risk_unknown": report.holdings_risk_unknown,
            "owner_holds_candidate": report.owner_holds_candidate,
            "trading_unit_defaulted": report.trading_unit_defaulted,
            "incomplete_reasons": [r.value for r in report.incomplete_reasons],
            "portfolio_value_basis": report.portfolio_value_basis.value,
            "cash_age_days": report.cash_age_days,
            "candidates_recorded": len(sections),
            "candidates_truncated": len(ordered) - len(sections),
        }
    )
    record["portfolio"] = {
        "portfolio_value": _dec(inputs.portfolio_value),
        "sector_exposure": (
            None
            if inputs.sector_exposure is None
            else {key: _dec(amount) for key, amount in sorted(inputs.sector_exposure.items())}
        ),
        "high_risk_exposure": _dec(inputs.high_risk_exposure),
    }
    record["candidates"] = sections
    validate_items_record(record)
    return record


def build_header_record(
    *,
    meta: RecordMeta,
    bundle: AllocationPolicyBundle,
    cash: CashSnapshot,
    outcome: str,
    skip_reason: str,
) -> dict[str, Any]:
    """SKIPPED・FAILED の日の記録(header だけ。candidates は空)。なぜ算出されなかったかを残す。"""
    if outcome not in ("SKIPPED", "FAILED"):
        raise _schema_fail("outcome", "must be SKIPPED or FAILED")
    record = _record(meta, bundle, cash)
    record["run"].update({"outcome": outcome, "skip_reason": skip_reason})
    validate_items_record(record)
    return record


def _record(meta: RecordMeta, bundle: AllocationPolicyBundle, cash: CashSnapshot) -> dict[str, Any]:
    if not isinstance(meta.business_date, dt.date) or isinstance(meta.business_date, dt.datetime):
        raise _schema_fail("business_date", "must be a date")
    require_keyed(meta.owner_ref)  # kv0・壊れた符号は書かない(実在の owner を kv0 で永続しない)
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": RECORD_KIND,
        "business_date": meta.business_date.isoformat(),
        "batch_id": meta.batch_id,
        "owner_ref": meta.owner_ref,
        "model": {
            "raer_model_version": meta.raer_model_version,
            "policy_id": bundle.policy_id,
            "config_hash": bundle.config_hash,
            "contract_version": CONTRACT_VERSION,
            "shadow_mode": meta.shadow_mode,
        },
        "policy": _policy_section(bundle),
        "run": {
            "outcome": "SKIPPED",
            "status": None,
            "unavailable_reason": None,
            "skip_reason": None,
            "policy_missing": [],
            "elapsed_ms": meta.elapsed_ms,
            "read_states": dict(meta.read_states),
        },
        "counts": {
            "candidate_records": 0,
            "candidates": 0,
            "exclusions": {},
            "holdings": 0,
            "holdings_price_missing": 0,
            "holdings_sector_unknown": 0,
            "holdings_risk_unknown": 0,
            "owner_holds_candidate": 0,
            "trading_unit_defaulted": 0,
            "incomplete_reasons": [],
            "portfolio_value_basis": "UNKNOWN",
            "cash_age_days": None,
            "candidates_recorded": 0,
            "candidates_truncated": 0,
            "add_on_gate_applied": meta.add_on_gate_applied,
        },
        "cash": {
            "available_cash": _dec(cash.amount),
            "last_reconciled_at": _iso(cash.last_reconciled_at),
            "updated_at": _iso(cash.updated_at),
        },
        "portfolio": {"portfolio_value": None, "sector_exposure": None, "high_risk_exposure": None},
        "candidates": [],
    }


# --- replay ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayReport:
    matched: bool
    mismatches: tuple[str, ...]


def _to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:  # 検証済みの記録では起きない
        raise _schema_fail("replay", "has a malformed number") from exc


def _constraints_from(policy: Mapping[str, Any]) -> AllocationConstraints:
    return AllocationConstraints(
        max_single_stock_amount=_to_decimal(policy["max_single_stock_amount"]),
        max_single_stock_ratio=_to_decimal(policy["max_single_stock_ratio"]),
        max_sector_ratio=_to_decimal(policy["max_sector_ratio"]),
        max_high_risk_ratio=_to_decimal(policy["max_high_risk_ratio"]),
        high_risk_flags=frozenset(policy["high_risk_flags"]),
        minimum_trading_unit=policy["minimum_trading_unit"],
    )


def _policy_from(policy: Mapping[str, Any]) -> AllocationPolicy:
    caps = policy["tier_quantity_caps"]
    return AllocationPolicy(
        min_raer_to_allocate=_to_decimal(policy["min_raer_to_allocate"]),
        comparison_key=policy["comparison_key"],
        tier_quantity_caps=(
            None
            if caps is None
            else {PriceTier(tier): _to_decimal(amount) for tier, amount in caps.items()}  # type: ignore[misc]
        ),
    )


def _raer_from(out: Mapping[str, Any]) -> RaerResult:
    return RaerResult(
        state=RaerState(out["state"]),
        value=_to_decimal(out["value"]),
        confidence=ConfidenceLevel(out["confidence"]) if out["confidence"] else None,
        risk_flags=frozenset(out["risk_flags"]),
        risk_flags_available=out["risk_flags_available"],
        model_version=out["model_version"],
        comparison_key=out["comparison_key"],
        axes_applied=frozenset(out["applied_axes"]),
        axes_not_applied=frozenset(out["unapplied_axes"]),
        reason_codes=tuple(out["reason_codes"]),
    )


def _candidate_from(section: Mapping[str, Any]) -> AllocationCandidate:
    return AllocationCandidate(
        stock_code=section["stock_code"],
        kind=CandidateKind(section["kind"]),
        buy_action=BuyAction(section["attrs"]["buy_action"]),
        current_price=_to_decimal(section["price"]["current_price"]),  # type: ignore[arg-type]
        price_as_of=None,
        price_tier=(
            PriceTier(section["attrs"]["price_tier"]) if section["attrs"]["price_tier"] else None
        ),
        raer=_raer_from(section["raer_out"]),
        current_position_value=_to_decimal(section["current_position_value"]),  # type: ignore[arg-type]
        sector=section["attrs"]["sector"],
    )


def _sector_exposure_from(raw: Mapping[str, str]) -> dict[str, Decimal]:
    exposure: dict[str, Decimal] = {}
    for key, amount in raw.items():
        number = _to_decimal(amount)
        if number is None:
            raise _schema_fail("replay", "has a malformed number")
        exposure[key] = number
    return exposure


def replay_items_record(record: Mapping[str, Any]) -> ReplayReport:
    """記録の入力部から配分を再計算し、記録の出力部と一致するかを返す(値を含めない)。"""
    validate_items_record(record)
    run = record["run"]
    if run["outcome"] != "COMPUTED":
        raise ReplayNotPossibleError("a header-only record has no computation to replay")
    if record["counts"]["candidates_truncated"]:
        raise ReplayNotPossibleError("a truncated record cannot be replayed")
    sections = record["candidates"]
    result = optimize_allocation(
        owner="replay",
        available_cash=_to_decimal(record["cash"]["available_cash"]),
        portfolio_value=_to_decimal(record["portfolio"]["portfolio_value"]),
        candidates=[_candidate_from(s) for s in sections],
        constraints=_constraints_from(record["policy"]),
        policy=_policy_from(record["policy"]),
        sector_exposure=(
            None
            if record["portfolio"]["sector_exposure"] is None
            else _sector_exposure_from(record["portfolio"]["sector_exposure"])
        ),
        high_risk_exposure=_to_decimal(record["portfolio"]["high_risk_exposure"]),
    )
    mismatches: list[str] = []
    if result.status.value != run["status"]:
        mismatches.append("run.status")
    expected_reason = result.unavailable_reason.value if result.unavailable_reason else None
    if expected_reason != run["unavailable_reason"]:
        mismatches.append("run.unavailable_reason")
    if list(result.policy_missing) != run["policy_missing"]:
        mismatches.append("run.policy_missing")
    recomputed = {a.stock_code: _allocation_section(a) for a in result.allocations}
    for index, section in enumerate(sections):
        if recomputed.get(section["stock_code"]) != section["allocation"]:
            mismatches.append(f"candidates[{index}].allocation")
    return ReplayReport(matched=not mismatches, mismatches=tuple(mismatches))


# --- 日単位の記録: 索引と skip(#925 kv1 設計 rev2。MUST-2)------------------------------------
#
# owner を持たない日単位の 2 種類の記録。どちらも owner の符号以外の識別子を持たず、全階層を
# allowlist で検証する。audit_id は決定的(条件付き書込 = 先着)。
#
# 索引(allocation_shadow_index): Shadow がその日に ★ 実際に記録が存在する ★ owner の符号の一覧。
#   CLI は鍵を持たず、索引 → 各 owner の項目つきの記録を読む。★ 全 owner の記録の後に書く ★
#   (索引が在る = それまでに記録された owner が全て載っている)。書けなかった owner は載せず、
#   件数と理由で数える(黙って落とさない)。索引が無い日は『記録が 0 件』ではなく『不明』。
# 日単位の skip(allocation_shadow_day_skip): 鍵が取れない日など、owner の符号を作れず owner 単位の
#   記録を 1 件も書けない日に、理由コードと件数だけを残す(owner を持たない)。

INDEX_RECORD_KIND: Final = "allocation_shadow_index"
DAY_SKIP_RECORD_KIND: Final = "allocation_shadow_day_skip"
#: 索引に載せる owner の符号の上限。観測のループがこれを超える owner を記録対象にしない
#: (OWNER_CAP として数える)ので、『書いたのに索引に載らない』は構造的に起きない
MAX_INDEX_OWNERS: Final = 16

#: 記録できなかった owner の理由(語彙。索引の unrecorded_reasons のキー)
UNRECORDED_REASONS: Final = (
    "BUDGET_EXHAUSTED",
    "KEY_UNAVAILABLE",
    "WRITE_FAILED",
    "OWNER_CAP",
    "REFUSED",
)

#: 日単位の skip の理由(語彙)
DAY_SKIP_REASONS: Final = ("KEY_UNAVAILABLE",)

_UNRECORDED_REASONS_SPEC = _Map(_CODE, "int", len(UNRECORDED_REASONS))

_INDEX_SPEC = {
    "schema_version": "int",
    "record_kind": "ident",
    "business_date": "date",
    "batch_id": "ident",
    "shadow_mode": "code",
    "key_version": "int",
    "owner_refs": _List("ref", MAX_INDEX_OWNERS),
    "owner_refs_truncated": "bool",
    "attempted_owner_count": "int",
    "recorded_owner_count": "int",
    "unrecorded_owner_count": "int",
    "unrecorded_reasons": _UNRECORDED_REASONS_SPEC,
}

_DAY_SKIP_SPEC = {
    "schema_version": "int",
    "record_kind": "ident",
    "business_date": "date",
    "batch_id": "ident",
    "shadow_mode": "code",
    "reason": _enum(*DAY_SKIP_REASONS),
    "key_unavailable_reason": _enum(*(r.value for r in KeyUnavailableReason)),
    "unrecorded_owner_count": "int",
}

INDEX_ID_PREFIX: Final = "allocation_shadow_index"
DAY_SKIP_ID_PREFIX: Final = "allocation_shadow_day_skip"
ITEMS_ID_PREFIX: Final = "allocation_shadow_items"


def index_audit_id(business_date: dt.date) -> str:
    """日単位の索引の決定的な audit_id(owner を持たない)。"""
    return f"{INDEX_ID_PREFIX}:{_date_text(business_date)}"


def day_skip_audit_id(batch_id: str) -> str:
    """日単位の skip の決定的な audit_id(owner を持たない)。"""
    return f"{DAY_SKIP_ID_PREFIX}:{_ident_text(batch_id, 'batch_id')}"


def items_audit_id(business_date: dt.date, owner_ref: KeyedOwnerRef) -> str:
    """項目つきの記録の決定的な audit_id。owner は鍵つきの符号(kv1)だけ。"""
    return f"{ITEMS_ID_PREFIX}:{_date_text(business_date)}:{require_keyed(owner_ref)}"


def _date_text(value: dt.date) -> str:
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        raise _schema_fail("business_date", "must be a date")
    return value.isoformat()


def _ident_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise _schema_fail(name, "has the wrong form")
    return value


def _count(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _schema_fail(name, "must be a non-negative int")
    return value


def build_index_record(
    *,
    business_date: dt.date,
    batch_id: str,
    shadow_mode: str,
    key_version: int,
    recorded: Sequence[KeyedOwnerRef],
    unrecorded_reasons: Mapping[str, int],
) -> dict[str, Any]:
    """日単位の索引。recorded = ★ 記録が実際に存在する owner の符号(kv1)だけ ★。

    書けなかった owner は `unrecorded_reasons`(理由コード別の件数)で数える。件数の整合
    (attempted = recorded + unrecorded、unrecorded = 理由別の合計)は builder が計算し、
    validate も検査する。
    """
    refs = [require_keyed(ref) for ref in recorded]
    if len(set(refs)) != len(refs):
        raise _schema_fail("owner_refs", "has a duplicate")
    if isinstance(key_version, bool) or not isinstance(key_version, int):
        raise _schema_fail("key_version", "must be an int")
    if any(parse_keyed_ref(ref)[0] != key_version for ref in refs):
        raise _schema_fail("owner_refs", "does not match the key version")
    reasons = {}
    for code, count in unrecorded_reasons.items():
        if code not in UNRECORDED_REASONS:
            raise _schema_fail("unrecorded_reasons", "has a reason outside the vocabulary")
        if _count(count, "unrecorded_reasons"):
            reasons[code] = count
    unrecorded = sum(reasons.values())
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": INDEX_RECORD_KIND,
        "business_date": _date_text(business_date),
        "batch_id": _ident_text(batch_id, "batch_id"),
        "shadow_mode": shadow_mode,
        "key_version": key_version,
        "owner_refs": sorted(refs),
        "owner_refs_truncated": reasons.get("OWNER_CAP", 0) > 0,
        "attempted_owner_count": len(refs) + unrecorded,
        "recorded_owner_count": len(refs),
        "unrecorded_owner_count": unrecorded,
        "unrecorded_reasons": dict(sorted(reasons.items())),
    }
    validate_index_record(record)
    return record


def validate_index_record(record: Mapping[str, Any]) -> None:
    """索引が allowlist に適合し、件数が整合しているか。不適合なら RecordSchemaError。"""
    if not isinstance(record, dict):
        raise _schema_fail("record", "must be an object")
    _check(_INDEX_SPEC, record, "record")
    if record["schema_version"] != SCHEMA_VERSION or record["record_kind"] != INDEX_RECORD_KIND:
        raise _schema_fail("record", "is not this kind or version")
    refs = record["owner_refs"]
    if len(set(refs)) != len(refs):
        raise _schema_fail("owner_refs", "has a duplicate")
    key_versions = set()
    for ref in refs:
        try:
            key_versions.add(parse_keyed_ref(ref)[0])
        except IdentifierRefError:
            raise _schema_fail("owner_refs", "has a reference that is not keyed") from None
    if not MIN_KEY_VERSION <= record["key_version"] <= MAX_KEY_VERSION:
        raise _schema_fail("key_version", "is out of range")
    if key_versions - {record["key_version"]}:
        raise _schema_fail("key_version", "does not match the owner references")
    reasons = record["unrecorded_reasons"]
    if set(reasons) - set(UNRECORDED_REASONS):
        raise _schema_fail("unrecorded_reasons", "has a reason outside the vocabulary")
    if any(count < 1 for count in reasons.values()):
        raise _schema_fail("unrecorded_reasons", "has a non-positive count")
    if record["recorded_owner_count"] != len(refs):
        raise _schema_fail("recorded_owner_count", "does not match the owner references")
    if record["unrecorded_owner_count"] != sum(reasons.values()):
        raise _schema_fail("unrecorded_owner_count", "does not match the reasons")
    if record["attempted_owner_count"] != len(refs) + record["unrecorded_owner_count"]:
        raise _schema_fail("attempted_owner_count", "does not match the counts")
    if record["owner_refs_truncated"] != (reasons.get("OWNER_CAP", 0) > 0):
        raise _schema_fail("owner_refs_truncated", "does not match the reasons")


def build_day_skip_record(
    *,
    business_date: dt.date,
    batch_id: str,
    shadow_mode: str,
    reason: KeyUnavailableReason,
    unrecorded_owner_count: int,
) -> dict[str, Any]:
    """鍵が取れない日の記録(owner を持たない)。理由コードと記録できなかった件数だけ。"""
    if not isinstance(reason, KeyUnavailableReason):
        raise _schema_fail("reason", "must be a KeyUnavailableReason")
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": DAY_SKIP_RECORD_KIND,
        "business_date": _date_text(business_date),
        "batch_id": _ident_text(batch_id, "batch_id"),
        "shadow_mode": shadow_mode,
        "reason": "KEY_UNAVAILABLE",
        "key_unavailable_reason": reason.value,
        "unrecorded_owner_count": _count(unrecorded_owner_count, "unrecorded_owner_count"),
    }
    validate_day_skip_record(record)
    return record


def validate_day_skip_record(record: Mapping[str, Any]) -> None:
    if not isinstance(record, dict):
        raise _schema_fail("record", "must be an object")
    _check(_DAY_SKIP_SPEC, record, "record")
    if record["schema_version"] != SCHEMA_VERSION or record["record_kind"] != DAY_SKIP_RECORD_KIND:
        raise _schema_fail("record", "is not this kind or version")
