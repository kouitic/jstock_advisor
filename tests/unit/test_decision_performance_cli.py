"""cli/decision_performance.pyのcompareコマンドの入力検証テスト
(判定精度向上機能次フェーズ、コードレビュー対応)。

min>maxの範囲不正・A/B範囲重複はいずれもDecisionPerformanceServiceを
呼び出す前にCLI側でエラー終了することを確認する(実ストレージへ触れずに
検証できる)。
"""

from __future__ import annotations

from typer.testing import CliRunner

from jstock_advisor.cli import decision_performance as cli_module

_runner = CliRunner()


def test_compare_rejects_min_greater_than_max_for_group_a() -> None:
    result = _runner.invoke(
        cli_module.app,
        [
            "compare",
            "--score", "timing",
            "--label-a", "a", "--min-a", "50", "--max-a", "20",
            "--label-b", "b", "--min-b", "-100", "--max-b", "-50",
            "--horizon", "60",
        ],
    )
    assert result.exit_code == 1
    assert "比較群A" in result.output


def test_compare_rejects_min_greater_than_max_for_group_b() -> None:
    result = _runner.invoke(
        cli_module.app,
        [
            "compare",
            "--score", "timing",
            "--label-a", "a", "--min-a", "50", "--max-a", "100",
            "--label-b", "b", "--min-b", "50", "--max-b", "20",
            "--horizon", "60",
        ],
    )
    assert result.exit_code == 1
    assert "比較群B" in result.output


def test_compare_rejects_overlapping_ranges() -> None:
    result = _runner.invoke(
        cli_module.app,
        [
            "compare",
            "--score", "timing",
            "--label-a", "a", "--min-a", "20",
            "--label-b", "b", "--max-b", "30",
            "--horizon", "60",
        ],
    )
    assert result.exit_code == 1
    assert "重複" in result.output


def test_compare_rejects_unknown_score_name() -> None:
    result = _runner.invoke(
        cli_module.app,
        [
            "compare",
            "--score", "not_a_score",
            "--label-a", "a", "--min-a", "20",
            "--label-b", "b", "--max-b", "-20",
            "--horizon", "60",
        ],
    )
    assert result.exit_code == 1
    assert "--score" in result.output
