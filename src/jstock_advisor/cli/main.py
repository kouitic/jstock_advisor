"""jstock CLIエントリポイント。"""

from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv

from jstock_advisor.cli import (
    analyze,
    audit,
    evaluation,
    feedback,
    holdings,
    performance,
    review,
    rules,
    shareholder_benefit,
    transactions,
    watchlist,
    watchlist_screening,
)

# プロジェクトルートの .env を読み込む(LINE_CHANNEL_ACCESS_TOKEN等)。
# 既にOS環境変数として設定済みの値は上書きしない。ファイルが無くても無視される。
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

app = typer.Typer(help="日本株 長期・高配当・株主優待重視の売買支援システム CLI")
app.add_typer(holdings.app, name="holdings")
app.add_typer(watchlist.app, name="watchlist")
app.add_typer(analyze.app, name="analyze")
app.add_typer(audit.app, name="audit")
app.add_typer(transactions.app, name="transactions")
app.add_typer(evaluation.app, name="evaluation")
app.add_typer(performance.app, name="performance")
app.add_typer(feedback.app, name="feedback")
app.add_typer(rules.app, name="rules")
app.add_typer(review.app, name="review")
app.add_typer(shareholder_benefit.app, name="shareholder-benefit")
app.add_typer(watchlist_screening.app, name="watchlist-screening")


@app.callback()
def _root_callback() -> None:
    """本ツールは投資判断を支援するためのものです。最終的な売買判断は利用者が行ってください。"""


if __name__ == "__main__":
    app()
