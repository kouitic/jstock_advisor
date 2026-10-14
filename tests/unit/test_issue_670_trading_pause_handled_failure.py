"""Issue #670(HF-5): 売買一時停止の設定(TradingPauseConfig)の取得失敗を、USERへ通知する。

## 何を固定するのか

```
TradingPauseService.is_buy_sell_paused() が、設定の取得に失敗して安全側(一時停止)へ倒れたとき
  C1  従来のfail-safe(戻り値 True = 売買登録を止める)・ログは変えない
  C2  技術的な取得失敗(例外)のときだけ、HF-0契約(#665)でHANDLED_FAILUREを1件通知する
  C3  意図的な一時停止(保存済みで pause_buy_sell=True)・通常(False)・未初期化(None)では通知しない
  C4  通知の重複を防ぐ: 1つのinstanceにつき最大1回(同じ応答の中で4回呼ばれても1回)。
      通知の発行が失敗・timeoutしても再試行しない
  C5  通知の発行が失敗しても、戻り値・LINEの応答を変えない(fail-soft)
  C6  通知の発行は待ちの上限を持つ(timeout_seconds。LINE webhookの同期応答を待たせない)
  C7  INCIDENT_NOTIFICATION_TOPIC_ARNが無い環境(運用者のCLI・ローカル・template反映前)では送らない
  C8  共通契約(#665)は加法のみ: timeout_seconds未指定の既存の呼び出し元は従来と完全に同じ
```

## ★ 「内容」行の文言は PROVISIONAL(暫定)

「内容」文の文言について、USERの承認は無い(USERが承認したのは、暫定の文面で実装してよく、deploy前に
USERが文面を承認する、という進め方のみ)。本テストが固定するのは「その member が解決される」ことと
「暫定であることがソースに明記されていること」であり、文言そのものが最終であるとは主張しない。

## 検査していない範囲

```
・実際のSNSへのpublish(publish関数・boto3 clientをスタブへ差し替える)・LINE送信(スタブ)
・Production(deploy前は、LINEに届かない。Lambdaの環境変数・sns:Publishはtemplateの配線 =
  test_infra_issue_725_incident_publish_wiring.py が静的に固定する)
・LINE webhookの実際の応答時間(timeoutの設定値の固定と、publish呼び出しが1回であることまで)
```

fixtureは架空値のみ。Production・AWSへは触れない。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.notification import incident_message
from jstock_advisor.domain.notification.incident_fingerprint import (
    IncidentFingerprintInput,
    compute_fingerprint,
)
from jstock_advisor.domain.notification.incident_message import (
    IncidentContent,
    IncidentJob,
    IncidentNotice,
    build_incident_message,
    resolve_incident_content,
    resolve_incident_job,
)
from jstock_advisor.domain.notification.incident_signal import FailureClass
from jstock_advisor.lambda_handlers import incident_notifier_handler
from jstock_advisor.services import incident_envelope_publisher
from jstock_advisor.services import trading_pause_service as mod
from jstock_advisor.services.trading_pause_service import TradingPauseService
from tests.support.iam_contract_helpers import resources

_NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.UTC)
_TOPIC_ENV = "INCIDENT_NOTIFICATION_TOPIC_ARN"
_REASON = "LINE_WEBHOOK_TRADING_PAUSE_CONFIG_FETCH_FAILED"


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Lambda相当の環境(topicのenvあり)で、発行された envelope を記録する(publish は呼ばない)。"""
    envelopes: list[dict[str, Any]] = []
    monkeypatch.setenv(_TOPIC_ENV, "synthetic-topic")
    monkeypatch.setattr(
        mod,
        "publish_incident_envelope",
        lambda e, **_kw: envelopes.append(e),
    )
    return envelopes


def _fail_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_store_dir: Path | None = None) -> None:
        raise RuntimeError("synthetic fetch failure: secret-detail-123")

    monkeypatch.setattr(mod._repo, "get", _boom)


def _service(store_dir: Path, **kwargs: Any) -> TradingPauseService:
    return TradingPauseService(store_dir=store_dir, **kwargs)


def _envelope() -> dict[str, Any]:
    return {
        "source": "trading_pause_config",
        "job_name": "line-webhook",
        "failure_stage": "TRADING_PAUSE_CONFIG_FETCH",
        "failure_type": "FETCH_FAILED",
        "reason_code": _REASON,
        "occurred_at": _NOW.isoformat(),
        "failure_class": "HANDLED_FAILURE",
    }


# =============================================================================
# C1 / C2: 取得失敗 -> fail-safe は不変、通知は1件
# =============================================================================


def test_c1_c2_fetch_failure_keeps_the_fail_safe_and_publishes_one_notice(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    _fail_fetch(monkeypatch)

    paused = _service(store_dir).is_buy_sell_paused(_NOW)

    assert paused is True  # 従来のfail-safe(売買登録を止める側)は変えない
    assert published == [_envelope()]


def test_c1_the_failure_log_is_unchanged(
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _fail_fetch(monkeypatch)

    with caplog.at_level(logging.ERROR):
        _service(store_dir).is_buy_sell_paused(_NOW)

    assert any(
        "TradingPauseConfigの取得に失敗しました。安全側(一時停止)として扱います" in r.getMessage()
        for r in caplog.records
    )


def test_c1_the_fail_safe_does_not_depend_on_the_notification(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通知の設定(env・引数)の有無にかかわらず、取得失敗なら True。"""
    _fail_fetch(monkeypatch)
    monkeypatch.delenv(_TOPIC_ENV, raising=False)
    assert _service(store_dir).is_buy_sell_paused(_NOW) is True
    monkeypatch.setenv(_TOPIC_ENV, "synthetic-topic")
    assert _service(store_dir, notify_fetch_failures=False).is_buy_sell_paused(_NOW) is True


def test_c2_without_now_the_wall_clock_is_used_for_occurred_at(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """既存の呼び出し元(引数なし)のため、now は省略可能。省略時は現在時刻(UTC)。"""
    _fail_fetch(monkeypatch)

    assert _service(store_dir).is_buy_sell_paused() is True

    [envelope] = published
    occurred_at = dt.datetime.fromisoformat(envelope["occurred_at"])
    assert occurred_at.tzinfo is not None
    assert abs((dt.datetime.now(dt.UTC) - occurred_at).total_seconds()) < 60


# =============================================================================
# C3: 通常・意図的な一時停止では通知しない
# =============================================================================


def test_c3_an_intentional_pause_does_not_notify(
    store_dir: Path, published: list[dict[str, Any]]
) -> None:
    service = _service(store_dir)
    service.init_config(pause_buy_sell=True, updated_by="owner-a", change_reason="test")

    assert service.is_buy_sell_paused(_NOW) is True
    assert published == []


def test_c3_a_normal_not_paused_state_does_not_notify(
    store_dir: Path, published: list[dict[str, Any]]
) -> None:
    service = _service(store_dir)
    service.init_config(pause_buy_sell=False, updated_by="owner-a", change_reason="test")

    assert service.is_buy_sell_paused(_NOW) is False
    assert published == []


def test_c3_an_uninitialized_record_does_not_notify(
    store_dir: Path, published: list[dict[str, Any]]
) -> None:
    """レコード未作成(例外ではない)は従来どおり「停止していない」。通知もしない。"""
    assert _service(store_dir).is_buy_sell_paused(_NOW) is False
    assert published == []


# =============================================================================
# C4: 重複通知の防止(1 instance につき最大1回。失敗しても再試行しない)
# =============================================================================


def test_c4_repeated_calls_in_one_instance_publish_once(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """conversation_service.py は1つの応答の中で is_buy_sell_paused() を最大4箇所から呼ぶ。"""
    _fail_fetch(monkeypatch)
    service = _service(store_dir)

    results = [service.is_buy_sell_paused(_NOW) for _ in range(4)]

    assert results == [True, True, True, True]
    assert len(published) == 1


def test_c4_a_failed_publish_is_not_retried_in_the_same_instance(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def _raise(_envelope: dict[str, Any], **_kw: Any) -> None:
        calls.append(1)
        raise TimeoutError("synthetic publish timeout")

    monkeypatch.setenv(_TOPIC_ENV, "synthetic-topic")
    monkeypatch.setattr(mod, "publish_incident_envelope", _raise)
    _fail_fetch(monkeypatch)
    service = _service(store_dir)

    results = [service.is_buy_sell_paused(_NOW) for _ in range(3)]

    assert results == [True, True, True]
    assert len(calls) == 1  # 失敗・timeoutでも、同じ応答の中で待ちが何度も起きない


def test_c4_a_new_instance_notifies_again(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """重複の抑止は instance 内。別の応答(別の instance)での再通知は、通知側の
    fingerprint による抑止(#502)が担う(本 service は状態を永続化しない)。"""
    _fail_fetch(monkeypatch)

    _service(store_dir).is_buy_sell_paused(_NOW)
    _service(store_dir).is_buy_sell_paused(_NOW)

    assert len(published) == 2


# =============================================================================
# C5: 通知の失敗は握る(fail-soft)
# =============================================================================


@pytest.mark.parametrize(
    "error",
    [RuntimeError("synthetic sns failure secret-detail-123"), TimeoutError("synthetic timeout")],
)
def test_c5_a_publish_failure_does_not_change_the_result_or_raise(
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    monkeypatch.setenv(_TOPIC_ENV, "synthetic-topic")

    def _raise(_envelope: dict[str, Any], **_kw: Any) -> None:
        raise error

    monkeypatch.setattr(mod, "publish_incident_envelope", _raise)
    _fail_fetch(monkeypatch)

    with caplog.at_level(logging.WARNING):
        paused = _service(store_dir).is_buy_sell_paused(_NOW)

    assert paused is True
    warnings = [r.getMessage() for r in caplog.records if "failed to publish" in r.getMessage()]
    assert len(warnings) == 1
    assert all("secret-detail" not in w for w in warnings)  # 例外のメッセージは出さない


# =============================================================================
# C6: 待ちの上限
# =============================================================================


def test_c6_the_publish_is_called_with_a_bounded_timeout(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setenv(_TOPIC_ENV, "synthetic-topic")
    monkeypatch.setattr(mod, "publish_incident_envelope", lambda _e, **kw: seen.append(kw))
    _fail_fetch(monkeypatch)

    _service(store_dir).is_buy_sell_paused(_NOW)

    assert seen == [{"timeout_seconds": mod._PUBLISH_TIMEOUT_SECONDS}]  # 名前つきの定数を参照


def test_c6_the_worst_case_wait_is_within_a_tenth_of_the_lambda_timeout() -> None:
    """★ 根拠つきの定数(サブちゃんSHOULD-1)★ 最悪の待ち(接続 + 読取)は、LINE webhook関数の
    Timeout(templateから読む。値をコピーしない)の1割以内。値を変えるとこのテストが一緒に動く。"""
    timeout = float(resources()["LineWebhookFunction"]["Properties"]["Timeout"])
    assert timeout == 30  # 前提の確認(変わったら、根拠を見直す合図)
    assert mod._PUBLISH_WORST_CASE_SECONDS == 2 * mod._PUBLISH_TIMEOUT_SECONDS
    assert 0 < mod._PUBLISH_WORST_CASE_SECONDS <= timeout * 0.1


class _FakeSns:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> dict[str, Any]:
        self.published.append(kwargs)
        return {"MessageId": "synthetic"}


def _capture_boto3_client(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], _FakeSns]:
    clients: list[dict[str, Any]] = []
    fake = _FakeSns()

    def _client(service: str, **kwargs: Any) -> _FakeSns:
        clients.append({"service": service, **kwargs})
        return fake

    monkeypatch.setenv(_TOPIC_ENV, "arn:aws:sns:ap-northeast-1:000000000000:synthetic")
    monkeypatch.setattr(incident_envelope_publisher.boto3, "client", _client)
    return clients, fake


def test_c6_c8_with_a_timeout_the_sns_client_gets_a_bounded_config_without_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients, fake = _capture_boto3_client(monkeypatch)

    incident_envelope_publisher.publish_incident_envelope(_envelope(), timeout_seconds=1.5)

    [client_args] = clients
    config = client_args["config"]
    assert client_args["service"] == "sns"
    assert config.connect_timeout == 1.5
    assert config.read_timeout == 1.5
    # botocoreのmax_attemptsは「初回を除く再試行回数」。total_max_attemptsで総試行回数を1に固定する
    assert config.retries == {"total_max_attempts": 1}
    assert len(fake.published) == 1


def test_c8_without_a_timeout_the_existing_callers_behave_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout_seconds 未指定なら、boto3 client は config なしで作られる(従来と同一)。"""
    clients, fake = _capture_boto3_client(monkeypatch)

    incident_envelope_publisher.publish_incident_envelope(_envelope())

    assert clients == [{"service": "sns"}]
    assert len(fake.published) == 1


def test_c8_the_allowlist_still_fails_closed_with_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients, fake = _capture_boto3_client(monkeypatch)
    bad = {**_envelope(), "stock_code": "0000"}

    with pytest.raises(ValueError):
        incident_envelope_publisher.publish_incident_envelope(bad, timeout_seconds=1.5)

    assert fake.published == []


# =============================================================================
# C7: topic が無い環境では送らない
# =============================================================================


def test_c7_without_the_topic_env_nothing_is_published(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.delenv(_TOPIC_ENV, raising=False)
    monkeypatch.setattr(mod, "publish_incident_envelope", lambda e, **_kw: calls.append(e))
    _fail_fetch(monkeypatch)

    assert _service(store_dir).is_buy_sell_paused(_NOW) is True
    assert calls == []


def test_c7_a_caller_can_opt_out_of_the_notification(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    _fail_fetch(monkeypatch)

    assert _service(store_dir, notify_fetch_failures=False).is_buy_sell_paused(_NOW) is True
    assert published == []


# =============================================================================
# envelope の内容(allowlist・識別子を含めない)
# =============================================================================


def test_the_envelope_has_only_allowlisted_keys_and_no_exception_text(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    _fail_fetch(monkeypatch)

    _service(store_dir).is_buy_sell_paused(_NOW)

    [envelope] = published
    assert set(envelope) <= incident_envelope_publisher.INCIDENT_ENVELOPE_ALLOWLIST
    text = json.dumps(envelope, ensure_ascii=False)
    assert "secret-detail" not in text and "synthetic fetch failure" not in text
    assert "Traceback" not in text and "RuntimeError" not in text
    assert envelope["failure_class"] == "HANDLED_FAILURE"  # GitHub Issueは自動起票されない(HF-0)


# =============================================================================
# LINE の本文・fingerprint(notifier 側の解決)
# =============================================================================


def test_the_reason_code_resolves_to_its_own_content_sentence() -> None:
    content = resolve_incident_content(_REASON)
    assert content is IncidentContent.LINE_WEBHOOK_TRADING_PAUSE_CONFIG_FETCH_FAILED
    assert content is not IncidentContent.OTHER


def test_the_job_resolves_to_the_existing_display_name() -> None:
    """表示名は既存の「LINE の応答」(新しい表示名は作らない)。"""
    assert resolve_incident_job("line-webhook") is IncidentJob.LINE_WEBHOOK
    assert IncidentJob.LINE_WEBHOOK.value == "LINE の応答"


def test_the_content_sentence_is_marked_provisional_in_the_source() -> None:
    """★ 「内容」文が暫定(USER の承認なし)であることが、ソースに明記されている。"""
    source = Path(incident_message.__file__).read_text(encoding="utf-8")
    member_line = source.index("LINE_WEBHOOK_TRADING_PAUSE_CONFIG_FETCH_FAILED = ")
    preceding = source[max(0, member_line - 900) : member_line]

    assert "PROVISIONAL" in preceding
    assert "USERの承認は無い" in preceding


def test_the_content_says_it_is_a_technical_failure_not_an_intentional_pause() -> None:
    """意図的な一時停止ではなく、技術的な取得失敗による安全側の停止であることが文面で分かる。"""
    sentence = IncidentContent.LINE_WEBHOOK_TRADING_PAUSE_CONFIG_FETCH_FAILED.value
    assert "取得できなかった" in sentence
    assert "安全側" in sentence


def test_the_line_body_shows_the_display_name_and_the_content_without_internal_names(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    _fail_fetch(monkeypatch)
    _service(store_dir).is_buy_sell_paused(_NOW)

    [envelope] = published
    signal = incident_notifier_handler._normalize_internal_message(envelope, _NOW)
    assert signal.failure_class is FailureClass.HANDLED_FAILURE
    body = build_incident_message(
        IncidentNotice(
            job=resolve_incident_job(signal.job_name),
            occurred_at=signal.occurred_at,
            failure_class=signal.failure_class,
            content=resolve_incident_content(signal.error_type),
        )
    )
    assert "対象: LINE の応答\n" in body
    assert "内容: " in body
    assert "システム側で調査情報を記録しました" not in body  # 恒久記録を示唆する固定文言は出さない
    # 内部の名前・reason code・段階名は本文に出ない
    for internal in ("line-webhook", "LINE_WEBHOOK", "TRADING_PAUSE", "CONFIG_FETCH"):
        assert internal not in body


def test_the_notice_fingerprint_is_distinct_from_the_other_handled_failures() -> None:
    this = IncidentFingerprintInput(
        environment="test",
        job_name="line-webhook",
        failure_stage="TRADING_PAUSE_CONFIG_FETCH",
        failure_type="FETCH_FAILED",
        error_type=_REASON,
        error_message=_REASON,
    )
    other = IncidentFingerprintInput(
        environment="test",
        job_name="holdings-watchlist",
        failure_stage="KILL_SWITCH_FETCH",
        failure_type="FETCH_FAILED",
        error_type="HOLDINGS_WATCHLIST_KILL_SWITCH_FETCH_FAILED",
        error_message="HOLDINGS_WATCHLIST_KILL_SWITCH_FETCH_FAILED",
    )
    assert compute_fingerprint(this) != compute_fingerprint(other)


# =============================================================================
# C4 の前提: 「instance ごと = LINE の1リクエストごと」(サブちゃんSHOULD-2)
# =============================================================================

_HANDLER = Path(__file__).resolve().parents[2] / (
    "src/jstock_advisor/lambda_handlers/line_webhook_handler.py"
)


def _calls_inside_functions(tree: ast.Module, name: str) -> tuple[int, int]:
    """name(...) の呼び出しを、(関数の中, 関数の外 = module 直下・class 本体・既定引数)で数える。"""
    inside = outside = 0

    def walk(node: ast.AST, in_function: bool) -> None:
        nonlocal inside, outside
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called == name:
                if in_function:
                    inside += 1
                else:
                    outside += 1
        for child in ast.iter_child_nodes(node):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                # 既定引数・デコレータは定義時(= module 読込時)に評価される = 関数の外
                body_ids = {id(n) for stmt in node.body for n in ast.walk(stmt)}
                walk(child, in_function or id(child) in body_ids)
            else:
                walk(child, in_function)

    walk(tree, False)
    return inside, outside


def test_c4_the_router_is_built_per_request_not_at_module_level() -> None:
    """★ 重複抑止の粒度の前提 ★ dedup は TradingPauseService の instance ごと。その instance は、
    handler の中で build_line_event_router() が ConversationService() → TradingPauseService() を
    新しく作ることで『1回のLINEリクエストごと』になっている。将来、router を module 直下へ移す
    (warm コンテナで使い回す)最適化が入ると、dedup が『コンテナの寿命の間 1 回』に変わり、
    本物の障害が何時間も通知されなくなる。その変更をこのテストが止める。"""
    tree = ast.parse(_HANDLER.read_text(encoding="utf-8"))

    inside, outside = _calls_inside_functions(tree, "build_line_event_router")

    assert inside >= 1  # 呼び出しが存在する(名前の変更で素通りしない)
    assert outside == 0  # module 直下・class 本体・既定引数・デコレータには無い


def test_c4_the_router_guard_detects_a_module_level_call() -> None:
    """検査そのものの確認: module 直下の呼び出しを数えられる。"""
    tree = ast.parse(
        "router = build_line_event_router()\n\n"
        "def handler():\n    return build_line_event_router()\n"
    )
    assert _calls_inside_functions(tree, "build_line_event_router") == (1, 1)
    tree = ast.parse("def handler(r=build_line_event_router()):\n    return 1\n")
    assert _calls_inside_functions(tree, "build_line_event_router") == (0, 1)


def test_c4_the_trading_pause_service_is_created_in_the_service_constructor_not_at_import() -> None:
    """ConversationService の構成時(= router の構築時)にだけ TradingPauseService が作られる。"""
    path = Path(mod.__file__).resolve().parents[1] / "services" / "conversation_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    inside, outside = _calls_inside_functions(tree, "TradingPauseService")

    assert inside >= 1
    assert outside == 0


def test_c4_two_router_builds_get_independent_trading_pause_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 挙動で固定(サブちゃん SHOULD-1)★ 重複抑止の単位 = TradingPauseService の instance。
    build_line_event_router() を 2 回呼ぶと別々の instance になる(= リクエストごとに作り直される)。
    関数の中で module 変数へ cache する・@lru_cache を付ける、といった AST の検査をすり抜ける
    最適化も、ここで落ちる(同じ instance が返るため)。"""
    from jstock_advisor.services.line_event_router import build_line_event_router

    first = build_line_event_router()
    second = build_line_event_router()

    assert first is not second
    assert first.conversation_service is not second.conversation_service
    assert (
        first.conversation_service._trading_pause  # noqa: SLF001 - 粒度の前提を測る
        is not second.conversation_service._trading_pause  # noqa: SLF001
    )


# =============================================================================
# 通知の発行元の到達性(サブちゃん SHOULD-2。job 名 "line-webhook" の固定の根拠を固定する)
# =============================================================================

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"


def _module_files() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in _PACKAGE_ROOT.rglob("*.py"):
        parts = list(path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _direct_imports(module: str, path: Path, known: dict[str, Path]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    base = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = base[: len(base) - (node.level - 1)]
                prefix = ".".join(anchor + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            targets = [prefix] + [f"{prefix}.{a.name}" for a in node.names]
        else:
            continue
        for target in targets:
            parts = target.split(".")
            for i in range(1, len(parts) + 1):
                candidate = ".".join(parts[:i])
                if candidate in known:
                    found.add(candidate)
    return found


def _reaches(root: str, target: str, known: dict[str, Path]) -> bool:
    seen: set[str] = set()
    stack = [root]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        if module == target:
            return True
        stack.extend(_direct_imports(module, known[module], known) - seen)
    return False


def test_c4_only_the_line_webhook_handler_reaches_the_trading_pause_service() -> None:
    """job 名を `line-webhook` に固定した根拠(本 service の本番の呼び出し元は LINE webhook だけ)。
    #725 で 9 関数すべてに通知用の環境変数が入ったため、環境変数の有無は防波堤にならない。
    別の handler がこの service へ到達するようになったら、job 名を引数にするなどの見直しが要る。"""
    known = _module_files()
    target = "jstock_advisor.services.trading_pause_service"
    handlers = sorted(m for m in known if m.startswith("jstock_advisor.lambda_handlers."))
    assert len(handlers) > 15  # 根を取れている(空振りでない)

    reaching = [m for m in handlers if _reaches(m, target, known)]

    assert reaching == ["jstock_advisor.lambda_handlers.line_webhook_handler"]


def test_c7_every_cli_construction_of_the_service_opts_out_of_notifications() -> None:
    """★ 『CLI では通知しない』のコード上の保証(サブちゃん SHOULD-3)★ 今は呼び出し経路と環境変数の
    不在で成り立っているが、将来 CLI から is_buy_sell_paused() を呼ぶ・環境変数が入る変更でも、
    CLI は通知しない。cli/ の TradingPauseService() の生成はすべて notify_fetch_failures=False。"""
    constructions = 0
    for path in sorted((_PACKAGE_ROOT / "cli").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "TradingPauseService":
                continue
            constructions += 1
            keywords = {k.arg: k.value for k in node.keywords}
            value = keywords.get("notify_fetch_failures")
            assert isinstance(value, ast.Constant) and value.value is False, (
                path.name,
                node.lineno,
            )
    assert constructions >= 3  # 検査が空振りでない
