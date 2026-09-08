"""Issue #70 F-B5: holdings の Recommendation 保存先を execution context で切り替える。

```
問題  holdings 側は repository を **常に本番テーブル**で生成し、VALIDATION の
      安全性を「保存箇所ごとの `if not is_validation:` ガード」に依存していた。
      現状すべての保存箇所にガードがあることは確認済みだが、
      **1 箇所の追加漏れで検証データが本番履歴へ混入する構造**だった。

      buy 側は `RecommendationRepository.for_execution_context()` で
      **repository 自体を切り替える**（生成経路を 1 本化し切替漏れを構造的に防ぐ、
      と同 factory の docstring が述べている）。
```

```
本 PR の方式（O-B）
  repository を for_execution_context へ切り替え、**既存のガードは残す**。

★ ガードを外さない理由（実測）
  ガードの中には DecisionSnapshot / HoldingDecisionResult の保存が同居しており、
  それらの repository は for_execution_context() を **持たない**。
  外すと VALIDATION がそれらの本番テーブルを汚す（本 Issue が防ごうとしている
  状態そのもの）。3 repository の factory 化は別 Issue で扱う。

-> したがって **観測可能な挙動は変わらない**。変わるのは
   「ガードを書き忘れたときの着地点」だけである（本番 -> 検証用）。
```

```
値はすべて架空値であり、実在の銘柄コード・所有者・保有データを含まない。
銘柄コードは実在しない "0000" を使う。Production への注入は行わない。
"""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    ExecutionMode,
    NotificationMode,
    RecommendationType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    PRODUCTION_FILE_NAME,
    VALIDATION_FILE_NAME,
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler, holdings_watchlist_handler

_STOCK = "0000"
_NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)

_NORMAL = ExecutionContext(mode=ExecutionMode.NORMAL)
_VALIDATION = ExecutionContext(mode=ExecutionMode.VALIDATION)
_VALIDATION_DRY_RUN = ExecutionContext(
    mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
)


def _recommendation(recommendation_id: str = "rec-0001") -> Recommendation:
    """架空の推奨レコード（保存先の検証にのみ使う）。"""
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK,
        stock_name="銘柄 X",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v0-test",
    )


# --- T-1 / T-2  repository 選択（実物の repository で検証）-------------------


def test_t1_validation_selects_the_validation_table() -> None:
    """★ VALIDATION では **検証用テーブル**を指すこと（本 Issue の目的）。"""
    repo = RecommendationRepository.for_execution_context(_VALIDATION)

    assert repo.file_name == VALIDATION_FILE_NAME


def test_t1b_validation_dry_run_also_selects_the_validation_table() -> None:
    """DRY_RUN は VALIDATION の補助設定であり、保存先の判定を変えないこと。"""
    repo = RecommendationRepository.for_execution_context(_VALIDATION_DRY_RUN)

    assert repo.file_name == VALIDATION_FILE_NAME


def test_t2_normal_selects_the_production_table() -> None:
    """★ NORMAL では従来どおり **本番テーブル**（挙動不変の固定）。"""
    repo = RecommendationRepository.for_execution_context(_NORMAL)

    assert repo.file_name == PRODUCTION_FILE_NAME


# --- T-3  VALIDATION は本番テーブルへ 1 件も書かない（既存契約の回帰）-------


def test_t3_validation_writes_nothing_to_the_production_table(tmp_path: Path) -> None:
    """★ VALIDATION で保存しても **本番ファイルは作られない / 空のまま**。

    これは既存の契約（ガードによる抑止）と重ねた二重の安全であり、
    本 PR で壊れていないことを固定する。
    """
    repo = RecommendationRepository.for_execution_context(_VALIDATION, store_dir=tmp_path)

    repo.save(_recommendation())

    production = tmp_path / PRODUCTION_FILE_NAME
    assert not production.exists() or production.read_text(encoding="utf-8").strip() in ("", "[]")


# --- T-4  ★ ガードを 1 つ外した状態を模す（F-B5 の被害が消えること）---------


def test_t4_a_missing_guard_no_longer_reaches_the_production_table(tmp_path: Path) -> None:
    """★★ 本 Issue の目的そのもの。

    F-B5 が指摘したのは「1 箇所の追加漏れで検証データが本番履歴へ混入する構造」である。
    ガードを書き忘れた保存（= ここでは `if` を通さず直接 save する）を模し、
    **着地点が本番ではなく検証用テーブルになる**ことを固定する。

    修正前はこの save が本番テーブルへ着地していた（repository が常に本番だったため）。
    """
    repo = RecommendationRepository.for_execution_context(_VALIDATION, store_dir=tmp_path)

    repo.save(_recommendation("rec-leaked"))  # ★ ガードを通さない = 書き忘れの再現

    production_records = RecommendationRepository(
        store_dir=tmp_path, file_name=PRODUCTION_FILE_NAME
    ).list_all()
    validation_records = RecommendationRepository(
        store_dir=tmp_path, file_name=VALIDATION_FILE_NAME
    ).list_all()

    assert production_records == [], "本番テーブルへ混入していないこと"
    assert [r.recommendation_id for r in validation_records] == ["rec-leaked"]


def test_t4b_normal_still_lands_on_the_production_table(tmp_path: Path) -> None:
    """逆側の固定: NORMAL の保存は従来どおり本番テーブルへ着地する。"""
    repo = RecommendationRepository.for_execution_context(_NORMAL, store_dir=tmp_path)

    repo.save(_recommendation("rec-normal"))

    production_records = RecommendationRepository(
        store_dir=tmp_path, file_name=PRODUCTION_FILE_NAME
    ).list_all()

    assert [r.recommendation_id for r in production_records] == ["rec-normal"]


# --- T-5  buy 側との対称性 / ガードの維持 ------------------------------------


def test_t5_holdings_uses_the_same_factory_as_buy() -> None:
    """★ holdings が buy と **同じ生成経路**を使っていること。

    factory の docstring が「呼び出し側はこのファクトリだけを使い、
    RecommendationRepository() を直接 VALIDATION 分岐で呼ばない」と定めている。
    どちらかが直接生成へ戻れば、この test が落ちる。
    """
    holdings_source = Path(inspect.getfile(holdings_watchlist_handler)).read_text(encoding="utf-8")
    buy_source = Path(inspect.getfile(buy_candidates_handler)).read_text(encoding="utf-8")

    for name, source in (("holdings", holdings_source), ("buy", buy_source)):
        assert "RecommendationRepository.for_execution_context(" in source, name
        assert "recommendation_repo = RecommendationRepository()" not in source, (
            f"{name}: 直接生成へ戻っている（VALIDATION が本番テーブルへ向く）"
        )


def test_t5b_holdings_keeps_the_individual_guards() -> None:
    """★ 既存の個別ガードを **削っていない**こと（O-B の前提）。

    ガードの中には DecisionSnapshot / HoldingDecisionResult の保存が同居しており、
    それらの repository は for_execution_context() を持たない。
    ガードを外すと VALIDATION がそれらの本番テーブルを汚す。
    """
    source = Path(inspect.getfile(holdings_watchlist_handler)).read_text(encoding="utf-8")

    assert source.count("if not execution_context.is_validation:") >= 5, (
        "VALIDATION で保存をスキップするガードが減っている"
    )
