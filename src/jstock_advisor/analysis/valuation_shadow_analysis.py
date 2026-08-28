"""valuation shadow分析の行生成・集計・export(Issue #20 Phase C)。

canonical出力は「1 Recommendation × 1 context × 1 hypothesis = 1 raw shadow
observation」(JSON Lines)。summaryはそこから導出可能な派生物であり、
承認済みの記述統計に限定する(成功率・優劣判定・ランキング・閾値提案は
出さない。performance結合は#28 dataset(recommendation_id join)との後段分析)。

【原則】
- 入力は保存済みRecommendationの判定時点値のみ(B1導出経由)。現在config・
  現在市場データ・Providerでの再計算はしない。
- B1のOBSERVATION_UNAVAILABLEはdropせず、status・理由付きの行として出力する。
- H_A×BUY_DECISIONでは保存済みvaluation_anchorの再構成self-checkを行い、
  saved/reconstructed/deltaを別列で出力する。どの現行式とも一致しない
  レコードはRECONSTRUCTION_MISMATCHとして可視化し、summaryのanchor差分
  統計から除外する(帳尻合わせの再計算はしない)。
- shadow価格はSHADOW_PRICEであり、約定・到達の判定はしない。
- 決定的: 同一入力から同一出力(行順・キー順・タイ規則を固定。時刻は
  呼び出し側が渡すgenerated_atのみ)。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from jstock_advisor.analysis.valuation_shadow_hypotheses import (
    ALL_HYPOTHESES,
    ANCHOR_FORMULAS,
    REPRESENTATIVE_ANCHOR_FORMULA,
    SELL_USABILITY_SHADOW_PARAMS,
    VALUATION_HYPOTHESIS_SET_VERSION,
    ValuationHypothesis,
    compute_anchor_candidates,
    reduce_population,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.valuation.valuation_spread_observation import (
    ObservationStatus,
    ValuationSpreadContext,
    ValuationSpreadObservation,
    derive_spread_observations,
)
from jstock_advisor.domain.valuation.valuation_taxonomy import (
    METHOD_DEPENDENCY_TAGS,
    VALUATION_TAXONOMY_VERSION,
)

VALUATION_SHADOW_EXPORT_SCHEMA_VERSION = "vs1"

# H_A再構成self-checkの許容誤差(vs1では厳密一致=0。判定時点の保存値からの
# 再構成であり丸めの帳尻合わせを許さない。現在config等から取得しない)。
RECONSTRUCTION_TOLERANCE = Decimal("0")


class ReconstructionStatus(StrEnum):
    """H_A×BUY_DECISIONでのsaved anchor再構成self-checkの結果。"""

    MATCHED_WEIGHTED_MEDIAN = "MATCHED_WEIGHTED_MEDIAN"
    MATCHED_MIN_WM_TRIMMED = "MATCHED_MIN_WM_TRIMMED"
    MATCHED_PERCENTILE_40 = "MATCHED_PERCENTILE_40"
    RECONSTRUCTION_MISMATCH = "RECONSTRUCTION_MISMATCH"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # saved anchorなし(LOW等)・対象外context


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _ratio(max_value: Decimal | None, min_value: Decimal | None) -> float | None:
    if max_value is None or min_value is None or min_value <= 0:
        return None
    return float(max_value / min_value)


def _effective_counts(methods: list[str]) -> dict[str, float | int | None]:
    """観測専用のeffective evidence count群(判定・usability・保存へは
    一切使用しない)。taxonomy未登録の方式が混じる場合はNone(推測しない)。"""
    if not methods:
        return {
            "effective_count_methods": 0,
            "effective_count_distinct_tag_sets": None,
            "effective_count_tag_jaccard": None,
        }
    if any(m not in METHOD_DEPENDENCY_TAGS for m in methods):
        return {
            "effective_count_methods": len(methods),
            "effective_count_distinct_tag_sets": None,
            "effective_count_tag_jaccard": None,
        }
    tag_sets = {m: METHOD_DEPENDENCY_TAGS[m] for m in methods}
    distinct = len({frozenset(tags) for tags in tag_sets.values()})
    jaccard_total = 0.0
    for m in methods:
        overlapping = sum(1 for other in methods if tag_sets[m] & tag_sets[other])
        jaccard_total += 1.0 / overlapping  # 自分自身と必ず重なるため常に>=1
    return {
        "effective_count_methods": len(methods),
        "effective_count_distinct_tag_sets": distinct,
        "effective_count_tag_jaccard": round(jaccard_total, 6),
    }


def _reconstruction(
    saved_anchor: Decimal | None, anchors: dict[str, Decimal | None]
) -> tuple[ReconstructionStatus, Decimal | None, str | None]:
    """saved anchorが現行3式(weighted_median / min(wm,tm) / p40)のどれで
    再構成できるかを判定する。戻り値は(status, 再構成anchor(一致した式の値、
    mismatch時はNone), delta(最も近い候補との差のstr Decimal))。"""
    if saved_anchor is None:
        return ReconstructionStatus.NOT_APPLICABLE, None, None
    branch_candidates: list[tuple[ReconstructionStatus, Decimal | None]] = [
        (ReconstructionStatus.MATCHED_WEIGHTED_MEDIAN, anchors["weighted_median"]),
        (ReconstructionStatus.MATCHED_MIN_WM_TRIMMED, anchors["min_wm_tm"]),
        (ReconstructionStatus.MATCHED_PERCENTILE_40, anchors["percentile_40"]),
    ]
    deltas: list[Decimal] = []
    for status, candidate in branch_candidates:
        if candidate is None:
            continue
        delta = abs(candidate - saved_anchor)
        deltas.append(delta)
        if delta <= RECONSTRUCTION_TOLERANCE:
            # delta=0のスケール表現("0.000"等)は情報を持たないため"0"へ正規化
            return status, candidate, "0" if delta == 0 else str(delta)
    if not deltas:
        return ReconstructionStatus.RECONSTRUCTION_MISMATCH, None, None
    return ReconstructionStatus.RECONSTRUCTION_MISMATCH, None, str(min(deltas))


def _unavailable_row(
    recommendation: Recommendation,
    observation: ValuationSpreadObservation,
    hypothesis: ValuationHypothesis,
) -> dict[str, object]:
    """canonical grain(1 Rec×1 context×1 仮説)をUNAVAILABLEでも維持する。
    仮説計算値(population/anchors等)は計算不能としてNone(推測計算はしない)。"""
    return {
        "recommendation_id": recommendation.recommendation_id,
        "stock_code": recommendation.stock_code,
        "recommendation_type": recommendation.recommendation_type.value,
        "recommended_at": recommendation.recommended_at.isoformat(),
        "context": observation.context.value,
        "observation_status": observation.status.value,
        "unavailable_reason": observation.unavailable_reason,
        "hypothesis_id": hypothesis.hypothesis_id,
        "hypothesis_origin": hypothesis.origin.value,
        "population": None,
        "population_count": None,
        "population_spread_ratio": None,
        "anchors": None,
    }


def _base_row(
    recommendation: Recommendation,
    observation: ValuationSpreadObservation,
    hypothesis: ValuationHypothesis,
    population: list[tuple[str, Decimal]],
    values: list[Decimal],
    anchors: dict[str, Decimal | None],
) -> dict[str, object]:
    values_by_method = dict(observation.values)
    row: dict[str, object] = {
        "recommendation_id": recommendation.recommendation_id,
        "stock_code": recommendation.stock_code,
        "recommendation_type": recommendation.recommendation_type.value,
        "recommended_at": recommendation.recommended_at.isoformat(),
        "context": observation.context.value,
        "observation_status": observation.status.value,
        "unavailable_reason": None,
        "hypothesis_id": hypothesis.hypothesis_id,
        "hypothesis_origin": hypothesis.origin.value,
        "source_methods_count": observation.methods_count,
        "source_min_method": observation.min_method,
        "source_max_method": observation.max_method,
        "source_spread_ratio": observation.spread_ratio,
        "excluded_methods": [
            {
                "method": e.method,
                "code": e.code,
                "actual_value": _s(e.actual_value),
                "reference_value": _s(e.reference_value),
            }
            for e in observation.excluded
        ],
        "population": [[label, str(value)] for label, value in population],
        "population_count": len(population),
        "population_spread_ratio": _ratio(
            max(values) if values else None, min(values) if values else None
        ),
        "anchors": {name: _s(anchors[name]) for name in ANCHOR_FORMULAS},
    }
    row.update(_effective_counts(sorted(values_by_method)))
    return row


def _add_buy_columns(
    row: dict[str, object],
    recommendation: Recommendation,
    anchors: dict[str, Decimal | None],
    *,
    with_reconstruction: bool,
) -> None:
    saved_anchor = recommendation.valuation_anchor
    row["saved_valuation_anchor"] = _s(saved_anchor)
    deltas: dict[str, float | None] = {}
    for name in ANCHOR_FORMULAS:
        candidate = anchors[name]
        if candidate is None or saved_anchor is None or saved_anchor <= 0:
            deltas[name] = None
        else:
            deltas[name] = float((candidate - saved_anchor) / saved_anchor * 100)
    row["anchor_delta_pct"] = deltas

    if with_reconstruction:
        status, reconstructed, delta = _reconstruction(saved_anchor, anchors)
        row["reconstruction_status"] = status.value
        row["shadow_reconstructed_anchor"] = _s(reconstructed)
        row["reconstruction_delta"] = delta

    representative = anchors[REPRESENTATIVE_ANCHOR_FORMULA]
    shadow_prices: dict[str, str | None] = {}
    for label, margin in (
        ("shadow_entry_price", recommendation.required_margin_of_safety_entry),
        ("shadow_standard_price", recommendation.required_margin_of_safety_standard),
        ("shadow_strong_price", recommendation.required_margin_of_safety_strong),
    ):
        # marginは判定時点の保存値のみ使用(未保存ならNone。現在configを使わない)。
        # SHADOW_PRICEであり約定・到達判定には使わない。
        if representative is None or margin is None:
            shadow_prices[label] = None
        else:
            shadow_prices[label] = str(representative * (Decimal("1") - margin))
    row["representative_anchor_formula"] = REPRESENTATIVE_ANCHOR_FORMULA
    row["shadow_prices"] = shadow_prices


def _add_sell_columns(
    row: dict[str, object], recommendation: Recommendation, values: list[Decimal]
) -> None:
    shadow_bear = min(values) if values else None
    shadow_bull = max(values) if values else None
    shadow_spread = _ratio(shadow_bull, shadow_bear)
    max_ratio = float(SELL_USABILITY_SHADOW_PARAMS["max_method_spread_ratio"])
    min_methods = int(SELL_USABILITY_SHADOW_PARAMS["min_methods_required"])
    shadow_usable = not (
        len(values) < min_methods
        or (shadow_spread is not None and shadow_spread >= max_ratio)
    )
    saved_usable = recommendation.fair_value_usable_for_trading_judgment
    row["saved_fair_value_bear"] = _s(recommendation.fair_value_bear)
    row["saved_fair_value_bull"] = _s(recommendation.fair_value_bull)
    row["saved_fair_value_spread_ratio"] = recommendation.fair_value_spread_ratio
    row["saved_usable_for_trading_judgment"] = saved_usable
    row["saved_unusable_reason_code"] = recommendation.fair_value_unusable_reason_code
    row["shadow_bear"] = _s(shadow_bear)
    row["shadow_bull"] = _s(shadow_bull)
    row["shadow_spread_ratio"] = shadow_spread
    row["shadow_usable_for_trading_judgment"] = shadow_usable
    # flip: 保存済みhistorical factが存在する場合のみ比較(Noneは比較不能)
    row["usability_flip"] = (
        None if saved_usable is None else (saved_usable != shadow_usable)
    )
    row["sell_usability_shadow_params"] = dict(SELL_USABILITY_SHADOW_PARAMS)


def build_shadow_rows(recommendation: Recommendation) -> list[dict[str, object]]:
    """1 Recommendationの全raw shadow observation行を生成する(決定的)。"""
    rows: list[dict[str, object]] = []
    for observation in derive_spread_observations(recommendation):
        if observation.status is ObservationStatus.OBSERVATION_UNAVAILABLE:
            # canonical grain統一: UNAVAILABLEでも全仮説分の行を生成する
            # (仮説別denominator・unavailable率を後段で計算可能にする)。
            rows.extend(
                _unavailable_row(recommendation, observation, hypothesis)
                for hypothesis in ALL_HYPOTHESES
            )
            continue
        for hypothesis in ALL_HYPOTHESES:
            values_by_method = dict(observation.values)
            population = reduce_population(values_by_method, hypothesis.clusters)
            values = [v for _, v in population]
            anchors = compute_anchor_candidates(values)
            row = _base_row(recommendation, observation, hypothesis, population, values, anchors)
            if observation.context in (
                ValuationSpreadContext.BUY_RAW,
                ValuationSpreadContext.BUY_DECISION,
            ):
                _add_buy_columns(
                    row,
                    recommendation,
                    anchors,
                    with_reconstruction=(
                        observation.context is ValuationSpreadContext.BUY_DECISION
                        and hypothesis.clusters is None
                    ),
                )
            else:
                _add_sell_columns(row, recommendation, values)
            rows.append(row)
    return rows


@dataclass(frozen=True)
class ShadowExportResult:
    """row_countはraw shadow行数(1 Rec×1 context×1 仮説、metadata行を除く)。
    unavailable_shadow_row_countは仮説展開後のUNAVAILABLE行数、
    unavailable_context_countは観測不能だった(Recommendation, context)組数
    (=展開前の件数)。両者を混同しないこと。"""

    row_count: int
    recommendation_count: int
    unavailable_shadow_row_count: int
    unavailable_context_count: int
    reconstruction_mismatch_count: int


def build_metadata(
    *, generated_at: dt.datetime, recommendation_count: int, row_count: int
) -> dict[str, object]:
    return {
        "record_kind": "metadata",
        "valuation_shadow_export_schema_version": VALUATION_SHADOW_EXPORT_SCHEMA_VERSION,
        "valuation_taxonomy_version": VALUATION_TAXONOMY_VERSION,
        "valuation_hypothesis_set_version": VALUATION_HYPOTHESIS_SET_VERSION,
        "sell_usability_shadow_params": dict(SELL_USABILITY_SHADOW_PARAMS),
        "hypotheses": [
            {
                "hypothesis_id": h.hypothesis_id,
                "origin": h.origin.value,
                "clusters": (
                    None
                    if h.clusters is None
                    else [sorted(cluster) for cluster in h.clusters]
                ),
                "derivation_note": h.derivation_note,
                "aliases": list(h.aliases),
            }
            for h in ALL_HYPOTHESES
        ],
        "generated_at": generated_at.isoformat(),
        "recommendation_count": recommendation_count,
        "row_count": row_count,
    }


def _row_sort_key(row: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(row.get("recommendation_id", "")),
        str(row.get("context", "")),
        str(row.get("hypothesis_id") or ""),
    )


def write_shadow_export(
    recommendations: list[Recommendation],
    output_path: Path,
    *,
    generated_at: dt.datetime,
    summary_path: Path | None = None,
) -> ShadowExportResult:
    """canonical raw shadow export(JSON Lines、先頭行がmetadata)を書き出す。
    summary_path指定時は承認済みの記述統計のみのCSVも書き出す。"""
    ordered = sorted(recommendations, key=lambda r: r.recommendation_id)
    rows: list[dict[str, object]] = []
    for recommendation in ordered:
        rows.extend(build_shadow_rows(recommendation))
    rows.sort(key=_row_sort_key)

    metadata = build_metadata(
        generated_at=generated_at,
        recommendation_count=len(ordered),
        row_count=len(rows),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    unavailable_rows = [
        row
        for row in rows
        if row["observation_status"] == ObservationStatus.OBSERVATION_UNAVAILABLE.value
    ]
    unavailable_contexts = {
        (row["recommendation_id"], row["context"]) for row in unavailable_rows
    }
    mismatch = sum(
        1
        for row in rows
        if row.get("reconstruction_status") == ReconstructionStatus.RECONSTRUCTION_MISMATCH.value
    )
    if summary_path is not None:
        _write_summary(rows, summary_path)
    return ShadowExportResult(
        row_count=len(rows),
        recommendation_count=len(ordered),
        unavailable_shadow_row_count=len(unavailable_rows),
        unavailable_context_count=len(unavailable_contexts),
        reconstruction_mismatch_count=mismatch,
    )


def _quantiles(values: list[float]) -> tuple[float | None, float | None, float | None]:
    if not values:
        return None, None, None
    if len(values) == 1:
        return values[0], values[0], values[0]
    p25, p50, p75 = statistics.quantiles(values, n=4)
    return p25, p50, p75


def _write_summary(rows: list[dict[str, object]], summary_path: Path) -> None:
    """承認済みの記述統計のみ(§11): 件数・UNAVAILABLE・mismatch・仮説別
    sample数・anchor delta分布・spread分布・usability flip・除外件数。
    最良仮説決定・ランキング・閾値提案は出力しない。

    anchor delta統計は、同一recommendationがRECONSTRUCTION_MISMATCHの場合
    そのcontextの全行を除外する(帳尻の合わない再構成を性能比較sampleへ
    混ぜない)。
    """
    mismatch_keys = {
        (row["recommendation_id"], row["context"])
        for row in rows
        if row.get("reconstruction_status") == ReconstructionStatus.RECONSTRUCTION_MISMATCH.value
    }
    # 単位の定義(§件数semantics):
    # - raw shadow row = 1 Recommendation×1 context×1 仮説(UNAVAILABLE含む)
    # - sample_count = そのcontext×仮説のAVAILABLE行数(mismatch除外後)
    # - unavailable_row_count = そのcontext×仮説のUNAVAILABLE行数
    # - _UNAVAILABLE_CONTEXTS_ = 観測不能な(Recommendation, context)組数
    #   (仮説展開前の件数。shadow行数と混同しない)
    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        if row.get("hypothesis_id") is None:
            continue
        groups.setdefault((str(row["context"]), str(row["hypothesis_id"])), []).append(row)

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "context",
                "hypothesis_id",
                "sample_count",
                "unavailable_row_count",
                "reconstruction_excluded_count",
                "anchor_delta_pct_representative_p25",
                "anchor_delta_pct_representative_p50",
                "anchor_delta_pct_representative_p75",
                "population_spread_ratio_p25",
                "population_spread_ratio_p50",
                "population_spread_ratio_p75",
                "usability_flip_count",
                "excluded_method_row_count",
            ]
        )
        unavailable_all = [
            row
            for row in rows
            if row["observation_status"] == ObservationStatus.OBSERVATION_UNAVAILABLE.value
        ]
        unavailable_contexts = {
            (row["recommendation_id"], row["context"]) for row in unavailable_all
        }
        writer.writerow(
            ["_ALL_", "_UNAVAILABLE_SHADOW_ROWS_", "", len(unavailable_all)] + [""] * 9
        )
        writer.writerow(
            ["_ALL_", "_UNAVAILABLE_CONTEXTS_", "", len(unavailable_contexts)] + [""] * 9
        )
        for (context, hypothesis_id), group in sorted(groups.items()):
            available = [
                row
                for row in group
                if row["observation_status"] != ObservationStatus.OBSERVATION_UNAVAILABLE.value
            ]
            unavailable_count = len(group) - len(available)
            eligible = [
                row
                for row in available
                if (row["recommendation_id"], row["context"]) not in mismatch_keys
            ]
            excluded_count = len(available) - len(eligible)
            deltas = [
                d[REPRESENTATIVE_ANCHOR_FORMULA]
                for row in eligible
                if isinstance(d := row.get("anchor_delta_pct"), dict)
                and d.get(REPRESENTATIVE_ANCHOR_FORMULA) is not None
            ]
            spreads = [
                s for row in eligible if isinstance(s := row.get("population_spread_ratio"), float)
            ]
            flips = sum(1 for row in eligible if row.get("usability_flip") is True)
            excluded_rows = sum(1 for row in eligible if row.get("excluded_methods"))
            d25, d50, d75 = _quantiles([float(v) for v in deltas])
            s25, s50, s75 = _quantiles(spreads)
            writer.writerow(
                [
                    context,
                    hypothesis_id,
                    len(eligible),
                    unavailable_count,
                    excluded_count,
                    d25,
                    d50,
                    d75,
                    s25,
                    s50,
                    s75,
                    flips,
                    excluded_rows,
                ]
            )
