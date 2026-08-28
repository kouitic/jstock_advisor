"""Calibration Dataset Builder(Issue #28 Phase B)。

Recommendation / EvaluationResult / DecisionSnapshot(いずれもread-only)を
結合し、「1 Recommendation × 1 evaluation horizon = 1 raw row」の正規化
datasetを決定的に生成する。判定変更・成功/失敗の再定義・閾値/margin較正・
統計分析(成功率・CI・bootstrap等)は一切行わない(Phase C以降の責務)。

設計原則:
- Recommendationはdedupしない(RAW datasetが正本の粒度)。sample定義
  (NON_OVERLAPPING_WINDOW等)は行を削除せずsample_selected等をannotateする
- EvaluationResult未生成でもrowを生成し、row_statusで
  EVALUATED / NOT_YET_EVALUABLE / EVALUATION_MISSING を区別する(黙ってdropしない)
- horizon一覧はconfig(schedule.evaluation_horizons_business_days・
  暦日ホライズン)を正本とし、exportメタデータへ記録する
- horizon到来判定・評価window終了日は既存RecommendationEvaluationServiceと
  同一の意味論(営業日: start=recommended_atのUTC暦日+BusinessCalendar、
  暦日: JST暦日+timedelta)を使う。独自の営業日計算を新設しない
- 出力は同一入力(+同一now)に対しバイト単位で決定的

schema versioning(CALIBRATION_DATASET_SCHEMA_VERSION):
- 列の追加のみ: minor更新は行わずversion据え置き可(後方互換)
- 既存列の意味・名前・型・serialization変更: versionを必ず上げる
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware, to_jst
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)

# 週次改善レビュー用のJST暦日ホライズン。recommendation_evaluation_service.pyの
# _CALENDAR_HORIZON_DAYS(config/review_improvement.yamlのevaluation_horizon_daysと
# 同期)を正本として参照する(独自定義しない)。
from jstock_advisor.services.recommendation_evaluation_service import _CALENDAR_HORIZON_DAYS

CALIBRATION_DATASET_SCHEMA_VERSION = "1"


class HorizonUnit(StrEnum):
    BUSINESS_DAYS = "BUSINESS_DAYS"
    CALENDAR_DAYS = "CALENDAR_DAYS"


class RowStatus(StrEnum):
    EVALUATED = "EVALUATED"
    NOT_YET_EVALUABLE = "NOT_YET_EVALUABLE"
    EVALUATION_MISSING = "EVALUATION_MISSING"


class SampleDefinition(StrEnum):
    RAW = "RAW"
    NON_OVERLAPPING_WINDOW = "NON_OVERLAPPING_WINDOW"


class SelectionReason(StrEnum):
    RAW = "RAW"
    FIRST_IN_WINDOW = "FIRST_IN_WINDOW"
    OVERLAPS_PRIOR_WINDOW = "OVERLAPS_PRIOR_WINDOW"


# orphan EvaluationResult(親Recommendation欠損)のIDをdiagnosticsへ残す上限。
# 大量IDでexport metadataを無制限に肥大化させない(countは常に全件を記録する)。
_ORPHAN_ID_SAMPLE_LIMIT = 20


@dataclass
class CalibrationRow:
    """1 Recommendation × 1 horizonのraw row。値はすべて「保存済みの事実」の
    正規化であり、再計算・再解釈をしない。"""

    # --- identity ---
    recommendation_id: str
    stock_code: str
    horizon_unit: HorizonUnit
    horizon_value: int
    recommendation_date_jst: dt.date
    recommended_at: dt.datetime
    row_status: RowStatus
    # --- horizon ---
    evaluation_due_date: dt.date  # 既存意味論で算出したhorizon到来日
    evaluation_date: dt.date | None
    evaluated_at: dt.datetime | None
    # --- signal(推奨時価格基準の事実) ---
    price_at_recommendation: Decimal
    price_return_pct: float | None
    mae_from_recommendation_price_pct: float | None  # = EvaluationResult.max_drawdown_pct
    mfe_from_recommendation_price_pct: float | None  # = EvaluationResult.max_gain_pct
    price_at_evaluation: Decimal | None
    # --- entry ---
    entry_buy_price: Decimal | None
    standard_buy_price: Decimal | None
    strong_buy_price: Decimal | None
    reached_entry_price: bool | None  # = reached_tentative_buy_price
    reached_standard_price: bool | None
    reached_strong_price: bool | None  # = reached_aggressive_buy_price
    business_days_to_reach_entry: int | None
    # 既存buy_price_based_return_pctの明確化alias(standard買値基準の仮想リターン。
    # 到達有無と無関係に算出される保存値)
    hypothetical_return_from_standard_price_pct: float | None
    # --- execution(判定時点事実) ---
    recommendation_type: str
    buy_action: str | None
    raw_buy_action: str | None
    watch_type: str | None
    stock_types: list[str]
    # --- score ---
    total_score: float | None
    company_quality_score: float | None
    purchase_attractiveness_score: float | None
    historical_valuation_score: float | None
    timing_score: float | None
    earnings_surprise_score: float | None
    earnings_trend_score: float | None
    market_score: float | None
    sector_score: float | None
    environment_score: float | None
    # --- valuation ---
    valuation_anchor: Decimal | None
    valuation_min: Decimal | None
    valuation_max: Decimal | None
    valuation_dispersion_ratio: Decimal | None
    decision_valuation_min: Decimal | None
    decision_valuation_max: Decimal | None
    required_margin_of_safety_entry: Decimal | None
    required_margin_of_safety_standard: Decimal | None
    required_margin_of_safety_strong: Decimal | None
    buy_price_reliability: str | None
    current_vs_entry_price_pct: Decimal | None
    # --- model version ---
    rule_version: str
    company_quality_score_model_version: str
    decision_snapshot_model_version: str | None
    input_facts_schema_version: str | None
    # --- outcome(legacy/current evaluation fact。再定義しない) ---
    evaluation_label: str | None
    label_evidence: str | None
    # --- benchmark(保存済み事実のみ。instrument解釈はmetadata側) ---
    benchmark_symbol: str | None
    benchmark_return_pct: float | None
    excess_return_pct: float | None
    # --- diagnostics ---
    decision_snapshot_present: bool
    duplicate_evaluation_count: int  # 1が正常。2以上は論理キー重複(異常の可視化)
    # --- sample metadata(selectorがannotate) ---
    sample_definition: SampleDefinition = SampleDefinition.RAW
    sample_selected: bool = True
    sample_group_id: str = ""
    selection_reason: SelectionReason = SelectionReason.RAW


@dataclass
class DatasetDiagnostics:
    orphan_evaluation_count: int = 0
    # 全IDではなく決定的な先頭サンプルのみ(countが正)
    orphan_evaluation_ids_sample: list[str] = field(default_factory=list)
    duplicate_evaluation_row_count: int = 0


@dataclass
class CalibrationDataset:
    metadata: dict[str, Any]
    rows: list[CalibrationRow]
    diagnostics: DatasetDiagnostics


class CalibrationDatasetBuilder:
    """read-only builder。repositoryへはlist系のみを呼ぶ(write系を一切呼ばない
    ことをテストのspyで固定している)。"""

    def __init__(
        self,
        config: AppConfig,
        business_calendar: BusinessCalendar,
        recommendation_repository: RecommendationRepository | None = None,
        evaluation_repository: EvaluationResultRepository | None = None,
        decision_snapshot_repository: DecisionSnapshotRepository | None = None,
    ) -> None:
        self._config = config
        self._calendar = business_calendar
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._evaluations = evaluation_repository or EvaluationResultRepository()
        self._snapshots = decision_snapshot_repository or DecisionSnapshotRepository()

    # --- horizon意味論(RecommendationEvaluationServiceと同一) ------------------

    def _business_horizons_for(self, recommendation_type_value: str) -> list[int]:
        horizons_cfg = self._config.schedule.evaluation_horizons_business_days
        specific = horizons_cfg.get(recommendation_type_value, [])
        common = horizons_cfg.get("all_types_common", [])
        return sorted(set(specific) | set(common))

    def _business_due_date(self, recommendation: Recommendation, horizon: int) -> dt.date:
        # 営業日評価のstartは「recommended_atのUTC暦日」(既存仕様。JST化しない)
        return self._calendar.add_business_days(recommendation.recommended_at.date(), horizon)

    @staticmethod
    def _calendar_due_date(recommendation: Recommendation, horizon_days: int) -> dt.date:
        # 暦日評価のstartは「recommended_atのJST暦日」(既存仕様)
        return to_jst(recommendation.recommended_at).date() + dt.timedelta(days=horizon_days)

    # --- build -----------------------------------------------------------------

    def build(
        self,
        now: dt.datetime,
        sample_definition: SampleDefinition = SampleDefinition.RAW,
    ) -> CalibrationDataset:
        require_timezone_aware(now)
        today_jst = evaluation_date_jst(now)

        recommendations = self._recommendations.list_all()
        evaluations = self._evaluations.list_all()
        snapshots = self._snapshots.list_all()

        known_recommendation_ids = {r.recommendation_id for r in recommendations}
        snapshot_by_recommendation = {
            s.recommendation_id: s for s in snapshots if s.recommendation_id is not None
        }

        evaluations_by_key: dict[tuple[str, HorizonUnit, int], list[EvaluationResult]] = {}
        orphan_ids: list[str] = []
        for evaluation in evaluations:
            if evaluation.recommendation_id not in known_recommendation_ids:
                orphan_ids.append(evaluation.evaluation_id)
                continue
            if evaluation.horizon_business_days is not None:
                key = (
                    evaluation.recommendation_id,
                    HorizonUnit.BUSINESS_DAYS,
                    evaluation.horizon_business_days,
                )
            else:
                key = (
                    evaluation.recommendation_id,
                    HorizonUnit.CALENDAR_DAYS,
                    evaluation.horizon_calendar_days or 0,
                )
            evaluations_by_key.setdefault(key, []).append(evaluation)

        diagnostics = DatasetDiagnostics(
            orphan_evaluation_count=len(orphan_ids),
            orphan_evaluation_ids_sample=sorted(orphan_ids)[:_ORPHAN_ID_SAMPLE_LIMIT],
        )

        rows: list[CalibrationRow] = []
        for recommendation in recommendations:
            for horizon in self._business_horizons_for(recommendation.recommendation_type.value):
                rows.append(
                    self._build_row(
                        recommendation,
                        HorizonUnit.BUSINESS_DAYS,
                        horizon,
                        self._business_due_date(recommendation, horizon),
                        today_jst,
                        evaluations_by_key,
                        snapshot_by_recommendation,
                        diagnostics,
                    )
                )
            calendar_horizon = _CALENDAR_HORIZON_DAYS
            rows.append(
                self._build_row(
                    recommendation,
                    HorizonUnit.CALENDAR_DAYS,
                    calendar_horizon,
                    self._calendar_due_date(recommendation, calendar_horizon),
                    today_jst,
                    evaluations_by_key,
                    snapshot_by_recommendation,
                    diagnostics,
                )
            )

        rows.sort(
            key=lambda r: (
                r.stock_code,
                r.recommendation_date_jst.isoformat(),
                r.recommended_at.astimezone(dt.UTC).isoformat(),
                r.recommendation_id,
                r.horizon_unit.value,
                r.horizon_value,
            )
        )

        _apply_sample_definition(rows, sample_definition, self._calendar)

        metadata = self._build_metadata(now, sample_definition, diagnostics, len(rows))
        return CalibrationDataset(metadata=metadata, rows=rows, diagnostics=diagnostics)

    def _build_row(
        self,
        recommendation: Recommendation,
        unit: HorizonUnit,
        horizon_value: int,
        due_date: dt.date,
        today_jst: dt.date,
        evaluations_by_key: dict[tuple[str, HorizonUnit, int], list[EvaluationResult]],
        snapshot_by_recommendation: dict[str, Any],
        diagnostics: DatasetDiagnostics,
    ) -> CalibrationRow:
        key = (recommendation.recommendation_id, unit, horizon_value)
        matched = evaluations_by_key.get(key, [])
        duplicate_count = len(matched)
        evaluation: EvaluationResult | None = None
        if matched:
            # 重複時の代表値は決定的ルール(evaluated_at昇順→evaluation_id辞書順)で
            # 「最初の評価結果を代表表示」する。正常化ではないことを
            # duplicate_evaluation_count(>1)で明示する。
            evaluation = sorted(
                matched,
                key=lambda e: (e.evaluated_at.astimezone(dt.UTC).isoformat(), e.evaluation_id),
            )[0]
            if duplicate_count > 1:
                diagnostics.duplicate_evaluation_row_count += 1
            row_status = RowStatus.EVALUATED
        elif due_date > today_jst:
            row_status = RowStatus.NOT_YET_EVALUABLE
        else:
            row_status = RowStatus.EVALUATION_MISSING

        facts = recommendation.buy_score_input_facts or {}
        facts_schema_version = facts.get("buy_score_input_facts_schema_version")
        snapshot = snapshot_by_recommendation.get(recommendation.recommendation_id)

        return CalibrationRow(
            recommendation_id=recommendation.recommendation_id,
            stock_code=recommendation.stock_code,
            horizon_unit=unit,
            horizon_value=horizon_value,
            recommendation_date_jst=to_jst(recommendation.recommended_at).date(),
            recommended_at=recommendation.recommended_at,
            row_status=row_status,
            evaluation_due_date=due_date,
            evaluation_date=evaluation.evaluation_date if evaluation else None,
            evaluated_at=evaluation.evaluated_at if evaluation else None,
            price_at_recommendation=recommendation.price_at_recommendation,
            price_return_pct=evaluation.price_return_pct if evaluation else None,
            mae_from_recommendation_price_pct=(
                evaluation.max_drawdown_pct if evaluation else None
            ),
            mfe_from_recommendation_price_pct=(evaluation.max_gain_pct if evaluation else None),
            price_at_evaluation=evaluation.price_at_evaluation if evaluation else None,
            entry_buy_price=recommendation.entry_buy_price,
            standard_buy_price=recommendation.standard_buy_price,
            strong_buy_price=recommendation.strong_buy_price,
            reached_entry_price=(
                evaluation.reached_tentative_buy_price if evaluation else None
            ),
            reached_standard_price=(
                evaluation.reached_standard_buy_price if evaluation else None
            ),
            reached_strong_price=(
                evaluation.reached_aggressive_buy_price if evaluation else None
            ),
            business_days_to_reach_entry=(
                evaluation.business_days_to_reach_price if evaluation else None
            ),
            hypothetical_return_from_standard_price_pct=(
                evaluation.buy_price_based_return_pct if evaluation else None
            ),
            recommendation_type=recommendation.recommendation_type.value,
            buy_action=recommendation.buy_action.value if recommendation.buy_action else None,
            raw_buy_action=(
                recommendation.raw_buy_action.value if recommendation.raw_buy_action else None
            ),
            watch_type=recommendation.watch_type.value if recommendation.watch_type else None,
            stock_types=[t.value for t in recommendation.stock_types],
            total_score=recommendation.total_score,
            company_quality_score=recommendation.company_quality_score,
            purchase_attractiveness_score=recommendation.purchase_attractiveness_score,
            historical_valuation_score=recommendation.historical_valuation_score,
            timing_score=recommendation.timing_score,
            earnings_surprise_score=recommendation.earnings_surprise_score,
            earnings_trend_score=recommendation.earnings_trend_score,
            market_score=recommendation.market_score,
            sector_score=recommendation.sector_score,
            environment_score=recommendation.environment_score,
            valuation_anchor=recommendation.valuation_anchor,
            valuation_min=recommendation.valuation_min,
            valuation_max=recommendation.valuation_max,
            valuation_dispersion_ratio=recommendation.valuation_dispersion_ratio,
            decision_valuation_min=recommendation.decision_valuation_min,
            decision_valuation_max=recommendation.decision_valuation_max,
            required_margin_of_safety_entry=recommendation.required_margin_of_safety_entry,
            required_margin_of_safety_standard=recommendation.required_margin_of_safety_standard,
            required_margin_of_safety_strong=recommendation.required_margin_of_safety_strong,
            buy_price_reliability=(
                recommendation.buy_price_reliability.value
                if recommendation.buy_price_reliability
                else None
            ),
            current_vs_entry_price_pct=recommendation.current_vs_entry_price_pct,
            rule_version=recommendation.rule_version,
            company_quality_score_model_version=(
                recommendation.company_quality_score_model_version
            ),
            decision_snapshot_model_version=(snapshot.model_version if snapshot else None),
            input_facts_schema_version=(
                str(facts_schema_version) if facts_schema_version is not None else None
            ),
            evaluation_label=evaluation.evaluation_label.value if evaluation else None,
            label_evidence=evaluation.label_evidence if evaluation else None,
            benchmark_symbol=evaluation.benchmark_symbol if evaluation else None,
            benchmark_return_pct=evaluation.benchmark_return_pct if evaluation else None,
            excess_return_pct=evaluation.excess_return_pct if evaluation else None,
            decision_snapshot_present=snapshot is not None,
            duplicate_evaluation_count=duplicate_count,
        )

    def _build_metadata(
        self,
        now: dt.datetime,
        sample_definition: SampleDefinition,
        diagnostics: DatasetDiagnostics,
        row_count: int,
    ) -> dict[str, Any]:
        horizons_cfg = self._config.schedule.evaluation_horizons_business_days
        # 保存済み事実(benchmark_symbol="TOPIX")と、export時点の現在コードによる
        # 解釈(TOPIX→どのinstrumentか)を混同しない。過去のEvaluationResultが
        # このinstrumentで算出されたことは保存データからは証明できないため、
        # rowではなくmetadataにのみ「現在コードの解釈」として記録する。
        from jstock_advisor.providers.market_data.yfinance_impl import _BENCHMARK_TICKERS

        return {
            "record_type": "metadata",
            "calibration_dataset_schema_version": CALIBRATION_DATASET_SCHEMA_VERSION,
            "as_of": now.astimezone(dt.UTC).isoformat(),
            "sample_definition": sample_definition.value,
            "row_count": row_count,
            "return_basis": "PRICE_ONLY",
            "return_basis_note": (
                "配当・株主優待・手数料・税金を含まない株価のみのリターン"
            ),
            "horizon_definition": {
                "BUSINESS_DAYS": {k: sorted(v) for k, v in sorted(horizons_cfg.items())},
                "CALENDAR_DAYS": [_CALENDAR_HORIZON_DAYS],
            },
            "benchmark_mapping_source": (
                "providers/market_data/yfinance_impl._BENCHMARK_TICKERS"
            ),
            "benchmark_mapping_interpreted_at_export": now.astimezone(dt.UTC).isoformat(),
            "benchmark_mapping_current_instrument": dict(sorted(_BENCHMARK_TICKERS.items())),
            "orphan_evaluation_count": diagnostics.orphan_evaluation_count,
            "orphan_evaluation_ids_sample": diagnostics.orphan_evaluation_ids_sample,
            "duplicate_evaluation_row_count": diagnostics.duplicate_evaluation_row_count,
        }


# --- sample selectors ---------------------------------------------------------


def _apply_sample_definition(
    rows: list[CalibrationRow],
    sample_definition: SampleDefinition,
    calendar: BusinessCalendar,
) -> None:
    del calendar  # NON_OVERLAPPING_WINDOWはevaluation_due_date(既算出)を使うため未使用
    if sample_definition == SampleDefinition.RAW:
        for row in rows:
            row.sample_definition = SampleDefinition.RAW
            row.sample_selected = True
            row.selection_reason = SelectionReason.RAW
            row.sample_group_id = (
                f"{row.recommendation_id}|{row.horizon_unit.value}|{row.horizon_value}"
            )
        return
    if sample_definition == SampleDefinition.NON_OVERLAPPING_WINDOW:
        _apply_non_overlapping_window(rows)
        return
    raise ValueError(f"unknown sample definition: {sample_definition}")


def _apply_non_overlapping_window(rows: list[CalibrationRow]) -> None:
    """銘柄×horizon(unit+value)単位で独立に、評価windowが重ならない最初の
    Recommendationのみをsample_selected=trueにする(行は削除しない)。

    window終了日には既存評価意味論で算出済みのevaluation_due_dateを使う
    (営業日horizonはBusinessCalendar由来、暦日horizonはJST暦日+timedelta。
    off-by-oneを避けるため独自計算しない)。windowの起点比較は
    「次の行の評価起点日 > 直前選択行のwindow終了日」で判定し、起点日は
    既存仕様と同じbasis(営業日: recommended_atのUTC暦日 / 暦日: JST暦日)。
    """
    groups: dict[tuple[str, HorizonUnit, int], list[CalibrationRow]] = {}
    for row in rows:
        groups.setdefault((row.stock_code, row.horizon_unit, row.horizon_value), []).append(row)

    for group_rows in groups.values():
        group_rows.sort(
            key=lambda r: (
                r.recommendation_date_jst.isoformat(),
                r.recommended_at.astimezone(dt.UTC).isoformat(),
                r.recommendation_id,
            )
        )
        window_end: dt.date | None = None
        current_group_id = ""
        for row in group_rows:
            start = (
                row.recommended_at.date()
                if row.horizon_unit == HorizonUnit.BUSINESS_DAYS
                else row.recommendation_date_jst
            )
            row.sample_definition = SampleDefinition.NON_OVERLAPPING_WINDOW
            if window_end is None or start > window_end:
                window_end = row.evaluation_due_date
                current_group_id = (
                    f"{row.stock_code}|{row.horizon_unit.value}|{row.horizon_value}"
                    f"|{start.isoformat()}"
                )
                row.sample_selected = True
                row.selection_reason = SelectionReason.FIRST_IN_WINDOW
            else:
                row.sample_selected = False
                row.selection_reason = SelectionReason.OVERLAPS_PRIOR_WINDOW
            row.sample_group_id = current_group_id


# --- serialization ------------------------------------------------------------

# CSV列順の正本(JSONLのキー順はsort_keysで決定的なため独立)。
CSV_COLUMNS: tuple[str, ...] = (
    # identity
    "recommendation_id",
    "stock_code",
    "horizon_unit",
    "horizon_value",
    "recommendation_date_jst",
    "recommended_at",
    "row_status",
    # horizon
    "evaluation_due_date",
    "evaluation_date",
    "evaluated_at",
    # signal
    "price_at_recommendation",
    "price_return_pct",
    "mae_from_recommendation_price_pct",
    "mfe_from_recommendation_price_pct",
    "price_at_evaluation",
    # entry
    "entry_buy_price",
    "standard_buy_price",
    "strong_buy_price",
    "reached_entry_price",
    "reached_standard_price",
    "reached_strong_price",
    "business_days_to_reach_entry",
    "hypothetical_return_from_standard_price_pct",
    # execution
    "recommendation_type",
    "buy_action",
    "raw_buy_action",
    "watch_type",
    "stock_types",
    # score
    "total_score",
    "company_quality_score",
    "purchase_attractiveness_score",
    "historical_valuation_score",
    "timing_score",
    "earnings_surprise_score",
    "earnings_trend_score",
    "market_score",
    "sector_score",
    "environment_score",
    # valuation
    "valuation_anchor",
    "valuation_min",
    "valuation_max",
    "valuation_dispersion_ratio",
    "decision_valuation_min",
    "decision_valuation_max",
    "required_margin_of_safety_entry",
    "required_margin_of_safety_standard",
    "required_margin_of_safety_strong",
    "buy_price_reliability",
    "current_vs_entry_price_pct",
    # model version
    "rule_version",
    "company_quality_score_model_version",
    "decision_snapshot_model_version",
    "input_facts_schema_version",
    # outcome
    "evaluation_label",
    "label_evidence",
    # benchmark
    "benchmark_symbol",
    "benchmark_return_pct",
    "excess_return_pct",
    # diagnostics
    "decision_snapshot_present",
    "duplicate_evaluation_count",
    # sample metadata
    "sample_definition",
    "sample_selected",
    "sample_group_id",
    "selection_reason",
)


def _serialize_value(value: Any) -> Any:
    """JSONL用の型正規化。Decimalは丸めず固定小数文字列、日時はUTC ISO8601、
    dateはISO、enumはvalue、listは各要素を再帰変換。float/bool/int/str/Noneは
    JSONの型で無損失表現できるためそのまま。"""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    return value


def row_to_record(row: CalibrationRow) -> dict[str, Any]:
    record: dict[str, Any] = {"record_type": "row"}
    for column in CSV_COLUMNS:
        record[column] = _serialize_value(getattr(row, column))
    return record


def to_jsonl(dataset: CalibrationDataset, *, selected_only: bool, include_pending: bool) -> str:
    """canonical export。1行目がrecord_type=metadata、以降がrecord_type=row。
    キー順はsort_keysで固定し、同一入力(+同一now)でバイト単位に一致する。"""
    lines = [json.dumps(dataset.metadata, ensure_ascii=False, sort_keys=True)]
    rows = _filtered_rows(dataset, selected_only=selected_only, include_pending=include_pending)
    for row in rows:
        lines.append(json.dumps(row_to_record(row), ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + "\n"


def to_csv(
    dataset: CalibrationDataset, *, selected_only: bool, include_pending: bool
) -> tuple[str, str]:
    """human convenience export。戻り値は(csv本文, sidecar .meta.json本文)。
    None→空文字 / bool→true・false / list→";"連結(要素にセミコロンを含まない
    enum値のみのため安全)。"""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    rows = _filtered_rows(dataset, selected_only=selected_only, include_pending=include_pending)
    for row in rows:
        record = row_to_record(row)
        writer.writerow([_csv_cell(record[column]) for column in CSV_COLUMNS])
    meta_json = json.dumps(dataset.metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return buffer.getvalue(), meta_json


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, list):
        return ";".join(str(v) for v in value)
    return str(value)


def _filtered_rows(
    dataset: CalibrationDataset, *, selected_only: bool, include_pending: bool
) -> list[CalibrationRow]:
    rows = dataset.rows
    if selected_only:
        rows = [r for r in rows if r.sample_selected]
    if not include_pending:
        rows = [r for r in rows if r.row_status != RowStatus.NOT_YET_EVALUABLE]
    return rows


def write_export(
    dataset: CalibrationDataset,
    output_path: Path,
    *,
    export_format: str,
    selected_only: bool,
    include_pending: bool,
) -> list[Path]:
    """exportをファイルへ書き出し、作成したファイルパス一覧を返す。"""
    if export_format == "jsonl":
        output_path.write_text(
            to_jsonl(dataset, selected_only=selected_only, include_pending=include_pending),
            encoding="utf-8",
            newline="\n",
        )
        return [output_path]
    if export_format == "csv":
        csv_text, meta_json = to_csv(
            dataset, selected_only=selected_only, include_pending=include_pending
        )
        meta_path = output_path.with_suffix(output_path.suffix + ".meta.json")
        output_path.write_text(csv_text, encoding="utf-8", newline="\n")
        meta_path.write_text(meta_json, encoding="utf-8", newline="\n")
        return [output_path, meta_path]
    raise ValueError(f"unknown export format: {export_format}")
