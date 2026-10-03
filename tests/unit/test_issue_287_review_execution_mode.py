"""Issue #287(#70 F-B9): 評価・レビュー系handlerのexecution_mode契約。

## 何を固定するのか

```
4 handler(evaluation / weekly_review / monthly_review / quarterly_review)が
`execution_mode` / `notification_mode` を**黙殺しない**
  -> 対応はせず、指定されたら例外で止める(REJECTS_EXPLICITLY。USER決定 2026-10-03 = A)
  -> 指定が無いとき(EventBridge Schedulerの自動実行。Inputなし)は**従来どおり**
```

★ 修正前は4 handlerとも`execution_mode`を読まず、`{"execution_mode": "VALIDATION"}`で
  手動起動しても完全な本番実行になっていた(評価結果・監査ログの書き込み、週次レビューの
  LINE送信・GitHub Issue起票)。

★ 値を見るテストは、**実際のhandler呼び出し経路**(`module.handler(event, context)`)を通す。
  拒否関数を単体で叩くだけでは、handlerが関数を呼んでいない退行を検出できないため。

★ 検査しているのは、拒否の分岐(a)と、Schedulerの通常実行が変わらないことの構造的な根拠
  (template・分岐・呼び出し位置・既存テスト)であり、**Productionの自然実行**での確認ではない
  (deploy後に4 handlerの自然実行が従来どおり成功し、拒否ログが0件であることで確認する)。
  Productionへのfailure injectionは行わない。fixtureはすべて架空値。
"""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from jstock_advisor.lambda_handlers import (
    evaluation_handler,
    monthly_review_handler,
    quarterly_review_handler,
    weekly_review_handler,
)
from jstock_advisor.lambda_handlers._review_execution_mode import (
    REJECTED_KEYS,
    ReviewExecutionModeNotSupportedError,
    reject_execution_mode,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

#: (handler module, 拒否時に名乗るhandler名, templateのFunction論理ID)
_HANDLERS = (
    (evaluation_handler, "evaluation", "EvaluationFunction"),
    (weekly_review_handler, "weekly review", "WeeklyReviewFunction"),
    (monthly_review_handler, "monthly review", "MonthlyReviewFunction"),
    (quarterly_review_handler, "quarterly review", "QuarterlyReviewFunction"),
)
_MODULES = tuple(m for m, _, _ in _HANDLERS)
_IDS = [m.__name__.rsplit(".", 1)[-1] for m in _MODULES]

_REJECTED_EVENTS = [
    {"execution_mode": "VALIDATION"},
    {"execution_mode": "NORMAL"},
    {"execution_mode": "NOT_A_MODE"},
    {"execution_mode": ""},
    {"notification_mode": "SUPPRESS"},
    {"execution_mode": "VALIDATION", "notification_mode": "SUPPRESS"},
]
_REJECTED_EVENT_IDS = [
    "validation",
    "normal",
    "unknown_value",
    "empty_string_is_still_specified",
    "notification_only",
    "both",
]

#: 拒否されず、本体へ到達すべきevent(Schedulerの自動実行・キーを含まないevent・値がNone)。
_PASS_THROUGH_EVENTS = [
    {},
    {"version": "0", "source": "aws.scheduler", "detail": {}},
    {"unrelated": "x"},
    {"execution_mode": None},
    {"notification_mode": None},
    {"execution_mode": None, "notification_mode": None},
]
_PASS_THROUGH_IDS = [
    "empty_scheduler_event",
    "scheduler_like_without_mode",
    "unrelated_key",
    "execution_mode_none",
    "notification_mode_none",
    "both_none",
]


class _ReachedBodyError(Exception):
    """拒否の分岐を素通りして、handlerの本体(最初の外部依存)まで到達したことを表す番兵。"""


def _raise_reached(*_args: Any, **_kwargs: Any) -> Any:
    raise _ReachedBodyError


def _install_side_effect_recorders(
    module: Any, monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]
) -> list[str]:
    """本体が使う外部依存(config・provider・LINE client・service・書き込み)を記録用へ差し替える。

    呼ばれたら名前を記録して番兵を上げる。拒否が効いていれば1つも呼ばれない。
    """
    calls: list[str] = []
    for name in names:

        def _recorder(*_a: Any, _name: str = name, **_k: Any) -> Any:
            calls.append(_name)
            raise _ReachedBodyError

        monkeypatch.setattr(module, name, _recorder)
    return calls


#: 各handlerが本体で使う、副作用を持ちうる外部依存の名前(モジュール属性)。
_SIDE_EFFECT_NAMES: dict[str, tuple[str, ...]] = {
    "evaluation_handler": (
        "load_config",
        "build_real_provider_bundle",
        "RecommendationEvaluationService",
        "record_run_summary",
        "publish_incident_envelope",
    ),
    "weekly_review_handler": (
        "load_config",
        "build_live_line_client_from_env",
        "WeeklyImprovementReviewService",
    ),
    "monthly_review_handler": ("is_first_saturday_of_month",),
    "quarterly_review_handler": ("is_first_saturday_of_month",),
}


def _short(module: Any) -> str:
    name: str = module.__name__.rsplit(".", 1)[-1]
    return name


# =============================================================================
# 1 handlerが execution_mode / notification_mode を拒否する(実際のhandler経路)
# =============================================================================


@pytest.mark.parametrize("module", _MODULES, ids=_IDS)
@pytest.mark.parametrize("event", _REJECTED_EVENTS, ids=_REJECTED_EVENT_IDS)
def test_handlers_refuse_to_run_when_a_mode_is_specified(
    module: Any, event: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """指定されたら本体へ入る前に拒否の例外で止まる(ValueError でもある)。

    ★ 本体の外部依存を番兵へ差し替えているため、拒否が効いていなければ
      `_ReachedBodyError` が上がり、別の理由で落ちたことを成功と誤認しない。
    """
    _install_side_effect_recorders(module, monkeypatch, _SIDE_EFFECT_NAMES[_short(module)])

    with pytest.raises(ReviewExecutionModeNotSupportedError):
        module.handler(event, object())
    with pytest.raises(ValueError):  # noqa: PT011 - 既存の不正mode指定と同じ型で受けられること
        module.handler(event, object())


@pytest.mark.parametrize("module", _MODULES, ids=_IDS)
def test_a_rejection_causes_no_side_effect_at_all(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 拒否時の副作用が0件であること(config・provider・LINE client・service・監査書き込み)。

    記録用に差し替えた外部依存が1つも呼ばれない = LINE送信・GitHub Issue起票・
    DynamoDB / 監査ログの書き込みへ到達しない。
    """
    calls = _install_side_effect_recorders(module, monkeypatch, _SIDE_EFFECT_NAMES[_short(module)])

    with pytest.raises(ReviewExecutionModeNotSupportedError):
        module.handler({"execution_mode": "VALIDATION"}, object())

    assert calls == []


@pytest.mark.parametrize("module", _MODULES, ids=_IDS)
def test_a_rejection_is_not_swallowed_by_an_unstubbed_handler(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """差し替えなしの実handlerでも拒否される(実際のconfig読み込み等へ進まない)。"""
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    with pytest.raises(ReviewExecutionModeNotSupportedError):
        module.handler({"notification_mode": "SUPPRESS"}, None)


# =============================================================================
# 2 Schedulerの通常実行(指定なし)は従来どおり
# =============================================================================


@pytest.mark.parametrize("module", [evaluation_handler, weekly_review_handler], ids=_IDS[:2])
@pytest.mark.parametrize("event", _PASS_THROUGH_EVENTS, ids=_PASS_THROUGH_IDS)
def test_handlers_with_a_body_are_unchanged_when_no_mode_is_specified(
    module: Any, event: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """指定が無い(または値がNone)eventは拒否されず、従来の本体へ進む。

    ★ 本体の最初の外部依存(`load_config`)を番兵にし、`ReviewExecutionModeNotSupportedError`
      ではなく番兵が上がることで「素通りして従来の処理へ入った」ことを確かめる。
    """
    monkeypatch.setattr(module, "load_config", _raise_reached)

    with pytest.raises(_ReachedBodyError):
        module.handler(event, object())


@pytest.mark.parametrize(
    ("module", "result_key"),
    [
        (monthly_review_handler, "is_monthly_review_day"),
        (quarterly_review_handler, "is_quarterly_review_day"),
    ],
    ids=["monthly_review_handler", "quarterly_review_handler"],
)
@pytest.mark.parametrize("event", _PASS_THROUGH_EVENTS, ids=_PASS_THROUGH_IDS)
def test_monthly_and_quarterly_handlers_return_the_same_result_when_no_mode_is_specified(
    module: Any, result_key: str, event: dict[str, Any]
) -> None:
    """副作用のないhandlerは、従来と同じ戻り値(skipped + 判定日フラグ)を返す。"""
    result = module.handler(event, None)

    assert result["skipped"] is True
    assert set(result) == {"skipped", result_key}


@pytest.mark.parametrize("module", _MODULES, ids=_IDS)
@pytest.mark.parametrize("event", [None, "text", [], 0], ids=["none", "str", "list", "int"])
def test_a_malformed_event_is_not_rejected_by_the_guard(
    module: Any, event: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """eventがdictでないとき、拒否の対象外(従来の挙動を変えない。形式不正は本Issueの範囲外)。"""
    names = _SIDE_EFFECT_NAMES[_short(module)]
    _install_side_effect_recorders(module, monkeypatch, names)

    try:
        module.handler(event, object())
    except ReviewExecutionModeNotSupportedError:
        pytest.fail("dictでないeventが拒否された")
    except _ReachedBodyError:
        pass  # 従来どおり本体へ進んだ


# =============================================================================
# 3 拒否関数そのものの契約
# =============================================================================


def test_the_rejected_keys_are_exactly_the_two_shared_resolver_keys() -> None:
    """共有resolverが読むキーを取りこぼさない(片方だけ増えて黙殺が復活しない)。"""
    from jstock_advisor.lambda_handlers import _execution_mode

    source = inspect.getsource(_execution_mode.resolve_execution_context)
    import re

    resolver_keys = set(re.findall(r'event\.get\("([^"]+)"', source))

    assert resolver_keys, "共有resolverがeventから読むキーを抽出できなかった"
    assert resolver_keys <= set(REJECTED_KEYS)
    assert set(REJECTED_KEYS) == {"execution_mode", "notification_mode"}


def test_the_rejection_error_is_a_value_error() -> None:
    assert issubclass(ReviewExecutionModeNotSupportedError, ValueError)


@pytest.mark.parametrize("module", _MODULES, ids=_IDS)
def test_the_error_log_names_only_the_handler_and_the_keys(
    module: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拒否の前にERRORログが**ちょうど1件**残り、handler名と拒否したキー名だけを含む。

    キーの値・eventのその他の内容は、ログにも例外メッセージにも出さない。
    """
    _install_side_effect_recorders(module, monkeypatch, _SIDE_EFFECT_NAMES[_short(module)])
    handler_name = next(name for m, name, _ in _HANDLERS if m is module)
    event = {
        "execution_mode": "SECRET-VALUE-XYZ",
        "notification_mode": "ANOTHER-VALUE-ABC",
        "other": "PRIVATE-DATA-123",
    }

    with caplog.at_level(logging.ERROR), pytest.raises(ReviewExecutionModeNotSupportedError) as exc:
        module.handler(event, object())

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    text = errors[0].getMessage() + " " + str(exc.value)
    assert handler_name in text
    assert "execution_mode" in text and "notification_mode" in text
    for forbidden in ("SECRET-VALUE-XYZ", "ANOTHER-VALUE-ABC", "PRIVATE-DATA-123", "other"):
        assert forbidden not in text


def test_the_error_log_lists_only_the_keys_that_were_specified(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """片方だけの指定では、その1つだけを名指しする(指定していないキーを挙げない)。"""
    with caplog.at_level(logging.ERROR), pytest.raises(ReviewExecutionModeNotSupportedError):
        reject_execution_mode({"notification_mode": "SUPPRESS"}, handler_name="x")

    message = caplog.records[0].getMessage()
    assert "keys=['notification_mode']" in message


def test_a_non_dict_event_returns_without_logging(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        reject_execution_mode(None, handler_name="x")  # 例外を上げず、何も記録しない
        reject_execution_mode("execution_mode", handler_name="x")
    assert caplog.records == []


# =============================================================================
# 4 呼び出し位置(handlerの最初の文)= 台帳と実装の対応づけ
# =============================================================================


def _handler_first_statement(module: Any) -> ast.stmt:
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    func = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "handler"
    )
    body = list(func.body)
    # docstringだけは「文」に数えない(将来docstringを足しても位置の検査を壊さない)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[0]


@pytest.mark.parametrize(("module", "handler_name", "_logical_id"), _HANDLERS, ids=_IDS)
def test_the_first_statement_of_each_handler_is_the_rejection_call(
    module: Any, handler_name: str, _logical_id: str
) -> None:
    """★ handlerの最初の文が `reject_execution_mode(event, handler_name=<名前>)` であること。

    台帳(REJECTS_EXPLICITLY)のセルを書き換えるだけで「直したことにする」ことと、
    呼び出しがnow取得・config読み込み・LINE client構築・service生成より後ろへずれる
    ことを防ぐ(拒否時の副作用0件の根拠)。
    """
    first = _handler_first_statement(module)

    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    call = first.value
    assert isinstance(call.func, ast.Name) and call.func.id == "reject_execution_mode"
    assert len(call.args) == 1 and isinstance(call.args[0], ast.Name)
    assert call.args[0].id == "event"
    keywords = {kw.arg: kw.value for kw in call.keywords}
    assert set(keywords) == {"handler_name"}
    assert isinstance(keywords["handler_name"], ast.Constant)
    assert keywords["handler_name"].value == handler_name
    # 名前が共通のhelperを指していること(各moduleに別実装を作らない)
    assert module.reject_execution_mode is reject_execution_mode


# =============================================================================
# 5 Schedulerの通常実行が変わらないことの構造的な根拠(template)
# =============================================================================


class _CfnLoader(yaml.SafeLoader):
    """CloudFormationの短縮形(!Ref等)を素朴なdictへ変換するだけの構文解析専用Loader。"""


def _cfn_multi_constructor(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    assert isinstance(node, yaml.MappingNode)  # noqa: S101 - CFNタグはこの3種のみ
    return {tag_suffix: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)  # type: ignore[no-untyped-call]


def _template_resources() -> dict[str, Any]:
    loaded = yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CfnLoader)
    assert isinstance(loaded, dict)
    resources: dict[str, Any] = loaded["Resources"]
    return resources


def _contains_key(node: Any, keys: tuple[str, ...]) -> bool:
    if isinstance(node, dict):
        return any(k in keys for k in node) or any(_contains_key(v, keys) for v in node.values())
    if isinstance(node, list):
        return any(_contains_key(v, keys) for v in node)
    if isinstance(node, str):
        return any(k in node for k in keys)
    return False


@pytest.mark.parametrize(("module", "_name", "logical_id"), _HANDLERS, ids=_IDS)
def test_the_template_never_schedules_these_handlers_with_a_mode(
    module: Any, _name: str, logical_id: str
) -> None:
    """★ 4 handlerの起動元(Events)は、Scheduleのみで、Inputにmodeのキーを含まない。

    Schedulerが `execution_mode` / `notification_mode` を送ることは構造上ない、を
    templateの記述として固定する(将来Inputが足されたら落ちる)。
    """
    resources = _template_resources()
    function = resources[logical_id]
    assert function["Properties"]["Handler"] == f"{module.__name__}.handler"

    events = function["Properties"].get("Events", {})
    assert events, f"{logical_id}: Eventsが無い(自動実行の前提が崩れた)"
    for event_name, event in events.items():
        assert event["Type"] in {"Schedule", "ScheduleV2"}, (
            f"{logical_id}.{event_name}: Schedule以外の起動元は、mode指定のeventを送りうる"
        )
        assert not _contains_key(event["Properties"].get("Input"), REJECTED_KEYS), (
            f"{logical_id}.{event_name}: Inputにmodeのキーがある(自動実行が拒否される)"
        )
