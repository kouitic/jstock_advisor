"""鍵の provider(`SecretsManagerKeyProvider`)の契約テスト(Issue #925 kv1。#128 R1)。

固定するもの
  (1) 失敗の分類 6 種(NOT_CONFIGURED / ACCESS_DENIED / NOT_FOUND / TIMEOUT / MALFORMED / ERROR)と、
      その到達可能性(全メンバーを返す経路が実在する)
  (2) ★ 例外を送出しない ★(任意の Exception。BaseException は畳まない)
  (3) 値の検証(ちょうど {key_version, key}・版の範囲・英数字 64 文字以上)
  (4) 成功だけを TTL つきでキャッシュ(失敗はキャッシュしない)
  (5) ★ 秘密が出ない ★: 鍵・ARN・account・Secret の名前が、ログ・例外・repr に含まれない
      (canary で検査)
  (6) boto3 の呼び出しは get_secret_value だけ(allowlist の proxy)・再試行なし・timeout
  (7) 失敗ごとに固定の印 1 行(template の MetricFilter の literal と一致)。
      成功・キャッシュ命中はログ 0 行
  (8) module は os.environ を読まない・スレッドを作らない・待たない
      (環境変数の読取と期限の壁は wiring の責務)
moto の Secrets Manager(依存の追加なし)と fake の client を使う。AWS には接続しない。
"""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)
from moto import mock_aws

from jstock_advisor.domain.entities.identifier_ref import (
    KeyMaterial,
    KeyUnavailable,
    KeyUnavailableReason,
    keyed_owner_ref,
)
from jstock_advisor.infrastructure.aws import allocation_shadow_key_provider as module
from jstock_advisor.infrastructure.aws.allocation_shadow_key_provider import (
    ANOMALY_MARKER,
    ANOMALY_REASONS,
    CONFIG_ISSUE_MARKER,
    CONFIG_ISSUE_REASONS,
    KeyProviderViolationError,
    SecretsManagerKeyProvider,
    _default_client_factory,
    _GetSecretValueOnlyClient,
    shared_key_provider,
)

_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = (
    _REPO
    / "src"
    / "jstock_advisor"
    / "infrastructure"
    / "aws"
    / "allocation_shadow_key_provider.py"
)
R = KeyUnavailableReason

#: 鍵の値の canary(ログ・例外・repr のどこにも出てはならない)。英数字 66 文字
CANARY = "Qx7" * 22
ACCOUNT = "123456789012"
SECRET_NAME = "canary-secret-name/owner-ref-key"
ARN = f"arn:aws:secretsmanager:ap-northeast-1:{ACCOUNT}:secret:{SECRET_NAME}-AbCdEf"
GOOD = json.dumps({"key_version": 1, "key": CANARY})


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{CANARY} {ARN}"}}, "GetSecretValue")


class FakeClient:
    """get_secret_value だけを持つ fake。応答か例外を返す。呼び出しを記録する。"""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def get_secret_value(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class Factory:
    """client_factory の fake。生成の引数と回数を記録する。"""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.created: list[tuple[str, float, float]] = []

    def __call__(self, region: str, connect: float, read: float) -> Any:
        self.created.append((region, connect, read))
        if isinstance(self.client, BaseException):
            raise self.client
        return self.client


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _provider(outcome: Any, **kwargs: Any) -> tuple[SecretsManagerKeyProvider, FakeClient, Factory]:
    client = FakeClient(outcome)
    factory = Factory(client)
    provider = SecretsManagerKeyProvider(
        secret_arn=kwargs.pop("secret_arn", ARN), client_factory=factory, **kwargs
    )
    return provider, client, factory


def _ok(secret: str = GOOD) -> dict[str, Any]:
    return {"SecretString": secret, "ARN": ARN}


# --- (1) 失敗の分類 ----------------------------------------------------------------------------


@pytest.mark.parametrize("arn", [None, "", "   ", 123, b"x", "not-an-arn", "arn:aws:s3:::bucket"])
def test_an_unusable_arn_is_not_configured_and_nothing_is_called(arn: Any) -> None:
    client = FakeClient(_ok())
    factory = Factory(client)
    result = SecretsManagerKeyProvider(secret_arn=arn, client_factory=factory).get()
    assert result == KeyUnavailable(R.NOT_CONFIGURED)
    assert factory.created == [] and client.calls == []


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (_client_error("AccessDeniedException"), R.ACCESS_DENIED),
        (_client_error("AccessDenied"), R.ACCESS_DENIED),
        (_client_error("DecryptionFailure"), R.ACCESS_DENIED),
        (NoCredentialsError(), R.ACCESS_DENIED),
        (PartialCredentialsError(provider="p", cred_var="v"), R.ACCESS_DENIED),
        (_client_error("ResourceNotFoundException"), R.NOT_FOUND),
        (_client_error("InvalidRequestException"), R.NOT_FOUND),
        (_client_error("InvalidParameterException"), R.NOT_CONFIGURED),
        (ConnectTimeoutError(endpoint_url="https://x"), R.TIMEOUT),
        (ReadTimeoutError(endpoint_url="https://x"), R.TIMEOUT),
        (TimeoutError(), R.TIMEOUT),
        (_client_error("ThrottlingException"), R.ERROR),
        (_client_error("InternalServiceError"), R.ERROR),
        (_client_error("SomethingNew"), R.ERROR),
        (RuntimeError(f"{CANARY} {ARN}"), R.ERROR),
        (OSError("connection refused"), R.ERROR),
        (ClientError({}, "GetSecretValue"), R.ERROR),  # 応答に Error が無い
    ],
)
def test_each_failure_of_the_call_maps_to_one_reason(outcome: BaseException, reason: R) -> None:
    provider, _, _ = _provider(outcome)
    assert provider.get() == KeyUnavailable(reason)


def test_a_failure_to_create_the_client_is_an_error() -> None:
    factory = Factory(RuntimeError(f"{CANARY} {ARN}"))
    result = SecretsManagerKeyProvider(secret_arn=ARN, client_factory=factory).get()
    assert result == KeyUnavailable(R.ERROR)


def test_every_reason_has_a_path_that_returns_it() -> None:
    """同型 sweep: KeyUnavailableReason の全メンバーを、実際に返す経路が実在する。"""
    reached = {
        SecretsManagerKeyProvider(secret_arn=None).get().reason,  # type: ignore[union-attr]
        _provider(_client_error("AccessDeniedException"))[0].get().reason,  # type: ignore[union-attr]
        _provider(_client_error("ResourceNotFoundException"))[0].get().reason,  # type: ignore[union-attr]
        _provider(TimeoutError())[0].get().reason,  # type: ignore[union-attr]
        _provider(_ok("not json"))[0].get().reason,  # type: ignore[union-attr]
        _provider(RuntimeError("x"))[0].get().reason,  # type: ignore[union-attr]
    }
    assert reached == set(R)


def test_the_two_observation_groups_split_every_reason_without_overlap() -> None:
    assert set(R) == CONFIG_ISSUE_REASONS | ANOMALY_REASONS
    assert set() == CONFIG_ISSUE_REASONS & ANOMALY_REASONS


# --- moto(実際の Secrets Manager の API の形)-------------------------------------------------


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    with mock_aws():
        yield boto3.client("secretsmanager", region_name="ap-northeast-1")


def _create(aws: Any, secret: str = GOOD) -> str:
    return aws.create_secret(Name=SECRET_NAME, SecretString=secret)["ARN"]  # type: ignore[no-any-return]


def test_moto_a_well_formed_secret_returns_a_key_that_makes_a_keyed_ref(aws: Any) -> None:
    arn = _create(aws)
    first = SecretsManagerKeyProvider(secret_arn=arn).get()
    second = SecretsManagerKeyProvider(secret_arn=arn).get()
    assert isinstance(first, KeyMaterial) and first.key_version == 1
    assert isinstance(second, KeyMaterial)
    ref = keyed_owner_ref("owner-a", first)
    assert ref.startswith("kv1.1:") and ref == keyed_owner_ref("owner-a", second)
    assert CANARY not in repr(first) and CANARY not in str(first)


def test_moto_a_missing_secret_is_not_found(aws: Any) -> None:
    missing = f"arn:aws:secretsmanager:ap-northeast-1:{ACCOUNT}:secret:absent-AbCdEf"
    assert SecretsManagerKeyProvider(secret_arn=missing).get() == KeyUnavailable(R.NOT_FOUND)


def test_moto_a_secret_scheduled_for_deletion_is_not_found(aws: Any) -> None:
    arn = _create(aws)
    aws.delete_secret(SecretId=arn, RecoveryWindowInDays=7)
    assert SecretsManagerKeyProvider(secret_arn=arn).get() == KeyUnavailable(R.NOT_FOUND)


def test_moto_a_binary_only_secret_is_malformed(aws: Any) -> None:
    arn = aws.create_secret(Name=SECRET_NAME, SecretBinary=b"x" * 64)["ARN"]
    assert SecretsManagerKeyProvider(secret_arn=arn).get() == KeyUnavailable(R.MALFORMED)


# --- (3) 値の検証 ------------------------------------------------------------------------------

_KEY_OK = "A" * 64


@pytest.mark.parametrize(
    "secret",
    [
        "not json",
        "[1, 2]",
        '"just a string"',
        "null",
        json.dumps({"key": _KEY_OK}),  # key_version の欠落
        json.dumps({"key_version": 1}),  # key の欠落
        json.dumps(
            {"key_version": 1, "key": _KEY_OK, "extra": 1}
        ),  # 余計なキー(別の Secret の疑い)
        json.dumps({"key_version": True, "key": _KEY_OK}),
        json.dumps({"key_version": "1", "key": _KEY_OK}),
        json.dumps({"key_version": 1.0, "key": _KEY_OK}),
        json.dumps({"key_version": 0, "key": _KEY_OK}),
        json.dumps({"key_version": 1000, "key": _KEY_OK}),
        json.dumps({"key_version": 1, "key": "A" * 63}),  # 短い(255 ビット以下)
        json.dumps({"key_version": 1, "key": ("A" * 63) + "-"}),  # 非英数字
        json.dumps({"key_version": 1, "key": ("A" * 63) + " "}),
        json.dumps({"key_version": 1, "key": ("A" * 63) + "\n"}),  # 末尾の改行(fullmatch で拒否)
        json.dumps({"key_version": 1, "key": 12345}),
        json.dumps({"key_version": 1, "key": None}),
        json.dumps({"key_version": 1, "key": "あ" * 64}),  # 非 ASCII
    ],
)
def test_a_secret_that_is_not_exactly_the_agreed_shape_is_malformed(secret: str) -> None:
    provider, _, _ = _provider(_ok(secret))
    assert provider.get() == KeyUnavailable(R.MALFORMED)


@pytest.mark.parametrize(
    "response", [None, [], "text", {}, {"SecretString": None}, {"SecretString": 5}]
)
def test_a_response_without_a_secret_string_is_malformed(response: Any) -> None:
    provider, _, _ = _provider(response)
    assert provider.get() == KeyUnavailable(R.MALFORMED)


@pytest.mark.parametrize("version", [1, 2, 998, 999])
def test_the_key_version_boundaries_are_accepted(version: int) -> None:
    secret = json.dumps({"key_version": version, "key": _KEY_OK})
    result = _provider(_ok(secret))[0].get()
    assert isinstance(result, KeyMaterial) and result.key_version == version


def test_the_minimum_key_length_is_accepted_and_a_longer_key_too() -> None:
    for length in (64, 65, 200):
        result = _provider(
            _ok(json.dumps({"key_version": 1, "key": "a1" * (length // 2) + "b" * (length % 2)}))
        )[0].get()
        assert isinstance(result, KeyMaterial), length


# --- (2) 例外を送出しない ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ValueError("x"),
        KeyError("k"),
        OSError("o"),
        RuntimeError("r"),
        AssertionError("a"),
        MemoryError(),
    ],
)
def test_get_never_raises_an_exception(error: Exception) -> None:
    result = _provider(error)[0].get()
    assert isinstance(result, KeyUnavailable)


def test_a_base_exception_is_not_swallowed() -> None:
    """KeyboardInterrupt 等は畳まない(契約の境界。例外を送出しないのは Exception まで)。"""
    with pytest.raises(KeyboardInterrupt):
        _provider(KeyboardInterrupt())[0].get()


def test_a_failure_inside_the_clock_is_an_error_not_an_exception() -> None:
    def broken_clock() -> float:
        raise RuntimeError("clock")

    provider, _, _ = _provider(_ok(), monotonic=broken_clock)
    provider._cached = (
        KeyMaterial(key_version=1, key=b"k" * 32),
        0.0,
    )  # キャッシュの確認で時計を読む
    assert provider.get() == KeyUnavailable(R.ERROR)


# --- (4) キャッシュ ----------------------------------------------------------------------------


def test_a_success_is_cached_within_the_ttl_and_fetched_again_after_it() -> None:
    clock = Clock()
    provider, client, _ = _provider(_ok(), monotonic=clock, cache_ttl_seconds=900.0)
    first = provider.get()
    clock.now += 899.9
    assert provider.get() is first and len(client.calls) == 1
    clock.now += 0.2  # 期限(900 秒)を過ぎた
    assert isinstance(provider.get(), KeyMaterial) and len(client.calls) == 2


def test_a_failure_is_never_cached() -> None:
    provider, client, _ = _provider(_client_error("ThrottlingException"))
    for _ in range(3):
        assert provider.get() == KeyUnavailable(R.ERROR)
    assert len(client.calls) == 3


def test_a_failure_followed_by_a_success_caches_the_success() -> None:
    provider, client, _ = _provider(TimeoutError())
    assert provider.get() == KeyUnavailable(R.TIMEOUT)
    client.outcome = _ok()
    assert isinstance(provider.get(), KeyMaterial)
    assert isinstance(provider.get(), KeyMaterial)
    assert len(client.calls) == 2  # 失敗 1 回 + 成功 1 回(その後はキャッシュ)


def test_a_malformed_response_is_not_cached() -> None:
    provider, client, _ = _provider(_ok("not json"))
    assert provider.get() == KeyUnavailable(R.MALFORMED)
    client.outcome = _ok()
    assert isinstance(provider.get(), KeyMaterial)
    assert len(client.calls) == 2


def test_the_cache_holds_nothing_but_the_key_material() -> None:
    provider, _, _ = _provider(_ok())
    provider.get()
    cached = provider._cached
    assert (
        cached is not None and isinstance(cached[0], KeyMaterial) and isinstance(cached[1], float)
    )
    held = {
        name: value
        for name, value in vars(provider).items()
        if isinstance(value, (str, bytes, dict))
    }
    assert CANARY not in json.dumps({k: str(v) for k, v in held.items()})


def test_shared_provider_keeps_one_instance_per_valid_arn_so_a_warm_run_reuses_the_cache() -> None:
    module._SHARED.clear()
    try:
        assert shared_key_provider(ARN) is shared_key_provider(ARN)
        other = ARN.replace("AbCdEf", "ZzZzZz")
        assert shared_key_provider(other) is not shared_key_provider(ARN)
        # 不正な ARN は保持しない(毎回新しい。NOT_CONFIGURED を返す)
        assert shared_key_provider(None) is not shared_key_provider(None)
        assert shared_key_provider("x").get() == KeyUnavailable(R.NOT_CONFIGURED)
        assert set(module._SHARED) == {ARN, other}
    finally:
        module._SHARED.clear()


# --- (6) boto3 の呼び出し ----------------------------------------------------------------------


def test_the_only_call_is_get_secret_value_with_the_secret_id() -> None:
    provider, client, factory = _provider(_ok())
    provider.get()
    assert client.calls == [{"SecretId": ARN}]  # VersionStage なし(AWSCURRENT)
    assert factory.created == [("ap-northeast-1", 2.0, 3.0)]  # region は ARN から・timeout が渡る


def test_the_proxy_refuses_every_other_operation() -> None:
    proxy = _GetSecretValueOnlyClient(FakeClient(_ok()))
    for name in ("describe_secret", "put_secret_value", "delete_secret", "list_secrets", "x"):
        with pytest.raises(KeyProviderViolationError):
            getattr(proxy, name)


def test_the_default_client_has_no_retry_and_the_timeouts() -> None:
    client = _default_client_factory("ap-northeast-1", 2.0, 3.0)
    config = client.meta.config
    assert config.connect_timeout == 2.0 and config.read_timeout == 3.0
    # ★ 再試行なし = 初回だけ。`max_attempts` は初回を除く再試行の回数なので、1 だと計 2 回呼ぶ ★
    assert config.retries.get("total_max_attempts") == 1
    assert config.retries.get("max_attempts", 0) == 0
    assert client.meta.region_name == "ap-northeast-1"


class _Raw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, *_: Any, **__: Any) -> Any:
        yield self._body

    def read(self, *_: Any) -> bytes:
        return self._body


def test_the_default_client_really_sends_one_request_even_when_the_server_fails(
    aws: Any,
) -> None:
    """サーバが 500 を返しても、送るのは 1 回だけ(設定の名前ではなく、送信の回数で確かめる)。"""
    from botocore.awsrequest import AWSResponse  # noqa: PLC0415

    sent: list[str] = []

    def fail_every_request(request: Any, **_: Any) -> AWSResponse:
        sent.append(request.url)
        return AWSResponse(
            request.url, 500, {}, _Raw(b'{"__type":"InternalServiceError","message":"x"}')
        )

    client = _default_client_factory("ap-northeast-1", 2.0, 3.0)
    client.meta.events.register("before-send.secrets-manager.GetSecretValue", fail_every_request)
    provider = SecretsManagerKeyProvider(secret_arn=ARN, client_factory=lambda *_: client)
    assert provider.get() == KeyUnavailable(R.ERROR)
    assert len(sent) == 1


def test_the_timeouts_are_passed_from_the_constructor() -> None:
    provider, _, factory = _provider(_ok(), connect_timeout_seconds=0.5, read_timeout_seconds=1.5)
    provider.get()
    assert factory.created == [("ap-northeast-1", 0.5, 1.5)]


# --- (5) 秘密が出ない --------------------------------------------------------------------------

_LEAK_SCENARIOS: list[tuple[str, Callable[[], tuple[SecretsManagerKeyProvider, Any, Any]]]] = [
    ("success", lambda: _provider(_ok())),
    ("denied", lambda: _provider(_client_error("AccessDeniedException"))),
    ("not_found", lambda: _provider(_client_error("ResourceNotFoundException"))),
    ("timeout", lambda: _provider(ReadTimeoutError(endpoint_url=f"https://{ACCOUNT}.example"))),
    ("malformed", lambda: _provider(_ok(json.dumps({"key_version": 1, "key": CANARY, "x": 1})))),
    ("error", lambda: _provider(RuntimeError(f"{CANARY} {ARN} {SECRET_NAME}"))),
    ("throttle", lambda: _provider(_client_error("ThrottlingException"))),
]


@pytest.mark.parametrize(("name", "build"), _LEAK_SCENARIOS, ids=[s[0] for s in _LEAK_SCENARIOS])
def test_no_secret_appears_on_any_surface(
    caplog: pytest.LogCaptureFixture, name: str, build: Callable[[], Any]
) -> None:
    """★ 面ごとに測る ★(面を連結すると、1 つの面が空でも通る)。各面は『空でない』ことも確かめる。

    canary は架空の固定値(実在の鍵・ARN・account ではない)。
    """
    provider, _, _ = build()
    with caplog.at_level(logging.DEBUG):
        result = provider.get()
        result_again = provider.get()
    failed = isinstance(result, KeyUnavailable)
    records = [r for r in caplog.records if r.name == module.__name__]
    surfaces: dict[str, str] = {
        "log_text": caplog.text,
        "log_messages": " | ".join(r.getMessage() for r in records),
        "log_args": " | ".join(str(r.args) for r in records),
        "log_exception": " | ".join(f"{r.exc_info} {r.exc_text}" for r in caplog.records),
        "repr_provider": repr(provider),
        "str_provider": str(provider),
        "repr_result": repr(result),
        "str_result": str(result),
        "repr_result_again": repr(result_again),
    }
    # 空振り防止: 面ごとに、その面が実際に何かを持っていること
    assert surfaces["repr_provider"] == "SecretsManagerKeyProvider()"
    assert surfaces["repr_result"].startswith(("KeyUnavailable(", "KeyMaterial("))
    assert surfaces["str_result"] and surfaces["repr_result_again"]
    if failed:
        # 失敗はキャッシュされないので、2 回の get() が 2 行(1 回につき 1 行)
        assert len(records) == 2
        assert "ALLOCATION_SHADOW_KEY_UNAVAILABLE" in surfaces["log_messages"]
        assert "ALLOCATION_SHADOW_KEY_UNAVAILABLE" in surfaces["log_text"]
        assert surfaces["log_args"].count("reason") == 0  # 引数は理由コードの語だけ(中身は下で測る)
        assert surfaces["log_exception"].count("None None") == 2  # 例外の情報を持たない
    else:
        assert records == []  # 成功・キャッシュ命中はログを出さない
    for record in caplog.records:
        assert record.exc_info is None  # 例外のスタックを出さない
    for surface_name, text in surfaces.items():
        for secret in (CANARY, ARN, ACCOUNT, SECRET_NAME, "AbCdEf"):
            assert secret not in text, (name, surface_name, secret)


def test_the_provider_repr_has_no_arn() -> None:
    assert repr(SecretsManagerKeyProvider(secret_arn=ARN)) == "SecretsManagerKeyProvider()"


# --- (7) ログの印 ------------------------------------------------------------------------------


def _single_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    records = [r for r in caplog.records if r.name == module.__name__]
    assert len(records) == 1
    return records[0]


@pytest.mark.parametrize(
    ("outcome", "reason", "marker", "level"),
    [
        (
            _client_error("AccessDeniedException"),
            R.ACCESS_DENIED,
            CONFIG_ISSUE_MARKER,
            logging.ERROR,
        ),
        (
            _client_error("ResourceNotFoundException"),
            R.NOT_FOUND,
            CONFIG_ISSUE_MARKER,
            logging.ERROR,
        ),
        (TimeoutError(), R.TIMEOUT, ANOMALY_MARKER, logging.WARNING),
        (_client_error("ThrottlingException"), R.ERROR, ANOMALY_MARKER, logging.WARNING),
        (_ok("not json"), R.MALFORMED, ANOMALY_MARKER, logging.WARNING),
    ],
)
def test_a_failure_logs_exactly_one_fixed_marker_line(
    caplog: pytest.LogCaptureFixture, outcome: Any, reason: R, marker: str, level: int
) -> None:
    provider, _, _ = _provider(outcome)
    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        provider.get()
    record = _single_record(caplog)
    assert record.levelno == level
    assert record.getMessage() == f"{marker} reason={reason.value}"


def test_an_unusable_arn_logs_the_config_marker_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        SecretsManagerKeyProvider(secret_arn=None).get()
    assert _single_record(caplog).getMessage() == f"{CONFIG_ISSUE_MARKER} reason=NOT_CONFIGURED"


def test_a_success_and_a_cache_hit_log_nothing(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = _provider(_ok())
    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        provider.get()
        provider.get()
    assert [r for r in caplog.records if r.name == module.__name__] == []


class _CfnLoader(yaml.SafeLoader):
    """CloudFormation の短縮形を無視して読む loader(構文解析専用)。"""


def _ignore_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]


_CfnLoader.add_multi_constructor("!", _ignore_tag)  # type: ignore[no-untyped-call]


def test_the_markers_equal_the_filter_patterns_of_the_template() -> None:
    """印の literal が infra/template.yaml の MetricFilter の FilterPattern と一致する(結合)。"""
    template = yaml.load(
        (_REPO / "infra" / "template.yaml").read_text(encoding="utf-8"), Loader=_CfnLoader
    )  # noqa: S506
    patterns = {
        resource["Properties"]["FilterPattern"]
        for resource in template["Resources"].values()
        if resource.get("Type") == "AWS::Logs::MetricFilter"
        and "ALLOCATION_SHADOW" in resource["Properties"]["FilterPattern"]
    }
    assert patterns == {f'"{CONFIG_ISSUE_MARKER}"', f'"{ANOMALY_MARKER}"'}


# --- (8) module の構造 -------------------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_module_does_not_read_the_environment() -> None:
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(_tree()) if isinstance(n, ast.Name)}
    imported = {a.name for n in ast.walk(_tree()) if isinstance(n, ast.Import) for a in n.names}
    assert not names & {"environ", "getenv"} and "os" not in imported


def test_the_module_creates_no_thread_and_never_sleeps() -> None:
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(_tree()) if isinstance(n, ast.Name)}
    imported = {a.name for n in ast.walk(_tree()) if isinstance(n, ast.Import) for a in n.names}
    from_imports = {n.module for n in ast.walk(_tree()) if isinstance(n, ast.ImportFrom)}
    assert not names & {"Thread", "sleep", "Timer"}
    assert "threading" not in imported | from_imports


def test_the_module_calls_only_get_secret_value_on_the_client() -> None:
    called = {
        n.func.attr
        for n in ast.walk(_tree())
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    forbidden = {
        "describe_secret",
        "put_secret_value",
        "delete_secret",
        "list_secrets",
        "update_secret",
        "rotate_secret",
        "put_resource_policy",
        "get_resource_policy",
        "create_secret",
    }
    assert not called & forbidden
    assert "get_secret_value" in called


# --- fail-closed: 期限が切れた後に失敗した日は、古い鍵へ落ちない(USER 決定 #122 …6138622792【3】)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (_client_error("AccessDeniedException"), R.ACCESS_DENIED),
        (_client_error("ResourceNotFoundException"), R.NOT_FOUND),
        (TimeoutError(), R.TIMEOUT),
        (_client_error("ThrottlingException"), R.ERROR),
        (RuntimeError("boom"), R.ERROR),
        (_ok("not json"), R.MALFORMED),  # 応答が仕様に合わない(呼び出し自体は成功)
        (_ok(json.dumps({"key_version": 1, "key": "short"})), R.MALFORMED),
    ],
)
def test_after_the_ttl_a_failure_never_falls_back_to_the_expired_key(
    outcome: Any, reason: R
) -> None:
    """★ 成功 → 期限切れ → 失敗 ★ の組合せ。『取れないなら前のを使えばいい』を許さない。

    入ると、回転で捨てたはずの鍵で符号が作られる(最も入り込みやすい善意のバグ)。
    """
    clock = Clock()
    provider, client, _ = _provider(_ok(), monotonic=clock, cache_ttl_seconds=900.0)
    first = provider.get()
    assert isinstance(first, KeyMaterial)  # 1 回目は成功してキャッシュされる
    client.outcome = outcome
    clock.now += 900.1  # 期限切れ
    result = provider.get()
    assert result == KeyUnavailable(reason)
    assert not isinstance(result, KeyMaterial)
    assert len(client.calls) == 2  # 期限後は必ず再取得を試みた(キャッシュで済ませていない)


def test_the_expired_key_is_not_resurrected_by_a_later_failure_either() -> None:
    clock = Clock()
    provider, client, _ = _provider(_ok(), monotonic=clock)
    assert isinstance(provider.get(), KeyMaterial)
    client.outcome = TimeoutError()
    clock.now += 1000.0
    assert provider.get() == KeyUnavailable(R.TIMEOUT)
    assert provider.get() == KeyUnavailable(R.TIMEOUT)  # 2 回続けても古い鍵は返らない
    client.outcome = _ok()
    assert isinstance(provider.get(), KeyMaterial)  # 回復したら新しい成功が返る


# --- ログの失敗で契約を破らない -----------------------------------------------------------------


def test_get_never_raises_even_when_the_logging_machinery_is_broken() -> None:
    """壊れた record factory(異常な構成)でも、get() は例外を送出しない。"""
    original = logging.getLogRecordFactory()

    def broken_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        raise RuntimeError("broken logging")

    logging.setLogRecordFactory(broken_factory)
    try:
        assert SecretsManagerKeyProvider(secret_arn="not-an-arn").get() == KeyUnavailable(
            R.NOT_CONFIGURED
        )
        provider, _, _ = _provider(_client_error("AccessDeniedException"))
        assert provider.get() == KeyUnavailable(R.ACCESS_DENIED)
        provider, _, _ = _provider(RuntimeError("x"))
        assert provider.get() == KeyUnavailable(R.ERROR)
    finally:
        logging.setLogRecordFactory(original)


def test_get_never_raises_even_when_a_log_filter_raises() -> None:
    class Broken(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            raise RuntimeError("broken filter")

    broken = Broken()
    module.logger.addFilter(broken)
    try:
        assert SecretsManagerKeyProvider(secret_arn=None).get() == KeyUnavailable(R.NOT_CONFIGURED)
    finally:
        module.logger.removeFilter(broken)
