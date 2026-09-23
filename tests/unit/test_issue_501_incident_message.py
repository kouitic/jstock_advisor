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

import pytest

from jstock_advisor.domain.notification import incident_message
from jstock_advisor.domain.notification.incident_message import (
    IncidentJob,
    IncidentNotice,
    build_incident_message,
    resolve_incident_job,
)

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

    assert len(functions) == 13  # 監視対象の Lambda は 12 本(#132)+ incident-notifier(#503)
    assert functions == set(incident_message._INTERNAL_NAME_TO_JOB)


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
    ],
)
def test_internal_names_map_to_user_facing_labels(internal_name: str, label: str) -> None:
    text = build_incident_message(_notice(resolve_incident_job(internal_name)))

    assert f"対象: {label}\n" in text
    assert internal_name not in text  # 内部名は本文に出ない
    _assert_allowlisted(text)


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
