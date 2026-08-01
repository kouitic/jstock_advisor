"""候補ユニバース(東証上場銘柄一覧・JPX400構成銘柄)のローカルキャッシュ管理CLI(6節)。

本番前の事前リハーサル・ローカル開発用の**任意**ツール(必須の初回セットアップ
手順ではない)。週次`WatchlistDispatcherFunction`の通常起動時にも同じDownloaderが
自動的にキャッシュを取得・検証・昇格するため、本番運用では通常このCLIを使う
必要はない。本番のS3キャッシュを週次スケジュール外で手動更新したい場合は、
`WatchlistDispatcherFunction`を`aws lambda invoke`で直接手動起動すること
(運用手順書参照)。ローカルCLIは常にローカルキャッシュ
(`data/cache/candidate_universe/`)のみを読み書きし、本番S3へは一切アクセスしない。
"""

from __future__ import annotations

import datetime as dt

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.collection_store import (
    resolve_candidate_universe_local_cache_dir,
)
from jstock_advisor.services.candidate_universe_downloader import (
    CandidateUniverseCacheIO,
    refresh_candidate_universe_cache,
)

app = typer.Typer(help="候補ユニバース(東証上場銘柄一覧・JPX400構成銘柄)のローカルキャッシュ管理")


@app.command("refresh")
def refresh() -> None:
    """ローカルキャッシュ(data/cache/candidate_universe/)を取得・検証・更新する。"""
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    cu = config.watchlist_screening.candidate_universe
    if cu.provider != "jpx":
        typer.echo(f'candidate_universe.provider="{cu.provider}"のため対象外です(jpxのみ対応)。')
        raise typer.Exit(code=1)
    assert cu.jpx_listed_issues_url is not None
    assert cu.jpx_400_weight_url is not None

    outcomes = refresh_candidate_universe_cache(
        cu.jpx_listed_issues_url, cu.jpx_400_weight_url, cu.target_market_segments, now
    )
    failed = False
    for outcome in outcomes:
        if outcome.promoted:
            assert outcome.metadata is not None
            typer.echo(
                f"{outcome.source}: 更新しました "
                f"(source_date={outcome.metadata.source_date}, "
                f"件数={outcome.metadata.selected_count})"
            )
        else:
            failed = True
            typer.echo(f"{outcome.source}: 更新に失敗しました({outcome.reason})")
    typer.echo(f"\nローカルキャッシュ保存先: {resolve_candidate_universe_local_cache_dir()}")
    if failed:
        raise typer.Exit(code=1)


@app.command("status")
def status() -> None:
    """ローカルキャッシュの現在の状態(source_date・件数・保存時刻)を表示する。"""
    cache_io = CandidateUniverseCacheIO()
    for source in ("listed_issues", "jpx400"):
        cached = cache_io.read_current(source)
        if cached is None:
            typer.echo(f"{source}: キャッシュなし")
            continue
        _, metadata = cached
        typer.echo(
            f"{source}: source_date={metadata.source_date} "
            f"件数={metadata.selected_count} "
            f"downloaded_at={metadata.downloaded_at.isoformat()} "
            f"promoted_at={metadata.promoted_at.isoformat()}"
        )
