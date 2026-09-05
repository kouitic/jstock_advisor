# AI 運用のメッセージ契約

**この文書の位置づけ**

作業 AI・ChatGPT・人間の間で交換されるメッセージの**形式**の正本(SSoT)である。

```
Worker の完了報告(NORMAL / FORENSIC)
BASELINE_INVARIANTS
報告の転送契約(一括コピー可能性)
Instruction の許可範囲(AUTHORIZED_PHASES)
Human Gate の提示フォーマット
確認質問の要否ポリシー
UNKNOWN の扱い
```

AI 非依存のリポジトリ運用ポリシーであり、特定の AI エージェントやセッションに
依存しない。[CLAUDE.md](../CLAUDE.md) は本文書への入口にすぎない。

本文書が定めるのは**メッセージの形式**であり、承認の要否・作業の可否そのものでは
ない。それらの正本は次のとおりで、本文書はいずれも複製しない。

```
開発 lifecycle / SSoT 書き戻し / Assignment Read Barrier /
WIP 運用 / state reconciliation        -> development_workflow.md
ChatGPT の責務 / Human とのやり取りの境界 /
レビュー判定 / Instruction ID の採番     -> chatgpt_collaboration_protocol.md
Issue の 4 軸 label                     -> issue_label_policy.md
機能領域 / 共通部品                      -> functional_domains.md
Production の運用手順                    -> operations_manual.md
```

**ルールを変更する場合は本文書を更新する。**

---

## 0. 現在の発効状態

```
NEW_CONTRACT_ACTIVE = NO
```

**本文書が main に入っただけでは発効しない。** 発効までは、報告形式は
[development_workflow.md](development_workflow.md) 2.5.5、Human Gate の提示は
[chatgpt_collaboration_protocol.md](chatgpt_collaboration_protocol.md) 3.7 の
現行運用が有効である。発効の手順と境界は 8節に定める。

---

## 1. なぜ形式を正本化するのか

形式の正本が無いと、Instruction ごとに報告項目が定義され、次の二重化が起きる。

```
Instruction 側  完了報告のフィールド一覧を毎回書き下ろす
Worker 側       ISSUE_STATE_SNAPSHOT の内容をチャット報告へほぼ丸ごと再掲する
```

実測(Issue #177 の 7 コメント)。

```
総文字数                     141,409
機械可読キーの延べ出現        659
うち毎回まったく同じ値だった   146 出現(22%)
```

毎回同じ値だった例。

```
DISCLOSURE = PUBLIC_SANITIZED  x14   RETROACTIVE_APPLICATION = NO  x13
STATE_DRIFT_DETECTED = NO      x9    PRODUCTION_CHANGED = NO       x9
CONFIG_CHANGED = NO            x7    INFRA_CHANGED = NO            x7
OTHER_ISSUES_MODIFIED = NO     x7    NEW_ISSUES_CREATED = 0        x7
```

これは運用の癖ではなく契約の欠落である。したがって「気をつけて短く書く」では
解決せず、正本を置く必要がある。

### 圧縮してよい範囲を channel で分ける

**ここを混ぜると監査可能性を壊す。**

| channel | 目的 | 圧縮 |
|---|---|---|
| C-1 Worker -> ChatGPT のチャット報告 | 次工程の判断材料 | **してよい** |
| C-2 Issue コメント / ISSUE_STATE_SNAPSHOT | durable な監査記録・SSoT | **必須項目を削らない** |
| C-3 ChatGPT -> Human の提示 | 人間の意思決定 | **してよい**(監査情報は分離) |

```
COMPACT_REPORT != SSOT_WRITEBACK_OMISSION
```

C-2 の必須項目(development_workflow.md 6.5.3)は削らない。C-2 で削れるのは、
同じ事実を散文と snapshot で二重に書いている部分である。

---

## 2. Worker の完了報告

### 2.1 固定するのは schema であり長さではない

```
FIXED_SCHEMA_NOT_FIXED_LENGTH = YES
```

論理フィールドを固定し、各フィールドは複数行を取ってよい。行数を短くするために
フィールドを落とすことは禁止する。

### 2.2 NORMAL_REPORT の必須フィールド

```
INSTRUCTION_ID       単一行
ASSIGNEE             単一行
INSTRUCTION_STATUS   単一行(ANSWERED)
REPORT_MODE          NORMAL | FORENSIC
RESULT               DONE | BLOCKED | NO_CHANGE_NEEDED
CHANGED_STATE        複数行可。実際に変わったことだけ。無ければ NONE
MATERIAL_FINDINGS    複数行可。次工程の判断を変える発見。無ければ NONE
VALIDATION           複数行可。実施した検証と結果
BASELINE_INVARIANTS  3節の形式
DURABLE              Issue コメント / snapshot の URL。不要だった場合は NONE
READY_FOR            次工程
```

Production に関わる Instruction では、上記に加えて必須とする。

```
PRODUCTION_CHANGED   YES | NO
```

```
Production 関連以外では PRODUCTION_CHANGED を書かない(baseline に含まれる)。
Production 関連では baseline に頼らず毎回明示する。取り違えの代償が大きい。
```

### 2.3 省略と違反の区別

```
CHANGED_STATE = NONE は有効な報告である。
「何も変わらなかった」ことは delta の 1 つであり、省略ではない。

state を変えたのに DURABLE = NONE は契約違反である。
DURABLE = NONE を使えるのは、state が変わらず writeback が不要だった場合に限る
(development_workflow.md 6.5.7)。
```

### 2.4 例

```
INSTRUCTION_ID     = JIRO-20260906-0NN
ASSIGNEE           = JIRO
INSTRUCTION_STATUS = ANSWERED

REPORT_MODE = NORMAL
RESULT      = DONE

CHANGED_STATE
  #181  status:開発中 -> status:開発済
  PR    #183 created / head 4f9c1a2
  CI    PASS(run 33979357372 / 7 of 7 / head 一致)

MATERIAL_FINDINGS   = NONE
VALIDATION          = ruff PASS / mypy PASS / targeted 15 passed
BASELINE_INVARIANTS = PR_ONLY_BASELINE:UNCHANGED

DURABLE   = https://github.com/<owner>/<repo>/issues/181#issuecomment-...
READY_FOR = CHATGPT_PR_REVIEW
```

---

## 3. BASELINE_INVARIANTS

### 3.1 目的

「変えていないこと」を毎回列挙する代わりに、phase ごとの既定集合を名前で指す。

```
BASELINE_INVARIANTS = DOCS_ONLY_BASELINE:UNCHANGED
```

逸脱がある場合は必ず明示する。

```
BASELINE_INVARIANTS = DOCS_ONLY_BASELINE
EXCEPTIONS          = NEW_ISSUES_CREATED = 1(#184)
```

```
禁止  baseline 名を書かずに UNCHANGED とだけ書く
      -> どの集合を指すか一意に決まらない

禁止  1 つでも逸脱しているのに UNCHANGED と書く
      -> baseline は「読み手が省略を復元できる」ことが前提であり、
         逸脱を黙ると復元が誤る
```

### 3.2 baseline 一覧

| baseline | 既定で起きないこと |
|---|---|
| `INVESTIGATION_BASELINE` | source / config / infra / tests / docs 変更、branch / commit / push / PR / merge、AWS mutation、Production 変更、他 Issue 変更、新規 Issue 作成、code WIP 取得 |
| `DESIGN_BASELINE` | INVESTIGATION_BASELINE と同じ。ただし対象 Issue への durable コメントと status label 変更は起こりうるため、それらは `CHANGED_STATE` へ書く |
| `DOCS_ONLY_BASELINE` | source / config / infra / tests / CI 変更、AWS mutation、Production 変更、merge、他 Issue 変更 |
| `CODE_IMPLEMENTATION_BASELINE` | infra / CI 変更、AWS mutation、Production 変更、merge、他 Issue 変更、PR 作成 |
| `PR_ONLY_BASELINE` | merge、AWS mutation、Production 変更、他 Issue 変更、追加の実装 |
| `POST_MERGE_BASELINE` | source / config / infra / docs 変更、branch / commit / push / PR、AWS mutation、Production 変更、他 Issue 変更、Issue close |
| `PRODUCTION_READ_ONLY_BASELINE` | あらゆる mutation(DynamoDB write / Lambda invoke / ChangeSet / deploy)、source 変更、秘密値の読み取り |
| `PRODUCTION_CHANGE_BASELINE` | 承認された exact ChangeSet 以外の変更、scope 外リソースへの操作、source 変更 |

```
DESIGN_BASELINE の注意

  「調査だけ」と「Issue の status を進める」は別である。
  Phase A で status を進める場合、それは baseline の逸脱ではなく
  CHANGED_STATE に書くべき成果である。
  ここを baseline へ入れると「変わったのに UNCHANGED」になる。
```

### 3.3 phase enum との関係

```
BASELINE_PHASE_MAPPING = MANY_TO_ONE
```

baseline と 5節の phase を 1 対 1 に対応させない。複数の phase が同じ baseline を
指してよい。

```
ISSUE_BOOTSTRAP / INVESTIGATE / DESIGN / STATE_RECONCILE
  -> いずれも「repo を変えない」という同じ性質で足りる

CHANGESET_CREATE / CHANGESET_EXECUTE
  -> phase としては別だが、baseline(何が起きないか)は共通
```

無理に揃えると、揃えるためだけの baseline が増えて曖昧さが戻る。両者は別の
列挙として保守する。

---

## 4. FORENSIC への昇格

### 4.1 昇格条件

次のいずれか 1 つでも該当したら compact を禁止する。

```
STATE_DRIFT                  SSoT と実体が食い違った
CI_FAILURE                   required check が失敗した
MERGE_CONFLICT               conflict が発生した
FILE_OVERLAP                 他 WIP と変更ファイルが重なった
DOMAIN_LOCK_CONFLICT         必要な領域が他者に保持されていた
UNKNOWN_STATE                判断に必要な事実が確定できなかった
SSOT_MISMATCH                Issue / label / snapshot / 実体が矛盾した
SECURITY_SENSITIVE_FINDING   秘密値・権限・PII に関わる発見
PRODUCTION_EXCEPTION         Production で想定外が起きた
UNEXPECTED_MUTATION          意図しない変更が発生した
PARTIAL_COMPLETION           指示の一部だけを完了した
INSTRUCTION_DEVIATION        指示と異なる判断をした
BASELINE_INVARIANT_EXCEPTION baseline から逸脱した
SCOPE_EXPANSION              宣言外の領域・共通部品を触る必要が生じた
EXTERNAL_STATE_CHANGE_DURING_WORK  作業中に main が進む等、外部状態が変わった
HUMAN_GATE_PRECONDITION_INVALIDATED  exact SHA / exact ChangeSet 承認の前提が失効した
```

```
REPORT_MODE = FORENSIC
```

### 4.2 FORENSIC で必ず出すもの

```
何が起きたか      観測した事実(推測と分離する)
いつ・どこで      timestamp / SHA / run id / path
どう判断したか    採った選択肢と、採らなかった選択肢
何が未確定か      UNKNOWN として残っているもの
影響範囲          他の Issue / 作業者 / Production への波及
必要な人間の判断  あるなら明示
生の証拠          ログ・diff・出力(省略しない)
```

```
「短くするため証拠を捨てる」設計は禁止する。
compact 化の対象は「毎回同じで既知の事実」だけであり、
異常時にしか出ない事実は一切圧縮しない。
```

### 4.3 mode の決定権

```
REPORT_MODE_OWNER = WORKER
```

異常が起きたかどうかは作業をした側にしか分からない。したがって Instruction は
5節の AUTHORIZED_PHASES を指定できるが、`REPORT_MODE` を指定できない。

```
Instruction が NORMAL 固定を強制することを禁止する。
```

```
判断に迷ったら FORENSIC を選ぶ。

REPORT_MODE_UNKNOWN -> FORENSIC(FAIL_VERBOSE)
```

---

## 5. Instruction の許可範囲(AUTHORIZED_PHASES)

### 5.1 禁止列挙から許可列挙へ

禁止事項を列挙する方式は、列挙漏れが「書いていないから許可」と誤読されうる。

```
AUTHORIZED_PHASES_REQUIRED = YES
UNLISTED_PHASE             = NOT_AUTHORIZED
```

### 5.2 phase enum

```
ISSUE_BOOTSTRAP        Issue の作成・本文・label の初期整備
INVESTIGATE            read-only 調査
DESIGN                 設計・提案(実装を伴わない)
IMPLEMENT              source / config / infra / docs / tests の変更と commit
PR_AND_CI              PR 作成と CI 確認
POST_MERGE             merge 後の状態同期
PRODUCTION_READ_ONLY   Production の read-only 観測
CHANGESET_CREATE       ChangeSet の作成
CHANGESET_EXECUTE      ChangeSet の実行
PRODUCTION_VERIFY      Production 検証
STATE_RECONCILE        SSoT の突合と書き戻し
```

`IMPLEMENT` を対象種別で分割しない。1 つの実装が source と docs にまたがることが
多く、phase を増やすと Instruction が再び長くなる。範囲の限定は 5.3 の補助 permission で
行う。

### 5.3 補助 permission

```
MERGE_ALLOWED       = YES | NO   既定 NO
ISSUE_CLOSE_ALLOWED = YES | NO   既定 NO
NEW_ISSUE_ALLOWED   = YES | NO   既定 NO
DOCS_SCOPE          = <許可する docs path のリスト>
```

```
例  AUTHORIZED_PHASES = INVESTIGATE, DESIGN
    AUTHORIZED_PHASES = IMPLEMENT, PR_AND_CI
                        MERGE_ALLOWED = NO
```

```
補助 permission は Human Gate を override しない。
MERGE_ALLOWED = YES であっても、merge には
chatgpt_collaboration_protocol.md 2節・2.6節の人間承認が別途必要である。
```

### 5.4 Human Gate との関係

```
AUTHORIZED_PHASES != HUMAN_GATE_APPROVAL
```

```
phase   「その種類の作業をしてよい」
gate    「この具体的な対象に対して実行してよい」

例  AUTHORIZED_PHASES = CHANGESET_CREATE
    -> ChangeSet を作る種類の作業は許可されている
    -> しかし exact ChangeSet の CREATE 承認は別途必要
    -> EXECUTE は別 phase かつ別 gate

どちらか一方だけでは実行できない。
```

phase enum と Human Gate の種別は**別の列挙として保守する**。名前が似ていても
1 対 1 に対応させない(`CHANGESET_CREATE` phase があっても、承認は exact ARN 単位)。

```
AUTHORIZED_PHASES != STATE_WRITE_PERMISSION_AUTOMATIC_GRANT
```

phase が許可されていても、development_workflow.md 6.5 の state 書き戻し義務は
消えない。

### 5.5 未記載の Instruction

```
AUTHORIZED_PHASES_MISSING
  -> INSTRUCTION_INVALID
  -> STOP
  -> 指示元へ再指示を要求する(調査も開始しない)
```

暗黙の既定を置かない。既定を置くと、記載漏れが「その範囲は許可された」という
既成事実になり、fail-closed のつもりで fail-open を作ることになる。

### 5.6 移行互換

```
PRE_ACTIVATION_RELAYED_INSTRUCTION -> GRANDFATHERED
```

本文書の発効(8節)時点で既に作業者へ渡っている Instruction は、発行時点の
契約のまま完了してよい。新しい契約を既存 Instruction へ遡及適用しない。

---

## 6. 報告の転送契約

作業 AI の報告は、**人間が手作業で ChatGPT へ転送する**ことを前提とする。内容が
正しくても、届いた時点で欠落すれば次工程の根拠にならない。

```
REPORT_IS_HUMAN_RELAYED = YES
```

```
規則

  報告全体を 1 回の操作で一括コピーできる構造にする
  1 つの外側コードブロックへ収められる形にする
  内部にコードフェンスが必要なら、外側のフェンスを長くする
    (内側が ``` なら外側は ```` にする)
  報告の一部を外側ブロックの外へ分散させない
  development_workflow.md 2.5.5 が定める冒頭 3 行は維持する
```

```
この契約は ChatGPT -> 作業 AI の指示文の出力形式
(chatgpt_collaboration_protocol.md 4.5)と対になるものであり、
同節が明示的に対象外としていた「作業 AI -> ChatGPT の報告」側を定める。
```

---

## 7. メッセージの対応付けと直列化

作業者ごとの Instruction 直列化は、人間による転送の安全性と回答の対応付けを
守るための統制である。**規則そのものの正本は
[development_workflow.md](development_workflow.md) 2.5節**であり、本文書へ複製しない。

本文書が定めるのは、それがメッセージ形式へどう現れるかである。

```
報告の冒頭 3 行(INSTRUCTION_ID / ASSIGNEE / INSTRUCTION_STATUS)は、
ChatGPT が PENDING を解消して次の Instruction を発行できるようにするための
対応付け情報である。省略・改変しない。

1 指示 1 回答を守る。複数の指示の結果を 1 つの回答へ混在させない。
```

```
PER_WORKER_INSTRUCTION_SERIALIZATION != PER_WORKER_CODE_WIP_MODEL
```

両者は直交する。詳細は development_workflow.md 2.5.3 を参照する。

---

## 8. Human Gate の提示フォーマット

### 8.1 固定 4 節 + 監査情報の分離

```
Human Gate — <GATE_TYPE> / <対象>

承認対象
  <何を承認するのか。1〜2 行>

承認すると起きること
  <承認直後に実行されること。箇条書き>

この承認では起きないこと
  <よく取り違えられる隣接操作を明示的に否定する>

推奨と理由
  <承認 | 保留 | 却下>
  <理由。判断の根拠となった事実>

---
AUDIT_INFO
  <SHA / run id / ARN / URL 等>
```

```
「起きないこと」を必須節にする理由

  本プロジェクトで繰り返し問題になったのは
  「merge したから Production も出たと思った」
  「ChangeSet を作ったから実行されると思った」
  という隣接操作の取り違えである。
  肯定形だけの提示では防げないため、否定形を固定節にする。
```

### 8.2 適用する gate 種別

| GATE_TYPE | 4 節を適用 | exact identifier の置き場所 |
|---|---|---|
| `DESIGN_GATE` | 可 | 不要 |
| `MERGE_GATE` | 可 | AUDIT_INFO でよい |
| `PRODUCTION_CHANGESET_CREATE_GATE` | 可 | **承認対象の本文へ残す** |
| `PRODUCTION_CHANGESET_EXECUTE_GATE` | 可 | **承認対象の本文へ残す** |
| `ROLLBACK_GATE` | 可 | **承認対象の本文へ残す** |
| `RELEASE_BLOCKER_REMOVAL_GATE` | 可 | AUDIT_INFO でよい(検証証拠は本文) |
| `ACTIVATION_GATE` | 可 | AUDIT_INFO でよい |

```
exact 承認では識別子が承認対象そのものである。

  ChangeSet の CREATE / EXECUTE と rollback では、承認は exact ARN /
  exact 対象に対してのみ有効である(chatgpt_collaboration_protocol.md 2節)。
  これを AUDIT_INFO へ追いやると「何を承認したか」が本文から消える。
```

### 8.3 承認単位を統合しない

```
APPROVAL_UNIT_CONSOLIDATION = NO
```

利用者向けの概念分類(Design 系 / Merge 系 / Production 系)と、実際の承認単位は
別物として扱う。**本文書は提示の形式を定めるだけであり、承認単位を 1 つも
統合・緩和しない。** 承認単位の正本は
[chatgpt_collaboration_protocol.md](chatgpt_collaboration_protocol.md) 2節である。

```
例  ChangeSet の CREATE と EXECUTE を「Production Gate 1 回」にまとめない。
    まとめると「作って内容を確認してから実行する」という安全弁が消える。
```

### 8.4 例

```
Human Gate — MERGE_GATE / PR #183

承認対象
  PR #183 を main へ merge すること

承認すると起きること
  - main への merge
  - main CI の実行と確認
  - Issue #181 の post-merge state reconciliation
  - #181 の code WIP 解放(main CI PASS と head 一致の確認後)

この承認では起きないこと
  - Production deploy
  - ChangeSet の CREATE / EXECUTE
  - Issue #181 の close
  - 新ルールの発効(別途 activation 承認が必要)

推奨と理由
  承認
  exact diff レビュー PASS / PR CI 7 of 7 green / 変更は docs のみ /
  merge blocker なし

---
AUDIT_INFO
  PR head  4f9c1a2...
  PR base  9bbc2ac...
  CI run   33979357372(event=pull_request / head 一致)
```

---

## 9. 確認質問の要否

### 9.1 判定

```
質問しない(既存の判断から一意に決まる)

  Q-1  現行 policy が唯一の答えを定めている
  Q-2  同じ scope について人間が既に明示承認している
  Q-3  分類等が機械的に一意に決まる
  Q-4  実装の詳細であり、利用者から見た挙動・trade-off を変えない
```

```
質問する(推測で埋めてはいけない)

  Q-5  合理的な仕様選択肢が複数あり、既存方針から一意に決まらない
  Q-6  不可逆・高リスクな操作
  Q-7  Human Gate の対象
  Q-8  Production の mutation
  Q-9  policy 同士が衝突している
  Q-10 SSoT が曖昧・矛盾している
  Q-11 調査しても解消しない unknown を埋めなければ進めない
  Q-12 利用者・業務への影響や trade-off が既存判断から決まらない
```

```
FEWER_QUESTIONS != GUESSING
FEWER_QUESTIONS != HUMAN_GATE_BYPASS
```

### 9.2 UNKNOWN の扱い

```
GUESS = FORBIDDEN
```

```
順序

  1  調査して確定できるか        -> 調査する(質問より先)
  2  SSoT から解決できるか        -> 解決する
  3  なお曖昧で、進行に必須       -> 質問する
  4  なお曖昧で、進行に不要       -> UNKNOWN のまま明示して進む
```

```
4 が重要である。「分からないから全部聞く」も「分からないから決め打つ」も
避ける。進行に不要な UNKNOWN は UNKNOWN と書いて残してよい。

判断そのものが UNKNOWN に依存する場合は fail closed とし、
4節の UNKNOWN_STATE として FORENSIC で報告する。
```

### 9.3 質問の形式

```
Q     <決めるべきこと>
A案   <内容> / 影響
B案   <内容> / 影響
推奨  <どちらか> / 理由
```

```
「どうしますか」だけの質問を禁止する。
判断材料を作るのは質問する側の責務である。
```

---

## 10. 発効の境界(本文書に固有)

```
NEW_CONTRACT_ACTIVE = NO
```

本文書の発効は、次をすべて終えたのち**人間の明示的な承認**をもって行う。

```
docs review -> PR -> CI -> 人間の merge 承認 -> merge -> main CI
-> 周知 -> 人間の activation 承認
-> domain WIP 集約 read model の tracking Issue 作成と初期化
-> 発効
```

```
MERGE_IS_NOT_ACTIVATION = YES
RETROACTIVE_APPLICATION = NO
```

発効時点で既に作業者へ渡っている Instruction と進行中の作業は、完了まで
発行時点の契約で扱う(5.6)。

```
本節は本文書の migration contract であり、
将来のすべての governance 変更へ適用される一般規則ではない。
一般化が必要になった場合は、その時点で別途 Human 判断を得る。
```

### 周知する項目

```
1  本文書の所在と、それが正本とする範囲
2  NORMAL_REPORT の必須フィールド(2節)
3  BASELINE_INVARIANTS の書き方と baseline 一覧(3節)
4  FORENSIC 昇格の 16 条件と、mode の決定権が作業者にあること(4節)
5  AUTHORIZED_PHASES の enum と、未記載時は STOP すること(5節)
6  報告の転送契約(6節)
7  Human Gate の固定 4 節(8節)
8  確認質問の要否と UNKNOWN の扱い(9節)
9  発効日時と、遡及適用しないこと
```

---

## 変更履歴

| 日付 | 変更概要 |
|---|---|
| 2026-09-06 | 新規作成(Issue #184)。作業 AI・ChatGPT・人間の間のメッセージ形式に正本が無く、Instruction ごとに報告項目が定義されていたため、「Instruction 側が毎回フィールドを書き下ろす」「Worker 側が ISSUE_STATE_SNAPSHOT をチャット報告へ再掲する」という二重化が構造的に発生していた(Issue #177 の 7 コメント 141,409 文字のうち、機械可読キー 659 出現中 146 出現が毎回同一値)。(1)圧縮してよい範囲を channel で分け、durable な snapshot の必須項目は削らないことを明記した(`COMPACT_REPORT != SSOT_WRITEBACK_OMISSION`)。(2)Worker の完了報告を `FIXED_SCHEMA_NOT_FIXED_LENGTH` として 11 の論理フィールドで固定し、Production 関連 Instruction のみ `PRODUCTION_CHANGED` を追加必須とした。`CHANGED_STATE = NONE` は有効な報告だが、state を変えたのに `DURABLE = NONE` は契約違反とした。(3)`BASELINE_INVARIANTS` を導入し、8 つの baseline を定義した。baseline 名の省略と、逸脱があるのに `UNCHANGED` と書くことを禁止した。phase enum とは `MANY_TO_ONE` とし、無理に 1 対 1 へ揃えない。(4)FORENSIC 昇格条件を 16 定め、`REPORT_MODE_OWNER = WORKER`(Instruction は NORMAL を強制できない)、迷ったら FORENSIC(FAIL_VERBOSE)とした。**短くするために証拠を捨てる設計を禁止**している。(5)`AUTHORIZED_PHASES`(11 phase + 補助 permission)を定め、`UNLISTED_PHASE = NOT_AUTHORIZED` / 未記載は `INSTRUCTION_INVALID` として STOP することとした。暗黙の既定を置くと記載漏れが既成事実になるためである。`AUTHORIZED_PHASES != HUMAN_GATE_APPROVAL` および `!= STATE_WRITE_PERMISSION_AUTOMATIC_GRANT` を明記した。(6)報告が人間により手作業で転送される前提を正本化し、一括コピー可能性を要求した(chatgpt_collaboration_protocol.md 4.5 が明示的に対象外としていた側)。(7)Human Gate の提示を固定 4 節 + AUDIT_INFO 分離とし、exact 承認では識別子を承認対象の本文へ残すこととした。**`APPROVAL_UNIT_CONSOLIDATION = NO` であり承認単位は 1 つも統合・緩和していない。** (8)確認質問の要否(Q-1〜Q-12)と UNKNOWN の扱い(調査 -> SSoT -> 質問 -> 明示保留)を定め、`GUESS = FORBIDDEN` とした。**本文書は形式の正本であり、承認の要否・作業の可否・WIP・label の規則はいずれも他文書が正本で、複製していない。** 作成時点で `NEW_CONTRACT_ACTIVE = NO` であり、merge だけでは発効しない。判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない |
