# Issue Label 運用ポリシー

## 0. この文書の位置づけ

本文書は、このリポジトリにおける **GitHub Issue の label 運用の正本
(Single Source of Truth)** である。

- Issue の labels は色分けではなく、**開発キュー・品質管理・リリース判断に使う
  正式なメタデータ**として扱う。
- 本ポリシーは**特定の AI・特定の担当者に依存しない Repository Development Policy**
  である。人間・AI エージェントのいずれが、どの製品・どのモデルで作業する場合も、
  同じ定義・同じ手順を適用する。
- 各エージェントの入口ファイル(`CLAUDE.md`、`AGENTS.md` など、エージェントが
  起動時に読み込む規約ファイル)は本文書へ到達するための参照にすぎず、
  **ルール本体を保持しない**。ルールが変わったときに更新するのは本文書であり、
  入口ファイルではない。新しいエージェントを導入する場合は、その入口ファイルから
  本文書を1行参照するだけでよい。
- 会話上の合意や個々のエージェントのメモリは正本ではない。
  それらと本文書が食い違う場合は、本文書を正とし、必要なら本文書を更新する。

---

## 1. 5軸モデル

Issue には次の5つの軸がある。

| 軸 | 問い | ラベル |
|---|---|---|
| **Issue Type** | 何の Issue か | `bug` / `design-defect` / `enhancement` / `investigation` / `calibration` / `tracking` / `not-a-bug` / `accepted-risk` |
| **Priority** | いつ対応するか(対応順序) | `priority:P0` / `priority:P1` / `priority:P2` / `priority:P3` |
| **Severity** | 問題が発生した場合の影響度 | `severity:SEV-1` / `severity:SEV-2` / `severity:SEV-3` / `severity:SEV-4` |
| **Release Blocker** | Production release を止めるか | `release-blocker` |
| **Progress Status** | 開発ライフサイクル上どこまで進んだか | `status:未着手` / `status:調査・設計中` / `status:設計済` / `status:開発中` / `status:開発済` / `status:マージ済` / `status:デプロイ済` / `status:本番検証済` |

この5軸とは別に、**判定軸ではない補助 metadata** として waiting label がある
(`waiting:本番検証` / `waiting:人間判断` / `waiting:外部条件`、§8)。

---

## 2. 5軸は独立して判定する

**この5軸を相互に自動推論してはならない。** それぞれ独立した根拠で判断する。

```
P0              ≠  release-blocker
SEV-1           ≠  P0
release-blocker ≠  SEV-1
bug             ≠  必ず release-blocker
```

具体的には、次のような推論をしない。

- 「P0 だから次回 release を止める」— Priority は対応順序であり、release 可否ではない。
- 「SEV-1 だから P0」— 影響度が大きくても、対応順序が最優先とは限らない。
- 「release-blocker だから Severity を引き上げる」— block 条件と影響度は別物。
- 「bug だから release-blocker」— 多くの bug は release を止めない。
- 「P0 だから status:開発中」— Priority は対応順序であり、進捗ではない。
- 「status:デプロイ済 だから release-blocker を解除してよい」— 進捗と block 条件は別物。

---

## 3. Issue Type

Issue には原則として Type を **1つだけ**設定する。

### `bug`

既存の仕様・契約・期待動作に対して、**実装が実際に誤動作していることが確認された** Issue。

- 正しいデータなのに誤判定する
- fail-close すべき経路が fail-open する
- `None` を `0` として扱い誤った結果を出す
- 本来到達すべき安全機構が機能しない

### `design-defect`

コードが**現在の設計どおり動作していても、その設計自体に問題があり**、
機能劣化を生む Issue。

- 機能が成立しない
- 過度に安全側へ倒れて実用不能になる
- 本来の目的を達成できない

「安全性 bug ではないが設計上問題」という場合はこちら。
**`bug` と `design-defect` は原則として同時に付けない。**

### `enhancement`

既存の誤動作修正ではなく、新機能・テスト強化・可観測性向上・開発基盤改善・
新しい仕組みの追加などを目的とする Issue。

### `investigation`

まだ欠陥と確定しておらず、`bug` / `design-defect` / `accepted-risk` / `not-a-bug`
などの結論を出すための調査 Issue。

**調査中の仮説だけを理由に `bug` を付けない。**

### `calibration`

実績データ蓄積後に、閾値・重み・パラメータ・判定水準などを再評価する Issue。
**現在の実装が欠陥であるという意味ではない。**

### `tracking`

親 Issue・監査一覧・追跡表。それ自体を直接修正することが主目的ではなく、
複数の Issue / findings の状態を追跡するもの。

### `not-a-bug`

調査の結果、問題ではないことが確定した Issue。
通常 `state=CLOSED` / `stateReason=NOT_PLANNED` と組み合わせる。
**`bug` とは同時に付けない。**

### `accepted-risk`

問題・リスク自体は存在するが、理由を明示したうえで対応しないと判断したもの。
「昔からそうだから」「たぶん問題ないから」だけでは `accepted-risk` にしない。
**受容理由と残余リスクを Issue へ記録する。**

### 排他関係のまとめ

| 組み合わせ | 可否 |
|---|---|
| `bug` + `design-defect` | 不可 |
| `bug` + `not-a-bug` | 不可 |
| `calibration` + `bug` | 不可(機械的に組み合わせない) |
| Type が2つ以上 | 原則不可 |
| Type が0個 | 不可(OPEN Issue には必ず Type を付ける) |

`calibration` の再評価の結果、現在の閾値自体が仕様違反・欠陥と確定した場合は、
別途 `bug` 化するか Type を再分類する。

---

## 4. Priority

| ラベル | 意味 |
|---|---|
| `priority:P0` | 最優先で対応する |
| `priority:P1` | 高 |
| `priority:P2` | 中 |
| `priority:P3` | 低 |

Priority は **対応順序・優先順位**を表す。

- **P0 だからといって次回 Production release を必ず止めるわけではない。**
  release 可否は `release-blocker` で別途判断する。
- **Severity が高い = Priority が高い、とは限らない。**

通常の実装 Issue では **Priority を設定することを基本とする**
(設定しない場合の扱いは §10 を参照)。

---

## 5. Severity

| ラベル | 意味 |
|---|---|
| `severity:SEV-1` | 重大 — 誤った投資判断・データ破壊に直結 |
| `severity:SEV-2` | 高 — 安全機構の無効化・仕様違反 |
| `severity:SEV-3` | 中 — 機能低下・可観測性の欠如 |
| `severity:SEV-4` | 低 — 軽微・表示のみ |

Severity は **発生した場合の影響度**であり、対応順序ではない。
したがって「SEV-1 だから自動的に P0」とはしない。逆も同様。

---

## 6. Release Blocker

Production release を**実際に止める** Issue にだけ `release-blocker` を設定する。
これは Priority / Severity とは完全に別軸である。

- `bug` + `priority:P0` + `severity:SEV-2` でも、release を止める必要がなければ
  `release-blocker` は付けない。
- `design-defect` + `priority:P1` + `severity:SEV-3` でも、
  特定機能を含む release を止めるなら `release-blocker` を付けてよい。

### 条件付き release-blocker

`release-blocker` が条件付きの場合、**label だけでは条件を表現できない。**

Issue 本文または最新の status comment へ、**何を含む release を止めるのか**を明記する。

```
BLOCKER_FOR_PRODUCTION_DEPLOYMENT_OF_#xx
NEXT_PRODUCTION_RELEASE_BLOCKER
```

`release-blocker` が付いていても「すべての Production release を止める」とは
限らない。**release 判断時は label の有無だけでなく、必ず block 条件を確認する。**

### blocking target の必須記録

`release-blocker` を付与する場合、**Issue 本文または最新の durable status comment
へ次の構造化情報を必ず記録する。** label だけでは block 対象を表現できないためである。

```
BLOCKER_MODE              = DEFECT_BLOCK | VERIFICATION_HOLD
BLOCKING_TARGET_TYPE      = ISSUE | COMMIT | RELEASE_CANDIDATE | PRODUCTION_NEXT
BLOCKING_TARGET           = 具体的対象
BLOCK_REASON              = 理由
BLOCKER_SCOPE             = 何を含む release を止めるのか
BLOCKER_REMOVAL_CONDITION = 解除条件
BLOCKER_ADDED_AT          = 付与日
```

必要に応じて次も記録する。

```
REMEDIATION_COMMIT           = sha | PENDING
PRODUCTION_VERIFICATION_PLAN = Issue の該当 section / comment への参照
```

**GitHub label 自体に値を持たせようとしない。** label は `release-blocker` の
存在だけを示し、詳細な target は Issue の durable record で管理する。
5軸モデル(§1)は変更しない。

#### 必須記録が不足している場合は fail-closed とする

`release-blocker` が付いているにもかかわらず、上記の必須記録のいずれかが
不足している場合は次のように扱う。

```
BLOCKER_METADATA_COMPLETE = NO
RELEASE_DECISION          = INSUFFICIENT_EVIDENCE
```

**必須記録が不足している `release-blocker` を、blocker が無いものとして
扱ってはならない。** release 可否を `INSUFFICIENT_EVIDENCE` とし、
blocking target / scope を確定するまで Production release へ進んではならない。

記録が無いことは「その blocker が release を止めない」ことの根拠にならない。
**label を無視して release することは禁止**である。
不足を解消する方法は、当該 Issue へ必須記録を追加して blocking target と
scope を確定させることであって、blocker を無視することではない。

### BLOCKER_MODE の定義

**`DEFECT_BLOCK`**

修正がまだ release artifact へ入っていないため、欠陥を未修正のまま Production へ
出すことを禁止する状態。

```
BLOCKER_MODE         = DEFECT_BLOCK
BLOCKING_TARGET_TYPE = PRODUCTION_NEXT
意味                  = remediation commit を含まない release は禁止
```

remediation commit が merge され、**その修正を Production へ入れる release 自身**を
この blocker で禁止してはならない。

**`VERIFICATION_HOLD`**

remediation commit は main / release candidate へ入っているが、
Production Verification が未完了の状態。

```
意味 = remediation release そのものの deploy は許容する
      deploy 後、Issue 定義の mandatory verification + ChatGPT PASS +
      human approval まで blocker を維持する
      この期間は、当該 verification を未完了のまま
      さらに次の通常 Production release へ進むことを禁止する
```

### remediation release の自己 block 禁止

```
BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER
```

`release-blocker` は「**問題を未解消のまま通過する release**」を止めるための
ものであり、「**その blocker 自身を解消するための remediation release**」を
永遠に禁止するものではない。

remediation release を許可する条件は次のとおりで、**すべて**満たすこと。

```
remediation fix が merge 済み
release scope に当該 fix が含まれる
Production Verification Plan が定義済み
blocker は deploy だけでは解除しない
unrelated piggyback 禁止ルールを満たす
exact release candidate SHA について人間承認
ChangeSet CREATE / EXECUTE は別途人間承認
```

これは「OPEN blocker があっても無視してよい」というルールでは**ない**。
release 可否は、各 blocker の `BLOCKING_TARGET` / `BLOCKER_SCOPE` を確認して
判定する。grouped release 側の条件は
[docs/development_workflow.md](development_workflow.md) §9 が正本。

### 既存 blocker の移行

本節の導入だけを理由に、既存 Issue の blocker metadata を一括書き換えしない。
現在 OPEN の `release-blocker` は、**次回の status update 時に新フォーマットへ
同期する**方針とする。既存の履歴を破壊しない。

### Production-target defect の release-blocker lifecycle

現行 Production に実害が出ている欠陥(Production-target defect)については、
`release-blocker` を次の lifecycle で扱う。

```
blocker 付与
  → 修正の merge
  → Production deploy
  → Immediate Verification
  → mandatory verification(Issue が定義したもの)
  → ChatGPT review
  → human approval
  → blocker 解除
```

重要な点は次のとおり。

- **deploy しただけでは解除しない。merge しただけでも解除しない。**
  Issue が `MANDATORY_FOR_RELEASE_BLOCKER_REMOVAL` と定義した verification が
  完了して初めて解除の判断ができる。
- **`Issue close` と `release-blocker 解除` は別判断である。**
  verification 完了前に Issue を close しない一方、blocker を解除しても
  後続 Phase が残るなら Issue は OPEN のままでよい。逆に、Issue を close しても
  blocker が別条件で残ることもありうる。
- 未完了の verification が
  **`OPTIONAL_POST_RELEASE_OBSERVATION` だけになった場合は、解除しうる。**
  この場合、自然な障害発生を待つことを必須とせず、事前に定義した代替証拠
  (unit / contract tests、CI、Immediate Verification、正常系 natural evidence)
  と ChatGPT review PASS、人間判断をもって解除可否を決める。
  分類の定義と代替証拠の要件は
  [docs/development_workflow.md](development_workflow.md) 7節が正本。
- **Issue 自身が自然な negative-path observation を Acceptance Criteria として
  明示している場合、これを勝手に `OPTIONAL` へ格下げしない。**

この lifecycle は、§6 冒頭の「`release-blocker` は Production release を
**実際に止める** Issue にだけ設定する」という意味を変更するものではない。
解除の手順を明確にするものである。

---

## 7. Progress Status

Progress Status は「**この Issue が開発ライフサイクル上どこまで進んだか**」だけを
表す軸である。Type / Priority / Severity / Release Blocker から自動推論しない。

### 7.1 8つの状態

| label | 意味 |
|---|---|
| `status:未着手` | Issue は登録済みだが、Phase A の調査・設計にまだ着手していない |
| `status:調査・設計中` | Phase A / investigation / design を実施中。設計は未確定 |
| `status:設計済` | Phase A 完了。実装方針が確定している。コード実装は未開始 |
| `status:開発中` | branch 上でコード・設定・docs 等の実装を開始しており、implementation complete に未到達 |
| `status:開発済` | implementation complete。必要な branch test / PR / CI まで到達しているが main へ未 merge(原則 OPEN PR + implementation complete + CI green) |
| `status:マージ済` | main へ merge 済み。Production へ反映すべき変更があるが、まだ deploy されていない |
| `status:デプロイ済` | Production 反映済み。ただし Issue が要求する必須 Production verification が未完了 |
| `status:本番検証済` | Issue 固有の必須 verification が完了し、技術的には close 可能な状態 |

`status:設計済` は Human decision 待ちでも成立する。設計が完了しているなら status は
`設計済` のままとし、待ち理由は §8 の waiting label で補助表現する。

### 7.2 排他制約

```
STATUS_LABEL_COUNT_PER_OPEN_ISSUE = 1（0 個禁止 / 2 個以上禁止）
```

status を遷移させるときは、**旧 status label を remove して新 status label を add** する
(置換)。履歴目的で複数の status を残さない。履歴は Issue State Snapshot が保持する。

```
status:設計済  --(implementation start)-->  status:開発中
```

### 7.3 進捗の測り方(複数 Phase を持つ Issue)

1つの修正対象を段階的に進める Issue(Phase A / B-1 / B-2 / …)では、
**到達した最も進んだ工程**を status とする。後続 Phase が残っていることは、
Issue が OPEN であること自体と Issue State Snapshot が表す。

umbrella / tracking Issue のように「完了」が単一の工程で定義できない Issue では、
到達点ではなく **その Issue の現在の活動段階**を status とする(活動中の tracking Issue は
`status:調査・設計中`)。

### 7.4 Production 変更を伴わない Issue

すべての Issue が Production lifecycle を通るわけではない。

```
test-only / docs-only / governance / investigation / tracking /
accepted-risk / not-a-bug
```

これらでは、不要な `status:マージ済` / `status:デプロイ済` を経由する必要はない。
その Issue 固有の完了条件(main CI green、deterministic verification、
documentation verification 等)を満たした時点で `status:本番検証済` へ進めてよい。

```
status:本番検証済 = 「Issue 固有の最終 verification 完了」を含む広義の final verified state
```

名称に「本番」が含まれるが、**Production 変更が存在しない Issue にも適用される**。
これは承認済みの 8 段階名称を維持したうえでの定義であり、名称の読み替えではなく
定義の明文化である。

### 7.5 GitHub state との関係

GitHub の `state=CLOSED` が Issue 完了そのものを表すため、`status:完了` という label は
新設しない。

```
CLOSED_ISSUE_STATUS_LABEL_POLICY = KEEP_FINAL_STATUS
```

close 時は最後の status label をそのまま残す。過去 Issue の一覧でも
「どこまで実証されて close されたか」が判別できるためである。
既存の CLOSED Issue への一括 backfill は必須としない。

---

## 8. waiting metadata(補助状態)

waiting label は Progress Status とは独立した補助軸であり、**判定軸ではない**。

| label | 意味 |
|---|---|
| `waiting:本番検証` | 実装 / merge / deploy 等は進んでいるが、自然実行・所定時刻・Production 観測等を待っている |
| `waiting:人間判断` | 技術調査・設計等は完了しているが、Human decision / approval がないと次工程へ進めない |
| `waiting:外部条件` | 外部サービスの事象、データ蓄積、自然障害の発生、外部情報の到着等を待っている |

```
WAITING_LABEL_COUNT_PER_ISSUE = 0 個以上（複数併用可。ただし必要最低限）
```

### 8.1 status と waiting の違い

```
status  = どこまで完了したか
waiting = なぜ今進んでいないか
```

両者は併用する。

```
status:デプロイ済 + waiting:本番検証
status:設計済     + waiting:人間判断
status:デプロイ済 + waiting:外部条件
```

### 8.2 waiting:人間判断 を付けない場合

merge 承認・Production ChangeSet EXECUTE 承認のように、**全 Issue 共通の短時間 gate**
ごとに機械的に付け外ししない。Issue が実質的に Human decision blocked になっている
場合にのみ使う。

### 8.3 重複を避ける

主因が明確なら 1 つに絞る。例えば「自然な障害事象の発生待ち」が主因の Issue では
`waiting:外部条件` を優先し、`waiting:本番検証` を重ねない。

---

## 9. Issue State Snapshot との関係

Progress Status label は **derived metadata** であり、Issue State Snapshot の代替ではない。

| | 役割 |
|---|---|
| GitHub label | 人間が Issue 一覧で現在地を把握するための粗い状態 |
| Issue State Snapshot | AI / review / gate 判断に使う詳細な SSoT |

```
SSoT 優先順位
  Issue State Snapshot / GitHub factual state（PR / merge / deploy evidence）
    ↓
  status label
```

### 9.1 label 同期の運用契約

```
STATE_TRANSITION_WRITEBACK_REQUIRED = YES（既存）
STATUS_LABEL_WRITEBACK_REQUIRED     = YES（新規）

WORKER_STATE_WRITE_OWNER        = ACTOR_WHO_CHANGED_STATE（既存）
WORKER_STATUS_LABEL_WRITE_OWNER = ACTOR_WHO_CHANGED_STATE（新規）
CHATGPT_STATE_READ_OWNER        = CHATGPT（既存・変更なし）
```

state を変更した当人が、同じ作業の中で status label も同期する。
同期対象となる state transition は次のとおり。

| transition | status |
|---|---|
| Issue created | `status:未着手` |
| PHASE_START | `status:調査・設計中` |
| PHASE_COMPLETE | `status:設計済` |
| IMPLEMENTATION_START | `status:開発中` |
| IMPLEMENTATION_COMPLETE(未 merge) | `status:開発済` |
| PR_MERGED(Production deploy が必要) | `status:マージ済` |
| PRODUCTION_DEPLOYED(verification 未了) | `status:デプロイ済` |
| PRODUCTION_VERIFIED | `status:本番検証済` |
| ISSUE_CLOSED | 最終 status を維持(`KEEP_FINAL_STATUS`) |

`OWNER_CHANGE` は status の変更理由にならない。

### 9.2 stale / 不整合 label の扱い

Assignment read barrier では、latest Issue State Snapshot と current status label の
整合を確認する。食い違う場合は次のように扱う。

```
例: snapshot は IMPLEMENTATION_START、label は status:未着手
  -> STATUS_RECONCILIATION_REQUIRED
```

このとき、**stale または missing な label だけを理由に既存の事実を捨てない。**
GitHub comments / PR / merge / deploy evidence から reconcile し、
事実に合わせて label を直す。逆はしない。

判定に足る証拠が得られない場合は、推測で label を付けず
`STATUS_RECONCILIATION_REQUIRED` として Issue 番号と不足証拠を報告する。

---

## 10. 推測して埋めない

Progress Status も推測で埋めない。証拠が得られない場合は
`status:未着手` を便宜的に付けず、`STATUS_RECONCILIATION_REQUIRED` として
Issue 番号と不足証拠を報告する(判定手順は §9.2)。

**最重要原則: label を「埋めること」を目的にしない。**

目的は、Issue の性質・対応優先順位・影響度・release 可否を、
**人間と AI の双方が同じ意味で判断できる状態にすること**である。

判断するための十分な根拠がない場合、**無理に label を付けない。**
特に既存 Issue について、「本文に Priority / Severity が書いていない」という理由だけで、
コード内容から勝手に P0/P1 や SEV-2 等を決めてはならない。

不明なものを推測して label 付けするより、
**「未確定」と明示して判断を求めることを優先する。**

### 未設定の2種類を区別する

`severity:*` が付いていない状態には、**意味の異なる2種類**がある。混同してはならない。

| 状態 | 意味 | 記載 |
|---|---|---|
| **N/A(適用対象外)** | Severity という**評価軸自体が適用されない**。欠陥ではない Issue(`enhancement` / `tracking` / `calibration` など) | 本文へ `Severity: N/A` と**確定判断として**記載する |
| **未確定(TRIAGE_REQUIRED)** | Severity を**評価すべき** Issue だが、根拠不足でまだ決定できない | `SEVERITY_TRIAGE_REQUIRED` として報告し、判断を求める |

Priority についても同じ区別を維持する
(「適用対象外」と「未確定」を将来的に混同しない)。
ただし通常の実装 Issue では Priority を設定することを基本とする。

### 報告用語(GitHub label ではない)

次の語は **GitHub の label ではなく、AI から人間への status / report vocabulary** である。
**label として作成・付与してはならない。**

| 用語 | 使う場面 |
|---|---|
| `PRIORITY_SEVERITY_TRIAGE_REQUIRED` | Priority と Severity の双方が未確定 |
| `SEVERITY_TRIAGE_REQUIRED` | Severity のみ未確定 |
| `PRIORITY_TRIAGE_REQUIRED` | Priority のみ未確定 |
| `LABEL_CONSISTENCY_DECISION_REQUIRED` | 本文と labels が矛盾し、どちらが最新の確定判断か人間の判断が要る |

---

## 11. label 化しないもの

### Phase を label 化しない

同じ Issue で Phase A / Phase B-1 / Phase B-2 / Production verification 等が
進行する場合でも、**Phase ごとの label を追加しない。**
Phase は Issue comment で管理する。

### 細粒度の一時的 status を label 化しない

§7 の Progress Status(8段階)と §8 の waiting metadata は label 化する。
それより細かい次のような一時的状態は、引き続き label 化しない。

```
IMPLEMENTATION_REVIEW_REQUIRED   PR_CREATED      PR_CI_GREEN
MERGE_READY                      PRODUCTION_PENDING
HANDOFF                          PAUSED          WAITING_REVIEW
```

これらは **Issue comment / PR state / GitHub Actions / Git branch** を
truth source とする。labels は Progress Status の粒度までに留める。

### Issue label を PR へコピーしない

Issue labels は Issue の分類情報である。
`priority:P0` / `severity:SEV-2` / `bug` 等を PR へ機械的にコピーしない。
PR label が必要な場合は、PR 独自の目的に応じて別途判断する。

---

## 12. tracking Issue の扱い

tracking Issue では、**子 Issue の Priority / Severity を親へ機械的に伝播しない。**

配下に P0 / SEV-2 や P1 / SEV-3 の Issue が存在しても、
tracking Issue 自身に `priority:P0` / `severity:SEV-2` を付けない。

**親 Issue 自身に Priority / Severity が明示的に定義されている場合のみ**設定する。

---

## 13. 運用フロー

### 13.1 新規 Issue 作成時

Issue 登録は「本文を書く → labels を設定する」までを**1つの作業**とする。
Issue だけ登録して labels を後回しにしない。

```
duplicate 検索
  ↓
Issue 作成
  ↓
5軸判定(Type / Priority / Severity / release-blocker 要否 / Progress Status)
  ↓
labels 設定
  ↓
Label consistency 確認
```

本文にも次のように記載し、**labels と一致させる**
(labels だけを truth source にしない)。

```
Classification: BUG
Priority:       P1
Severity:       SEV-2
Release blocker: NO
Progress status: 未着手
```

新規 Issue の Progress Status は `status:未着手` から始める。

### 13.2 Issue 着手時

実装開始前に必ず **Issue 本文 / 最新コメント / labels** を確認する。
このとき latest Issue State Snapshot と current status label の整合も確認する
(§9.2)。

矛盾がある場合、**そのまま実装を開始しない。**
明らかな label 同期漏れなら修正する。判断が必要なら
`LABEL_CONSISTENCY_DECISION_REQUIRED` として報告する。
status label と snapshot が食い違う場合は
`STATUS_RECONCILIATION_REQUIRED` として扱い、事実に合わせて label を直す。

着手報告には次を含める。

```
Labels: ...
Label consistency: PASS / FAIL
Status label: <現在の status:>
```

FAIL なら実装開始前に解消する。

着手して Phase A を開始したら、その作業の中で status label を
`status:未着手` から `status:調査・設計中` へ置換する
(`STATUS_LABEL_WRITEBACK_REQUIRED = YES`、§9.1)。

### 13.3 investigation 完了時

調査後は結論に合わせて labels を再評価する。

| 結論 | 操作 |
|---|---|
| CONFIRMED_BUG | `investigation` を外す → `bug` を付ける → Priority / Severity を確定 → release-blocker 要否を判断 |
| DESIGN_DEFECT | `investigation` を外す → `design-defect` を付ける |
| NOT_A_BUG | `investigation` を外す → `not-a-bug` を付ける → 必要に応じて `NOT_PLANNED` で close |
| ACCEPTED_RISK | `investigation` を外す → `accepted-risk` を付ける → 受容理由・残余リスクを記録 |

分類が変わった場合、**Issue 本文の過去の判断を削除しない。**
取り消し線などで「当初そう判断したが、調査によって撤回した」という
**監査証跡を残す**(実例は §14 の #79)。

### 13.4 Issue close 前

close する前に最終 Label consistency check を行う。

- Type は最終結論と一致しているか
- Priority は本文と一致しているか
- Severity は本文と一致しているか(N/A と未確定を取り違えていないか)
- `release-blocker` を残すべきか
- `bug` + `not-a-bug` になっていないか
- `investigation` のまま結論済みになっていないか
- `accepted-risk` の根拠が記録されているか
- status label が到達した最終工程を表しているか(通常は `status:本番検証済`)

矛盾を解消してから close する。close 時に status label は削除せず、
最終 status をそのまま残す(`KEEP_FINAL_STATUS`、§7.5)。
完了報告には次を含める。

```
Final labels: ...
Label consistency: PASS
Final status: <status:...>
```

### 13.5 Production release 前

OPEN な `release-blocker` を検索する。

ただし **`release-blocker` label がある = 必ず今回 release 禁止、とは限らない。**
各 Issue の本文 / 最新コメントの block 条件を確認し、
今回の release 対象 commit に block 対象機能が含まれるかを判定する(§6)。

### 13.6 別 bug を発見した場合

作業中の Issue とは別の bug / design-defect を発見した場合:

```
既存 Issue 検索 → 重複有無確認 → 無ければ新規 Issue 登録
  → Type 設定 → Priority 検討 → Severity 検討
  → release-blocker 要否検討 → Parent / Related 記載
  → 現在の Issue では原則修正しない
```

**opportunistic fix は禁止。**

---

## 14. 実例

以下は本ポリシーの適用例である。
**Issue 番号はあくまで「例」であり、ポリシーの定義そのものを
個別 Issue の事情へ依存させない。**

| Issue | labels | ポイント |
|---|---|---|
| #81 | `bug` `priority:P0` `severity:SEV-2` `release-blocker` | 判定軸がすべて立つ例 |
| #82 | `design-defect` `priority:P1` `severity:SEV-3` `release-blocker` | 安全性 bug ではないが、特定機能を含む release を止める**条件付き blocker** |
| #85 | `enhancement` `priority:P0` | P0 でも `release-blocker` を付けない例。Severity は N/A |
| #49 | `tracking` | 子 Issue に P0 / SEV-2 があっても親へ伝播しない |
| #73 | `tracking` `priority:P2` | 親自身の本文に Priority 明示があるため設定した例 |
| #79 | `not-a-bug`(CLOSED / NOT_PLANNED) | 調査結果に応じて再分類し、本文に撤回の監査証跡を残した例 |
| #87 | `enhancement` `priority:P2` | Severity **N/A**(適用対象外)であり、未確定ではない例 |

---

## 15. 現行 label 一覧

| label | description |
|---|---|
| `bug` | Something isn't working |
| `design-defect` | 設計上の欠陥(安全性バグではないが機能が成立しない) |
| `enhancement` | New feature or request |
| `investigation` | 調査してbug/仕様/許容リスクの結論を出す(欠陥確定前) |
| `calibration` | 実績データ蓄積後の閾値・パラメータ再評価(欠陥ではない) |
| `tracking` | 親Issue・追跡表(それ自体は修正対象ではない) |
| `not-a-bug` | 調査の結果、欠陥ではないと確定 |
| `accepted-risk` | 既知だが対応しないと判断したリスク |
| `priority:P0` | 最優先で対応する |
| `priority:P1` | 高 |
| `priority:P2` | 中 |
| `priority:P3` | 低 |
| `severity:SEV-1` | 重大(誤った投資判断・データ破壊に直結) |
| `severity:SEV-2` | 高(安全機構の無効化・仕様違反) |
| `severity:SEV-3` | 中(機能低下・可観測性の欠如) |
| `severity:SEV-4` | 低(軽微・表示のみ) |
| `release-blocker` | 次回Production releaseの前に解消が必要 |
| `status:未着手` | Progress Status: 登録済みだがPhase A調査・設計に未着手 |
| `status:調査・設計中` | Progress Status: Phase A / 調査・設計を実施中(設計確定前) |
| `status:設計済` | Progress Status: Phase A完了。実装方針が確定、コード実装は未開始 |
| `status:開発中` | Progress Status: branch上で実装中(implementation complete未到達) |
| `status:開発済` | Progress Status: implementation complete。PR/CIまで到達、main未merge |
| `status:マージ済` | Progress Status: mainへmerge済み。Production反映が必要だが未deploy |
| `status:デプロイ済` | Progress Status: Production反映済み。必須verificationが未完了 |
| `status:本番検証済` | Progress Status: Issue固有の最終verification完了。技術的にclose可能 |
| `waiting:本番検証` | 補助metadata: 自然実行・所定時刻・Production観測を待っている |
| `waiting:人間判断` | 補助metadata: Human decision / approvalがないと次工程へ進めない |
| `waiting:外部条件` | 補助metadata: 外部事象・データ蓄積・外部情報の到着を待っている |

`release-blocker` の実際の適用範囲は、Issue 本文の block 条件も確認すること(§6)。

label description を変更する場合は、**意味が他軸と重ならないこと**を確認する
(例: `priority:P0` の説明に「次回リリース前に着手」と書くと
Release Blocker 軸と混同されるため不可)。

---

## 16. 変更履歴

| 日付 | 変更概要 |
|---|---|
| 2026-08-30 | 初版作成(#87)。4軸モデル、Type 8種と排他関係、Priority / Severity の定義、条件付き release-blocker、推測禁止と報告用語、Severity の「N/A」と「TRIAGE_REQUIRED」の区別、Phase / status を label 化しない方針、tracking への非伝播、運用フロー、実例を規定。 |
| 2026-09-03 | §6 へ blocking target semantics を追加(#122)。`release-blocker` 付与時の必須記録(`BLOCKER_MODE` / `BLOCKING_TARGET_TYPE` / `BLOCKING_TARGET` / `BLOCK_REASON` / `BLOCKER_SCOPE` / `BLOCKER_REMOVAL_CONDITION` / `BLOCKER_ADDED_AT`)を定め、必須記録が不足する場合は fail-closed(`BLOCKER_METADATA_COMPLETE=NO` / `RELEASE_DECISION=INSUFFICIENT_EVIDENCE`)として blocker を無視した release を禁止した。`BLOCKER_MODE` の `DEFECT_BLOCK` / `VERIFICATION_HOLD` を定義し、`BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER`(remediation release を自身の blocker で禁止しない)と、その許可条件7点を明文化した。既存 blocker の metadata は一括書き換えせず次回 status update 時に同期する。**既存の4軸独立性・条件付き blocker・Production-target defect の lifecycle・Issue close と blocker 解除の分離はいずれも変更していない** |
| 2026-09-05 | 第5軸 **Progress Status** を追加(#122)。`status:` 8種(未着手 / 調査・設計中 / 設計済 / 開発中 / 開発済 / マージ済 / デプロイ済 / 本番検証済)を定義し、OPEN Issue には常に1つだけ付与する排他制約(`STATUS_LABEL_COUNT_PER_OPEN_ISSUE = 1`)を規定した。判定軸ではない補助 metadata として `waiting:` 3種(本番検証 / 人間判断 / 外部条件)を追加し、「status = どこまで完了したか」「waiting = なぜ今進んでいないか」の区別を明文化した。Progress Status は derived metadata であり Issue State Snapshot の代替ではないこと(SSoT 優先順位は snapshot / GitHub factual state が上位)、`STATUS_LABEL_WRITEBACK_REQUIRED = YES` と `WORKER_STATUS_LABEL_WRITE_OWNER = ACTOR_WHO_CHANGED_STATE`、state transition と status の mapping、不整合時の `STATUS_RECONCILIATION_REQUIRED` を規定した。close 時は最終 status を残す(`CLOSED_ISSUE_STATUS_LABEL_POLICY = KEEP_FINAL_STATUS`)。Production 変更を伴わない Issue(test-only / docs-only / governance / investigation / tracking 等)はマージ済・デプロイ済を経由せず、Issue 固有の最終 verification 完了をもって本番検証済としてよい。**既存の Type / Priority / Severity / Release Blocker の定義と独立性は変更していない** |
