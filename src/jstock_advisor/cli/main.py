"""jstock CLIエントリポイント。"""

from __future__ import annotations

import typer

from jstock_advisor.cli import analyze, holdings, watchlist

app = typer.Typer(help="日本株 長期・高配当・株主優待重視の売買支援システム CLI")
app.add_typer(holdings.app, name="holdings")
app.add_typer(watchlist.app, name="watchlist")
app.add_typer(analyze.app, name="analyze")


@app.callback()
def _root_callback() -> None:
    """本ツールは投資判断を支援するためのものです。最終的な売買判断は利用者が行ってください。"""


if __name__ == "__main__":
    app()
