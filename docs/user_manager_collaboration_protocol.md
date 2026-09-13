# 利用者 ↔ 管理者 協働プロトコル

この文書は、**リポジトリの所有者(以下 利用者)と管理者** が、
複数の開発者(作業 AI)を使って開発を進めるときの
**会話・意思決定・レビュー・作業指示・承認の進め方**を定める。

```
USER_MANAGER_COLLABORATION_SSOT
```

## 0. この文書の位置づけ

### なぜ必要か

合意したルールがチャット履歴の中にしか無いと、次のことが起きる。

- 新しいセッションを開くとルールが失われ、統制が再現できない
- 「前にそう決めた」が、どの決定だったのか追えなくなる
- AI の記憶や要約に依存すると、内容が少しずつずれていく

そこで、**変わりにくい恒久ルールは GitHub の文書に置き、
そのつど変わる状態は文書に書かない**という分離を採る。

### 既存文書との責務分離

内容を重複させない。詳細は各文書が正本であり、ここからは参照する。

| 文書 | 責務 |
|---|---|
| [CLAUDE.md](../CLAUDE.md) | 作業 AI が最初に読む入口 |
| [docs/development_workflow.md](development_workflow.md) | 開発・レビュー・release・作業 AI 共通の開発プロセス |
| [docs/issue_label_policy.md](issue_label_policy.md) | Issue 分類 / Priority / release-blocker / Progress Status |
| [docs/operations_manual.md](operations_manual.md) | Production 運用手順 |
| **本文書** | **利用者 ↔ 管理者 間の会話・意思決定・レビュー・作業指示・Human Gate の運用** |
| `docs/policy_registry.yaml` | 操作 -> 読むべき正本の節の**索引**(`REGISTRY_IS_NOT_SSOT = YES`。規則本文を持たない) |

同じルールが複数箇所にあると必ず片方が古くなる。
本文書は「誰が何を決めるか」「どう指示し、どう受け取るか」に限定する。

---

## 1. 役割分担

### 目的

「推奨」と「決定」を取り違えないため。
管理者がどれだけ確信を持って推奨しても、それは承認ではない。

### 役割は権限と責務で定義する

```
PRODUCT_AGNOSTIC_ROLE_NAMING = YES
```

役割は**生成 AI 製品名でも個人名でも定義しない**。担当は変わりうるためである。
本文書は役割だけを定義し、**現在の担当は別に記録する**(10節)。

```
ROLE_ASSIGNMENT_SSOT = Issue #122 の最新の durable な体制記録
```

```
役割                       識別子
利用者・承認者             USER
管理者                     MANAGER
レビュワー                 REVIEWER
開発者(デプロイ権限あり)   DEVELOPER_WITH_DEPLOY
開発者(デプロイ権限なし)   DEVELOPER
```

本文書および関連文書で「作業 AI」と書かれている箇所は、
`DEVELOPER_WITH_DEPLOY` と `DEVELOPER` の総称である。

```
歴史的名称

  2026-09-06 まで管理者は ChatGPT 上の AI が担っていた。
  過去の Issue コメント・snapshot に残る "ChatGPT" / `ACTOR = CHATGPT` は
  当時の管理者を指す。append-only の記録であり書き換えない。
```

### ルール

**USER(利用者)**

```
要件・目的の決定
優先順位の最終判断
Human Gate の承認
Production 変更の承認
merge の承認と実行(MERGE_EXECUTOR = USER)
release-blocker 解除の承認
業務仕様上の最終意思決定
```

**MANAGER(管理者)**

```
MANAGER_ROLE = WORK_PLANNING_AND_EXECUTION_MANAGEMENT

利用者の要求・決定の整理
作業計画の作成と作業分解
優先順位の整理
開発者への作業指示と instruction queue の管理
担当割当の管理
Progress Status / Phase の管理
実績・dependency・scope・WIP の管理
management review(計画のレビュー / scope の確認 / 進捗・実績の確認 /
                 acceptance と Progress Status の管理上の確認)
  ★ 3.9〜3.14節の independent review ではない。あちらは REVIEWER が行う
  ★ MANAGER_REVIEW_CAN_SUBSTITUTE_INDEPENDENT_REVIEW = NO(下記の禁止を参照)
review trigger の管理
PHASE_1_INPUT_MANIFEST の作成(3.9節 / 3.14節)
REVIEWER への review 依頼と developer report の handoff
finding を受けた開発者への修正指示
Human Gate へ到達したかどうかの判定
release / verification の判定
Issue / PR / Production evidence の整合確認
Assignment Read Barrier の実行(5.5節)
LOCKED_DOMAINS / LOCK_LEVEL / compatibility evidence の検証(3.8節)
利用者への判断材料の提示
```

```
MANAGER_REVIEW_CAN_SUBSTITUTE_INDEPENDENT_REVIEW = NO

禁止  承認すること(推奨するだけ。承認は USER)
      merge の実行 / Production 操作 / AWS 操作
      判定語を独自に増やすこと(3節の4種から選ぶ)
      自分が管理した review target の最終 reviewer を兼務すること
      REVIEWER の finding を自分で消すこと
      REVIEWER の verdict を自分の判断だけで PASS へ変更すること
      REVIEWER を経由しない「MANAGER review 済み」を
      独立レビューの代替として扱うこと
```

**REVIEWER(レビュワー)**

```
REVIEWER_ROLE = INDEPENDENT_REVIEW

設計レビュー(3.10節)
コードレビュー(3.11節)
blind-first Phase 1(3.9節)
primary evidence の独立取得
current SSOT との照合
exact diff のレビュー
証拠の強度の分類(3.12節)
requirement traceability(必要な場合)
counterexample / missing path の確認(必要な場合)
反証確認(3.13節。必要な場合)
INDEPENDENT_REVIEW_SNAPSHOT の固定(3.14節)
Phase 2 での developer claim との比較
final review verdict
```

```
禁止  開発者へ直接実装を指示すること
      作業計画を変更すること
      担当割当を変更すること
      Progress Status を管理すること
      finding を自分で修正すること
      レビュー対象の code / docs を自分で変更すること
      merge / deploy を実行すること
```

```
REVIEWER_IS_REVIEW_ONLY = YES

レビューを成立させるための read-only 調査と、
許可された範囲の evidence 取得はレビューの責務に含む。
```

**DEVELOPER_WITH_DEPLOY(開発者・デプロイ権限あり)**

```
管理者の指示に基づく具体作業
調査 / 設計 / 実装 / テスト
GitHub 操作
evidence の収集
承認済み範囲の Production 作業(1.5節の PRODUCTION_DEPLOYMENT_EXECUTOR)
```

**DEVELOPER(開発者・デプロイ権限なし)**

```
DEVELOPER_WITH_DEPLOY と同じ。ただし deploy 実作業を行わない
(1.5節の DEPLOY_OPERATION_DELEGATION = FORBIDDEN_BY_DEFAULT)
Production の read-only 観測と evidence の分析は行う
```

```
2 つの開発者役割の差は「deploy 実作業を行うか」の 1 点だけである。
調査・設計・実装・報告・state 書き戻しの規則はすべて共通である。
```

**MANAGER と REVIEWER は別の役割である**

```
MANAGER_AND_REVIEWER = SEPARATE_ROLES
SAME_SESSION_DUAL_ROLE = FORBIDDEN
REVIEWER_ROLE_SEPARATION = REQUIRED
TARGET_REVIEW_FRESHNESS = REQUIRED(レビュー対象ごと。初回の Phase 1 の開始前)
SESSION_CREATION_FRESHNESS = NOT_REQUIRED(原則)
MANAGER_REVIEWER_ROLE_COMBINATION = FORBIDDEN_FOR_SAME_REVIEW_TARGET
DEVELOPER_INSTRUCTION_OWNER = MANAGER
REVIEW_FINDING_OWNER = REVIEWER
FINDING_REMEDIATION_INSTRUCTION_OWNER = MANAGER
```

**役割を分けることと、セッションを分けることは別である。**
役割が別でも、開発者の報告や管理者の結論を先に読んでいれば blind-first の
独立性は成立しない。**独立性は session の新しさではなく、レビュー対象ごとに
何を先に読んだかで決まる**(3.9節 / 3.14節)。

```
REVIEWER_ACTOR    役割としての REVIEWER(現在の担当は ROLE_ASSIGNMENT_SSOT)
REVIEWER_SESSION  原則 persistent。レビュー対象ごとに作り直さない

レビュー対象の分離は session を分けることでは作らない。
REVIEW_ID / PHASE_1_INPUT_MANIFEST / TARGET_FRESHNESS_CHECK /
INDEPENDENT_REVIEW_SNAPSHOT で作る(3.14節)。
```

**新しいレビュー対象ごとに、その対象についての blind-first を立て直す。**
同じ session を続けてよいが、対象ごとに `TARGET_FRESHNESS_CHECK` を通す
(`TARGET_REVIEW_FRESHNESS`)。
**適用の範囲と例外は 3.14節の `REVIEW_SESSION_REUSE_POLICY` が正本である**
(本節へ複製しない)。

```
REVIEWER != USER
REVIEWER_VERDICT != USER_APPROVAL

REVIEWER が PASS を出しても Human Gate(2節)を代替しない。
```

review の基本的な流れは 3.14節の `REVIEW_SESSION_LIFECYCLE` が正本である
(本節へ複製しない)。

**現在有効な役割規則が本節の役割分離か、その導入前の役割規則かは、
3.14節の `ACTIVE_ROLE_MODEL_SSOT` による**
(`ACTIVATION_STATE_SSOT` = Issue #353。発効前は本節が現在有効な規則ではない)。

**現在有効な review session の規則が本節の persistent 方式か、その導入前の方式かは、
3.14節の `ACTIVE_SESSION_POLICY_SSOT` による**
(`SESSION_POLICY_ACTIVATION_STATE_SSOT` = Issue #355。発効前は本節の session 規則は
現在有効な規則ではない)。

### 例

```
MANAGER   「MERGE_READY=YES。人間承認へ進んでよい」
          -> これは推奨であって承認ではない。merge してはいけない

USER      「PR #146 の SHA 9f4dac0e を merge してよい」
          -> ここで初めて merge できる
```

### 例外

なし。管理者が利用者の承認を代行することはない。

---

## 1.5 Production deploy 関連作業の担当

### 目的

Production への deploy 工程は、**手順の途中で担当が入れ替わると
前提が引き継がれない**。承認対象の exact SHA、承認対象の exact ChangeSet、
build 済み artifact の同一性といった前提は、一連の作業として保持される必要がある。

そこで実作業の担当を1体へ集約する。

```
PRODUCTION_DEPLOYMENT_EXECUTOR = DEVELOPER_WITH_DEPLOY
```

現在この役割を担う個体は ROLE_ASSIGNMENT_SSOT(1節)を参照する。
**本文書へ担当者名を焼き込まない。**

### ルール

`DEVELOPER_WITH_DEPLOY` が担当する範囲は最低限次を含む。

```
release 対象 SHA の最終確認
main CI の確認
release-blocker inventory の確認
clean worktree / unpushed の確認
sam build
Production ChangeSet CREATE
ChangeSet 内容の read-only 確認
人間の EXECUTE 承認後の ChangeSet EXECUTE
CloudFormation terminal state の確認
immediate Production verification
deploy artifact の同一性確認
stack event の確認
```

`DEVELOPER` は調査・設計・実装・targeted test・PR 作成・release readiness 調査・
Production Verification Plan の設計・Production evidence の read-only 分析まで
担当できるが、**deploy の実作業は既定で行わない**。

```
DEPLOY_OPERATION_DELEGATION = FORBIDDEN_BY_DEFAULT
対象役割 DEVELOPER

対象   sam deploy / ChangeSet CREATE / ChangeSet EXECUTE /
       Production config mutation / manual Production invoke /
       migration / backfill / failure injection / その他 deploy 実作業
```

### Human Gate は緩和しない

**担当を集約することと、承認が不要になることは無関係である。**

```
ChangeSet CREATE と ChangeSet EXECUTE は別の Human Gate
PR merge の承認は Production の承認ではない
main が進めば exact SHA の承認は失効する
ChangeSet を再作成すれば exact ChangeSet の承認は失効する
```

deploy 担当が 1 つの役割へ集約されていることは、
**人間承認なしに実行してよいという意味には一切ならない**(2節)。

### verification の担当は内容で分ける

deploy 直後の immediate verification は `DEVELOPER_WITH_DEPLOY` が担当する。

一方、自然実行後の**業務的な** Production evidence 分析は、
内容に応じて 管理者 が割り当ててよい。

```
運用寄り(stack / Lambda / IAM / scheduler / logs)
  -> DEVELOPER_WITH_DEPLOY を優先
業務ロジック寄り(分類比較 / スコア分布 / 業務判断)
  -> DEVELOPER へ read-only 分析を割当可
```

```
PRODUCTION_DEPLOYMENT_EXECUTOR = DEVELOPER_WITH_DEPLOY
  ≠ ALL_PRODUCTION_ANALYSIS_ASSIGNEE = DEVELOPER_WITH_DEPLOY
```

### 例外

ユーザーが明示的に別の担当を指定した場合は、
8節の `LATEST_EXPLICIT_HUMAN_DECISION_WINS_TEMPORARILY` に従う。

---

## 1.6 ユーザーへの説明の水準

### 目的

Human Gate は「人間が理解したうえで決める」ことが前提である。
**理解できない説明に対する承認は Human Gate として成立しない。**
内部コードや短縮表記だけを並べた回答は、受け取った側が意味を判断できないため、
承認・却下の材料にならない。

### 前提とする知識水準

```
USER_EXPLANATION_LEVEL = IT_FOUNDATION_AWS_LITERATE
```

ユーザーは **IT の基礎知識(応用情報技術者試験相当)を持ち、AWS の主要な
マネージドサービスの名称と概要を理解している**。
一方で **本プロジェクトの実装・運用の詳細は自明として扱わない**。

```
USER_CAN_MAKE_AN_INFORMED_DECISION = REQUIRED
```

説明の目的は「噛み砕くこと」自体ではなく、**ユーザーが根拠を持って
判断できる状態にすること**である。過剰な言い換えはむしろ因果関係を見えなくする。

### そのまま使ってよいもの

一般的な IT / AWS の用語は、毎回初歩から言い換えない。

```
Lambda / DynamoDB / CloudFormation / S3 / Secrets Manager / EventBridge / IAM
CI / PR / merge / main / Production / PITR / RPO / RTO
```

```
不要   「Lambda = サーバーを管理せずコードを実行するサービス」を毎回添える
禁止   AWS のサービス名まで一般語へ置き換える
```

### 説明が必要なもの

判断に効くのは用語の辞書的意味ではなく、**背景・因果関係・影響**である。
次は説明を添える。

```
A  本プロジェクト固有の運用概念
   BLOCKED_BY_RELEASE_SCOPE / waiting:本番検証 / grouped release /
   code WIP / Assignment Read Barrier / Issue State Snapshot 等

B  AWS でも挙動を取り違えやすい概念
   ChangeSet の CREATE と EXECUTE の違い
   CloudFormation の Dynamic Reference が再解決される条件
   Deletion Protection / DeletionPolicy / UpdateReplacePolicy の違い
   PITR の restore が新しいテーブルとして作られること
   merge 済みだが Production 未反映という状態

C  Human Gate の範囲
   今回何を承認するのか / 承認すると何が起きるか /
   この承認ではまだ何が起きないか / 次にどの承認が必要か
```

### 内部コードだけで回答しない

`ISSUE_166_PRODUCTION_GATE = BLOCKED_BY_RELEASE_SCOPE` のような
機械可読の状態値を**併記すること自体は禁止しない**(監査証跡として有用)。
ただし **それだけをユーザー向けの説明として提示してはならない**。

```
INTERNAL_STATUS_ONLY_RESPONSE = FORBIDDEN
```

### 最低限説明する内容

状況に応じて、次を含める。

```
1  今どうなっているか
2  なぜそうなっているか
3  今進めると何が問題・危険なのか
4  次に何をするのか
5  今ユーザーがすることは何か
6  次にユーザーの判断が必要になるのはいつか
```

ユーザーの操作・判断が不要なときは、
**「今あなたがすることはありません」と明示する**。
書かないと「何か待たれているのでは」と誤解させる。

### Human Gate の依頼

承認を依頼するときは最低これを説明する。

```
A  今回何を決めてもらいたいか
B  承認すると何が起きるか
C  この承認ではまだ何が起きないか
D  承認せず待つ場合どうなるか
E  管理者の推奨
F  その理由
```

C を落とすと、ユーザーは「承認＝本番反映」と受け取る。
2.5節の `PROPOSED / APPROVED / EXECUTED / VERIFIED` の区別が
説明の側で崩れないようにするための必須項目である。

### 回答の順序

```
1  結論
2  今どうなっているか
3  理由・影響
4  これからの順番
5  今ユーザーがすること
6  必要なら技術的な証拠
```

機械可読の状態値や SHA を回答の冒頭へ大量に並べることを標準としない。

### 技術的な正確さを落とさない

分かりやすさのために技術的な意味を変えない。特に次を混同させない。

```
ChangeSet の作成  !=  Production への反映
merge            !=  Production 承認
```

```
悪い例  「本番反映の準備が終わったので承認をお願いします」
        -> 何が起きるのか、まだ何が起きないのかが分からない

良い例  「PR #172 は main へ merge 済みで CI も通っていますが、
         Production の baseline から main までを deploy すると、
         #166 だけでなく未承認の #117 の CloudFormation 変更も含まれます。
         #117 では Lambda の Environment 更新により Secrets Manager の
         Dynamic Reference が再解決されるため、#166 単独のつもりで
         release することはできません。」
```

後者が想定する粒度である。用語を平易にするのではなく、
**因果関係と判断ポイントを平易に示す**。

```
禁止   技術情報を削りすぎて因果関係が見えなくなる説明
```

### 技術的な情報は残す

```
IT_FOUNDATION_AWS_LITERATE != TECHNICAL_DETAIL_FORBIDDEN
```

Issue 番号 / PR 番号 / SHA / CI run / ChangeSet の識別子 / 内部状態値は、
**監査証跡として残してよい**。ユーザー向けの説明と、監査用の情報を分けて示す。

### 適用範囲

本節が対象とするのは **管理者から利用者への回答**である。

```
対象      管理者 -> 利用者
対象外    開発者 -> 管理者 / ユーザーへの完了報告
          (機械可読形式を引き続き使用してよい。4.5節・7節の contract は不変)
```

### 例外

ユーザーが明示的に「内部状態だけ」「表だけ」等の形式を求めた場合は、
その形式を優先してよい。
ただしその場合も、Human Gate の意味(何が起きて何が起きないか)を
誤解させてはならない。

---

## 2. Human Gate

### 目的

取り返しのつかない操作を、人間の明示的な意思なしに実行しないため。

### ルール

少なくとも次はユーザーの明示承認が必要である。

```
PR merge
Production ChangeSet CREATE
Production ChangeSet EXECUTE
Production rollback / corrective mutation
release-blocker REMOVE
```

これに加えて、既存 governance が人間承認を要求する操作
(Production deploy / manual Production Lambda invocation /
Production data write / migration / backfill / failure injection 等)は
[development_workflow.md](development_workflow.md) 10節、
および [operations_manual.md](operations_manual.md) が正本である。
**本文書はそれらを緩和しない。**

承認の性質について、次の3点は特に取り違えやすい。

```
承認は「その操作・その対象」に限る       別の文脈へ拡張しない
ChangeSet の execute 承認は exact ARN のみ有効   再作成した時点で失効する
Issue close と release-blocker 解除は別判断      片方の承認は他方を含まない
```

### 例

```
「ChangeSet を CREATE してよい」
  -> CREATE のみ。EXECUTE は含まない

「PR #146 を merge してよい」
  -> merge のみ。Production deploy の承認ではない

「deploy が成功した」
  -> release-blocker 解除の条件を満たしたわけではない
```

### 提示のフォーマット

承認を求める際の**提示形式**(固定 4 節 + `AUDIT_INFO` の分離、gate 種別ごとの
exact identifier の扱い)は
[docs/ai_operation_message_contract.md](ai_operation_message_contract.md) 8節が
正本である。本文書は**どの操作に承認が要るか**を定め、形式を複製しない。

```
APPROVAL_UNIT_CONSOLIDATION = NO
```

同文書は提示形式を定めるだけであり、本節の承認単位を 1 つも統合・緩和しない。
同文書は既に発効している(発効状態の正本は同文書 0節の
`ACTIVATION_STATE_SSOT`)。

### 例外

なし。緊急時であっても Human Gate は省略しない。
急ぐ場合は、承認を得る速度を上げるのであって、gate を飛ばすのではない。

---

## 2.5 提案・承認・実行・検証を混同しない

### 目的

この4つは日常会話ではひとまとめに「終わった」と表現されがちだが、
**取り違えると、承認されていない操作を実行済みとみなす**という
最も危険な誤りにつながる。

```
STATE_SEPARATION                             = YES
PROPOSED_APPROVED_EXECUTED_VERIFIED_DISTINCT = YES
```

### ルール

```
PROPOSED   管理者 または作業 AI が提案・推奨しただけ
           人間の承認ではない

APPROVED   ユーザーが対象と操作を明示的に承認した状態
           まだ実行済みとは限らない

EXECUTED   承認された操作が実際に実行された状態
           成功が確認済みとは限らない

VERIFIED   実行結果を read-only の evidence 等で確認し、
           期待した状態になったことを検証した状態
```

前の状態が成立していても、次の状態を自動的には満たさない。

### 例

```
MERGE_READY = YES
  -> PROPOSED。merge を実行してよいという意味ではない

ユーザーが GitHub 上で merge した
  -> APPROVED + EXECUTED
     main CI を確認する前なので VERIFIED ではない

merge commit / origin/main / main CI SUCCESS を確認した
  -> VERIFIED
```

release-blocker についても同じ4段である。

```
release-blocker を解除できる状態だと判断した   PROPOSED
ユーザーが解除を承認した                       APPROVED
label を実際に削除した                         EXECUTED
削除後の GitHub state を確認した               VERIFIED
```

### 例外

なし。本節は 2節の Human Gate を弱めない。
`PROPOSED` がどれだけ強い推奨であっても `APPROVED` の代わりにはならない。

---

## 2.6 merge の実行者

### 目的

merge は Human Gate であり、実行主体を曖昧にしない。

```
MERGE_EXECUTOR = USER
```

### ルール

通常フローは次のとおり。

```
1  作業 AI が PR を作成する
2  管理者 が PR をレビューする
3  管理者 が MERGE_READY 判定を提示する
4  ユーザーが GitHub 上で直接 merge する
5  管理者 が必要に応じて read-only の post-merge 確認を行う
6  main CI を確認する
7  Production は別の Human Gate
```

通常は作業 AI へ「merge してください」という作業指示を出さない。

ユーザーが 管理者 へ事前に「merge を承認します」と宣言することは
**必須ではない**。管理者 が `MERGE_READY` を提示したうえで
ユーザー自身が GitHub で merge を実行した場合、

```
USER_MERGE_ACTION = HUMAN_APPROVAL + EXECUTION
```

として扱ってよい。承認と実行が同一の操作で成立している。

### MERGE_READY 判定に含める確認(G2)

上記 3 の `MERGE_READY` 判定では、diff の妥当性に加えて
**merge 後にその Issue がどう見えるか**を確認する。

```
この PR が merge された後、その Issue に別の Progress Status 相当の
残作業が残るか
```

残る場合、merge 後の Issue は「実装は終わっている」と読める label のまま
未実装の作業単位を抱えることになる。原則として **merge より前に Issue を分割する**
(判定ルールの正本は
[issue_label_policy.md](issue_label_policy.md) §7.3、
snapshot の記録項目は
[development_workflow.md](development_workflow.md) 6.5.3節)。

これは既存の review gate へ確認項目を 1 つ加えるものであり、
**新しい Human Gate を増やすものではない。** `MERGE_EXECUTOR = USER` も
2節の Production Human Gate も変更しない。

### 例外

ユーザーが明示的に「今回は作業 AI に merge させる」と決めた場合は、
8節の `LATEST_EXPLICIT_HUMAN_DECISION_WINS_TEMPORARILY` に従う。

管理者 が `MERGE_READY` を出していない PR をユーザーが merge した場合でも、
それは人間による実行であるから、**勝手に revert / rollback しない**。
必要なら post-merge review で状態と影響を確認する。

**PR を merge したことを Production の承認として扱ってはならない。**
Production deploy は本節とは別の Human Gate である(2節)。

---

## 3. 管理者のレビュー判定

### 目的

「証拠が足りない」と「不合格」は別物である。
これを混ぜると、確認不足のまま次へ進むか、逆に問題のない作業を止めることになる。

### ルール

```
FINAL_REVIEW_VERDICT_OWNER = REVIEWER(1節 / 3.14節)
REVIEW_KIND = MANAGEMENT_REVIEW | INDEPENDENT_REVIEW

本節は判定語を定める。review lifecycle 上で最終 verdict を出す主体は
REVIEWER であり、管理者は自分が管理した対象の最終 reviewer を兼務しない。
```

**判定語は両方の review で共通である。どちらの review の結論かは `REVIEW_KIND` で
示す。** review 結果を durable record へ残すときは `REVIEW_KIND` を verdict と
併記し、記録だけを読んで一意に判別できるようにする。

```
例  REVIEW_KIND = INDEPENDENT_REVIEW / REVIEW_VERDICT = PASS
    REVIEW_KIND = MANAGEMENT_REVIEW  / REVIEW_VERDICT = PASS

★ 判定語は 4 種のままである。REVIEW_KIND は判定語ではない。
```

使用する判定は4つ。

| 判定 | 意味 |
|---|---|
| `PASS` | 内容・証拠ともに要求を満たしている |
| `PASS_WITH_CONDITIONS` | 方針は妥当だが、明示した条件を満たすことを前提に前進してよい |
| `REJECT` | 内容に問題がある。修正が必要 |
| `INSUFFICIENT_EVIDENCE` | 良し悪し以前に、判断に必要な証拠が揃っていない |

特に重要な区別。

```
INSUFFICIENT_EVIDENCE  ≠  REJECT
    「間違っている」ではなく「まだ判断できない」。
    足りない証拠を特定して取得するのが次の行動であり、
    推測で PASS にしてはならない。

PASS_WITH_CONDITIONS   ≠  Human Gate 通過
    条件付き合格はレビューの結論であって、承認ではない。
    merge / deploy には別途ユーザーの承認が要る。
```

### 例

```
「実 provider の PRE_OPEN 実測が未取得」
  -> INSUFFICIENT_EVIDENCE。過去時刻を注入しても当時の挙動は再現できない。
     再現できないものを「たぶん大丈夫」として PASS にしない
```

### 例外

なし。判定語を独自に増やさない。

---

## 3.5 判断は証拠を先に置く(EVIDENCE FIRST)

### 目的

作業 AI の報告は**事実の主張であって事実そのものではない**。
重要な判断を自己申告だけで確定させると、報告と実体がずれたときに
誰も気づかないまま次工程へ進む。

```
EVIDENCE_FIRST = YES
```

### ルール

重要 Gate では、管理者 が read-only で確認できる情報を、
可能な範囲で GitHub / CI / Production の実体と突合する。

```
突合の対象例
  PR HEAD / PR の merge state / main SHA / CI 結果
  Issue state / label / release-blocker
  Production artifact / Production stack state / Production verification
```

報告と実体が矛盾した場合の手順。

```
1  差異を明示する
2  実体を優先して再確認する
3  根拠なく AI の報告を正しいものとして扱わない
4  それでも確定できなければ INSUFFICIENT_EVIDENCE とする
```

### 適用範囲

```
主対象   Human Gate / merge readiness / release readiness /
         Production verification / release-blocker removal /
         Issue close eligibility

対象外   軽微な作業報告を毎回過剰に検証すること
```

すべての報告を毎回検証し直すルールではない。
**誤ると取り返しがつかない判断**に絞って適用する。

**独立に取得する場合、その取得は開発者の報告を読む ★ 前に行う。**
順序だけを定めるものであり、本節の適用範囲は変えない
(入力の境界は 3.9節、証跡は 3.14節)。

### 例外

なし。ただし上記の適用範囲を超えて検証コストを広げない。

---

## 3.6 証拠の鮮度と検証可能性

### 目的

Issue 本文・過去のコメント・最新のコメント・現在の実体が食い違うとき、
どれを採用するかを決めておく。

```
FRESHER_VERIFIABLE_EVIDENCE_WINS = YES
```

### ルール

原則として次の順で採用する。

```
1  CURRENT_VERIFIABLE_STATE        現在の GitHub state / label / PR / code / CI
2  LATEST_DURABLE_VERIFIED_EVIDENCE 検証済みの最新 durable comment
3  OLDER_DURABLE_EVIDENCE           過去の durable comment
4  STALE_DESCRIPTIVE_TEXT           古い記述(Issue 本文の状況説明など)
```

**単純に「タイムスタンプが新しいものが常に勝つ」ではない。**
`freshness` と `verifiability` の**両方**で判断する。

次の場合、最新であっても無条件には優先しない。

```
単なる推測である
verification されていない自己申告である
現在の実体と矛盾している
```

### 例

```
古い Issue 本文が「Production は未対応」と書いていて、
現在の stack / artifact が対応済みであることを確認できる
  -> 現在の実体を採用する

最新の durable comment が過去の Issue 本文を訂正している
  -> 訂正後を採用する

最新コメントが「たぶん直っているはず」と書いている
  -> 検証されていないため採用しない。実体を確認するか
     INSUFFICIENT_EVIDENCE とする
```

### 例外

古い記述が stale であっても、**履歴として削除・改ざんしない**。
現在状態を明確にしたい場合は、最新の durable comment を追加するか、
本文を現在状態へ同期する。過去の記録を消して辻褄を合わせない。

---

## 3.7 merge 判断を支援する提示形式

### 目的

merge を実行するのはユーザーである(2.6節)。
したがって PR レビュー結果は、**ユーザーが GitHub 上で
merge するかどうかをその場で判断できる**形で提示する必要がある。

特に重要なのは、**残課題があること**と
**その残課題が今回の merge を止めるべきか**は別だという点である。
これが区別されていないと、止める必要のない PR が滞留する。

### ルール

管理者 が PR レビュー結果を提示する場合、
**PR 番号を必ず明示したうえで**、最低限次をセットで示す。

```
PR_NUMBER                    どの PR の話かを必ず明示する

REVIEW_VERDICT               PASS / PASS_WITH_CONDITIONS /
                             REJECT / INSUFFICIENT_EVIDENCE(3節)

MERGE_READY                  YES / NO

REMAINING_ISSUES_OR_CONCERNS NONE または具体的内容

MERGE_BLOCKING_CONCERN       YES / NO
                             残課題が今回の merge を止めるかどうか

OTHER_ISSUE_IMPACT           NONE または Issue 番号と影響

PRODUCTION_IMPACT            NONE / NOT_DEPLOYED / HAS_IMPACT 等

RECOMMENDED_ACTION           MERGE / FIX_BEFORE_MERGE / HOLD /
                             MERGE_AND_TRACK_SEPARATELY 等

INDEPENDENT_REVIEW_SNAPSHOT  3.14節の snapshot comment の URL
                             独立レビューを行った場合は省略しない

REVIEW_INPUT_EVIDENCE        Phase 1 で取得した primary evidence の identity
                             BASE_SHA / HEAD_SHA / MERGE_BASE / DIFF_HASH /
                             CI_RUN_ID 等(3.11節 / 3.14節)

REVIEW_KIND                  MANAGEMENT_REVIEW | INDEPENDENT_REVIEW(3節)
                             REVIEW_VERDICT を提示するときは併記する
```

```
残課題があっても今回の merge を妨げない   -> MERGE_BLOCKING_CONCERN = NO
merge 前に修正が必要                      -> MERGE_BLOCKING_CONCERN = YES
```

後ろの 2 項目は、**判定が何を読んで出されたか**を提示へ残すためのものである。
判定語だけを示すと、その判定を支える根拠は後からいくらでも作れる(3.14節)。
独立レビュー(3.9節〜3.14節)を行った場合、この 2 項目を省略しない。
(この「後ろの 2 項目」は `INDEPENDENT_REVIEW_SNAPSHOT` と `REVIEW_INPUT_EVIDENCE` を
指す。`REVIEW_KIND` を末尾へ足しても指示対象は変わらない。)

**`REVIEW_VERDICT` を提示するときは `REVIEW_KIND` を併記する。**
判定語は management review と独立レビューで共通であり(3節)、
種別を書かないと提示だけを読んだときにどちらの結論か分からない。

```
適用
  INDEPENDENT_REVIEW_SNAPSHOT から導出した verdict を提示する
                                        -> REVIEW_KIND = INDEPENDENT_REVIEW
  管理者自身の plan / gate / progress 等の management review の結果を提示する
                                        -> REVIEW_KIND = MANAGEMENT_REVIEW

禁止
  REVIEW_KIND を省略したまま REVIEW_VERDICT だけを提示し、
  review の主体を読み手に推測させること
  本項の追加を理由に 3節の 4 つの判定語を変更すること
  本項の追加を理由に本節の既存 8+2 項目の意味を変更すること

★ 本項は provenance(誰のどのレビューか)の field であり、判定語ではない。
★ review の内容・判定基準・Human Gate は変更しない。
```

### 例

```
PR #<番号>

  REVIEW_VERDICT = PASS
  MERGE_READY    = YES

  REMAINING_ISSUES_OR_CONCERNS =
    命名と意味に若干のズレがある

  MERGE_BLOCKING_CONCERN = NO

  OTHER_ISSUE_IMPACT =
    NONE。関連 Issue の後続 Phase は未着手のまま

  PRODUCTION_IMPACT = NOT_DEPLOYED

  RECOMMENDED_ACTION = MERGE

  INDEPENDENT_REVIEW_SNAPSHOT =
    https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>

  REVIEW_INPUT_EVIDENCE =
    BASE_SHA <base> / HEAD_SHA <head> / MERGE_BASE <mb>
    DIFF_HASH <hash> / CI_RUN_ID <run>

  REVIEW_KIND = INDEPENDENT_REVIEW
```

ユーザーはこれを見て GitHub 上で merge を判断できる。

### 例外

**この形式を Markdown の表へ固定しない。**
管理者の通常の回答として読みやすく提示できればよく、
項目が揃っていることが要件である。

なお、この提示自体は `PROPOSED` にとどまる(2.5節)。
`MERGE_READY = YES` は承認でも実行でもない。

### 発効後の正本

```
BEFORE_ISSUE_184_ACTIVATION  本節が提示形式の正本である
AFTER_ISSUE_184_ACTIVATION   ai_operation_message_contract.md 8節が正本となり、
                             本節の提示項目はそこへ吸収される
```

発効後は、本節を提示形式の**並列の規範として扱わない**(`DUPLICATE_SSOT` を
避けるため)。本節が定める `REVIEW_VERDICT` / `MERGE_BLOCKING_CONCERN` 等の
**判断の中身**は発効後も本節が正本であり、変わるのは提示のしかただけである。

```
MERGE_APPROVAL_IS_BOUND_TO_EXACT_REVIEWED_HEAD = YES
```

merge の承認は、レビューした exact PR head SHA に紐づく。head が変われば
以前の承認は失効する(2節の「承認はその操作・その対象に限る」と同じ原則)。

---

## 3.8 機能領域 WIP のレビュー観点

### 目的

領域ベースの WIP モデル(development_workflow.md 2.6節)では、作業者が
**自分で** 触る領域と `LOCK_LEVEL` を判定する。この判定の誤りは CI では
検出できない。「本来は買い判定の領域も lock すべきだったのに、保有判断の
領域だけで進めた」という誤りは、レビューでしか気づけない。

### ルール

`LOCK_REVIEW_OWNER = MANAGER`

作業 AI の実装レビュー(PR review / Phase 完了レビュー)では、既存の観点に
加えて次を確認する。

```
PRIMARY_DOMAIN         宣言された主領域が、変更内容と合っているか
LOCKED_DOMAINS         実際の変更が影響する領域をすべて含んでいるか
SHARED_TOUCHED         触った共通部品が漏れなく挙がっているか
LOCK_LEVEL             変更の種類に対して弱すぎないか
LEVEL_1 の証拠          LOCK_LEVEL = 1 を主張している場合、
                       5 つの compatibility evidence が実測で示されているか
SCOPE_EXPANSION        宣言時の PLANNED_FILES から実際の変更が広がっていないか。
                       広がっている場合、掲示し直されているか
```

```
判定できない場合は 3節の INSUFFICIENT_EVIDENCE とする。
「追加だけの diff に見えるから LOCK_LEVEL_1 でよい」と推測で通さない。
とくに enum 値の追加は、追加しかしていなくても網羅的な分岐・対応表・
入力バリデーション・直列化・永続データの読み手を壊し得る。
```

#### lock の漏れが確認された場合

```
LOCK_OMISSION_REVIEW_PASS_ALLOWED = NO
```

`LOCKED_DOMAINS` の漏れ、`SHARED_TOUCHED` の漏れ、`LOCK_LEVEL` の過小判定が
**実体との突合で確認された**場合、そのレビューは合格にしない。lock の漏れは
領域ベース WIP の安全性そのものを破る。他の作業者が「その領域は空いている」と
判断して並行着手できてしまうためである。

判定は既存の4種(3節)から選ぶ。**判定語を独自に増やさない。**

```
material な lock omission が確認できた
    -> REJECT
       修正が必要な状態であり、条件付きで前進してよい状態ではない

漏れているかどうかを判断する証拠が足りない
    -> INSUFFICIENT_EVIDENCE
       不合格ではない。参照元の実測を要求する

lock 自体は妥当だが、宣言の説明が不足している
    -> INSUFFICIENT_EVIDENCE
       証拠(実測した参照元と件数)の追加を要求する
```

```
禁止  確認された lock omission を PASS_WITH_CONDITIONS で通すこと。
      PASS_WITH_CONDITIONS は方針が妥当な場合の判定であり、
      lock 漏れは方針ではなく安全性の欠落である。
```

`REJECT` とする場合も、作業者が実測をやり直せるよう次を具体的に示す。

```
1  漏れている領域(D<n>)
2  その根拠となる参照元(どの path が、どの機能を経由して、どの領域に属するか)
3  関係する共通部品(S-<nn>)と、その consumer
4  必要な追加 lock(LOCKED_DOMAINS へ加えるべき領域と LOCK_LEVEL)
```

```
「具体的に理由を示す」ことは「REJECT しない」ことではない。
理由の提示は判定を弱める根拠にならない。
```

指摘を受けた作業者は次の順で対応する。

```
STOP(実装を止める)
-> DOMAIN_WIP_DECLARATION を再評価する
-> 必要な領域の code WIP を取得する(取得できなければ着手しない)
-> scope を再宣言する
-> 必要なら最新 main を取り込む
-> re-review を受ける
```

```
取得すべき領域の code WIP を他者が保持していた場合、
その実装は「直すべき指摘があるまま進める」のではなく、
development_workflow.md 2.6.7 に従って設計変更・待機・Issue 分割の
いずれかを選ぶ。
```

### DoD 申告の確認

`DOD_DECLARATION_REVIEW_OWNER = MANAGER`

作業 AI の実装レビューでは、領域・lock の観点に加えて次を確認する。
判定基準の本文は development_workflow.md 3節が正本であり、本節へ複製しない。

```
DoD 5 項目の申告があるか(空欄・無言の省略が無いか)
申告と diff が矛盾していないか
```

```
★ レビュワーが「正しいか」を判定するのではなく、
  **「申告されているか」「矛盾していないか」**を見る。
  正しさの一次責任は実装者にある。

  例  閾値の定数に diff があるのに「1 境界の連続性 = 該当なし」-> FAIL
      「該当あり・未解消」と書かれているが引き継ぎ先の Issue が無い -> FAIL
```

```
本節は Human Gate を増やすものではない。DoD の申告は CI で強制せず
(development_workflow.md 3節)、未記入は 3節の判定 4 種のうち
INSUFFICIENT_EVIDENCE として扱い、記入を求める。
```

### 適用範囲

```
対象     作業 AI の実装レビューにおける領域・lock の妥当性確認
対象外   領域・機能・共通部品の一覧そのもの
         -> docs/functional_domains.md が正本
対象外   WIP の取得・解放・割り込み・main 追随のルール本文
         -> development_workflow.md 2.6節が正本
```

```
本節は development_workflow.md 2.6節が発効している期間
(`CURRENT_WIP_RULE = DOMAIN_WIP_RULE_V1`)に適用する。発効状態の正本は
同 2.6.10 の `ACTIVATION_STATE_SSOT` に従う。同節の発効を
もって適用を開始する。それまでは確認義務を課さない。
```

### 例外

`LOCKED_DOMAINS` の確認は Human Gate を増やすものではない。**2節の
Human Gate、2.6節の merge 実行者、Production approval、exact ChangeSet
approval はいずれも変更しない。**

---

## 3.9 レビューの入力境界(BLIND_FIRST_INPUT_BOUNDARY)

### 目的

fresh session であることは、独立したレビューであることを意味しない。

```
fresh session != blind-first review
```

**独立性はレビュー対象ごとに決まる。**

```
TARGET_REVIEW_FRESHNESS    そのレビュー対象について、Phase 1 の snapshot を固定する
                           前に BLIND_FIRST_PHASE_1_FORBIDDEN の入力を取得していないこと
SESSION_CREATION_FRESHNESS 原則 不要。session を新しく作ったことは
                           独立性の根拠にならない

★ 新しい session でも、その対象の開発者報告を先に読んでいれば成立しない。
★ 続いている session でも、その対象について読んでいなければ成立する。
★ 判定の範囲は ★ 本節の BLIND_FIRST_PHASE_1_FORBIDDEN に分類される情報に限る。
  ★ 他の対象を通じて Issue 名や進行状況を偶発的に目にしただけでは失われない。
★ 確認の手順と PRIOR_EXPOSURE の申告は 3.14節の TARGET_FRESHNESS_CHECK が正本である。
```

REVIEWER が最初に Issue の全コメント・PR 本文・PR Conversation を一括取得すると、
その時点で開発者の結論・リスク評価・テストの説明を読んでしまう。以後の判断は
それに引きずられる。**何を先に読むかを規則にする。**

### Phase の定義

```
PHASE_1  primary evidence の取得 / independent review / snapshot の固定
PHASE_2  developer report の取得 / developer claim との比較
PHASE_3  final review verdict
```

### Phase 1 で取得してよいもの

```
BLIND_FIRST_PHASE_1_ALLOWED

  Issue body
  USER requirements
  USER decisions
  current classification(label / Progress Status / Priority)
  current SSOT(該当条文の原文)
  origin/main SHA
  base SHA / head SHA / merge-base
  exact diff
  design artifact(設計そのもの。設計者の自己評価を除く)
  related source code
  related tests
  CI result(job ごとの conclusion)
  workflow definition / ruleset
  machine-readable repository state(branch / tag / label / file の実体)
```

### Phase 1 で読まないもの

```
BLIND_FIRST_PHASE_1_FORBIDDEN

  developer completion report
  developer self-review
  developer risk assessment
  developer root-cause explanation
  developer test interpretation
  developer の「PASS / 問題なし」という結論
  developer implementation summary
  PR 本文のうち developer が記載した説明・自己評価部分
```

### Issue コメントの扱い

Issue コメントには利用者の要求・決定、開発者の報告、管理者の指示、レビュー結果が
混在する。**「Issue の全コメントを最初に読む」としてはならない。**

```
★ 全セッションが同一の GitHub identity で投稿する。
  -> comment の author からは利用者の要求と開発者の報告を区別できない
  -> 本文の ACTOR / DECIDED_BY 等のマーカーは自己申告であり、機械的な境界にならない
  -> 本人性の識別は Issue #332 の論点であり、本節では解決しない
```

したがって自動分類は成立しない。**Phase 1 の入力は manifest で与える。**

**分ける根拠は役割の分離であって、セッションの分離ではない。**
MANAGER と REVIEWER は 1節で別の役割であり(`MANAGER_AND_REVIEWER = SEPARATE_ROLES`)、
同一セッションが両方を兼ねることはできない(`SAME_SESSION_DUAL_ROLE = FORBIDDEN`)。
そのうえで本節の入力境界(blind-first)を課す。

```
PHASE_1_INPUT_MANIFEST
  Phase 1 で読んでよい comment / artifact の URL を列挙したもの

PHASE_1_INPUT_MANIFEST_CREATOR  MANAGER(3.14 の REVIEW_SESSION_CREATOR)
PHASE_1_REVIEWER                REVIEWER

制約    MANAGER != REVIEWER
        manifest に developer completion report を ★ 含めてはならない
        manifest は snapshot へ記録し、事後に検査できる形にする
```

```
★ この方式は依存を消さない。範囲を MANAGER が決める構造は残る。
  manifest を snapshot へ記録することで、「developer report が含まれていなかったか」を
  第三者が後から検査できるようにする。★ 依存を消すのではなく、監査可能にする。
```

### PR 本文の扱い

**PR 本文の全体を Phase 1 の必須入力にしない。** コードレビューの Phase 1 で必要な
ものは、すべて GitHub artifact から直接取得できる。

```
base / head SHA   gh pr view --json baseRefOid,headRefOid
changed files     gh pr diff --name-only / git diff --name-only
exact diff        git diff <merge-base>..<head>
CI                gh pr view --json statusCheckRollup
Issue reference   Issue 側から辿る
```

**PR 本文は Phase 2 の developer claim comparison で読む。** ただし 1 点の例外がある。

```
★ PR 本文の「正本が必須と定める節が存在するか」の検査は Phase 1 で行う
  理由 = 節の存在は構文であり、developer の主張ではない
★ 節の中身(宣言の内容が正しいか)は Phase 1 で読まない。Phase 2 で読む
```

---

### 適用の深さ(3.5節の適用範囲をそのまま使う)

**新しい深さの軸を作らない。** 3.5節が既に
**誤ると取り返しがつかない判断**を主対象として定めている。その境界を使う。

```
ALL_REVIEWS              本節の入力境界(blind-first)
                         primary evidence の独立取得
                         developer report を読む前の snapshot 固定(3.14節)
                         証拠の強度の分類(3.12節)

3.5節の主対象に該当する   3.10節 / 3.11節を full で行う
                         requirement traceability
                         failure mode / counterexample / missing path の確認
                         反証確認(3.13節)

該当しない               独立取得は行う(省略しない)
                         traceability は省略してよい
                         反証確認は省略してよい
```

```
3.5節の主対象(原文)
  Human Gate / merge readiness / release readiness /
  Production verification / release-blocker removal / Issue close eligibility

3.5節は「すべての報告を毎回検証し直すルールではない」と定めている。
本節以降もその適用範囲を広げない。
```

**この境界の定義は本節にだけ置く。** 3.10節 / 3.11節 / 3.13節 / 3.14節は
本節を参照する(同じ境界を複数の節で定義し直さない)。

---

## 3.10 設計レビューの観点(DESIGN_REVIEW_PROTOCOL)

```
DESIGN_REVIEW_ACTOR = REVIEWER(1節)
```

### 17 の観点

**該当しない観点は `NOT_APPLICABLE` と理由を書く。** 空欄にしない。

```
 1 REQUIREMENT_COVERAGE        10 TESTABILITY
 2 SSOT_CONSISTENCY            11 ROLLBACK
 3 ROOT_CAUSE_FIT              12 ACTIVATION_BOUNDARY
 4 RESPONSIBILITY_BOUNDARY     13 DATA / STATE MIGRATION
 5 ARCHITECTURE_FIT            14 CONCURRENCY / RETRY / IDEMPOTENCY
 6 FAILURE_MODE                15 UNKNOWN / ASSUMPTION
 7 BACKWARD_COMPATIBILITY      16 ISSUE_SPLIT / LIFECYCLE
 8 SECURITY                    17 HUMAN_GATE
 9 OPERABILITY
```

### traceability

**REVIEWER 自身が作る。開発者の対応表を写さない。**

```
REQUIREMENT -> DESIGN_ELEMENT -> EVIDENCE -> GAP -> VERDICT
```

### 手順

```
DESIGN_REVIEW_PHASE_1
  1  Issue body / 利用者の要求 / 利用者の決定を取得する
  2  current SSOT を ★ 原文で取得する(設計者の引用を根拠にしない)
  3  design artifact を取得する
  4  ★ developer explanation を見ずに requirements を再構築する
  5  17 の観点を評価する
  6  failure mode / counterexample を探す
  7  requirement traceability を作成する
  8  ★ INDEPENDENT_REVIEW_SNAPSHOT を固定する(3.14)

DESIGN_REVIEW_PHASE_2
  9  developer rationale / self-review を読む
  10 independent finding との差を比較する
  11 必要なら finding を更新する
  12 ★ 更新理由を記録する
  13 final verdict
```

---

### FULL_REVIEW_APPLICABILITY

```
3.5節の主対象に該当する   6(failure mode / counterexample)と
                         7(requirement traceability)を必須とする

該当しない               1〜5 / 8 と DESIGN_REVIEW_PHASE_2 は行う。
                         6 / 7 は省略してよい
```

**新しい深さの label を作らない。** 3.9節「適用の深さ」と同じ境界である。

---

## 3.11 コードレビューの観点(CODE_REVIEW_PROTOCOL)

```
CODE_REVIEW_ACTOR = REVIEWER(1節)
```

### 既存規則を再利用する

```
merge-base からの exact diff   development_workflow.md 3節「レビュー対象の指定」
実装パイプラインの diff review  同 3節
機能領域 WIP の観点             本文書 3.8節
```

**3.8節の観点は REVIEWER が独立に確認する。ただし所有者は移らない。**

```
REVIEWER    3.8節の観点(LOCKED_DOMAINS / LOCK_LEVEL / compatibility evidence /
            DoD 申告)を ★ 独立に確認する
MANAGER     3.8節の management responsibility / ownership を ★ 引き続き持つ
            LOCK_REVIEW_OWNER = MANAGER
            DOD_DECLARATION_REVIEW_OWNER = MANAGER

★ REVIEWER が確認しても ownership は REVIEWER へ ★ 移らない。
★ 3.8節の本文は変更していない。
```

**新設しない。** 本節が加えるのは次の 4 点だけである。

```
取得する artifact の列挙
requirement traceability
surrounding code の確認
PR 本文の必須節の確認
```

### 手順

```
CODE_REVIEW_PHASE_1
  1  Issue requirement / 利用者の決定を取得する
  2  base / head / merge-base を取得する
  3  exact diff を取得する
  4  changed files を取得する
  5  ★ surrounding code を取得する
  6  caller / callee / interface を確認する
  7  tests を取得する
  8  CI 結果を取得する
  9  requirement -> implementation -> test を追跡する
  10 counterexample / missing path を探す
  11 ★ PR 本文の必須節が存在するかを確認する(★ 節の中身は読まない)
  12 ★ INDEPENDENT_REVIEW_SNAPSHOT を固定する(3.14)

CODE_REVIEW_PHASE_2
  13 PR 本文 / developer completion report を読む
  14 developer claim との差を比較する
  15 ★ developer assertion only の項目を LEVEL_C として分類する(3.12)
  16 final finding / verdict
```

```
★ 5 と 11 を独立させている理由

  diff だけを読むと、diff の外にある呼び出し元・既存の契約・
  必須節の欠落を見落とす。実際に、変更されたファイルを列挙しながら
  その中身を読まないまま PASS を出した事例がある。
  ★ 5 は「列挙したファイルを読む」ことを、★ 11 は「PR 本文の構文を見る」ことを、
  それぞれ独立した手順として置く。
```

### developer report の位置づけ

```
DEVELOPER_REPORT = SECONDARY_EVIDENCE
```

**価値が無いという意味ではない。** Phase 2 で次の用途に使う。

```
developer intent       なぜその設計にしたか
known limitation       本人が把握している限界
local-only evidence    REVIEWER が取得できない実行結果
test command / output  再現の手がかり(3.12 の LEVEL_B の材料)
design rationale       設計の根拠
unverified measurement 観測値など(LEVEL_B または LEVEL_C)
```

---

### FULL_REVIEW_APPLICABILITY

```
3.5節の主対象に該当する   9(requirement -> implementation -> test の追跡)と
                         10(counterexample / missing path)を必須とする

該当しない               1〜8 / 11 / 12 と CODE_REVIEW_PHASE_2 は行う。
                         9 / 10 は省略してよい
```

**新しい深さの label を作らない。** 3.9節「適用の深さ」と同じ境界である。
5(surrounding code)と 11(PR 本文の必須節)は省略の対象ではない。

---

## 3.12 証拠の強度(EVIDENCE_CLASSIFICATION)

### 3.6節とは軸が違う。置き換えない

```
3.6節  鮮度 × 検証可能性   どの記述を採用するか
本節   誰が取得したか       その証拠の強度
```

**併存させる。** 一方が他方を上書きしない。

### 3 段階

```
LEVEL_A  INDEPENDENTLY_VERIFIED      REVIEWER 自身が取得・再現した
LEVEL_B  REPRODUCIBLE_EVIDENCE       手順が示され、第三者が再現できる
LEVEL_C  DEVELOPER_ASSERTION_ONLY    開発者の申告のみ
```

### 測定の独立性(MEASUREMENT_INDEPENDENCE)

```
(a) 測定コマンドを添える(どこで測ったかを含む)
(b) sanitize 済み生出力   CURRENTLY_NOT_ACTIVE
    本節の発効に含めない。Issue #334 で repository の公開方針と
    公開可能な範囲が決まった後に、提出の範囲を別途判断する
    関連 Issue = #334
(c) 高リスク項目は独立に再測定する(★ 管理者自身の測定も対象)
(d) 開発者の申告のみで支えられる測定は ★ LEVEL_C とする。別の語を作らない
```

```
(a) / (c) / (d) は本節の発効とともに有効である。
(b) だけは有効な必須事項に含めない。
本節が main へ入ることは、sanitize 済み生出力の提出義務の発効を意味しない。

理由 = 何を sanitize すれば公開してよいかは repository の公開方針に依存し、
       それは #334 の判断対象である。先に義務だけを発効させると、
       公開可能な範囲が未定のまま提出を求めることになる。
```

### PASS の条件

**判定語は 3節の 4 語のままである。本節は新しい判定語を作らない。**

```
★ 重要な要求が LEVEL_C だけで支えられている場合、PASS を出さない
  -> INSUFFICIENT_EVIDENCE とする(3節の語彙の適用であり、新設ではない)
```

```
★ LEVEL_A / B / C は ★ 証拠の強度であって ★ 判定語ではない。混同しない。
```

---

## 3.13 反証確認(DISCONFIRMING_REVIEW)

**「壊れていないか」を探す工程を、明示的に置く。**

```
DISCONFIRMING_REVIEW_ACTOR = REVIEWER(1節)
```

### 適用

```
3.5節の主対象に該当する   DISCONFIRMING_REVIEW = REQUIRED
該当しない               DISCONFIRMING_REVIEW = OPTIONAL
```

**新しい判定語も深さの label も作らない。** 3.9節「適用の深さ」と同じ境界である。

```
DISCONFIRMING_CHECKS_PERFORMED
  何を「壊れている可能性」として調べたかを列挙する
```

```
記載例
  別の caller が存在しないか確認
  error path を確認
  stale state のときの挙動を確認
  requirement が未実装の経路を確認
  CI が green でも漏れる経路がないか確認
```

```
★ Finding の件数にノルマを作らない。0 件でよい。
★ REQUIRED の場合、「反証を試みたこと」の記録は省略できない(3.14節)。
  Finding は「無かった」が成立する。反証は「しなかった」であって「無かった」ではない。
★ OPTIONAL の場合も、行ったのであれば記録する。行わなかったのであれば
  3.14節の NOT_APPLICABLE として省略した事実を残す(黙って空欄にしない)。
```

---

## 3.14 独立レビューの証跡(INDEPENDENT_REVIEW_SNAPSHOT)

### 目的

最終報告で REVIEWER 自身が「先に独立レビューしました」と書くだけでは証拠にならない。
**判定だけを先に置いても、その判定を支える根拠は後から作れる。**

```
★ 本 snapshot は development_workflow.md 6.5.3 の ISSUE_STATE_SNAPSHOT とは別物である。
  あちら = Issue の現在状態 / こちら = レビューの証跡
  ★ 6.5.3 の約 30 の固定キーを要求しない。
```

### 必須 field(全レビュー共通)

```
REVIEW_ID                       <YYYYMMDDTHHMMSSffffffZ>-REVIEWER-<NONCE>
                                ★ 6.5.3 の STATE_ID と同じ生成方式。新方式を作らない
REVIEW_KIND                     INDEPENDENT_REVIEW(3節。★ 追加。既存 field は変えない)
REVIEW_TARGET                   DESIGN | CODE(★ レビューの種別)
REVIEW_TARGET_REF               ★ 必須。レビュー対象の一意な参照
                                CODE  = Issue 番号 + BASE / HEAD
                                DESIGN = 対象 design artifact の URL
                                (★ REVIEW_TARGET は種別であり、対象の同一性は本 field が持つ)
ISSUE_REF                       #NNN
SSOT_REF                        読んだ条文(file + anchor)の列挙
CREATED_AT                      実測 UTC
PHASE_1_INPUT_MANIFEST          Phase 1 で読んだ comment / artifact の URL の列挙
PRIOR_EXPOSURE                  ★ 必須。NONE か、目にした他者の結論の申告
                                (下記 REVIEW_SESSION_REUSE_POLICY が定める形式)
TARGET_FRESHNESS_CHECK          ★ 必須。PASS | UNKNOWN
                                判定主体 = REVIEWER。UNKNOWN の経路は下記
                                REVIEW_SESSION_REUSE_POLICY が定める
CONTAMINATION_CHECK             ★ 必須。PASS | FAIL
                                当該レビュー対象について
                                BLIND_FIRST_PHASE_1_FORBIDDEN(3.9節)の情報を
                                snapshot 固定より前に取得していないこと
PRELIMINARY_FINDINGS            ★ 必須
EVIDENCE_GAPS                   ★ 必須
DISCONFIRMING_CHECKS_PERFORMED  ★ field は必須(欠落にしない)
                                値は 3.13節の適用による(下記「NONE の扱い」)
PRELIMINARY_VERDICT             3節の 4 語のいずれか(★ 新語を作らない)
```

### 対象ごとに追加で必須

```
CODE    BASE_SHA / HEAD_SHA / MERGE_BASE / DIFF_HASH / REVIEWED_FILES / CI_RUN_ID
        DIFF_HASH = git diff <merge-base>..<head> の sha256
DESIGN  DESIGN_ARTIFACT_REF(設計が書かれた comment の URL)
```

### 任意

```
REQUIREMENTS_RECONSTRUCTED / CONSTRAINTS_RECONSTRUCTED
```

### NONE の扱い

```
PRELIMINARY_FINDINGS = NONE
  ★ 有効値である。Finding を無理に作らせない。

EVIDENCE_GAPS = NONE
  ★ 有効値である。ただし ★ 未確認事項が存在するのに NONE と書いてはならない。
  区別する
    NONE       確認した結果、未確認事項が存在しない
    項目の列挙  未確認事項がある
  ★ 「確認していないので分からない」を NONE と書かない。
  ★ これは新しい禁止ではない。3節の「推測で PASS にしてはならない」と
    issue_label_policy.md 7.4.2 の「未観測を PASS と書かない」が既に禁じている。

DISCONFIRMING_CHECKS_PERFORMED
  ★ field 自体は全レビューで残す。欠落にしない。
  3.5節の主対象に該当する(3.13節 = REQUIRED)
    ★ 実際に行った確認内容を書く。★ NONE も NOT_APPLICABLE も認めない。
  該当しない(3.13節 = OPTIONAL)
    ★ 次の形を認める。
      DISCONFIRMING_CHECKS_PERFORMED =
        NOT_APPLICABLE: 3.5節の主対象外のため省略
    ★ 実施対象外であることを明示する。空欄にはしない。
  理由 = Finding が無いことと、反証を試みなかったことは別である。
         NOT_APPLICABLE は「対象外」であり「0 件だった」ではない。
```

### 保存先

```
SNAPSHOT_DESTINATION = 対象 Issue の comment
```

```
★ repository の file にしない。REVIEWER 自身が commit することになり、
  「REVIEWER は修正者にならない」という原則に反する。
★ 外部(gist 等)にしない。公開面が増える(11節)。
★ コードレビューでは PR comment でもよいが、設計レビューには PR が
  存在しない場合があるため、★ 形式を揃えて Issue comment を第一候補とする。
```

### Phase 1 の完了条件

```
PHASE_1_COMPLETE =
      必須 field がすべて埋まっている(REVIEW_TARGET に応じた追加分を含む)
  AND TARGET_FRESHNESS_CHECK と CONTAMINATION_CHECK が埋まっている
      (★ 判定だけを後から作れないようにするため、明示して条件に含める)
  AND snapshot が Issue comment として投稿されている
  AND その comment の URL が確定している
```

```
★ 埋まっていない field を残して Phase 2 へ進まない。
★ 空欄にしない。
  確認できなかったのであれば EVIDENCE_GAPS へ「何を確認できなかったか」を書く。
  反証を試みられなかったのであれば、その理由を
  DISCONFIRMING_CHECKS_PERFORMED へ書く(試みなかったことを隠さない)。
```

### REVIEW_SESSION_LIFECYCLE

```
 1  DEVELOPER         artifact を作成・固定する(branch push / design comment)
 2  REVIEW_TRIGGER    developer の完了報告
 3  MANAGER           review target を確定し、PHASE_1_INPUT_MANIFEST を作成する
 4  MANAGER           REVIEWER へ review を依頼する
 5  REVIEWER P1       BLIND_FIRST_PHASE_1_ALLOWED のみ取得する
 6  REVIEWER          INDEPENDENT_REVIEW_SNAPSHOT を Issue comment として固定する
 7  MANAGER           snapshot の URL を確認する
 8  MANAGER           developer report を REVIEWER へ handoff する
 9  REVIEWER P2       developer claim と独立の結論を比較する
10  REVIEWER          final finding / verdict を出す
11  MANAGER           finding を受領し、必要なら開発者へ修正を指示する
12  USER              必要な Human decision / Human Gate

★ REVIEWER は finding を自分で修正しない
```

```
REVIEW_SESSION_CREATOR   MANAGER(1節)
REVIEWER_ROLE            REVIEWER(1節)
                         ★ MANAGER != REVIEWER。同一セッションが兼ねない
                           (SAME_SESSION_DUAL_ROLE = FORBIDDEN)
TARGET_REVIEW_FRESHNESS  REQUIRED(レビュー対象ごと)
                         ★ 役割が別であっても、対象ごとの blind-first は必要である(3.9節)
                         ★ 要求するのは ★ session の新規作成ではなく、
                           下記 TARGET_FRESHNESS_CHECK の通過である
REVIEW_TRIGGER           developer の完了報告を受けた時点
                         ★ developer が push した時点ではない
                           (artifact が固定されていない可能性がある)
PHASE_1_INPUT_PROVIDER   MANAGER が manifest を渡す
                         ★ 渡すのは URL の列挙だけである。本文を要約して渡さない
DEVELOPER_REPORT_HANDOFF_POINT
                         snapshot が comment として投稿され、その URL を
                         MANAGER が確認した後
                         ★ それより前に渡してはならない
REVIEW_SESSION_END_CONDITION
                         次のいずれか(下記 E)
                           FINAL_VERDICT = PASS
                           REVIEW_TARGET_ABANDONED
                           REVIEW_TARGET_REPLACED
                           USER による review 終了判断
                         ★ REVIEWER は finding を直さない
```

### review session の再利用(REVIEW_SESSION_REUSE_POLICY)

**レビュー対象ごとに session を作り直さない。**
独立性が要るのは「その対象についての最初の評価を、開発者の自己評価より前に
形成すること」であり、session を新しくすること自体ではない。
最初の snapshot を固定した後に同じ reviewer が developer report や修正内容を読むことは、
Phase 2 と remediation review の ★ 本来の仕事である。

```
PERSISTENT_REVIEWER_SESSION = YES

INITIAL_INDEPENDENCE      レビュー対象ごとの blind-first Phase 1
CONTINUITY_AFTER_SNAPSHOT same reviewer session

★ blind-first を弱める規則ではない。要求するものを
  「session の新規作成」から「対象ごとの入力境界」へ置き換えるだけである。
```

```
SESSION_REUSE_ALLOWED_FOR   DIFFERENT_ISSUE / DIFFERENT_INDEPENDENT_REVIEW_TARGET /
                            UNRELATED_PR / NEW_REVIEW_LIFECYCLE /
                            PHASE_2 / FINDING_REMEDIATION_REVIEW / RE_REVIEW /
                            NEW_COMMIT_ON_SAME_REVIEW_TARGET / CI_RECHECK

★ 別の Issue / 別の PR / 新しい review lifecycle であることだけを理由に
  新しい session を要求してはならない。
```

**新しいレビュー対象の初回 Phase 1 の前に、対象ごとの確認を通す。**

```
TARGET_FRESHNESS_CHECK
  1  REVIEW_TARGET_REF を固定する
     (REVIEW_TARGET = 種別 / REVIEW_TARGET_REF = 対象の一意な参照)
  2  REVIEW_ID を新規に発行する
  3  MANAGER が PHASE_1_INPUT_MANIFEST を作成する
  4  REVIEWER が、その対象について developer report / developer self-assessment /
     MANAGER の review 結論 / その他の disallowed input(3.9節)を
     既に取得していないかを確認する
  5  未取得であれば ★ 同じ session で Phase 1 を開始してよい
  6  取得済みであれば、その対象について blind-first は成立しない
     (下記 NEW_REVIEW_SESSION_REQUIRED_IF の A)
```

```
NEW_REVIEW_SESSION_REQUIRED_IF
  A  その対象の developer report 等を Phase 1 より前に読んでしまった
  B  その対象へ MANAGER / DEVELOPER として関与した履歴がある
  C  session の文脈が壊れ、manifest の境界を一意に保てない
  D  USER が明示的に新しい session を要求した
  E  その他、current protocol が定める対象固有の contamination

★ DIFFERENT_ISSUE / DIFFERENT_PR / NEW_REVIEW_LIFECYCLE だけを理由に
  新しい session を要求してはならない(上記のいずれかに当たる場合にだけ要求する)。
```

**レビュー対象ごとに、開始時の確認を記録する。**

```
REVIEW_TARGET
REVIEW_TARGET_REF
REVIEW_ID
TARGET_FRESHNESS_CHECK
PHASE_1_INPUT_MANIFEST
CONTAMINATION_CHECK = PASS
```

```
記録先 = 上記の INDEPENDENT_REVIEW_SNAPSHOT の必須 field

★ TARGET_FRESHNESS_CHECK と CONTAMINATION_CHECK は ★ 必須 field である
  (PHASE_1_COMPLETE の条件に明示して含める)。
★ 記録先を持たない判定は ★ 事後に検査できない。3.9節が manifest について
  求めていることと同じ扱いにする。
```

```
PERSISTENT_REVIEWER_SESSION_POLICY_ACTIVE = YES | NO
SESSION_POLICY_ACTIVATION_STATE_SSOT = Issue #355 の最新の durable な activation 記録
ACTIVE_SESSION_POLICY_SSOT = PERSISTENT_REVIEWER_SESSION_POLICY_ACTIVE = YES のとき
                               current main の本書 1節 / 3.9節 / 3.14節の session 規則
                             PERSISTENT_REVIEWER_SESSION_POLICY_ACTIVE = NO のとき
                               PRE_ACTIVATION_SESSION_POLICY_SSOT
PRE_ACTIVATION_SESSION_POLICY_SSOT
                           = Issue #355 の durable な pre-activation 記録が固定した
                             immutable な base commit の
                             docs/user_manager_collaboration_protocol.md
                             (1節 / 3.9節 / 3.14節の session 規則)

★ 識別子は #353 の ACTIVATION_STATE_SSOT と分ける。同じ名前が同一文書で
  2 つの Issue へ束縛されると、限定なしの参照がどちらを指すか一意に読めない。

★ 発効状態の固定値を本書へ埋め込まない。現在値は上記 SSoT を fresh に読む
  (3.14節の ROLE_SEPARATION_ACTIVE / 2.6節の CURRENT_WIP_RULE と同じ方式)。
★ 本改訂自身のレビューは ★ 改訂前の規則に従う
  (CURRENT_POLICY_APPLIES_TO_ITS_OWN_CHANGE = YES)。
```

**発効前は、本節の persistent 方式が現在有効な session 規則ではない。**
本改訂が main へ入ると、旧い session 規則の本文は main から消える。
しかし `PERSISTENT_REVIEWER_SESSION_POLICY_ACTIVE = NO` の間に有効なのは
**旧い session 規則のほう**である。そこで、その期間にどこを読めばよいかを
`ACTIVE_SESSION_POLICY_SSOT` で一意に決める。

```
読む順序(PERSISTENT_REVIEWER_SESSION_POLICY_ACTIVE = NO の場合)
  1  Issue #355 の最新の durable な activation 記録で発効状態を確認する
  2  同 Issue の durable な pre-activation 記録から immutable な base commit を得る
  3  その commit の docs/user_manager_collaboration_protocol.md の
     1節 / 3.9節 / 3.14節の session 規則を読む
```

**旧い session 規則の本文を本書へ複製しない。**
複製すると同じ規則が 2 か所に存在し、どちらが正本か分からなくなる。
本書が持つのは「**どこを読めば取得できるか**」だけである。

**base commit の SHA を本書へ書かない。**
変わりうる値・環境に属する値は Issue の durable record 側で固定し、
本書は pointer だけを持つ(`ACTIVE_ROLE_MODEL_SSOT` / `ACTIVATION_STATE_SSOT` と
同じ扱い。静的な文書を、変わりうる状態の唯一の根拠にしない)。

**上記 4 の「取得していないか」の範囲**

```
TARGET_FRESHNESS_SCOPE = BLIND_FIRST_PHASE_1_FORBIDDEN_ONLY

当該レビュー対象について、3.9節の BLIND_FIRST_PHASE_1_FORBIDDEN に分類される
情報を Phase 1 の snapshot 固定より前に取得したかどうかで判定する。

★ 他の対象のレビューを通じて、その対象の Issue 名や進行状況を偶発的に
  目にしただけでは contamination として扱わない。
★ 目的は完全な情報遮断ではなく、開発者の自己評価等による anchoring より前に
  独立した Phase 1 を固定することである。
```

**Phase 1 の開始時に、目にした他者の結論を申告する。**

```
PRIOR_EXPOSURE  ★ 必須(3.14節の snapshot へ記録する)

  PRIOR_EXPOSURE = NONE
  または
  PRIOR_EXPOSURE =
    source:                        どこで目にしたか
    summary:                       何を目にしたか
    exposure_type:                 種類
    BLIND_FIRST_FORBIDDEN_MATCH:   3.9節の列挙に当たるか
    independence_impact:           独立性への影響

★ persistent な session では ★ 完全な無知状態を前提にしない。
  隠さずに申告し、事後に検査できるようにする。
```

```
判定
  一般 metadata / workflow 情報 / 利用者の優先順位
    -> ★ 原則 freshness を失わない
  developer completion report / developer self-assessment /
  developer root-cause explanation / PASS・READY 等の developer の結論 /
  それを実質的に転記した MANAGER の結論
    -> ★ blind-first forbidden の候補

★ 曖昧な場合は ★ TARGET_FRESHNESS = UNKNOWN とし、★ MANAGER へ戻す。
  ★ 自分で「たぶん大丈夫」と判断して Phase 1 を進めない。
```

**`TARGET_FRESHNESS = UNKNOWN` を MANAGER へ戻した後の経路**

```
(a) その曝露が BLIND_FIRST_PHASE_1_FORBIDDEN(3.9節)に当たる
    -> 上記 NEW_REVIEW_SESSION_REQUIRED_IF の A に該当する。新しい session を開始する
(b) 当たらない
    -> PRIOR_EXPOSURE へ記録し、★ 同じ session で Phase 1 を開始してよい
(c) MANAGER でも一意に判定できない
    -> USER の判断を仰ぐ(8節 POLICY_AUTHORITY = HUMAN_ONLY)

★ 新しい判定基準を作らない。上記の A〜E と PRIOR_EXPOSURE に接続するだけである。
★ MANAGER が単独で freshness の成立を宣言しない
  (1節 MANAGER_REVIEW_CAN_SUBSTITUTE_INDEPENDENT_REVIEW = NO)。
```

---

```
A INITIAL REVIEW  上記 12 段階の 1〜6
                  MANAGER が PHASE_1_INPUT_MANIFEST を作り、REVIEWER が
                  TARGET_FRESHNESS_CHECK を通したうえで Phase 1 を行い
                  INDEPENDENT_REVIEW_SNAPSHOT を固定する。
                  ★ ここで REVIEW_ID を発行する。★ session は続いていてよい
B PHASE 2         上記 12 段階の 7〜10
                  ★ 同じ review session で developer claim と比較し verdict を出す
C REMEDIATION     MANAGER が finding を開発者へ指示する(12 段階の 11) ->
                  開発者が修正し new HEAD と evidence を固定する ->
                  MANAGER が remediation handoff を行う ->
                  ★ 同じ review session が 前回レビュー済み HEAD からの exact diff を見て、
                  finding の解消 / regression / 新しい finding を確認し verdict を更新する
D REPEAT          必要な回数だけ C を繰り返す。★ fresh session を作り直さない
E TERMINATION     FINAL_VERDICT = PASS / REVIEW_TARGET_ABANDONED /
                  REVIEW_TARGET_REPLACED / USER による review 終了判断
```

```
REVIEW_TARGET_LIFECYCLE = 同一の REVIEW_ID における、
                          初回 Phase 1(A)から E TERMINATION までの範囲

★ 範囲を指す語の定義だけである。★ 旧規定の「同じ session を使える範囲は
  同一の REVIEW_ID / REVIEW_TARGET_LIFECYCLE に限る」という制限は復活させない。
★ session の継続可否は SESSION_REUSE_ALLOWED_FOR と
  NEW_REVIEW_SESSION_REQUIRED_IF が定める。
```

**同じ session でも「前回の記憶だけ」でレビューしない。**
再 review のとき MANAGER は少なくとも次を渡す。

```
REVIEW_ID
PREVIOUS_REVIEWED_HEAD
NEW_HEAD
DIFF_RANGE
RESOLVED_FINDINGS
DEVELOPER_RESPONSE
CI_RESULT
NEW_EVIDENCE
```

```
REVIEWER が確認すること
  前回の finding が ★ 本当に解消されたか
  修正による regression が無いか
  scope creep が無いか
  新しい finding が生じていないか
```

再 review の結果も append-only で記録する。

```
例  REVIEW_ID        = <初回に発行したもの>
    REVIEW_ITERATION = 2
    REVIEW_KIND      = INDEPENDENT_REVIEW
    PREVIOUS_HEAD    = <前回レビュー済み SHA>
    CURRENT_HEAD     = <今回の SHA>
    RESOLVED_FINDINGS = <解消した finding>
    NEW_FINDINGS      = <新しい finding>
    REVIEW_VERDICT    = <3節の 4 語のいずれか>

★ 旧 verdict を書き換えない。新しい iteration として足す。
```

### この role 分離の発効

```
ROLE_SEPARATION_ACTIVATION = Issue #353 の activation boundary(12 条件)
ROLE_SEPARATION_ACTIVE     = YES | NO
ACTIVATION_STATE_SSOT      = Issue #353 の最新の durable な activation 記録
ACTIVE_ROLE_MODEL_SSOT     = ROLE_SEPARATION_ACTIVE = YES のとき
                               current main の本書 1節 / 3.9〜3.14節
                             ROLE_SEPARATION_ACTIVE = NO のとき
                               PRE_ACTIVATION_ROLE_MODEL_SSOT
PRE_ACTIVATION_ROLE_MODEL_SSOT
                           = Issue #353 の durable な pre-activation 記録が固定した
                             immutable な base commit の
                             docs/user_manager_collaboration_protocol.md
                             (1節 / 3.9〜3.14節)
CURRENT_POLICY_APPLIES_TO_ITS_OWN_CHANGE = YES
```

**発効状態の固定値を本書へ埋め込まない。**
`ROLE_SEPARATION_ACTIVE` の現在値は上記 SSoT を fresh に読んで確認する
(2.6節の `CURRENT_WIP_RULE` / ai_operation_message_contract.md の
`NEW_CONTRACT_ACTIVE` と同じ方式であり、新方式を作らない)。

**発効前は、本書の 1節 / 3.9〜3.14節が現在有効な役割規則ではない。**
本改訂が main へ入ると、旧い役割規則の本文は main から消える。
しかし `ROLE_SEPARATION_ACTIVE = NO` の間に有効なのは**旧い役割規則のほう**である。
そこで、その期間にどこを読めばよいかを `ACTIVE_ROLE_MODEL_SSOT` で一意に決める。

```
読む順序(ROLE_SEPARATION_ACTIVE = NO の場合)
  1  Issue #353 の最新の durable な activation 記録で ROLE_SEPARATION_ACTIVE を確認する
  2  同 Issue の durable な pre-activation 記録から immutable な base commit を得る
  3  その commit の docs/user_manager_collaboration_protocol.md の 1節 / 3.9〜3.14節を読む
```

**旧い役割規則の本文を本書へ複製しない。**
複製すると同じ規則が 2 か所に存在し、どちらが正本か分からなくなる。
本書が持つのは「**どこを読めば取得できるか**」だけである。

**base commit の SHA を本書へ書かない。**
変わりうる値・環境に属する値は Issue の durable record 側で固定し、
本書は pointer だけを持つ(`ACTIVATION_STATE_SSOT` と同じ扱い。
静的な文書を、変わりうる状態の唯一の根拠にしない)。

MANAGER と REVIEWER を別の役割とする改訂は、**Issue #353 が定める 12 の
activation 条件を満たした時点で発効する**。この改訂自身のレビューは改訂前の
規則(管理者役割の fresh な別セッション)で行い、**発効前に「REVIEWER が
レビューした」と記録しない**。

### この規則が埋めないもの

```
★ MANAGER が manifest を誤って作れば blind-first は崩れる。
  manifest を snapshot へ記録することで ★ 崩れたことを事後に検査できる。
  ★ 崩れないことは保証しない。

★ REVIEWER が manifest 外の情報を取得しても検出できない。
  PHASE_1_INPUT_MANIFEST と REVIEWED_FILES は ★ 自己申告である。
  ★ DIFF_HASH と changed files の一致で「対象が正しいか」は検査できるが、
    「読んだか」は検査できない。

★ Phase の順序は追跡できるが、「読んでいなかったこと」は証明できない。
  確認方法 = snapshot comment の created_at と、Phase 2 の最終報告が引用する
  developer report の comment URL を突き合わせる。

★ TARGET_FRESHNESS_CHECK / CONTAMINATION_CHECK / PRIOR_EXPOSURE も
  ★ REVIEWER の自己申告である。★ 「読んでいない」ことは検査できない。
  ★ 物理的な新規 session を要求しないため、★ session 境界による
    構造的な保証は無い。
  ★ 記録することで ★ 事後に検査できる形にするだけである
    (申告と snapshot の created_at / manifest / 引用 URL を突き合わせる)。
```

---

## 4. 作業 AI への指示プロトコル

### 目的

複数の AI が並行作業するとき、どの指示に対する回答なのかが曖昧だと、
**古い指示への回答を根拠に次工程へ進んでしまう**。

### ルール(ユーザー ↔ 管理者 から見た運用)

仕様の正本は [development_workflow.md](development_workflow.md) 2.5節。
ここでは指示する側の運用として要点だけ示す。

```
INSTRUCTION_ID            すべての作業指示に一意な ID を付ける
                          形式 <ASSIGNEE>-<YYYYMMDD>-<連番>

状態                      PENDING / ANSWERED / WITHDRAWN

PER_WORKER_SERIALIZATION = YES
GLOBAL_SERIALIZATION     = NO

EMERGENCY SUPERSEDE       緊急時のみ PENDING を差し替える
                          EMERGENCY=YES / SUPERSEDES=<旧 ID> /
                          PREVIOUS_INSTRUCTION_STATUS=WITHDRAWN を明記
```

**直列化は作業者ごとである。**
これを全体直列化と誤読すると、片方の AI が作業している間もう一方を
遊ばせることになり、並行作業の意味が失われる。

### 例

```
太郎  TARO-20260904-001 = PENDING
次郎  JIRO-20260904-001 = ANSWERED

  JIRO-20260904-002 を出してよい     別 worker のキューは独立
  TARO-20260904-002 は出さない       同一 worker の直前指示が PENDING
```

### 例外

緊急時のみ、PENDING の指示を差し替えてよい。
撤回済み ID への遅れて届いた回答は、有効な完了報告として扱わない。

---

## 4.1 Instruction ID の採番

### 目的

`<ASSIGNEE>-<YYYYMMDD>-<連番>` の連番をいつリセットするかが曖昧だと、
**日付が変わっても連番が伸び続け、ID から「その日の何件目か」が読めなくなる**。
採番は 管理者 が行うため、その規則をここに置く。

### ルール

```
形式                       <ASSIGNEE>-<YYYYMMDD>-<NNN>
例                         JIRO-20260905-061 / TARO-20260905-072

INSTRUCTION_ID_DATE_TIMEZONE = Asia/Tokyo
SERIAL_SCOPE                 = PER_ASSIGNEE_PER_JST_DATE
```

`YYYYMMDD` は**日本時間の日付**を使う。UTC の日付は使わない。
連番は**作業者ごと・日本時間の日付ごと**に独立して管理する。
TARO と JIRO で同じカウンタを共有しない。

#### 同一日の中では単調増加

```
001 -> 002 -> 003 -> ... -> NNN
```

#### 日付が変わったら 001 へ戻す

```
SERIAL_RESET_ON_DATE_CHANGE = 001
```

日本時間で `YYYYMMDD` が変わったら、その日の最初の ID は必ず `001` とする。
前日の連番を翌日へ引き継がない。

```
GOOD    JIRO-20260905-061  ->  JIRO-20260906-001
        TARO-20260905-072  ->  TARO-20260906-001

BAD     JIRO-20260905-061  ->  JIRO-20260906-062
```

```
禁止   NEXT_SERIAL = 前日の連番 + 1

正     日付が変わった      NEXT_SERIAL = 001
       同じ日付のまま      NEXT_SERIAL = その日の最後に使った連番 + 1
```

#### 作業者ごとに独立

同じ日付でも作業者が違えば衝突ではない。

```
TARO-20260906-001
JIRO-20260906-001      どちらも有効
```

#### 同一日の中で番号を再利用しない

同一作業者・同一日付では、一度使った `NNN` を再利用しない。
次はいずれも「使用済み」として扱う。

```
実行完了 / ANSWERED / FAILED / BLOCKED /
CANCELLED / SUPERSEDED / 途中停止 / 作業開始後の取消
```

```
ONE_ID_ONE_RELAY_EVENT = YES
```

**作業者へ relay した時点で、その ID はその 1 回の relay に紐づく。**
以後は結果がどうであれ再利用しない。失敗した指示・作業者が BLOCKED を返した
指示の ID を再利用すると、どちらへの回答かを判別できなくなる。

### 作業 AI へ渡す前の下書き

管理者の内部で作成しただけで、まだ作業 AI へ提示していない下書きは、
ユーザーの求めに応じて**同じ ID のまま内容を修正してよい**。

```
RELAYED_TO_ASSIGNEE = YES になった後は、
同じ ID で異なる指示内容へ差し替えない。必要なら新しい ID を採番する。
```

これは 8節の「指示の差し替えは EMERGENCY のときだけ」を緩めるものではない。
**まだ届いていない下書き**と、**届いた後の指示**を区別しているだけである。

### 例外

なし。緊急差し替え(4節)を行う場合も、新しい ID は本節の規則で採番する。

---

## 4.5 管理者から開発者への指示文の出力形式

### 目的

管理者 が作成した作業指示は、**ユーザーが手作業でコピーして
開発者へ転送する**。つまり指示文はそのまま転送される前提の成果物である。

この経路で次のコミュニケーションロスが起きる。

```
指示の一部だけをコピーしてしまう
複数箇所に分かれていて転記漏れが起きる
Markdown のコードフェンスが入れ子になり形式が崩れる
コピー先で Markdown として解釈され、内容が意図せず変形する
```

いずれも**指示内容が正しくても、届いた時点で壊れている**という失敗であり、
作業 AI 側では検出できない。出力形式の側で防ぐ。

### ルール

```
A  管理者 -> TARO / JIRO の作業指示は、ユーザーが一括コピーして
   そのまま転送できる形式で出力する

B  指示全文は原則として「1つの外側コードブロック」の中へすべて収める

C  作業指示の一部を外側コードブロックの前後へ分散させない
   ユーザー向けの説明はコードブロックの外に置いてよいが、
   作業 AI へ転送すべき指示本文は必ず1つのコードブロック内だけで完結させる

D  外側コードブロック内部の指示文はプレーンテキストとして記述する

E  外側コードブロック内部で Markdown コードフェンスをネストしない
   指示内で例示を行う場合も、内側のコードブロックを作らず、
   プレーンテキストの字下げや区切り線で表現する

F  Markdown の見出し・表・引用等に依存しなくても意味が成立する指示文にする

G  目的は次の3点である

       COPYABILITY       = ONE_BLOCK
       INNER_FORMAT      = PLAIN_TEXT
       NESTED_CODE_FENCE = FORBIDDEN
```

`E` は特に壊れやすい。外側のブロックの中でコードフェンスを開くと、
そこで外側のブロックが閉じてしまい、以降がコードブロックの外へ出る。
結果として **C に違反した状態が意図せず発生する**。

`F` は転送先での再解釈に備えるためである。見出しや表に意味を負わせると、
プレーンテキストとして貼られた時点で構造が失われ、内容が変わってしまう。

### 例

**GOOD**

```
管理者の回答

  ユーザー向けの説明文(コードブロックの外。転送対象ではない)

  [単一の外側コードブロック]
    INSTRUCTION_ID = ...
    ASSIGNEE       = ...
    ...
    指示全文
    ...
    STOP
  [外側コードブロック終了]

このブロックだけをコピーすれば、作業 AI への情報連携が完結する。
```

**BAD-1 — 指示が複数箇所へ分散している**

```
コードブロックA  概要
通常の文章       追加条件
コードブロックB  完了報告の形式

-> ユーザーが複数箇所をコピーする必要があり、指示漏れの原因になる(C 違反)
```

**BAD-2 — 外側ブロックの内部でフェンスをネストしている**

```
外側コードブロックの中で、さらに Markdown の
python / text 等のコードブロックを開いてしまう

-> コードフェンスの対応が崩れ、Markdown 表示やコピー時に形式が崩れる
-> そこで外側ブロックが閉じ、以降が地の文になるため、
   「ブロックだけ」をコピーすると後半が欠落する(E 違反 -> C 違反)
```

### 適用範囲

```
対象      管理者 -> TARO の作業指示
          管理者 -> JIRO の作業指示

対象外    ユーザーへの通常の説明・レビュー結果・相談
          (すべての回答をコードブロック化するルールではない)

対象外    TARO / JIRO -> 管理者 の作業報告
          回答側の形式は development_workflow.md 2.5節等の既存ルールを維持する
```

### 例外

本節が定めるのは**指示内容そのもの**ではなく、
**管理者 が指示をどの形式でユーザーへ提示するか**である。
指示の中身に関する既存ルール(4節・5節・7節)はいずれも変更しない。

---

## 5. 管理者 が新しい指示を出す前の確認

### 目的

作業中の AI に別の作業を重ねると、どちらも中途半端に終わる。
また、既に終わっている前提で指示を出すと、前提が崩れたまま進む。

### ルール

通常指示を出す前に、対象 worker について次を確認する。

```
1  直前の INSTRUCTION_ID
2  その状態
3  ANSWERED または WITHDRAWN か
4  現在の code WIP / investigation WIP
5  対象 Issue
6  未通過の Human Gate の有無
```

同一 worker の直前指示が `PENDING` なら、通常の次指示を発行しない。
**別 worker の `PENDING` は妨げにならない。**

### 検証手順を指示するとき

指示に検証を含める場合、ローカル検証は狭く速く保つ。

```
targeted tests / related regression / ruff / mypy   を基本とする
local full pytest は原則として指示しない
全体回帰の正本は PR CI とする
```

`suite 全体でしか観測できない事象`(test order dependency /
global state pollution 等)を調べることが目的の場合のみ例外とし、
その理由を明示させる。

**詳細と例外条件の正本は
[development_workflow.md](development_workflow.md) 4節である。**
本文書へ手順を複製しない。

### 例外

`EMERGENCY=YES` の差し替えのみ(4節)。

---

## 5.5 Assignment Read Barrier(state を読み直す責務)

### 目的

5節は「作業者が空いているか」を確認する。本節は「**Issue の現況が本当に
その状態か**」を確認する。両者は別である。作業者が空いていても、Issue の
現況が古ければ誤った指示になる。

```
ASSIGNMENT_READ_BARRIER_OWNER = YES
```

### ルール

新しい Issue または別 Phase へ作業 AI を割り当てる前に、
[development_workflow.md](development_workflow.md) 6.5節の
**Assignment Read Barrier** を実行する。

```
管理者 は、記憶・会話要約・古い Issue 記述だけを根拠に
新規 implementation を指示してはならない。
```

確認項目・applicability(N/A 条件)・`ASSIGNMENT_BASELINE` の形式・
`ISSUE_STATE_SNAPSHOT` の contract・freshness gate・P0 例外はいずれも
**development_workflow.md 6.5節が正本**である。本文書へ複製しない。

本文書が定めるのは、**それを誰が実行するか**だけである。

```
read barrier の実行            管理者
state の書き戻し               state を変えた actor(開発者 / 管理者 / 利用者)
```

### drift を検出した場合

```
ISSUE_STATE_FRESHNESS_GATE = FAIL
  -> 新規 implementation 指示を出さない
  -> 先に read-only の status reconciliation を指示する
  -> 同期後に implementation gate を再評価する
```

`STATE_DRIFT_DETECTED` は不合格判定ではない。3節の `INSUFFICIENT_EVIDENCE` と
同じく「まだ判断材料が揃っていない」状態であり、**推測で埋めて先へ進めない。**

### Priority の鮮度確認

read barrier では state だけでなく **Priority の鮮度**も確認する。

```
PRIORITY_READ_OWNER            = MANAGER
ASSIGNMENT_PRIORITY_FRESHNESS_REQUIRED = YES
```

worker assignment を出す前に、**latest priority label** と
**latest Issue evidence**(最新コメント / snapshot / PR / Production evidence)の
整合を確認する。このとき **functional evidence(投資判断への影響)と
non-functional evidence(security / privacy / data protection / cost /
reliability 等)の双方**を確認する。Priority は両者の高い方で決まるため、
片方だけを見て「変化なし」と判断しない。判定基準そのものは
[docs/issue_label_policy.md](issue_label_policy.md) §4 が正本、
再評価すべき時点は [development_workflow.md](development_workflow.md) 9.5節が
正本であり、本文書へ複製しない。本文書が定めるのは**誰が確認するか**だけである。

矛盾していた場合。

```
PRIORITY_RECONCILIATION_REQUIRED
  -> 原則、新しい通常 implementation assignment を出す前に reconcile する
```

例外は Production の P0 incident に対する**必要最小限の containment** のみで、
その場合も事後に reconcile する。

### ユーザーが state を変えた後(merge 等)

merge は 2.6節のとおり `MERGE_EXECUTOR = USER` であり、作業 AI は
`PR_MERGED` / `MAIN_CI_PASS` を自ら書き戻せない。

```
NEXT_MANAGER_GATE_OWNS_RECONCILIATION = YES
```

ユーザーによる merge・label 変更・Issue 操作の後、**次の 管理者 gate が
reconciliation の確認責任を持つ。** 管理者 自身が durable comment を残しても、
作業 AI へ reconciliation を指示してもよい。**「いずれ誰かが同期するだろう」
として次工程へ進めない。**

### 例

```
BAD
  会話要約に「#N は未実装」とあった
  -> そのまま「Phase B を実装してください」と指示
  -> 実際には remote branch へ実装済み commit が push されていた
  -> 二重実装になりかけた

GOOD
  割当前に Issue / labels / 最新 snapshot / 後続コメント / PR /
  remote branch / main 包含を確認
  -> branch 上に未 merge の実装を検出
  -> STATE_DRIFT_DETECTED=YES として実装指示を出さず、
     先に status reconciliation を指示
```

この失敗は「handoff コメントが雑だったから」ではなく、
**割当前に現況を読み直す手順が無かったから**起きる。
丁寧な handoff では代替できない。

### 例外

`development_workflow.md` 6.5節の **P0 例外**のみ。
P0 の Production incident で即時の被害抑止が必要な場合に限り
read barrier を最小確認へ縮小してよい。

**ただし Human Gate(2節)・merge 承認(2.6節)・Production approval・
exact ChangeSet approval はいずれも緩和しない。**
読む手間を減らす例外であり、承認を飛ばす例外ではない。

---

## 6. 回答の対応付け(correlation)

### 目的

「どの指示への回答か」が確定していない報告を、
次工程の根拠にしないため。

### ルール

回答は `INSTRUCTION_ID` で対応付ける。次の場合は自動的には根拠にしない。

```
ID が欠落している
別の ID が書かれている
撤回済み(WITHDRAWN)の ID である
```

この場合はまず対応関係を確認する。破棄するという意味ではない。

**MANUAL_CORRELATION**

ユーザーが明示的に「これは太郎の `TARO-xxx` への回答である」と対応付け、
管理者 が内容の一致を確認できた場合に限り、
`MANUAL_CORRELATION` として扱ってよい。

```
MANUAL_CORRELATION = YES
CORRELATED_ID      = <対象 INSTRUCTION_ID>
CORRELATED_BY      = USER
```

### 例外

`MANUAL_CORRELATION` は例外処理であり、常用しない。
毎回これに頼る状態は、ID を付けていないのと変わらない。

---

## 7. 1指示 / 1回答

### 目的

複数の作業結果が1つの回答に混ざると、
どこまでが完了しているのかが読み取れなくなる。

### ルール

```
作業 AI  1つの回答には1つの INSTRUCTION_ID の結果だけを書く
         別指示の残作業・別 Issue の追加調査を混ぜない

管理者  「前回答へのレビュー」と「別 worker への新規指示」を
         不用意に混在させない
         必要な場合は対象 worker と INSTRUCTION_ID を明示して分ける
```

### 例外

1つの指示の中に複数の確認項目が含まれる場合は、当然まとめて回答してよい。
禁止しているのは**別々の指示の結果を混ぜること**である。

---

## 8. ルール変更の扱い

### 目的

会話で決めた最新の判断と、文書の記述が食い違うことは必ず起きる。
そのとき何を優先するかを、あらかじめ決めておく。

### ルール

```
LATEST_EXPLICIT_HUMAN_DECISION_WINS_TEMPORARILY
```

ユーザーが明示的に確定した判断は、その場では文書より優先する。
ただし**文書を放置しない**。恒久ルールであれば、
次の適切なタイミングで本文書または該当する SSoT へ同期する。

同期は governance change であるため、
[development_workflow.md](development_workflow.md) 9.5節により
**Issue を起点とする**(doc-only であっても governance rule の変更は Issue 必須)。

### POLICY_AUTHORITY = HUMAN_ONLY

```
POLICY_AUTHORITY = HUMAN_ONLY
```

**本節が既に定めていることへ識別子を与えるものであり、規則の内容は変更しない。**
上の「ルール」は以前から「`管理者 が独自の判断でルールを追加・変更してよい`という
意味ではない」「作業 AI が独自にルールを変える根拠にはならない」と定めている。
参照可能な識別子が無かったため、機械からも入口からも指せなかった。
本項はそれを指せるようにする(Issue #337)。

恒久規則が成立する条件。

```
利用者の明示的な承認  +  本文書または該当 SSoT への反映
```

管理者・開発者は、いずれの役割であっても恒久規則を制定できない。

#### AI が制定してはならないもの

```
新しい義務 / 新しい禁止 / 新しい Human Gate / 新しい承認条件
新しい必須フィールド / 新しい必須フォーマット
新しい作業停止条件 / 新しい完了条件
正本に無い status / gate / declaration を、強制力のあるものとして運用すること
```

**この列挙は例示である。** 他の AI に対する義務・禁止・Gate・完了条件として
扱うものは、名称がここに無くてもすべて本項の対象である。

#### 一回限りの指示と恒久ルールの判定

```
判定質問  その指示を「他の AI に対する義務・禁止・Gate・完了条件」として扱うか

扱う      -> 恒久ルール。下記 RULE_PROPOSAL の手続きへ
扱わない  -> 一回限りの作業指示。文書化しない
```

利用者のその場限りの具体的な操作指示(「この Issue を先に見て」等)は
恒久ルールではなく、本項によって無効化されない。

#### 単独では恒久規則の正本にならないもの

```
チャットの合意 / AI の memory / handoff / Issue コメント /
過去の AI の出力 / 慣行 / 多数回使用されている用語
```

いずれも記録としては有効である。**恒久規則の正本になるのは本文書と各 SSoT だけ**
であるという意味である。

### RULE_PROPOSAL(恒久規則を追加・変更する手続き)

上の「ルール」が求める Issue 起点の同期を、手続きとして具体化する。
**新しい承認を追加するものではない。** 既存の Issue 起点の原則
([development_workflow.md](development_workflow.md) 9.5節)と、
governance / docs 改善の集約(同 9.6節)に接続する。

```
1  提案      Issue へ RULE_PROPOSAL として書く
             何を / なぜ / どの正本のどの節へ入れるか / 既存規則との関係
             (新設か、既存規則の明確化か)を区別して書く
2  承認      利用者が承認する。承認の記録は下記
3  反映      正本へ PR で反映する。CLAUDE.md へ規則本文を複製しない
4  発効      merge と main CI PASS の後。必要なら明示的な発効宣言を置く
```

**docs が main に入っただけでは発効しない規則がある。** 前例として
[development_workflow.md](development_workflow.md) 2.6.10節と
[ai_operation_message_contract.md](ai_operation_message_contract.md) 0節が
「人間による明示的な発効宣言」を要件としている。新しい発効方式を作らず、
この形を踏襲する。

```
IMPLEMENTATION_INSTRUCTION != NEW_POLICY_EFFECTIVE_FOR_ALL_WORK
```

規則を作るための作業指示が出たことと、その規則が全作業へ適用されることは別である。

**承認記録の書式は
[ai_operation_message_contract.md](ai_operation_message_contract.md) 8節が正本**
であり、本文書へ複製しない。

### MEMORY_POLICY_AUTHORITY = NONE

```
MEMORY_POLICY_AUTHORITY = NONE
```

AI の memory は恒久規則の正本にならない。本節の適用範囲の明示であり、
新しい規則ではない。

memory は repository の外にあり、CI からも review からも見えない。そこへ規範情報を
保存すると、セッションをまたいで正本と同じ強さで再現する。実際に、撤回された
運用ルールが作業 AI の memory へ「利用者からのフィードバック」として保存されていた
(Issue #337)。撤回の連絡が無ければ、翌日以降も従い続ける状態だった。

```
保持してよい    作業途中の事実 / Issue 番号 / branch / commit SHA /
                未完了の作業 / 調査結果 / 環境の癖
保持してはいけない
                「〜しなければならない」「〜は禁止」といった規範
                Human Gate / 必須フィールド / 必須フォーマット / 完了条件
                新しい status / 新しい承認条件
```

**出所を区別して保存する。** 利用者が述べたことと、AI が自分で決めたことを
混ぜない。**自分が作った運用を「利用者からのフィードバック」として保存しない。**

#### session bootstrap

セッション開始時に規則を memory から復元しない。必要になった時点で
正本を読む(JIT lookup)。どの操作でどの節を読むかの索引は
`docs/policy_registry.yaml` にある(索引であり正本ではない)。

### 例

```
ユーザーが明示的に、新しい判定語を導入すると決定した
  -> 最新の明示的な Human decision として、その場では適用する
  -> 恒久ルールなら Issue を起点に本文書または該当 SSoT へ同期する
  -> 一時的な運用なら文書化しない
```

この節が優先を認めるのは、**ユーザーが明示的に確定した判断**に限る。
`管理者 が独自の判断でルールを追加・変更してよい`という意味ではない。
判定語について言えば、3節の4判定は恒久ルールであり、
**管理者 が独自に判定語を増やすことは本節の対象外である**(3節の例外なしを維持)。

### 例外

一時的・その作業限りの取り決めは文書化しない。
恒久ルールと一時的判断を区別することがこの節の要点である。

開発者についても同様であり、本節は
「ユーザーの明示判断が文書より新しい場合の優先順位」を定めるものであって、
作業 AI が独自にルールを変える根拠にはならない。

---

## 9. セッション開始時の bootstrap

### 目的

**「GitHub に置けば 管理者 が自動的に常時読み込む」という前提は成り立たない。**
管理者 はリポジトリを勝手に読まない。明示的に読ませる必要がある。

### ルール

新しいチャット / セッションを開始するとき、
または大きな開発作業を始めるときは、可能な限り最初に次を確認する。

```
1  docs/user_manager_collaboration_protocol.md   (本文書)
2  CLAUDE.md
3  作業に必要な development / release の SSoT
     docs/development_workflow.md
     docs/issue_label_policy.md
     docs/operations_manual.md
```

読み込みが行われていない状態で統制上の判断
(Human Gate / release 判定 / blocker の扱い)を進めない。

### 例外

軽微な質問や、統制判断を伴わないやり取りでは省略してよい。

---

## 10. 恒久ルールと現在状態を分ける

### 目的

現在の状態を恒久文書に書くと、更新が追いつかず、
**文書が「たいてい古い情報」になって信用されなくなる**。

### ルール

次は**恒久文書に書かない**(dynamic state)。

```
現在の TARO / JIRO の INSTRUCTION_ID とその状態
現在作業中の Issue
現在の PR
current main SHA
current Production SHA
現在待ちの Human Gate
```

これらは次で管理する。

```
GitHub Issue
Pull Request
現在の会話
Issue / PR への durable status comment
```

### 例

```
文書に書く      「release-blocker の解除には Production verification と
                  人間承認が必要」            <- 恒久ルール

文書に書かない  「現在 #52 と #61 が release-blocker」   <- 現在状態
                 -> Issue の label と durable comment が正本
```

### 例外

なし。恒久文書に日付つきで残すのは「変更履歴」だけである。

---

## 11. 公開リポジトリとしての取り扱い

### 目的

このリポジトリは公開されている。
統制文書そのものが情報漏洩の経路にならないようにする。

### ルール

公開する記録(Issue / PR / comment / 本文書)へ次を載せない。

```
実在人物の個人情報(氏名・家族名・個人メール・住所・電話番号)
実際の保有数量 / 取得単価 / portfolio 価値 / 個別保有銘柄
secret の実値
AWS account ID / 不要な ARN
具体的な security attack map
```

詳細は [CLAUDE.md](../CLAUDE.md) の個人情報ルールが正本であり、
CI の `pii-scan` が既知の実在人物名を検知した場合はビルドを失敗させる。

記録が必要な場合は次を明示する。

```
DISCLOSURE = PUBLIC_SANITIZED
```

構造・件数・割合・commit SHA・Issue / PR 番号は記載してよい。

### 例外

なし。「調査のためだけ」であっても secret の実値を取得・出力しない。

---

## 12. この文書が扱わないこと

```
実装の進め方 / lane / WIP 制限                        -> development_workflow.md
ローカルテスト方針 / local full pytest の可否と例外    -> development_workflow.md 4節
指示プロトコルの仕様                                   -> development_workflow.md 2.5節
Issue state 同期の仕様(writeback / snapshot / gate)  -> development_workflow.md 6.5節
作業報告 / Human Gate 提示 / AUTHORIZED_PHASES / 確認質問の形式
                                                       -> ai_operation_message_contract.md
機能領域ベースの WIP 運用ルール                         -> development_workflow.md 2.6節
機能領域・機能・共通部品の一覧                         -> functional_domains.md
Issue の分類と label                                   -> issue_label_policy.md
Production の具体的な運用手順                          -> operations_manual.md
利用者から見た機能仕様                                 -> functional_spec.md
```

本文書を厚くしすぎない。
迷ったら「これは誰が誰へどう伝えるかの話か」を基準に判断する。
そうでなければ他文書が正本である。

---

## 変更履歴

| 日付 | 変更内容 |
|---|---|
| 2026-09-04 | 新規作成(Issue #122)。ユーザー ↔ ChatGPT 間の協働ルールを、チャット履歴・AI の記憶に依存させず GitHub 上の SSoT として管理するための文書。役割分担(承認はユーザー / 推奨は ChatGPT)、Human Gate の一覧と「承認はその操作・その対象に限る」原則、レビュー判定4種と `INSUFFICIENT_EVIDENCE ≠ REJECT` / `PASS_WITH_CONDITIONS ≠ Human Gate 通過` の区別、指示プロトコル(`INSTRUCTION_ID` / 作業者ごとの直列化 / 緊急差し替え)を指示側から見た運用として整理、4.5節「ChatGPT から作業 AI への指示文の出力形式」(指示はユーザーが手作業で転送するため、内容が正しくても届いた時点で壊れるという失敗が起きる。これを出力形式の側で防ぐ。**作業 AI への指示は単一の外側コードブロックへ収める** / **内部は plain text とする** / **nested code fence は禁止** / **一括コピー可能性を確保する**。指示の一部を外側ブロックの前後へ分散させず、例示は字下げや区切り線で表現し、見出し・表・引用に依存しなくても意味が成立する指示文にする。GOOD / BAD-1(分散)/ BAD-2(フェンスのネスト)の例を記載。対象は ChatGPT から TARO / JIRO への作業指示のみで、ユーザーへの通常回答や作業 AI からの報告形式は対象外)、新規指示前の確認手順、回答の correlation と例外的な `MANUAL_CORRELATION`、1指示1回答、ルール変更時の `LATEST_EXPLICIT_HUMAN_DECISION_WINS_TEMPORARILY` と同期義務(優先を認めるのはユーザーが明示的に確定した判断に限り、ChatGPT や作業 AI が独自にルールを追加・変更する根拠にはしない)、セッション開始時の明示的 bootstrap(自動読み込みを前提にしない)、恒久ルールと dynamic state の分離、公開リポジトリでの取り扱いを記載した。**既存 governance の要求はいずれも緩和していない**(merge / Production 操作 / release-blocker 解除の人間承認、`PER_WORKER_SERIALIZATION=YES` / `GLOBAL_SERIALIZATION=NO` の区別を含む)。コード・Production 挙動の変更なし |
| 2026-09-04 | 協働ルールを追加(Issue #122)。**2.5節「提案・承認・実行・検証を混同しない」** — `PROPOSED` / `APPROVED` / `EXECUTED` / `VERIFIED` を別状態として扱う(`STATE_SEPARATION=YES`)。`MERGE_READY=YES` は提案であって承認でも実行でもなく、実行しただけでは検証済みでもない。release-blocker についても「解除できると判断」「解除を承認」「label を削除」「削除後の state を確認」を別状態とする。2節の Human Gate は緩和しない。**2.6節「merge の実行者」** — `MERGE_EXECUTOR = USER`。通常は作業 AI へ merge を指示せず、ChatGPT の `MERGE_READY` 提示後にユーザー自身が GitHub 上で merge する(`USER_MERGE_ACTION = HUMAN_APPROVAL + EXECUTION`)。事前の承認宣言は必須にしない。ChatGPT が `MERGE_READY` を出していない PR をユーザーが merge した場合も、人間による実行であるため勝手に revert しない。**PR merge を Production の承認として扱わない。** **3.5節「判断は証拠を先に置く」** — `EVIDENCE_FIRST=YES`。重要 Gate では作業 AI の自己申告だけで事実認定せず、GitHub / CI / Production の実体と突合する。矛盾時は差異を明示し実体を優先し、確定できなければ `INSUFFICIENT_EVIDENCE` とする。軽微な報告を毎回過剰検証するルールではない。**3.6節「証拠の鮮度と検証可能性」** — `FRESHER_VERIFIABLE_EVIDENCE_WINS=YES`。現在の検証可能な実体 > 検証済みの最新 durable comment > 過去の durable comment > 古い記述、の順で採用する。ただし単純なタイムスタンプ順ではなく、freshness と verifiability の両方で判断する。未検証の推測は最新であっても優先しない。古い記述が stale でも履歴として削除・改ざんしない。**3.7節「merge 判断を支援する提示形式」** — PR 番号を必ず明示し、`REVIEW_VERDICT` / `MERGE_READY` / `REMAINING_ISSUES_OR_CONCERNS` / `MERGE_BLOCKING_CONCERN` / `OTHER_ISSUE_IMPACT` / `PRODUCTION_IMPACT` / `RECOMMENDED_ACTION` をセットで示す。**残課題があることと、それが merge を止めるべきかは別**であることを `MERGE_BLOCKING_CONCERN` で明示する。形式は Markdown の表へ固定しない。あわせて5節へ、指示に検証を含める際は targeted tests を基本とし local full pytest を原則指示しない旨を記載した(詳細と例外条件の正本は development_workflow.md 4節。本文書へ複製しない)。既存ルール(指示プロトコル / 直列化 / 1指示1回答 / 出力形式 / レビュー判定4種 / Human Gate / dynamic state 分離 / PUBLIC_SANITIZED)はいずれも変更していない。コード・Production 挙動の変更なし |
| 2026-09-04 | 1.5節「Production deploy 関連作業の担当」を新設(Issue #122)。deploy 工程は途中で担当が入れ替わると、承認対象の exact SHA・exact ChangeSet・build 済み artifact の同一性といった前提が引き継がれない。そこで実作業の担当を1体へ集約し `PRODUCTION_DEPLOYMENT_EXECUTOR = TARO` とした。対象は release 対象 SHA の最終確認 / main CI / release-blocker inventory / clean worktree・unpushed / sam build / ChangeSet CREATE / ChangeSet の read-only 確認 / 承認後の EXECUTE / CloudFormation terminal state / immediate Production verification / deploy artifact の同一性確認 / stack event 確認。次郎は調査・設計・実装・PR 作成・release readiness 調査・Verification Plan 設計・Production evidence の read-only 分析まで担当できるが、deploy 実作業は既定で行わない(`DEPLOY_OPERATION_DELEGATION_TO_JIRO = FORBIDDEN_BY_DEFAULT`)。**担当の集約は承認の省略を意味しない。** ChangeSet CREATE と EXECUTE は別 Human Gate、PR merge の承認は Production の承認ではない、main advance で exact SHA 承認は失効、ChangeSet 再作成で exact ChangeSet 承認は失効、という既存の区別をいずれも維持する。また immediate verification は太郎の担当とする一方、自然実行後の業務的な evidence 分析は内容に応じて割り当ててよく、`PRODUCTION_DEPLOYMENT_EXECUTOR = TARO` は `ALL_PRODUCTION_ANALYSIS_ASSIGNEE = TARO` を意味しないことを明記した。ユーザーが別担当を明示指定した場合は 8節の `LATEST_EXPLICIT_HUMAN_DECISION_WINS_TEMPORARILY` に従う。あわせて development_workflow.md 10節へ、deploy 実作業の担当の正本が本節であることの参照を1行追加した(詳細は複製していない)。既存ルール(`MERGE_EXECUTOR=USER` / 状態分離 / EVIDENCE FIRST / 証拠の鮮度 / ローカルテスト方針 / 指示プロトコル / 出力形式 / Human Gate / PUBLIC_SANITIZED)は変更していない。コード・Production 挙動の変更なし |
| 2026-09-04 | 5.5節「Assignment Read Barrier(state を読み直す責務)」を新設(Issue #157)。5節は「作業者が空いているか」を確認するが、**Issue の現況が本当にその状態か**は確認していなかった。会話要約や古い Issue 本文だけを根拠に「未実装」と判断し、remote branch 上の実装済み commit を見落として二重実装になりかけた事例が発生している。そこで `CHATGPT_ASSIGNMENT_READ_BARRIER_OWNER = YES` とし、新しい Issue / 別 Phase へ作業 AI を割り当てる前に development_workflow.md 6.5節の Assignment Read Barrier を ChatGPT が実行することを定めた。**確認項目・applicability・`ASSIGNMENT_BASELINE`・`ISSUE_STATE_SNAPSHOT` contract・freshness gate・P0 例外の正本は development_workflow.md 6.5節であり、本文書へ複製していない。** 本節が定めるのは実行主体(read barrier = ChatGPT / state の書き戻し = state を変えた actor)だけである。あわせて、`STATE_DRIFT_DETECTED` は不合格判定ではなく 3節の `INSUFFICIENT_EVIDENCE` と同じく判断材料の不足であり推測で埋めないこと、`ISSUE_STATE_FRESHNESS_GATE = FAIL` では実装指示を出さず先に read-only reconciliation を指示すること、ユーザーによる merge・label 変更・Issue 操作の後は `NEXT_CHATGPT_GATE_OWNS_RECONCILIATION = YES` として次の ChatGPT gate が同期確認の責任を持つこと(「いずれ誰かが同期するだろう」で次工程へ進まない)を記載した。12節へ Issue state 同期の正本の所在を1行追加した。例外は 6.5節の P0 例外のみで、**Human Gate(2節)・merge 承認(2.6節)・Production approval・exact ChangeSet approval はいずれも緩和していない。** 既存ルール(役割分担 / 指示プロトコル / 直列化 / 1指示1回答 / 出力形式 / レビュー判定4種 / EVIDENCE FIRST / 証拠の鮮度 / dynamic state 分離 / PUBLIC_SANITIZED)は変更していない。コード・Production 挙動の変更なし |
| 2026-09-05 | Assignment Read Barrier へ「Priority の鮮度確認」を追加(#122)。`CHATGPT_PRIORITY_READ_OWNER = CHATGPT` / `ASSIGNMENT_PRIORITY_FRESHNESS_REQUIRED = YES` とし、worker assignment を出す前に latest priority label と latest Issue evidence の整合を確認することを定めた。矛盾時は `PRIORITY_RECONCILIATION_REQUIRED` として、原則あたらしい通常 implementation assignment より先に reconcile する(例外は Production P0 incident の必要最小限 containment のみで、その場合も事後に reconcile する)。**Priority の判定基準は issue_label_policy.md §4、再評価時点は development_workflow.md 9.5節が正本であり本文書へ複製していない。** 本節が定めるのは確認の実行主体だけである。既存の役割分担 / Human Gate / レビュー判定 / read barrier の所有者・例外は変更していない |
| 2026-09-05 | Priority の鮮度確認について、functional evidence と non-functional evidence(security / privacy / data protection / cost / reliability 等)の双方を確認することを最小追記した(#122)。Priority は両者の高い方で決まるため、片方だけを見て「変化なし」と判断しない。**判定基準は issue_label_policy.md §4.13〜§4.21 が正本であり本文書へ複製していない。** 実行主体(`CHATGPT_PRIORITY_READ_OWNER`)と例外は変更していない |
| 2026-09-05 | Severity 軸の廃止(#122)に伴い、責務表の記載を Issue 分類 / Priority / release-blocker / Progress Status へ同期した。worker instruction・レビュー・assignment read barrier のいずれでも Severity の判定・writeback・鮮度確認を要求しない。**過去の instruction 例やコメントに残る Severity 記載は履歴として保持する。** Priority の鮮度確認(functional / non-functional 双方)と実行主体、Human Gate・レビュー判定・役割分担は変更していない |
| 2026-09-05 | ユーザーの明示承認により2つの恒久ルールを追加(Issue #122)。**1.6節「ユーザーへの説明の水準」** — `USER_EXPLANATION_LEVEL = IT_FOUNDATION_AWS_LITERATE`。Human Gate は「人間が理解したうえで決める」ことが前提であり、理解できない説明に対する承認は Human Gate として成立しない。前提とする知識水準は **IT 基礎知識(応用情報技術者試験相当)+ AWS 主要マネージドサービスの名称・概要の理解**であり、一方で **本プロジェクトの実装・運用の詳細は自明として扱わない**。目的は噛み砕くこと自体ではなく `USER_CAN_MAKE_AN_INFORMED_DECISION` を満たすこと。Lambda / DynamoDB / CloudFormation / S3 / Secrets Manager / EventBridge / IAM / CI / PR / merge / main / Production / PITR / RPO / RTO 等の一般的な IT・AWS 用語はそのまま使ってよく、毎回初歩から言い換えない(サービス名を一般語へ置き換えるのは禁止)。代わりに **本プロジェクト固有の運用概念**(BLOCKED_BY_RELEASE_SCOPE / waiting:本番検証 / grouped release / code WIP / Assignment Read Barrier / Issue State Snapshot 等)、**AWS でも取り違えやすい挙動**(ChangeSet の CREATE と EXECUTE の違い / Dynamic Reference の再解決条件 / Deletion Protection・DeletionPolicy・UpdateReplacePolicy の違い / PITR restore が新しいテーブルになること / merge 済みだが Production 未反映という状態)、**Human Gate の範囲**には背景・因果関係・影響を添える。機械可読の状態値の併記は禁止しないが、それだけをユーザー向け説明としない(`INTERNAL_STATUS_ONLY_RESPONSE = FORBIDDEN`)。最低限「今どうなっているか / なぜ / 進めると何が危険か / 次に何をするか / 今ユーザーがすること / 次に判断が要るのはいつか」を含め、ユーザーの操作が不要なら「今あなたがすることはありません」と明示する。Human Gate の依頼では「承認すると何が起きるか」と**「この承認ではまだ何が起きないか」**を必ず対で示し、`ChangeSet 作成 != Production 反映` / `merge != Production 承認` を説明の側でも崩さない。技術情報を削りすぎて因果関係が見えなくなる説明は禁止し、`IT_FOUNDATION_AWS_LITERATE != TECHNICAL_DETAIL_FORBIDDEN`(Issue/PR/SHA/CI run 等は監査証跡として残す)ことを明記。**対象は ChatGPT からユーザーへの回答のみ**で、作業 AI の完了報告は従来どおり機械可読形式でよい(4.5節・7節の contract は不変)。**4.1節「Instruction ID の採番」** — `INSTRUCTION_ID_DATE_TIMEZONE = Asia/Tokyo` / `SERIAL_SCOPE = PER_ASSIGNEE_PER_JST_DATE` / `SERIAL_RESET_ON_DATE_CHANGE = 001`。日本時間で日付が変わったら連番を 001 へ戻し、前日の連番を翌日へ引き継がない。作業者ごとに独立(同日でも TARO / JIRO の 001 は衝突ではない)。同一作業者・同一日付では使用済み番号を再利用しない(完了 / CANCELLED / SUPERSEDED / 途中停止 / 取消をいずれも使用済みとする)。作業 AI へ未提示の下書きは同じ ID のまま修正してよいが、`RELAYED_TO_ASSIGNEE = YES` の後は同じ ID で内容を差し替えない。development_workflow.md 2.5.1節へは cross-reference のみを置き、採番規則の全文は複製していない。**既存 governance はいずれも緩和していない**(Human Gate / `CREATE != EXECUTE` / `merge != Production 承認` / `PER_WORKER_SERIALIZATION` / 緊急差し替えの条件を含む)。Severity 軸は復活させていない。コード・Production 挙動の変更なし |
| 2026-09-06 | 3.8節「機能領域 WIP のレビュー観点」を新設(Issue #177)。development_workflow.md 2.6節の領域ベース WIP モデルでは、作業者が自分で触る領域と `LOCK_LEVEL` を判定するが、**その判定の誤りは CI では検出できない**。「本来は買い判定の領域も lock すべきだったのに保有判断の領域だけで進めた」という誤りはレビューでしか気づけないため、`CHATGPT_LOCK_REVIEW_OWNER = CHATGPT` として、実装レビューの観点へ `PRIMARY_DOMAIN` / `LOCKED_DOMAINS` / `SHARED_TOUCHED` の網羅性 / `LOCK_LEVEL` の妥当性 / LEVEL_1 の compatibility evidence / scope 拡大の有無を追加した(人間承認 H5)。判定できない場合は 3節の `INSUFFICIENT_EVIDENCE` とし、「追加だけの diff に見えるから LEVEL_1 でよい」と推測で通さない。**確認された lock omission は合格にしない**(`LOCK_OMISSION_REVIEW_PASS_ALLOWED = NO`)。lock の漏れは他の作業者が「その領域は空いている」と誤判断して並行着手できてしまうため、領域ベース WIP の安全性そのものを破る。material な omission が確認できた場合は `REJECT`、判断する証拠が足りない場合と宣言の説明が不足している場合は `INSUFFICIENT_EVIDENCE` とし、**確認された omission を `PASS_WITH_CONDITIONS` で通すことを禁止する**(条件付き合格は方針が妥当な場合の判定であり、lock 漏れは方針ではなく安全性の欠落である)。判定語は 3節の 4 種から選び独自に増やさない。`REJECT` とする場合も、漏れている領域 / その根拠となる参照元 / 関係する共通部品と consumer / 必要な追加 lock を具体的に示し、作業者は STOP -> 宣言の再評価 -> 必要 lock の取得 -> scope 再宣言 -> 必要なら main 取り込み -> re-review の順で対応する。**「具体的に理由を示す」ことは「REJECT しない」ことではない。** **領域・機能・共通部品の一覧は functional_domains.md、WIP ルール本文は development_workflow.md 2.6節が正本であり本文書へ複製していない。** 本節は 2.6節の発効(`DOMAIN_WIP_MODEL_ACTIVE = YES`)をもって適用を開始し、それまでは確認義務を課さない。あわせて 12節へ正本の所在を 2 行追加した。**2節の Human Gate、2.6節の merge 実行者、Production approval、exact ChangeSet approval はいずれも変更していない。** コード・Production 挙動の変更なし |
| 2026-09-06 | 2.6節へ「MERGE_READY 判定に含める確認(G2)」を追加した(Issue #181)。PR レビューでは diff の妥当性に加え、**その PR が merge された後にその Issue へ別の Progress Status 相当の残作業が残るか**を確認し、残る場合は原則として merge より前に Issue を分割する。Progress Status は Issue 全体を表す単一 label であるため、未実装の作業単位を抱えたまま `status:マージ済` へ進むと「実装は終わっている」と読める label のまま release 判定を誤らせる(Issue #20 で実際に発生し、post-merge の reconciliation で分割した)。**これは既存の review gate へ確認項目を 1 つ加えるものであり、新しい Human Gate を増やすものではない。** `MERGE_EXECUTOR = USER`・`USER_MERGE_ACTION = HUMAN_APPROVAL + EXECUTION`・merge を Production 承認として扱わない原則・2節の Production Human Gate はいずれも変更していない。判定ルールの正本は issue_label_policy.md §7.3、snapshot の記録項目は development_workflow.md 6.5.3節であり、本文書へ複製していない。docs のみの変更であり、コード・Production 挙動の変更なし |
| 2026-09-06 | 3.7節へ発効後の正本の所在と `MERGE_APPROVAL_IS_BOUND_TO_EXACT_REVIEWED_HEAD = YES` を追記した(Issue #184)。docs/ai_operation_message_contract.md の merge から発効までの間、提示形式の正本が本節と同文書のどちらかが曖昧になりうるため、`BEFORE_ISSUE_184_ACTIVATION` は本節、`AFTER_ISSUE_184_ACTIVATION` は同文書 8節と明示し、発効後に本節を並列の規範として扱わないことにした(`DUPLICATE_SSOT` の回避)。**本節が定める `REVIEW_VERDICT` / `MERGE_BLOCKING_CONCERN` 等の判断の中身は発効後も本節が正本であり、変わるのは提示のしかただけである。** あわせて merge 承認がレビューした exact PR head SHA に紐づき、head が変われば失効することを明記した(2節の「承認はその操作・その対象に限る」と同じ原則であり、新設の緩和ではない)。コード・Production 挙動の変更なし |
| 2026-09-06 | Instruction ID の使用済み判定を補完し、Human Gate の提示形式の正本を参照へ移した(Issue #184)。(1)4.1節の「使用済み」の列挙へ `ANSWERED` / `FAILED` / `BLOCKED` を追加し、`ONE_ID_ONE_RELAY_EVENT = YES` を明記した。従来の列挙(実行完了 / CANCELLED / SUPERSEDED / 途中停止 / 作業開始後の取消)には、**指示が失敗した場合と作業者が BLOCKED を返した場合**が含まれておらず、その ID を再利用すると回答の対応付けが壊れる余地が残っていた。採番規則そのもの(Asia/Tokyo の日付 / 作業者別の日次連番 / 日付変更で 001 へリセット)は変更していない。(2)2節へ「提示のフォーマット」を追加し、承認を求める際の形式(固定 4 節 + AUDIT_INFO の分離、gate 種別ごとの exact identifier の扱い)の正本が新設した docs/ai_operation_message_contract.md 8節であることを参照で示した。**本文書は「どの操作に承認が要るか」を定め、形式を複製しない。** 同文書は提示形式のみを定めるものであり `APPROVAL_UNIT_CONSOLIDATION = NO`、本節の承認単位を 1 つも統合・緩和していない。(3)12節へ正本の所在を 1 行追加した。既存の Human Gate 一覧 / `MERGE_EXECUTOR = USER` / exact ChangeSet approval / レビュー判定 4 種 / 指示プロトコル / 3.8節の lock omission 判定はいずれも変更していない。コード・Production 挙動の変更なし |
| 2026-09-06 | 役割を製品非依存にし、ファイル名を chatgpt_collaboration_protocol.md から改称した(Issue #190)。管理・レビュー役が 2026-09-06 に ChatGPT 上の AI から交代したことで、**役割が特定の生成AI製品名で書かれていると、担当が変わるたびに正本を書き換えることになる**という構造的な問題が表面化した。そこで `PRODUCT_AGNOSTIC_ROLE_NAMING = YES` とし、役割を権限と責務で定義する。(1)1節を 4 役割(`USER` / `MANAGER` / `DEVELOPER_WITH_DEPLOY` / `DEVELOPER`)で書き直し、各役割の権限・責務・禁止事項を明記した。開発者 2 役割の差は「deploy 実作業を行うか」の 1 点だけであり、調査・設計・実装・報告・state 書き戻しの規則はすべて共通である。(2)**現在の担当は本文書へ焼き込まない**(`ROLE_ASSIGNMENT_SSOT = Issue #122 の最新の durable な体制記録`)。恒久文書と現在状態を分ける 10節の原則に従う。(3)1.5節の `PRODUCTION_DEPLOYMENT_EXECUTOR` と `DEPLOY_OPERATION_DELEGATION` を役割ベースへ改めた(担当者名を書かない)。(4)識別子を `<対象>_OWNER = <役割>` 形式へ統一した(`LOCK_REVIEW_OWNER` / `ASSIGNMENT_READ_BARRIER_OWNER` / `PRIORITY_READ_OWNER` / `STATE_READ_OWNER` = `MANAGER`、`NEXT_MANAGER_GATE_OWNS_RECONCILIATION`、`USER_MANAGER_COLLABORATION_SSOT`)。接頭辞へ役割名を埋め込まないため、次に体制が変わっても識別子名が変わらない。(5)本文中の "ChatGPT" 47 か所を役割名へ置換し、歴史的名称として 1 節で 1 か所だけ定義した(過去の記録が誰を指すか分かるようにするため)。(6)ファイル名を製品非依存へ改称した。**転送用スタブは残さない。** 過去の GitHub metadata からの参照 61 件を実測したところ**すべて平文で markdown link は 0 件**であり、改称で壊れるリンクが存在しないためである。**過去の Issue コメント・snapshot に残る "ChatGPT" / `ACTOR = CHATGPT` は append-only の記録であり書き換えていない。** 本文書の変更履歴の過去エントリも編集していない。承認単位・Human Gate・レビュー判定 4 種・指示プロトコル・merge 実行者はいずれも変更していない。コード・Production 挙動の変更なし |
| 2026-09-08 | 3.8節へ「DoD 申告の確認」を追加した(Issue #252、打ち手 D-1)。development_workflow.md 3節が新設した DoD 5 項目の申告について、`DOD_DECLARATION_REVIEW_OWNER = MANAGER` とし、**「DoD 5 項目の申告があるか(空欄・無言の省略が無いか)」「申告と diff が矛盾していないか」**の2 項目を実装レビューの確認観点へ加えた。**レビュワーが「正しいか」を判定するのではなく「申告されているか」「矛盾していないか」を見る**(正しさの一次責任は実装者にある)。閾値の定数に diff があるのに「境界の連続性 = 該当なし」と書かれている場合や、「該当あり・未解消」と書かれているのに引き継ぎ先の Issue が無い場合は FAIL とする。**判定基準の本文は development_workflow.md 3節が正本であり本文書へ複製していない。**DoD の申告は CI で強制せず(H-252-3)、未記入は 3節の判定 4 種のうち INSUFFICIENT_EVIDENCE として扱い記入を求める(判定語を独自に増やさない)。**3.8節の既存の観点(PRIMARY_DOMAIN / LOCKED_DOMAINS / SHARED_TOUCHED / LOCK_LEVEL / LEVEL_1 の compatibility evidence / SCOPE_EXPANSION)と `LOCK_OMISSION_REVIEW_PASS_ALLOWED = NO`、2節の Human Gate、2.6節の merge 実行者、Production approval、exact ChangeSet approval、レビュー判定 4 種はいずれも変更していない。** 既存節の削除・書き換えは行っていない(純粋な追加)。コード・Production 挙動の変更なし |
| 2026-09-12 | 8節へ `POLICY_AUTHORITY = HUMAN_ONLY` / `RULE_PROPOSAL` / `MEMORY_POLICY_AUTHORITY = NONE` の 3 項を追記し、0節の責務分離表へ `docs/policy_registry.yaml` の 1 行を追加した(Issue #337)。★ **`POLICY_AUTHORITY` は既存規則への識別子付与であり、規則の内容を変更していない**(8節は以前から「管理者が独自の判断でルールを追加・変更してよいという意味ではない」「作業 AI が独自にルールを変える根拠にはならない」と定めていた。参照可能な識別子が無かったため機械からも入口からも指せず、実際に正本外の運用ルールが 4 件課された)。AI が制定してはならないものの列挙・一回限りの指示と恒久ルールの判定質問・単独では恒久規則の正本にならないものの列挙を追加したが、いずれも**既存規則の適用範囲の明示**である。`RULE_PROPOSAL` は 8節が既に要求する Issue 起点の同期(development_workflow.md 9.5節)へ手続きを与えるものであり、★ **新しい承認を追加していない**。発効の形は 2.6.10節と ai_operation_message_contract.md 0節の前例を踏襲し、新方式を作っていない。`MEMORY_POLICY_AUTHORITY` は 8節の適用範囲の明示である(memory は repository の外にあり CI からも review からも見えないため、規範情報を保存するとセッションをまたいで正本と同じ強さで再現する。実例 = 撤回された運用ルールが作業 AI の memory へ「利用者からのフィードバック」として保存されていた)。**承認記録の書式は ai_operation_message_contract.md 8節が正本であり複製していない。****1節の役割定義・2節の Human Gate・2.6節の merge 実行者・3節のレビュー判定 4 種・4節の指示形式・10節の恒久ルールと現在状態の分離はいずれも変更していない。** docs のみの変更であり、コード・Production 挙動の変更なし |
| 2026-09-12 | 3節へ ★ **3.9〜3.14 を新設**し、3.5節へ順序の 1 行を追記した(Issue #333)。レビューが独立していなかった。fresh session であることは独立したレビューを意味せず、最初に Issue の全コメントや PR 本文を一括取得すると ★ **その時点で開発者の結論を読んでしまう**。3.9 で入力の境界(BLIND_FIRST_PHASE_1_ALLOWED / FORBIDDEN)と Phase の定義を、3.10 で設計レビューの 17 観点と traceability を、3.11 でコードレビューの手順を、3.12 で証拠の強度(LEVEL_A / B / C)を、3.13 で反証確認を、3.14 で INDEPENDENT_REVIEW_SNAPSHOT と REVIEW_SESSION_LIFECYCLE を定めた。★ **節番号は末尾へ追加し、既存の 3.6〜3.8 を繰り下げていない**(ai_operation_message_contract.md 2026-09-07 の前例。他文書からの参照を無効にしないため)。★ **判定語を増やしていない**。3節の 4 語をそのまま使い、LEVEL_A/B/C は★ 証拠の強度であって判定語ではないことを明記した。★ **3.6節を置き換えていない**(3.6 = 鮮度 × 検証可能性 / 3.12 = 誰が取得したか。軸が違うため併存)。★ **新しい役割を作っていない**(reviewer session は管理者役割の別インスタンス。1節は不変)。DISCONFIRMING_CHECKS_PERFORMED のみ ★ NONE を認めないのは、「Finding が無かった」と「反証を試みなかった」が別だからである。EVIDENCE_GAPS = NONE の乱用の禁止は ★ 新しい禁止ではなく、3節の「推測で PASS にしない」と issue_label_policy.md 7.4.2 の「未観測を PASS と書かない」の適用である。**1節の役割定義・2節の Human Gate・2.6節の merge 実行者・3節の判定語 4 種・3.5〜3.8節の既存本文・4節の指示形式・8節のルール変更の扱い・10節・11節はいずれも変更していない。** docs のみの変更であり、コード・Production 挙動の変更なし。**3.7節へ 2 項目を追記した**(INDEPENDENT_REVIEW_SNAPSHOT / REVIEW_INPUT_EVIDENCE)。判定語だけを提示すると、その判定を支える根拠が後から作れてしまうため、**判定が何を読んで出されたか**を提示へ残す。**既存 8 項目は 1 文字も変更していない**(追加は末尾のみ)。例にも同じ 2 項目を反映し、本文と例が食い違わないようにした。設計は当初この拡張先を ai_operation_message_contract.md の 3.7節としていたが、**同文書に 3.7節は存在せず**(3節は BASELINE_INVARIANTS)、「merge 判断を支援する提示形式」を持つ節は本書の 3.7節だけであるため、**設計の誤記として MANAGER 判断で訂正した**(項目数も 6 ではなく 8 であった)。**レビューの深さは 3.5節の適用範囲をそのまま使う**(3.9節「適用の深さ」)。全レビューで blind-first の入力境界・primary evidence の独立取得・developer report を読む前の snapshot 固定・証拠の強度の分類を行い、**3.5節の主対象(誤ると取り返しがつかない判断)に該当する場合にだけ** traceability と反証確認(3.13節)を必須とする。該当しない場合は独立取得のみ必須で、traceability と反証確認は省略してよい。**新しい深さの軸も label も判定語も作っていない**(3.5節の既存の境界を参照するだけである)。3.14節の DISCONFIRMING_CHECKS_PERFORMED は **field 自体は全レビューで必須**とし、主対象外では `NOT_APPLICABLE` を認める(欠落にせず、実施対象外であることを明示する)。主対象では NONE も NOT_APPLICABLE も認めない。3.12節の測定の独立性のうち **(b) sanitize 済み生出力は `CURRENTLY_NOT_ACTIVE` とし、本節の発効に含めない**。何を sanitize すれば公開してよいかは repository の公開方針に依存し、それは Issue #334 の判断対象であるため、**本書の merge が提出義務の発効を意味しないようにした**((a)(c)(d) は発効する)。PRELIMINARY_FINDINGS / EVIDENCE_GAPS の必須と NONE の semantics は変更していない |
| 2026-09-12 | 1節へ **REVIEWER(レビュワー)の役割を追加**し、3.9〜3.14節のレビュー実施主体を `MANAGER` と `REVIEWER` へ分離した(Issue #353。#333 から split)。**利用者が role model を変更した**ものであり、旧記録(REVIEWER_ROLE = MANAGER / reviewer session は管理者役割の別インスタンス)が当時誤っていたという意味ではない。旧記録は historical record として残し、本行で新しい決定を記録する。変更の理由は、管理者が「作業計画・指示・進捗管理」と「独立レビュー」を同一役割で兼ねる構造では、**自分が管理した対象の最終 reviewer を自分が務める**ことになり、独立レビューが成立しないためである。実際に role boundary が未確定であることを理由に通常の review lifecycle が進められない状態が生じた。1節へ `MANAGER_AND_REVIEWER = SEPARATE_ROLES` / `SAME_SESSION_DUAL_ROLE = FORBIDDEN` / `REVIEWER_ROLE_SEPARATION = REQUIRED` / `FRESH_REVIEW_SESSION = REQUIRED` / `MANAGER_REVIEWER_ROLE_COMBINATION = FORBIDDEN_FOR_SAME_REVIEW_TARGET` / `REVIEWER != USER` / `REVIEWER_VERDICT != USER_APPROVAL` を置き、MANAGER の禁止へ `MANAGER_REVIEW_CAN_SUBSTITUTE_INDEPENDENT_REVIEW = NO` と「自分が管理した review target の最終 reviewer 兼務」「REVIEWER の finding を自分で消す」「REVIEWER の verdict を自分の判断だけで PASS へ変更する」「REVIEWER を経由しない MANAGER review を独立レビューの代替として扱う」を追加した。**役割を分けることとセッションを分けることは別である**ため、`FRESH_REVIEW_SESSION = REQUIRED` を残している(役割が別でも、開発者の報告や管理者の結論を先に読んでいれば blind-first の独立性は成立しない)。3.9節は manifest の作成者を `PHASE_1_INPUT_MANIFEST_CREATOR = MANAGER` / レビュー実施主体を `PHASE_1_REVIEWER = REVIEWER` とし、制約を `MANAGER != REVIEWER` へ改めた(分ける根拠は**役割の分離**であってセッションの分離ではない)。3.14節の `REVIEW_SESSION_LIFECYCLE` を **10 段階から 12 段階**へ改め、`REVIEW_SESSION_CREATOR = MANAGER` / `REVIEWER_ROLE = REVIEWER` / `PHASE_1_INPUT_PROVIDER = MANAGER` とし、**「新しい役割を作らない。reviewer session は管理者役割の別インスタンスとする」の 2 行を削除**した。3.10 / 3.11 / 3.12 / 3.13節はレビュー実施主体を `REVIEWER` と明示しただけであり(3.10節へ `DESIGN_REVIEW_ACTOR = REVIEWER` / 3.11節へ `CODE_REVIEW_ACTOR = REVIEWER` / 3.13節へ `DISCONFIRMING_REVIEW_ACTOR = REVIEWER` の 1 ブロックずつ、3.12節は `LEVEL_A` の主体語を `REVIEWER` へ)、**17 の観点・traceability・手順・exact diff・surrounding code・PR 本文の必須節の確認・`LEVEL_A` / `LEVEL_B` / `LEVEL_C` の意味・反証確認の REQUIRED / OPTIONAL 条件・レビューの深さはいずれも変更していない**。3節は**題名も判定語 4 種も変更しておらず**、`FINAL_REVIEW_VERDICT_OWNER = REVIEWER` の 1 ブロックを冒頭へ足しただけである。**2節の Human Gate・2.6節の merge 実行者・利用者と開発者の権限・1.5節の Production 担当・8節のルール変更の扱い・10節・11節はいずれも変更していない。**`policy_registry.yaml` は見出し(anchor)が 1 つも変わらないため更新していない。CLAUDE.md は**役割識別子の一覧に `REVIEWER` の 1 行を足しただけ**であり、役割定義の本文も現在の担当も書いていない(定義の正本は本書 1節、担当の正本は `ROLE_ASSIGNMENT_SSOT`)。**本改訂は Issue #353 の activation boundary(12 条件)を満たした時点で発効する**。`CURRENT_POLICY_APPLIES_TO_ITS_OWN_CHANGE = YES` であり、**この改訂自身のレビューは改訂前の規則(管理者役割の fresh な別セッション)で行う**。発効前に「REVIEWER がレビューした」と記録しない。**独立レビュー(現行規則による fresh な管理者セッション)の条件へ対応して次を加えた。**3.10節へ `DESIGN_REVIEW_ACTOR = REVIEWER` / 3.11節へ `CODE_REVIEW_ACTOR = REVIEWER` を置きレビュー実施主体を 3.9節 / 3.13節と同じ形式で明示した。1節へ `MANAGER_REVIEW_CAN_SUBSTITUTE_INDEPENDENT_REVIEW = NO` を識別子として置いた(規範は既にあり、**参照可能な名前が無かった**。2026-09-12 の #337 の行が同じ失敗形を記録している)。3.11節へ「**REVIEWER は 3.8節の観点を独立に確認するが、`LOCK_REVIEW_OWNER` / `DOD_DECLARATION_REVIEW_OWNER` は MANAGER のままである**」を明記した(**3.8節の本文は変更していない**。確認しても ownership は移らない)。3.14節へ `ROLE_SEPARATION_ACTIVE` と `ACTIVATION_STATE_SSOT` を置いた(**発効状態の固定値を本書へ埋め込まず**、Issue #353 の最新の durable な記録を fresh に読む。2.6節の `CURRENT_WIP_RULE` / ai_operation_message_contract.md の `NEW_CONTRACT_ACTIVE` と同じ方式)。3節へ `REVIEW_KIND = MANAGEMENT_REVIEW | INDEPENDENT_REVIEW` を加え、review 結果の durable record では verdict と併記して一意に判別できるようにした。3.14節の snapshot と 3.7節の提示形式へも `REVIEW_KIND` を **追加のみ**で足した(3.7節は利用者へ merge 判断を提示する境界であり、ここで種別が落ちると**最終提示だけを読んだときにどちらのレビューの結論か分からない**ためである。適用と禁止を併記し、既存 8+2 項目の本文・順序・必須性は 1 文字も変えていない。例にも同じ field を足し、本文と例が食い違わないようにした)(**判定語 4 種は不変であり、既存 field も変更していない**。`REVIEW_KIND` は判定語ではない)。1節の MANAGER の責務から「調査・設計・実装結果のレビュー」という**包括表現を外し**、management review(計画のレビュー / scope の確認 / 進捗・実績の確認 / acceptance と Progress Status の管理上の確認)として書き直した。同じ語で independent review まで担うように読めたためである。CLAUDE.md へは 0節の読み分けへ 1 行、2節へ pointer を 1 項だけ足した(**規則本文も現在の担当も書いていない**)。あわせて `REVIEW_SESSION_REUSE_POLICY` を 3.14節へ定めた。`FRESHNESS_REQUIRED_AT = INITIAL_PHASE_1_ONLY` とし、Phase 2・finding 対応後の再レビュー・同一対象への追加 commit の確認は**同じ review session を継続してよい**こととし、別 Issue / 別のレビュー対象 / 無関係な PR / 新しい review lifecycle では**新しい fresh session を開始する**ことを明示した。A〜E の段階・再レビュー時に管理者が渡す 8 項目・`REVIEW_ITERATION` を持つ append-only の記録例・`REVIEW_SESSION_END_CONDITION` の 4 条件を加えている。**12 段階の順序と文言は変更しておらず、A と B がそれを指す**(拡張であって書き換えではない)。**blind-first を弱める変更ではない**。独立性が要るのは「最初の評価を開発者の自己評価より前に形成すること」であり、`INITIAL_INDEPENDENCE`(fresh session + blind な Phase 1)と `CONTINUITY_AFTER_SNAPSHOT`(同じ reviewer session)として**要求する時点を明示した**ものである。あわせて 1節の `FRESH_REVIEW_SESSION = REQUIRED` へ **(初回の blind-first Phase 1 の開始時)** の限定を添え、3.14節の `FRESHNESS_REQUIRED_AT` を正本とする pointer を1節と 3.14節の双方へ置いた。**1節だけを読むと「レビューのたびに fresh session が必要」と読めた**ためである(限定は 3.14節にしかなく、入口の CLAUDE.md は 1節を指している。実際にこの改訂自身の再レビューで「同じ session を使ってよいか」が現行規則から一意に読めず、利用者の判断を要した)。**追加のみであり、既存の文は変更していない。新しい規則も作っていない。**さらに 3.14節の発効ブロックへ `ACTIVE_ROLE_MODEL_SSOT` と `PRE_ACTIVATION_ROLE_MODEL_SSOT` を加えた。**本改訂が main へ入ると旧い役割規則の本文は main から消えるが、`ROLE_SEPARATION_ACTIVE = NO` の間に有効なのは旧いほうである**ため、その期間にどこを読めばよいかが一意に決まらないという指摘(PR #354 の merge を止める finding)への対応である。発効前は Issue #353 の durable な pre-activation 記録が固定した **immutable な base commit の本書 1節 / 3.9〜3.14節**を読む、という pointer だけを置き、**旧い本文を複製していない**(同じ規則が 2 か所にあると正本が分からなくなる)。**base commit の SHA も本書へ書いていない**(変わりうる値は Issue 側の durable record で固定する。`ACTIVATION_STATE_SSOT` / `CURRENT_WIP_RULE` / `NEW_CONTRACT_ACTIVE` と同じ扱いであり、新しい方式を作っていない)。あわせて 1節の末尾へ **どちらの役割規則が現在有効かは 3.14節の `ACTIVE_ROLE_MODEL_SSOT` による**という pointer を 1 行置いた。1節へ直接入った読み手が、**発効前であることに気づかないまま新しい役割規則を有効と読む**経路が残っていたためである(入口の CLAUDE.md は レビュワーへ 1節を読むよう指示している)。**追加のみであり、責務・禁止・識別子・既存の文はいずれも変更していない。**docs のみの変更であり、コード・Production 挙動の変更なし |
| 2026-09-13 | 1節 / 3.9節 / 3.14節の **review session に関する要件を「session の新規作成」から「レビュー対象ごとの入力境界」へ改めた**(Issue #355。#353 で入れた `REVIEW_SESSION_REUSE_POLICY` の設計欠陥)。**利用者の要件は「レビュー対象ごとに新しい session を作ること」ではなく、「REVIEWER の session を Issue をまたいで継続し、対象ごとに blind-first を立て直すこと」であった**。旧規定は `SESSION_REUSE_FORBIDDEN_FOR = DIFFERENT_ISSUE / DIFFERENT_INDEPENDENT_REVIEW_TARGET / UNRELATED_PR / NEW_REVIEW_LIFECYCLE` と定めており、**発効直後に実際に独立レビューが開始できなくなった**(REVIEWER は現行規則を正しく適用して停止した。停止の判断は正しく、規則の側が誤っていた)。1節は `FRESH_REVIEW_SESSION = REQUIRED` を **`TARGET_REVIEW_FRESHNESS = REQUIRED`(レビュー対象ごと)と `SESSION_CREATION_FRESHNESS = NOT_REQUIRED`(原則)へ置き換え**、`REVIEWER_ACTOR` と `REVIEWER_SESSION`(原則 persistent)を分けて、**対象の分離は session を分けることではなく `REVIEW_ID` / `PHASE_1_INPUT_MANIFEST` / `TARGET_FRESHNESS_CHECK` / `INDEPENDENT_REVIEW_SNAPSHOT` で作る**ことを明記した。3.9節へ `TARGET_REVIEW_FRESHNESS` / `SESSION_CREATION_FRESHNESS` の定義を置いた(**新しい session でもその対象の開発者報告を先に読んでいれば成立せず、続いている session でもその対象について読んでいなければ成立する**)。3.14節は `PERSISTENT_REVIEWER_SESSION = YES` とし、`SESSION_REUSE_ALLOWED_FOR` へ **`DIFFERENT_ISSUE` / `DIFFERENT_INDEPENDENT_REVIEW_TARGET` / `UNRELATED_PR` / `NEW_REVIEW_LIFECYCLE` を含め**、**`SESSION_REUSE_FORBIDDEN_FOR` の 4 項目を削除**した。代わりに `TARGET_FRESHNESS_CHECK`(6 step)と `NEW_REVIEW_SESSION_REQUIRED_IF`(A〜E)を識別子つきで置き、**別の Issue / 別の PR / 新しい lifecycle であることだけを理由に新しい session を要求してはならない**と明記した。対象ごとの開始時に `REVIEW_TARGET` / `REVIEW_ID` / `TARGET_FRESHNESS_CHECK` / `PHASE_1_INPUT_MANIFEST` / `CONTAMINATION_CHECK = PASS` を記録する。発効は `PERSISTENT_REVIEWER_SESSION_POLICY_ACTIVE = YES | NO` と `SESSION_POLICY_ACTIVATION_STATE_SSOT = Issue #355 の最新の durable な activation 記録`で表し、**固定値を本書へ埋め込まない**(`ROLE_SEPARATION_ACTIVE` / `CURRENT_WIP_RULE` と同じ方式であり、新しい方式を作っていない)。**blind-first は弱めていない**。Phase 1 は開発者報告の受領前であり、入力は manifest で限定し、disallowed input を読まず、snapshot を先に固定し、Phase 2 で比較し、finding と verdict は REVIEWER が独立して出す。**変えたのは「blind-first の成立に物理的な新規 session 作成が必須」という部分だけである**。`TARGET_FRESHNESS_CHECK` の 4 の範囲は **利用者の判断**により `TARGET_FRESHNESS_SCOPE = BLIND_FIRST_PHASE_1_FORBIDDEN_ONLY` とした(Issue #355 issuecomment-5647258830)。**他の対象を通じて Issue 名や進行状況を偶発的に目にしただけでは contamination として扱わない**(目的は完全な情報遮断ではなく、開発者の自己評価等による anchoring より前に独立した Phase 1 を固定することである。範囲をここまで広げる案は**persistent な session と両立しない**ため採らなかった)。あわせて `PRIOR_EXPOSURE` を **必須**とした(同判断)。**persistent な session では完全な無知状態を前提にしない**ため、Phase 1 の開始時に `NONE` か、`source` / `summary` / `exposure_type` / `BLIND_FIRST_FORBIDDEN_MATCH` / `independence_impact` を記録する。判定の目安(一般 metadata や workflow 情報は原則 freshness を失わない / 開発者の完了報告・自己評価・root-cause 説明・`PASS` 等の結論、およびそれを実質的に転記した管理者の結論は blind-first forbidden の候補)と、**曖昧なら `TARGET_FRESHNESS = UNKNOWN` として管理者へ戻す**ことも本文へ置いた。**role separation・`REVIEW_KIND`・判定語 4 種・レビューの深さ・Phase 1 / Phase 2 の構造・Human Gate・利用者の権限・`MANAGER_REVIEW_CAN_SUBSTITUTE_INDEPENDENT_REVIEW = NO`・finding remediation の流れ・Production gate・`ACTIVE_ROLE_MODEL_SSOT` の分岐・12 段階の順序はいずれも変更していない。** `policy_registry.yaml` は見出し(anchor)が変わらないため更新していない。CLAUDE.md も変更していない(入口からの到達は既存の pointer で成立する)。**本改訂は Issue #355 の activation 記録をもって発効する**。独立レビュー(iteration 1)の finding へ対応して次を加えた。3.14節の発効ブロックへ `ACTIVE_SESSION_POLICY_SSOT` と `PRE_ACTIVATION_SESSION_POLICY_SSOT` を置いた(F1 / HIGH)。**本改訂が main へ入ると旧い session 規則の本文は main から消えるが、`PERSISTENT_REVIEWER_SESSION_POLICY_ACTIVE = NO` の間に有効なのは旧いほうである**ため、その期間にどこを読めばよいかが一意に決まらなかった。発効前は Issue #355 の durable な pre-activation 記録が固定した **immutable な base commit の本書 1節 / 3.9節 / 3.14節の session 規則**を読む、という pointer だけを置き、**旧い本文を複製していない**。**base commit の SHA も本書へ書いていない**(変わりうる値は Issue 側の durable record で固定する。`ACTIVE_ROLE_MODEL_SSOT` / `ACTIVATION_STATE_SSOT` / `CURRENT_WIP_RULE` と同じ扱いであり、新しい方式を作っていない)。あわせて 1節の末尾へ**どちらの session 規則が現在有効かは 3.14節の `ACTIVE_SESSION_POLICY_SSOT` による**という pointer を置いた(1節へ直接入った読み手が、発効前であることに気づかないまま新しい session 規則を有効と読む経路が残っていたためである)。新設側の識別子は `ACTIVATION_STATE_SSOT` から **`SESSION_POLICY_ACTIVATION_STATE_SSOT` へ改名**した(F2 / MEDIUM。同一文書内で同じ名前が #353 と #355 の 2 値へ束縛され、限定なしの参照がどちらを指すか一意に読めなかった。**#353 側の `ACTIVATION_STATE_SSOT` は変更していない**)。`TARGET_FRESHNESS_CHECK`(PASS | UNKNOWN)と `CONTAMINATION_CHECK`(PASS | FAIL)を3.14節の **snapshot の必須 field** へ追加し、`PHASE_1_COMPLETE` の条件へ**両者が埋まっていること**を明示した(F3 / MEDIUM。利用者判断は両者を「各 target 開始時に記録」と定めていたが記録先が無く、**未記録のまま Phase 1 完了が成立した**。判定だけを先に置いて根拠を後から作れる状態を防ぐという 3.14節の目的に反する。`PRIOR_EXPOSURE` について適用した是正原則を他の 2 項目へ広げたものである)。対象の一意性は `REVIEW_TARGET_REF`(CODE = Issue 番号 + BASE / HEAD、DESIGN = artifact の URL)として**`REVIEW_TARGET`(種別)と分け**、`TARGET_FRESHNESS_CHECK` の 1 と開始時の記録を同 field へ改めた(F4。**`REVIEW_TARGET` の定義と値域は変更していない**)。無置換で削除されていた `REVIEW_TARGET_LIFECYCLE` は**同一 `REVIEW_ID` の初回 Phase 1 から E TERMINATION までの範囲**として定義を置き直した(**旧規定の session 制限は復活させない**。範囲を指す語だけを戻す)。「この規則が埋めないもの」へ、`TARGET_FRESHNESS_CHECK` / `CONTAMINATION_CHECK` / `PRIOR_EXPOSURE` が**いずれも自己申告であり「読んでいない」ことは検査できない**(物理的な新規 session を要求しないため session 境界による構造的な保証が無い。記録することで事後に検査できる形にするだけである)を追記した(F5)。`TARGET_FRESHNESS = UNKNOWN` を管理者へ戻した後の経路を、(a) `BLIND_FIRST_PHASE_1_FORBIDDEN` に当たる場合は `NEW_REVIEW_SESSION_REQUIRED_IF` の A / (b) 当たらない場合は `PRIOR_EXPOSURE` へ記録して同 session 継続 / (c) 管理者でも一意に判定できない場合は利用者判断(8節 `POLICY_AUTHORITY = HUMAN_ONLY`)として定義した(F6。**新しい判定基準を作らず既存の A〜E と `PRIOR_EXPOSURE` へ接続するだけであり**、管理者が単独で freshness の成立を宣言しないことを明示した)。**いずれも追加であり、role separation・`REVIEW_KIND`・判定語 4 種・12 段階の順序・Human Gate・利用者の権限・`ACTIVE_ROLE_MODEL_SSOT` の分岐・`TARGET_FRESHNESS_SCOPE`・`SESSION_REUSE_ALLOWED_FOR`・`NEW_REVIEW_SESSION_REQUIRED_IF` の A〜E はいずれも変更していない。**docs のみの変更であり、コード・Production 挙動の変更なし |
