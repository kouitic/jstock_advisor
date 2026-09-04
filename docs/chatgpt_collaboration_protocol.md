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
| [docs/issue_label_policy.md](issue_label_policy.md) | Issue 分類 / Priority / Severity / release-blocker |
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
```

`E` は特に壊れやすい。外側のブロックの中でコードフェンスを開くと、
そこで外側のブロックが閉じてしまい、以降がコードブロックの外へ出る。
結果として **C に違反した状態が意図せず発生する**。

指示の中でコード例・コマンド・設定値を示したい場合は、
フェンスを使わず**字下げ**で表現する。

### 例

```
良い例  指示全文が1つのブロックに収まり、内部の例示は字下げ

    INSTRUCTION_ID = TARO-20260904-00X
    ASSIGNEE       = TARO

    1. 作業内容
       次のコマンドで確認する

           pytest tests/unit/test_example.py -q

       期待値
           passed

    2. 完了報告
       ...

悪い例  内部でフェンスを開いてしまう
        -> そこで外側ブロックが閉じ、以降が地の文になる
        -> ユーザーが「ブロックだけ」をコピーすると後半が欠落する
```

### 例外

指示ではない通常の会話・レビュー結果・相談は、この形式に縛られない。
本節が対象とするのは、**そのまま作業 AI へ転送される指示文**である。

作業 AI からユーザーへ返す長文報告も、同じ理由から
1つのコードブロックへまとめることが望ましい(こちらは推奨であり、
転送を前提としないため必須ではない)。

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

### 例外

`EMERGENCY=YES` の差し替えのみ(4節)。

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
実装の進め方 / lane / WIP 制限 / テスト方針  -> development_workflow.md
Issue の分類と label                          -> issue_label_policy.md
Production の具体的な運用手順                 -> operations_manual.md
利用者から見た機能仕様                        -> functional_spec.md
```

本文書を厚くしすぎない。
迷ったら「これは誰が誰へどう伝えるかの話か」を基準に判断する。
そうでなければ他文書が正本である。

---

## 変更履歴

| 日付 | 変更内容 |
|---|---|
| 2026-09-04 | 新規作成(Issue #122)。ユーザー ↔ ChatGPT 間の協働ルールを、チャット履歴・AI の記憶に依存させず GitHub 上の SSoT として管理するための文書。役割分担(承認はユーザー / 推奨は ChatGPT)、Human Gate の一覧と「承認はその操作・その対象に限る」原則、レビュー判定4種と `INSUFFICIENT_EVIDENCE ≠ REJECT` / `PASS_WITH_CONDITIONS ≠ Human Gate 通過` の区別、指示プロトコル(`INSTRUCTION_ID` / 作業者ごとの直列化 / 緊急差し替え)を指示側から見た運用として整理、4.5節「指示文の出力形式」(指示はユーザーが手作業で転送するため、全文を1つの外側コードブロックへ収め、前後へ分散させず、内部でコードフェンスをネストしない。例示は字下げで表現する。内容が正しくても届いた時点で壊れるという失敗を出力形式の側で防ぐ)、新規指示前の確認手順、回答の correlation と例外的な `MANUAL_CORRELATION`、1指示1回答、ルール変更時の `LATEST_EXPLICIT_HUMAN_DECISION_WINS_TEMPORARILY` と同期義務(優先を認めるのはユーザーが明示的に確定した判断に限り、ChatGPT や作業 AI が独自にルールを追加・変更する根拠にはしない)、セッション開始時の明示的 bootstrap(自動読み込みを前提にしない)、恒久ルールと dynamic state の分離、公開リポジトリでの取り扱いを記載した。**既存 governance の要求はいずれも緩和していない**(merge / Production 操作 / release-blocker 解除の人間承認、`PER_WORKER_SERIALIZATION=YES` / `GLOBAL_SERIALIZATION=NO` の区別を含む)。コード・Production 挙動の変更なし |
