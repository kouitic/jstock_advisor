"""trial 専用(merge しない): subset に載っていない Markdown を実際に読むテスト。"""

from pathlib import Path


def test_reads_a_markdown_that_is_not_listed() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "README.md").read_text(encoding="utf-8")
