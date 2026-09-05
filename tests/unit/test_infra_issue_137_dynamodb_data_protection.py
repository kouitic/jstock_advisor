"""Issue #137: DynamoDB の復旧能力(PITR / 削除保護 / Retain)の回帰テスト。

## 何を守るテストか

Production の DynamoDB には、失うと再生成できない利用者所有データ(保有・購入
ロット・取引など)がある。Phase A の実測時点では、PITR・deletion protection・
backup・CloudFormation の Retain ポリシーが **いずれも 1 つも設定されていなかった**。
つまり誤削除・誤更新・stack 削除のいずれからも復旧できない状態だった。

Phase B では、Phase A のデータ分類にもとづき保護対象を限定して設定を入れた。
本テストが固定するのは次の 4 点である。

1. 保護対象へ 4 つの設定がすべて揃っていること
   (PITR / DeletionProtectionEnabled / DeletionPolicy / UpdateReplacePolicy)
2. 対象外(cache・一時状態)へ誤って保護を付けていないこと
   cache は外部から再取得でき、一時状態(ロック・claim・進捗)は復元すると
   古い状態が復活して二次障害を起こすため、保護対象にしてはならない
3. すべての DynamoDB リソースが「保護対象」か「対象外」のどちらか一方に
   ちょうど属すること(新テーブル追加時の分類漏れを検出する)
4. 保護対象・対象外の件数が Phase A の分類結果と一致すること

テンプレートの静的検証のみで、AWS へのアクセスは行わない。

## 新しいテーブルを追加するとき

`_UNPROTECTED_RESOURCES` へ足すか、保護設定を付けるかのどちらかが必要になる。
どちらもしなければ本テストが落ちる。これは意図した挙動であり、
「再生成できるか」を必ず一度考えてから追加させるための guardrail である。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_DYNAMODB_TYPE = "AWS::DynamoDB::Table"

#: 保護対象外。Phase A 分類の CACHE(外部から再取得可能)と
#: TEMPORARY(ロック・claim・進捗・VALIDATION 専用)のみをここへ列挙する。
#: 業務データを誤ってここへ入れると復旧できなくなるため、追加時は
#: 「失っても再取得・再生成できるか」を必ず確認すること。
_UNPROTECTED_RESOURCES = frozenset(
    {
        # CACHE — 外部 provider から再取得できる
        "EdinetDailyDocumentListCacheTable",
        "EdinetDisclosureCacheTable",
        "EdinetFilingCacheTable",
        "WatchlistFinancialCacheTable",
        "WatchlistPriceCacheTable",
        # TEMPORARY — 復元すると古い状態が復活して二次障害を起こす
        "BuyCandidateBatchCompletionTable",
        "ConversationStatesTable",
        "DailyNotificationPriorityTable",
        "NotificationClaimsTable",
        "TradeDetectionRunLockTable",
        "WatchlistCandidateProgressTable",
        "WatchlistRotationDispatchLeaseTable",
        # TEMPORARY — VALIDATION 実行専用
        "ValidationDailyNotificationPriorityTable",
        "ValidationHoldingsSnapshotTable",
        "ValidationHoldingsSnapshotTableV2",
        "ValidationRecommendationsTable",
        "ValidationWatchStateTable",
    }
)

#: Phase A のデータ分類にもとづく件数。分類が変わったら意図的に更新する。
_EXPECTED_PROTECTED_COUNT = 37
_EXPECTED_UNPROTECTED_COUNT = 17


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _dynamodb_resources() -> dict[str, dict[str, Any]]:
    resources = _load_template()["Resources"]
    return {
        name: body
        for name, body in resources.items()
        if body.get("Type") == _DYNAMODB_TYPE
    }


def _protected_names() -> list[str]:
    return [n for n in _dynamodb_resources() if n not in _UNPROTECTED_RESOURCES]


def _pitr_enabled(body: dict[str, Any]) -> bool:
    spec = (body.get("Properties") or {}).get("PointInTimeRecoverySpecification") or {}
    return spec.get("PointInTimeRecoveryEnabled") is True


# --- 3 / F: 分類の網羅と件数 --------------------------------------------------


def test_every_dynamodb_resource_is_classified_exactly_once() -> None:
    """新しいテーブルを分類せずに追加すると落ちる（分類漏れの検出）。"""
    resources = _dynamodb_resources()

    unknown = _UNPROTECTED_RESOURCES - resources.keys()
    assert not unknown, f"対象外リストに存在しないリソースがある: {sorted(unknown)}"

    protected = set(_protected_names())
    unprotected = _UNPROTECTED_RESOURCES & resources.keys()
    assert protected | unprotected == resources.keys()
    assert not (protected & unprotected)


def test_protection_target_counts_match_the_phase_a_classification() -> None:
    resources = _dynamodb_resources()

    assert len(_protected_names()) == _EXPECTED_PROTECTED_COUNT
    assert len(_UNPROTECTED_RESOURCES & resources.keys()) == _EXPECTED_UNPROTECTED_COUNT


# --- A: PITR ------------------------------------------------------------------


def test_protected_tables_enable_point_in_time_recovery() -> None:
    """PITR が無いと、誤更新・誤削除のあとに時点復元できない。"""
    resources = _dynamodb_resources()

    missing = [name for name in _protected_names() if not _pitr_enabled(resources[name])]
    assert missing == [], f"PITR が無効な保護対象: {sorted(missing)}"


# --- B: deletion protection ---------------------------------------------------


def test_protected_tables_enable_deletion_protection() -> None:
    """DeleteTable そのものを止める。PITR とは守る対象が異なる。"""
    resources = _dynamodb_resources()

    missing = [
        name
        for name in _protected_names()
        if (resources[name].get("Properties") or {}).get("DeletionProtectionEnabled") is not True
    ]
    assert missing == [], f"削除保護が無効な保護対象: {sorted(missing)}"


# --- C / D: CloudFormation の Retain ------------------------------------------


def test_protected_tables_retain_on_stack_delete_and_replacement() -> None:
    """stack 削除・置換で実体を消さない。deletion protection とは別の契機に効く。"""
    resources = _dynamodb_resources()

    for name in _protected_names():
        body = resources[name]
        assert body.get("DeletionPolicy") == "Retain", f"{name} に DeletionPolicy: Retain が無い"
        assert body.get("UpdateReplacePolicy") == "Retain", (
            f"{name} に UpdateReplacePolicy: Retain が無い"
        )


# --- E: 対象外へ誤って付けていないこと ----------------------------------------


def test_cache_and_temporary_tables_are_not_protected() -> None:
    """cache は再取得でき、一時状態は復元すると二次障害を起こすため保護しない。"""
    resources = _dynamodb_resources()

    for name in sorted(_UNPROTECTED_RESOURCES & resources.keys()):
        body = resources[name]
        properties = body.get("Properties") or {}

        assert not _pitr_enabled(body), f"{name} は PITR 対象外のはず"
        assert properties.get("DeletionProtectionEnabled") is not True, (
            f"{name} は削除保護の対象外のはず"
        )
        assert body.get("DeletionPolicy") != "Retain", f"{name} に Retain は不要"
        assert body.get("UpdateReplacePolicy") != "Retain", f"{name} に Retain は不要"
