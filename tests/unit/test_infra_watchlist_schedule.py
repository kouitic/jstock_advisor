"""平日毎日起動化(2026-08)対応: infra/template.yamlのSchedule定義を検証する。

CloudFormationの短縮形タグ(!Sub/!GetAtt等)はyaml.safe_load()では解釈できず
専用ローダーの追加は本テストの目的に対して過剰なため、テンプレートの生テキスト
に対する文字列アサーションで検証する(既存のinfra関連テストが無いための新設、
このリポジトリの他のinfra検証は`sam validate --lint`で別途行う)。

対応するユーザー要求のテスト項目:
#1 平日06:00 JSTのNEW_CANDIDATE_SCREENING Scheduleのみ存在すること
#2 旧・土曜07:00 Scheduleが削除されていること
#3 WATCHLIST_MAINTENANCEの独立Scheduleが存在しないこと
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "infra" / "template.yaml"


def _template_text() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _dispatcher_function_block(text: str) -> str:
    """WatchlistDispatcherFunctionリソースブロックのみを切り出す
    (トップレベルの次のリソース定義の直前まで)。"""
    start = text.index("\n  WatchlistDispatcherFunction:\n")
    end = text.index("\n  WatchlistWorkerFunction:\n", start)
    return text[start:end]


# --- テスト#1: 平日06:00 JSTのNEW_CANDIDATE_SCREENING Scheduleのみ存在 -----------


def test_weekday_morning_schedule_exists_with_correct_cron() -> None:
    block = _dispatcher_function_block(_template_text())
    assert "WeekdayMorning:" in block
    assert 'ScheduleExpression: "cron(0 6 ? * MON-FRI *)"' in block
    assert "ScheduleExpressionTimezone: Asia/Tokyo" in block


def test_weekday_morning_schedule_has_no_explicit_job_type_input() -> None:
    """InputでNEW_CANDIDATE_SCREENINGを明示せず、handler側のデフォルト
    (event.get("job_type", "NEW_CANDIDATE_SCREENING"))に委ねる設計であること。
    WeekdayMorningブロック内にInput:行が存在しないことで確認する。"""
    block = _dispatcher_function_block(_template_text())
    weekday_start = block.index("WeekdayMorning:")
    # ブロック末尾(次のイベント定義 or リソースブロック終端)まで
    weekday_block = block[weekday_start : weekday_start + 400]
    assert "Input:" not in weekday_block.split("\n\n")[0]


# --- テスト#2: 旧・土曜07:00 NEW_CANDIDATE_SCREENING Scheduleが削除済み ----------


def test_old_saturday_new_candidate_screening_schedule_is_removed() -> None:
    text = _template_text()
    assert "SaturdayEarlyMorning:" not in text
    assert 'cron(0 7 ? * SAT *)' not in text


# --- テスト#3: WATCHLIST_MAINTENANCEの独立Scheduleが一切存在しない -------------


def test_no_independent_watchlist_maintenance_schedule_exists() -> None:
    text = _template_text()
    assert "SundayMaintenanceReview:" not in text
    assert 'cron(0 7 ? * SUN *)' not in text
    assert '"job_type": "WATCHLIST_MAINTENANCE"' not in text
    assert "'job_type': 'WATCHLIST_MAINTENANCE'" not in text


def test_only_one_schedule_v2_exists_on_dispatcher_function() -> None:
    """WatchlistDispatcherFunctionのEvents配下にScheduleV2が1つだけ
    (WeekdayMorningのみ)であること。"""
    block = _dispatcher_function_block(_template_text())
    assert block.count("Type: ScheduleV2") == 1
