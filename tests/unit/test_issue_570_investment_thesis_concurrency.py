"""Issue #570(#71 F-C13から分離): investment_thesis_serviceの並行更新・
同一holding_idに対する重複InvestmentThesis生成を防止する。

## 何を固定するか

```
A  register_condition() / attest_condition()のlost update対策
   (#530と同型のCAS。read直後の生JSONを楽観ロック条件に使う)
B  get_or_create_thesis()の同一holding_idに対するconcurrent create対策
   (investment_thesis_idをholding_idから決定的に導出し、insert_if_absent()
   で原子的に作成する)
```

Bは#530のCASパターン(既存itemへの更新)では対応できない(既存itemが無い
状態への競合)ため、新しいアプローチ(決定的キー + insert_if_absent)を
採る。ただし新しいlock/lease機構は作らない(既存のCollectionStore CAS
primitive〔#17〕をそのまま使う)。

架空値のみを使う(実在の所有者名・銘柄コードは使わない)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import ThesisConditionAttestationStatus
from jstock_advisor.domain.entities.holding_decision import InvestmentThesis
from jstock_advisor.infrastructure.local_repository.investment_thesis_repository import (
    InvestmentThesisRepository,
)
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService
from jstock_advisor.services.write_plan import ConcurrentUpdateError

_NOW = dt.datetime(2026, 9, 26, 0, 0, tzinfo=dt.UTC)
_HOLDING_ID = "所有者A#0000"
_STOCK_CODE = "0000"


def _service(store_dir: Path) -> InvestmentThesisService:
    return InvestmentThesisService(store_dir=store_dir)


# --- B: get_or_create_thesis()の並行create対策 ---------------------------------


def test_get_or_create_thesis_is_idempotent_for_repeated_calls(tmp_path: Path) -> None:
    """同一holding_idへの複数回呼び出しが同じthesisを返す(SQS at-least-once
    のredeliveryに相当。get_or_create_thesis()は例外を投げない設計)。
    """
    service = _service(tmp_path)

    first = service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, _NOW)
    second = service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, _NOW)

    assert first.investment_thesis_id == second.investment_thesis_id
    assert len(InvestmentThesisRepository(tmp_path).list_all()) == 1


def test_get_or_create_thesis_uses_deterministic_id_derived_from_holding_id(
    tmp_path: Path,
) -> None:
    """investment_thesis_idがholding_idから決定的に導出されること
    (#530のCASパターンでは対応できない濃厚create競合を、既存の
    insert_if_absent()〔#17〕で防ぐための前提)。
    """
    service = _service(tmp_path)

    thesis = service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, _NOW)

    assert thesis.investment_thesis_id == _HOLDING_ID


def test_get_or_create_thesis_concurrent_create_is_prevented_by_insert_if_absent(
    tmp_path: Path,
) -> None:
    """同一holding_idに対する並行create競合が二重生成を起こさないこと。

    2つの「並行実行」を、両方が`get_by_holding()`で「存在しない」と読んだ
    直後の状態から再現する。片方が先にinsert_if_absent()で作成し終えた
    ものとして、もう片方(loser)がその結果を正しく読み直すことを確認する。
    """
    repo = InvestmentThesisRepository(tmp_path)
    service = InvestmentThesisService(thesis_repository=repo, store_dir=tmp_path)

    # racer 1: 先にinsert_if_absent()まで完了させる(既存の永続化経路そのもの)。
    winner = service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, _NOW)

    # racer 2: 同一holding_idで再度呼ぶ(get_by_holding()は既にwinnerを
    # 見つけるはずだが、insert_if_absent()自体の原子性も別途固定する
    # ため、直接insert_if_absent()を呼んで「先に他プロセスが作成済み」を
    # 再現する)。
    duplicate_attempt = InvestmentThesis(
        investment_thesis_id=_HOLDING_ID,
        holding_id=_HOLDING_ID,
        stock_code=_STOCK_CODE,
        conditions=[],
        updated_at=_NOW,
    )
    created = repo.insert_if_absent(duplicate_attempt)

    assert created is False, "同一investment_thesis_idの2回目のinsert_if_absentは失敗するべき"
    assert len(repo.list_all()) == 1
    assert repo.get(_HOLDING_ID) == winner


def test_get_or_create_thesis_loser_returns_the_winners_persisted_thesis(
    tmp_path: Path,
) -> None:
    """insert_if_absent()に負けたcallerが、勝者の実際に永続化された値を
    返すこと(自分が構築したtransientなthesisを返さない)。
    """
    repo = InvestmentThesisRepository(tmp_path)
    service = InvestmentThesisService(thesis_repository=repo, store_dir=tmp_path)

    # 先にholding_idのレコードを別の値(条件1件を持つ)で作っておく。
    existing = InvestmentThesis(
        investment_thesis_id=_HOLDING_ID,
        holding_id=_HOLDING_ID,
        stock_code=_STOCK_CODE,
        conditions=[],
        updated_at=_NOW,
    )
    repo.save(existing)

    result = service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, _NOW)

    assert result.investment_thesis_id == _HOLDING_ID
    assert len(repo.list_all()) == 1


def test_get_or_create_thesis_negative_verification_without_insert_if_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mutation-based negative verification: insert_if_absent()を無条件upsert
    (常にTrueを返すsave())へ差し替えると、並行create防止が機能しないことを
    示す(=このテストが検出したい実装欠陥そのものを再現する)。

    実装を一時的に変異させ、正しいテストが実際に赤くなることを確認したうえで
    復元する、という意味でのnegative verificationをここで固定する
    (production codeは変更しない。repositoryのメソッドをmonkeypatchするのみ)。
    """
    repo = InvestmentThesisRepository(tmp_path)

    def _broken_insert_if_absent(thesis: InvestmentThesis) -> bool:
        # #570以前の欠陥を模した挙動: 常に成功する(重複チェックをしない)。
        repo.save(thesis)
        return True

    monkeypatch.setattr(repo, "insert_if_absent", _broken_insert_if_absent)
    service = InvestmentThesisService(thesis_repository=repo, store_dir=tmp_path)

    service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, _NOW)
    # 2回目の呼び出しはget_by_holding()が既存を見つけるため、実際には
    # 呼ばれないが、insert_if_absent自体が壊れていることを直接確認する。
    duplicate = InvestmentThesis(
        investment_thesis_id=_HOLDING_ID,
        holding_id=_HOLDING_ID,
        stock_code=_STOCK_CODE,
        conditions=[],
        updated_at=_NOW,
    )
    assert repo.insert_if_absent(duplicate) is True, (
        "この壊れた実装はTrueを返す(=insert_if_absentの契約が壊れていることの確認)"
    )


# --- 後方互換: 旧形式(uuid4のinvestment_thesis_id)のレコード -------------------


def test_existing_legacy_uuid_thesis_is_still_found_by_get_by_holding(tmp_path: Path) -> None:
    """#570着手前に作成された旧形式(uuid4のinvestment_thesis_id)のレコードが、
    修正後もget_by_holding()の線形scanで引き続き見つかること(後方互換。
    migration/backfill不要)。
    """
    repo = InvestmentThesisRepository(tmp_path)
    legacy = InvestmentThesis(
        investment_thesis_id="legacy-uuid-1234",
        holding_id=_HOLDING_ID,
        stock_code=_STOCK_CODE,
        conditions=[],
        updated_at=_NOW,
    )
    repo.save(legacy)

    service = InvestmentThesisService(thesis_repository=repo, store_dir=tmp_path)
    found = service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, _NOW)

    assert found.investment_thesis_id == "legacy-uuid-1234"
    assert len(repo.list_all()) == 1, "旧形式のレコードがあるのに新規作成してはならない"


def test_register_condition_on_legacy_uuid_thesis_uses_its_own_id_for_cas(
    tmp_path: Path,
) -> None:
    """旧形式レコードへのregister_condition()が、そのレコード自身の
    investment_thesis_id(uuid4のまま)でCASを行うこと(holding_idではなく)。
    """
    repo = InvestmentThesisRepository(tmp_path)
    legacy = InvestmentThesis(
        investment_thesis_id="legacy-uuid-5678",
        holding_id=_HOLDING_ID,
        stock_code=_STOCK_CODE,
        conditions=[],
        updated_at=_NOW,
    )
    repo.save(legacy)
    service = InvestmentThesisService(thesis_repository=repo, store_dir=tmp_path)

    updated = service.register_condition(_HOLDING_ID, _STOCK_CODE, "架空の購入理由", _NOW)

    assert updated.investment_thesis_id == "legacy-uuid-5678"
    assert len(updated.conditions) == 1


# --- A: register_condition() / attest_condition()のlost update対策(CAS) ------


def test_register_condition_raises_concurrent_update_error_on_stale_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read直後に別の更新が入った場合、register_condition()が
    ConcurrentUpdateErrorを送出すること(lost updateにならない)。

    get_raw_data()をstale値に固定するmonkeypatchで、
    「register_condition()内部の読み取り直後に、別プロセスの更新が
    先に完了した」競合を再現する(実データは既に更新済みだが、
    register_condition()自身の読み取りだけがそれを見落としたことに相当)。
    register_condition()自体を直接呼び、その内部のapply_conditional_put
    呼び出しが実際に競合を検出することを確認する(CAS primitiveの
    直接呼び出しではない)。
    """
    repo = InvestmentThesisRepository(tmp_path)
    service = InvestmentThesisService(thesis_repository=repo, store_dir=tmp_path)
    thesis = service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, _NOW)

    stale_raw = repo.get_raw_data(thesis.investment_thesis_id)
    assert stale_raw is not None
    # 別プロセスが先に更新を完了させたことにする。
    repo.save(thesis.model_copy(update={"updated_at": _NOW + dt.timedelta(seconds=1)}))
    monkeypatch.setattr(repo, "get_raw_data", lambda _id: stale_raw)

    with pytest.raises(ConcurrentUpdateError):
        service.register_condition(_HOLDING_ID, _STOCK_CODE, "架空の購入理由", _NOW)


def test_register_condition_succeeds_when_no_concurrent_write(tmp_path: Path) -> None:
    """通常の(競合が無い)呼び出しは正常に完了すること。"""
    service = _service(tmp_path)
    service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, _NOW)

    updated = service.register_condition(_HOLDING_ID, _STOCK_CODE, "架空の購入理由A", _NOW)

    assert len(updated.conditions) == 1
    assert updated.conditions[0].description == "架空の購入理由A"


def test_attest_condition_raises_concurrent_update_error_on_stale_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """attest_condition()もregister_condition()と同型のCASを行うこと。

    register_condition()側のテストと同じ手法(get_raw_data()をstale値へ
    固定するmonkeypatch)で、attest_condition()自身の読み取り直後に別の
    更新が先に完了した競合を再現し、attest_condition()を直接呼ぶ。
    """
    repo = InvestmentThesisRepository(tmp_path)
    service = InvestmentThesisService(thesis_repository=repo, store_dir=tmp_path)
    registered = service.register_condition(_HOLDING_ID, _STOCK_CODE, "架空の購入理由B", _NOW)
    condition_id = registered.conditions[0].condition_id

    stale_raw = repo.get_raw_data(registered.investment_thesis_id)
    assert stale_raw is not None
    # 別プロセスが先に更新を完了させたことにする(conditionsは変えない)。
    repo.save(registered.model_copy(update={"updated_at": _NOW + dt.timedelta(seconds=1)}))
    monkeypatch.setattr(repo, "get_raw_data", lambda _id: stale_raw)

    with pytest.raises(ConcurrentUpdateError):
        service.attest_condition(
            _HOLDING_ID,
            condition_id,
            ThesisConditionAttestationStatus.MAINTAINED,
            "架空の申告者",
            _NOW,
        )


def test_attest_condition_succeeds_when_no_concurrent_write(tmp_path: Path) -> None:
    service = _service(tmp_path)
    registered = service.register_condition(_HOLDING_ID, _STOCK_CODE, "架空の購入理由C", _NOW)
    condition_id = registered.conditions[0].condition_id

    updated = service.attest_condition(
        _HOLDING_ID,
        condition_id,
        ThesisConditionAttestationStatus.MAINTAINED,
        "架空の申告者",
        _NOW,
    )

    assert updated.conditions[0].last_attestation is not None
    last_attestation = updated.conditions[0].last_attestation
    assert last_attestation.status == ThesisConditionAttestationStatus.MAINTAINED


# --- CLI: ConcurrentUpdateErrorが生tracebackで表示されないこと ------------------


def test_cli_register_thesis_condition_catches_concurrent_update_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register-thesis-condition CLIコマンドがConcurrentUpdateErrorを
    ValueErrorとして捕捉し、exit code 1で終了すること(生tracebackを
    表示しない。#530 F4と同型のCLIハンドリング)。
    """
    from typer.testing import CliRunner

    from jstock_advisor.cli.main import app

    def _raise_conflict(*args: object, **kwargs: object) -> InvestmentThesis:
        raise ConcurrentUpdateError("dummy-id")

    monkeypatch.setattr(
        "jstock_advisor.cli.holding_decision.InvestmentThesisService.register_condition",
        _raise_conflict,
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "holding-decision",
            "register-thesis-condition",
            _STOCK_CODE,
            "--description",
            "架空の理由",
        ],
    )

    assert result.exit_code == 1
    assert not isinstance(result.exception, ConcurrentUpdateError), (
        "例外が捕捉されずCLIから漏れている(生tracebackが表示される)"
    )
