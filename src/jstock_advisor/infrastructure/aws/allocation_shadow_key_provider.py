"""購入側 Shadow の owner の符号を作る鍵の provider(Issue #925 kv1。#128 R1)。

Secrets Manager の Secret を 1 回読み、`KeyMaterial`(取れなければ `KeyUnavailable`)を返す。
`identifier_ref.KeyProvider`(Protocol)の実装。★ 例外を送出しない ★。

## 責務の境界
* provider = ARN を引数で受け、`GetSecretValue` を 1 回呼び、値を検証して返す。成功だけを TTL つきで
  保持する(warm な実行をまたぐ)。AWS の呼び出しの上限時間は botocore の timeout で持つ
  (スレッドを作らない・待たない)
* 環境変数の読取・Shadow 全体の期限の壁・run 内で鍵の取得を 1 回に束ねるのは wiring の責務
  (この module は `os.environ` を読まない)

## 失敗の分類(`KeyUnavailableReason`。値・ARN・メッセージを持たない)
  NOT_CONFIGURED  ARN が未設定・空・形が不正(AWS を呼ばない)/ InvalidParameterException
  ACCESS_DENIED   権限なし・復号できない・資格情報なし
  NOT_FOUND       Secret が無い・削除予約
  TIMEOUT         接続・読取の timeout
  MALFORMED       値が仕様に合わない(形・キー・版・長さ)
  ERROR           上記以外(throttling・接続不能・想定外)

## 秘密の扱い
* boto3 の呼び出しは `get_secret_value` だけ(allowlist の proxy。IAM も同じ 1 action)
* 鍵・ARN・account・Secret の名前・例外のメッセージを、ログ・例外・repr に出さない
* 保持するのは `KeyMaterial` だけで、取得した文字列・辞書は保持しない。★ ただしメモリからの消去は
  保証しない ★(CPython の `del`・再代入は解放・零化を保証せず、`str` は不変で零化できない)。
  実効の境界は『鍵を読める role を 2 つに限る』こと(infra/template.yaml。#947)
* ログは失敗 1 回につき 1 行の固定の印(template の MetricFilter が数える)。
  成功・キャッシュ命中では出さない
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)

from jstock_advisor.domain.entities.identifier_ref import (
    MAX_KEY_VERSION,
    MIN_KEY_VERSION,
    IdentifierRefError,
    KeyMaterial,
    KeyUnavailable,
    KeyUnavailableReason,
)

logger = logging.getLogger(__name__)

#: 失敗の印(infra/template.yaml の MetricFilter の FilterPattern と一致させる。契約テストで固定)
CONFIG_ISSUE_MARKER: Final = "ALLOCATION_SHADOW_KEY_UNAVAILABLE_CONFIG"
ANOMALY_MARKER: Final = "ALLOCATION_SHADOW_KEY_UNAVAILABLE_ANOMALY"
#: 設定の不備(デプロイ・権限・資源の不備。1 件で気づくべき)の系統。
#: 残りは異常(一時的な障害・値の不正)
CONFIG_ISSUE_REASONS: Final = frozenset(
    {
        KeyUnavailableReason.NOT_CONFIGURED,
        KeyUnavailableReason.ACCESS_DENIED,
        KeyUnavailableReason.NOT_FOUND,
    }
)
ANOMALY_REASONS: Final = frozenset(
    {
        KeyUnavailableReason.TIMEOUT,
        KeyUnavailableReason.MALFORMED,
        KeyUnavailableReason.ERROR,
    }
)

DEFAULT_CACHE_TTL_SECONDS: Final = 900.0
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final = 2.0
DEFAULT_READ_TIMEOUT_SECONDS: Final = 3.0

_ARN_PATTERN: Final = re.compile(
    r"^arn:aws[a-z-]*:secretsmanager:(?P<region>[a-z0-9-]+):\d{12}:secret:\S+$"
)
#: 英数字 64 文字以上 = 256 ビット以上(template の GenerateSecretString と同じ前提)
_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9]{64,}")
_SECRET_KEYS: Final = frozenset({"key_version", "key"})

_ACCESS_DENIED_CODES: Final = frozenset(
    {"AccessDeniedException", "AccessDenied", "DecryptionFailure", "UnauthorizedOperation"}
)
_NOT_FOUND_CODES: Final = frozenset({"ResourceNotFoundException", "InvalidRequestException"})
_NOT_CONFIGURED_CODES: Final = frozenset({"InvalidParameterException"})


class KeyProviderViolationError(RuntimeError):
    """allowlist 外の操作(get_secret_value 以外)を呼ぼうとした。"""


class _GetSecretValueOnlyClient:
    """`get_secret_value` だけを通す client の proxy。それ以外の属性アクセスは例外。"""

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_client", client)

    def get_secret_value(self, **kwargs: Any) -> Any:
        return self._client.get_secret_value(**kwargs)

    def __getattr__(self, name: str) -> Any:
        # 実在の属性(get_secret_value・_client)以外だけがここへ来る
        raise KeyProviderViolationError("only get_secret_value is allowed")


def _default_client_factory(region: str, connect_timeout: float, read_timeout: float) -> Any:
    """boto3 の client。再試行なし(total_max_attempts 1 = 初回だけ)・接続と読取の timeout つき。

    ★ `max_attempts` は『初回を除く再試行の回数』なので、再試行なしには使えない(1 だと計 2 回呼ぶ)★
    boto3 は遅延 import。
    """
    import boto3  # noqa: PLC0415 - 実際に鍵を読むときだけ必要
    from botocore.config import Config  # noqa: PLC0415

    return boto3.client(
        "secretsmanager",
        region_name=region,
        config=Config(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )


def _region_of(secret_arn: object) -> str | None:
    """ARN の形が正しければ region。不正なら None(AWS を呼ばない)。"""
    if not isinstance(secret_arn, str):
        return None
    match = _ARN_PATTERN.match(secret_arn)
    return match.group("region") if match else None


def _classify(error: Exception) -> KeyUnavailableReason:
    """AWS の呼び出しの失敗を理由コードへ(値・メッセージを見ない。コードの種類だけ)。"""
    if isinstance(error, (ConnectTimeoutError, ReadTimeoutError, TimeoutError)):
        return KeyUnavailableReason.TIMEOUT
    if isinstance(error, (NoCredentialsError, PartialCredentialsError)):
        return KeyUnavailableReason.ACCESS_DENIED
    if isinstance(error, ClientError):
        response = error.response if isinstance(error.response, Mapping) else {}
        detail = response.get("Error")
        code = detail.get("Code") if isinstance(detail, Mapping) else None
        if code in _ACCESS_DENIED_CODES:
            return KeyUnavailableReason.ACCESS_DENIED
        if code in _NOT_FOUND_CODES:
            return KeyUnavailableReason.NOT_FOUND
        if code in _NOT_CONFIGURED_CODES:
            return KeyUnavailableReason.NOT_CONFIGURED
    return KeyUnavailableReason.ERROR


def _parse(response: object) -> KeyMaterial | None:
    """応答の SecretString を検証して KeyMaterial へ。合わなければ None(理由の詳細を出さない)。"""
    try:
        if not isinstance(response, Mapping):
            return None
        secret = response.get("SecretString")
        if not isinstance(secret, str):  # SecretBinary のみ・欠落
            return None
        body = json.loads(secret)
        if not isinstance(body, dict) or set(body) != _SECRET_KEYS:
            return None
        version, key = body["key_version"], body["key"]
        if isinstance(version, bool) or not isinstance(version, int):
            return None
        if not MIN_KEY_VERSION <= version <= MAX_KEY_VERSION:
            return None
        if not isinstance(key, str) or _KEY_PATTERN.fullmatch(key) is None:
            return None
        return KeyMaterial(key_version=version, key=key.encode("ascii"))
    except (ValueError, TypeError, IdentifierRefError):
        return None


def _unavailable(reason: KeyUnavailableReason) -> KeyUnavailable:
    """固定の印を 1 行だけ出して、理由コードを返す(ARN・値・例外のメッセージを出さない)。"""
    if reason in CONFIG_ISSUE_REASONS:
        logger.error("%s reason=%s", CONFIG_ISSUE_MARKER, reason.value)
    else:
        logger.warning("%s reason=%s", ANOMALY_MARKER, reason.value)
    return KeyUnavailable(reason)


class SecretsManagerKeyProvider:
    """Secrets Manager から鍵を読む `KeyProvider`。★ `get()` は例外を送出しない ★"""

    def __init__(
        self,
        *,
        secret_arn: str | None,
        client_factory: Callable[[str, float, float], Any] = _default_client_factory,
        monotonic: Callable[[], float] = time.monotonic,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
    ) -> None:
        self._secret_arn = secret_arn
        self._client_factory = client_factory
        self._monotonic = monotonic
        self._ttl = cache_ttl_seconds
        self._connect_timeout = connect_timeout_seconds
        self._read_timeout = read_timeout_seconds
        # (鍵, 期限)。成功だけを入れる(不変の tuple の代入は原子的。失敗は入れない)
        self._cached: tuple[KeyMaterial, float] | None = None

    def __repr__(self) -> str:
        return "SecretsManagerKeyProvider()"  # ARN を含めない

    def get(self) -> KeyMaterial | KeyUnavailable:
        try:
            return self._get()
        except Exception:  # noqa: BLE001 - 契約: 例外を送出しない。種類も出さない
            return _unavailable(KeyUnavailableReason.ERROR)

    def _get(self) -> KeyMaterial | KeyUnavailable:
        region = _region_of(self._secret_arn)
        if region is None:
            return _unavailable(KeyUnavailableReason.NOT_CONFIGURED)
        cached = self._cached
        if cached is not None and self._monotonic() < cached[1]:
            return cached[0]
        try:
            client = _GetSecretValueOnlyClient(
                self._client_factory(region, self._connect_timeout, self._read_timeout)
            )
        except Exception:  # noqa: BLE001
            return _unavailable(KeyUnavailableReason.ERROR)
        try:
            response = client.get_secret_value(SecretId=self._secret_arn)
        except Exception as error:  # noqa: BLE001 - 分類して畳む
            return _unavailable(_classify(error))
        material = _parse(response)
        if material is None:
            return _unavailable(KeyUnavailableReason.MALFORMED)
        self._cached = (material, self._monotonic() + self._ttl)
        return material


_SHARED: dict[str, SecretsManagerKeyProvider] = {}


def shared_key_provider(secret_arn: str | None) -> SecretsManagerKeyProvider:
    """ARN ごとに process で 1 つの provider(warm な Lambda の実行をまたいでキャッシュを保つ)。

    ARN が不正(未設定・空・形が不正)なら、保持せずに新しい provider を返す(NOT_CONFIGURED を返す)。
    """
    if _region_of(secret_arn) is None:
        return SecretsManagerKeyProvider(secret_arn=secret_arn)
    assert isinstance(secret_arn, str)  # noqa: S101 - _region_of が str を保証
    provider = _SHARED.get(secret_arn)
    if provider is None:
        provider = _SHARED.setdefault(secret_arn, SecretsManagerKeyProvider(secret_arn=secret_arn))
    return provider
