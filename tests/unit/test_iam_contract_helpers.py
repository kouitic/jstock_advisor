"""tests/support/iam_contract_helpers.pyの回帰テスト(Issue #663 PR-1 T5)。

新しいcontract testがこの共有helperを使って書けること、かつ既存5つの
test_infra_*.pyファイルがそれぞれ独自に実装しているinline版と同じ結果を
返すことを固定する。既存5ファイル自体は変更していない(import-timeの
副作用を避けるため、既存ファイルをmoduleとしてimportするのではなく、
同じinlineロジックをこのテスト内に再現して比較する)。

テンプレートの静的解析のみを行う。AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.support.iam_contract_helpers import load_template, resources

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"


def _inline_load_template() -> dict[str, Any]:
    """既存5ファイルが個別に持つ_load_template()と同一のロジック
    (T5比較対象。既存ファイルからのコピーではなく、独立に再現する)。
    """

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def test_load_template_matches_existing_inline_implementation() -> None:
    """T5: 共有helperのload_template()が、既存5ファイルのinline実装と
    同じtop-level構造を返すこと。

    注意: トップレベルkeyの一致のみを比較し、dict全体の深い`==`比較は
    行わない。`Conditions`/`Rules`配下には`!Equals`/`!If`等にネストされた
    `!Ref`等が、multi_constructorによる変換を受けずPyYAMLの生Nodeオブジェクト
    のまま残る(Nodeは値ではなく同一性で比較されるため、2回読み込んだ
    結果同士がstructurally同じでも`==`では一致しない)。これは今回の
    抽出で新しく持ち込んだ制約ではなく、既存5ファイルが共有する
    `_load_template()`実装が元々持っていた挙動であり、`resources()`配下
    (IAM Policy等、実際に全5ファイルが参照する部分)には影響しない
    (下のtest_resources_matches_existing_inline_implementationで
    深い比較を行い、問題が無いことを確認している)。
    """
    assert load_template().keys() == _inline_load_template().keys()


def test_resources_matches_existing_inline_implementation() -> None:
    """T5: 共有helperのresources()が、既存5ファイルのinline実装
    (_load_template()["Resources"])と同じ内容を返すこと。
    """
    assert resources() == _inline_load_template()["Resources"]


def test_resources_contains_known_lambda_functions() -> None:
    """新規contract testがこのhelperを使って実際にLambda定義へ到達できること
    (AC1の最小確認。#529の対象であるWatchlistBatchReconcilerFunctionを例に取る)。
    """
    res = resources()
    assert "WatchlistBatchReconcilerFunction" in res
    assert res["WatchlistBatchReconcilerFunction"]["Type"] == "AWS::Serverless::Function"
