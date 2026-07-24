"""ルールバージョン管理・改善提案CLIコマンド(要求仕様41・43・44・45節)。

すべての承認系コマンド(approve-version/activate-version/approve-proposal等)は
人間が明示的に実行しない限り効果を持たない。自動承認・自動適用は行わない。
"""

from __future__ import annotations

from collections.abc import Callable

import typer

from jstock_advisor.domain.entities.rule_version import RuleProposal, RuleVersion
from jstock_advisor.services.backtest_service import BacktestService
from jstock_advisor.services.rule_proposal_service import RuleProposalService
from jstock_advisor.services.rule_version_service import RuleVersionService

app = typer.Typer(help="ルールバージョン管理・改善提案(要人間承認)")


# --- ルールバージョン --------------------------------------------------------


@app.command("list-versions")
def list_versions() -> None:
    """ルールバージョン一覧を表示する。"""
    versions = RuleVersionService().list_all()
    if not versions:
        typer.echo("ルールバージョンはありません。")
        return
    for v in versions:
        active_mark = "[ACTIVE] " if v.is_active else ""
        typer.echo(
            f"{active_mark}{v.rule_version} ({v.approval_status.value}): {v.change_description}"
        )


@app.command("create-version")
def create_version(
    rule_version: str = typer.Argument(..., help="新しいバージョン識別子(例: v2-mvp)"),
    description: str = typer.Option(..., "--description", help="変更内容"),
    reason: str = typer.Option(..., "--reason", help="変更理由"),
    based_on_review: str = typer.Option(None, "--based-on-review"),
    previous_version: str = typer.Option(None, "--previous-version"),
) -> None:
    """新しいルールバージョンをDRAFTとして作成する。"""
    service = RuleVersionService()
    try:
        version = service.create_draft(
            rule_version=rule_version,
            change_description=description,
            change_reason=reason,
            based_on_review=based_on_review,
            previous_version=previous_version,
        )
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"DRAFTとして作成しました: {version.rule_version}")


@app.command("submit-version")
def submit_version(rule_version: str = typer.Argument(...)) -> None:
    """DRAFTのバージョンを承認申請(PROPOSED)にする。"""
    _run_version_action(RuleVersionService().submit_for_approval, rule_version)


@app.command("approve-version")
def approve_version(
    rule_version: str = typer.Argument(...),
    approved_by: str = typer.Option(..., "--approved-by"),
) -> None:
    """PROPOSEDのバージョンを承認する。"""
    service = RuleVersionService()
    try:
        version = service.approve(rule_version, approved_by)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"承認しました: {version.rule_version} (approved_by={approved_by})")


@app.command("reject-version")
def reject_version(rule_version: str = typer.Argument(...)) -> None:
    """PROPOSEDのバージョンを却下する。"""
    _run_version_action(RuleVersionService().reject, rule_version)


@app.command("activate-version")
def activate_version(rule_version: str = typer.Argument(...)) -> None:
    """承認済みのバージョンを有効化する(既存の有効バージョンは自動的に無効化される)。

    設定ファイル(config/*.yaml)自体の変更は本コマンドの対象外であり、別途手動で
    反映する必要がある。
    """
    _run_version_action(RuleVersionService().activate, rule_version)
    typer.echo("※config/*.yamlへの実際の値の反映は別途手動で行ってください。")


@app.command("rollback-version")
def rollback_version(
    target_version: str = typer.Argument(..., help="ロールバック先のバージョン"),
) -> None:
    """現在の有効バージョンから、指定バージョンへロールバックする。"""
    service = RuleVersionService()
    try:
        version = service.rollback_to(target_version)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"ロールバックしました: {version.rule_version} を有効化しました")


def _run_version_action(action: Callable[[str], RuleVersion], rule_version: str) -> None:
    try:
        result = action(rule_version)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"更新しました: {result.rule_version} ({result.approval_status.value})")


# --- バックテスト -------------------------------------------------------------


@app.command("backtest")
def backtest(
    target: str = typer.Argument(
        ..., help="対象パラメータ(例: screening.total_yield.min_total_yield_pct)"
    ),
    current_value: float = typer.Argument(...),
    proposed_value: float = typer.Argument(...),
) -> None:
    """単一指標の閾値変更について、既存の評価済み推奨を用いた感応度分析を行う。"""
    result = BacktestService().run(target, current_value, proposed_value)
    if not result.supported:
        typer.echo(f"バックテスト対象外/データ不足: {result.reason_unsupported}")
        raise typer.Exit(code=1)

    typer.echo(f"対象: {target} ({current_value} → {proposed_value})")
    typer.echo(f"現行ルールでの評価件数: {result.evaluation_count_current}")
    typer.echo(f"新ルールでの評価件数: {result.evaluation_count_proposed}")
    if result.current_performance is not None:
        typer.echo(
            f"現行: 成功率={result.current_performance.success_rate_pct} "
            f"平均リターン={result.current_performance.avg_price_return_pct}"
        )
    if result.proposed_performance is not None:
        typer.echo(
            f"新ルール: 成功率={result.proposed_performance.success_rate_pct} "
            f"平均リターン={result.proposed_performance.avg_price_return_pct}"
        )
    if result.excluded_recommendation_ids:
        typer.echo(f"除外される推奨: {len(result.excluded_recommendation_ids)}件")


# --- 改善提案 -----------------------------------------------------------------


@app.command("propose")
def propose(
    target: str = typer.Argument(...),
    current_value: str = typer.Argument(...),
    proposed_value: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    risk_impact: str = typer.Option(..., "--risk-impact"),
    overfitting_risk: str = typer.Option(..., "--overfitting-risk"),
    rollback_condition: str = typer.Option(..., "--rollback-condition"),
    application_period: str = typer.Option(None, "--application-period"),
) -> None:
    """改善提案(RuleProposal)をDRAFTとして作成する。評価件数不足の場合はエラーになる。"""

    def _coerce(value: str) -> float | str:
        try:
            return float(value)
        except ValueError:
            return value

    service = RuleProposalService()
    try:
        proposal = service.create_proposal(
            target=target,
            current_value=_coerce(current_value),
            proposed_value=_coerce(proposed_value),
            reason=reason,
            risk_impact=risk_impact,
            overfitting_risk_assessment=overfitting_risk,
            rollback_condition=rollback_condition,
            recommended_application_period=application_period,
        )
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"DRAFTとして作成しました: {proposal.proposal_id}")
    typer.echo(f"  評価件数: {proposal.evaluation_count}")
    typer.echo(f"  現行成績: {proposal.current_rule_performance}")
    typer.echo(f"  新ルール成績: {proposal.proposed_rule_backtest_performance}")
    typer.echo(f"  差分: {proposal.performance_diff}")


@app.command("list-proposals")
def list_proposals() -> None:
    """改善提案一覧を表示する。"""
    proposals = RuleProposalService().list_all()
    if not proposals:
        typer.echo("改善提案はありません。")
        return
    for p in proposals:
        typer.echo(
            f"[{p.status.value}] {p.proposal_id} {p.target}: "
            f"{p.current_value} → {p.proposed_value}({p.reason})"
        )


@app.command("submit-proposal")
def submit_proposal(proposal_id: str = typer.Argument(...)) -> None:
    """DRAFTの提案を承認申請(PROPOSED)にする。"""
    _run_proposal_action(RuleProposalService().submit_for_review, proposal_id)


@app.command("approve-proposal")
def approve_proposal(proposal_id: str = typer.Argument(...)) -> None:
    """PROPOSEDの提案を承認する。実際の設定反映・バージョン有効化は別途手動で行う。"""
    _run_proposal_action(RuleProposalService().approve, proposal_id)
    typer.echo(
        "※承認は記録されましたが、config/*.yamlへの反映・"
        "rules activate-versionによる有効化は別途手動で行ってください。"
    )


@app.command("reject-proposal")
def reject_proposal(proposal_id: str = typer.Argument(...)) -> None:
    """PROPOSEDの提案を却下する。"""
    _run_proposal_action(RuleProposalService().reject, proposal_id)


def _run_proposal_action(action: Callable[[str], RuleProposal], proposal_id: str) -> None:
    try:
        result = action(proposal_id)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"更新しました: {result.proposal_id} ({result.status.value})")
