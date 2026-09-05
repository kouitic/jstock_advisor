# ユーザー ↔ ChatGPT 協働プロトコル

この文書は、**リポジトリの所有者(以下ユーザー)と ChatGPT** が、
複数の作業 AI(太郎 / 次郎)を使って開発を進めるときの
**会話・意思決定・レビュー・作業指示・承認の進め方**を定める。

```
USER_CHATGPT_COLLABORATION_SSoT
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
| **本文書** | **ユーザー ↔ ChatGPT 間の会話・意思決定・レビュー・作業指示・Human Gate の運用** |

同じルールが複数箇所にあると必ず片方が古くなる。
本文書は「誰が何を決めるか」「どう指示し、どう受け取るか」に限定する。

---

## 1. 役割分担

### 目的

「推奨」と「決定」を取り違えないため。
ChatGPT がどれだけ確信を持って推奨しても、それは承認ではない。

### ルール

**USER(ユーザー)**

```
要件・目的の決定
優先順位の最終判断
Human Gate の承認
Production 変更の承認
merge の承認
release-blocker 解除の承認
業務仕様上の最終意思決定
```

**ChatGPT**

```
作業計画の作成
優先順位の整理
調査・設計・実装結果のレビュー
作業 AI(太郎 / 次郎)への作業指示
instruction queue の管理
Human Gate へ到達したかどうかの判定
release / verification の判定
Issue / PR / Production evidence の整合確認
```

**TARO / JIRO(作業 AI)**

```
ChatGPT の指示に基づく具体作業
調査 / 設計 / 実装 / テスト
GitHub 操作
承認済み範囲の Production 作業
evidence の収集
```

### 例

```
ChatGPT   「MERGE_READY=YES。人間承認へ進んでよい」
          -> これは推奨であって承認ではない。merge してはいけない

USER      「PR #146 の SHA 9f4dac0e を merge してよい」
          -> ここで初めて merge できる
```

### 例外

なし。ChatGPT がユーザーの承認を代行することはない。

---

## 1.5 Production deploy 関連作業の担当

### 目的

Production への deploy 工程は、**手順の途中で担当が入れ替わると
前提が引き継がれない**。承認対象の exact SHA、承認対象の exact ChangeSet、
build 済み artifact の同一性といった前提は、一連の作業として保持される必要がある。

そこで実作業の担当を1体へ集約する。

```
PRODUCTION_DEPLOYMENT_EXECUTOR = TARO
```

### ルール

太郎が担当する範囲は最低限次を含む。

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

次郎は調査・設計・実装・targeted test・PR 作成・release readiness 調査・
Production Verification Plan の設計・Production evidence の read-only 分析まで
担当できるが、**deploy の実作業は既定で行わない**。

```
DEPLOY_OPERATION_DELEGATION_TO_JIRO = FORBIDDEN_BY_DEFAULT

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

太郎が deploy 担当であることは、
**人間承認なしに実行してよいという意味には一切ならない**(2節)。

### verification の担当は内容で分ける

deploy 直後の immediate verification は太郎が担当する。

一方、自然実行後の**業務的な** Production evidence 分析は、
内容に応じて ChatGPT が割り当ててよい。

```
運用寄り(stack / Lambda / IAM / scheduler / logs)   -> 太郎を優先
業務ロジック寄り(分類比較 / スコア分布 / 業務判断)   -> 次郎へ read-only 分析を割当可
```

```
PRODUCTION_DEPLOYMENT_EXECUTOR = TARO
  ≠ ALL_PRODUCTION_ANALYSIS_ASSIGNEE = TARO
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
E  ChatGPT の推奨
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

本節が対象とするのは **ChatGPT からユーザーへの回答**である。

```
対象      ChatGPT -> ユーザー
対象外    作業 AI -> ChatGPT / ユーザーへの完了報告
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
PROPOSED   ChatGPT または作業 AI が提案・推奨しただけ
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
2  ChatGPT が PR をレビューする
3  ChatGPT が MERGE_READY 判定を提示する
4  ユーザーが GitHub 上で直接 merge する
5  ChatGPT が必要に応じて read-only の post-merge 確認を行う
6  main CI を確認する
7  Production は別の Human Gate
```

通常は作業 AI へ「merge してください」という作業指示を出さない。

ユーザーが ChatGPT へ事前に「merge を承認します」と宣言することは
**必須ではない**。ChatGPT が `MERGE_READY` を提示したうえで
ユーザー自身が GitHub で merge を実行した場合、

```
USER_MERGE_ACTION = HUMAN_APPROVAL + EXECUTION
```

として扱ってよい。承認と実行が同一の操作で成立している。

### 例外

ユーザーが明示的に「今回は作業 AI に merge させる」と決めた場合は、
8節の `LATEST_EXPLICIT_HUMAN_DECISION_WINS_TEMPORARILY` に従う。

ChatGPT が `MERGE_READY` を出していない PR をユーザーが merge した場合でも、
それは人間による実行であるから、**勝手に revert / rollback しない**。
必要なら post-merge review で状態と影響を確認する。

**PR を merge したことを Production の承認として扱ってはならない。**
Production deploy は本節とは別の Human Gate である(2節)。

---

## 3. ChatGPT のレビュー判定

### 目的

「証拠が足りない」と「不合格」は別物である。
これを混ぜると、確認不足のまま次へ進むか、逆に問題のない作業を止めることになる。

### ルール

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

重要 Gate では、ChatGPT が read-only で確認できる情報を、
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

ChatGPT が PR レビュー結果を提示する場合、
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
```

```
残課題があっても今回の merge を妨げない   -> MERGE_BLOCKING_CONCERN = NO
merge 前に修正が必要                      -> MERGE_BLOCKING_CONCERN = YES
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
```

ユーザーはこれを見て GitHub 上で merge を判断できる。

### 例外

**この形式を Markdown の表へ固定しない。**
ChatGPT の通常の回答として読みやすく提示できればよく、
項目が揃っていることが要件である。

なお、この提示自体は `PROPOSED` にとどまる(2.5節)。
`MERGE_READY = YES` は承認でも実行でもない。

---

## 3.8 機能領域 WIP のレビュー観点

### 目的

領域ベースの WIP モデル(development_workflow.md 2.6節)では、作業者が
**自分で** 触る領域と `LOCK_LEVEL` を判定する。この判定の誤りは CI では
検出できない。「本来は買い判定の領域も lock すべきだったのに、保有判断の
領域だけで進めた」という誤りは、レビューでしか気づけない。

### ルール

`CHATGPT_LOCK_REVIEW_OWNER = CHATGPT`

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

`LOCKED_DOMAINS` に漏れがあった場合は `REJECT` ではなく、まず
**何が漏れているか(どの参照元がどの領域に属するか)を具体的に示す。**
作業者が実測をやり直せる形で返す。

### 適用範囲

```
対象     作業 AI の実装レビューにおける領域・lock の妥当性確認
対象外   領域・機能・共通部品の一覧そのもの
         -> docs/functional_domains.md が正本
対象外   WIP の取得・解放・割り込み・main 追随のルール本文
         -> development_workflow.md 2.6節が正本
```

```
本節は development_workflow.md 2.6節の発効(DOMAIN_WIP_MODEL_ACTIVE = YES)を
もって適用を開始する。それまでは確認義務を課さない。
```

### 例外

`LOCKED_DOMAINS` の確認は Human Gate を増やすものではない。**2節の
Human Gate、2.6節の merge 実行者、Production approval、exact ChangeSet
approval はいずれも変更しない。**

---

## 4. 作業 AI への指示プロトコル

### 目的

複数の AI が並行作業するとき、どの指示に対する回答なのかが曖昧だと、
**古い指示への回答を根拠に次工程へ進んでしまう**。

### ルール(ユーザー ↔ ChatGPT から見た運用)

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
採番は ChatGPT が行うため、その規則をここに置く。

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
実行完了 / CANCELLED / SUPERSEDED / 途中停止 / 作業開始後の取消
```

### 作業 AI へ渡す前の下書き

ChatGPT の内部で作成しただけで、まだ作業 AI へ提示していない下書きは、
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

## 4.5 ChatGPT から作業 AI への指示文の出力形式

### 目的

ChatGPT が作成した作業指示は、**ユーザーが手作業でコピーして
太郎 / 次郎へ転送する**。つまり指示文はそのまま転送される前提の成果物である。

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
A  ChatGPT -> TARO / JIRO の作業指示は、ユーザーが一括コピーして
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
ChatGPT の回答

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
対象      ChatGPT -> TARO の作業指示
          ChatGPT -> JIRO の作業指示

対象外    ユーザーへの通常の説明・レビュー結果・相談
          (すべての回答をコードブロック化するルールではない)

対象外    TARO / JIRO -> ChatGPT の作業報告
          回答側の形式は development_workflow.md 2.5節等の既存ルールを維持する
```

### 例外

本節が定めるのは**指示内容そのもの**ではなく、
**ChatGPT が指示をどの形式でユーザーへ提示するか**である。
指示の中身に関する既存ルール(4節・5節・7節)はいずれも変更しない。

---

## 5. ChatGPT が新しい指示を出す前の確認

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
CHATGPT_ASSIGNMENT_READ_BARRIER_OWNER = YES
```

### ルール

新しい Issue または別 Phase へ作業 AI を割り当てる前に、
[development_workflow.md](development_workflow.md) 6.5節の
**Assignment Read Barrier** を実行する。

```
ChatGPT は、記憶・会話要約・古い Issue 記述だけを根拠に
新規 implementation を指示してはならない。
```

確認項目・applicability(N/A 条件)・`ASSIGNMENT_BASELINE` の形式・
`ISSUE_STATE_SNAPSHOT` の contract・freshness gate・P0 例外はいずれも
**development_workflow.md 6.5節が正本**である。本文書へ複製しない。

本文書が定めるのは、**それを誰が実行するか**だけである。

```
read barrier の実行            ChatGPT
state の書き戻し               state を変えた actor(作業 AI / ChatGPT / ユーザー)
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
CHATGPT_PRIORITY_READ_OWNER            = CHATGPT
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
NEXT_CHATGPT_GATE_OWNS_RECONCILIATION = YES
```

ユーザーによる merge・label 変更・Issue 操作の後、**次の ChatGPT gate が
reconciliation の確認責任を持つ。** ChatGPT 自身が durable comment を残しても、
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
ChatGPT が内容の一致を確認できた場合に限り、
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

ChatGPT  「前回答へのレビュー」と「別 worker への新規指示」を
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

### 例

```
ユーザーが明示的に、新しい判定語を導入すると決定した
  -> 最新の明示的な Human decision として、その場では適用する
  -> 恒久ルールなら Issue を起点に本文書または該当 SSoT へ同期する
  -> 一時的な運用なら文書化しない
```

この節が優先を認めるのは、**ユーザーが明示的に確定した判断**に限る。
`ChatGPT が独自の判断でルールを追加・変更してよい`という意味ではない。
判定語について言えば、3節の4判定は恒久ルールであり、
**ChatGPT が独自に判定語を増やすことは本節の対象外である**(3節の例外なしを維持)。

### 例外

一時的・その作業限りの取り決めは文書化しない。
恒久ルールと一時的判断を区別することがこの節の要点である。

作業 AI(太郎 / 次郎)についても同様であり、本節は
「ユーザーの明示判断が文書より新しい場合の優先順位」を定めるものであって、
作業 AI が独自にルールを変える根拠にはならない。

---

## 9. セッション開始時の bootstrap

### 目的

**「GitHub に置けば ChatGPT が自動的に常時読み込む」という前提は成り立たない。**
ChatGPT はリポジトリを勝手に読まない。明示的に読ませる必要がある。

### ルール

新しいチャット / セッションを開始するとき、
または大きな開発作業を始めるときは、可能な限り最初に次を確認する。

```
1  docs/chatgpt_collaboration_protocol.md   (本文書)
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
| 2026-09-06 | 3.8節「機能領域 WIP のレビュー観点」を新設(Issue #177)。development_workflow.md 2.6節の領域ベース WIP モデルでは、作業者が自分で触る領域と `LOCK_LEVEL` を判定するが、**その判定の誤りは CI では検出できない**。「本来は買い判定の領域も lock すべきだったのに保有判断の領域だけで進めた」という誤りはレビューでしか気づけないため、`CHATGPT_LOCK_REVIEW_OWNER = CHATGPT` として、実装レビューの観点へ `PRIMARY_DOMAIN` / `LOCKED_DOMAINS` / `SHARED_TOUCHED` の網羅性 / `LOCK_LEVEL` の妥当性 / LEVEL_1 の compatibility evidence / scope 拡大の有無を追加した(人間承認 H5)。判定できない場合は 3節の `INSUFFICIENT_EVIDENCE` とし、「追加だけの diff に見えるから LEVEL_1 でよい」と推測で通さない。`LOCKED_DOMAINS` の漏れは `REJECT` ではなく、どの参照元がどの領域に属するかを具体的に示して作業者が実測をやり直せる形で返す。**領域・機能・共通部品の一覧は functional_domains.md、WIP ルール本文は development_workflow.md 2.6節が正本であり本文書へ複製していない。** 本節は 2.6節の発効(`DOMAIN_WIP_MODEL_ACTIVE = YES`)をもって適用を開始し、それまでは確認義務を課さない。あわせて 12節へ正本の所在を 2 行追加した。**2節の Human Gate、2.6節の merge 実行者、Production approval、exact ChangeSet approval はいずれも変更していない。** コード・Production 挙動の変更なし |
