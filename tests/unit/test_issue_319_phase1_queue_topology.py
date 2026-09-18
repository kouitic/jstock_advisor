"""infra/template.yamlのSQS topology不変条件(Issue #319 Phase 1)。

PR #396の独立レビューで、追加した6件のSQS/DLQリソースを既存のinfra系146件の
テストが一切guardしていないことが判明した。代表mutation(VisibilityTimeout誤値・
RedrivePolicy削除・QueueName衝突・既存Lambdaへの早期SQS接続)を入れても
既存テストは全てpassのままであり、「Phase 1の主要契約を壊してもCIがgreen」
という状態だった。本モジュールはこれを塞ぐ。

★ 本モジュールが固定するのは**Phase 1境界を含む**。Phase 1は「Queue/DLQを
  作るだけで、既存Lambda実行経路へ一切接続しない」設計であり、
  test_phase1_buy_queue_is_not_connected_to_lambda /
  test_phase1_holdings_queue_is_not_connected_to_lambda は**Phase 1限定**の
  制約である。Phase 2(dispatch側のSQS接続実装)着手時には、この2テストを
  「正しいQueueだけが正しいworkerへ接続される」という**Phase 2契約**へ
  更新すること(CIを赤いまま放置しない)。

★ 本モジュールはYAMLの構造を読むだけである。CloudFormation/SAMの構文検証
  ではない(構文・SAMのtransform結果はsam validate --lintで別途確認する)。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_AWS_STACK_NAME_PLACEHOLDER = "${AWS::StackName}"


class _CfnLoader(yaml.SafeLoader):
    """CloudFormationの短縮形組み込み関数を素朴なdictへ変換する構文解析専用Loader。"""


def _cfn_multi_constructor(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    assert isinstance(node, yaml.MappingNode)  # noqa: S101 - CFNタグはこの3種のみ
    return {tag_suffix: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)  # type: ignore[no-untyped-call]


def _load_template() -> dict[str, Any]:
    loaded = yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CfnLoader)
    assert isinstance(loaded, dict)  # noqa: S101 - templateはmapping
    return loaded


def _resources() -> dict[str, Any]:
    return dict(_load_template()["Resources"])


def _references_logical_id(node: Any, logical_id: str) -> bool:
    """部分木の中に、指定した論理IDへのRef/GetAtt/Subによる参照が1つでもあるか。

    Phase 1では「QueueがLambdaから一切参照されない」ことを固定したいが、
    参照の現れ方は複数ある(Events配下のSQSトリガー、Environment変数への注入、
    PoliciesのSQSSendMessagePolicy/Statement.Resource)。個別に文字列一致を
    書くと書き漏れが起きるため、構造を再帰的に辿って網羅する。
    """
    if isinstance(node, dict):
        if node.get("Ref") == logical_id:
            return True
        get_att = node.get("GetAtt")
        if isinstance(get_att, str) and get_att.split(".")[0] == logical_id:
            return True
        if isinstance(get_att, list) and get_att and get_att[0] == logical_id:
            return True
        sub = node.get("Sub")
        if sub is not None:
            sub_template = sub[0] if isinstance(sub, list) else sub
            if isinstance(sub_template, str) and re.search(
                rf"\$\{{{re.escape(logical_id)}(\.[A-Za-z]+)?\}}", sub_template
            ):
                return True
        return any(_references_logical_id(v, logical_id) for v in node.values())
    if isinstance(node, list):
        return any(_references_logical_id(v, logical_id) for v in node)
    return False


# --- Phase 1で追加した3段構成×2系統(#319 Phase A設計S-1) -----------------------

_MAIN_QUEUE_CONTRACTS: dict[str, dict[str, Any]] = {
    "BuyCandidateQueue": {
        "queue_name_sub": f"{_AWS_STACK_NAME_PLACEHOLDER}-buy-candidate-queue",
        "visibility_timeout": 5400,
        "redrive_target": "BuyCandidateTerminalFailureQueue",
        "max_receive_count": 3,
    },
    "HoldingsWatchlistQueue": {
        "queue_name_sub": f"{_AWS_STACK_NAME_PLACEHOLDER}-holdings-watchlist-queue",
        "visibility_timeout": 5400,
        "redrive_target": "HoldingsWatchlistTerminalFailureQueue",
        "max_receive_count": 3,
    },
}

_TERMINAL_FAILURE_QUEUE_CONTRACTS: dict[str, dict[str, Any]] = {
    "BuyCandidateTerminalFailureQueue": {
        "queue_name_sub": f"{_AWS_STACK_NAME_PLACEHOLDER}-buy-candidate-terminal-failure",
        "visibility_timeout": 120,
        "redrive_target": "BuyCandidateTerminalFailureDLQ",
        "max_receive_count": 3,
    },
    "HoldingsWatchlistTerminalFailureQueue": {
        "queue_name_sub": f"{_AWS_STACK_NAME_PLACEHOLDER}-holdings-watchlist-terminal-failure",
        "visibility_timeout": 120,
        "redrive_target": "HoldingsWatchlistTerminalFailureDLQ",
        "max_receive_count": 3,
    },
}

_DLQ_CONTRACTS: dict[str, dict[str, Any]] = {
    "BuyCandidateTerminalFailureDLQ": {
        "queue_name_sub": f"{_AWS_STACK_NAME_PLACEHOLDER}-buy-candidate-terminal-failure-dlq",
        "message_retention_period": 1209600,
    },
    "HoldingsWatchlistTerminalFailureDLQ": {
        "queue_name_sub": f"{_AWS_STACK_NAME_PLACEHOLDER}-holdings-watchlist-terminal-failure-dlq",
        "message_retention_period": 1209600,
    },
}

_ALL_NEW_QUEUE_NAMES = (
    tuple(_MAIN_QUEUE_CONTRACTS)
    + tuple(_TERMINAL_FAILURE_QUEUE_CONTRACTS)
    + tuple(_DLQ_CONTRACTS)
)

# (関数の論理ID, 未接続であるべきPhase 1メインQueueの論理ID)
_PHASE1_FUNCTION_QUEUE_PAIRS = (
    ("BuyCandidatesFunction", "BuyCandidateQueue"),
    ("HoldingsWatchlistFunction", "HoldingsWatchlistQueue"),
)

_PHASE1_UNUSED_PARAMETER_DEFAULTS: dict[str, int] = {
    "BuyCandidateReservedConcurrentExecutions": 50,
    "BuyCandidateSqsBatchSize": 1,
    "HoldingsWatchlistReservedConcurrentExecutions": 50,
    "HoldingsWatchlistSqsBatchSize": 1,
}


def test_all_new_phase1_queues_exist() -> None:
    """母集団そのものが空になっていないこと(テストが素通りするのを防ぐ)。"""
    resources = _resources()
    missing = [name for name in _ALL_NEW_QUEUE_NAMES if name not in resources]
    assert not missing, f"#319 Phase 1で追加したはずのQueue/DLQが見つからない: {missing}"


@pytest.mark.parametrize("logical_id", sorted(_MAIN_QUEUE_CONTRACTS))
def test_main_queue_contract(logical_id: str) -> None:
    """メインQueue(BuyCandidateQueue/HoldingsWatchlistQueue)の契約を固定する。

    VisibilityTimeout=5400(対象Lambda Timeout 900秒の6倍、Watchlistと同じ比率)、
    RedrivePolicyの行き先とmaxReceiveCount=3。
    """
    contract = _MAIN_QUEUE_CONTRACTS[logical_id]
    props = _resources()[logical_id]["Properties"]
    assert _resources()[logical_id]["Type"] == "AWS::SQS::Queue"
    assert props["VisibilityTimeout"] == contract["visibility_timeout"], (
        f"{logical_id}.VisibilityTimeoutが{contract['visibility_timeout']}でない"
    )
    redrive = props["RedrivePolicy"]
    assert redrive["deadLetterTargetArn"] == {"GetAtt": f"{contract['redrive_target']}.Arn"}, (
        f"{logical_id}のRedrivePolicy先が{contract['redrive_target']}でない"
    )
    assert redrive["maxReceiveCount"] == contract["max_receive_count"]


@pytest.mark.parametrize("logical_id", sorted(_TERMINAL_FAILURE_QUEUE_CONTRACTS))
def test_terminal_failure_queue_contract(logical_id: str) -> None:
    """terminal failure Queueの契約を固定する。VisibilityTimeout=120、
    RedrivePolicyの行き先とmaxReceiveCount=3。"""
    contract = _TERMINAL_FAILURE_QUEUE_CONTRACTS[logical_id]
    props = _resources()[logical_id]["Properties"]
    assert _resources()[logical_id]["Type"] == "AWS::SQS::Queue"
    assert props["VisibilityTimeout"] == contract["visibility_timeout"], (
        f"{logical_id}.VisibilityTimeoutが{contract['visibility_timeout']}でない"
    )
    redrive = props["RedrivePolicy"]
    assert redrive["deadLetterTargetArn"] == {"GetAtt": f"{contract['redrive_target']}.Arn"}, (
        f"{logical_id}のRedrivePolicy先が{contract['redrive_target']}でない"
    )
    assert redrive["maxReceiveCount"] == contract["max_receive_count"]


@pytest.mark.parametrize("logical_id", sorted(_DLQ_CONTRACTS))
def test_dlq_contract(logical_id: str) -> None:
    """真正DLQの契約を固定する。MessageRetentionPeriod=1209600(14日)。"""
    contract = _DLQ_CONTRACTS[logical_id]
    resource = _resources()[logical_id]
    assert resource["Type"] == "AWS::SQS::Queue"
    assert resource["Properties"]["MessageRetentionPeriod"] == contract["message_retention_period"]


@pytest.mark.parametrize("logical_id", sorted(_ALL_NEW_QUEUE_NAMES))
def test_queue_name_is_pinned(logical_id: str) -> None:
    """QueueNameが既存resource名と衝突しない、意図した値へ固定されていること。

    既存のBuyCandidatesFunction/HoldingsWatchlistFunction(Lambda)の
    FunctionNameと文字列衝突しないよう、明示的に"-queue"接尾辞を付けた
    経緯(#319 Phase 1詳細設計)を含めて固定する。
    """
    contract = (
        _MAIN_QUEUE_CONTRACTS.get(logical_id)
        or _TERMINAL_FAILURE_QUEUE_CONTRACTS.get(logical_id)
        or _DLQ_CONTRACTS[logical_id]
    )
    props = _resources()[logical_id]["Properties"]
    assert props["QueueName"] == {"Sub": contract["queue_name_sub"]}, (
        f"{logical_id}.QueueNameが期待値と異なる(既存resourceとの命名衝突の可能性)"
    )


def test_new_queue_names_are_mutually_distinct() -> None:
    """6件のQueueNameが互いに異なること(コピペによる値の使い回しを検知する)。"""
    names = [
        _resources()[logical_id]["Properties"]["QueueName"]["Sub"]
        for logical_id in _ALL_NEW_QUEUE_NAMES
    ]
    assert len(names) == len(set(names)), f"QueueNameに重複がある: {names}"


# --- Phase 1境界: 既存Lambda実行経路へ一切接続しないこと -------------------------


@pytest.mark.parametrize(("function_id", "queue_id"), _PHASE1_FUNCTION_QUEUE_PAIRS)
def test_phase1_queue_is_not_connected_to_its_lambda(function_id: str, queue_id: str) -> None:
    """[Phase 1限定] 新設Queueが対象Lambdaの実行経路へ一切接続されていないこと。

    Events(SQSトリガー)・Environment変数・Policies(SQSSendMessagePolicy/
    Statement.Resource)のいずれの経路でも、対象Queueへの参照が無いことを
    構造再帰で確認する(個別の文字列一致だと書き漏れが起きるため)。

    ★ Phase 2でdispatch側のSQS接続を実装したら、本テストは
      「正しいQueueだけが正しいworkerへ接続される」というPhase 2契約へ
      更新すること。このテストをそのまま残してCIを赤くしない。
    """
    function_props = _resources()[function_id]["Properties"]
    assert not _references_logical_id(function_props, queue_id), (
        f"{function_id}が{queue_id}を参照している。"
        "Phase 1はQueue/DLQを作るだけで、既存Lambda実行経路へ接続しない設計である"
        "(Phase 2で意図的に接続する場合は本テストをPhase 2契約へ更新すること)"
    )


def test_phase1_no_event_source_mapping_references_new_main_queues() -> None:
    """[Phase 1限定] 新設メインQueueを参照するEventSourceMapping相当resourceが
    まだ存在しないこと。"""
    resources = _resources()
    for logical_id, resource in resources.items():
        if resource.get("Type") != "AWS::Lambda::EventSourceMapping":
            continue
        for queue_id in _MAIN_QUEUE_CONTRACTS:
            assert not _references_logical_id(resource, queue_id), (
                f"{logical_id}(EventSourceMapping)が{queue_id}を参照している。"
                "Phase 1ではまだ存在しないはずである"
            )


# --- Phase 1境界: 新規Parameter 4件は宣言のみで未使用であること -----------------


def test_phase1_parameters_exist_with_expected_defaults() -> None:
    params = _load_template()["Parameters"]
    for name, default in _PHASE1_UNUSED_PARAMETER_DEFAULTS.items():
        assert name in params, f"{name} Parameterが無い"
        assert params[name]["Type"] == "Number"
        assert params[name]["Default"] == default, f"{name}のDefaultが{default}でない"


@pytest.mark.parametrize("param_name", sorted(_PHASE1_UNUSED_PARAMETER_DEFAULTS))
def test_phase1_new_parameter_is_declared_but_unused(param_name: str) -> None:
    """[Phase 1限定] 新規Parameter 4件がどのResourceからも参照されていないこと。

    ★ HoldingsWatchlistReservedConcurrentExecutions=50は最終確定値ではない
      (buy側のnatural verification結果とholdings側の負荷実測を踏まえて
      Production activation直前に再判断する。#319 Phase 1詳細設計コメント)。
      本テストはPhase 1時点で未参照であることのみを固定し、値の妥当性は
      判定しない。
    """
    resources = _resources()
    assert not _references_logical_id(resources, param_name), (
        f"{param_name}がResourcesから参照されている。"
        "Phase 1ではこの4 Parameterは宣言のみで未使用のはずである"
        "(Phase 2でLambdaへ接続する際に更新すること)"
    )
