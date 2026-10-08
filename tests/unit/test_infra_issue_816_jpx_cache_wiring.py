"""Issue #816: 銘柄名の解決(JPX上場銘柄一覧キャッシュの読取)の配線を固定する。

## 何が起きていたか

`services/watchlist_display_name.py` の `StockDisplayNameResolver` は、`CandidateUniverseCacheIO`
経由で JPX 上場銘柄一覧キャッシュ(S3)を読む。ところが finalizer(`watchlist_batch_finalizer`)を
実行する次の 3 関数には、環境変数 `CANDIDATE_UNIVERSE_CACHE_BUCKET` も S3 の読取権限も無く、
銘柄名が外部 provider・銘柄コードへ fallback していた
(#116 と同種。BuyCandidatesFunction は #116 で対処済み)。

* WatchlistWorkerFunction / WatchlistTerminalFailureHandlerFunction /
  WatchlistBatchReconcilerFunction

## 本モジュールが固定するもの

1. **配線**: 上の 3 関数に環境変数(`!Ref CandidateUniverseCacheBucket`)と、`s3:GetObject` のみ・
   `current/*` 限定の Statement が**1 つ**あること。
   ListBucket・書込・ワイルドカードは持たない(最小権限)。
2. **回帰の宣言表**: 全 Lambda 関数について『JPX キャッシュを読む必要があるか』を宣言する。
   新しい関数を追加したのに宣言が無ければ赤になる。
   宣言が YES なら配線を、NO なら配線が無いことを要求する。
3. **到達性の根拠**: 宣言表の根拠(JPX キャッシュを読む service を構築する src 上の呼び出し元)が
   変わったら赤になる。新しい呼び出し元が増えたとき、宣言表の見直しを促す。

テンプレートと src の静的解析のみを行う(AWS へのアクセスはしない)。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from tests.support.iam_contract_helpers import resources

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "jstock_advisor"

_ENV_KEY = "CANDIDATE_UNIVERSE_CACHE_BUCKET"
_BUCKET_REF = {"Fn::Ref": "CandidateUniverseCacheBucket"}
_READ_SID = "CandidateUniverseCacheReadForJpxStockName"
_READ_RESOURCE = {"Fn::Sub": "${CandidateUniverseCacheBucket.Arn}/current/*"}

# 本 Issue(#816)で配線した 3 関数
_TARGETS = (
    "WatchlistWorkerFunction",
    "WatchlistTerminalFailureHandlerFunction",
    "WatchlistBatchReconcilerFunction",
)

# 回帰の宣言表: 全 Lambda 関数 → (JPX キャッシュの読取が必要か, 理由)。
# True  = 銘柄名の解決または canonical 業種の shadow 観測で、キャッシュを実行時に読む。
# False = どちらの service も構築しない(下の到達性の根拠で固定)。
_DECLARATION: dict[str, tuple[bool, str]] = {
    "BuyCandidatesFunction": (True, "BuySignalService → JpxIndustrySource(#116)"),
    "BuyCandidateWorkerFunction": (True, "BuySignalService → JpxIndustrySource(#116)"),
    "WatchlistDispatcherFunction": (
        True,
        "finalizer → StockDisplayNameResolver。キャッシュの更新も担う",
    ),
    "WatchlistWorkerFunction": (True, "finalizer → StockDisplayNameResolver(#816)"),
    "WatchlistTerminalFailureHandlerFunction": (True, "finalizer → StockDisplayNameResolver(#816)"),
    "WatchlistBatchReconcilerFunction": (True, "finalizer → StockDisplayNameResolver(#816)"),
    "LineWebhookFunction": (True, "ConversationService → StockDisplayNameResolver"),
    "HoldingsWatchlistFunction": (
        False,
        "BuySignalService も StockDisplayNameResolver も構築しない",
    ),
    "HoldingsWatchlistWorkerFunction": (
        False,
        "BuySignalService も StockDisplayNameResolver も構築しない",
    ),
    "DisclosureCheckFunction": (False, "BuySignalService も StockDisplayNameResolver も構築しない"),
    "EvaluationFunction": (False, "BuySignalService も StockDisplayNameResolver も構築しない"),
    "IncidentNotifierFunction": (
        False,
        "BuySignalService も StockDisplayNameResolver も構築しない",
    ),
    "WeeklyReviewFunction": (False, "BuySignalService も StockDisplayNameResolver も構築しない"),
    "MonthlyReviewFunction": (False, "BuySignalService も StockDisplayNameResolver も構築しない"),
    "QuarterlyReviewFunction": (False, "BuySignalService も StockDisplayNameResolver も構築しない"),
}


def _functions() -> dict[str, dict[str, Any]]:
    return {
        name: res["Properties"]
        for name, res in resources().items()
        if res.get("Type") == "AWS::Serverless::Function"
    }


def _env(properties: dict[str, Any]) -> dict[str, Any]:
    return (properties.get("Environment") or {}).get("Variables") or {}


def _statements(properties: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in properties.get("Policies", []):
        if isinstance(entry, dict) and "Statement" in entry:
            out.extend(entry["Statement"])
    return out


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _s3_statements(properties: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        s
        for s in _statements(properties)
        if any(str(a).startswith(("s3:", "*")) for a in _as_list(s.get("Action")))
    ]


def _bucket_policy_templates(properties: dict[str, Any]) -> list[str]:
    """SAM の S3 policy template(S3ReadPolicy / S3CrudPolicy 等)でキャッシュ bucket を指すもの。"""
    out: list[str] = []
    for entry in properties.get("Policies", []):
        if not isinstance(entry, dict) or "Statement" in entry:
            continue
        for name, args in entry.items():
            if (
                name.startswith("S3")
                and isinstance(args, dict)
                and args.get("BucketName") == _BUCKET_REF
            ):
                out.append(name)
    return out


def _has_read_access(properties: dict[str, Any]) -> bool:
    if _bucket_policy_templates(properties):
        return True
    return any(
        s.get("Resource") == _READ_RESOURCE or s.get("Resource") == [_READ_RESOURCE]
        for s in _s3_statements(properties)
    )


# --- 1 配線 ----------------------------------------------------------------------


@pytest.mark.parametrize("name", _TARGETS)
def test_target_function_has_the_bucket_environment_variable(name: str) -> None:
    assert _env(_functions()[name])[_ENV_KEY] == _BUCKET_REF


@pytest.mark.parametrize("name", _TARGETS)
def test_target_function_has_exactly_one_least_privilege_s3_statement(name: str) -> None:
    properties = _functions()[name]
    [statement] = _s3_statements(properties)
    assert statement == {
        "Sid": _READ_SID,
        "Effect": "Allow",
        "Action": "s3:GetObject",
        "Resource": _READ_RESOURCE,
    }
    # SAM の S3ReadPolicy / S3CrudPolicy は GetObjectAcl・Put・Delete・List 等まで
    # 付与するため使わない。
    assert _bucket_policy_templates(properties) == []


@pytest.mark.parametrize("name", _TARGETS)
def test_target_function_grants_no_list_write_or_wildcard_on_s3(name: str) -> None:
    for statement in _s3_statements(_functions()[name]):
        actions = [str(a) for a in _as_list(statement["Action"])]
        assert actions == ["s3:GetObject"], actions
        assert statement["Effect"] == "Allow"
        resource = str(statement["Resource"])
        assert "current/*" in resource
        assert resource.count("*") == 1  # current/* のみ(bucket 全体・ワイルドカード Resource なし)


def test_the_three_targets_are_exactly_the_ones_missing_before_this_issue() -> None:
    """宣言表で YES の関数のうち、環境変数を持つ関数は、配線後はすべて読取権限も持つ(逆も同様)。"""
    for name, properties in _functions().items():
        assert (_ENV_KEY in _env(properties)) == _has_read_access(properties), name


# --- 2 回帰の宣言表 --------------------------------------------------------------


def test_every_lambda_function_declares_whether_it_reads_the_jpx_cache() -> None:
    assert sorted(_functions()) == sorted(_DECLARATION), (
        "Lambda 関数の追加・削除があった。JPX キャッシュの読取の要否を _DECLARATION に宣言すること"
    )


@pytest.mark.parametrize("name", sorted(_DECLARATION))
def test_declared_requirement_matches_the_wiring(name: str) -> None:
    needs, reason = _DECLARATION[name]
    properties = _functions()[name]
    if needs:
        assert _env(properties).get(_ENV_KEY) == _BUCKET_REF, (name, reason)
        assert _has_read_access(properties), (name, reason)
    else:
        assert _ENV_KEY not in _env(properties), (name, reason)
        assert not _has_read_access(properties), (name, reason)
        assert _s3_statements(properties) == [], (name, reason)


# --- 3 到達性の根拠 --------------------------------------------------------------


def _source_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _callers(call_name: str) -> set[str]:
    """src 上で `call_name(...)` を呼んでいる(定義は除く)ファイルの、src からの相対パス。"""
    found: set[str] = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == call_name:
                found.add(path.relative_to(_SRC).as_posix())
    return found


def _importers(module: str) -> set[str]:
    found: set[str] = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            from_match = isinstance(node, ast.ImportFrom) and node.module == module
            plain_match = isinstance(node, ast.Import) and any(a.name == module for a in node.names)
            if from_match or plain_match:
                found.add(path.relative_to(_SRC).as_posix())
    return found


def test_only_known_callers_build_the_stock_display_name_resolver() -> None:
    assert _callers("build_stock_display_name_resolver") == {
        "cli/watchlist_screening.py",
        "services/conversation_service.py",  # LineWebhookFunction
        # Dispatcher / Worker / TerminalFailureHandler / Reconciler
        "services/watchlist_batch_finalizer.py",
    }


def test_only_known_callers_build_the_buy_signal_service() -> None:
    assert _callers("BuySignalService") == {
        "cli/analyze.py",
        # BuyCandidatesFunction / BuyCandidateWorkerFunction
        "lambda_handlers/buy_candidates_handler.py",
    }


def test_only_known_lambda_handlers_import_the_batch_finalizer() -> None:
    importers = {
        p
        for p in _importers("jstock_advisor.services.watchlist_batch_finalizer")
        if p.startswith("lambda_handlers/")
    }
    assert importers == {
        "lambda_handlers/watchlist_batch_reconciler_handler.py",
        "lambda_handlers/watchlist_dispatcher_handler.py",
        "lambda_handlers/watchlist_terminal_failure_handler.py",
        "lambda_handlers/watchlist_worker_handler.py",
    }
