"""ユーザー定性フィードバックCLIコマンド(要求仕様47節)。"""

from __future__ import annotations

import typer

from jstock_advisor.domain.jst import format_jst
from jstock_advisor.services.user_feedback_service import UserFeedbackService

app = typer.Typer(help="推奨・売買記録に対する定性フィードバックの記録")


@app.command("add")
def add_feedback(
    recommendation_id: str = typer.Option(None, "--recommendation-id"),
    transaction_id: str = typer.Option(None, "--transaction-id"),
    satisfaction_score: int = typer.Option(None, "--satisfaction-score", help="1〜5"),
    risk_explanation_adequate: bool = typer.Option(
        None, "--risk-explanation-adequate/--no-risk-explanation-adequate"
    ),
    notification_timing_appropriate: bool = typer.Option(
        None, "--notification-timing-appropriate/--no-notification-timing-appropriate"
    ),
    recommended_price_practical: bool = typer.Option(
        None, "--recommended-price-practical/--no-recommended-price-practical"
    ),
    reason_convincing: bool = typer.Option(None, "--reason-convincing/--no-reason-convincing"),
    helpful_for_decision: bool = typer.Option(
        None, "--helpful-for-decision/--no-helpful-for-decision"
    ),
    comment: str = typer.Option(None, "--comment"),
) -> None:
    """フィードバックを記録する。"""
    service = UserFeedbackService()
    try:
        feedback = service.submit(
            recommendation_id=recommendation_id,
            transaction_id=transaction_id,
            satisfaction_score=satisfaction_score,
            risk_explanation_adequate=risk_explanation_adequate,
            notification_timing_appropriate=notification_timing_appropriate,
            recommended_price_practical=recommended_price_practical,
            reason_convincing=reason_convincing,
            helpful_for_decision=helpful_for_decision,
            comment=comment,
        )
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"記録しました: {feedback.feedback_id}")


@app.command("list")
def list_feedback(
    recommendation_id: str = typer.Argument(None, help="推奨IDで絞り込む(省略時は全件)"),
) -> None:
    """記録済みのフィードバックを表示する。"""
    service = UserFeedbackService()
    items = service.list_feedback(recommendation_id)
    if not items:
        typer.echo("フィードバックはありません。")
        return
    for item in items:
        typer.echo(
            f"[{format_jst(item.created_at)}] recommendation_id={item.recommendation_id} "
            f"transaction_id={item.transaction_id} satisfaction_score={item.satisfaction_score}"
        )
        if item.comment:
            typer.echo(f"  コメント: {item.comment}")
