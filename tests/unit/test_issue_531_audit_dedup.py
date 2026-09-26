"""Issue #531(#71 F-C14): 監査記録を決定的IDのrecord_if_absent()へ統一する(Tier 1)。

対象は#528/#558と同型の欠陥(非同期fan-out/CLI再試行の再配信で、`AuditService.
record()`〔呼び出しごとにaudit_idをuuid4で新規生成〕が監査ログを重複記録する)
のうち、batch_id/stock_codeが呼び出し時点で既に引数として存在する最小scope
(Tier 1)である。

本ファイルは`services/watchlist_screening_audit.py`側(D4/S。domain競合なし)の
3関数を対象とする。`lambda_handlers/buy_candidates_handler.py`側(F-01。D5が
PR #621〔#591〕により競合中)は、D5解放後に別PRで追加する(#531 issuecomment
参照。1 Issue内でのdomain競合起因の実装単位分離)。

- `record_candidate_audit()`
- `record_repository_result_audit()`
- `record_rotation_commit_audit()`

いずれも`AuditService(repository=fake_repo)`(実サービス+`save_if_absent()`の
意味論を最小限で模倣したfake repository。test_issue_558_batch_id_idempotency.py
の`_FakeAuditRepository`と同型)で検証する。architecturally正しいAuditService
自体の挙動を経由することで、テスト側のfakeがrecord_if_absent()の意味論を
誤って再実装するリスクを避ける。

mutation-based negative verification: production関数を一時的に
record_if_absent()ではなくrecord()(常に成功)を呼ぶよう変異させ、
このテストが実際に赤くなることを確認したうえで復元した(下記コメント参照)。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.services import watchlist_screening_audit
from jstock_advisor.services.audit_service import AuditService

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


class _FakeAuditRepository:
    """`AuditLogRepository.save_if_absent()`の意味論(決定的audit_idの原子的な
    条件付き挿入)を最小限で模倣する(test_issue_558_batch_id_idempotency.pyの
    `_FakeAuditRepository`と同型)。
    """

    def __init__(self) -> None:
        self.saved_audit_ids: list[str] = []
        self.save_calls: int = 0

    def save(self, entry: object) -> None:
        self.save_calls += 1
        self.saved_audit_ids.append(entry.audit_id)  # type: ignore[attr-defined]

    def save_if_absent(self, entry: object) -> bool:
        audit_id = entry.audit_id  # type: ignore[attr-defined]
        if audit_id in self.saved_audit_ids:
            return False
        self.save_calls += 1
        self.saved_audit_ids.append(audit_id)
        return True


def _fake_audit_service() -> tuple[AuditService, _FakeAuditRepository]:
    repo = _FakeAuditRepository()
    return AuditService(repository=repo), repo


def test_record_candidate_audit_is_not_duplicated_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2(candidate側): 同一(batch_id, stock_code)の再配信で複製されない。"""
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    watchlist_screening_audit.record_candidate_audit(
        "1234", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-531-c"
    )
    watchlist_screening_audit.record_candidate_audit(
        "1234", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-531-c"
    )

    assert repo.save_calls == 1


def test_record_candidate_audit_without_batch_id_keeps_using_record_uuid4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: batch_id=None(既存呼び出し)は従来どおりrecord()(uuid4)のまま
    (後方互換ガード)。2回呼べば2件になる(dedupしない)。
    """
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    watchlist_screening_audit.record_candidate_audit(
        "1234", None, "DATA_INSUFFICIENT", _NOW, batch_id=None
    )
    watchlist_screening_audit.record_candidate_audit(
        "1234", None, "DATA_INSUFFICIENT", _NOW, batch_id=None
    )

    assert repo.save_calls == 2
    assert len(set(repo.saved_audit_ids)) == 2  # uuid4なので毎回別のaudit_id


def test_record_repository_result_audit_is_not_duplicated_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2(repository_result側): 同一(batch_id, stock_code)の再配信で複製されない。"""
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    for _ in range(2):
        watchlist_screening_audit.record_repository_result_audit(
            batch_id="batch-531-d",
            stock_code="1234",
            stock_name="テスト",
            rank=1,
            total_score=80.0,
            repository_result=watchlist_screening_audit.REPOSITORY_RESULT_ADDED,
            added_to_watchlist=True,
            registration_source="AUTO_SCREENING",
            registration_policy="policy-a",
            now=_NOW,
        )

    assert repo.save_calls == 1


def test_record_rotation_commit_audit_is_not_duplicated_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2(rotation_commit側): 同一batch_idのfinalize再試行で複製されない
    (2回目のexpected_versionが1回目のcommit後の状態に対してconflict判定に
    なり、真の1回目の結果を誤った失敗記録で上書きしないことが目的)。
    """
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    watchlist_screening_audit.record_rotation_commit_audit(
        "batch-531-e",
        1,
        ["S"],
        ["E"],
        False,
        10,
        {"passed": 10},
        True,
        _NOW,
    )
    # finalizeの再試行相当: 2回目はcommitted=Falseの誤った失敗記録になりうる。
    watchlist_screening_audit.record_rotation_commit_audit(
        "batch-531-e",
        1,
        ["S"],
        ["E"],
        False,
        10,
        {"passed": 10},
        False,
        _NOW,
    )

    assert repo.save_calls == 1


def test_record_candidate_audit_id_includes_stock_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: audit_idからstock_codeを落とすと、同一batch_id内の別銘柄の記録が
    「既に存在する」と誤判定され、正当な監査記録が黙って失われる
    (#62 build_removal_audit_id()の前例に倣い、各構成要素の必要性を固定する)。
    """
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    watchlist_screening_audit.record_candidate_audit(
        "1111", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-531-f2a"
    )
    watchlist_screening_audit.record_candidate_audit(
        "2222", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-531-f2a"
    )

    assert repo.save_calls == 2


def test_record_candidate_audit_id_includes_batch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: audit_idからbatch_idを落とすと、別バッチの同一銘柄の記録が失われる。"""
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    watchlist_screening_audit.record_candidate_audit(
        "1111", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-531-f2b1"
    )
    watchlist_screening_audit.record_candidate_audit(
        "1111", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-531-f2b2"
    )

    assert repo.save_calls == 2


def test_record_repository_result_audit_id_includes_stock_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: repository_result側でも、同一batch_id内の別銘柄の記録が
    stock_code抜きのidで衝突しないことを固定する。"""
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    for stock_code in ("1111", "2222"):
        watchlist_screening_audit.record_repository_result_audit(
            batch_id="batch-531-f2c",
            stock_code=stock_code,
            stock_name="テスト",
            rank=1,
            total_score=80.0,
            repository_result=watchlist_screening_audit.REPOSITORY_RESULT_ADDED,
            added_to_watchlist=True,
            registration_source="AUTO_SCREENING",
            registration_policy="policy-a",
            now=_NOW,
        )

    assert repo.save_calls == 2


def test_record_repository_result_audit_id_includes_batch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: repository_result側でも、別バッチの同一銘柄の記録がbatch_id抜きの
    idで衝突しないことを固定する。"""
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    for batch_id in ("batch-531-f2d1", "batch-531-f2d2"):
        watchlist_screening_audit.record_repository_result_audit(
            batch_id=batch_id,
            stock_code="1111",
            stock_name="テスト",
            rank=1,
            total_score=80.0,
            repository_result=watchlist_screening_audit.REPOSITORY_RESULT_ADDED,
            added_to_watchlist=True,
            registration_source="AUTO_SCREENING",
            registration_policy="policy-a",
            now=_NOW,
        )

    assert repo.save_calls == 2


def test_record_rotation_commit_audit_id_includes_batch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: rotation_commit側でも、別バッチの記録がbatch_id抜きのidで
    衝突しないことを固定する。"""
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    for batch_id in ("batch-531-f2e1", "batch-531-f2e2"):
        watchlist_screening_audit.record_rotation_commit_audit(
            batch_id, 1, ["S"], ["E"], False, 10, {"passed": 10}, True, _NOW
        )

    assert repo.save_calls == 2


def test_record_candidate_and_repository_result_audit_ids_do_not_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: 同一(batch_id, stock_code)でも、decision_type prefixが異なるため
    candidate側とrepository_result側のaudit_idは衝突しない。prefixを落とすと、
    片方の記録がもう片方を「既に存在する」としてスキップし、別種の監査記録が
    黙って失われる(サブちゃん指摘: 候補種別の衝突)。"""
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    watchlist_screening_audit.record_candidate_audit(
        "1111", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-531-f2f"
    )
    watchlist_screening_audit.record_repository_result_audit(
        batch_id="batch-531-f2f",
        stock_code="1111",
        stock_name="テスト",
        rank=1,
        total_score=80.0,
        repository_result=watchlist_screening_audit.REPOSITORY_RESULT_ADDED,
        added_to_watchlist=True,
        registration_source="AUTO_SCREENING",
        registration_policy="policy-a",
        now=_NOW,
    )

    assert repo.save_calls == 2


def test_record_candidate_audit_negative_verification_without_record_if_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mutation-based negative verification: production関数を一時的に
    record_if_absent()ではなくrecord()を呼ぶよう変異させ、このテストが
    検出したい実装欠陥(#528/#558と同型の重複記録)そのものを再現する。
    """
    audit_service, repo = _fake_audit_service()
    monkeypatch.setattr(watchlist_screening_audit, "AuditService", lambda: audit_service)

    def _broken_record_if_absent(**kwargs: object) -> object:
        kwargs.pop("audit_id", None)
        return audit_service.record(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(audit_service, "record_if_absent", _broken_record_if_absent)

    watchlist_screening_audit.record_candidate_audit(
        "1234", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-531-broken"
    )
    watchlist_screening_audit.record_candidate_audit(
        "1234", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-531-broken"
    )

    assert repo.save_calls == 2, "この壊れた実装は2件保存する(=重複防止が効いていない確認)"
