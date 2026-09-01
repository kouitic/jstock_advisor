"""Issue #116: BUY経路のJPX canonical業種shadow observationの配線を固定する。

#54 Phase B-1 の shadow observation(`services/jpx_industry_source.py`)は
`CandidateUniverseCacheIO` 経由でJPX上場銘柄一覧キャッシュ(S3)を読むが、
`infra/template.yaml` の `BuyCandidatesFunction` に

- `CANDIDATE_UNIVERSE_CACHE_BUCKET` 環境変数
- 当該バケットへの読み取り権限

のいずれも定義されていなかったため、Production では
`jpx_lookup_status` が **100% `SOURCE_UNAVAILABLE`**(canonical解決率0%)となり、
Phase B-1 の主目的である「BUY経路でJPX canonical業種を解決できる割合」が
測定不能だった。

本モジュールが固定する契約:

1. **配線(infra)**: `BuyCandidatesFunction` に環境変数とバケット読み取り権限が
   存在し、Resource がワイルドカードでないこと(Test F)
2. **fail-soft**: 環境変数が無い / S3読み取りが失敗する場合でも、例外を送出せず
   `SOURCE_UNAVAILABLE` として観測し、BUY判定を止めないこと(Test C / D)
3. **shadow-only**: shadow の解決状態(`RESOLVED` / `NOT_FOUND` /
   `SOURCE_UNAVAILABLE`)が変わっても、同一の snapshot 入力に対する
   BUY判定結果が一切変わらないこと(Test E)

`RESOLVED` / `NOT_FOUND` の観測内容そのもの(Test A / B)は
`tests/unit/test_buy_signal_service.py` の
`test_canonical_industry_observation_records_jpx_resolution` /
`test_canonical_industry_observation_records_unresolved_state` が既に固定している。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jstock_advisor.domain.classification.canonical_industry import JpxLookupStatus
from jstock_advisor.services import candidate_universe_downloader as downloader_module
from jstock_advisor.services import jpx_industry_source as module
from jstock_advisor.services.jpx_industry_source import JpxIndustrySource

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_FUNCTION_LOGICAL_ID = "BuyCandidatesFunction"
_BUCKET_LOGICAL_ID = "CandidateUniverseCacheBucket"
_BUCKET_ENV_NAME = "CANDIDATE_UNIVERSE_CACHE_BUCKET"

# conftest の autouse fixture が `_load_jpx_industry_map` を差し替えるため、
# ローダ本体を検証するテストでは実装関数へ戻す(test_jpx_industry_source.py と同方針)。
_REAL_LOAD_JPX_INDUSTRY_MAP = module._load_jpx_industry_map  # noqa: SLF001


@pytest.fixture
def real_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_load_jpx_industry_map", _REAL_LOAD_JPX_INDUSTRY_MAP)


# --- Test C / D: fail-soft contract -------------------------------------------------


def test_missing_bucket_env_is_source_unavailable_without_raising(
    real_loader: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """環境変数未設定(= Issue #116 の Production 実態)でも例外にせず観測値にする。

    `CandidateUniverseCacheIO.__init__` が `resolve_candidate_universe_bucket()`
    経由で `RuntimeError` を送出するが、`_load_jpx_industry_map()` が捕捉して
    `None` を返し、`SOURCE_UNAVAILABLE` として記録される。
    """
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "jstock-advisor-buy-candidates")
    monkeypatch.delenv(_BUCKET_ENV_NAME, raising=False)

    result = JpxIndustrySource().lookup("1234")

    assert result.status is JpxLookupStatus.SOURCE_UNAVAILABLE
    assert result.entry is None


def test_s3_read_failure_is_source_unavailable_without_raising(
    real_loader: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3読み取りが失敗しても例外を送出せず、BUY判定を止めない。"""

    class _FailingCacheIO:
        def __init__(self) -> None:
            pass

        def read_current(self, source: str) -> object:
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr(downloader_module, "CandidateUniverseCacheIO", _FailingCacheIO)
    monkeypatch.setattr(module, "CandidateUniverseCacheIO", _FailingCacheIO)

    result = JpxIndustrySource().lookup("1234")

    assert result.status is JpxLookupStatus.SOURCE_UNAVAILABLE
    assert result.entry is None


def test_missing_cache_object_is_source_unavailable_without_raising(
    real_loader: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """キャッシュ未生成(read_currentがNone)でも例外にしない。

    `read_current()` はキー不在を `NoSuchKey` で捕捉して `None` を返す契約であり、
    この分岐が成立するには `s3:ListBucket` が必要(S3は ListBucket を持たない主体へ
    404 ではなく 403 を返すため)。infra 側の権限付与理由は template.yaml のコメント参照。
    """

    class _EmptyCacheIO:
        def __init__(self) -> None:
            pass

        def read_current(self, source: str) -> object | None:
            return None

    monkeypatch.setattr(module, "CandidateUniverseCacheIO", _EmptyCacheIO)

    result = JpxIndustrySource().lookup("1234")

    assert result.status is JpxLookupStatus.SOURCE_UNAVAILABLE
    assert result.entry is None


# --- Test F: infra 配線 -------------------------------------------------------------


class _CfnLoader(yaml.SafeLoader):
    """CloudFormationの短縮形組み込み関数(!GetAtt/!Ref/!Sub等)を素朴なdictへ
    変換するだけの構文解析専用Loader(tests/unit/test_infra_iam_stock_analysis.py と同一手法)。
    """


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
    assert isinstance(loaded, dict)
    return loaded


def _function_properties(logical_id: str) -> dict[str, Any]:
    resources = _load_template()["Resources"]
    assert logical_id in resources, f"{logical_id} が template.yaml に存在しない"
    properties = resources[logical_id]["Properties"]
    assert isinstance(properties, dict)
    return properties


def _iter_statements(policies: Any) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    if isinstance(policies, list):
        for policy in policies:
            if isinstance(policy, dict) and "Statement" in policy:
                entries = policy["Statement"]
                if isinstance(entries, list):
                    statements.extend(e for e in entries if isinstance(e, dict))
    return statements


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _references_bucket(node: Any) -> bool:
    """!GetAtt <Bucket>.Arn / !Ref <Bucket> / !Sub "${<Bucket>.Arn}/..." のいずれかを含むか。"""
    if isinstance(node, str):
        return f"${{{_BUCKET_LOGICAL_ID}." in node
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key in {"GetAtt", "Ref"}
                and isinstance(value, str)
                and value.split(".", 1)[0] == _BUCKET_LOGICAL_ID
            ):
                return True
            if _references_bucket(value):
                return True
        return False
    if isinstance(node, list):
        return any(_references_bucket(item) for item in node)
    return False


def test_buy_candidates_function_has_candidate_universe_cache_bucket_env() -> None:
    """BuyCandidatesFunction に環境変数が配線され、値がバケット論理IDを参照している。

    バケット名の直書きは許可しない(!Ref で解決させる)。
    """
    variables = _function_properties(_FUNCTION_LOGICAL_ID)["Environment"]["Variables"]

    assert _BUCKET_ENV_NAME in variables, (
        f"{_FUNCTION_LOGICAL_ID} に {_BUCKET_ENV_NAME} が無い。"
        "JpxIndustrySource が SOURCE_UNAVAILABLE のままになる(Issue #116)"
    )
    assert variables[_BUCKET_ENV_NAME] == {"Ref": _BUCKET_LOGICAL_ID}


def test_buy_candidates_function_can_read_candidate_universe_cache() -> None:
    """JPXキャッシュの読み取りに必要な最小Actionが付与されている。"""
    policies = _function_properties(_FUNCTION_LOGICAL_ID)["Policies"]
    statements = [s for s in _iter_statements(policies) if _references_bucket(s.get("Resource"))]

    assert statements, (
        f"{_FUNCTION_LOGICAL_ID} に {_BUCKET_LOGICAL_ID} への権限が無い(Issue #116)"
    )

    granted = {
        action for statement in statements for action in _as_list(statement.get("Action", []))
    }
    # read_current() が実際に呼ぶのは get_object のみ。
    assert "s3:GetObject" in granted
    # キー不在を NoSuchKey(404)として観測するために必要。
    assert "s3:ListBucket" in granted


def test_buy_candidates_function_bucket_access_is_read_only_and_not_wildcard() -> None:
    """付与するのは読み取りのみで、Resource にワイルドカードを使わない。"""
    policies = _function_properties(_FUNCTION_LOGICAL_ID)["Policies"]
    statements = [s for s in _iter_statements(policies) if _references_bucket(s.get("Resource"))]

    for statement in statements:
        assert statement.get("Effect") == "Allow"
        for resource in _as_list(statement.get("Resource")):
            assert resource != "*", "Resource: '*' は禁止(Issue #116)"
        for action in _as_list(statement.get("Action", [])):
            assert isinstance(action, str)
            assert action != "s3:*", "s3:* は禁止(読み取り専用にする)"
            # promote() は WatchlistDispatcherFunction 側の責務であり、
            # BuyCandidatesFunction は書き込み・削除を行わない。
            assert not action.startswith("s3:Put")
            assert not action.startswith("s3:Delete")


def test_holdings_watchlist_function_is_not_wired_for_jpx_shadow() -> None:
    """便乗配線をしない。JpxIndustrySource の consumer は buy_signal_service のみ。"""
    properties = _function_properties("HoldingsWatchlistFunction")
    variables = properties.get("Environment", {}).get("Variables", {})

    assert _BUCKET_ENV_NAME not in variables, (
        "HoldingsWatchlistFunction は JpxIndustrySource を使わないため配線しない(Issue #116)"
    )
