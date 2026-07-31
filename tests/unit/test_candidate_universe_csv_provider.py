from pathlib import Path

import pytest

from jstock_advisor.interfaces.candidate_universe import CandidateUniverseError
from jstock_advisor.providers.candidate_universe.csv_impl import CsvCandidateUniverseProvider


def _write_csv(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_missing_file_raises_candidate_universe_error(tmp_path: Path) -> None:
    provider = CsvCandidateUniverseProvider(tmp_path / "does_not_exist.csv")
    with pytest.raises(CandidateUniverseError):
        provider.get_candidate_universe()


def test_missing_required_column_raises_candidate_universe_error(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "universe.csv", "not_stock_code\nfoo\n")
    provider = CsvCandidateUniverseProvider(path)
    with pytest.raises(CandidateUniverseError):
        provider.get_candidate_universe()


def test_empty_csv_returns_empty_universe_without_error(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "universe.csv", "stock_code\n")
    provider = CsvCandidateUniverseProvider(path)
    result = provider.get_candidate_universe()
    assert result.stock_codes == []
    assert result.raw_row_count == 0


def test_normal_rows_are_normalized_deduplicated_and_validated(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "universe.csv",
        "stock_code,memo\n"
        "7203, トヨタ\n"  # 前後の空白は正規化される
        "7203,重複\n"  # 2回目は重複として除外
        "  ,空白のみ\n"  # strip後に空になる行はraw_row_countにも含めない
        "AB1,短すぎる\n"  # 4桁未満は不正コード
        "9984,ソフトバンクG\n",
    )
    provider = CsvCandidateUniverseProvider(path)
    result = provider.get_candidate_universe()

    assert result.stock_codes == ["7203", "9984"]
    assert result.raw_row_count == 4
    assert result.duplicate_count == 1
    assert result.invalid_code_count == 1


def test_preserves_first_occurrence_order(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "universe.csv", "stock_code\n9984\n7203\n1234\n")
    provider = CsvCandidateUniverseProvider(path)
    result = provider.get_candidate_universe()
    assert result.stock_codes == ["9984", "7203", "1234"]
