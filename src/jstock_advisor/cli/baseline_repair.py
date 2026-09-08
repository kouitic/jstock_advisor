"""投資仮説 baseline の pointer 不整合を検出・修復する運用 CLI(Issue #272)。

## なぜ要るか

`InvestmentThesisService.get_active_baseline()` は次の 2 つを integrity_error として
返し、`HoldingDecisionService` はその時点で判定を打ち切る。

    (A) pointer が無く、baseline の履歴だけがある
    (B) pointer はあるが、指す baseline が見つからない / version が食い違う

いずれも **その保有の判定が永久に停止する**。通常経路で pointer を作るのは
`activate_baseline()` だけで、それを呼ぶ `holding_decision_service.py:267` は
integrity_error の early return より後にあるため、**自力では復旧しない**。
毎日の BATCH_SUMMARY に failed として現れ続けるが、直す手段が無かった。

本 CLI がその手段である。

## 設計上の約束(Issue #272)

- **自動修復はしない。** バッチ内で pointer を作り直すことはしない。どの baseline を
  active にするかは判定基準そのものであり、黙って入れ替わってはならない。
- **既定は dry-run。** `scan` は読み取りのみ。`apply` は明示的に呼ばなければ動かない。
- **(B) は `--baseline-id` を必須にする。** pointer が指す version が違う場合、
  どちらが正しいかは人にしか決められない。自動で最新を選ばない。
- **PII を出さない。** holding_id は `<所有者>#<銘柄コード>` であり実在人物を含む。
  出力は `log_ref()`(sha256 の先頭 8 文字)に揃える(Issue #135)。
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from pathlib import Path

import typer

from jstock_advisor.domain.entities.holding_decision import InvestmentThesisBaseline
from jstock_advisor.domain.entities.owner import log_ref
from jstock_advisor.infrastructure.aws.baseline_pointer import (
    BaselinePointerConflictError,
    create_pointer,
    get_pointer,
    update_pointer,
)
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.investment_thesis_baseline_repository import (
    InvestmentThesisBaselineRepository,
)

app = typer.Typer(help="投資仮説 baseline の pointer 不整合の検出と修復 (Issue #272)")

_UPDATED_BY = "baseline-repair"


class RepairReason(StrEnum):
    """integrity_error の区分。修復の方法がそれぞれ違う。"""

    #: (A) pointer が無く履歴だけがある -> create_pointer で作る
    POINTER_MISSING = "POINTER_MISSING"
    #: (B) pointer が指す baseline が存在しない -> update_pointer で張り替える
    BASELINE_NOT_FOUND = "BASELINE_NOT_FOUND"
    #: (B) pointer と baseline の version が食い違う -> update_pointer で張り替える
    VERSION_MISMATCH = "VERSION_MISMATCH"


@dataclasses.dataclass(frozen=True)
class RepairTarget:
    """修復対象 1 件。★ holding_id の生値は保持するが、表示には使わない。"""

    holding_id: str
    reason: RepairReason
    history: tuple[InvestmentThesisBaseline, ...]
    #: (B) のときのみ非 None。楽観ロックに使う現在の pointer version。
    current_pointer_version: int | None
    #: (B) のときのみ非 None。pointer が現在指している baseline の version。
    pointed_version: int | None

    @property
    def requires_explicit_baseline(self) -> bool:
        """(B) は自動で選ばない。人が --baseline-id で指定する。"""
        return self.reason is not RepairReason.POINTER_MISSING

    @property
    def default_candidate(self) -> InvestmentThesisBaseline | None:
        """(A) の既定候補 = 履歴の最新 version。

        ★ (B) では使わない。pointer が意図的に古い baseline を指している場合が
          あり得るため、version 不一致を「最新で上書き」してはならない。
        """
        if not self.history:
            return None
        return max(self.history, key=lambda b: b.version)


def _detect(
    holdings: HoldingRepository,
    baselines: InvestmentThesisBaselineRepository,
    store_dir: Path | None,
) -> list[RepairTarget]:
    """integrity_error になる保有を列挙する。★ 書き込みは一切しない。

    判定は `get_active_baseline()` と同じ順序で行う(同じ入力から同じ結論を出す)。
    """
    targets: list[RepairTarget] = []
    for holding in holdings.list_all():
        history = tuple(baselines.list_by_holding(holding.holding_id))
        pointer = get_pointer(holding.holding_id, store_dir)

        if pointer is None:
            if not history:
                # 初回。pointer も履歴も無いのは正常であり integrity_error ではない。
                continue
            targets.append(
                RepairTarget(
                    holding_id=holding.holding_id,
                    reason=RepairReason.POINTER_MISSING,
                    history=history,
                    current_pointer_version=None,
                    pointed_version=None,
                )
            )
            continue

        pointed = baselines.get(pointer.active_baseline_id)
        if pointed is None:
            targets.append(
                RepairTarget(
                    holding_id=holding.holding_id,
                    reason=RepairReason.BASELINE_NOT_FOUND,
                    history=history,
                    current_pointer_version=pointer.pointer_version,
                    pointed_version=None,
                )
            )
        elif pointed.version != pointer.active_baseline_version:
            targets.append(
                RepairTarget(
                    holding_id=holding.holding_id,
                    reason=RepairReason.VERSION_MISMATCH,
                    history=history,
                    current_pointer_version=pointer.pointer_version,
                    pointed_version=pointed.version,
                )
            )
    return targets


def _print_target(target: RepairTarget) -> None:
    """★ holding_id・銘柄コード・所有者名を平文で出さない(Issue #135)。

    ★ `baseline_id` も出さない。生成規則が
      `investment_thesis_service.py:129` の `f"{holding_id}:v{version}"` であり、
      **holding_id(= 所有者#銘柄コード)がそのまま埋め込まれている**ため。
      baseline は holding 内で version が一意(baseline_id の主キー構成上)なので、
      **version だけで特定できる**。
    """
    typer.echo(f"holding_ref={log_ref(target.holding_id)}  reason={target.reason.value}")
    if target.pointed_version is not None:
        typer.echo(f"  pointer が指す version: {target.pointed_version}")
    typer.echo(f"  履歴 {len(target.history)} 件:")
    for baseline in sorted(target.history, key=lambda b: b.version):
        typer.echo(
            f"    version={baseline.version}"
            f" status={baseline.status.value} origin={baseline.origin.value}"
        )
    if target.requires_explicit_baseline:
        typer.echo("  ★ 修復には --baseline-version の明示指定が必要です(自動で選びません)")
    else:
        candidate = target.default_candidate
        if candidate is not None:
            typer.echo(
                f"  既定の候補: version={candidate.version}  ★ --baseline-version で上書きできます"
            )


def _resolve_baseline(
    target: RepairTarget, baseline_version: int | None
) -> InvestmentThesisBaseline | None:
    """採用する baseline を決める。決められなければ None を返す。

    ★ version で指定する(baseline_id は PII を含むため入出力に使わない)。
      version は 1 つの holding の履歴内で一意である。
    """
    if baseline_version is not None:
        for baseline in target.history:
            if baseline.version == baseline_version:
                return baseline
        return None
    if target.requires_explicit_baseline:
        return None
    return target.default_candidate


@app.command("scan")
def scan(
    store_dir: Path | None = typer.Option(None, help="ローカルストアのディレクトリ"),
) -> None:
    """pointer 不整合を検出して一覧表示する(★ 読み取りのみ。書き込みは行わない)。"""
    targets = _detect(
        HoldingRepository(store_dir), InvestmentThesisBaselineRepository(store_dir), store_dir
    )
    if not targets:
        typer.echo("対象 0 件(pointer 不整合は検出されませんでした)")
        typer.echo("★ 0 件は「壊れている」ではありません。対象なしを意味します。")
        return

    typer.echo(f"対象 {len(targets)} 件")
    for target in targets:
        typer.echo("")
        _print_target(target)
    typer.echo("")
    typer.echo("★ これは dry-run です。修復するには apply を実行してください。")


@app.command("apply")
def apply(
    holding_ref: str = typer.Option(
        ..., help="修復する保有の holding_ref(scan が表示した sha256 形式)"
    ),
    baseline_version: int | None = typer.Option(
        None,
        help="採用する baseline の version。version 不一致・baseline 不在では必須",
    ),
    store_dir: Path | None = typer.Option(None, help="ローカルストアのディレクトリ"),
) -> None:
    """pointer を復元する(★ 書き込みを行う。Production では人間の承認のもとで実行する)。

    ★ scan の結果を引き継がず、実行時にもう一度判定し直す(状態が変わっている
      可能性があるため)。既に整合していれば対象に現れず、何も書き込まない。
    """
    targets = _detect(
        HoldingRepository(store_dir), InvestmentThesisBaselineRepository(store_dir), store_dir
    )
    matched = [t for t in targets if log_ref(t.holding_id) == holding_ref]
    if not matched:
        typer.echo(f"holding_ref={holding_ref} は現在の対象に含まれません(既に整合している可能性)")
        raise typer.Exit(code=1)
    if len(matched) > 1:
        # holding_ref は sha256 の先頭 8 文字であり、理論上は衝突しうる。
        # ★ どれか 1 件を黙って選ぶと **別の保有の pointer を書き換える**。
        typer.echo(
            f"holding_ref={holding_ref}: 一意ではありません({len(matched)} 件が該当)。"
            "誤った保有を書き換えないため中断します"
        )
        raise typer.Exit(code=1)

    target = matched[0]
    baseline = _resolve_baseline(target, baseline_version)
    if baseline is None:
        if target.requires_explicit_baseline and baseline_version is None:
            typer.echo(
                f"holding_ref={holding_ref}: reason={target.reason.value} では"
                " --baseline-version の指定が必要です(自動で選びません)"
            )
        else:
            typer.echo(f"holding_ref={holding_ref}: 指定された version が履歴にありません")
        raise typer.Exit(code=1)

    try:
        if target.reason is RepairReason.POINTER_MISSING:
            created = create_pointer(
                target.holding_id,
                baseline.baseline_id,
                baseline.version,
                updated_by=_UPDATED_BY,
                store_dir=store_dir,
            )
            if created is None:
                typer.echo(f"holding_ref={holding_ref}: pointer の作成に失敗しました")
                raise typer.Exit(code=1)
        else:
            assert target.current_pointer_version is not None
            update_pointer(
                target.holding_id,
                baseline.baseline_id,
                baseline.version,
                expected_pointer_version=target.current_pointer_version,
                updated_by=_UPDATED_BY,
                store_dir=store_dir,
            )
    except BaselinePointerConflictError:
        typer.echo(
            f"holding_ref={holding_ref}: 他の更新と競合したため中断しました(再実行してください)"
        )
        raise typer.Exit(code=1) from None

    typer.echo(
        f"holding_ref={holding_ref}: pointer を復元しました"
        f"(reason={target.reason.value} version={baseline.version})"
    )
