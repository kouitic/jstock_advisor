"""cli/holding_decision.pyのcompareコマンドの表示テスト(レビュー指摘F2対応)。

Issue #258 受入条件(3): 初回評価としての除外件数は0件でも必ず表示する
(黙って捨てない)。`CompareSummary`が件数を保持することは
test_holding_decision_compare_service.pyで固定済みだが、それが実際に
CLIの標準出力へ表示されることは別途検証が要る(excluded==0のときだけ
表示を省く、という結線ミスでもサービス側のテストは全て通ってしまうため)。

`tests/conftest.py`のautouse fixture(`_isolated_default_store_dir`)により、
本テストが使う既定storeは自動的にtmp_pathへ隔離される(実データへは触れない)。
"""

from __future__ import annotations

from typer.testing import CliRunner

from jstock_advisor.cli import holding_decision as cli_module

_runner = CliRunner()

_ARGS = ["compare", "--stock-code", "2914", "--source", "mock"]


def test_compare_prints_summary_line_even_when_excluded_count_is_zero() -> None:
    first = _runner.invoke(cli_module.app, _ARGS)
    assert first.exit_code == 0
    assert "■ 集計" in first.output

    # 同一storeへの2回目呼び出し: baseline作成済みのためfirst_evaluationが
    # Falseになり、除外件数は0件になる(test_holding_decision_compare_service.py
    # のtest_run_compare_wires_first_evaluation_flag_onto_the_returned_rowで
    # 確認済みの実際の状態遷移)。
    second = _runner.invoke(cli_module.app, _ARGS)
    assert second.exit_code == 0
    assert "初回評価として0件を既定で除外" in second.output


def test_compare_prints_summary_line_when_excluded_count_is_nonzero() -> None:
    result = _runner.invoke(cli_module.app, _ARGS)
    assert result.exit_code == 0
    assert "■ 集計" in result.output
    assert "初回評価として1件を既定で除外" in result.output
