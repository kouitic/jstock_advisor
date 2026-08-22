"""保有判断スコア方式のランタイムConfig・個別購入理由CLIコマンド(実装プラン)。

set-mode/kill-switch等はRuntimeConfig(DynamoDB専用テーブル)を直接更新し、
再デプロイ不要で反映される。ローカル実行時は既定でローカルJSONストアを操作するが、
`--target aws`を指定すると本番DynamoDBを直接操作する
(AWS認証情報が環境に設定済みであることが前提)。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
from collections.abc import Iterator
from pathlib import Path

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    AccountType,
    FinancialPolicyOverride,
    RuntimeConfigMode,
    ThesisConditionAttestationStatus,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import ReasonImpact
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.infrastructure.aws.baseline_pointer import BaselinePointerConflictError
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.infrastructure.local_repository import (
    holding_decision_runtime_config_repository as runtime_config_repo,
)
from jstock_advisor.services.holding_decision_backtest_service import (
    BacktestRow,
    business_date,
    resolve_target_stock_codes,
    run_history_replay,
    run_live_comparison,
    write_backtest_csv,
)
from jstock_advisor.services.holding_decision_compare_service import (
    CompareRow,
    run_compare,
    write_compare_csv,
)
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
    RuntimeConfigAlreadyInitializedError,
)
from jstock_advisor.services.investment_thesis_service import (
    InvestmentThesisService,
)
from jstock_advisor.services.provider_factory import (
    build_mock_provider_bundle,
    build_real_provider_bundle,
)

app = typer.Typer(help="保有判断スコア方式のランタイムConfig・個別購入理由(要人間操作)")

_AWS_OVERRIDE_ENV_VAR = "AWS_LAMBDA_FUNCTION_NAME"


@contextlib.contextmanager
def _target_backend(target: str) -> Iterator[None]:
    """--target aws指定時、ローカルCLIから本番DynamoDBバックエンドを直接操作する。

    infrastructure/collection_store.pyのrunning_on_lambda()はAWS_LAMBDA_FUNCTION_NAME
    環境変数の有無だけでバックエンドを判定するため、CLIセッション中だけこの変数を
    一時的に設定してDynamoDB経路を強制する(AWS認証情報は別途環境に設定済みであること)。
    """
    if target != "aws":
        yield
        return
    previous = os.environ.get(_AWS_OVERRIDE_ENV_VAR)
    os.environ[_AWS_OVERRIDE_ENV_VAR] = "cli-target-aws-override"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_AWS_OVERRIDE_ENV_VAR, None)
        else:
            os.environ[_AWS_OVERRIDE_ENV_VAR] = previous


def _cache_ttl_seconds() -> int:
    return load_config().holding_decision.runtime_config_cache_ttl_seconds


@app.command("init-runtime-config")
def init_runtime_config(
    changed_by: str = typer.Option(..., "--changed-by"),
    mode: RuntimeConfigMode = typer.Option(RuntimeConfigMode.LEGACY, "--mode"),
    notification_enabled: bool = typer.Option(False, "--notification-enabled"),
    financial_policy_override: FinancialPolicyOverride = typer.Option(
        FinancialPolicyOverride.DEFAULT, "--financial-policy-override"
    ),
    target: str = typer.Option("local", "--target", help="local | aws"),
) -> None:
    """RuntimeConfigの初回作成(既に存在する場合は失敗する)。"""
    with _target_backend(target):
        service = HoldingDecisionRuntimeConfigService(cache_ttl_seconds=_cache_ttl_seconds())
        try:
            created = service.init_config(
                updated_by=changed_by,
                mode=mode,
                notification_enabled=notification_enabled,
                financial_policy_override=financial_policy_override,
            )
        except RuntimeConfigAlreadyInitializedError as e:
            typer.echo(str(e))
            raise typer.Exit(code=1) from e
    typer.echo(
        f"初期化しました: mode={created.mode.value} "
        f"notification_enabled={created.notification_enabled} "
        f"config_version={created.config_version}"
    )


@app.command("set-mode")
def set_mode(
    mode: RuntimeConfigMode = typer.Argument(...),
    changed_by: str = typer.Option(..., "--changed-by"),
    reason: str = typer.Option(..., "--reason"),
    target: str = typer.Option("local", "--target", help="local | aws"),
) -> None:
    """mode(legacy/shadow/active)を切り替える。再デプロイ不要。"""
    with _target_backend(target):
        service = HoldingDecisionRuntimeConfigService(cache_ttl_seconds=_cache_ttl_seconds())
        current = service.get_config()
        if current.is_fallback:
            typer.echo("RuntimeConfigが未初期化です。先にinit-runtime-configを実行してください。")
            raise typer.Exit(code=1)
        try:
            updated = service.update_config(
                expected_config_version=current.config.config_version,
                mode=mode,
                notification_enabled=current.config.notification_enabled,
                financial_policy_override=current.config.financial_policy_override,
                updated_by=changed_by,
                change_reason=reason,
            )
        except (runtime_config_repo.RuntimeConfigConflictError, BaselinePointerConflictError) as e:
            typer.echo(f"他の更新が先に行われたため再確認が必要です: {e}")
            raise typer.Exit(code=1) from e
    typer.echo(f"modeを{updated.mode.value}へ更新しました(config_version={updated.config_version})")


@app.command("kill-switch")
def kill_switch(
    state: str = typer.Argument(..., help="on | off"),
    changed_by: str = typer.Option(..., "--changed-by"),
    reason: str = typer.Option(..., "--reason"),
    target: str = typer.Option("local", "--target", help="local | aws"),
) -> None:
    """保有銘柄分析に関するLINE通知をすべて停止する緊急スイッチ(modeとは独立)。

    停止対象: 旧売却通知・新保有判断通知・利確通知・ポートフォリオ集中リスク通知・
    バッチ完了サマリー通知(コードレビュー対応で適用範囲を拡張)。
    停止しないもの: 判定処理そのもの・Recommendation/HoldingDecisionResultの保存・
    監査ログの記録(空振りにはならない)。

    `on`は`notification_enabled=False`(通知停止)、`off`は`notification_enabled=True`
    (通知許可)に対応する。
    """
    if state not in ("on", "off"):
        typer.echo("stateはon/offのいずれかを指定してください。")
        raise typer.Exit(code=1)
    notification_enabled = state == "off"
    with _target_backend(target):
        service = HoldingDecisionRuntimeConfigService(cache_ttl_seconds=_cache_ttl_seconds())
        current = service.get_config()
        if current.is_fallback:
            typer.echo("RuntimeConfigが未初期化です。先にinit-runtime-configを実行してください。")
            raise typer.Exit(code=1)
        try:
            updated = service.update_config(
                expected_config_version=current.config.config_version,
                mode=current.config.mode,
                notification_enabled=notification_enabled,
                financial_policy_override=current.config.financial_policy_override,
                updated_by=changed_by,
                change_reason=reason,
            )
        except runtime_config_repo.RuntimeConfigConflictError as e:
            typer.echo(f"他の更新が先に行われたため再確認が必要です: {e}")
            raise typer.Exit(code=1) from e
    typer.echo(
        f"kill switchを{state}にしました(notification_enabled={updated.notification_enabled})"
    )


@app.command("show-runtime-config")
def show_runtime_config(
    target: str = typer.Option("local", "--target", help="local | aws"),
) -> None:
    with _target_backend(target):
        service = HoldingDecisionRuntimeConfigService(cache_ttl_seconds=_cache_ttl_seconds())
        lookup = service.get_config()
    if lookup.is_fallback:
        typer.echo(
            "RuntimeConfigは未初期化です(フォールバック既定値: mode=legacy, notification無効)。"
        )
        return
    cfg = lookup.config
    typer.echo(
        f"mode={cfg.mode.value} notification_enabled={cfg.notification_enabled} "
        f"financial_policy_override={cfg.financial_policy_override.value} "
        f"config_version={cfg.config_version} updated_by={cfg.updated_by} "
        f"updated_at={cfg.updated_at} change_reason={cfg.change_reason}"
    )


# --- バックテスト/リプレイ(実装プラン修正5) -----------------------------------


def _print_backtest_rows(rows: list[BacktestRow]) -> None:
    if not rows:
        typer.echo("該当するデータがありません。")
        return
    typer.echo(
        f"{'date':<12}{'stock_code':<12}{'source':<9}"
        f"{'legacy':<20}{'legacy要通知':<12}{'legacy作成':<10}{'legacy送信':<10}"
        f"{'new_score':<11}{'new_category':<28}{'new要通知':<10}{'new作成':<8}{'new送信':<8}"
        f"{'legacy match':<20}new match"
    )
    for row in rows:
        typer.echo(
            f"{row.evaluated_at.date().isoformat():<12}{row.stock_code:<12}{row.source:<9}"
            f"{(row.legacy_recommendation_type or '-'):<20}"
            f"{str(row.legacy_should_notify):<12}{str(row.legacy_recommendation_created):<10}"
            f"{str(row.legacy_notification_sent):<10}"
            f"{('-' if row.new_score is None else f'{row.new_score:.2f}'):<11}"
            f"{(row.new_category or '-'):<28}"
            f"{str(row.new_should_notify):<10}{str(row.new_recommendation_created):<8}"
            f"{str(row.new_notification_sent):<8}"
            f"{row.legacy_match_method:<20}{row.new_match_method}"
        )
        for warning in (
            row.legacy_match_warning,
            row.legacy_notification_warning,
            row.new_match_warning,
            row.new_notification_warning,
        ):
            if warning:
                typer.echo(f"    ! {warning}")


def _build_holding_override(
    stock_code: str,
    purchase_price: str,
    purchase_date: str,
    shares: str,
    now: dt.datetime,
) -> Holding:
    """--purchase-price等から非保有銘柄用の仮Holdingを構築する(コードレビュー対応)。

    ExternalValueParserで解析する(全角・カンマ・".0"付き入力に対応するため、
    Typer側で先にDecimal/date/intへ変換させない)。妥当性検証もここで行う。
    """
    price = ExternalValueParser.decimal(purchase_price)
    date_value = ExternalValueParser.date(purchase_date)
    shares_value = ExternalValueParser.integer(shares)
    if price is None or date_value is None or shares_value is None:
        typer.echo(
            "--purchase-price/--purchase-date/--sharesの形式が不正です"
            "(price=数値、date=YYYY-MM-DD等、shares=整数)。"
        )
        raise typer.Exit(code=1)
    if price <= 0:
        typer.echo("--purchase-priceは正の値で指定してください。")
        raise typer.Exit(code=1)
    if shares_value <= 0:
        typer.echo("--sharesは正の値で指定してください。")
        raise typer.Exit(code=1)
    if date_value > business_date(now):
        typer.echo("--purchase-dateは評価基準日(JST)以前の日付を指定してください。")
        raise typer.Exit(code=1)
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name=stock_code,
        shares=shares_value,
        average_purchase_price=price,
        total_purchase_amount=price * shares_value,
        first_purchase_date=date_value,
        last_purchase_date=date_value,
        account_type=AccountType.GENERAL,
        created_at=now,
        updated_at=now,
    )


@app.command("backtest")
def backtest(
    stock_code: list[str] = typer.Option(
        [], "--stock-code", help="対象銘柄コード(複数指定可、省略時は全保有銘柄)"
    ),
    start_date: str = typer.Option(
        None,
        "--start-date",
        help="YYYY-MM-DD。指定するとreplayモード(過去に保存された評価結果の再生)",
    ),
    end_date: str = typer.Option(
        None, "--end-date", help="YYYY-MM-DD(--start-date指定時のみ有効。省略時は本日)"
    ),
    source: str = typer.Option(
        "mock", "--source", help="liveモード(期間未指定時)のデータ取得元: mock(既定)/ real"
    ),
    allow_same_day_fallback: bool = typer.Option(
        False,
        "--allow-same-day-fallback",
        help=(
            "replayモード専用。旧方式Recommendationとの対応付けで、近接時刻"
            "(既定5分)候補が無い場合に同一日候補での対応付け(信頼度中程度)を"
            "許容する(既定False。有効時はSAME_DAY_FALLBACK行をActive移行判断"
            "の集計から除外すること)"
        ),
    ),
    purchase_price: str | None = typer.Option(
        None,
        "--purchase-price",
        help="非保有銘柄をliveモードで評価する場合の取得単価(単一銘柄限定)",
    ),
    purchase_date: str | None = typer.Option(
        None, "--purchase-date", help="非保有銘柄をliveモードで評価する場合の取得日(単一銘柄限定)"
    ),
    shares: str | None = typer.Option(
        None, "--shares", help="非保有銘柄をliveモードで評価する場合の株数(単一銘柄限定)"
    ),
    csv_path: Path = typer.Option(None, "--csv", help="結果をCSVへ出力するパス"),
) -> None:
    """保有判断スコア方式のバックテスト/リプレイ(実装プラン修正5)。

    --start-date/--end-dateを指定しない場合はliveモード(指定銘柄・全保有銘柄を
    現在のデータで新旧両エンジンにかけて比較する)。指定した場合はreplayモード
    (指定期間に実際に保存されたHoldingDecisionResult/Recommendationを再生する。
    このシステムは財務・配当・優待データの過去時点スナップショットを保持して
    いないため、真の意味での過去時点シミュレーションはできない。詳細は
    運用手順書のバックテスト手順を参照)。

    非保有銘柄はliveモードで新方式のみ評価され、旧方式は評価されない
    (架空の取得単価による誤評価を防ぐため)。--purchase-price/--purchase-date/
    --sharesをすべて指定した場合に限り、単一銘柄指定時だけ旧方式も評価する。
    """
    stock_codes = resolve_target_stock_codes(stock_code)
    if not stock_codes:
        typer.echo("対象銘柄がありません(--stock-codeを指定するか、保有銘柄を登録してください)。")
        raise typer.Exit(code=1)

    purchase_options = (purchase_price, purchase_date, shares)
    if any(o is not None for o in purchase_options) and not all(
        o is not None for o in purchase_options
    ):
        typer.echo(
            "--purchase-price/--purchase-date/--sharesはすべて指定するか、"
            "すべて省略してください(一部のみの指定はできません)。"
        )
        raise typer.Exit(code=1)
    if all(o is not None for o in purchase_options) and len(stock_codes) != 1:
        typer.echo("--purchase-price等は単一銘柄(--stock-codeを1件のみ指定)の場合のみ使用できます。")
        raise typer.Exit(code=1)
    if all(o is not None for o in purchase_options) and start_date is not None:
        typer.echo("--purchase-price等はreplayモード(--start-date指定時)では使用できません。")
        raise typer.Exit(code=1)

    if start_date is not None:
        try:
            parsed_start = dt.date.fromisoformat(start_date)
            parsed_end = dt.date.fromisoformat(end_date) if end_date else dt.date.today()
        except ValueError as e:
            typer.echo("--start-date/--end-dateはYYYY-MM-DD形式で指定してください。")
            raise typer.Exit(code=1) from e
        rows = run_history_replay(
            stock_codes, parsed_start, parsed_end, allow_same_day_fallback=allow_same_day_fallback
        )
    else:
        now = dt.datetime.now(dt.UTC)
        config = load_config()
        providers = (
            build_real_provider_bundle(now, config)
            if source == "real"
            else build_mock_provider_bundle(now)
        )
        holding_overrides = None
        if all(o is not None for o in purchase_options):
            assert purchase_price is not None
            assert purchase_date is not None
            assert shares is not None
            override = _build_holding_override(
                stock_codes[0], purchase_price, purchase_date, shares, now
            )
            holding_overrides = {stock_codes[0]: override}
        rows = run_live_comparison(
            stock_codes, providers, config, now, holding_overrides=holding_overrides
        )

    _print_backtest_rows(rows)

    if csv_path is not None:
        write_backtest_csv(rows, csv_path)
        typer.echo(f"CSVへ出力しました: {csv_path} ({len(rows)}件)")


# --- Shadow比較レポート(実装プラン修正6) ---------------------------------------


def _format_reasons(reasons: tuple[ReasonImpact, ...]) -> str:
    return "; ".join(f"{r.reason_code}({r.score_impact:+.1f})" for r in reasons)


def _print_compare_rows(rows: list[CompareRow]) -> None:
    if not rows:
        typer.echo("該当するデータがありません。")
        return
    for row in rows:
        typer.echo(f"■ {row.stock_code}")
        typer.echo(f"  旧方式判定: {row.legacy_category}(要通知={row.legacy_should_notify})")
        typer.echo(
            f"  新方式判定: {row.new_category or '-'}"
            f"(score={'−' if row.new_score is None else f'{row.new_score:.2f}'}, "
            f"要通知={row.new_should_notify})"
        )
        typer.echo(f"  差分: {row.category_diff} / 通知差分: {row.should_notify_diff.value}")
        coverage = "-" if row.coverage_overall is None else f"{row.coverage_overall:.2f}"
        typer.echo(f"  coverage(overall): {coverage}")
        typer.echo(
            f"  hard gate: 発動={row.hard_gate_triggered} "
            f"理由={','.join(row.hard_gate_reason_codes) or '-'}"
        )
        typer.echo(f"  保有を支持する要因: {_format_reasons(row.positive_reasons) or '-'}")
        typer.echo(f"  主な減点要因: {_format_reasons(row.negative_reasons) or '-'}")


@app.command("compare")
def compare(
    stock_code: list[str] = typer.Option(
        [], "--stock-code", help="対象銘柄コード(複数指定可、省略時は全保有銘柄)"
    ),
    source: str = typer.Option("mock", "--source", help="データ取得元: mock(既定)/ real"),
    csv_path: Path = typer.Option(None, "--csv", help="結果をCSVへ出力するパス"),
) -> None:
    """Shadow運用比較レポート(実装プラン修正6)。

    指定銘柄(または全保有銘柄)を現在のデータで新旧両エンジンにかけ、判定・
    score・通知差分に加えて、coverage・ハードゲート・主な加点/減点理由を
    1銘柄ごとに表示する。mode=shadowで運用しているときに、本稼働へ切り替えて
    よいかを判断する材料として使う。
    """
    stock_codes = resolve_target_stock_codes(stock_code)
    if not stock_codes:
        typer.echo("対象銘柄がありません(--stock-codeを指定するか、保有銘柄を登録してください)。")
        raise typer.Exit(code=1)

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = (
        build_real_provider_bundle(now, config)
        if source == "real"
        else build_mock_provider_bundle(now)
    )
    rows = run_compare(stock_codes, providers, config, now)

    _print_compare_rows(rows)

    if csv_path is not None:
        write_compare_csv(rows, csv_path)
        typer.echo(f"CSVへ出力しました: {csv_path} ({len(rows)}件)")


# --- 個別購入理由(CustomThesisCondition) -------------------------------------


@app.command("register-thesis-condition")
def register_thesis_condition(
    stock_code: str = typer.Argument(...),
    description: str = typer.Option(..., "--description"),
) -> None:
    """銘柄固有の個別購入理由を登録する(現状holding_id=stock_codeのエイリアス)。"""
    service = InvestmentThesisService()
    thesis = service.register_condition(stock_code, stock_code, description)
    new_condition = thesis.conditions[-1]
    typer.echo(f"登録しました: condition_id={new_condition.condition_id} 「{description}」")


@app.command("attest-thesis-condition")
def attest_thesis_condition(
    stock_code: str = typer.Argument(...),
    condition_id: str = typer.Argument(...),
    status: ThesisConditionAttestationStatus = typer.Argument(...),
    attested_by: str = typer.Option(..., "--attested-by"),
) -> None:
    """個別購入理由の維持状況を人間が申告する(自由記述の自動解釈は行わない)。"""
    service = InvestmentThesisService()
    try:
        service.attest_condition(stock_code, condition_id, status, attested_by)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"申告しました: {condition_id} -> {status.value}")


@app.command("show-thesis")
def show_thesis(stock_code: str = typer.Argument(...)) -> None:
    service = InvestmentThesisService()
    thesis = service.get_thesis(stock_code)
    if thesis is None or not thesis.conditions:
        typer.echo(f"{stock_code}: 登録済みの個別購入理由はありません。")
        return
    for c in thesis.conditions:
        attestation = (
            f"{c.last_attestation.status.value}({c.last_attestation.attested_at})"
            if c.last_attestation is not None
            else "未申告"
        )
        typer.echo(f"{c.condition_id}: {c.description} [{attestation}]")
