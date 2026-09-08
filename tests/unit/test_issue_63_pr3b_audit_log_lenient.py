"""Issue #63 PR-3b(A-U3 後半): audit_log を LENIENT + item_id PLAIN で宣言する。

監査記録の1件がdecodeできないだけで、他の記録の閲覧・レビューまで止まる必要はない。
skipした件数はWARNINGへ必ず出るため、黙って無視することにはならない
(#245のO-4が週次棚卸で数える観測点)。

```
★ ローカルJSON実装は _load() で全件decodeしてから読み書きするため、
  STRICTのままだと壊れた1件が「監査記録を書くだけ」の経路も止める。
  本ファイルはread側とwrite側の両方を固定する。

★ Productionの挙動は変わらない。Lambdaが使うDynamoDB実装は put_item で
  decodeを通らず、Lambdaからaudit_logを読む経路も0件である(#245 Phase A)。
  したがってProduction検証の観測点は無く、CLI経路のテストが証拠となる。
```

```
不正レコードはfixtureとしてのみ作る。**Productionへの注入は行わない。**
値はすべて架空値であり、実在人物の情報・実際の保有データを含めない。
銘柄コードは実在しない "0000" / "0001" を使う。
```
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli.audit import app as audit_app
from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore
from jstock_advisor.services.csv_import_ledger import build_row_audit_id

_STOCK_GOOD = "0001"
_STOCK_BROKEN = "0000"

# audit_id は生成ID または `<用途>:<識別子>` 形式。所有者名を含まない。
_GOOD_ID = "buy_signal:0001:2026-09-08T00:00:00+00:00"
_BROKEN_ID = "watchlist_removal:0000:2026-09-08T00:00:00+00:00"


def _good_row() -> dict[str, Any]:
    """decodeできる架空の監査記録。"""
    return {
        "audit_id": _GOOD_ID,
        "timestamp": "2026-09-08T00:00:00+00:00",
        "stock_code": _STOCK_GOOD,
        "decision_type": "buy_signal",
        "input_values": {"score": 1},
        "calculation_formulas": {"score": "1"},
        "output_values": {"decision": "hold"},
        "data_sources": [],
        "rule_version": "v0-test",
    }


def _broken_row() -> dict[str, Any]:
    """decodeできない架空の監査記録(timestampが日時として読めない)。"""
    row = _good_row()
    row["audit_id"] = _BROKEN_ID
    row["stock_code"] = _STOCK_BROKEN
    row["timestamp"] = "not-a-timestamp"
    return row


def _seed(store_dir: Path, rows: list[dict[str, Any]] | None = None) -> Path:
    """正常1件 + 不正1件を置く。"""
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / "audit_log.json"
    path.write_text(
        json.dumps(rows if rows is not None else [_good_row(), _broken_row()], ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# --- T-1  LENIENT: 1件skipして残りが返る -------------------------------------


def test_lenient_returns_the_remaining_audit_entries(tmp_path: Path) -> None:
    """★ 壊れた1件があっても、他の監査記録は読める(本PRの目的)。"""
    _seed(tmp_path)

    entries = AuditLogRepository(store_dir=tmp_path).list_all()

    assert [e.audit_id for e in entries] == [_GOOD_ID]


def test_list_by_stock_is_not_blocked_by_a_broken_entry(tmp_path: Path) -> None:
    """CLIが使う絞り込み経路でも同じであること。"""
    _seed(tmp_path)

    entries = AuditLogRepository(store_dir=tmp_path).list_by_stock(_STOCK_GOOD)

    assert [e.audit_id for e in entries] == [_GOOD_ID]


def test_writing_a_new_entry_is_not_blocked_by_a_broken_entry(tmp_path: Path) -> None:
    """★ **書くだけの経路**も止まらない(ここが read 側と同じくらい重要)。

    ローカルJSON実装は upsert でも _load() で全件decodeするため、
    STRICTのままだと「監査記録を1件残す」だけの処理が、無関係な壊れた1件で
    落ちていた。判定を行った事実そのものが記録されなくなる。
    """
    _seed(tmp_path)
    repo = AuditLogRepository(store_dir=tmp_path)

    repo.save(AuditLogEntry.model_validate(_good_row() | {"audit_id": "uuid-like-0002"}))

    assert {e.audit_id for e in repo.list_all()} == {_GOOD_ID, "uuid-like-0002"}


# --- T-2  PLAIN: audit_id が平文で出る ---------------------------------------


def test_plain_disclosure_shows_the_audit_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 復旧できるよう、どの記録が壊れているかを平文で出す。

    audit_idは生成IDまたは `<用途>:<識別子>` 形式であり、所有者名・氏名を
    含まない(#135 Phase Aが実測した `<所有者>#<銘柄コード>` 形式の
    6 collectionと重ならない)。
    """
    _seed(tmp_path)

    with caplog.at_level(logging.WARNING):
        AuditLogRepository(store_dir=tmp_path).list_all()

    assert _BROKEN_ID in caplog.text
    assert "sha256:" not in caplog.text, "PLAIN ではハッシュ表記にしない"


def test_each_failure_is_countable_for_the_weekly_inventory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ #245 O-4 が週次棚卸で数える観測点を固定する。

    LENIENTは「黙って無視する」ことではない。数えられなければ、
    壊れたレコードが増えていることに誰も気づけない。

    数える対象は **1件ごとのWARNING**(`persistence record decode failed`)である。
    collection名・所在・例外種別・policyが出るため、これで数えられる。

    ★ 実測メモ: 走査単位の集計 `emit_failure_summary()`
      (`persistence decode summary ... failed=N`)は
      **src のどこからも呼ばれていない**(`decode_records()` /
      `iter_decoded_records()` も同様に呼び出し元0件)。
      json_store も dynamodb_store も `RecordFailureCollector.handle()` を
      直接使うため、集計行は出ない。
      本PRの範囲(呼び出し側の宣言)では直せない(S-17を触るため)。
      #245 の O-4 は 1件ごとのWARNINGを数える前提で運用する。
    """
    _seed(tmp_path)

    with caplog.at_level(logging.WARNING):
        AuditLogRepository(store_dir=tmp_path).list_all()

    assert "persistence record decode failed" in caplog.text
    assert "collection=audit_log.json" in caplog.text
    assert "policy=LENIENT" in caplog.text
    assert "error=ValidationError" in caplog.text


# --- T-3  宣言していない collection は STRICT + HASH のまま（回帰固定）--------


def test_undeclared_collection_keeps_strict_and_hash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 既定を変えていないこと。「宣言しなければ挙動は変わらない」(PR-2の前提)。

    本PRが宣言したのは audit_log だけである。同じファイルを既定の
    JsonCollectionStore で読むと、従来どおり最初の失敗で例外が出る。
    """
    _seed(tmp_path)
    default_store: JsonCollectionStore[AuditLogEntry] = JsonCollectionStore(
        AuditLogEntry, "audit_log.json", "audit_id", tmp_path
    )

    with pytest.raises(Exception):  # noqa: B017 - pydantic の ValidationError をそのまま通す
        default_store.list_all()

    assert _BROKEN_ID not in caplog.text, "既定では平文の主キーを出さない"


def test_holdings_collection_is_still_strict(tmp_path: Path) -> None:
    """★ 保有・取引のような「静かに間違う」collectionはSTRICTのまま。

    本PRがそれらへ波及していないことを固定する。LENIENTを広げると
    保有比率・取得単価が静かに狂うため、audit_logに限定している。
    """
    (tmp_path / "holdings_v2.json").write_text(
        json.dumps([{"audit_id": _BROKEN_ID, "timestamp": "not-a-timestamp"}], ensure_ascii=False),
        encoding="utf-8",
    )
    store: JsonCollectionStore[AuditLogEntry] = JsonCollectionStore(
        AuditLogEntry, "holdings_v2.json", "audit_id", tmp_path
    )

    with pytest.raises(Exception):  # noqa: B017
        store.list_all()


# --- T-4  CLI 経路（#245 の観測点） ------------------------------------------


def test_cli_audit_show_continues_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """★ CLI(`audit show`)が落ちずに結果を表示し、WARNINGが端末へ出ること。

    #245 Phase Aの実測どおり、decodeが起きるのはCLIの経路だけである。
    Lambdaからaudit_logを読む経路は0件のため、Productionで観測すべき事象は無く、
    **このテストがPR-3bの検証の中心**になる。
    """
    _seed(tmp_path)
    monkeypatch.setattr(
        "jstock_advisor.cli.audit.AuditLogRepository",
        lambda: AuditLogRepository(store_dir=tmp_path),
    )

    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(audit_app, [_STOCK_GOOD])

    assert result.exit_code == 0, result.output
    assert "buy_signal" in result.output, "壊れた1件があっても他の記録は表示される"
    assert _BROKEN_ID in caplog.text, "何が壊れているかがWARNINGで分かる"


def test_cli_audit_show_used_to_fail_before_this_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 変更前の組み合わせ(STRICT)なら同じ入力でCLIが失敗することを示す。

    宣言が外れる回帰が入れば、この差が消えて落ちる。
    """
    _seed(tmp_path)

    class _StrictRepo(AuditLogRepository):
        def __init__(self) -> None:
            self._store = JsonCollectionStore(
                AuditLogEntry, "audit_log.json", "audit_id", tmp_path
            )

    monkeypatch.setattr("jstock_advisor.cli.audit.AuditLogRepository", _StrictRepo)

    result = CliRunner().invoke(audit_app, [_STOCK_GOOD])

    assert result.exit_code != 0, "STRICT では壊れた1件でCLI全体が止まっていた"


# --- T-5  csv_import_ledger（D6）が安全側であること --------------------------


def test_broken_claim_is_still_treated_as_claimed(tmp_path: Path) -> None:
    """★ 壊れたclaimレコードも「claim済み」として扱われる(二重適用の防止)。

    CSV取込の行claimは insert_if_absent で行う。json_storeは
    `item_id in items or item_id in quarantined` で判定するため、
    LENIENTでskipされたレコードもquarantinedに残り「既にある」と見える。

    ★ ここが崩れると、壊れたclaim1件につきCSVの1行が**二重に適用**される。
      LENIENT化にあたって最初に確認すべき点であり、回帰として固定する。
    """
    claim_id = build_row_audit_id("f" * 64, 1)
    broken_claim = _good_row() | {"audit_id": claim_id, "timestamp": "not-a-timestamp"}
    _seed(tmp_path, [_good_row(), broken_claim])
    repo = AuditLogRepository(store_dir=tmp_path)

    accepted = repo.save_if_absent(
        AuditLogEntry.model_validate(_good_row() | {"audit_id": claim_id})
    )

    assert accepted is False, "壊れていても『既にclaim済み』であること"


def test_quarantined_entry_is_not_silently_dropped_on_write(tmp_path: Path) -> None:
    """★ 他のidへの書き込みで、壊れたレコードが黙って消えないこと。

    消えてしまうと「壊れていた事実」が失われ、復旧も監査もできなくなる。
    """
    path = _seed(tmp_path)
    repo = AuditLogRepository(store_dir=tmp_path)

    repo.save(AuditLogEntry.model_validate(_good_row() | {"audit_id": "uuid-like-0003"}))

    ids = {row["audit_id"] for row in json.loads(path.read_text(encoding="utf-8"))}
    assert _BROKEN_ID in ids, "壊れたレコードはrawのまま残ること"


def test_broken_entry_can_still_be_deleted_for_recovery(tmp_path: Path) -> None:
    """★ 復旧経路: 壊れた1件はidを名指しして削除できる(自己修復)。"""
    path = _seed(tmp_path)

    assert AuditLogRepository(store_dir=tmp_path).delete(_BROKEN_ID) is True

    ids = {row["audit_id"] for row in json.loads(path.read_text(encoding="utf-8"))}
    assert ids == {_GOOD_ID}


# --- T-6  正常時は差分が無いこと ---------------------------------------------


def test_no_behaviour_change_when_every_record_decodes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 壊れたレコードが無ければ、挙動もログも従来どおりであること。

    本変更が「壊れているとき」にしか効かないことを固定する。
    """
    _seed(tmp_path, [_good_row()])

    with caplog.at_level(logging.WARNING):
        entries = AuditLogRepository(store_dir=tmp_path).list_all()

    assert [e.audit_id for e in entries] == [_GOOD_ID]
    assert caplog.text == "", "平常時はWARNINGを出さない(監視は0件を平常とする)"


def test_timestamp_is_timezone_aware(tmp_path: Path) -> None:
    """読めた記録の内容が変わっていないこと(decodeそのものは触っていない)。"""
    _seed(tmp_path)

    entry = AuditLogRepository(store_dir=tmp_path).get(_GOOD_ID)

    assert entry is not None
    assert entry.timestamp == dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    assert entry.stock_code == _STOCK_GOOD
