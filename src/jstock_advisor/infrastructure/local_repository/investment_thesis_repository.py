"""InvestmentThesis(個別購入理由)のローカルリポジトリ(実装プラン18節)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.holding_decision import InvestmentThesis
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class InvestmentThesisRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[InvestmentThesis] = build_collection_store(
            InvestmentThesis, "investment_theses.json", "investment_thesis_id", store_dir
        )

    def get(self, investment_thesis_id: str) -> InvestmentThesis | None:
        return self._store.get(investment_thesis_id)

    def get_by_holding(self, holding_id: str) -> InvestmentThesis | None:
        """holding_idによる線形scan。

        Issue #511(#570での是正前)に作成された旧形式(uuid4のinvestment_
        thesis_id)のレコードも、新形式(holding_id自体をPKとする。#570)の
        レコードも、いずれもこのscanで見つかる(PK形式に依存しない後方互換)。
        """
        items = self._store.find(lambda t: t.holding_id == holding_id)
        return items[0] if items else None

    def get_raw_data(self, investment_thesis_id: str) -> str | None:
        """楽観的並行性制御(`replace_if_raw_matches()`)のための生JSON取得。"""
        return self._store.get_raw_data(investment_thesis_id)

    def insert_if_absent(self, thesis: InvestmentThesis) -> bool:
        """investment_thesis_idが未存在の場合のみ原子的に追加してTrue。

        Issue #570: `get_or_create_thesis()`が同一holding_idから決定的に
        導出したinvestment_thesis_id(=holding_id自体)を渡すことで、同一
        holding_idへの並行createを1件のみへ収束させる(既存のCollectionStore
        CAS primitive〔#17〕をそのまま使う。新しいlock/lease機構は作らない)。
        """
        return self._store.insert_if_absent(thesis)

    def replace_if_raw_matches(
        self, investment_thesis_id: str, expected_raw_data: str, thesis: InvestmentThesis
    ) -> bool:
        """expected_raw_dataが現在値と一致する場合のみ更新する(楽観ロック)。

        Issue #570: register_condition()/attest_condition()のlost update対策
        (#530と同型。services/write_plan.pyのapply_conditional_put()経由で呼ぶ)。
        """
        return self._store.replace_if_raw_matches(investment_thesis_id, expected_raw_data, thesis)

    def delete_if_raw_matches(self, investment_thesis_id: str, expected_raw_data: str) -> bool:
        """`services.write_plan._CasCapableRepository`のProtocol適合のために
        委譲するのみ(本Issueでは削除を行わないため呼び出し元は無い)。
        """
        return self._store.delete_if_raw_matches(investment_thesis_id, expected_raw_data)

    def save(self, thesis: InvestmentThesis) -> None:
        self._store.upsert(thesis)

    def list_all(self) -> list[InvestmentThesis]:
        return self._store.list_all()
