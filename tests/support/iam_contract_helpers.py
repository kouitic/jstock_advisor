"""infra/template.yamlをIAM contract testから読むための共有YAML解析helper
(Issue #663 PR-1)。

## なぜこのmoduleが要るか

`test_infra_issue_503_incident_notifier_iam.py` /
`test_infra_issue_507_reconciler_metrics_iam.py` /
`test_infra_issue_529_trade_event_reconciliation_iam.py` /
`test_infra_issue_557_topic_policy_sid.py` /
`test_infra_issue_559_sqs_policy_sid_contract.py`は、いずれも独立に
CloudFormation intrinsic function(`!Ref`/`!GetAtt`/`!Sub`等)を含む
`infra/template.yaml`をYAML構造として読み込むための`_load_template()`/
`_resources()`を再実装している(5ファイルで実質同一コードが重複。
Issue #663 Root Cause調査で判明したDRY違反)。

## この抽出で変えていないこと

既存5ファイルの`_load_template()`/`_resources()`から**識別子・ロジックを
1行も変えずに**移動した(`tests/support/time_semantics_registry.py`の
抽出〔Issue #277〕と同じ方針)。既存5ファイル自体は変更していない
(退行リスクを最小化するため。新helperへの移行は任意のfollow-up。
Issue #663 JIRO設計コメント参照)。新規にcontract testを書く場合、
このmoduleを使うことでYAML解析部分の再実装を避けられる。

テンプレートの静的解析のみを行う。AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"


def load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def resources() -> dict[str, Any]:
    return load_template()["Resources"]
