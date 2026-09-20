"""Issue 運用の label 欠落・status 滞留を検出する read-only の script(Issue #487。#220 Phase C-1)。

Issue の一覧(JSON)を入力に取り、次を検出して一覧にする。**ネットワークを使わない。何も書き換えない。**
収集(GitHub API)は別の Issue(scheduled workflow。#489)が担う。

    gh issue list --state open --limit 500 --json number,labels,createdAt,updatedAt > issues.json
    python scripts/audit_issue_labels.py --input issues.json --now 2026-09-20T00:00:00Z \\
        --deployed-days 7 --merged-days 7

## 検出するもの(機械で判定できるものだけ。docs/issue_label_policy.md §7.3.9 と整合)

    MISSING_TYPE        Type label(bug / enhancement 等)が無い
    MISSING_PRIORITY    priority:P0〜P3 が無い
    MISSING_STATUS      status:* が無い
    MULTIPLE_STATUS     status:* が 2 個以上(§7.2 = OPEN Issue で 1 個)
    UNKNOWN_STATUS      status:* が §7.1 の 8 つの語彙に無い
    STALE_DEPLOYED      status:デプロイ済 が閾値の日数を超えて続いている
    STALE_MERGED        status:マージ済 が閾値の日数を超えて続いている

## 検出しないもの(semantic 判断)

**残作業単位の数・split の要否・Priority の妥当性・tracking の本文の記載の有無は判断しない。**
tracking の Priority 欠落は「欠落」として出し、「§12 の N/A の可能性」と注記するに留める
(§12 は「本文へ確定判断として記載する」ことを条件とするが、その記載の有無は機械では判定できない)。

## 滞留の日数

`status_since`(その status label を付けた時刻。収集側が供給する)があればそれを、無ければ `updatedAt`
を起点にする。**updatedAt はコメント 1 つで更新されるため、滞留を過小に見積もる**(#220 の #36 の型)。
起点をどれにするか・閾値を何日にするかは、別の Issue(#488)の決定である。本 script は引数で受け、
**既定値を置かない**(閾値を指定しなければ、滞留は検出せず、日数だけを参考として出す)。
現在時刻は引数 `--now` で受け、内部で現在時刻を取得しない(結果を決定的にする)。

## 入力が壊れているとき

**「検査できなかった」を「検出 0 件」へ倒さない。** 入力が読めない・空・想定の形でない場合は exit 2。

## exit code

    0  検査できた(検出の有無は出力で示す。既定では検出があっても 0)
    1  検査できて、検出があった(`--fail-on-findings` を指定したときだけ)
    2  検査できなかった(入力が読めない・空・形が想定と違う)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TYPE_LABELS = frozenset(
    {
        "bug",
        "design-defect",
        "enhancement",
        "investigation",
        "calibration",
        "tracking",
        "not-a-bug",
        "accepted-risk",
    }
)
PRIORITY_LABELS = frozenset({"priority:P0", "priority:P1", "priority:P2", "priority:P3"})
STATUS_LABELS = frozenset(
    {
        "status:未着手",
        "status:調査・設計中",
        "status:設計済",
        "status:開発中",
        "status:開発済",
        "status:マージ済",
        "status:デプロイ済",
        "status:本番検証済",
    }
)
STATUS_DEPLOYED = "status:デプロイ済"
STATUS_MERGED = "status:マージ済"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNCHECKABLE = 2


class InputError(ValueError):
    """入力が想定の形でない(検査できなかった)。"""


@dataclass(frozen=True)
class Finding:
    number: int
    condition: str
    detail: str

    def render(self) -> str:
        return f"#{self.number} {self.condition} {self.detail}"


@dataclass(frozen=True)
class DwellObservation:
    number: int
    status: str
    days: int
    basis: str


@dataclass
class Result:
    checked: int
    findings: list[Finding]
    dwell: list[DwellObservation]


def _parse_time(value: object, *, where: str) -> dt.datetime:
    if not isinstance(value, str):
        raise InputError(f"{where}: 日時が文字列でない")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{where}: 日時を解釈できない: {value!r}") from exc
    if parsed.tzinfo is None:
        raise InputError(f"{where}: タイムゾーンが無い日時: {value!r}")
    return parsed.astimezone(dt.UTC)


def _label_names(issue: dict[str, Any], number: int) -> set[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        raise InputError(f"#{number}: labels が配列でない")
    names: set[str] = set()
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise InputError(f"#{number}: label の形が想定と違う")
        names.add(label["name"])
    return names


def _dwell_since(issue: dict[str, Any], number: int) -> tuple[dt.datetime, str]:
    if issue.get("status_since") is not None:
        return _parse_time(issue["status_since"], where=f"#{number} status_since"), "status_since"
    if issue.get("updatedAt") is None:
        raise InputError(f"#{number}: status_since も updatedAt も無い(滞留の起点を決められない)")
    return _parse_time(issue["updatedAt"], where=f"#{number} updatedAt"), "updatedAt"


def audit(
    issues: object,
    *,
    now: dt.datetime,
    deployed_days: int | None = None,
    merged_days: int | None = None,
) -> Result:
    """Issue の一覧を検査する。入力が想定の形でなければ InputError(検査できなかった)。"""
    if not isinstance(issues, list) or not issues:
        raise InputError("入力が空、または配列でない")
    findings: list[Finding] = []
    dwell: list[DwellObservation] = []
    checked = 0
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            raise InputError("Issue の形が想定と違う(number が無い)")
        number = issue["number"]
        if str(issue.get("state", "OPEN")).upper() != "OPEN":
            continue
        checked += 1
        names = _label_names(issue, number)
        if not names & TYPE_LABELS:
            findings.append(Finding(number, "MISSING_TYPE", "Type label が無い"))
        if not names & PRIORITY_LABELS:
            note = ""
            if "tracking" in names:
                note = "(tracking。§12 の N/A の可能性。本文の確定判断の記載の有無は判断しない)"
            findings.append(Finding(number, "MISSING_PRIORITY", "priority label が無い" + note))
        statuses = sorted(n for n in names if n.startswith("status:"))
        if not statuses:
            findings.append(Finding(number, "MISSING_STATUS", "status label が無い"))
        if len(statuses) > 1:
            findings.append(Finding(number, "MULTIPLE_STATUS", "status label が 2 個以上: " + " ".join(statuses)))
        for s in statuses:
            if s not in STATUS_LABELS:
                findings.append(Finding(number, "UNKNOWN_STATUS", f"語彙に無い status label: {s}"))
        for status, threshold, condition in (
            (STATUS_DEPLOYED, deployed_days, "STALE_DEPLOYED"),
            (STATUS_MERGED, merged_days, "STALE_MERGED"),
        ):
            if status not in names:
                continue
            since, basis = _dwell_since(issue, number)
            days = (now - since).days
            dwell.append(DwellObservation(number, status, days, basis))
            if threshold is not None and days >= threshold:
                findings.append(
                    Finding(number, condition, f"{status} が {days} 日続いている(閾値 {threshold} 日。起点 = {basis})")
                )
    return Result(checked=checked, findings=findings, dwell=dwell)


def render(result: Result, *, deployed_days: int | None, merged_days: int | None) -> str:
    lines = [
        f"CHECKED_OPEN_ISSUES = {result.checked}",
        f"FINDINGS            = {len(result.findings)}",
        f"THRESHOLDS          = deployed={deployed_days} merged={merged_days}(None = 滞留は検出せず、日数だけを出す)",
    ]
    lines.extend(f"  {f.render()}" for f in result.findings)
    if result.dwell:
        lines.append("DWELL(参考。status label が付いている Issue の日数。長い順)")
        for d in sorted(result.dwell, key=lambda x: (-x.days, x.number)):
            lines.append(f"  #{d.number} {d.status} {d.days} 日(起点 = {d.basis})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="Issue の一覧(JSON)")
    parser.add_argument("--now", default=None, help="現在時刻(ISO 8601、タイムゾーン付き)。省略時は実行時の UTC")
    parser.add_argument("--deployed-days", type=int, default=None)
    parser.add_argument("--merged-days", type=int, default=None)
    parser.add_argument("--fail-on-findings", action="store_true")
    args = parser.parse_args(argv)

    try:
        raw = Path(args.input).read_text(encoding="utf-8")
        issues = json.loads(raw)
        now = _parse_time(args.now, where="--now") if args.now else dt.datetime.now(dt.UTC)
        result = audit(
            issues, now=now, deployed_days=args.deployed_days, merged_days=args.merged_days
        )
    except (OSError, json.JSONDecodeError, InputError) as exc:
        print(f"検査できなかった: {exc}", file=sys.stderr)
        return EXIT_UNCHECKABLE

    print(render(result, deployed_days=args.deployed_days, merged_days=args.merged_days))
    if result.findings and args.fail_on_findings:
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
