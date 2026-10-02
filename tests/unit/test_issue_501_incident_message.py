"""Issue #501(#132 X-2): 「異常 1 通」の本文の builder と、allowlist(H-30)の不変条件。

確認すること:

    1 本文の形: Issue 本文の例どおり。時刻は JST の「時:分」(UTC → JST の日付境界を含む)。
    2 allowlist が不変条件: 本文の全体が、固定の文型(許可した項目だけ)に一致する。
      ★ 検査そのものの確認: 禁止する値を含む本文を、この検査が拒否する(常に真の検査にしない)。
    3 ★ 禁止する値を、実際に builder へ渡される入力の中へ置く: 内部名を受け取る唯一の入口
      `resolve_incident_job()` へ、識別子・銘柄・所有者・例外文・ARN 等を渡しても、本文に現れない。
    4 型で締める: 自由な文字列を受け取れない(`IncidentNotice` の型と値の検査)。
    5 利用者向けの名称の対応表: 全 Lambda 関数を網羅する(infra/template.yaml と突き合わせる)。
      未知の名前は汎用の名称へ落ち、内部名を出さない。
    6 ネットワーク・ファイル・AWS へ触れない(module の import の検査)。
"""

from __future__ import annotations

import ast
import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from jstock_advisor.domain.notification import incident_message
from jstock_advisor.domain.notification.incident_message import (
    IncidentContent,
    IncidentJob,
    IncidentNotice,
    build_incident_message,
    resolve_incident_content,
    resolve_incident_job,
)
from jstock_advisor.domain.notification.incident_signal import FailureClass

_UTC = dt.UTC
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEADLINE = "⚠️ 本番処理でエラーが発生しました。システム側で調査情報を記録しました。"

# ★ 本文に出してよい job の利用者向けの名称の、完全一致リスト(人がレビューして固定した集合)。
# 検査側の allowlist は、`IncidentJob` から自動生成せず、ここから作る。列挙へ値を足しても、
# この集合を意図して更新しない限り、検査は許容範囲を広げない(列挙の中身は人のレビューを経る)。
_REVIEWED_JOB_LABELS = {
    "BUY_CANDIDATES": "買い候補チェック",
    "HOLDINGS_WATCHLIST": "保有株チェック",
    "DISCLOSURE_CHECK": "開示チェック",
    "EVALUATION": "過去の推奨の評価",
    "WATCHLIST_SCREENING": "ウォッチリスト自動追加",
    "WEEKLY_REVIEW": "週次レビュー",
    "MONTHLY_REVIEW": "月次レビュー",
    "QUARTERLY_REVIEW": "四半期レビュー",
    "LINE_WEBHOOK": "LINE の応答",
    "INCIDENT_NOTIFIER": "異常通知の中継処理",
    "ASYNC_INVOKE_FAILURE": "非同期実行の失敗",
    "OTHER": "その他の処理",
}

# 本文の全体が一致すべき、固定の文型(allowlist)。job の名称は、上の完全一致リストの値だけを許す。
_LABELS = "|".join(re.escape(label) for label in _REVIEWED_JOB_LABELS.values())
_ALLOWLISTED = re.compile(
    rf"{re.escape(_HEADLINE)}\n"
    rf"対象: (?:{_LABELS})\n"
    r"発生時刻: [0-2]\d:[0-5]\d"
    r"(?:\n件数: \d+件)?"
    r"(?:\n連続日数: \d+日)?"
    r"(?:\n継続中: (?:はい|いいえ))?"
)


def _assert_allowlisted(text: str) -> None:
    """本文の全体が allowlist の文型に一致する(1 文字でも外れれば AssertionError)。"""
    assert _ALLOWLISTED.fullmatch(text), f"allowlist 外の本文: {text!r}"


def _notice(job: IncidentJob = IncidentJob.BUY_CANDIDATES, **kwargs: object) -> IncidentNotice:
    at = kwargs.pop("occurred_at", dt.datetime(2026, 8, 3, 23, 3, tzinfo=_UTC))
    return IncidentNotice(job=job, occurred_at=at, **kwargs)  # type: ignore[arg-type]


# --- 1 本文の形 ---------------------------------------------------------------------------


def test_the_message_matches_the_example_in_the_issue() -> None:
    """Issue 本文の例: 対象と発生時刻(UTC 23:03 = JST 08:03)。"""
    text = build_incident_message(_notice())

    assert text == f"{_HEADLINE}\n対象: 買い候補チェック\n発生時刻: 08:03"
    _assert_allowlisted(text)


def test_the_optional_items_are_added_only_when_given() -> None:
    bare = build_incident_message(_notice())
    full = build_incident_message(_notice(failure_count=3, consecutive_days=2, is_ongoing=True))

    assert "件数" not in bare and "連続日数" not in bare and "継続中" not in bare
    assert full.endswith("件数: 3件\n連続日数: 2日\n継続中: はい")
    _assert_allowlisted(bare)
    _assert_allowlisted(full)
    assert build_incident_message(_notice(is_ongoing=False)).endswith("継続中: いいえ")
    assert build_incident_message(_notice(failure_count=0)).endswith("件数: 0件")  # 0 も出す


@pytest.mark.parametrize(
    ("utc", "jst_text"),
    [
        (dt.datetime(2026, 8, 3, 14, 59, tzinfo=_UTC), "23:59"),  # JST の日付が変わる直前
        (dt.datetime(2026, 8, 3, 15, 0, tzinfo=_UTC), "00:00"),  # JST 翌日 00:00
        (dt.datetime(2026, 8, 3, 23, 3, tzinfo=_UTC), "08:03"),
        (dt.datetime(2026, 8, 4, 8, 30, tzinfo=dt.timezone(dt.timedelta(hours=9))), "08:30"),
    ],
)
def test_the_time_is_shown_in_jst(utc: dt.datetime, jst_text: str) -> None:
    text = build_incident_message(_notice(occurred_at=utc))

    assert text.splitlines()[2] == f"発生時刻: {jst_text}"


# --- 1b Issue #724: HANDLED_FAILUREのheadline分岐・「内容」行 -------------------------------

_HANDLED_HEADLINE = "⚠️ 本番処理の一部で問題が発生しました。"


def test_unhandled_failure_body_is_byte_for_byte_unchanged() -> None:
    """契約4: UNHANDLED_FAILURE(既定値)の本文は1バイトも変わらない。"""
    text = build_incident_message(_notice())
    assert text == f"{_HEADLINE}\n対象: 買い候補チェック\n発生時刻: 08:03"
    assert "内容" not in text


def test_handled_failure_uses_neutral_headline_and_adds_content_line() -> None:
    notice = _notice(
        failure_class=FailureClass.HANDLED_FAILURE,
        content=IncidentContent.BUY_CANDIDATES_ANALYSIS_FAILED,
    )
    text = build_incident_message(notice)
    assert text == (
        f"{_HANDLED_HEADLINE}\n対象: 買い候補チェック\n"
        f"内容: {IncidentContent.BUY_CANDIDATES_ANALYSIS_FAILED.value}\n発生時刻: 08:03"
    )


def test_handled_failure_without_content_omits_the_line() -> None:
    """理論上の後方互換ケース(contentが未設定のHANDLED_FAILURE)。"""
    text = build_incident_message(_notice(failure_class=FailureClass.HANDLED_FAILURE))
    assert "内容" not in text
    assert text.startswith(_HANDLED_HEADLINE)


def test_resolve_incident_content_known_reason_code() -> None:
    assert (
        resolve_incident_content("BUY_CANDIDATES_ANALYSIS_FAILED")
        is IncidentContent.BUY_CANDIDATES_ANALYSIS_FAILED
    )
    assert (
        resolve_incident_content("watchlist_queue_backlog")
        is IncidentContent.WATCHLIST_QUEUE_BACKLOG
    )
    assert resolve_incident_content("CloudWatchAlarm") is IncidentContent.CLOUDWATCH_ALARM


def test_resolve_incident_content_unknown_or_non_string_falls_back_to_other() -> None:
    assert resolve_incident_content("some-future-reason-code") is IncidentContent.OTHER
    assert resolve_incident_content(None) is IncidentContent.OTHER
    assert resolve_incident_content(123) is IncidentContent.OTHER


# --- 1c PR #740レビュー是正(MUST F-1): 対応表の網羅性をsrcから機械的に検証する -------------

# 現在実際にfailure_class="HANDLED_FAILURE"を設定している発行元のreason_code
# (grep全数確認・レビュー済み)。このsetは下のtest_actual_handled_failure_reason_codes_*
# が、src自体をASTで再抽出した結果と突き合わせる(人のレビューを経た側と、srcの実体を
# 読んだ側の両方が一致することを固定する)。
_REVIEWED_CURRENT_HANDLED_FAILURE_REASON_CODES = frozenset(
    {
        # buy_candidates_handler.py(_notify_handled_failure_safely呼び出し3箇所)
        "BUY_CANDIDATES_ANALYSIS_FAILED",
        "BUY_CANDIDATES_EVALUATION_RECORD_SAVE_FAILED",
        "BUY_CANDIDATES_NOTIFICATION_OUTCOME_RECORD_UPDATE_FAILED",
        # holdings_watchlist_handler.py(同3箇所)
        "HOLDINGS_WATCHLIST_PORTFOLIO_PRICE_FETCH_FAILED",
        "HOLDINGS_WATCHLIST_EVALUATION_RECORD_SAVE_FAILED",
        "HOLDINGS_WATCHLIST_ANALYSIS_FAILED",
        # evaluation_handler.py(同2箇所。モジュール定数経由)
        "EVALUATION_AGGREGATE_COMMIT_FAILED",
        "EVALUATION_AUDIT_PERSIST_FAILED",
        # watchlist_batch_finalizer.py(envelope辞書に直接記載。2箇所)
        "watchlist_finalizer_repository_add_failed",
        "watchlist_finalizer_unexpected_error_count",
        # watchlist_batch_reconciler_handler.py(_HANDLED_FAILURE_BOUNDARY_METADATA。6件)
        "reconciler_completion_recovery_invoke_failed",
        "reconciler_trade_event_reconciliation_failed",
        "reconciler_finalize_retry_unexpected_error",
        "reconciler_notification_retry_unexpected_error",
        "reconciler_timeout_finalizing_unexpected_error",
        "reconciler_maintenance_trigger_retry_unexpected_error",
    }
)

# 先行登録(現在はenvelopeがfailure_classキー自体を持たない、または
# _normalize_alarm_message()がfailure_classを設定しないため、常にUNHANDLED_FAILURE
# としてのみ到達し、contentは計算されない)。
_RESERVED_OPERATIONAL_TREND_REASON_CODES = frozenset(
    {
        "watchlist_missed_schedule",
        "watchlist_universe_load_failure_streak",
        "watchlist_queue_backlog",
        "watchlist_deletion_zero_streak",
        "buy_candidates_stuck_batch",
        "holdings_watchlist_stuck_batch",
    }
)
_RESERVED_ALARM_REASON_CODES = frozenset({"CloudWatchAlarm"})


def _module_level_string_constants(tree: ast.Module) -> dict[str, str]:
    """モジュールtop-levelの`NAME = "literal"`代入を集める(call引数がNameの場合の解決用)。"""
    constants: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            constants[node.targets[0].id] = node.value.value
    return constants


def _extract_call_reason_codes(path: Path, func_name: str, arg_index: int) -> set[str]:
    """`func_name(...)`呼び出しの`arg_index`番目の位置引数を文字列として解決する。

    Constant(文字列リテラル直書き)はそのまま、Name(モジュール定数経由)は
    top-level代入から解決する。いずれでもない場合は、staticに解決できない値が
    紛れ込んでいるということなので、guardの前提が崩れている合図としてAssertionErrorにする
    (黙ってスキップしない)。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = _module_level_string_constants(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != func_name or len(node.args) <= arg_index:
            continue
        arg = node.args[arg_index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.add(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in constants:
            found.add(constants[arg.id])
        else:
            raise AssertionError(
                f"{path.name}: {func_name}の{arg_index}番目の引数を静的に解決できない"
                f"(動的な値の可能性。guardの前提が崩れている)"
            )
    return found


def _extract_envelope_dict_reason_codes(path: Path) -> set[str]:
    """`{"failure_class": "HANDLED_FAILURE", "reason_code": "...", ...}`型の
    辞書リテラルから、同じ辞書内のreason_codeを抽出する。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {
            k.value: v
            for k, v in zip(node.keys, node.values, strict=True)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        fc = pairs.get("failure_class")
        rc = pairs.get("reason_code")
        if (
            isinstance(fc, ast.Constant)
            and fc.value == "HANDLED_FAILURE"
            and isinstance(rc, ast.Constant)
            and isinstance(rc.value, str)
        ):
            found.add(rc.value)
    return found


def _extract_boundary_metadata_reason_codes(path: Path, dict_name: str) -> set[str]:
    """`dict_name = {"KEY": ("TYPE", "reason_code"), ...}`型のmodule定数から、
    各valueタプルの2要素目(reason_code)を抽出する。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        target_matches = (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == dict_name for t in node.targets)
        ) or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == dict_name
        )
        if not target_matches:
            continue
        assert isinstance(node.value, ast.Dict), f"{dict_name}はdict literalである前提"
        for value in node.value.values:
            assert isinstance(value, ast.Tuple) and len(value.elts) == 2, (
                f"{dict_name}の値は(type, reason_code)の2要素tupleである前提"
            )
            reason_elt = value.elts[1]
            assert isinstance(reason_elt, ast.Constant) and isinstance(reason_elt.value, str)
            found.add(reason_elt.value)
    return found


def _actual_handled_failure_reason_codes() -> set[str]:
    """src全体から、現在実際にfailure_class="HANDLED_FAILURE"で発行される
    reason_codeを機械的に抽出する(test_every_lambda_function_in_the_template_
    has_an_entryと同型の、allowlistとsrcの実体を突き合わせるguard)。"""
    handlers_dir = _REPO_ROOT / "src" / "jstock_advisor" / "lambda_handlers"
    services_dir = _REPO_ROOT / "src" / "jstock_advisor" / "services"

    found: set[str] = set()
    for filename in ("buy_candidates_handler.py", "holdings_watchlist_handler.py"):
        found |= _extract_call_reason_codes(
            handlers_dir / filename, "_notify_handled_failure_safely", 1
        )
    found |= _extract_call_reason_codes(
        handlers_dir / "evaluation_handler.py", "_notify_handled_failure_safely", 1
    )
    found |= _extract_envelope_dict_reason_codes(services_dir / "watchlist_batch_finalizer.py")
    found |= _extract_boundary_metadata_reason_codes(
        handlers_dir / "watchlist_batch_reconciler_handler.py",
        "_HANDLED_FAILURE_BOUNDARY_METADATA",
    )
    return found


def test_actual_handled_failure_reason_codes_match_the_reviewed_set() -> None:
    """srcから機械的に抽出した「現在実際にHANDLED_FAILUREとして発行される
    reason_code」が、人のレビューを経た集合と一致する(発行元が増減したら、
    レビュー側〔_REVIEWED_CURRENT_HANDLED_FAILURE_REASON_CODES〕を更新する
    まで赤くなる)。"""
    assert _actual_handled_failure_reason_codes() == _REVIEWED_CURRENT_HANDLED_FAILURE_REASON_CODES


def test_reason_code_to_content_covers_every_actual_handled_failure_reason_code() -> None:
    """MUST F-1是正: 現在実際にHANDLED_FAILUREとして発行されるreason_codeが、
    1件でも_REASON_CODE_TO_CONTENTから欠けていたら赤くなる。"""
    content_keys = set(incident_message._REASON_CODE_TO_CONTENT)
    assert _actual_handled_failure_reason_codes() <= content_keys


def test_reason_code_to_content_has_no_keys_beyond_actual_and_reserved() -> None:
    """対応表の全キーが、(現在実際にHANDLED_FAILUREとして発行されるreason_code)
    ∪ (明示的にレビュー済みの先行登録key)のいずれかである。未知のキーが紛れ込んだ
    場合に検知する。"""
    content_keys = set(incident_message._REASON_CODE_TO_CONTENT)
    expected = (
        _actual_handled_failure_reason_codes()
        | _RESERVED_OPERATIONAL_TREND_REASON_CODES
        | _RESERVED_ALARM_REASON_CODES
    )
    assert content_keys == expected


# ★ IncidentJob(_REVIEWED_JOB_LABELS)の先例と同じ手法: 列挙から動的に作らず、
# 人が手で書いた完全一致リストにする(列挙へ値を足しても、この集合は自動では
# 広がらない)。
_REVIEWED_INCIDENT_CONTENT_LABELS = {
    "BUY_CANDIDATES_ANALYSIS_FAILED": "銘柄分析の一部が完了しませんでした",
    "BUY_CANDIDATES_EVALUATION_RECORD_SAVE_FAILED": "買い候補の判定結果の記録保存に失敗しました",
    "BUY_CANDIDATES_NOTIFICATION_OUTCOME_RECORD_UPDATE_FAILED": "通知結果の記録更新に失敗しました",
    "HOLDINGS_WATCHLIST_PORTFOLIO_PRICE_FETCH_FAILED": (
        "保有資産見積もりに必要な株価取得の一部に失敗しました"
    ),
    "HOLDINGS_WATCHLIST_EVALUATION_RECORD_SAVE_FAILED": (
        "保有銘柄の判定結果の記録保存に失敗しました"
    ),
    "HOLDINGS_WATCHLIST_ANALYSIS_FAILED": "保有銘柄分析の一部が完了しませんでした",
    "EVALUATION_AGGREGATE_COMMIT_FAILED": "評価結果の集計確定に失敗しました",
    "EVALUATION_AUDIT_PERSIST_FAILED": "評価処理の記録保存に失敗しました",
    "WATCHLIST_FINALIZER_REPOSITORY_ADD_FAILED": "ウォッチリストへの銘柄追加の一部に失敗しました",
    "WATCHLIST_FINALIZER_UNEXPECTED_ERROR_COUNT": (
        "ウォッチリスト判定処理で想定外のエラーが発生しました"
    ),
    "RECONCILER_COMPLETION_RECOVERY_INVOKE_FAILED": "処理完了の復旧処理の呼び出しに失敗しました",
    "RECONCILER_TRADE_EVENT_RECONCILIATION_FAILED": "売買記録の整合性確認処理に失敗しました",
    "RECONCILER_FINALIZE_RETRY_UNEXPECTED_ERROR": "処理完了の再試行で想定外のエラーが発生しました",
    "RECONCILER_NOTIFICATION_RETRY_UNEXPECTED_ERROR": "通知の再試行で想定外のエラーが発生しました",
    "RECONCILER_TIMEOUT_FINALIZING_UNEXPECTED_ERROR": (
        "処理時間超過後の後処理で想定外のエラーが発生しました"
    ),
    "RECONCILER_MAINTENANCE_TRIGGER_RETRY_UNEXPECTED_ERROR": (
        "メンテナンス処理の再試行で想定外のエラーが発生しました"
    ),
    "WATCHLIST_MISSED_SCHEDULE": "定時実行が行われなかった可能性があります",
    "WATCHLIST_UNIVERSE_LOAD_FAILURE_STREAK": "銘柄ユニバースの取得が複数日連続で失敗しています",
    "WATCHLIST_QUEUE_BACKLOG": "処理待ちが滞留しています",
    "WATCHLIST_DELETION_ZERO_STREAK": "ウォッチリストからの削除が複数日連続で発生していません",
    "BUY_CANDIDATES_STUCK_BATCH": "買い候補チェックの処理が完了せず滞留している可能性があります",
    "HOLDINGS_WATCHLIST_STUCK_BATCH": "保有株チェックの処理が完了せず滞留している可能性があります",
    "CLOUDWATCH_ALARM": "システムの監視アラームが検知されました",
    "OTHER": "技術的な問題を検知しました",
}


def test_the_incident_content_enum_is_exactly_the_reviewed_set() -> None:
    """★ IncidentJobの先例と同じ手法: 列挙の中身を完全一致リストで固定する。
    列挙へ値を足す・値を書き換えると、この検査が赤くなる。"""
    assert {member.name: member.value for member in IncidentContent} == (
        _REVIEWED_INCIDENT_CONTENT_LABELS
    )


# --- 2 allowlist が不変条件(検査そのものの確認) -----------------------------------------------


@pytest.mark.parametrize(
    "leaked",
    [
        # 許可した文型に、余計な行・語を足した本文は、検査が拒否する(常に真の検査ではない)。
        f"{_HEADLINE}\n対象: 買い候補チェック\n発生時刻: 08:03\n原因: Traceback (most recent)",
        f"{_HEADLINE}\n対象: buy-candidates\n発生時刻: 08:03",  # 内部の関数名
        f"{_HEADLINE}\n対象: 買い候補チェック(0000)\n発生時刻: 08:03",  # 銘柄コードの混入
        f"{_HEADLINE}\n対象: 買い候補チェック\n発生時刻: 08:03\nowner-a",  # 所有者
        f"{_HEADLINE}\n対象: 買い候補チェック\n発生時刻: 8時3分",  # 文型の外の時刻表記
    ],
)
def test_the_allowlist_check_rejects_a_leaked_message(leaked: str) -> None:
    with pytest.raises(AssertionError):
        _assert_allowlisted(leaked)


# --- 3 禁止する値を、実際に builder へ渡される入力の中へ置く ------------------------------------

# 禁止する値の例(すべて架空の値。実在の識別子・ARN・account ID ではない)。
_FORBIDDEN = {
    "stock_code": "0000",
    "owner": "owner-a",
    "holding_id": "owner-a#0000",
    "exception": "KeyError: 'secret_field' at line 42",
    "stack_trace": 'Traceback (most recent call last):\n  File "/var/task/app.py", line 1',
    "arn": "ar" + "n:aw" + "s:lambda:ap-northeast-1:000000000000:function:x",  # 架空(分割して記述)
    "account_id": "000000000000",
    "request_id": "00000000-0000-0000-0000-000000000000",
    "dynamodb_key": "batch_id=watchlist-00000000T000000-00000000",
    "internal_path": "/var/task/jstock_advisor/services/x.py",
    "secret": "LINE_CHANNEL_ACCESS_TOKEN=xxxxxxxx",
    "holdings": "保有株数=100 取得価格=1234.5",
}


@pytest.mark.parametrize("kind", sorted(_FORBIDDEN))
def test_forbidden_values_passed_as_the_internal_name_never_reach_the_message(kind: str) -> None:
    """内部名を受け取る唯一の入口へ、禁止する値を渡しても、本文に現れない。"""
    value = _FORBIDDEN[kind]

    for internal_name in (value, f"jstock-advisor-{value}", f"buy-candidates {value}"):
        job = resolve_incident_job(internal_name)
        text = build_incident_message(_notice(job))

        assert job is IncidentJob.OTHER  # 部分一致で既知の job へ引かれない
        assert value not in text
        assert internal_name not in text
        _assert_allowlisted(text)


def test_a_known_name_with_extra_text_is_not_treated_as_known() -> None:
    """既知の名前に文字列を足しても(接頭辞・接尾辞)、既知の job にはならず、汎用へ落ちる。"""
    assert resolve_incident_job("buy-candidates") is IncidentJob.BUY_CANDIDATES
    assert resolve_incident_job("buy-candidates-extra") is IncidentJob.OTHER
    assert resolve_incident_job("x-buy-candidates") is IncidentJob.OTHER


@pytest.mark.parametrize("value", [None, 123, b"buy-candidates", ["buy-candidates"], object()])
def test_a_non_string_name_falls_back_to_the_generic_job(value: object) -> None:
    """異常の通知を組み立てる経路で、入力の不備によって通知が失われない(例外にしない)。"""
    assert resolve_incident_job(value) is IncidentJob.OTHER


def test_the_incident_job_enum_is_exactly_the_reviewed_set() -> None:
    """★ 列挙の中身を、完全一致リストで固定する(他の allowlist の型パターンと同じ手法)。

    `IncidentJob` の値は、そのまま本文に出る。列挙へ値を足す・値を書き換えると、この検査が赤に
    なり、`_REVIEWED_JOB_LABELS`(人のレビューを経た集合)を意図して更新するまで通らない。
    検査側の allowlist(`_LABELS`)もこの集合から作っているため、列挙を足しても許容範囲は
    自動では広がらない(本文の allowlist 検査も、未レビューの値を持つ本文を拒否する)。
    """
    assert {job.name: job.value for job in IncidentJob} == _REVIEWED_JOB_LABELS


def test_every_mapped_job_is_a_member_of_the_reviewed_set() -> None:
    """対応表の写し先も、レビュー済みの集合に含まれる(対応表から未レビューの値へ引けない)。"""
    reviewed = set(_REVIEWED_JOB_LABELS.values())

    assert {job.value for job in incident_message._INTERNAL_NAME_TO_JOB.values()} <= reviewed


def test_the_allowlist_rejects_a_message_with_an_unreviewed_job_label() -> None:
    """未レビューの名称(たとえば列挙へ足された値)を持つ本文は、allowlist 検査が拒否する。"""
    unreviewed = "所有者Aの保有株情報"  # 架空の値。列挙へ足されても、検査は許容範囲を広げない
    text = f"{_HEADLINE}\n対象: {unreviewed}\n発生時刻: 08:03"

    with pytest.raises(AssertionError):
        _assert_allowlisted(text)


# --- 4 型で締める --------------------------------------------------------------------------


def test_the_notice_has_no_free_text_field() -> None:
    """自由な文字列を持たない: 受け取れる項目は、列挙・時刻・件数・日数・真偽値だけ。"""
    import dataclasses

    fields = {f.name: str(f.type) for f in dataclasses.fields(IncidentNotice)}

    assert fields == {
        "job": "IncidentJob",
        "occurred_at": "dt.datetime",
        "failure_count": "int | None",
        "consecutive_days": "int | None",
        "is_ongoing": "bool | None",
        # Issue #724: HANDLED_FAILUREのときだけ「内容」行とheadline分岐に使う。
        # いずれも列挙(FailureClass/IncidentContent)であり自由文字列ではない。
        "failure_class": "FailureClass",
        "content": "IncidentContent | None",
    }
    assert not any("str" in annotation for annotation in fields.values())


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"job": "買い候補チェック"}, TypeError),  # 文字列を job として渡せない(列挙のみ)
        ({"job": "buy-candidates"}, TypeError),
        ({"occurred_at": "2026-08-03T23:03:00Z"}, TypeError),
        ({"occurred_at": dt.datetime(2026, 8, 3, 23, 3)}, ValueError),  # naive は UTC 扱いしない
        ({"failure_count": True}, TypeError),  # bool は件数ではない
        ({"failure_count": "3"}, TypeError),
        ({"failure_count": 1.5}, TypeError),
        ({"failure_count": -1}, ValueError),
        ({"consecutive_days": True}, TypeError),
        ({"consecutive_days": "2"}, TypeError),
        ({"consecutive_days": -1}, ValueError),
        ({"is_ongoing": 1}, TypeError),  # 真偽値だけ
        ({"is_ongoing": "yes"}, TypeError),
    ],
)
def test_the_notice_rejects_anything_outside_the_allowed_types(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    base: dict[str, object] = {
        "job": IncidentJob.BUY_CANDIDATES,
        "occurred_at": dt.datetime(2026, 8, 3, 23, 3, tzinfo=_UTC),
    }
    with pytest.raises(error):
        IncidentNotice(**{**base, **kwargs})  # type: ignore[arg-type]


# --- 5 利用者向けの名称の対応表 -----------------------------------------------------------------


def test_every_lambda_function_in_the_template_has_an_entry() -> None:
    """全 Lambda 関数が対応表にある(関数が増えたら、ここが赤くなり、対応表を更新する合図)。"""
    template = (_REPO_ROOT / "infra" / "template.yaml").read_text(encoding="utf-8")
    functions = set(re.findall(r'FunctionName: !Sub "\$\{AWS::StackName\}-([a-z0-9-]+)"', template))

    # 監視対象の Lambda は 12 本(#132)+ incident-notifier(#503)+ worker 2 本(#533)
    assert len(functions) == 15
    assert functions == set(incident_message._INTERNAL_NAME_TO_JOB)


def _load_template_resources() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    loaded = yaml.load(
        (_REPO_ROOT / "infra" / "template.yaml").read_text(encoding="utf-8"), Loader=_Loader
    )
    return loaded["Resources"]


def _referenced_logical_id(value: object) -> str | None:
    """`!GetAtt X.Arn`(ロード後は{"Fn::GetAtt": "X.Arn"})からXを取り出す。"""
    if isinstance(value, dict) and isinstance(value.get("Fn::GetAtt"), str):
        return value["Fn::GetAtt"].split(".")[0]
    return None


def _terminal_failure_sink_queue_names(resources: dict[str, Any]) -> set[str]:
    """真正のDLQ(終端の失敗の受け皿)のQueueName(スタック名の前置を除いたもの)を、
    命名規約
    (例: `-dlq`サフィックス)ではなく実際の構造から特定する(Issue #349サブちゃん
    レビューF1: 名前の綴りに依存すると、別の命名規約〔例: `-deadletter`〕で
    追加された5本目のDLQを検知できない。#505 F1と同じ「内容で特定する」考え方)。

    「終端の失敗の受け皿」とは、次のいずれかを満たし、かつ自身はRedrivePolicyを
    持たない(さらに先へリダイレクトされない=redrive chainの終端である)Queueで
    ある(サブちゃんレビューR1: 判定基準を「配線されている(参照されている)」
    だけにすると、まだどこからも配線されていない孤立DLQを拾えない退行が
    あったため、和集合にした)。

        (a) 他のQueueのRedrivePolicy.deadLetterTargetArnの宛先、または
            LambdaのEventInvokeConfig.DestinationConfig.OnFailure.Destinationの
            宛先として参照されている(配線済み)
        (b) MessageRetentionPeriod=1209600(14日。運用調査用の長期保持。既存4本
            すべてがこの値を明示的に持つ。中間キューはVisibilityTimeout/
            RedrivePolicyのみでMessageRetentionPeriodを明示しない=既定4日)
    """
    queue_logical_ids = {
        name for name, r in resources.items() if r.get("Type") == "AWS::SQS::Queue"
    }
    has_own_redirect = {
        name for name in queue_logical_ids if "RedrivePolicy" in resources[name]["Properties"]
    }

    referenced_as_failure_target: set[str] = set()
    for resource in resources.values():
        props = resource.get("Properties", {})
        redrive = props.get("RedrivePolicy")
        if isinstance(redrive, dict):
            target = _referenced_logical_id(redrive.get("deadLetterTargetArn"))
            if target:
                referenced_as_failure_target.add(target)
        on_failure = (
            props.get("EventInvokeConfig", {}).get("DestinationConfig", {}).get("OnFailure", {})
        )
        if isinstance(on_failure, dict):
            target = _referenced_logical_id(on_failure.get("Destination"))
            if target:
                referenced_as_failure_target.add(target)

    long_retention = {
        name
        for name in queue_logical_ids
        if resources[name]["Properties"].get("MessageRetentionPeriod") == 1209600
    }

    terminal_ids = ((referenced_as_failure_target & queue_logical_ids) | long_retention) - (
        has_own_redirect
    )
    # !Sub "${AWS::StackName}-xxx" は {"Fn::Sub": "${AWS::StackName}-xxx"} へロードされる。
    names: set[str] = set()
    for name in terminal_ids:
        queue_name = resources[name]["Properties"]["QueueName"]
        if isinstance(queue_name, dict) and isinstance(queue_name.get("Fn::Sub"), str):
            names.add(queue_name["Fn::Sub"].removeprefix("${AWS::StackName}-"))
    return names


def test_every_terminal_dlq_in_the_template_has_an_entry() -> None:
    """全ての終端DLQ(redrive chainの終端。命名規約ではなく構造で特定する。
    Issue #349)が対応表にある。

    増減したら、ここが赤くなり `_QUEUE_NAME_TO_JOB` を更新する合図になる
    (test_every_lambda_function_in_the_template_has_an_entry と同型のガード)。
    """
    resources = _load_template_resources()
    stripped = _terminal_failure_sink_queue_names(resources)

    assert len(stripped) == 4  # #349: WatchlistTerminalFailure / BuyCandidateTerminalFailure /
    # HoldingsWatchlistTerminalFailure / AsyncInvokeFailure の4本
    assert stripped == set(incident_message._QUEUE_NAME_TO_JOB)


@pytest.mark.parametrize(
    ("internal_name", "label"),
    [
        ("buy-candidates", "買い候補チェック"),
        ("jstock-advisor-buy-candidates", "買い候補チェック"),  # スタック名の前置を除く
        ("holdings-watchlist", "保有株チェック"),
        ("disclosure-check", "開示チェック"),
        ("evaluation", "過去の推奨の評価"),
        ("watchlist-dispatcher", "ウォッチリスト自動追加"),
        ("watchlist-worker", "ウォッチリスト自動追加"),
        ("watchlist-terminal-failure-handler", "ウォッチリスト自動追加"),
        ("watchlist-batch-reconciler", "ウォッチリスト自動追加"),
        ("weekly-review", "週次レビュー"),
        ("monthly-review", "月次レビュー"),
        ("quarterly-review", "四半期レビュー"),
        ("line-webhook", "LINE の応答"),
        ("incident-notifier", "異常通知の中継処理"),
        # Issue #349: SQS DLQ(キュー名)からの解決。
        ("watchlist-terminal-failure-dlq", "ウォッチリスト自動追加"),
        ("jstock-advisor-watchlist-terminal-failure-dlq", "ウォッチリスト自動追加"),
        ("buy-candidate-terminal-failure-dlq", "買い候補チェック"),
        ("holdings-watchlist-terminal-failure-dlq", "保有株チェック"),
        # ★ BuyCandidatesFunction/HoldingsWatchlistFunctionの両方が共有するため、
        # どちらか一方の既存jobへ誤って割り当てず専用の名称を持つ(USER決定)。
        ("async-invoke-failure-dlq", "非同期実行の失敗"),
    ],
)
def test_internal_names_map_to_user_facing_labels(internal_name: str, label: str) -> None:
    text = build_incident_message(_notice(resolve_incident_job(internal_name)))

    assert f"対象: {label}\n" in text
    assert internal_name not in text  # 内部名は本文に出ない
    _assert_allowlisted(text)


def test_async_invoke_failure_dlq_is_not_attributed_to_either_sharing_function() -> None:
    """AsyncInvokeFailureDLQはBuyCandidatesFunction/HoldingsWatchlistFunctionの両方が
    共有する(Issue #318)。メッセージ単体からはどちらの関数由来か区別できないため、
    どちらか一方の既存jobへ誤って割り当てないこと(USER決定)を固定する。"""
    job = resolve_incident_job("async-invoke-failure-dlq")

    assert job is IncidentJob.ASYNC_INVOKE_FAILURE
    assert job is not IncidentJob.BUY_CANDIDATES
    assert job is not IncidentJob.HOLDINGS_WATCHLIST
    assert job is not IncidentJob.OTHER


def test_an_unknown_job_falls_back_to_the_generic_label_without_the_internal_name() -> None:
    text = build_incident_message(_notice(resolve_incident_job("some-new-internal-job")))

    assert "対象: その他の処理\n" in text
    assert "some-new-internal-job" not in text


# --- 6 ネットワーク・ファイル・AWS へ触れない ---------------------------------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "datetime",
    "dataclasses",
    "enum",
    "jstock_advisor.domain.jst",
    # Issue #724: FailureClassのみ(列挙)。incident_signal.py自身もpure domain
    # module(ネットワーク・ファイル・AWSに触れない)であることを確認済み。
    "jstock_advisor.domain.notification.incident_signal",
}


def test_the_module_imports_nothing_that_touches_network_files_or_aws() -> None:
    tree = ast.parse(Path(incident_message.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported <= _ALLOWED_IMPORTS, imported - _ALLOWED_IMPORTS
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    called = {n.func.id for n in calls if isinstance(n.func, ast.Name)}
    assert not called & {"open", "print", "exec", "eval", "__import__"}
