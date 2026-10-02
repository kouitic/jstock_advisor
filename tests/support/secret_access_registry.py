"""github-app secret へ実際にアクセスする runtime 関数の、共有の期待集合(Issue #696)。

## なぜこのモジュールが要るか

github-app secret(`GithubAppSecretArn`)の最小権限は、2 つの層で独立に固定されている。

    UNIT1  identity policy 層   tests/unit/test_infra_issue_133_secretsmanager_least_privilege.py
    UNIT2  resource policy 層   tests/unit/test_issue_680_secret_resource_policy.py

どちらも「実際に `secretsmanager:GetSecretValue` を行う関数」を同じ 2 件として、それぞれ
独立にハードコードしていた。一方だけを更新しても他方のテストは気づけず、#680 の toggle
(`SecretResourcePolicyEnabled`)が有効になった後に drift すると、新しい Lambda が
secretsmanager の AccessDenied になり日次バッチが止まりうる。

## 置いてあるもの

    SECRETSMANAGER_RUNTIME_FUNCTIONS  期待集合の定義(関数の論理 ID)。ここ 1 箇所だけを編集する
    runtime_role_logical_id()         SAM が `Policies` から暗黙に作る実行 role の論理 ID の規約

UNIT1 / UNIT2 はこの定義から期待値を導出する。さらに
tests/unit/test_issue_696_secret_allowlist_drift.py が、この registry に依存せず、
template の identity policy と resource policy を直接突き合わせる(registry を更新し忘れても、
2 つの層が食い違えば落ちる)。

## 変えていないこと

期待集合の値(IncidentNotifierFunction / WeeklyReviewFunction の 2 件)は従来と同一である。
infra/template.yaml・IAM policy・resource policy の内容・セキュリティ方針は変更していない。
"""

from __future__ import annotations

# github-app secret を実行時に `secretsmanager:GetSecretValue` で取得する関数(論理 ID)。
# 新しい関数をここへ足す場合は、template の identity policy(その関数の Policies)と、
# resource policy(GithubAppSecretResourcePolicy)の allow-list の両方を、意図して更新すること。
# 片方だけを更新すると、UNIT1 / UNIT2 / test_issue_696 のいずれかが落ちる。
SECRETSMANAGER_RUNTIME_FUNCTIONS: tuple[str, ...] = (
    "IncidentNotifierFunction",
    "WeeklyReviewFunction",
)


def runtime_role_logical_id(function_logical_id: str) -> str:
    """SAM が `Policies` から暗黙に生成する、関数の実行 role の論理 ID。

    規約は「関数の論理 ID + "Role"」で、template の `!GetAtt IncidentNotifierFunctionRole.Arn` 等と
    一致する(その一致は test_issue_696 が template 全体の参照から確認する)。
    """
    return f"{function_logical_id}Role"
