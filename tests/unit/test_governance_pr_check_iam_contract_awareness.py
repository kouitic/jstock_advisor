"""governance_pr_check.pyのIAM contract気付きgateの回帰テスト
(Issue #663 PR-2。AC2/T1-T4)。

意味判定(実際にIAM不足があるか)は行わない。handler/repository層の
変更と、infra/template.yaml・tests/unit/test_infra_*.pyの変更有無という
ファイル一覧の形だけで機械的に判定する(AC3)。
"""

from __future__ import annotations

from scripts.governance_pr_check import check_iam_contract_awareness


def test_t1_handler_only_change_triggers_warning() -> None:
    """T1: handler層のみ変更・template.yaml不変・test_infra_*.py不変
    (#529のcounter-example)でWARNINGが出ること。
    """
    changed = [
        "src/jstock_advisor/lambda_handlers/watchlist_batch_reconciler_handler.py",
    ]
    warnings = check_iam_contract_awareness(changed)
    assert len(warnings) == 1
    assert "#529" in warnings[0]


def test_t1_repository_only_change_triggers_warning() -> None:
    """T1相当: infrastructure/aws・local_repository配下の変更でも同様に
    WARNINGが出ること。
    """
    changed = ["src/jstock_advisor/infrastructure/aws/trade_event_record_repository.py"]
    assert len(check_iam_contract_awareness(changed)) == 1

    changed = [
        "src/jstock_advisor/infrastructure/local_repository/watchlist_repository.py",
    ]
    assert len(check_iam_contract_awareness(changed)) == 1


def test_t2_template_change_suppresses_warning() -> None:
    """T2: template.yamlも変更されている場合はWARNINGが出ないこと。"""
    changed = [
        "src/jstock_advisor/lambda_handlers/watchlist_batch_reconciler_handler.py",
        "infra/template.yaml",
    ]
    assert check_iam_contract_awareness(changed) == []


def test_t3_infra_contract_test_change_suppresses_warning() -> None:
    """T3: tests/unit/test_infra_*.pyが変更されている場合はWARNINGが
    出ないこと。
    """
    changed = [
        "src/jstock_advisor/lambda_handlers/watchlist_batch_reconciler_handler.py",
        "tests/unit/test_infra_issue_529_trade_event_reconciliation_iam.py",
    ]
    assert check_iam_contract_awareness(changed) == []


def test_t4_docs_only_change_does_not_trigger_warning() -> None:
    """T4: handler/repository層を一切変更しないPR(ドキュメントのみ等)では
    WARNINGが出ないこと。
    """
    changed = ["docs/operations_manual.md"]
    assert check_iam_contract_awareness(changed) == []


def test_empty_changed_files_does_not_trigger_warning() -> None:
    """--changed-files-file未指定(空リスト)の場合はgate自体をスキップする。"""
    assert check_iam_contract_awareness([]) == []


def test_unit_test_outside_test_infra_naming_does_not_suppress_warning() -> None:
    """test_infra_*.py以外のunit test(例: handler自身の振る舞いテスト)を
    追加しただけでは、IAM contract testを追加したことにはならないため、
    WARNINGは出続けること(#529のPR #572自身がこのケース。repository/
    service層のテストは追加されたがIAM contract testは追加されなかった)。
    """
    changed = [
        "src/jstock_advisor/lambda_handlers/watchlist_batch_reconciler_handler.py",
        "tests/unit/test_issue_529_trade_event_reconciliation.py",
    ]
    assert len(check_iam_contract_awareness(changed)) == 1
