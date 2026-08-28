"""NotificationLogテーブルへのGSI/TTL用index属性のbackfill・移行検証スクリプト(Issue #32)。

既存のNotificationLog item(Phase Aのコードデプロイ以前に書き込まれたもの)は
トップレベルのindex属性(nl_stock_type_key / nl_holding_type_key / nl_sent_sort /
nl_expires_at)を持たないため、GSI作成後もインデックスへ載らない。このスクリプトは
既存itemへ同属性を付与する(data JSON本体は一切変更しない)。

キー生成は通常save経路と同じpure関数
(notification_log_repository.build_index_attributes)を共有しており、
backfillと通常writeのロジックがdriftしない。

モード(既定はdry-run。--executeを明示しない限り一切書き込まない):
  dry-run(既定): 対象件数・更新予定属性の集計を表示するだけ。write無し。
  --execute:      属性を実際に書き込む。誤爆防止のため--confirm-tableに
                  --tableと完全一致するテーブル名を再入力すること。
  --verify:       read-onlyの移行検証。属性coverage・TTL除外・parse不能itemを
                  検査し、対象GSIが既に存在する場合のみ「GSI Queryのlatest ==
                  Scan由来のlatest」の完全一致(latest等価性)も検査する
                  (phase-aware: GSI未作成の段階では等価性検査をskipと明示し、
                  failureとは扱わない)。

終了コード: 0=成功 / 1=失敗(parse不能item検出・coverage不足・latest等価性
不一致・引数不備)。parse不能itemが1件でも存在する場合、他のitemの処理自体は
継続するが、終了コードは必ず1とし「migration完了」とは見なさない。

冪等・再実行可能: 既に正しい属性を持つitemはskipされるため、何度実行しても
結果は同じ(2回目以降のexecuteは更新0件になる)。

本番実行は必ず人間が明示承認のうえ行うこと(dry-run→execute→verifyの順)。
手順はdocs/operations_manual.md 15節参照。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    EXPIRES_AT_ATTRIBUTE,
    HOLDING_SCOPE_INDEX_NAME,
    HOLDING_SCOPE_KEY_ATTRIBUTE,
    SENT_SORT_ATTRIBUTE,
    STOCK_SCOPE_INDEX_NAME,
    STOCK_SCOPE_KEY_ATTRIBUTE,
    build_index_attributes,
    build_sent_sort_value,
)

_INDEX_ATTRIBUTE_NAMES = (
    STOCK_SCOPE_KEY_ATTRIBUTE,
    HOLDING_SCOPE_KEY_ATTRIBUTE,
    SENT_SORT_ATTRIBUTE,
    EXPIRES_AT_ATTRIBUTE,
)


@dataclass
class ItemPlan:
    notification_id: str
    attributes_to_set: dict[str, str | int]
    attributes_to_remove: list[str]

    @property
    def needs_update(self) -> bool:
        return bool(self.attributes_to_set) or bool(self.attributes_to_remove)


@dataclass
class ScanResult:
    plans: list[ItemPlan] = field(default_factory=list)
    parsed_logs: list[NotificationLog] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


def _normalize_existing_value(value: Any) -> Any:
    """boto3 resource layerはN属性をDecimalで返すため、intへ正規化して比較する。"""
    if isinstance(value, Decimal):
        return int(value)
    return value


def _plan_for_item(raw_item: dict[str, Any]) -> tuple[ItemPlan, NotificationLog]:
    log = NotificationLog.model_validate_json(raw_item["data"])
    desired = build_index_attributes(log)
    to_set = {
        name: value
        for name, value in desired.items()
        if _normalize_existing_value(raw_item.get(name)) != value
    }
    # 期待属性集合に含まれない既存index属性は削除対象(例: ATTENTIONへ誤って
    # nl_expires_atが付与されていた場合)。冪等な収束のためexecuteで取り除く。
    to_remove = [
        name for name in _INDEX_ATTRIBUTE_NAMES if name not in desired and name in raw_item
    ]
    return ItemPlan(log.notification_id, to_set, to_remove), log


def _scan_and_plan(table: Any) -> ScanResult:
    result = ScanResult()
    scan_kwargs: dict[str, Any] = {}
    while True:
        response = table.scan(**scan_kwargs)
        for raw_item in response.get("Items", []):
            item_id = str(raw_item.get("notification_id", "<missing notification_id>"))
            try:
                plan, log = _plan_for_item(raw_item)
            except Exception as exc:  # noqa: BLE001 - 不正itemを列挙して継続する
                result.parse_errors.append(f"{item_id}: {type(exc).__name__}: {exc}")
                continue
            result.plans.append(plan)
            result.parsed_logs.append(log)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return result


def _print_summary(result: ScanResult) -> None:
    total = len(result.plans) + len(result.parse_errors)
    to_update = [plan for plan in result.plans if plan.needs_update]
    set_counts: dict[str, int] = {name: 0 for name in _INDEX_ATTRIBUTE_NAMES}
    remove_counts: dict[str, int] = {name: 0 for name in _INDEX_ATTRIBUTE_NAMES}
    for plan in to_update:
        for name in plan.attributes_to_set:
            set_counts[name] += 1
        for name in plan.attributes_to_remove:
            remove_counts[name] += 1
    print(f"対象item総数: {total}")
    print(f"  parse成功: {len(result.plans)} / parse不能: {len(result.parse_errors)}")
    print(f"  更新が必要: {len(to_update)} / 既に正しくskip: {len(result.plans) - len(to_update)}")
    print("  更新予定属性(SET)の内訳:")
    for name in _INDEX_ATTRIBUTE_NAMES:
        print(f"    {name}: {set_counts[name]}")
    removals = {name: count for name, count in remove_counts.items() if count}
    if removals:
        print(f"  削除予定属性(REMOVE)の内訳: {removals}")
    if result.parse_errors:
        print("  parse不能item(このスクリプトでは変更しない。個別に原因を確認すること):")
        for line in result.parse_errors:
            print(f"    {line}")


def _execute_plans(table: Any, plans: list[ItemPlan]) -> int:
    updated = 0
    for plan in plans:
        if not plan.needs_update:
            continue
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        set_parts: list[str] = []
        for i, (name, value) in enumerate(sorted(plan.attributes_to_set.items())):
            names[f"#s{i}"] = name
            values[f":s{i}"] = value
            set_parts.append(f"#s{i} = :s{i}")
        remove_parts: list[str] = []
        for i, name in enumerate(sorted(plan.attributes_to_remove)):
            names[f"#r{i}"] = name
            remove_parts.append(f"#r{i}")
        expression = ""
        if set_parts:
            expression += "SET " + ", ".join(set_parts)
        if remove_parts:
            expression += (" " if expression else "") + "REMOVE " + ", ".join(remove_parts)
        names["#pk"] = "notification_id"
        update_kwargs: dict[str, Any] = {
            "Key": {"notification_id": plan.notification_id},
            "UpdateExpression": expression,
            # data JSON本体には一切触れない。存在しないitemを誤って新規作成しない
            # よう、item存在を条件にする。
            "ConditionExpression": "attribute_exists(#pk)",
            "ExpressionAttributeNames": names,
        }
        if values:
            update_kwargs["ExpressionAttributeValues"] = values
        table.update_item(**update_kwargs)
        updated += 1
    return updated


def _existing_index_names(table: Any) -> set[str]:
    description = table.meta.client.describe_table(TableName=table.table_name)
    indexes = description["Table"].get("GlobalSecondaryIndexes") or []
    return {gsi["IndexName"] for gsi in indexes}


def _verify_coverage(result: ScanResult) -> list[str]:
    violations: list[str] = []
    for plan in result.plans:
        if plan.needs_update:
            detail_parts = []
            if plan.attributes_to_set:
                detail_parts.append(f"不足/不一致: {sorted(plan.attributes_to_set)}")
            if plan.attributes_to_remove:
                detail_parts.append(f"余分な属性: {plan.attributes_to_remove}")
            violations.append(f"{plan.notification_id}: {'; '.join(detail_parts)}")
    return violations


def _verify_latest_equivalence(
    table: Any, result: ScanResult, key_attribute: str, index_name: str
) -> list[str]:
    """テーブル内の全distinct scope keyについて、GSI Query(降順Limit=1)の結果と
    Scan(全件+Python比較)由来のlatestが一致することを確認する。"""
    from boto3.dynamodb.conditions import Key

    expected_latest: dict[str, tuple[str, str]] = {}
    for log in result.parsed_logs:
        desired = build_index_attributes(log)
        scope_key = desired.get(key_attribute)
        if not isinstance(scope_key, str):
            continue
        sort_value = build_sent_sort_value(log.sent_at, log.notification_id)
        current = expected_latest.get(scope_key)
        if current is None or sort_value > current[0]:
            expected_latest[scope_key] = (sort_value, log.notification_id)
    mismatches: list[str] = []
    for scope_key, (_, expected_id) in sorted(expected_latest.items()):
        response = table.query(
            IndexName=index_name,
            KeyConditionExpression=Key(key_attribute).eq(scope_key),
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        actual_id = str(items[0]["notification_id"]) if items else None
        if actual_id != expected_id:
            mismatches.append(f"{index_name} / {scope_key}: GSI={actual_id} Scan={expected_id}")
    print(f"  {index_name}: {len(expected_latest)} scope keyのlatest等価性を検証")
    return mismatches


def _run_verify(table: Any, result: ScanResult) -> int:
    failed = False
    coverage_violations = _verify_coverage(result)
    if coverage_violations:
        failed = True
        print(f"coverage違反: {len(coverage_violations)}件")
        for line in coverage_violations:
            print(f"  {line}")
    else:
        print("coverage: OK(全itemが期待どおりのindex/TTL属性を保持)")
    if result.parse_errors:
        failed = True
        print(f"parse不能item: {len(result.parse_errors)}件(上記一覧参照)")
    existing_indexes = _existing_index_names(table)
    for key_attribute, index_name in (
        (STOCK_SCOPE_KEY_ATTRIBUTE, STOCK_SCOPE_INDEX_NAME),
        (HOLDING_SCOPE_KEY_ATTRIBUTE, HOLDING_SCOPE_INDEX_NAME),
    ):
        if index_name not in existing_indexes:
            print(f"  {index_name}: 未作成のためlatest等価性検証をskip(このphaseでは正常)")
            continue
        mismatches = _verify_latest_equivalence(table, result, key_attribute, index_name)
        if mismatches:
            failed = True
            print(f"latest等価性の不一致: {len(mismatches)}件")
            for line in mismatches:
                print(f"  {line}")
    print("verify結果: " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


def main(argv: list[str] | None = None, dynamodb_resource: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table", required=True, help="対象テーブル名(例: jstock-notification_log)"
    )
    parser.add_argument("--region", default="ap-northeast-1", help="AWSリージョン")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="実際に属性を書き込む(既定はdry-run)")
    mode.add_argument("--verify", action="store_true", help="read-onlyの移行検証を行う")
    parser.add_argument(
        "--confirm-table",
        default=None,
        help="--execute時の誤爆防止。--tableと完全一致するテーブル名を再入力する",
    )
    args = parser.parse_args(argv)

    if args.execute and args.confirm_table != args.table:
        print(
            "--executeには--confirm-tableで対象テーブル名の再入力が必要です "
            f"(--table {args.table} と完全一致させること)。書き込みは行っていません。"
        )
        return 1

    if dynamodb_resource is None:
        import boto3

        dynamodb_resource = boto3.resource("dynamodb", region_name=args.region)
    table = dynamodb_resource.Table(args.table)

    result = _scan_and_plan(table)
    _print_summary(result)

    if args.verify:
        return _run_verify(table, result)

    if args.execute:
        updated = _execute_plans(table, result.plans)
        print(f"更新実行: {updated}件(data JSON本体は変更していない)")
    else:
        print(
            "dry-runのため書き込みは行っていません"
            "(実行するには--execute --confirm-table <table>)。"
        )

    if result.parse_errors:
        print("parse不能itemが存在するため、migration完了とは見なせません(exit 1)。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
