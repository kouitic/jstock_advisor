"""保有判断スコア方式のランタイムConfig・個別購入理由CLIコマンド(実装プラン)。

set-mode/kill-switch等はRuntimeConfig(DynamoDB専用テーブル)を直接更新し、
再デプロイ不要で反映される。ローカル実行時は既定でローカルJSONストアを操作するが、
`--target aws`を指定すると本番DynamoDBを直接操作する
(AWS認証情報が環境に設定済みであることが前提)。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    FinancialPolicyOverride,
    RuntimeConfigMode,
    ThesisConditionAttestationStatus,
)
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
    RuntimeConfigAlreadyInitializedError,
)
from jstock_advisor.infrastructure.local_repository.holding_decision_runtime_config_repository import (
    RuntimeConfigConflictError,
)
from jstock_advisor.infrastructure.aws.baseline_pointer import BaselinePointerConflictError
from jstock_advisor.services.investment_thesis_service import (
    BaselineActivationExhaustedError,
    InvestmentThesisService,
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
        except (RuntimeConfigConflictError, BaselinePointerConflictError) as e:
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
    """新旧どちらの売却系LINE通知も停止する緊急スイッチ(modeとは独立)。"""
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
        except RuntimeConfigConflictError as e:
            typer.echo(f"他の更新が先に行われたため再確認が必要です: {e}")
            raise typer.Exit(code=1) from e
    typer.echo(f"kill switchを{state}にしました(notification_enabled={updated.notification_enabled})")


@app.command("show-runtime-config")
def show_runtime_config(target: str = typer.Option("local", "--target", help="local | aws")) -> None:
    with _target_backend(target):
        service = HoldingDecisionRuntimeConfigService(cache_ttl_seconds=_cache_ttl_seconds())
        lookup = service.get_config()
    if lookup.is_fallback:
        typer.echo("RuntimeConfigは未初期化です(フォールバック既定値: mode=legacy, notification無効)。")
        return
    cfg = lookup.config
    typer.echo(
        f"mode={cfg.mode.value} notification_enabled={cfg.notification_enabled} "
        f"financial_policy_override={cfg.financial_policy_override.value} "
        f"config_version={cfg.config_version} updated_by={cfg.updated_by} "
        f"updated_at={cfg.updated_at} change_reason={cfg.change_reason}"
    )


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
