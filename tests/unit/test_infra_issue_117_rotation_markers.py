"""Issue #117 Phase R1: credential rotation 用の強制再解決マーカーの回帰テスト。

## 何を守るテストか

Secrets Manager の値だけを更新しても、CloudFormation の dynamic reference は
**それを含むリソースに実変更が無ければ再解決されない**。その結果
「Secrets Manager は新しい値、Lambda 環境変数は旧 credential」という状態が
沈黙のまま続く。R1 はこれを防ぐため、秘密と同じ Environment ブロックへ
非秘密のマーカーを置き、ローテーション時に運用者が明示的に値を変えることで
リソース更新を確実に発生させる。

したがって本テストが固定するのは次の 3 点である。

1. マーカーが存在し、安全な既定値を持つこと（既存デプロイを壊さない）
2. マーカーが **秘密の dynamic reference と同じ Environment ブロック** にあること
   （別の場所にあると、更新されても当該 dynamic reference が再解決されない）
3. 秘密の受け渡し自体は R1 で変えていないこと（恒久対策は別 Phase）

テンプレートの静的検証のみで、AWS へのアクセスは行わない。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_LINE_PARAMETER = "LineCredentialRotationVersion"
_EDINET_PARAMETER = "EdinetCredentialRotationVersion"
_LINE_MARKER_ENV = "LINE_CREDENTIAL_ROTATION_VERSION"
_EDINET_MARKER_ENV = "EDINET_CREDENTIAL_ROTATION_VERSION"

#: R1 の時点で dynamic reference のまま維持する秘密（恒久対策は別 Phase）。
_GLOBAL_SECRET_ENV = (
    "EDINET_API_KEY",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_USER_ID",
)
#: R1 のローテーション対象外。専用のマーカーを持たせない。
_WEBHOOK_SECRET_ENV = "LINE_CHANNEL_SECRET"


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _globals_environment() -> dict[str, Any]:
    return _load_template()["Globals"]["Function"]["Environment"]["Variables"]


def _parameters() -> dict[str, Any]:
    return _load_template()["Parameters"]


# --- A / B: パラメータの存在と既定値の契約 -----------------------------------


def test_rotation_version_parameters_exist_as_strings() -> None:
    parameters = _parameters()

    for name in (_LINE_PARAMETER, _EDINET_PARAMETER):
        assert name in parameters, f"{name} が存在しない"
        assert parameters[name]["Type"] == "String"


def test_rotation_version_parameters_have_safe_defaults() -> None:
    """既定値が無いと、既存の samconfig（新パラメータを渡していない）でデプロイが失敗する。"""
    parameters = _parameters()

    for name in (_LINE_PARAMETER, _EDINET_PARAMETER):
        assert "Default" in parameters[name], f"{name} に既定値が無いと既存デプロイを壊す"
        assert parameters[name]["Default"] == "0"


def test_rotation_version_parameters_are_not_secret_typed() -> None:
    """マーカーは環境変数として平文で残るため、秘密を入れる設計にしてはならない。"""
    parameters = _parameters()

    for name in (_LINE_PARAMETER, _EDINET_PARAMETER):
        # NoEcho は「秘密を入れる想定」を意味してしまうため付けない。
        assert parameters[name].get("NoEcho") is not True
        description = str(parameters[name]["Description"])
        assert "秘密値" in description, "秘密を入れてはならない旨を Description で明示する"


# --- C: LINE と EDINET は独立して回せること ----------------------------------


def test_line_and_edinet_markers_are_independent_parameters() -> None:
    """LINE と EDINET を同一波でローテーションしない運用方針に合わせる。"""
    environment = _globals_environment()

    assert environment[_LINE_MARKER_ENV] == {"Fn::Ref": _LINE_PARAMETER}
    assert environment[_EDINET_MARKER_ENV] == {"Fn::Ref": _EDINET_PARAMETER}
    assert environment[_LINE_MARKER_ENV] != environment[_EDINET_MARKER_ENV]


# --- F: マーカーは秘密と同じ Environment ブロックにあること --------------------


def test_markers_live_in_the_same_environment_block_as_the_secrets() -> None:
    """別ブロックに置くと、マーカーを変えても当該 dynamic reference が再解決されない。

    これが R1 の中核契約であり、ここが壊れると仕組み全体が無効になる。
    """
    environment = _globals_environment()

    for env_name in _GLOBAL_SECRET_ENV:
        assert env_name in environment, f"{env_name} が Globals から消えている"

    assert _LINE_MARKER_ENV in environment
    assert _EDINET_MARKER_ENV in environment


# --- D: 秘密の受け渡し方式は R1 では変えない ---------------------------------


def test_secret_dynamic_references_are_preserved() -> None:
    """実行時取得（恒久対策）は別 Phase。R1 では dynamic reference を維持する。"""
    environment = _globals_environment()

    for env_name in _GLOBAL_SECRET_ENV:
        rendered = str(environment[env_name])
        assert "resolve:secretsmanager" in rendered, f"{env_name} の dynamic reference が失われた"
        assert ":SecretString" in rendered


# --- H: 対象外の credential を巻き込まない -----------------------------------


def test_webhook_channel_secret_has_no_rotation_marker() -> None:
    """Webhook 署名検証用の秘密は R1 のローテーション対象外。

    公式仕様上、この秘密は再発行すると即座に旧値が無効化され、
    アクセストークンのような有効期間延長の手段が無い。したがって必須
    ローテーションの対象から外しており、専用マーカーも持たせない。
    """
    template = _load_template()
    webhook_env = template["Resources"]["LineWebhookFunction"]["Properties"]["Environment"][
        "Variables"
    ]

    assert _WEBHOOK_SECRET_ENV in webhook_env, "対象外だが受け渡し自体は維持する"
    assert _LINE_MARKER_ENV not in webhook_env
    assert _EDINET_MARKER_ENV not in webhook_env

    parameters = _parameters()
    assert not any(
        "ChannelSecret" in name and "RotationVersion" in name for name in parameters
    ), "Webhook 署名検証用の秘密に専用のローテーションマーカーを作らない"


# --- G: 無関係な設定を変えていないこと ---------------------------------------


def test_unrelated_global_environment_is_unchanged() -> None:
    environment = _globals_environment()

    assert environment["DYNAMODB_TABLE_PREFIX"] == {"Fn::Ref": "TablePrefix"}
    assert environment["JSTOCK_CONFIG_DIR"] == "/opt"


def test_globals_function_settings_are_unchanged() -> None:
    function_globals = _load_template()["Globals"]["Function"]

    assert function_globals["Runtime"] == "python3.12"
    assert function_globals["Timeout"] == 300
    assert function_globals["MemorySize"] == 512


# --- E: 秘密値がテンプレートへ混入していないこと ------------------------------


def test_template_contains_no_literal_secret_material() -> None:
    """マーカー導入で秘密がテンプレートへ直書きされていないことを固定する。

    秘密はすべて Secrets Manager の dynamic reference か、ARN パラメータ経由で
    渡される。ここでは「値そのものらしき文字列」が混ざっていないことを見る。
    """
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")

    # LINE のチャネルアクセストークンは長い base64 風文字列になる。
    assert not re.search(r"[A-Za-z0-9+/]{80,}={0,2}", text), "長大なトークン様文字列がある"
    for forbidden in ("Bearer ", "-----BEGIN", "channel_secret:", "api_key:"):
        assert forbidden not in text, f"秘密らしき記述がある: {forbidden}"


# --- I: テンプレートが構文として妥当であること --------------------------------


def test_template_parses_and_declares_every_referenced_parameter() -> None:
    """マーカーが未宣言パラメータを参照していると、デプロイ時に初めて失敗する。"""
    template = _load_template()
    declared = set(template["Parameters"])
    environment = template["Globals"]["Function"]["Environment"]["Variables"]

    for env_name in (_LINE_MARKER_ENV, _EDINET_MARKER_ENV):
        referenced = environment[env_name]["Fn::Ref"]
        assert referenced in declared, f"{referenced} が Parameters に宣言されていない"
