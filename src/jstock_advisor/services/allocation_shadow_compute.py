"""購入側 Shadow の『計算の組み立て』(Issue #603 PR-F2-2a)。配線なし・dormant。

#603 の Portfolio Allocation を、永続済みの BUY 推奨・保有・買付余力から 1 owner ずつ計算し、
F2-1 の項目つきの記録(`allocation_shadow_items`)の辞書にして返す。**どこからも import されない**
(handler の配線・kv1 の鍵・索引・書込は PR-F2-2b)。設計: #603 …6139444045(rev3)・…6157822822(rev4)。

## 責務

  1. `prepare_batch_inputs`: バッチ共通の入力を作る(保有の Scan 1 回・推奨の一括取得 1 回。
     候補の RAER は owner に依らないので 1 回だけ計算する)
  2. `compute_owner`: owner 1 人の計算(買付余力の読取 1 回 → F1 の組み立て → 配分 → F2-1 の記録)

## 契約

  * ★ 実在の AWS・repository に触れない: 読取は `ShadowInputReader`(Protocol)の注入だけ。F2-2b が
    実在の reader を差し込む。この module は repository・boto3 を import しない
  * ★ 例外を送出しない: 読取の失敗・期限・計算の例外は、理由コードつきの結果(header だけの記録)に
    畳む。値・例外のメッセージを記録しない(型名だけ)
  * ★ 補完しない: 価格の欠測・推奨の欠測・観測できないリスクの印は、0・前回値・中立値で補わず、
    F1 の規則(算出不能・除外)へ渡す。推奨のない保有は `RiskNotRecorded`
  * ★ owner の符号は呼び出し側(F2-2b)が `resolve_owner_ref` で作って渡す(`KeyedOwnerRef`)。
    この module は鍵・kv0 の符号関数に触れない。owner の平文は F1 の入力(メモリ上)にだけ使い、記録・
    ログ・例外へ出さない
  * 時計を読まない(`now` は引数)。I/O は期限つきの使い捨てワーカー(`call_with_timeout`)の中

## I/O の数(案 β の枠)

  バッチ共通 = 保有の Scan 1 + 推奨の一括取得 1(内部で ≤ 5 回の BatchGetItem)
  owner ごと = 買付余力 1
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.domain.entities.enums import BuyIndustrySector
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.identifier_ref import KeyedOwnerRef
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware
from jstock_advisor.domain.price_freshness import (
    PriceFreshnessVerdict,
    evaluate_buy_price_freshness,
    evaluate_holdings_price_freshness,
)
from jstock_advisor.domain.signals.allocation_inputs import (
    CandidateRecord,
    CashInput,
    HoldingRecord,
    HoldingRisk,
    RiskNotRecorded,
    RiskObserved,
    assemble_allocation_inputs,
)
from jstock_advisor.domain.signals.allocation_policy_config import AllocationPolicyBundle
from jstock_advisor.domain.signals.allocation_shadow_record import (
    CandidateDetail,
    CashSnapshot,
    RecordMeta,
    build_header_record,
    build_items_record,
)
from jstock_advisor.domain.valuation.raer import (
    COMBINED_MODEL_VERSION,
    RaerEvaluation,
    RiskContributionStatus,
    RiskFlag,
    to_raer_result,
)
from jstock_advisor.domain.valuation.raer_composition import (
    OperatingTrendScore,
    RaerCoefficients,
    RaerInputs,
    compose_raer,
)
from jstock_advisor.domain.valuation.raer_inputs import (
    AssemblerPolicy,
    InputProvenance,
    RiskFlagSource,
    assemble_raer_inputs,
    facts_from_recommendation,
    holding_market_view_from_recommendation,
    risk_flag_view_from_recommendation,
)
from jstock_advisor.services.allocation_shadow_service import (
    CashRead,
    CashReadStatus,
    Deadline,
    IoTimeoutError,
    ShadowOutcome,
    ShadowResult,
    ShadowSkipReason,
    call_with_timeout,
    read_available_cash,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

#: 候補 ∪ 保有の銘柄数の上限(BatchGetItem を 5 回まで)。超える日は読まずにスキップ
MAX_RECOMMENDATIONS: Final = 500

_DERIVED: Final = "DERIVED"
_NOTE: Final = "NOTE"


class ShadowInputReader(Protocol):
    """入力の読取元(注入。F2-2b が実在の repository を包んで渡す)。例外は呼び出し側が理由に畳む。"""

    def read_holdings(self) -> Sequence[Holding]: ...

    def read_recommendations(self, ids: Sequence[str]) -> Mapping[str, Recommendation]: ...

    def read_available_cash(self, owner: str) -> AvailableCash | None: ...


@dataclass(frozen=True)
class ComputeConfig:
    """計算に使う設定一式(呼び出し側が既存の config loader から作る。新しい閾値を作らない)。"""

    bundle: AllocationPolicyBundle
    coefficients: RaerCoefficients | None  # None = 係数が読めない(縮退)
    assembler_policy: AssemblerPolicy
    calendar: BusinessCalendar
    shadow_mode: str = "SHADOW"


@dataclass(frozen=True)
class BatchSpec:
    """バッチ共通の指定。候補の推奨 ID は finalize の ranking entries から呼び出し側が渡す。"""

    batch_id: str
    now: dt.datetime
    candidate_ids: Mapping[str, str]  # 銘柄コード → 推奨 ID(BUY 候補)
    holding_recommendation_id: Callable[[str], str]  # 銘柄コード → 決定的な推奨 ID(保有の銘柄)


@dataclass(frozen=True)
class BatchInputs:
    """バッチ共通の入力(owner に依らない部分は計算済み)。"""

    batch_id: str
    now: dt.datetime
    business_date: dt.date
    config: ComputeConfig
    candidates: tuple[CandidateRecord, ...]
    details: Mapping[str, CandidateDetail]
    recommendations: Mapping[str, Recommendation]  # 銘柄コード → 推奨(保有側の事実の出所)
    holdings_by_owner: Mapping[str, tuple[Holding, ...]]
    read_states: Mapping[str, str]

    @property
    def owners(self) -> tuple[str, ...]:
        """保有に現れる owner(昇順)。買付余力だけを持つ owner は対象外(Scan を増やさない)。"""
        return tuple(sorted(self.holdings_by_owner))


@dataclass(frozen=True)
class BatchUnavailable:
    """バッチ共通の入力を作れなかった(全 owner が同じ理由で算出不能)。"""

    reason: ShadowSkipReason
    read_states: Mapping[str, str]


@dataclass(frozen=True)
class OwnerComputation:
    """owner 1 人分の結果。result = 枠の記録の元(scalar だけ)/ record = 項目つき・header の記録。"""

    result: ShadowResult
    record: dict[str, Any] | None  # None = header すら作れなかった(枠の記録だけ書く)


def _timed[T](fn: Callable[[], T], deadline: Deadline, io_timeout_seconds: float) -> T:
    """総予算の残りと I/O の上限の小さい方だけ待つ。残りが無ければ IoTimeoutError。"""
    timeout = min(io_timeout_seconds, deadline.remaining())
    if timeout <= 0.0:
        raise IoTimeoutError()
    return call_with_timeout(fn, timeout)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _risk_of_holding(rec: Recommendation | None) -> HoldingRisk:
    """保有の印。推奨が無い・検出の記録が無い(旧い記録)ときは『観測できていない』側へ倒す。"""
    if rec is None:
        return RiskNotRecorded()
    view = risk_flag_view_from_recommendation(rec)
    flags = frozenset(flag.value for flag in view.flags)
    if view.source is RiskFlagSource.DETECTION_RECORD:
        return RiskObserved(flags, frozenset(flag.value for flag in view.unobserved))
    # 旧い記録(安全余裕率の調整)・観測なし: 観測できなかった語を区別できない = 全語が未観測
    return RiskObserved(flags, frozenset(flag.value for flag in RiskFlag) - flags)


def _candidate_detail(
    rec: Recommendation,
    inputs: RaerInputs,
    evaluation: RaerEvaluation,
    provenance: InputProvenance,
    verdict: PriceFreshnessVerdict,
) -> CandidateDetail:
    composition = evaluation.composition
    quality = inputs.quality
    trend = inputs.operating_trend
    trend_score = trend if isinstance(trend, OperatingTrendScore) else None
    confidence = inputs.expected_return.fair_value_confidence
    raer_in: dict[str, Any] = {
        "upside_pct": _text(composition.upside_pct) if composition else None,
        "income_pct": _text(composition.income_pct) if composition else None,
        "fair_value_confidence": confidence.value if confidence is not None else None,
        "quality_score": _text(quality.holding_quality_score_0_50) if quality else None,
        "quality_coverage_ratio": _text(quality.coverage_ratio) if quality else None,
        "quality_min_coverage": _text(quality.min_coverage) if quality else None,
        "trend_score": _text(trend_score.value) if trend_score else None,
        "trend_partial_coverage": _text(trend_score.partial_coverage) if trend_score else None,
        "trend_components_used": (
            sorted(component.value.lower() for component in trend_score.components_used)
            if trend_score
            else None
        ),
        "trend_weights": (
            {
                "operating_income": _text(trend_score.weights.operating_income),
                "operating_cashflow": _text(trend_score.weights.operating_cashflow),
                "dividend_direction": _text(trend_score.weights.dividend_direction),
                "acceleration": _text(trend_score.weights.acceleration),
            }
            if trend_score
            else None
        ),
        "risk_flags_observed": inputs.risk_flags is not None,
        "company_quality_model_version": quality.company_quality_model_version if quality else None,
        "earnings_trend_model_version": (
            trend_score.earnings_trend_model_version if trend_score else None
        ),
    }
    extra: dict[str, Any] = {}
    if composition is not None:
        extra = {
            "coefficient_set_id": composition.coefficient_set_id,
            "contributions": {c.axis.value: _text(c.pct_points) for c in composition.contributions},
            "risk_contributions": {
                item.flag.value: _text(item.deduction_pct)
                for item in composition.risk_items
                if item.status is RiskContributionStatus.COUNTED
            },
            "base": {
                "upside": _text(composition.upside_pct),
                "income": _text(composition.income_pct),
                "base": _text(composition.base_pct),
            },
            "spec_flags": sorted(flag.value for flag in composition.spec_flags),
        }
    notes: dict[str, str] = {}
    for entry in provenance.entries:
        notes[entry.item.value] = _DERIVED if entry.derived else str(entry.reason.value)  # type: ignore[union-attr]
        for note in entry.notes:
            notes[note.value] = _NOTE
    return CandidateDetail(
        fair_value_confidence=confidence.value if confidence is not None else None,
        price_as_of_date=rec.price_as_of_date,
        freshness_verdict=verdict.value,
        raer_in=raer_in,
        raer_extra=extra,
        provenance=notes,
    )


def _candidates(
    spec: BatchSpec,
    config: ComputeConfig,
    coefficients: RaerCoefficients,
    recs: Mapping[str, Recommendation],
    evaluation_date: dt.date,
) -> tuple[list[CandidateRecord], dict[str, CandidateDetail], int]:
    """候補の CandidateRecord と付加情報。推奨が無い・組み立てに失敗した候補は数える(補わない)。"""
    records: list[CandidateRecord] = []
    details: dict[str, CandidateDetail] = {}
    skipped = 0
    for stock_code in sorted(spec.candidate_ids):
        rec = recs.get(spec.candidate_ids[stock_code])
        if rec is None:
            skipped += 1
            continue
        try:
            verdict, _reason = evaluate_buy_price_freshness(
                rec.price_as_of_date, spec.now, config.calendar
            )
            assembled = assemble_raer_inputs(
                facts_from_recommendation(rec),
                evaluation_date=evaluation_date,
                price_freshness=verdict,
                policy=config.assembler_policy,
            )
            evaluation = compose_raer(
                evaluation_date=evaluation_date, inputs=assembled.inputs, coefficients=coefficients
            )
            price = (
                None if verdict is PriceFreshnessVerdict.HARD_STOP else rec.price_at_recommendation
            )
            records.append(
                CandidateRecord(
                    stock_code=stock_code,
                    buy_action=rec.buy_action,
                    current_price=price,
                    sector=rec.buy_industry_sector or BuyIndustrySector.UNKNOWN,
                    trading_unit=rec.trading_unit,
                    raer=to_raer_result(evaluation),
                )
            )
            details[stock_code] = _candidate_detail(
                rec, assembled.inputs, evaluation, assembled.provenance, verdict
            )
        except Exception as exc:  # noqa: BLE001 - 1 件の失敗で他の候補を止めない(型名だけ記録)
            logger.warning("allocation shadow candidate skipped (%s)", type(exc).__name__)
            skipped += 1
    return records, details, skipped


def prepare_batch_inputs(
    spec: BatchSpec,
    config: ComputeConfig,
    reader: ShadowInputReader,
    *,
    deadline: Deadline,
    io_timeout_seconds: float,
) -> BatchInputs | BatchUnavailable:
    """バッチ共通の入力を作る。例外を送出しない(失敗は理由コードに畳む)。"""
    states: dict[str, str] = {}
    try:
        require_timezone_aware(spec.now)
        if config.coefficients is None:
            return BatchUnavailable(ShadowSkipReason.COEFFICIENTS_UNAVAILABLE, states)
        try:
            holdings = list(_timed(reader.read_holdings, deadline, io_timeout_seconds))
        except IoTimeoutError:
            states["holdings"] = "TIMEOUT"
            return BatchUnavailable(ShadowSkipReason.TIME_LIMIT, states)
        except Exception as exc:  # noqa: BLE001
            logger.warning("allocation shadow holdings read failed (%s)", type(exc).__name__)
            states["holdings"] = "ERROR"
            return BatchUnavailable(ShadowSkipReason.HOLDINGS_READ_FAILED, states)
        states["holdings"] = "OK"

        held = [h for h in holdings if h.shares >= 1]
        ids: dict[str, str] = dict(spec.candidate_ids)
        for holding in held:
            ids.setdefault(holding.stock_code, spec.holding_recommendation_id(holding.stock_code))
        id_list = sorted(set(ids.values()))
        if len(id_list) > MAX_RECOMMENDATIONS:
            states["recommendations"] = "NOT_READ"
            return BatchUnavailable(ShadowSkipReason.INPUT_LIMIT_EXCEEDED, states)
        try:
            recs_by_id = dict(
                _timed(lambda: reader.read_recommendations(id_list), deadline, io_timeout_seconds)
            )
        except IoTimeoutError:
            states["recommendations"] = "TIMEOUT"
            return BatchUnavailable(ShadowSkipReason.TIME_LIMIT, states)
        except Exception as exc:  # noqa: BLE001
            logger.warning("allocation shadow recommendations read failed (%s)", type(exc).__name__)
            states["recommendations"] = "ERROR"
            return BatchUnavailable(ShadowSkipReason.RECOMMENDATIONS_READ_FAILED, states)

        evaluation_date = evaluation_date_jst(spec.now)
        records, details, skipped = _candidates(
            spec, config, config.coefficients, recs_by_id, evaluation_date
        )
        states["recommendations"] = "OK" if skipped == 0 else "PARTIAL"
        by_owner: dict[str, list[Holding]] = {}
        for holding in held:
            by_owner.setdefault(holding.owner, []).append(holding)
        by_code = {code: recs_by_id[rec_id] for code, rec_id in ids.items() if rec_id in recs_by_id}
        return BatchInputs(
            batch_id=spec.batch_id,
            now=spec.now,
            business_date=evaluation_date,
            config=config,
            candidates=tuple(records),
            details=details,
            recommendations=by_code,
            holdings_by_owner={
                owner: tuple(sorted(items, key=lambda h: h.stock_code))
                for owner, items in by_owner.items()
            },
            read_states=dict(states),
        )
    except Exception as exc:  # noqa: BLE001 - 契約: 例外を送出しない
        logger.warning("allocation shadow batch inputs failed (%s)", type(exc).__name__)
        return BatchUnavailable(ShadowSkipReason.COMPUTATION_FAILED, states)


def _holding_records(inputs: BatchInputs, owner_holdings: Sequence[Holding]) -> list[HoldingRecord]:
    records: list[HoldingRecord] = []
    for holding in owner_holdings:
        rec = inputs.recommendations.get(holding.stock_code)
        price: Decimal | None = None
        sector = BuyIndustrySector.UNKNOWN
        if rec is not None:
            view = holding_market_view_from_recommendation(rec)
            verdict, _reason = evaluate_holdings_price_freshness(
                view.price_as_of_date, inputs.now, inputs.config.calendar
            )
            if verdict is not PriceFreshnessVerdict.DATA_INSUFFICIENT:
                price = view.price
            sector = view.buy_industry_sector
        records.append(
            HoldingRecord(
                owner=holding.owner,
                stock_code=holding.stock_code,
                shares=holding.shares,
                current_price=price,
                sector=sector,
                risk=_risk_of_holding(rec),
            )
        )
    return records


def _skip(
    inputs: BatchInputs,
    meta: RecordMeta,
    cash: CashSnapshot,
    reason: ShadowSkipReason,
    outcome: ShadowOutcome = ShadowOutcome.SKIPPED,
    facts: Mapping[str, Any] | None = None,
) -> OwnerComputation:
    record = build_header_record(
        meta=meta,
        bundle=inputs.config.bundle,
        cash=cash,
        outcome=outcome.value,
        skip_reason=reason.value,
    )
    return OwnerComputation(ShadowResult(outcome, reason, dict(facts or {})), record)


def compute_owner(
    inputs: BatchInputs,
    owner: str,
    owner_ref: KeyedOwnerRef,
    *,
    reader: ShadowInputReader,
    deadline: Deadline,
    io_timeout_seconds: float,
    elapsed_ms: int | None = None,
) -> OwnerComputation:
    """owner 1 人の計算。例外を送出しない(失敗は header だけの記録 + 理由コード)。"""
    states = dict(inputs.read_states)
    no_cash = CashSnapshot(None, None, None)

    def meta() -> RecordMeta:
        return RecordMeta(
            batch_id=inputs.batch_id,
            business_date=inputs.business_date,
            owner_ref=owner_ref,
            shadow_mode=inputs.config.shadow_mode,
            raer_model_version=COMBINED_MODEL_VERSION,
            elapsed_ms=elapsed_ms,
            read_states=dict(states),
        )

    cash_snapshot = no_cash
    try:
        timeout = min(io_timeout_seconds, deadline.remaining())
        if timeout <= 0.0:
            states["cash"] = CashReadStatus.TIMEOUT.value
            return _skip(inputs, meta(), no_cash, ShadowSkipReason.TIME_LIMIT)
        cash = read_available_cash(owner, timeout_seconds=timeout, get=reader.read_available_cash)
        states["cash"] = cash.status.value
        reason = cash.as_skip_reason()
        if reason is not None or cash.amount is None:
            return _skip(inputs, meta(), no_cash, reason or ShadowSkipReason.COMPUTATION_FAILED)
        cash_snapshot = _snapshot(cash)
        holdings = _holding_records(inputs, inputs.holdings_by_owner.get(owner, ()))
        allocation_inputs = assemble_allocation_inputs(
            owner=owner,
            now=inputs.now,
            candidates=inputs.candidates,
            holdings=holdings,
            cash=CashInput(cash.amount, cash.last_reconciled_at),
            bundle=inputs.config.bundle,
        )
        result = allocation_inputs.optimize(inputs.config.bundle)
        record = build_items_record(
            meta=meta(),
            bundle=inputs.config.bundle,
            cash=cash_snapshot,
            inputs=allocation_inputs,
            result=result,
            details=inputs.details,
        )
        facts = {
            "status": result.status.value,
            "candidates": allocation_inputs.report.candidates,
            "allocated": sum(1 for a in result.allocations if a.decision.value == "ALLOCATED"),
        }
        return OwnerComputation(ShadowResult(ShadowOutcome.COMPUTED, None, facts), record)
    except Exception as exc:  # noqa: BLE001 - 契約: 例外を送出しない(型名だけ記録)
        logger.warning("allocation shadow owner compute failed (%s)", type(exc).__name__)
        try:
            return _skip(
                inputs,
                meta(),
                cash_snapshot,
                ShadowSkipReason.COMPUTATION_FAILED,
                ShadowOutcome.FAILED,
                {"error_type": type(exc).__name__},
            )
        except Exception as inner:  # noqa: BLE001 - header すら作れない(枠の記録だけ残す)
            logger.warning("allocation shadow header failed (%s)", type(inner).__name__)
            return OwnerComputation(
                ShadowResult(
                    ShadowOutcome.FAILED,
                    ShadowSkipReason.COMPUTATION_FAILED,
                    {"error_type": type(exc).__name__},
                ),
                None,
            )


def _snapshot(cash: CashRead) -> CashSnapshot:
    return CashSnapshot(cash.amount, cash.last_reconciled_at, cash.updated_at)
