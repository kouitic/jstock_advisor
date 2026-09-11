"""推奨の定点評価結果のローカルリポジトリ(要求仕様29〜36節)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


@dataclass
class CompletedHorizonIndex:
    """「どの(recommendation_id, horizon)が評価済みか」をrun開始時に1回だけ
    読み込んで保持する索引(Issue #113)。

    これが無い状態では`exists_for_horizon()`が
    「dueな(recommendation, horizon)の組ごとに1回」evaluation_resultsの
    フルテーブルScanを行っていた(本番実測で1実行あたり最大9,663回のScan =
    約270万RCU)。定点評価ループの実行時間の支配項であり、
    **評価ループの内側からexists_for_*系を呼んではならない**。

    run中に保存した評価結果はrecord_*()で索引へ反映すること
    (同一run内での二重保存を防ぐ。逐次実行下での冪等性は従来どおり
    この索引が担保する。並行実行下の重複防止は本索引の責務ではなく
    Issue #71の担当範囲)。
    """

    business_days: set[tuple[str, int]] = field(default_factory=set)
    calendar_days: set[tuple[str, int]] = field(default_factory=set)

    def has_business_horizon(self, recommendation_id: str, horizon_business_days: int) -> bool:
        return (recommendation_id, horizon_business_days) in self.business_days

    def has_calendar_horizon(self, recommendation_id: str, horizon_calendar_days: int) -> bool:
        return (recommendation_id, horizon_calendar_days) in self.calendar_days

    def record_business_horizon(self, recommendation_id: str, horizon_business_days: int) -> None:
        self.business_days.add((recommendation_id, horizon_business_days))

    def record_calendar_horizon(self, recommendation_id: str, horizon_calendar_days: int) -> None:
        self.calendar_days.add((recommendation_id, horizon_calendar_days))

    def __len__(self) -> int:
        return len(self.business_days) + len(self.calendar_days)


class EvaluationResultRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[EvaluationResult] = build_collection_store(
            EvaluationResult, "evaluation_results.json", "evaluation_id", store_dir
        )

    def list_all(self) -> list[EvaluationResult]:
        return self._store.list_all()

    def list_by_recommendation(self, recommendation_id: str) -> list[EvaluationResult]:
        return self._store.find(lambda e: e.recommendation_id == recommendation_id)

    def get(self, evaluation_id: str) -> EvaluationResult | None:
        return self._store.get(evaluation_id)

    def load_completed_horizon_index(self) -> CompletedHorizonIndex:
        """評価済み(recommendation_id, horizon)の索引を**1回の全件走査**で構築する。

        定点評価ループはこの索引だけを参照し、`exists_for_horizon()` /
        `exists_for_calendar_horizon()`をループ内から呼ばないこと(Issue #113)。
        保持するのはIDとhorizonのタプルのみで、EvaluationResult本体は保持しない。
        """
        index = CompletedHorizonIndex()
        for evaluation in self._store.iter_all():
            if evaluation.horizon_business_days is not None:
                index.record_business_horizon(
                    evaluation.recommendation_id, evaluation.horizon_business_days
                )
            if evaluation.horizon_calendar_days is not None:
                index.record_calendar_horizon(
                    evaluation.recommendation_id, evaluation.horizon_calendar_days
                )
        return index

    def exists_for_horizon(self, recommendation_id: str, horizon_business_days: int) -> bool:
        """単発の存在確認(CLI・テスト用)。

        **定点評価のループ内から呼ばないこと**(1回あたりテーブル全件走査になる。
        Issue #113)。ループでは`load_completed_horizon_index()`を使う。
        """
        return any(
            e.recommendation_id == recommendation_id
            and e.horizon_business_days == horizon_business_days
            for e in self._store.iter_all()
        )

    def exists_for_calendar_horizon(
        self, recommendation_id: str, horizon_calendar_days: int
    ) -> bool:
        """単発の存在確認(CLI・テスト用)。ループ内から呼ばないこと(Issue #113)。"""
        return any(
            e.recommendation_id == recommendation_id
            and e.horizon_calendar_days == horizon_calendar_days
            for e in self._store.iter_all()
        )

    def save(self, evaluation: EvaluationResult) -> None:
        """無条件に書き込む(CLI・テスト・既存呼び出し用)。

        ★ 定点評価ループからは使わない。並行実行下で二重保存になるため。
        ループは `insert_if_absent()` を使うこと(Issue #71 F-C12)。
        """
        self._store.upsert(evaluation)

    def insert_if_absent(self, evaluation: EvaluationResult) -> bool:
        """同じ一意キーの評価が無い場合だけ保存する(Issue #71 F-C12)。

        ★ 保存できたら True、既に存在していたら False。
        DynamoDB 実装は `attribute_not_exists(evaluation_id)` の条件付き
        put_item で**原子的**に判定する。したがって、2 つの実行が同時に
        事前確認(CompletedHorizonIndex)を通り抜けても、保存に成功するのは
        片方だけになる。

        ★ これが効くのは `evaluation_id` が**決定的なキー**であるときだけである
        (`build_evaluation_id()`)。uuid4 のままでは毎回違うキーになるため、
        条件が常に成立して二重保存を防げない。

        ★ False のとき、呼び出し側は**保存成功数へ加算してはならない**。
        加算すると、実際には 1 件しか保存されていないのに 2 件成功したと
        集計され、定点評価の母数が実態からずれる。
        """
        return self._store.insert_if_absent(evaluation)
