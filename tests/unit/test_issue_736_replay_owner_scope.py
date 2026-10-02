"""Issue #736: `holding-decision backtest` の replay モードを、指定 owner の結果だけに絞る。

#579 で live モードと `--stock-code` 省略時の列挙は `--owner` で絞られたが、replay の再生そのものは
所有者で絞られず、`--owner B` を指定しても他の所有者の行が混ざっていた。USER 決定(2026-10-03。
U-1 = B)に従い、replay も holding_id を用いて指定 owner の結果だけに絞る。

絞り込みの規則(MANAGER 判断 Q1 / Q2。所有者移行の既存の規則を適用する)
  HoldingDecisionResult   holding_id の所有者部分。owner 対応前の旧形式(区切り無し)は
                          DEFAULT_OWNER の所有。
                          区切りが 2 つ以上の不正な形式は、どの owner にも属さない(除外 + 警告ログ)
  旧方式 Recommendation    owner。未設定(owner 対応前)は DEFAULT_OWNER の所有。
                          対応付け・単独の行とも、同じ規則で絞る

fixture は架空値のみ(owner は "owner-a" / "owner-b")。ローカル JSON ストアのみ。
Production・AWS へは触れない。

## このテストが検査している範囲 / していない範囲

している    run_history_replay の owner による絞り込み
            (HoldingDecisionResult の行・対応付け・単独の行)と、
            CLI の `--owner` の既定と受け渡し、help / 運用手順書の記載の更新。
していない  ・live モード(#579 で固定済み)
            ・Production のデータの実態(owner 対応前の旧形式が残っているかは未観測)
            ・Recommendation の `owner` と `holding_id` が食い違うデータ
              (owner を優先し、holding_id は見ない)
"""

from __future__ import annotations

import datetime as dt
import inspect
import logging
from pathlib import Path

import pytest

from jstock_advisor.cli import holding_decision as holding_decision_cli
from jstock_advisor.domain.entities.owner import (
    DEFAULT_OWNER,
    InvalidOwnerError,
    build_holding_id,
)
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.migrations.conversions import DEFAULT_MIGRATION_OWNER
from jstock_advisor.services.holding_decision_backtest_service import (
    BacktestRow,
    LegacyRecommendationMatchMethod,
    run_history_replay,
)
from tests.unit.test_holding_decision_backtest_service import _hd_result, _recommendation

_CODE = "2914"
_START = dt.date(2026, 6, 1)
_END = dt.date(2026, 6, 30)
_T_A = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
_T_B = dt.datetime(2026, 6, 15, 9, 0, tzinfo=dt.UTC)
_T_LEGACY = dt.datetime(2026, 6, 15, 10, 0, tzinfo=dt.UTC)
_OWNER_A = "owner-a"
_OWNER_B = "owner-b"


@pytest.fixture
def repos(tmp_path: Path) -> tuple[HoldingDecisionResultRepository, RecommendationRepository]:
    store_dir = tmp_path / "local_store"
    return HoldingDecisionResultRepository(store_dir), RecommendationRepository(store_dir)


def _save_result(
    hd_repo: HoldingDecisionResultRepository,
    evaluated_at: dt.datetime,
    holding_id: str,
    result_id: str,
) -> None:
    result = _hd_result(_CODE, evaluated_at, result_id=result_id, should_notify=False)
    hd_repo.save(result.model_copy(update={"holding_id": holding_id}))


def _save_rec(
    rec_repo: RecommendationRepository,
    recommended_at: dt.datetime,
    rec_id: str,
    owner: str | None,
) -> None:
    rec = _recommendation(_CODE, recommended_at, recommendation_id=rec_id)
    if owner is not None:
        rec = rec.model_copy(update={"owner": owner, "holding_id": build_holding_id(owner, _CODE)})
    rec_repo.save(rec)


def _replay(
    repos: tuple[HoldingDecisionResultRepository, RecommendationRepository], owner: str
) -> list[BacktestRow]:
    hd_repo, rec_repo = repos
    return run_history_replay(
        [_CODE],
        _START,
        _END,
        owner,
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )


def _times(rows: list[BacktestRow]) -> list[dt.datetime]:
    return [row.evaluated_at for row in rows]


def _two_owners(repos) -> None:  # type: ignore[no-untyped-def]
    """同じ銘柄を 2 人の owner が持ち、それぞれ結果と旧方式 Recommendation がある(近接する時刻)。"""
    hd_repo, rec_repo = repos
    _save_result(hd_repo, _T_A, build_holding_id(_OWNER_A, _CODE), "r-a")
    _save_result(hd_repo, _T_B, build_holding_id(_OWNER_B, _CODE), "r-b")
    _save_rec(rec_repo, _T_A + dt.timedelta(minutes=1), "rec-a", _OWNER_A)
    _save_rec(rec_repo, _T_B + dt.timedelta(minutes=1), "rec-b", _OWNER_B)


# --- T1: 2 owner が同じ銘柄 ----------------------------------------------------------


def test_t1_replay_returns_only_the_requested_owners_rows(repos) -> None:  # type: ignore[no-untyped-def]
    _two_owners(repos)
    rows_a = _replay(repos, _OWNER_A)
    rows_b = _replay(repos, _OWNER_B)
    assert _times(rows_a) == [_T_A]
    assert _times(rows_b) == [_T_B]
    # 自分の旧方式 Recommendation とだけ対応付く(取り違えない)
    assert rows_a[0].legacy_match_method == LegacyRecommendationMatchMethod.NEAREST_TIMESTAMP.value
    assert rows_b[0].legacy_match_method == LegacyRecommendationMatchMethod.NEAREST_TIMESTAMP.value


def test_t1_default_owner_sees_only_its_own_rows(repos) -> None:  # type: ignore[no-untyped-def]
    """--owner 省略(= CLI が DEFAULT_OWNER へ解決)では、他の owner の行は混ざらない。"""
    _two_owners(repos)
    hd_repo, _ = repos
    _save_result(hd_repo, _T_LEGACY, build_holding_id(DEFAULT_OWNER, _CODE), "r-default")
    assert _times(_replay(repos, DEFAULT_OWNER)) == [_T_LEGACY]
    assert _times(_replay(repos, _OWNER_A)) == [_T_A]


# --- T2: 旧形式・不正な holding_id -----------------------------------------------------


def test_t2_legacy_format_holding_id_belongs_to_the_default_owner(repos) -> None:  # type: ignore[no-untyped-def]
    """owner 対応前の旧形式(区切り無し = stock_code そのもの)は DEFAULT_OWNER の所有(Q1)。"""
    hd_repo, _ = repos
    _save_result(hd_repo, _T_LEGACY, _CODE, "r-legacy")
    assert _times(_replay(repos, DEFAULT_OWNER)) == [_T_LEGACY]
    assert _replay(repos, _OWNER_A) == []


def test_t2_the_legacy_owner_rule_matches_the_migration_rule() -> None:
    """旧形式の帰属先(DEFAULT_OWNER)が、所有者移行の規則(DEFAULT_MIGRATION_OWNER)とずれない。"""
    assert DEFAULT_OWNER == DEFAULT_MIGRATION_OWNER


def test_t2_malformed_holding_id_is_excluded_from_every_owner_with_a_count_only_warning(
    repos, caplog: pytest.LogCaptureFixture
) -> None:  # type: ignore[no-untyped-def]
    hd_repo, _ = repos
    malformed = "dup#dup#" + _CODE  # 区切りが 2 つ以上 = 不正(識別子らしき値を含める)
    _save_result(hd_repo, _T_A, malformed, "r-bad")
    with caplog.at_level(logging.WARNING):
        rows_default = _replay(repos, DEFAULT_OWNER)
        rows_a = _replay(repos, _OWNER_A)
    assert rows_default == [] and rows_a == []  # 例外で止まらず、どの owner にも出ない
    warnings = [r for r in caplog.records if "invalid owner format" in r.getMessage()]
    assert len(warnings) == 2  # replay 1 回につき 1 件
    assert all("1 holding decision results" in r.getMessage() for r in warnings)
    assert all("dup" not in r.getMessage() for r in warnings)  # holding_id は出さない


# --- T3: 旧方式 Recommendation --------------------------------------------------------


def test_t3_other_owners_legacy_recommendation_does_not_appear_as_a_standalone_row(repos) -> None:  # type: ignore[no-untyped-def]
    """対応する結果が無い旧方式 Recommendation は単独の行になるが、owner で絞る。"""
    _, rec_repo = repos
    _save_rec(rec_repo, _T_LEGACY, "rec-b-only", _OWNER_B)
    assert _replay(repos, _OWNER_A) == []
    assert _times(_replay(repos, _OWNER_B)) == [_T_LEGACY]


def test_t3_legacy_recommendation_without_owner_belongs_to_the_default_owner(repos) -> None:  # type: ignore[no-untyped-def]
    _, rec_repo = repos
    _save_rec(rec_repo, _T_LEGACY, "rec-unset", None)
    assert _times(_replay(repos, DEFAULT_OWNER)) == [_T_LEGACY]
    assert _replay(repos, _OWNER_A) == []


def test_t3_a_result_is_never_matched_with_another_owners_recommendation(repos) -> None:  # type: ignore[no-untyped-def]
    """同じ銘柄・近接した時刻でも、別 owner の旧方式 Recommendation とは対応付けない。"""
    hd_repo, rec_repo = repos
    _save_result(hd_repo, _T_A, build_holding_id(_OWNER_A, _CODE), "r-a")
    _save_rec(rec_repo, _T_A + dt.timedelta(minutes=1), "rec-b", _OWNER_B)
    rows = _replay(repos, _OWNER_A)
    assert len(rows) == 1  # b の Recommendation は単独の行にもならない
    assert rows[0].legacy_recommendation_created is not True
    assert rows[0].legacy_recommendation_type == "UNKNOWN_NO_MATCH"
    assert rows[0].legacy_match_method == LegacyRecommendationMatchMethod.UNKNOWN_NO_MATCH.value
    # owner-b から見ると、自分の Recommendation だけが(単独の行として)出る
    assert _times(_replay(repos, _OWNER_B)) == [_T_A + dt.timedelta(minutes=1)]


# --- T4: 一方が他方の前方部分になる owner の組(PR #756 の SHOULD S-1)--------------------
#
# validate_owner が拒否するのは「空」「長すぎる」「区切り文字を含む」の 3 つだけなので、
# 既定の owner(DEFAULT_OWNER)を前方に含む別の owner も有効な入力である。owner の比較が
# 前方一致へ退行すると、--owner 省略(= 既定の owner)の replay へ別の owner の行が混ざる
# (#736 / #579 が塞ごうとしている方向と逆の誤り)。完全一致であることを固定する。

_PREFIX_PAIRS = [
    pytest.param(_OWNER_A, _OWNER_A + "2", id="owner-a_and_owner-a2"),
    pytest.param(DEFAULT_OWNER, DEFAULT_OWNER + "2", id="default_owner_and_its_extension"),
]


@pytest.mark.parametrize(("short", "long"), _PREFIX_PAIRS)
def test_t4_owners_where_one_is_a_prefix_of_the_other_do_not_see_each_others_results(
    repos, short: str, long: str
) -> None:  # type: ignore[no-untyped-def]
    hd_repo, _ = repos
    _save_result(hd_repo, _T_A, build_holding_id(short, _CODE), "r-short")
    _save_result(hd_repo, _T_B, build_holding_id(long, _CODE), "r-long")
    assert _times(_replay(repos, short)) == [_T_A]
    assert _times(_replay(repos, long)) == [_T_B]


@pytest.mark.parametrize(("short", "long"), _PREFIX_PAIRS)
def test_t4_owners_where_one_is_a_prefix_of_the_other_do_not_see_each_others_recommendations(
    repos, short: str, long: str
) -> None:  # type: ignore[no-untyped-def]
    _, rec_repo = repos
    _save_rec(rec_repo, _T_A, "rec-short", short)
    _save_rec(rec_repo, _T_B, "rec-long", long)
    assert _times(_replay(repos, short)) == [_T_A]
    assert _times(_replay(repos, long)) == [_T_B]


def test_t4_the_default_owner_does_not_see_a_longer_owner_that_starts_with_it(repos) -> None:  # type: ignore[no-untyped-def]
    """--owner 省略(= 既定の owner)の replay に、それを前方に含む別の owner の行は混ざらない。"""
    hd_repo, rec_repo = repos
    longer = DEFAULT_OWNER + "2"
    _save_result(hd_repo, _T_A, _CODE, "r-legacy-format")  # 旧形式 = 既定の owner の所有
    _save_result(hd_repo, _T_B, build_holding_id(longer, _CODE), "r-longer")
    _save_rec(rec_repo, _T_LEGACY, "rec-longer", longer)
    assert _times(_replay(repos, DEFAULT_OWNER)) == [_T_A]
    assert _times(_replay(repos, longer)) == [_T_B, _T_LEGACY]


@pytest.mark.parametrize(
    "raw_owner", [" owner-a ", "ｏｗｎｅｒ-ａ"], ids=["whitespace", "fullwidth"]
)
def test_t4_the_requested_owner_is_normalized_before_it_is_compared(repos, raw_owner: str) -> None:  # type: ignore[no-untyped-def]
    """入力の揺れ(前後の空白・全角)があっても、正規化後の owner で比べる(列挙が空にならない)。"""
    _two_owners(repos)
    assert _times(_replay(repos, raw_owner)) == [_T_A]


def test_t4_an_invalid_requested_owner_is_rejected(repos) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidOwnerError):
        _replay(repos, "owner#a")


# --- T6: CLI ---------------------------------------------------------------------------


def test_t6_cli_passes_the_owner_to_the_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_replay(stock_codes, start_date, end_date, owner, **kwargs):  # type: ignore[no-untyped-def]
        captured["owner"] = owner
        return []

    monkeypatch.setattr(holding_decision_cli, "run_history_replay", _fake_replay)
    holding_decision_cli.backtest(
        stock_code=[_CODE],
        owner=_OWNER_A,
        start_date="2026-06-01",
        end_date="2026-06-30",
        source="mock",
        allow_same_day_fallback=False,
        purchase_price=None,
        purchase_date=None,
        shares=None,
        csv_path=None,
    )
    assert captured["owner"] == _OWNER_A


def test_t6_cli_owner_option_defaults_to_the_default_owner_and_the_help_describes_the_scope() -> (
    None
):
    option = inspect.signature(holding_decision_cli.backtest).parameters["owner"].default
    assert option.default == DEFAULT_OWNER
    help_text = option.help
    # 新しい仕様(replay も所有者で絞る・旧形式は既定の所有者のもの)を述べ、旧仕様の記載は残さない
    assert "絞り込み" in help_text and "旧形式" in help_text
    assert "列挙にのみ使う" not in help_text


def test_t6_operations_manual_no_longer_says_replay_ignores_the_owner() -> None:
    manual = (Path(__file__).resolve().parents[2] / "docs" / "operations_manual.md").read_text(
        encoding="utf-8"
    )
    assert "再生そのものは所有者で絞らない" not in manual
