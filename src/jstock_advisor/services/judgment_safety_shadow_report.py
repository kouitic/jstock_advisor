"""shadow監査記録の集計(Issue #458 / #160 PR-4)。**純関数のみ。I/Oも書き込みも持たない。**

「安全条件(G1〜G4)を適用していたら何件がどうsuppressされたか」を、Phase 2(誤検出・過剰抑制の
レビュー)の材料として集計する。あわせて、`AuditLogTable`の増加量・scanメトリクスを、baselineと
比較できる形で返す(U13 = OPTION_C: 実測してから専用Tableの要否を判断する)。

**本moduleは閾値の判定・専用Tableへの移行の提案をしない**(判断はUSER/MANAGER)。
`not_evaluated`(入力が無く評価できなかった条件)は「該当なし」ではなく、件数へ含めず別掲する。
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.domain.jst import to_jst

SUPPORTED_SCHEMA_VERSION: Final = 1
CONDITION_IDS: Final[tuple[str, ...]] = ("G1", "G2", "G3", "G4")

#: 概算コストの単価(USD / 100万 読み取りユニット)。**公開単価は変更されうる概算値**。
READ_UNIT_PRICE_USD_PER_MILLION: Final = 0.285
READ_UNIT_PRICE_NOTE: Final = (
    "ap-northeast-1のオンデマンド読み取り単価(2026-09-21時点の想定値。要確認)"
)

#: 記録が3日未満のときの月次外挿は参考値。
MIN_DAYS_FOR_EXTRAPOLATION: Final = 3
DAYS_PER_MONTH: Final = 30

NOTES: Final[tuple[str, ...]] = (
    "G3は測定可能な2項目の下限値である(測定不能の3項目は含まない)。",
    "G4は保有のFULL_PROFIT_TAKEのみが対象である(買い経路は対象外)。",
    "not_evaluatedは「該当なし」ではない(入力が無く評価できなかった)。件数へ含めず別掲する。",
    "DescribeTableの値は概算(およそ6時間ごとに更新)。実測値はScanの結果である。",
    "概算コストは公開単価に基づく概算である。",
    "本ツールは閾値の判定・専用Tableへの移行の提案をしない(判断はUSER/MANAGER。U13)。",
)
NO_RECORDS_MESSAGE: Final = (
    "shadow記録がありません(SHADOW有効化の前、または期間外)。0件は「問題なし」を意味しません。"
)


@dataclass(frozen=True)
class Window:
    """JST暦日での期間(両端を含む)。Noneは無制限。"""

    date_from: dt.date | None = None
    date_to: dt.date | None = None

    def contains(self, day: dt.date) -> bool:
        if self.date_from is not None and day < self.date_from:
            return False
        return not (self.date_to is not None and day > self.date_to)


@dataclass(frozen=True)
class Baseline:
    records: int
    size_bytes: int
    date: dt.date


@dataclass(frozen=True)
class TableSnapshot:
    """DescribeTableの値(概算)。"""

    item_count: int
    table_size_bytes: int


@dataclass(frozen=True)
class ScanSnapshot:
    """Scanの実測値。"""

    read_pages: int
    scanned_count: int
    count: int
    total_rru: float
    elapsed_time_sec: float
    item_bytes: int
    shadow_item_bytes: int
    decision_type_counts: dict[str, int]
    all_records_by_jst_date: dict[str, int]


def jst_date_of(entry: AuditLogEntry) -> dt.date:
    return to_jst(entry.timestamp).date()


def _schema_version(entry: AuditLogEntry) -> object:
    return entry.input_values.get("schema_version")


def _findings(entry: AuditLogEntry) -> list[dict[str, Any]]:
    raw = entry.output_values.get("findings")
    return [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []


def _not_evaluated(entry: AuditLogEntry) -> list[str]:
    raw = entry.output_values.get("not_evaluated")
    return [str(c) for c in raw] if isinstance(raw, list) else []


def _judgment_label(entry: AuditLogEntry) -> str:
    """買いは`buy_action`、保有の利確は`recommendation_type`を判定の区分とする。"""
    engine = entry.input_values.get("engine")
    if engine == "BUY_CANDIDATES":
        return str(entry.input_values.get("buy_action"))
    return str(entry.input_values.get("recommendation_type"))


def _unmeasurable_g3_inputs(entries: list[AuditLogEntry]) -> list[str]:
    """測定不能のG3入力は、記録自体が保持する値(`output_values`)の和集合を出す。

    判断の安全条件のmodule(`judgment_safety.py`)を参照しない(その参照元は、事実の供給側と
    shadowの記録側だけに限る契約のため。集計は読み取り側であり、評価の定義へ依存しない)。
    記録が0件のときは空になる(0件は「問題なし」ではない旨を別に表示する)。
    """
    names: set[str] = set()
    for entry in entries:
        raw = entry.output_values.get("unmeasurable_g3_inputs")
        if isinstance(raw, list):
            names.update(str(n) for n in raw)
    return sorted(names)


def build_shadow_result_metrics(
    entries: Iterable[AuditLogEntry], window: Window, *, unparsed: int
) -> dict[str, Any]:
    """shadow記録の集計(SHADOW_RESULT_METRICS)。"""
    in_window = [e for e in entries if window.contains(jst_date_of(e))]
    supported: list[AuditLogEntry] = []
    # 未知のschema_versionは`unparsed`ではなく別掲する(集計の前提が違うため、混ぜない)。
    unknown_schema: Counter[str] = Counter()
    for entry in in_window:
        if _schema_version(entry) == SUPPORTED_SCHEMA_VERSION:
            supported.append(entry)
        else:
            unknown_schema[str(_schema_version(entry))] += 1

    per_day: Counter[str] = Counter(jst_date_of(e).isoformat() for e in supported)
    by_engine: Counter[str] = Counter(str(e.input_values.get("engine")) for e in supported)
    reason_codes: Counter[str] = Counter()
    not_evaluated_counts: Counter[str] = Counter()
    findings_by_condition: Counter[str] = Counter()
    records_with_finding: Counter[str] = Counter()
    evaluated_records: Counter[str] = Counter()
    judgment: dict[str, dict[str, int]] = {}
    combinations: Counter[str] = Counter()
    suppressible_records = 0
    suppressible_findings = 0
    overlap_records = 0

    for entry in supported:
        findings = _findings(entry)
        not_evaluated = set(_not_evaluated(entry))
        conditions = sorted({str(f.get("condition_id")) for f in findings})
        for condition in CONDITION_IDS:
            if condition not in not_evaluated:
                evaluated_records[condition] += 1
        for condition in not_evaluated:
            not_evaluated_counts[condition] += 1
        for finding in findings:
            findings_by_condition[str(finding.get("condition_id"))] += 1
            reason_codes[str(finding.get("reason_code"))] += 1
        for condition in conditions:
            records_with_finding[condition] += 1
        if len(conditions) >= 2:
            overlap_records += 1
            combinations["+".join(conditions)] += 1
        suppressing = [f for f in findings if f.get("would_suppress") is True]
        if suppressing:
            suppressible_records += 1
            suppressible_findings += len(suppressing)
        label = _judgment_label(entry)
        bucket = judgment.setdefault(label, {"records": 0, "with_finding": 0})
        bucket["records"] += 1
        if findings:
            bucket["with_finding"] += 1

    condition_counts: dict[str, dict[str, Any]] = {}
    for condition in CONDITION_IDS:
        evaluated = evaluated_records[condition]
        with_finding = records_with_finding[condition]
        condition_counts[condition] = {
            "findings": findings_by_condition[condition],
            "records_with_finding": with_finding,
            "evaluated_records": evaluated,
            # 分母はnot_evaluatedを除いた評価済みの記録数(0のときはNone。0割りを0%と読ませない)。
            "rate": (with_finding / evaluated) if evaluated else None,
        }

    dates = sorted(per_day)
    return {
        "measurement_window": {
            "from": window.date_from.isoformat() if window.date_from else None,
            "to": window.date_to.isoformat() if window.date_to else None,
            "jst_dates_with_records": [dates[0], dates[-1]] if dates else [],
        },
        "shadow_record_count": len(supported),
        "records_per_day": dict(sorted(per_day.items())),
        "by_engine": dict(sorted(by_engine.items())),
        "condition_counts": condition_counts,
        "reason_code_counts": dict(sorted(reason_codes.items())),
        "recommendation_type_counts": dict(sorted(judgment.items())),
        "not_evaluated_counts": {c: not_evaluated_counts[c] for c in CONDITION_IDS},
        "would_suppress_counts": {
            "records_with_any_suppressible_finding": suppressible_records,
            "findings_total": suppressible_findings,
        },
        "overlap": {
            "records_with_2plus_conditions": overlap_records,
            "by_combination": dict(sorted(combinations.items())),
        },
        "unmeasurable_g3_inputs": _unmeasurable_g3_inputs(supported),
        "unparsed": unparsed,
        "unknown_schema_versions": dict(sorted(unknown_schema.items())),
        "notes": list(NOTES),
        **({"message": NO_RECORDS_MESSAGE} if not supported else {}),
    }


def _table_block(table: TableSnapshot | None) -> dict[str, Any] | None:
    if table is None:
        return None
    return {
        "item_count": table.item_count,
        "table_size_bytes": table.table_size_bytes,
        "source": "describe_table(概算。約6時間ごとに更新)",
    }


def build_vs_baseline(table: TableSnapshot, baseline: Baseline, today: dt.date) -> dict[str, Any]:
    days = (today - baseline.date).days
    records_delta = table.item_count - baseline.records
    size_delta = table.table_size_bytes - baseline.size_bytes
    block: dict[str, Any] = {
        "records_delta": records_delta,
        "records_ratio": (table.item_count / baseline.records) if baseline.records else None,
        "size_delta_bytes": size_delta,
        "size_ratio": (table.table_size_bytes / baseline.size_bytes)
        if baseline.size_bytes
        else None,
        "days_elapsed": days,
        "records_per_day_since_baseline": (records_delta / days) if days >= 1 else None,
        "note": (
            "経過日数が1日未満のため、日あたり増加量は算出しない"
            if days < 1
            else (
                "経過日数が短い(3日未満)ため、日あたり増加量は参考値"
                if days < MIN_DAYS_FOR_EXTRAPOLATION
                else "DescribeTableは概算のため、差は目安である"
            )
        ),
    }
    return block


def build_monthly_extrapolation(
    per_day: dict[str, int], shadow_item_bytes: int, shadow_matched: int
) -> dict[str, Any]:
    """直近の日別件数から、30日あたりのshadow記録の増加を外挿する(参考値)。"""
    if not per_day or shadow_matched == 0:
        return {"records": None, "bytes_estimate": None, "note": "shadow記録が無いため外挿できない"}
    average = sum(per_day.values()) / len(per_day)
    records = average * DAYS_PER_MONTH
    average_bytes = shadow_item_bytes / shadow_matched
    return {
        "records": round(records),
        "bytes_estimate": round(records * average_bytes),
        "days_used": len(per_day),
        "note": (
            "記録がある日が3日未満のため参考値"
            if len(per_day) < MIN_DAYS_FOR_EXTRAPOLATION
            else "記録がある日の平均から30日分を外挿(実測ではない)"
        ),
    }


def build_storage_read_metrics(
    scan: ScanSnapshot | None,
    table: TableSnapshot | None,
    baseline: Baseline,
    today: dt.date,
    *,
    shadow_matched: int,
    shadow_records_per_day: dict[str, int],
) -> dict[str, Any]:
    """表の増加量・scanメトリクス(STORAGE_READ_METRICS)。`scan`がNoneなら`--describe-only`。"""
    result: dict[str, Any] = {
        "table_metrics": _table_block(table),
        "baseline": {
            "records": baseline.records,
            "size_bytes": baseline.size_bytes,
            "date": baseline.date.isoformat(),
            "estimated_full_scan_cost_usd_lt": 0.01,
        },
        "vs_baseline": build_vs_baseline(table, baseline, today) if table is not None else None,
    }
    if scan is None:
        return result
    cost = scan.total_rru * READ_UNIT_PRICE_USD_PER_MILLION / 1_000_000
    result.update(
        {
            "audit_total_examined": scan.scanned_count,
            "shadow_matched": shadow_matched,
            "match_ratio": (shadow_matched / scan.scanned_count) if scan.scanned_count else None,
            "elapsed_time_sec": round(scan.elapsed_time_sec, 3),
            "read_pages": scan.read_pages,
            "consumed_capacity": {"total_rru": scan.total_rru},
            "estimated_read_cost_usd": cost,
            "estimated_read_cost_basis": READ_UNIT_PRICE_NOTE,
            "scanned_item_bytes": scan.item_bytes,
            "decision_type_counts": dict(
                sorted(scan.decision_type_counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            "records_per_day_all": dict(sorted(scan.all_records_by_jst_date.items())),
            "estimated_monthly_shadow_growth": build_monthly_extrapolation(
                shadow_records_per_day, scan.shadow_item_bytes, shadow_matched
            ),
        }
    )
    return result
