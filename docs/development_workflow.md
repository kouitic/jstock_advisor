# 開発ワークフロー・運用ガバナンス

**この文書の位置づけ**

本リポジトリにおける開発の進め方(lane / WIP / 実装パイプライン / テスト方針 /
記録先 / 検証 / release 判断 / 人間承認の境界)の**正本(SSoT)**である。

AI 非依存のリポジトリ運用ポリシーであり、特定の AI エージェントやセッションに
依存しない。[CLAUDE.md](../CLAUDE.md) は本文書への入口にすぎない。

label の 4 軸モデル(Issue Type / Priority / Release Blocker /
Progress Status)そのものは [docs/issue_label_policy.md](issue_label_policy.md) が
正本である。本文書は label の意味を定義せず、**release-blocker の lifecycle** と
**Priority を再評価すべき時点**についてのみ同文書と接続する。

Production の具体的な運用手順(deploy 手順・テーブル追加時の注意・
read-only 観測での副作用確認等)は [docs/operations_manual.md](operations_manual.md)
が正本である。本文書はそれらの手順を複製しない。

機能領域・機能・共通部品の一覧は
[docs/functional_domains.md](functional_domains.md) が正本である。本文書は
2.6 節でその一覧を用いた WIP 運用**ルール**を定め、一覧そのものを複製しない。

作業報告・Human Gate 提示・Instruction の許可範囲といった**メッセージの形式**は
[docs/ai_operation_message_contract.md](ai_operation_message_contract.md) が
正本である(作成時点で `NEW_CONTRACT_ACTIVE = NO`)。本文書は作業の可否と
state の扱いを定め、形式を複製しない。

**ルールを変更する場合は本文書を更新する。**

---

## 0. 現在のモード: Stabilization Sprint

2026-09-02 より、開発運用を **STABILIZATION_SPRINT / PIPELINE_MODE** とする
(Issue #122、人間承認済み)。

```
期間            約10営業日
新機能          原則停止
P2 / P3 修正    原則停止
P0              即対応
P1              最優先で着手
queue reorder   原則1日1回
割込み          P0 のみ即割込み可
```

安定化を最優先し、既存の未解消 P0/P1 を短期で殲滅することを目的とする。
Sprint 終了後は 1 節以降の lane 定義のみを残し、「P2/P3 原則停止」は解除する。

---

## 1. Lane(A)

Issue を次の 4 lane へ割り当てる。上位 lane を優先する。

| Lane | 対象 |
|---|---|
| **Lane0** | Production incident / P0 |
| **Lane1** | P1 の `bug` / `design-defect` |
| **Lane2** | Production Verification Wait(実装・deploy 済みで検証待ち) |
| **Lane3** | P2 / P3 / `enhancement` |

- **P0 は即時割込み可。** 他 lane の作業中でも中断して着手してよい。
- Lane2 の Issue は**新規実装候補から除外する**(実装は完了しているため)。
- queue の並び替えは原則 1 日 1 回に限る(頻繁な組み替えで着手が分散するのを防ぐ)。
  P0 の割込みはこの制限の対象外。

Issue の実装状況は次の区分で表現する。

| 区分 | 意味 |
|---|---|
| `CODE_WORK_REQUIRED` | 実装が必要。implementation WIP の対象 |
| `PRODUCTION_VERIFICATION_ONLY` | 実装・deploy 済み。検証の完了待ち |
| `HUMAN_DECISION_BLOCKED` | 人間の判断・外部前提の確定待ち |
| `ALREADY_IMPLEMENTED` | 対応済み。close 判断のみ残る |
| `NOT_READY` | 依存 Issue の未完了等により着手できない |

---

## 2. WIP 制限(B)

AI エージェント 1 体あたり:

```
IMPLEMENTATION_WIP <= 1
INVESTIGATION_WIP  <= 2
```

- **同一 Issue の parallel implementation を禁止する。**
  1 つの Issue に対する implementation owner は常に 1 体
  (`one Issue / one implementation owner`)。
  他エージェントが担当する Issue の branch / PR を操作しない。
- **PR CI 待ち / review 待ち / Production verification 待ちは、
  implementation WIP から解放してよい。** これらの待ち時間を
  AI の待機理由にしない。次の Issue へ進む。
- **ただし同一 Issue へ修正が必要になった場合は、最優先で戻る。**
  review 指摘・CI 失敗・verification 失敗が発生したら、
  新しく着手した作業より優先して対応する。


**機能領域ベースの WIP モデル(`DOMAIN_WIP_RULE_V1`)は 2.6 節が正本であり、
既に発効している。** 本節の `MAX_CONCURRENT_CODE_WIP_PER_WORKER = 1` は
2.6 節の R5 として維持されるが、着手可否の判定は 2.6 節による。

---

## 2.5 指示プロトコル(B')

複数の AI エージェントへ並行して作業を依頼する際、**どの指示に対する回答か**が
曖昧になると、古い指示への回答を次工程の根拠にしてしまう事故が起きる。
本節はその防止のための統制である。

### 2.5.1 INSTRUCTION_ID

指示側から各エージェントへ出す**すべての作業指示に一意な ID を付与する**。

```
形式   <ASSIGNEE>-<YYYYMMDD>-<連番>
例     TARO-20260904-001
       JIRO-20260904-001
```

連番の採番規則(日本時間基準・作業者ごと・日付が変わったら 001 へリセット・
同一日での再利用禁止)の正本は
[chatgpt_collaboration_protocol.md](chatgpt_collaboration_protocol.md) 4.1節。
採番するのは指示側であるため、本文書へは複製しない。

作業者は**回答時に、対応した INSTRUCTION_ID を必ず明記する**。

回答が次のいずれかに該当する場合、その回答を**自動的には次工程の根拠にしない**。
まず指示と回答の対応関係を確認する。

```
INSTRUCTION_ID が無い
別の ID が付いている
WITHDRAWN 済みの ID への回答である
```

### 2.5.2 指示の状態

各 INSTRUCTION_ID を最低限次の 3 状態で管理する。

```
PENDING     指示済み、回答未受領
ANSWERED    回答を受領した
WITHDRAWN   撤回済み(緊急差し替え等)。以後この ID への回答は無効
```

### 2.5.3 直列化は「作業者ごと」— 全体を止めない

```
PER_WORKER_SERIALIZATION = YES
GLOBAL_SERIALIZATION     = NO
```

**同一作業者**について、直前の通常指示が `PENDING` の間は次の通常指示を追加しない。
新しい通常指示を出す前に、その作業者の直前指示が `ANSWERED` または `WITHDRAWN`
であることを確認する。

**他の作業者の `PENDING` は、新規指示の発行を妨げない。** キューは作業者ごとに独立である。

```
誤り   「太郎が作業中なら次郎にも指示できない」
正しい 「太郎が PENDING なら太郎への次の通常指示を出さない。次郎へは出してよい」
```

#### 例 A — 別 worker への並行指示は可

```
太郎   TARO-20260904-001 = PENDING
次郎   JIRO-20260904-001 = ANSWERED

-> JIRO-20260904-002 を次郎へ発行してよい
-> TARO-20260904-002 を太郎へ発行してはいけない
   (太郎の 001 が ANSWERED / WITHDRAWN になるまで待つ)
```

#### 領域ベース WIP とは別概念

```
PER_WORKER_INSTRUCTION_SERIALIZATION != PER_WORKER_CODE_WIP_MODEL
ORTHOGONAL = YES
```

本節の直列化と、2.6節の `DOMAIN_WIP_RULE_V1` は目的が異なる。

```
本節(指示の直列化)  人間による転送の安全性と、回答の対応付けを守る
                    制御するのは「同一作業者へ同時に relay する指示の本数」

2.6節(code WIP)     同時に壊れる範囲と semantic conflict を防ぐ
                    制御するのは「同時に保持できる code WIP と領域 lock」
```

**片方が空いても、もう片方の制約は解除されない。**

```
誤り  「その領域は空いているから、次の指示を出してよい」
      -> 領域が空いていても、その作業者の直前の指示が PENDING なら出さない

誤り  「1 人 1 code WIP だから、指示も 1 本しか出していないはず」
      -> read-only の調査指示は code WIP を消費しないが、
         relay された指示としては PENDING である
```

---

### 2.5.4 緊急差し替え(EMERGENCY SUPERSEDE)

緊急時に限り、同一作業者の `PENDING` 指示が存在していても新指示へ差し替えてよい。
その場合、新指示に次を**必ず明記する**。

```
EMERGENCY                   = YES
SUPERSEDES                  = <旧 INSTRUCTION_ID>
PREVIOUS_INSTRUCTION_STATUS = WITHDRAWN
```

旧指示はその時点で無効になる。**その後に旧指示への遅延回答が届いても、
有効な完了報告として扱わない。**

#### 例 B — 緊急差し替え

```
太郎   TARO-20260904-003 = PENDING(Production 自然検証の待機中)

Production で新たな障害を検知したため差し替える:

INSTRUCTION_ID              = TARO-20260904-004
EMERGENCY                   = YES
SUPERSEDES                  = TARO-20260904-003
PREVIOUS_INSTRUCTION_STATUS = WITHDRAWN

-> 以後 TARO-20260904-003 への回答が届いても完了報告として扱わない
-> 太郎は TARO-20260904-004 に対してのみ回答する
```

### 2.5.5 回答フォーマット

作業報告の**冒頭**に最低限次を記載する。

```
INSTRUCTION_ID     = <対応した ID>
ASSIGNEE           = <TARO | JIRO>
INSTRUCTION_STATUS = ANSWERED
```

**複数の古い指示の結果を 1 つの回答へ混在させない。**
1 指示 1 回答を原則とし、対応関係を一意に保つ。

冒頭 3 行より後の**報告の構造**(NORMAL / FORENSIC の切替、
`BASELINE_INVARIANTS`、一括コピー可能性、Instruction の
`AUTHORIZED_PHASES`)は
[docs/ai_operation_message_contract.md](ai_operation_message_contract.md) が
正本である。本節はその冒頭 3 行のみを定め、詳細を複製しない。

```
NEW_CONTRACT_ACTIVE = NO
```

同文書は作成時点で発効していない。発効までは本節の 3 行のみが必須であり、
それ以外の報告構造は現行運用のままとする。

### 2.5.6 適用範囲

本節は **AI エージェントへの作業指示の統制**であり、Production release 手順そのもの
ではない。9 節(grouped release)・10 節(人間承認の境界)の要求を緩和しない。
とくに merge / Production deploy / ChangeSet / manual invoke 等の人間承認は、
INSTRUCTION_ID の有無にかかわらず従来どおり必要である。

---

## 2.6 機能領域ベースの WIP モデル(DOMAIN_WIP_RULE_V1)

```
CURRENT_WIP_RULE = DOMAIN_WIP_RULE_V1
EFFECTIVE_FROM   = 2026-09-06 02:27 JST(2026-09-05T17:27:01Z)
```

**本節は既に発効している。** 発効の手順と、発効時点で進行中だった作業の
扱いは 2.6.10 に定める。

```
ACTIVATION_STATE_SSOT = Issue #177 の最新の durable な activation 記録
```

発効状態は運用の中で変わりうる(試行の結果、人間の判断で 2 節へ戻すことも
ありうる。2.6.10)。**本書のような静的な文書を、変わりうる状態の唯一の
根拠にしない。** 現在の発効状態を確認する必要がある場合は、Issue #177 の
最新の durable な activation 記録を fresh に読む(6.5.4 の read barrier と
同じ原則)。上記の値は本節を改訂した時点のものである。

領域・機能・共通部品の一覧は
[docs/functional_domains.md](functional_domains.md) が正本であり、
本節へ複製しない。本節は**ルール**を、同文書は**判定材料**を持つ。

### 2.6.1 目的

2 節の担当者単位 WIP は「同時に壊れる範囲の最小化」には有効だが、
互いに無関係な機能領域まで直列化する。一方、単純に並行数を増やすと
Git では検出できない衝突が起きる。

```
衝突の型 1  同じファイルを同時に編集する         -> Git が検出できる
衝突の型 2  同じ判定契約・永続契約を同時に変える   -> Git は検出できない
衝突の型 3  共通 module 経由で他領域が壊れる      -> PR 単体では見落とす
```

本節は型 2・型 3 を防ぐため、並行可否を**ファイルの重複ではなく
機能領域の重複**で判定する。

### 2.6.2 基本規則

```
R1  ONE_ACTIVE_CODE_WIP_PER_FUNCTIONAL_DOMAIN
    1 つの機能領域で同時に進行できる code WIP は 1 件

R2  異なる領域であれば、異なる作業者が並行して code WIP を持てる

R3  1 つの Issue が複数領域を触る場合、触るすべての領域の code WIP を
    同時に取得する。どれか 1 つでも取れなければ着手しない

R4  SHARED を触る場合は LOCK_LEVEL(2.6.5)に従って関係領域を取得する

R5  MAX_CONCURRENT_CODE_WIP_PER_WORKER = 1
    領域が空いていても、1 人が同時に 2 件の実装を持つことは許さない

R6  read-only の調査・設計・レビュー・state 同期は code WIP を消費しない
```

R5 は 2 節の意図(同時に壊れる範囲の最小化)を維持するためである。
並行度は**作業者を増やすこと**で得るものであり、1 人の掛け持ちでは得ない。

### 2.6.3 着手可否の最終ゲート

```
CODE_WIP_ACQUIRE_ALLOWED =
      DOMAIN_WIP_AVAILABLE
  AND FILE_OVERLAP_ACCEPTABLE
  AND SHARED_CONTRACT_OVERLAP_ACCEPTABLE
  AND DEPENDENCY_ORDER_CLEAR
  AND BASE_MAIN_COMPATIBLE
```

```
DOMAIN_WIP_AVAILABLE
  触るすべての領域について、他の作業者が code WIP を保持していないこと。
  保持状況は 2.6.9 の SSoT を fresh に読んで確認する(記憶で判断しない)

FILE_OVERLAP_ACCEPTABLE
  進行中の他 WIP の変更予定ファイルと重ならないこと。
  1 つでも重なる見込みがあれば OVERLAP_DETECTED = YES として着手しない。
  「たぶん大丈夫」で進めない

SHARED_CONTRACT_OVERLAP_ACCEPTABLE
  進行中の他 WIP が LOCK_LEVEL_2 以上で触っている SHARED を、
  自分も読む・書く場合は着手しない。
  読むだけでも、相手が意味を変えている最中なら不可

DEPENDENCY_ORDER_CLEAR
  先に main へ入るべき上流 Issue が未 merge のまま下流を実装しない

BASE_MAIN_COMPATIBLE
  自分の base が現在の origin/main と互換であること
```

```
5 条件のうち 1 つでも判定できない場合は CODE_WIP_ACQUIRE_ALLOWED = NO。
「不明」は「可」ではない。
```

### 2.6.4 掲示(DOMAIN_WIP_DECLARATION)

code WIP を取得する作業者は、実装を始める前に Issue へ次を掲示する。

```
DOMAIN_WIP_DECLARATION

ISSUE                  #<番号>
ASSIGNEE               <作業者>
INSTRUCTION_ID         <取得根拠となった指示 ID>
ACQUIRED_AT            <UTC タイムスタンプ>

PRIMARY_DOMAIN         D<n>
LOCKED_DOMAINS         D<n> [, D<m> ...]   R3 / R4 で取得したすべて
SHARED_TOUCHED         S-<nn> [, ...] | NONE
LOCK_LEVEL             1 | 2 | 3 | N/A
COMPATIBILITY_EVIDENCE LOCK_LEVEL = 1 のとき必須(2.6.5)
PLANNED_FILES          <変更予定の主要 path>
PLANNED_CONTRACTS      <触る永続契約> | NONE
BASE_MAIN_SHA          <SHA>
```

`SHARED` を `PRIMARY_DOMAIN` として宣言することはできない。SHARED は領域では
なく層であり、影響する領域を `LOCKED_DOMAINS` へ展開する。

`PLANNED_FILES` は着手時点の見込みでよい。見込みが外れること自体は違反では
なく、**増えたときに黙って続けること**が違反である(2.6.7)。

### 2.6.5 SHARED の LOCK_LEVEL

```
LOCK_LEVEL_1  ADDITIVE_AND_BACKWARD_COMPATIBLE
              追加であり、かつ後方互換であることを実測できた場合のみ
              -> 参照領域の code WIP を奪わない(掲示は必須)

LOCK_LEVEL_2  BEHAVIOR_CHANGE
              既存の入出力・判定結果が変わりうる
              -> functional_domains.md K節の「lock する領域」をすべて取得

LOCK_LEVEL_3  CONTRACT_BREAKING
              永続表現・provider 契約の破壊的変更
              -> K節の領域に加えて全領域を lock。さらに人間承認が必須
```

`LOCK_LEVEL_1` を主張するには、次の 5 つを**すべて実測**して
`COMPATIBILITY_EVIDENCE` へ記載する。

```
EXISTING_CONSUMER_BEHAVIOR_UNCHANGED = YES
    既存の呼び出し元の挙動が変わらない

EXISTING_SERIALIZATION_COMPATIBLE = YES
    新要素を含まないデータが従来どおり読み書きできる

EXISTING_VALIDATION_COMPATIBLE = YES
    従来通っていた入力が通り、従来弾いていた入力が弾かれる

EXISTING_PERSISTED_READ_COMPATIBLE = YES
    保存済みデータの読み手が壊れない。
    新旧の Lambda が同時に動く期間(デプロイ途中・段階リリース)を含めて評価する

EXHAUSTIVE_CONSUMER_IMPACT = NONE
    網羅的な分岐・対応表・許可リストのいずれにも影響がない。
    参照元を全件列挙したうえで確認する
```

```
1 つでも NO または UNKNOWN なら LOCK_LEVEL_1 を主張してはならない。
UNKNOWN_COMPATIBILITY_FAILS_CLOSED = YES
```

種類ごとの既定。

```
enum 値の追加
    DEFAULT = LOCK_LEVEL_2
    consumer を全件実測し、既存 consumer が未知の値を安全に扱うことを
    証明できた場合に限り LOCK_LEVEL_1 へ下げてよい

optional な永続 field の追加
    LOCK_LEVEL_2 として調査を開始し、reader compatibility と
    serialization compatibility を実測できた場合に LOCK_LEVEL_1

新しい関数・モジュールの追加
    既存の呼び出し元をひとつも変更しない場合に限り LOCK_LEVEL_1。
    既存関数から新関数を呼ぶよう書き換えた時点で LOCK_LEVEL_2 以上

分類できない場合
    LOCK_LEVEL_3(fail-closed)
```

```
禁止  「diff が追加だけだから LOCK_LEVEL_1」という判定。
      LOCK_LEVEL は diff の形ではなく、
      既存の利用者から見た振る舞いが変わらないことで決める。

      enum 値の追加は追加しかしていなくても、網羅的な分岐 / 対応表 /
      入力バリデーション / 直列化 / 永続データの読み手 / 外部の利用者を
      壊し得る。「追加だから安全」は成り立たない。
```

### 2.6.6 code WIP の解放

```
CODE_WIP_RELEASED = PR_MERGED
                AND MAIN_CI_PASS
                AND MAIN_HEAD_EXACT
                AND NO_CORRECTIVE_CODE_WIP_REQUIRED
```

```
解放しない時点   PR を作成した時点
                 ChatGPT review が PASS した時点
                 CI が PASS した時点(main CI と main HEAD の一致まで見る)

解放が意味しないこと
                 Production へ反映済みである
                 Production で検証済みである
                 Issue が CLOSED である
```

解放が意味するのは「その領域で次の実装を開始できる」ことだけである。
Issue の OPEN / CLOSED、Production の状態とは分離して扱う。

途中で放棄する場合は `CODE_WIP_ABANDONED` として、残置物(branch 名 /
stash の有無 / 未 merge の変更)を明記する。**勝手に branch を消さない・
stash を drop しない。**

### 2.6.7 実装中の scope 拡大(SCOPE_EXPANSION_RULE)

実装中に、宣言していない領域・SHARED を触る必要が生じた場合。

```
E1  その場で実装を止める
E2  追加で必要な領域の WIP が空いているかを fresh に確認する
E3  空いていれば LOCKED_DOMAINS を追記して掲示し直し、続行する
E4  空いていなければ続行しない。次のいずれかを選び、理由とともに報告する
      (a) 追加領域を触らない実装へ設計を変える
      (b) 追加領域の WIP が空くまで待つ
      (c) Issue を分割する
```

```
禁止  宣言外の領域を黙って触る。
      「もう半分書いたから」は続行の理由にならない。
```

### 2.6.8 P0 割り込みと main 追随

```
P0_INTERRUPT_RULE

P0(動かない・データ破壊)の Issue は、対象領域の code WIP が他者に
保持されていても着手できる。ただし次の手順を守る。

I1  割り込む側が P0_INTERRUPT_DECLARATION を掲示する
      対象領域 / 割り込む理由 / 影響する進行中 WIP / 想定所要
I2  進行中の WIP 保持者は作業を停止し、変更を commit または stash して
      作業中である旨と残置物を掲示する(破棄しない)
I3  P0 の PR が main へ merge され main CI が PASS する
I4  停止していた側が最新 main を取り込み直して再開する
I5  再開時は必要な re-review を行う
```

```
掲示なしの割り込みは認めない。停止側が気づかず衝突する原因になる。
P1 以下は既存 WIP を強制的に中断しない。順番を待つか、別領域の Issue を
先に進める。優先度の入れ替えが必要な場合は人間の判断を仰ぐ。
```

```
MAIN_SYNC_RULE

自分の base より後に main が進んだ場合、進んだ内容が次のいずれかと
重なるなら main を取り込み直す。

  自分の LOCKED_DOMAINS
  自分の SHARED_TOUCHED
  自分の PLANNED_FILES

いずれとも重ならない場合の取り込みは任意。
force push が必要になる方法を安易に選ばない。main の取り込みは merge を
既定とし、rebase はレビュー済みの履歴を書き換えない場合に限る。
```

```
RE_REVIEW_REQUIRED

取り込み直した結果、次のいずれかならレビューをやり直す。

  SHARED の LOCK_LEVEL_2 以上の変更が入った
  自分が読む永続契約の形が変わった
  自分の変更ファイルに conflict が発生した
  取り込み後にテスト結果が変わった

「CI が通ったから同じ」とはみなさない。conflict 解消時の判断は
人間・レビュアが見ていない変更である。
```

### 2.6.9 現在の WIP 保持状況の SSoT

```
WIP_STATE_SSOT = 各 Issue の DOMAIN_WIP_DECLARATION
                 + 最新の ISSUE_STATE_SNAPSHOT(6.5.3)
```

```
置かない  docs 側での「現在どの領域が埋まっているか」の管理
```

現況は頻繁に変わるため、PR 経由で更新する docs には置かない。

#### 集約 read model(index)

当初は「専用の WIP 管理 Issue を作らない」としていた。管理表を別に作ると Issue と
二重更新になり、片方だけ更新される事故が起きるためである。この懸念は残るが、
**現況の把握に OPEN Issue の横断読みが毎回必要になる**という運用上の負荷も実測
されたため、人間の承認により次の限定的な例外を設ける(Issue #184)。

```
READ_MODEL_STATUS  = CACHE_ONLY
READ_MODEL_IS_SSOT = NO
INDEX_ONLY         = YES
```

```
WIP_STATE_SSOT は変更しない。
正本は引き続き 各 Issue の DOMAIN_WIP_DECLARATION + 最新の ISSUE_STATE_SNAPSHOT
である。read model は SSoT の複製ではなく、**どの Issue を読めばよいかの索引**
として持つ。
```

保管方式と必須項目。

```
保管      専用の tracking Issue の本文を可変 cache として使う
          (append-only のコメントで現況を管理しない。最新がどれか埋もれるため)
掲示      本文の冒頭へ次を常時掲示する
            READ_MODEL_ISSUE_IS_NOT_SSOT = YES
            CURRENT_STATE_CACHE_ONLY     = YES
label     Issue Type = tracking / status:調査・設計中 / 恒久 OPEN
          (issue_label_policy.md 7.3.5。完了が単一工程で定義できない Issue は
           到達点ではなく現在の活動段階を status とする)
更新責務  READ_MODEL_UPDATE_OWNER = ACTOR_WHO_CHANGED_WIP
          WIP を取得・解放した作業者が、同じ作業単位の中で更新する
```

```
必須 field

  GENERATED_AT / SOURCE_MAIN / SOURCE_ISSUES
  ACTIVE_DOMAIN_WIP / SHARED_LOCKS / WORKER_WIP /
  GRANDFATHERED_WIP / UNKNOWN_BRANCH_STATE

GENERATED_AT と SOURCE_MAIN の無い read model は無効として扱う。
各行は SOURCE_ISSUES を持ち、必ず SSoT へ辿れるようにする。
```

不一致と staleness の扱い。

```
READ_MODEL_MISMATCH -> ISSUE_SNAPSHOTS_WIN -> READ_MODEL_REBUILD_REQUIRED
READ_MODEL_STALE    -> INDEX_USE_FORBIDDEN -> 下記の全走査へフォールバック
```

```
DOMAIN_WIP_READ_MODEL != ASSIGNMENT_READ_BARRIER_BYPASS

read model を見てよいのは「どの Issue を読むべきか」までである。
lock 取得の確定は、read model が名指しした Issue の宣言と snapshot を
fresh に読んで行う(6.5.4 の read barrier を省略できるようにはしない)。
```

```
ACTIVATION_BOUNDARY

本節の例外は Issue #184 の発効をもって有効になる。
それまでは read model の tracking Issue を作成しない。
```

```
着手前の確認手順

1  OPEN な Issue のうち status:開発中 のものを fresh に列挙する
2  それぞれの DOMAIN_WIP_DECLARATION を読む
3  自分が触る領域と重ならないことを確認する

memory・会話要約・古い Issue 本文だけで判断しない(6.5.4 と同じ原則)。
```

### 2.6.10 発効の境界

```
ACTIVATION_STATE   = 発効済み(2026-09-06 02:27 JST)
ACTIVATION_STATE_SSOT = Issue #177 の最新の durable な activation 記録
```

**docs が main に入っただけでは本節は有効にならなかった。** 発効は次をすべて
終えたのち、人間による明示的な発効宣言をもって行われた。同じ手順は、将来
本節を改訂して再発効する場合にも適用する。

```
docs review -> PR -> CI -> 人間の merge 承認 -> merge -> main CI
-> 周知(2.6.11) -> 人間による明示的な発効宣言
```

```
RETROACTIVE_APPLICATION = NO

発効時点で進行中の code WIP は、完了まで 2 節の担当者単位ルールで扱う。
発効後に新しく取得する WIP から本節を適用する。
```

```
巻き戻し

発効後に「lock の粒度が粗すぎて動けない」「逆に衝突が増えた」等が
観測された場合、人間の判断で 2 節のルールへ戻せる。
戻す場合も宣言を要し、黙って併用しない。
```

試行期間として、発効から 2 週間はゲート判定で迷った事例を Issue #177 へ
記録し、試行後に領域一覧と LOCK_LEVEL の見直しを 1 度行う。試行の終了は
期間の経過だけでは成立せず、人間とレビューによる continue / amend / rollback の
判断をもって確定する。

### 2.6.11 発効時の周知(COMMUNICATION)

発効宣言の前に、次を TARO / JIRO / ChatGPT / 人間の全員へ周知する。

```
1  領域カタログの所在(docs/functional_domains.md)
2  着手可否の最終ゲート(2.6.3)
3  DOMAIN_WIP_DECLARATION の書式(2.6.4)
4  SHARED の LOCK_LEVEL と LEVEL_1 の証拠要件(2.6.5)
5  code WIP の解放条件(2.6.6)
6  scope 拡大時の停止義務(2.6.7)
7  P0 割り込みの手順(2.6.8)
8  新機能・新領域・廃止時のカタログ維持義務(functional_domains.md M節)
9  現在の WIP 保持状況の SSoT と着手前の確認手順(2.6.9)
10 発効日時(EFFECTIVE_FROM)
11 遡及適用しないこと(RETROACTIVE_APPLICATION = NO)
```

周知は発効の前提であり、周知を省いて発効しない。

---

## 3. 実装パイプライン(C)

標準の流れ:

```
implementation
  → targeted pytest
  → related regression
  → ruff
  → mypy
  → local commit
  → remote branch push
  → ChatGPT remote diff review
  → PASS
  → PR
  → required CI
  → MERGE_READY
  → HUMAN MERGE APPROVAL
  → merge
  → main CI
```

- **push 前の ChatGPT review は不要。**
  push は **review checkpoint** として扱い、レビュワーが GitHub 上の確定 diff を
  直接参照できるようにする。
- push は通常 push とし、**force push を使わない**
  (remote branch 公開後の履歴 rewrite を避けるため、rebase ではなく
  `git merge --no-ff origin/main` で最新 main を取り込む)。
- **merge には人間の明示承認が必要**(9 節)。
- merge method は通常の merge commit。squash / rebase merge / auto-merge /
  `--admin` は使用しない。承認対象の exact SHA に対してのみ成立させるため
  `--match-head-commit` を指定する。

### レビュー対象の指定

branch の分岐点が古い場合、`git diff origin/main..HEAD` は**他 Issue の変更を
削除差分として表示する**。レビュー依頼時は必ず merge-base を明示する。

```
正しい   git diff <merge-base>..<HEAD>
誤り     git diff origin/main..HEAD   ← 他 Issue の変更が削除として混入する
```

### Issue の自動 close を避ける

PR 本文では、Issue に後続 Phase が残る場合や Production 自然検証を close 条件と
する場合、`Fixes` / `Closes` を使わず **`Refs #xxx`** を使う。
`Fixes #xxx` は merge 時に Issue を自動 close するため、検証前に close されてしまう。

---

## 3.5 時間意味論変更ゲート(C')

Issue #52 / #143 / #148 は、いずれも「時刻・営業日・市場セッションの扱い」に
起因した。本節はその再発を上流で止めるためのゲートである(Issue #145)。

**大多数の PR には何も課さない。** トリガに該当する変更にのみ適用する。

### 3.5.1 トリガ判定

次の diff があるときだけ `TIME_SEMANTICS_IMPACT = YES` となる。
判定はファイルパスと diff の有無で決まり、著者・レビュワーの主観に依存しない。

```
T1  domain/market_session.py / business_calendar.py / jst.py / price_freshness.py
T2  provider が返す日付・時刻の決定ロジック
    (as_of_date / fetched_at / bar date / 取得窓の start・end / timezone 変換 /
     外部ライブラリの日付境界の使い方)
T3  時刻由来値を受け取って業務分岐する consumer
T4  test / mock / fixture の clock または期待日
```

T4 を含めるのは、**Issue #143 が「テスト期待値の変更」として現れた**ためである。

### 3.5.2 control identifiers

```
C-BM  固定 clock の境界マトリクス(全境界)
C-BS  分岐する状態のみの固定 clock テスト
C-MG  mutation guard(緩める方向の変更で必ず落ちること)
C-CO  affected cohort の組み合わせ実行(同一プロセス)
C-CS  clock 固定化の影響範囲分析(どの cohort に属するかの特定と記録)
C-PC  provider contract テスト
C-EL  外部ライブラリの境界仕様の独立根拠(version を必ず記録)
C-TZ  timezone / 取得窓の証拠
C-MD  mock 期待値が provider contract 由来であること(リテラルの期待日で固定)
```

### 3.5.3 決定表

| TRIGGER | REQUIRED_CONTROLS |
|---|---|
| **T1** | `C-BM` `C-MG` `C-CO` |
| **T2** | `C-PC` `C-EL` `C-TZ` `C-MD` |
| **T3** | `C-BS` |
| **T4** | `C-CS` `C-CO` + 変更対象が属する層(T1/T2/T3)の control |

**T1-T4 は階層ではない。** 番号の大小に上下関係はなく、それぞれが独立した
control の集合である。T1 は T2 を包含しない(共有ヘルパを変えても provider
契約の証拠は得られない)。

複数トリガに該当する場合:

```
REQUIRED_CONTROLS = 該当する全トリガの control の union
```

「より強いトリガを1つ選ぶ」方式は採らない。control が欠落するためである。

```
例  T1 + T2  ->  C-BM C-MG C-CO C-PC C-EL C-TZ C-MD
    T2 + T4  ->  C-PC C-EL C-TZ C-MD C-CS C-CO
    T3 のみ  ->  C-BS
```

### 3.5.4 control の内容

**`C-BM`(T1 のみ)** — 次の境界を固定 clock で網羅する。

```
寄り付き直前 / 寄り付きちょうど / 立会中 / 大引け1分前 / 大引けちょうど /
大引け後 / 非営業日 / 連休明け / UTC-JST 日跨ぎ / 真の未来日
```

業務上存在しない境界は N/A としてよいが、**理由を明記する**。無言の省略は不可。

**`C-BS`(T3)** — 実際に分岐する状態のみでよい。**無関係な全境界は不要。**
最も件数が多いのは T3 であり、ここへ全境界を課さないことが本ゲートを軽く保つ要点である。

**`C-CO` / `C-CS`(T4)** — 1 ファイルだけ clock を固定して他モジュールとの整合を
壊さないこと。固定化後、単独実行だけでなく **cohort の組み合わせ実行**で回帰が
無いことを確認する。cohort は `tests/unit/test_time_semantics_guard.py` の
registry に定義されている。

Issue #143 は単独実行と全件 CI では確認したが、**部分集合の組み合わせ実行を
確認しなかった**ため Issue #148 を露出させた。

**順序依存 cohort** — cohort のメンバを揃えるだけでは不十分な場合がある。
共有 module-global state による汚染には**方向**があるためである。

```
実測(Issue #148)
  integration -> handler   handler 側 11 件が失敗する
  handler -> integration   失敗しない
```

pytest の収集順(アルファベット)は `handler -> integration` であり、
**full CI ではこの汚染方向を通らない**。したがって順序依存が既知、または
合理的に疑われる cohort では `DECLARED_ORDER_CASES` を宣言し、その順序で実行する。

```
全順列は要求しない(cohort が 5 件なら 120 通りとなり組合せ爆発する)。
宣言された「既知・高リスクな汚染方向」のみを対象とする。
```

宣言した順序で既知の失敗が出る場合は `KNOWN_FAILURE_ISSUE` に owner Issue を
記載する。ただし **「red だが既知」で済ませない。**

```
期待する失敗集合 と 実際の失敗集合 を比較し、一致することを確認する。
新規の失敗が 1 件でもあれば、それは当該変更の regression として扱う。
```

order case は `tests/unit/test_time_semantics_guard.py` に宣言し、
同ファイルが metadata の健全性(登録済みモジュールのみ / cohort 外を参照しない /
重複が無い / 順序依存と宣言した cohort が order case を持つ)を検証する。
**実行そのものは自動化しない**(1 回あたり数分を要するため、CI を重くしない)。


**`C-EL`(T2)** — 「このコードがそう書いてあるから、実装者はそう理解している
はず」は**根拠にしない**。official docs / installed source / upstream source /
read-only experiment のいずれかで確認し、**version とともに記録する**。

**`C-MD`(T2)** — mock の期待値を判定側 helper から自己参照的に導出した
assertion は、恒真になりうるため provider contract の証拠と認めない。

```
不可  assert snapshot.as_of_date <= latest_plausible_bar_date(now, calendar)
      (mock が helper ちょうどで打ち切る実装なら失敗しえない)

可    局面ごとにリテラルの期待日を固定する
      ("PRE_OPEN_0800", _BUSINESS_DAY, 8, 0, _PREV_BUSINESS_DAY)
```

### 3.5.5 証拠の扱い

```
主証拠  固定 clock の境界マトリクス / mutation guard /
        provider の取得窓分析 / 外部ライブラリ境界の独立根拠
補助    CI がどの時刻(局面)で回ったか
```

**CI が「今の時刻で」green であることを correctness の主証拠にしない。**
Issue #143 では、大引け後に回った CI の green が false green であった。

導入しないもの:

```
CI の複数時刻実行 / 時刻別 matrix job / sleep・wait / 現在時刻依存テスト
```

検出確率を上げるだけで非決定性そのものは残るため、対策として誤りである。
正しい対策はテストを時刻非依存にすることに帰着する。

`full pytest` のローカル必須化も行わない(4節の方針を変えない)。

### 3.5.6 registry

時刻に敏感なテストモジュールは
`tests/unit/test_time_semantics_guard.py` の registry に登録する。
registry 自身の健全性(登録漏れ・削除・化石化した例外)も同ファイルで検証される。

```
FORBIDDEN         wall clock 呼び出しがあれば FAIL
ALLOWED_EXISTING  既存の残存リスク。rationale と owner Issue が必須。
                  負債が解消され呼び出しが 0 件になったら FAIL し、
                  FORBIDDEN への更新を促す
```

**全テストファイルを走査する方式は採らない。** 市場セッションに接触しない
wall clock の使用は正当であり、リスクベースの登録制とする。

### 3.5.7 参照 contract(Issue #52)

日足 bar の妥当性について確定した契約。ドキュメント上の参照であり、
本節が Production の挙動を定めるものではない。

```
寄り付き前          当日付の bar は存在し得ない        -> fail-close
立会中(未確定)     当日付の bar は存在しうる          -> 正常
大引け後(確定)     当日付の bar は存在する            -> 正常
非営業日            当日付の bar は存在し得ない        -> fail-close
翌日以降の日付      その時点で存在し得ない            -> fail-close
```

### 3.5.8 PR での宣言

`.github/PULL_REQUEST_TEMPLATE.md` に従い、非該当なら
`TIME_SEMANTICS_IMPACT = NO` の1行のみでよい。該当する場合は
`TRIGGERS` / `REQUIRED_CONTROLS` / `EVIDENCE` を記録する。

`NO` の宣言は免罪符ではない。変更内容と矛盾する場合、レビュワーはこれを FAIL とする。
PR 本文を CI で自動解析する仕組みは導入しない。

#### order-sensitive cohort に該当する場合

order case の実行は**自動化しない**(CI を重くしないため)。手動実行である以上、
実行が忘れられると metadata だけが残り、ゲートが実質的に働かなくなる。
したがって **実行証拠を PR review の必須項目とする**。

`EVIDENCE` へ最低限:

```
ORDER_CASES_EXECUTED = <実行した order case 名>
ORDER_CASE_RESULTS   = <各 case の結果>
```

該当する order case が `KNOWN_FAILURE_ISSUE` を持つ場合はさらに:

```
EXPECTED_FAILURE_SET = <期待する失敗集合の根拠>
ACTUAL_FAILURE_SET   = <実際の失敗集合の根拠>
FAILURE_SET_MATCH    = YES | NO
```

`FAILURE_SET_MATCH = NO`(= 既知失敗と一致しない)場合、差分は当該変更の
regression として扱う。**「red だが既知」で済ませない。**

期待する失敗集合のテスト名一覧を registry へハードコードすることは要求しない
(owner Issue の解消により変動するため)。実行時の出力を根拠として示せばよい。

**order-sensitive でない cohort には、この追加記録を求めない。**

---

## 4. ローカルテスト方針(D)

原則、ローカルでは次のみを実行する。

```
targeted pytest        変更した箇所を直接covertするテスト
related regression     変更が影響しうる周辺(-k で絞り込む)
ruff check src tests
mypy src
```

```
LOCAL_FULL_PYTEST_DEFAULT = FORBIDDEN
FULL_SUITE_AUTHORITY      = PR_CI
```

**全体回帰の正本は PR CI(required checks)である。**
local full suite を既定の手順にしない。1 回 20 分規模を要し、
各 worker が同じ全体テストを何度も繰り返すことになるため、
着手速度を大きく損なう。

**これは品質基準を下げるルールではない。**
ローカルでは狭く速く検証し、CI で広く検証する、という分担である。
全体回帰そのものは PR CI で必ず実行される。

### full suite を local 実行してよい場合(例外)

**suite 全体でしか確認できない具体的な理由がある場合**に限り実行してよい。

```
test order dependency
global state pollution
fixture lifecycle の問題
import-time side effect
collection の問題
CI の full-suite failure の再現
共通 test infrastructure の変更
suite-wide interaction の確認が Issue の主目的である
```

例外を使う場合は、作業報告等へ必ず次を明示する。

```
LOCAL_FULL_PYTEST_EXCEPTION = YES
REASON                      = <具体的理由>
```

```
「念のため」「安全のため」「一応」は理由にならない。
```

これらは**何を確認したいのかを述べていない**ため、例外の要件を満たさない。
上記の例のように、`suite 全体でなければ観測できない事象`を名指しする。

docs のみの変更では pytest / mypy / ruff を機械的に回す必要はない(11 節)。

---

## 5. GitHub への永続化(E)

次は**会話上の報告だけで完結させず、GitHub へ永続化する**。

- investigation(調査結果・再現・root cause)
- design decision(採用案と不採用案、その理由)
- verification result(Immediate / Natural / Negative-path)
- human decision(承認内容と承認対象の exact 識別子)
- merge / deploy result(SHA・CI 結果・ChangeSet ARN 等)

**会話のみを SSoT にしない。** セッションが変わっても同じ前提で作業を継続できる
ことを要件とする。

永続化先は原則として対象 Issue のコメント。運用ルール自体の変更は本文書へ。

**いつ・誰が・どの形式で current state を書き戻すかは 6.5節が正本である。**

---

## 6. 現況判断と status freshness(F)

Issue の現況は、次の 3 つを**総合して**判断する。

1. 最新の確定 status comment
2. current labels
3. Issue body

**どれか 1 つが常に正しいと決めない。** とくに「labels が Acceptance Criteria より
常に正しい」といった単純な優先順位を置かない。3 者が矛盾する場合は、
勝手に推測して実装を進めず、**どれが最新の確定判断かを確認する**
([CLAUDE.md](../CLAUDE.md) の既存ルール)。

Issue body に旧状態の記載が残ることは許容する。**historical evidence として
保持してよい。**

### close 前の最小同期

close する前に、**現況と矛盾する次の箇所のみ**を同期する。

- status(Issue state / verification 状態 / Production 反映状態)
- Production 状態の記載(旧 SHA・旧 deploy gate 等)
- Acceptance Criteria の checkbox

同期は**訂正の追記**によって行い、旧記載そのものを削除しない。
「上記は当時の状態であり、現況は次のとおり」と分かる形にする。

**Problem / Evidence / Root cause / 旧判断・旧 AC の取り下げ記録 /
accepted residual risk 等の歴史的記録を無闇に削除しない。**

本節は現況の**判断**を定める。書き戻しの発火条件・snapshot の形式・
新規割当前の read barrier は **6.5節**が正本である。

---

## 6.5 Issue state の同期(F')

6節は「現況をどう**判断**するか」を定める。本節は「現況をどう**書き戻し**、
新しい作業を割り当てる前にどう**読み直す**か」を定める。

```
ISSUE_STATE_SSOT = GITHUB_ISSUE

STATE_TRANSITION_WRITEBACK_REQUIRED             = YES
HANDOFF_IS_NOT_A_SUBSTITUTE_FOR_STATE_WRITEBACK = YES
WORK_COMPLETE_REQUIRES_SSOT_WRITEBACK           = YES
ASSIGNMENT_READ_BARRIER_REQUIRED                = YES
LATEST_VERIFIABLE_GITHUB_STATE_WINS             = YES

WORKER_STATE_WRITE_OWNER = ACTOR_WHO_CHANGED_STATE
CHATGPT_STATE_READ_OWNER = CHATGPT
```

state を変えた者が書き、次の割当を出す者が読む。**どちらか一方だけでは成立しない。**

### 6.5.1 なぜ handoff だけでは足りないか

handoff は「別の Issue / 別の担当へ移る時」にしか発火しない。しかし state は
その途中でも変わる。branch を push した、PR を作った、merge された、main CI が
落ちた、deploy した——これらは handoff を伴わずに起きる。

```
Issue 本文・snapshot   実装未着手
remote branch          実装済み commit が push 済み
  -> 「新規実装」として指示すると二重実装になる
```

したがって writeback の trigger は handoff ではなく **state transition** とする。

---

### 6.5.2 State transition writeback(WRITE 側)

次を writeback の trigger とする。

```
PHASE_START                     PHASE_COMPLETE
IMPLEMENTATION_START            IMPLEMENTATION_COMPLETE
BRANCH_PUSHED
PR_CREATED                      PR_REVIEW_PASS            PR_MERGED
MAIN_CI_PASS                    MAIN_CI_FAIL
PRODUCTION_DEPLOYED             PRODUCTION_VERIFIED       PRODUCTION_VERIFICATION_FAILED
BLOCKED                         UNBLOCKED
HUMAN_DECISION_REQUIRED         HUMAN_DECISION_RESOLVED
OWNER_CHANGE                    HANDOFF
```

`HANDOFF` は trigger の 1 つにすぎない。**handoff が無くても state が変われば
writeback が必要である。**

#### comment を増やしすぎないための batching

全 trigger で無条件にコメントすると Issue がノイズ化し、かえって最新 snapshot を
見失う。trigger を 2 種へ分ける。

```
ASSIGNMENT_VISIBLE    他の worker が現況を誤判断しうる transition
                      IMPLEMENTATION_START / BRANCH_PUSHED / PR_CREATED / PR_MERGED /
                      MAIN_CI_FAIL / PRODUCTION_DEPLOYED / PRODUCTION_VERIFIED /
                      PRODUCTION_VERIFICATION_FAILED / BLOCKED / UNBLOCKED /
                      HUMAN_DECISION_REQUIRED / HUMAN_DECISION_RESOLVED /
                      OWNER_CHANGE / HANDOFF

BATCHABLE             同一作業単位の内部的な進行
                      PHASE_START / PHASE_COMPLETE / IMPLEMENTATION_COMPLETE /
                      PR_REVIEW_PASS / MAIN_CI_PASS
```

```
BATCHABLE          作業完了時の 1 snapshot へ集約してよい
ASSIGNMENT_VISIBLE 次の作業割当より前に必ず durable 化する
```

原則として **1 作業単位(1 INSTRUCTION_ID)につき snapshot は 1 件**とし、
その中で複数 transition をまとめて記録する。ただし作業が長く、途中で
`ASSIGNMENT_VISIBLE` な transition が発生し、それが次の割当より前に見えない
状態になる場合は、その時点で追加の snapshot を出す。

```
COMMENT_SPAM_MINIMIZED      = YES   1 作業単位 1 snapshot を原則とする
STALE_STATE_WINDOW_BOUNDED  = YES   未書き戻しの ASSIGNMENT_VISIBLE transition が
                                    ある間は次の割当を出さない
```

---

### 6.5.3 ISSUE_STATE_SNAPSHOT contract

current state の記録は自由文だけにせず、機械的に読める固定キーを含める。

```
ISSUE_STATE_SNAPSHOT

DISCLOSURE          = PUBLIC_SANITIZED

STATE_ID            = <YYYYMMDDTHHMMSSffffffZ>-<ACTOR>-<INSTRUCTION_ID|MANUAL>-<NONCE>
SUPERSEDES_STATE_ID = <previous STATE_ID|NONE|LEGACY_NO_STATE_ID>
SUPERSEDES_SNAPSHOT_URL = <url|N/A>   LEGACY_NO_STATE_ID のときのみ必須
STATUS_AS_OF        = <ISO-8601>
ISSUE               = #<number>

CLASSIFICATION      = <issue type>
PRIORITY            = <P0|P1|P2|P3>
RELEASE_BLOCKER     = <YES|NO>
STATUS              = <status: label>

PHASE               = <current phase>
IMPLEMENTATION      = <state>
OWNER               = <TARO|JIRO|USER|NONE>

BRANCH              = <name|NONE>
BRANCH_HEAD         = <sha|NONE>
PR                  = <number|NONE>
PR_STATE            = <state|NONE>
MERGED_TO_MAIN      = <YES|NO>

MAIN_SHA            = <sha|N/A>
MAIN_CI             = <state|N/A>
PRODUCTION_DEPLOYED = <YES|NO|PARTIAL|UNKNOWN|N/A>
PRODUCTION_VERIFIED = <YES|NO|PARTIAL|UNKNOWN|N/A>

RESIDUAL_WORK_UNITS          = <残作業単位の列挙|NONE>
UNIQUE_PROGRESS_STATUS_COUNT = <n>
ISSUE_SPLIT_REQUIRED         = <YES|NO>
SPLIT_TARGET_ISSUES          = <#nnn, #nnn|NONE>

CURRENT_BLOCKER     = <value|NONE>
NEXT_ACTION         = <value|NONE>
NEXT_ACTION_ALLOWED = <YES|NO>
```

`N/A` を許容する。Production へ到達していない Issue へ Production 行を
`UNKNOWN` として並べると、確認していないのか該当しないのかが区別できない。

`RESIDUAL_WORK_UNITS` 以下の 4 行は
[issue_label_policy.md](issue_label_policy.md) §7.3 の
`ONE_ISSUE_ONE_PROGRESS_LIFECYCLE` を検証した結果を残すためのものである。
残作業単位を列挙し、それぞれに相当する Progress Status を割り当て、
種類数が 2 以上なら `ISSUE_SPLIT_REQUIRED = YES` とする。
**判定基準は「独立しているか」ではなく status divergence であり**、
判定ルールそのものの正本は issue_label_policy.md §7.3 で、本節へ複製しない。
`UNIQUE_PROGRESS_STATUS_COUNT = 1` / `ISSUE_SPLIT_REQUIRED = NO` も明示的に書く
(確認したうえで不要と判断した記録になる)。

snapshot は **current labels を記録するもの**であり、label の分類基準ではない。
Type / Priority / Release Blocker / Progress Status の 4軸モデルの正本は
[issue_label_policy.md](issue_label_policy.md) であり、本節はそれを変更しない。

#### STATE_ID の方式

単調増加の手動整数は、**TARO と JIRO が並行して snapshot を出した場合に
同じ番号を取り合って壊れる**。番号の採番自体に排他が要るため採用しない。

```
STATE_ID = <YYYYMMDDTHHMMSSffffffZ>-<ACTOR>-<INSTRUCTION_ID|MANUAL>-<NONCE>

例  20260904T081341882304Z-JIRO-JIRO-20260904-020-a1b2c3d4
    20260904T081341882304Z-CHATGPT-MANUAL-9f72c1ab
    20260904T081342001234Z-USER-MANUAL-4d8e01f7
```

各成分の責務を分ける。**一意性を担うのは NONCE だけである。**

```
timestamp        人間が概ね時系列を読むための監査補助 / correlation 補助
                 collision resistance の根拠にはしない
ACTOR            発行主体の識別
                 TARO / JIRO / CHATGPT / USER
                 既存運用と整合する actor を追加してよいが、
                 無制限の自由文字列にはしない
INSTRUCTION_ID   作業の correlation
  | MANUAL       指示 ID を持たない操作は MANUAL とする
                 collision resistance の根拠にはしない
NONCE            STATE_ID の collision resistance を担保する識別成分
```

##### collision resistance は NONCE が担う

```
STATE_ID の collision resistance は独立生成の NONCE によって担保し、
timestamp / ACTOR / INSTRUCTION_ID 単独の一意性には依存しない。
```

依存させてはならない理由は 2 つある。

```
INSTRUCTION_ID   「必ず一意である」は運用規律にすぎない。
                 本プロジェクトでは既に instruction ID の collision が発生している。
                 規律は破られうるものであり、破られた時に状態追跡まで
                 壊れる設計にしない。

timestamp+ACTOR  衝突確率を下げるだけで一意性を保証しない。
                 同一 ACTOR が同一 microsecond 内に 2 つの snapshot を生成すれば
                 両者は同一になる。
```

本節が目的とするのは「通常は衝突しない」ことではなく、**運用規律が破られても
state tracking が壊れないこと**である。したがって一意性の担保は、既存要素から
独立した NONCE へ寄せる。

###### NONCE の生成

```
用いてよい   UUID4(フル)
             UUID4 由来の短縮表現(8 桁以上の hex 等)
             十分に長い random hex token

禁止         timestamp 派生
             INSTRUCTION_ID 派生
             ACTOR 派生
```

既存要素から決定論的に導出した値は、その要素が衝突した時に同時に衝突するため、
collision 耐性が増えない。**独立に生成すること。**

短縮表現は数学的な絶対一意を保証するものではない。本 contract が要求するのは
`collision-resistant unique identifier` であり、絶対一意の証明ではない。

timestamp を microseconds まで固定桁で保持するのは、監査時に時系列を読みやすく
するためであり、一意性のためではない。

##### 順序と「最新」の判定

```
timestamp prefix  人間が概ね時系列を読むための補助
                  識別 / correlation / 監査補助に使う
```

**STATE_ID の辞書順を「最新 snapshot」判定の唯一の truth source にしない。**

```
LATEST_SNAPSHOT_TRUTH_SOURCE = GITHUB_COMMENT_ORDER
```

「最新 snapshot」は Issue のコメント列で最後に現れる `ISSUE_STATE_SNAPSHOT`
とする。actor のクロックずれや後から追記された snapshot があっても、
GitHub のコメント順が正本である。

##### SUPERSEDES_STATE_ID

直前の snapshot の `STATE_ID` を設定する。一致しない場合は
`STATE_DRIFT_DETECTED = YES` として扱う。

```
不一致 = 必ず不正、ではない
         parallel write / race / stale read の検出シグナルとして扱う
         reconciliation で current state を再構成する
```

「直前の snapshot」は GitHub のコメント順で特定する
(`LATEST_SNAPSHOT_TRUTH_SOURCE = GITHUB_COMMENT_ORDER`)。

###### 3 つの場合を区別する

本 contract の運用開始前に書かれた status comment には `STATE_ID` が無い。
これを `NONE` で表すと、**先行 snapshot が無いのか、あるが legacy 形式なのかが
区別できず**、最初の 1 件だけ `SUPERSEDES_STATE_ID` による race / stale read の
検出が成立しない。次のとおり分ける。

```
CASE 1  先行 snapshot が存在しない
        SUPERSEDES_STATE_ID     = NONE
        SUPERSEDES_SNAPSHOT_URL = N/A

CASE 2  先行 snapshot が存在し STATE_ID を持つ
        SUPERSEDES_STATE_ID     = <実際の predecessor STATE_ID>
        SUPERSEDES_SNAPSHOT_URL = N/A

CASE 3  先行 snapshot は存在するが legacy 形式で STATE_ID を持たない
        SUPERSEDES_STATE_ID     = LEGACY_NO_STATE_ID
        SUPERSEDES_SNAPSHOT_URL = <predecessor の実際のコメント URL>   必須
```

```
NONE                = 先行が「無い」
LEGACY_NO_STATE_ID  = 先行は「有る」が STATE_ID contract より前のもの
```

**`LEGACY_NO_STATE_ID` は「ID が分からない」という意味ではない。** 意味は次の
3 条件をすべて満たすことである。

```
predecessor snapshot が存在する
その predecessor が STATE_ID contract より前に書かれている
その predecessor に STATE_ID フィールドが無い
```

CASE 3 で `SUPERSEDES_SNAPSHOT_URL` を必須とするのは、STATE_ID で辿れない 1 点を
**コメント URL で監査可能にする**ためである。ここを省略すると鎖が切れる。

###### migration の規則

```
禁止   legacy snapshot を書き換える
       legacy snapshot へ STATE_ID を後から付け足す
       推測・逆算による fake / inferred な STATE_ID を作る
       predecessor URL を推測や手入力で捏造する
       predecessor が STATE_ID を持つのに LEGACY_NO_STATE_ID を使う
       predecessor が存在しないのに LEGACY_NO_STATE_ID を使う(それは NONE)
```

```
LEGACY_NO_STATE_ID は初回移行の 1 件にのみ使用できる
以後の snapshot は CASE 2(実際の predecessor STATE_ID)へ戻る
```

historical comment は append-only であり、predecessor は必ず実際のコメント順から
特定する。race / stale read が疑われる場合の扱いは上記と同じで、推測で埋めず
reconciliation を行う。

```
LEGACY_MIGRATION_RULE_EFFECTIVE_FROM = 本規則の追加以降
```

**過去の snapshot へ遡及適用しない。** 本規則より前に `NONE` で記録された
移行時 snapshot は、当時の contract に従った historical evidence としてそのまま
保持する。辻褄合わせのために過去を書き換えない。

#### append-only

```
古い snapshot を削除・改変しない
訂正は新しい snapshot の追加で行う
```

これは 6節「旧記載そのものを削除しない」と同じ原則である。監査履歴を残す。

#### 証拠の採用順序

snapshot も絶対視しない。**証拠の採用順序の正本は
[chatgpt_collaboration_protocol.md](chatgpt_collaboration_protocol.md) 3.6節**であり、
ChatGPT に限らず全 actor へ適用する。序列を本節へ複製しない
(複製すると正本の改訂時に本節が stale になり、本節が防ごうとしている事故を
本節自身が起こす)。

```
snapshot          IMPLEMENTATION = NOT_STARTED
remote branch     より新しい実装 commit が存在する
  -> CURRENT_VERIFIABLE_STATE が優先
  -> STATE_DRIFT_DETECTED = YES / STATUS_RECONCILIATION_REQUIRED = YES
```

---

### 6.5.4 Assignment Read Barrier(READ 側)

新しい Issue または別 Phase へ作業者を割り当てる**前**に実行する。
実行主体は ChatGPT(`CHATGPT_STATE_READ_OWNER = CHATGPT`)。

```
1   Issue current state / labels
2   最新の ISSUE_STATE_SNAPSHOT
3   その snapshot より後の Issue comments
4   関連する open / closed / merged PR
5   関連する remote branch
6   current main SHA
7   branch / PR commit が main に含まれるか
8   main CI
9   Production release state          (関連する場合)
10  Production verification state     (関連する場合)
11  残作業単位と Progress Status の整合
```

**記憶・会話要約・古い Issue 記述だけを根拠に新規実装を指示しない。**

#### fresh-read の対象

```
FRESH_READ_SOURCE                      = CURRENT_REMOTE_SSOT
LOCAL_WORKTREE_IS_NOT_SSOT             = YES
STALE_FEATURE_BRANCH_IS_NOT_FRESH_READ = YES
```

現在有効な規則・カタログ・契約を読むときは、**明示 ref で remote を読む**。

```
規則・カタログ・契約を読む  git show origin/main:<path>
                            または GitHub 上の main を読む

branch の diff を確認する    その branch を読む(この用途に限る)
```

手元の作業 branch は、その branch が分岐した時点の内容である。他の作業者が
main へ入れた改訂を含まない。**main の state と branch の state を混同しない。**

```
実例  #177 の作業 branch 上で docs を読み、#181 が main へ入れた
      issue_label_policy.md の改訂前の規則を「現行ルール」として一時的に
      参照した事例が発生している(2026-09-06 / 検出後に main で読み直し)。
```

#### 取得が途中で打ち切られた場合

```
RETRIEVAL_TRUNCATED -> FRESH_READ_COMPLETE = NO
```

Issue のコメント一覧・snapshot は件数が増えると取得が途中で打ち切られることが
ある。**途中までしか読んでいない状態を「読み終えた」として扱わない。**

```
最新コメントの末尾 / 最新 snapshot 等を追加取得し、
latest state を再構成するまで assignment 判断を行わない。
```

打ち切りに気づいたまま進めることは、6.5.6 の freshness gate FAIL と同じ扱いと
する。この状態は
[ai_operation_message_contract.md](ai_operation_message_contract.md) 4節の
`UNKNOWN_STATE` に該当し、compact な報告で流さない。

#### 11 の確認内容(G1: lifecycle 分岐の検出)

1〜10 で集めた事実をもとに、その Issue に残っている作業単位を列挙し、
それぞれへ現在相当する Progress Status を割り当てる
(判定ルールの正本は [issue_label_policy.md](issue_label_policy.md) §7.3)。

```
UNIQUE_PROGRESS_STATUS_COUNT > 1
  -> STATUS_RECONCILIATION_REQUIRED
  -> ISSUE_STATE_FRESHNESS_GATE = FAIL と同じ扱いとし、
     implementation assignment を出さない
  -> 先に split の要否を判断し、必要なら分割してから status を確定する
```

current status label と残 scope が矛盾する場合(例: `status:マージ済` だが
未実装の作業単位が残る)も同様に扱う。**勝手に label を変えない。**

#### applicability(不要な確認を強制しない)

全 Issue で AWS / Production を確認させると、到達しない Issue にまでコストが出る。

```
Production 未到達の Issue          9 / 10 は N/A
branch が存在しないと確認済み       5 / 7 は N/A(存在しないことの確認自体は実施する)
PR が存在しないと確認済み           4 は N/A(同上)
docs のみの Issue                  8 / 9 / 10 は N/A
```

ただし **implementation state を判断する Issue では 1〜5 を原則必須**とする。
「branch は無いだろう」という推測で 5 を省略しない。ここを省略したことが
本節を設けた直接の原因である。

---

### 6.5.5 ASSIGNMENT_BASELINE

新しい指示には、read barrier で確認した baseline を持たせる。

```
ASSIGNMENT_BASELINE

ISSUE_STATE_AS_OF     = <time>
CURRENT_MAIN_SHA      = <sha>
LATEST_STATE_SNAPSHOT = <url|NONE>
RELATED_BRANCHES      = <list|NONE>
RELATED_PRS           = <list|NONE>
IMPLEMENTATION_STATE  = <state>
```

指示文を肥大化させない。branch / PR / Production が明らかに該当しない単純な
Issue では `N/A` / `NONE` を許容する。**baseline を書かないことは許容しない**
(確認したうえで該当なし、と、確認していない、は別である)。

---

### 6.5.6 State Freshness Gate

次のいずれかに該当する場合、新規 implementation を開始してはならない。

```
最新 snapshot が存在しない
snapshot が current GitHub evidence より古い
snapshot と branch / PR / main が矛盾する
Phase 状態が複数 source で矛盾する
owner が不明である
```

```
ISSUE_STATE_FRESHNESS_GATE             = FAIL
STATUS_RECONCILIATION_REQUIRED         = YES
NEW_IMPLEMENTATION_INSTRUCTION_ALLOWED = NO
```

まず **read-only の reconciliation** を行い、snapshot を現況へ同期する。
完了後に implementation gate を再評価する。

#### P0 例外(適用範囲は最小)

```
条件   P0 の Production incident で、即時の被害抑止が必要な場合に限る
緩和   read barrier を最小確認(Issue state / labels / 最新 snapshot /
       current main SHA)へ縮小し、freshness gate FAIL でも
       被害抑止のための指示を出してよい
記録   P0_BARRIER_REDUCED = YES と理由を残す
復旧   被害抑止の完了後、通常作業を再開する前に full reconciliation を行う
```

```
緩和しないもの
  Human Gate / merge 承認 / Production approval / exact ChangeSet approval
  PRODUCTION_DEPLOYMENT_EXECUTOR / DEPLOY_OPERATION_DELEGATION_TO_JIRO
  Production failure injection 禁止
  release-blocker lifecycle
```

**P0 例外は「読む手間を減らす」ものであり、「承認を飛ばす」ものではない。**

---

### 6.5.7 Work Complete Gate

作業の完了条件を変更する。実装・テスト・push・報告だけでは完了としない。

```
WORK_COMPLETE = TECHNICAL_WORK_COMPLETE
                AND REQUIRED_SSOT_WRITEBACK_COMPLETE
```

例えば branch push まで行った作業は、`IMPLEMENTATION` / `BRANCH` /
`BRANCH_HEAD` / `NEXT_ACTION` を snapshot へ同期してから `ANSWERED` とする。

不要な snapshot は強制しない。

```
SSOT_WRITEBACK_REQUIRED =
    state が変化した
    OR
    既存の state 記載が stale だと判明した
```

```
read-only の調査で state が変化せず、既存記載も stale でなかった
  -> snapshot 不要(報告のみでよい)
read-only の調査だが、既存記載が stale だと判明した
  -> 訂正の snapshot が必要
```

---

### 6.5.8 handoff の責務(再定義)

```
ISSUE_STATE_SNAPSHOT   current state の主要記録
HANDOFF                次担当への補足情報
```

handoff へ current state 全体を再コピーしない。二重管理になり、両者が食い違う。

```
HANDOFF minimum

LATEST_STATE_SNAPSHOT   = <url>
WHY_HANDOFF             = <reason>
NEXT_RECOMMENDED_ACTION = <action>
SPECIAL_CAUTION         = <notes|NONE>
```

handoff を残す既存の運用は維持する。そのうえで
`HANDOFF_IS_NOT_A_SUBSTITUTE_FOR_STATE_WRITEBACK = YES` を明記する。

---

### 6.5.9 人間が state を変えた後の責務

merge は `MERGE_EXECUTOR = USER` であり(10節)、作業 AI だけでは全 transition を
書き戻せない。

```
worker が PR 作成 -> USER が GitHub 上で merge
  -> PR_MERGED / MAIN_CI_PASS を worker は書き戻せない
```

```
NEXT_CHATGPT_GATE_OWNS_RECONCILIATION = YES
```

ChatGPT は post-merge の gate で次を確認する。

```
merge commit / current main SHA / main CI / Issue state の writeback
残作業単位と Progress Status の整合
```

snapshot が無ければ、ChatGPT 自身が記録するか、worker へ reconciliation を
指示して同期させてから次工程へ進む。ChatGPT が直接 Issue を書き換える運用を
必須にはしない。**要点は「誰かがやるだろう」を禁止し、next gate owner を
明示することである。**

#### G3: final status transition より前に split を判断する

merge 後は「その Issue の残作業が何か」が最も明確になる時点である。ここで
`RESIDUAL_WORK_UNITS` と `UNIQUE_PROGRESS_STATUS_COUNT` を確認する
(判定ルールの正本は [issue_label_policy.md](issue_label_policy.md) §7.3)。

```
UNIQUE_PROGRESS_STATUS_COUNT > 1
  -> final status transition より先に split する
  -> 分割してから status を確定する
```

merge した成果物だけを見て `status:マージ済` へ進め、未実装の作業単位を
不可視にしない。これを見落とすと、その Issue は「実装は終わっている」と
読める label のまま残り、release 判定を誤らせる。

---

### 6.5.10 State drift audit

queue reorder は原則 1 日 1 回(0節)。これに合わせて軽量な drift audit を行う。

```
P0 / P1 の open Issue      必須
P2 / P3                    全件走査しない。次のいずれかに該当するものだけ
                             queue 候補として着手を検討している
                             remote branch または open PR が存在する
                             直近で human decision を待っている
```

**Stabilization Sprint の速度を落とさないため、P2 / P3 の全件重走査は行わない。**

検出対象の例:

```
Issue は NOT_STARTED だが branch が存在する
Issue は PR 未 merge だが PR が merged
Issue は Production 未 deploy だが deploy 済みの evidence がある
Issue は verification pending だが PASS evidence がある
labels が最新の triage と不一致
同一 finding の owner が複数 Issue に存在する
```

検出時:

```
STATE_DRIFT_DETECTED           = YES
IMPLEMENTATION_START           = BLOCKED
STATUS_RECONCILIATION_REQUIRED = YES
```

---

## 7. Negative-path verification(G)

negative path(異常系)の検証要求を 2 種へ分類する。

### `MANDATORY_FOR_RELEASE_BLOCKER_REMOVAL`

その検証が完了するまで `release-blocker` を解除できない。
Issue 本文の Production Verification Plan に明示する。

### `OPTIONAL_POST_RELEASE_OBSERVATION`

自然な障害発生を待つことを必須としない。次の**事前に定義された代替証拠**が
揃っていることをもって判断してよい。

- unit / contract tests(mutation 検証で検出力を確認したもの)
- CI required checks
- Immediate Verification(deploy 直後の read-only 確認)
- 正常系の natural evidence(自然実行が正常終端していること)

そのうえで **ChatGPT review PASS + 人間判断**により解除可否を決める。

### 分類を勝手に変えない

**Issue 自身が「自然な negative-path observation」を Acceptance Criteria として
明示している場合、これを勝手に `OPTIONAL` へ変更しない。**
分類の変更には Issue の更新と人間の判断が要る。

### Production failure injection は禁止

Production で `AccessDenied` / provider failure / Lambda failure 等を
**人工的に発生させない**。必要と判断される場合は別途人間の明示承認を得る。

---

## 8. AWS / CloudWatch の pagination(H)

**「存在しない」「件数が少ない」ことを根拠に Production の欠陥や正常性を
主張する場合、pagination の完了を必須とする。**

対象例:

- CloudWatch Logs `filter_log_events`(`nextToken`)
- DynamoDB `Scan` / `Query`(`LastEvaluatedKey`)
- CloudFormation の list 系 API(`NextToken`)
- Lambda の list 系 API(`NextMarker`)

**単一 page を全件と解釈しない。** 最初の 1 ページだけを見て
「0 件」「1 件しかない」と判定してはならない。

件数を報告する場合は、pagination を完了させたうえでの件数であることを示す。
上限を設けて打ち切った場合は、**打ち切った事実と上限値を明記する**
(黙って truncate した結果を全件のように報告しない)。

なお、検索条件そのものの取りこぼしにも注意する。名前・文字列パターンで
絞り込む場合、対象が別表現で記録されている可能性を確認する
(例: CloudFormation の Processed テンプレートでは、テーブル名の実体ではなく
論理 ID で参照されている)。

---

## 9. Production grouped release(I)

複数 Issue をまとめて release する場合、次を**すべて**満たすこと。

```
SCOPE_EXTERNAL_OPEN_RELEASE_BLOCKERS = 0
  (release scope 外に OPEN な release-blocker が無い)
included Issues がすべて merge 済み
各 Issue の Production Verification Plan が既知
先行する mandatory verification に未完了が無い
included Issues が相互に競合しない
release inventory を再構築済み(baseline → target の全 commit を列挙し、
  意図しない Issue 由来の commit が混入していないことを確認)
人間が exact release candidate(SHA)を承認
```

### release scope 内の blocker remediation は release へ含められる

第1条件は「**release scope 外**に OPEN な release-blocker が無いこと」である。
`OPEN な release-blocker = 0` ではない。

`release-blocker` の lifecycle は
[docs/issue_label_policy.md](issue_label_policy.md) §6 のとおり

```
blocker 付与 → 修正の merge → Production deploy → Immediate Verification
  → mandatory verification → ChatGPT review → human approval → blocker 解除
```

であり、**Production deploy が blocker 解除の前提**である。したがって
「OPEN blocker が 1 件でもあれば deploy できない」と適用すると循環し、
Production-target defect を恒久的に remediate できなくなる。

release scope 内の `release-blocker` については、次を**すべて**満たす場合に
release へ含めてよい。

```
今回の release がその blocker の remediation を含む
その修正が merge 済み
その Issue の Production Verification Plan が定義済み
deploy 後も blocker を維持する(deploy では解除しない)
mandatory verification + ChatGPT review + human approval まで解除しない
```

これは「OPEN blocker を無視してよい」という緩和ではない。
**解除条件は一切緩めない。** deploy はあくまで verification の前提であり、
deploy 自体が blocker を解除する根拠にはならない。

release scope 外に OPEN な `release-blocker` がある場合は、その blocker の
block 条件を確認し、今回の release を止めるものかどうかを判断する。
止めるものであれば grouped release へ進まない。

**`release-blocker` label の有無だけで release 可否を判断しない。**
各 blocker の `BLOCKER_MODE` / `BLOCKING_TARGET` / `BLOCKER_SCOPE` を確認する。
これらの記録形式と `DEFECT_BLOCK` / `VERIFICATION_HOLD` の定義、および
`BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER` は
[docs/issue_label_policy.md](issue_label_policy.md) §6 が正本である。

### release inventory の追跡要件

Production release 監査では、baseline → target の**全 commit** について
次を追跡できること。

```
ISSUE
PR
COMMIT
BLOCKER_MODE
BLOCKING_TARGET
VERIFICATION_STATUS
```

Issue へ辿れない commit が baseline → target に含まれる場合は、
その commit の由来を確定させるまで release へ進まない。

### blocker remediation release への piggyback は禁止

Production が**障害状態**にあり、それを解消するための release
(`BLOCKER_REMEDIATION_RELEASE`)には、**無関係な変更を相乗りさせない**。

この場合の release scope は「その障害の修正のみ」であることを、
baseline → target の commit 列挙で確認する。
`単に main が最新だから OK` としない。

`BLOCKER_REMEDIATION_RELEASE`(Production 障害の解消専用)と、
複数の Production-target blocker をまとめて解消する通常の grouped release は
別物である。前者は scope を障害修正のみに限定する。後者は上記の条件を
満たす限り複数 Issue を含めてよいが、**いずれの場合も
release scope 外の変更を便乗させない**点は共通である。

---

## 9.5 すべての変更を Issue 起点とする

```
NO_BEHAVIOR_OR_OPERATIONAL_CHANGE_WITHOUT_ISSUE
```

理由を問わず、**挙動・構成・運用・契約へ影響する変更は必ず GitHub Issue を
起点とする。**

対象:

```
application code
infrastructure
IAM
config / threshold
schema
CI/CD
operational behavior
Production-affecting docs
governance rules
```

変更理由が次のいずれであっても Issue は必須である。

```
bug fix / design correction / requirement addition / requirement change
refactor / security improvement / operations improvement
```

**「小さい変更だから」「ついでだから」を Issue 省略の理由にしない。**

### scope 外の不具合をその場で直さない

```
OPPORTUNISTIC_FIX_FORBIDDEN = YES
FIX_FIRST_ISSUE_LATER       = FORBIDDEN
```

調査・実装の途中で、現在の作業 scope の外にある不具合を見つけることがある。
**その場で直さない。**

```
新規 finding を見つけた場合

  通常   duplicate check -> Issue 化 -> 通常の lifecycle
  P0     duplicate check -> Issue 化 -> 2.6.8 の P0_INTERRUPT_RULE
                          -> 必要な Human Gate
```

```
P0 は「Issue を省略してよい」ではなく
「Issue を作ったうえで最優先に割り込んでよい」という意味である。
P0 であっても、修正を先に行い Issue を後から作ることは認めない。
```

理由は 3 つある。

```
1  宣言した scope の外を触ると、レビュー対象・回帰範囲が宣言と食い違う
2  2.6節の発効後は、宣言外の領域を触ることが domain lock の逸脱になる
3  Issue が無いと、その修正がなぜ必要だったかを後から追えない
```

```
本節は「コードの修正」を対象とする。Production の緊急停止操作
(operations_manual.md の kill switch 等)は運用操作であり、本節の対象ではない。
```

### Issue 省略を許す唯一の例外

純粋な doc-only であり、次の**両方**を満たすものだけ Issue なしを許容してよい。

```
NO_BEHAVIOR_CHANGE    = YES
NO_OPERATIONAL_CHANGE = YES
```

例: typo 修正 / 表現だけの修正 / 壊れたリンク修正 / Markdown formatting。

この場合も PR 本文へ次を記録する。

```
ISSUE_EXCEPTION       = DOC_ONLY_NON_BEHAVIORAL
NO_BEHAVIOR_CHANGE    = YES
NO_OPERATIONAL_CHANGE = YES
```

**判断が曖昧なら Issue を作る側へ倒す。**
**governance rule の変更はこの例外に含まれない。** 本文書のような workflow 変更は
Issue 必須である。

### Issue Type は既存体系を維持する

新しい Type label を不用意に追加しない。
[docs/issue_label_policy.md](issue_label_policy.md) §3 の

```
bug / design-defect / enhancement / investigation
calibration / tracking / not-a-bug / accepted-risk
```

を維持する。`requirement-change` / `refactor` / `security` / `operations` /
`governance` 等は、現時点では**新しい Type 軸を作らず**、
既存 Type + Issue 本文の Reason / Scope で表現する。

```
新要件            -> enhancement
セキュリティ改善   -> bug / design-defect / enhancement を実態で判定
governance 変更   -> enhancement / tracking 等を実態で判定
```

4軸モデル(Issue Type / Priority / Release Blocker / Progress Status)を崩さない。
Severity は 2026-09-05 に廃止済みで、新規付与・再評価・writeback は行わない
([issue_label_policy.md](issue_label_policy.md) §5)。

### Priority を読み直す時点(いつ再評価するか)

Priority の**判定基準**は [docs/issue_label_policy.md](issue_label_policy.md) §4 が
正本である。本節は**いつ読み直し、いつ再評価するか**だけを定める。

Priority は起票時から永久固定ではない。次の時点で fresh に読み直す。

```
Issue 起票時 / duplicate check 後
Phase A 完了時
Production evidence を取得した時
Action delta が判明した時
notification delta が判明した時
Production reachability が判明した時
remediation で主要 impact が消えた時
worker assignment queue を決める時
```

次のいずれかが判明した場合は再評価を必須とする。

```
PRIORITY_REEVALUATION_REQUIRED = YES

1. Production reachability が変わった
2. Action delta が判明した
3. notification delta が判明した
4. batch-wide failure が実到達と判明した
5. latent -> normal recurring と判明した
6. normal recurring -> not reachable と判明した
7. high-impact finding が resolved / moved / out-of-scope になった
8. security exposure が判明した
9. privacy exposure が判明した
10. data recoverability の状況が判明した
11. AWS / resource の cost anomaly が判明した
12. capacity / resource exhaustion が判明した
13. compliance requirement が判明した
14. compensating control が追加・除去された
```

Priority は投資機能への影響だけでなく非機能リスクも含めて評価する
(`MAX(FUNCTIONAL_PRIORITY, NON_FUNCTIONAL_PRIORITY)`)。判定基準は
[issue_label_policy.md](issue_label_policy.md) §4.13〜§4.21 が正本である。

Priority を変更した場合は、その作業の中で GitHub label を更新し、根拠を
durable comment として書き戻す(記載項目は issue_label_policy.md §4.11)。

```
WORKER_PRIORITY_LABEL_WRITE_OWNER = ACTOR_WHO_CHANGED_PRIORITY
```

Priority の変更を理由に Progress Status(§6.5 の state / `status:` label)を
巻き戻さない。両者は独立している。

判定できない場合は推測で埋めず、`PRIORITY_RECONCILIATION_REQUIRED` として
不足している証拠を報告する。

### commit / PR と Issue の追跡

Issue 必須の変更では、**PR が必ず Issue を参照する。**

```
Refs #xx    基本。Production Verification pending の Issue では
            merge 時点で自動 close させないため必ずこちらを使う
Fixes #xx   Issue 全体をその PR で完了させる場合のみ
```

commit 単位でも、どの Issue 由来かを release inventory から追跡可能にする。
**main への直接 push は禁止**(既存方針を維持)。

---

## 10. 人間承認の境界(J)

次の操作には**人間の明示承認が必要**である。Sprint による高速化でこれらを
緩和しない。

```
merge
Production deploy
ChangeSet creation
ChangeSet execution
manual Production Lambda invocation
Production data write
migration / backfill
failure injection
```

補足:

- **ChangeSet の作成も Production 操作として扱う**(execute だけではない)。
- ChangeSet の execute 承認は **exact ChangeSet ARN に対してのみ有効**。
  ChangeSet を再作成した場合、その承認は失効する。新しい ARN を提示して
  再承認を得る。
- **Issue close と release-blocker 解除は別判断**である
  ([docs/issue_label_policy.md](issue_label_policy.md) §6)。
- 承認は**その操作・その対象に限る**。ある文脈での承認を別の文脈へ拡張しない。
- 作業指示の**宛先が異なる**場合(他エージェント宛の承認)、その承認をもって
  操作しない。宛先を確認する。
- **deploy 実作業を誰が行うか**は
  [chatgpt_collaboration_protocol.md](chatgpt_collaboration_protocol.md) 1.5節が正本
  (`PRODUCTION_DEPLOYMENT_EXECUTOR`)。**担当の集約は承認の省略を意味しない。**
  本節の人間承認は担当者が誰であっても従来どおり必要である。

### 併せて緩和しないもの

```
one Issue / one implementation owner
hidden-write 確認([CLAUDE.md](../CLAUDE.md) / operations_manual 18節)
duplicate Issue 確認(起票前に必ず duplicate search)
release-blocker lifecycle(issue_label_policy §6)
Production failure injection 禁止
```

---

## 11. docs のみの変更(DOC_ONLY_CHANGE)

変更が Markdown / ドキュメントのみの場合:

```
実施する    git diff --check(空白・行末の破損確認)
            PII / secret の混入確認
            相対リンク・パスの整合確認
            Markdown 構造の確認(見出し階層・表・コードフェンス)
実施不要    pytest / mypy(コード変更が無いため)
            ruff(Markdown のみなら対象外)
```

報告では `DOC_ONLY_CHANGE=YES` を明示する。

本節は doc-only 変更の**検証手順**を定めるものであり、**Issue の要否とは別**である。
Issue なしで進められるのは §9.5 の `ISSUE_EXCEPTION=DOC_ONLY_NON_BEHAVIORAL`
(`NO_BEHAVIOR_CHANGE` かつ `NO_OPERATIONAL_CHANGE`)を満たす場合に限る。
**governance rule の変更は doc-only であっても Issue 必須**である。

---

## 変更履歴

| 日付 | 変更概要 |
|---|---|
| 2026-09-02 | 新規作成(Issue #122)。Stabilization Sprint の lane / WIP 制限 / 実装パイプライン / ローカルテスト方針 / GitHub 永続化 / status freshness / negative-path 分類 / AWS pagination / grouped release / 人間承認の境界を正本化した。既存の Human merge approval・Human Production approval・exact ChangeSet approval・release-blocker lifecycle・one Issue / one implementation owner・hidden-write 確認・Production failure injection 禁止・duplicate Issue 確認はいずれも緩和していない |
| 2026-09-03 | §9 の grouped release 第1条件を「OPEN な release-blocker = 0」から「release scope 外に OPEN な release-blocker が無い」へ修正(Issue #122)。release-blocker lifecycle(docs/issue_label_policy.md §6)では Production deploy が blocker 解除の前提であるため、旧記述では Production-target defect の remediation release が循環して実施不能になる不整合があった。あわせて release scope 内 blocker を release へ含めるための条件(remediation を含む / merge 済み / Verification Plan 定義済み / deploy 後も blocker 維持 / mandatory verification + ChatGPT review + human approval まで解除しない)を明文化し、`BLOCKER_REMEDIATION_RELEASE` と通常の grouped release の区別を補足した。**解除条件は一切緩和していない** |
| 2026-09-03 | release-blocker の blocking target semantics を導入(Issue #122)。`BLOCKER_MODE`(`DEFECT_BLOCK` / `VERIFICATION_HOLD`)・`BLOCKING_TARGET` 等の必須記録と `BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER` は docs/issue_label_policy.md §6 を正本とし、本文書 §9 はそれを参照する。§9 の第1条件を `SCOPE_EXTERNAL_OPEN_RELEASE_BLOCKERS = 0` として明示し、release inventory の追跡要件(ISSUE / PR / COMMIT / BLOCKER_MODE / BLOCKING_TARGET / VERIFICATION_STATUS)を追加した。あわせて §9.5 `NO_BEHAVIOR_OR_OPERATIONAL_CHANGE_WITHOUT_ISSUE`(挙動・構成・運用・契約へ影響する変更は Issue 必須。例外は `NO_BEHAVIOR_CHANGE` かつ `NO_OPERATIONAL_CHANGE` の doc-only のみで、governance 変更は例外に含まない)・既存 Issue Type 体系の維持・Refs/Fixes の使い分け・main 直接 push 禁止の再確認を追加した。**既存の解除条件・piggyback 禁止・人間承認境界・4軸独立性はいずれも緩和していない** |
| 2026-09-03 | §3.5「時間意味論変更ゲート」を新設(Issue #145)。Issue #52 / #143 / #148 の再発を上流で止めるため、時刻・営業日・市場セッション・timezone・外部ライブラリの日付境界に触れる変更へのみ適用するゲートを定めた。トリガ T1-T4 をファイルパスと diff の有無で客観判定し、control(C-BM / C-BS / C-MG / C-CO / C-CS / C-PC / C-EL / C-TZ / C-MD)を決定表で対応づける。**T1-T4 は階層ではなく独立した control 集合**であり、複数該当時は union を要求する(「より強いトリガを1つ選ぶ」方式は control が欠落するため採らない)。最も件数の多い consumer 変更(T3)には分岐する状態のみを課し、全境界マトリクスは共有ヘルパ変更(T1)に限定する。トリガ非該当の PR には追加負担を課さない。外部ライブラリの境界仕様はコードからの推測を根拠とせず version つきの独立根拠を要求し、mock の期待値を判定側 helper から自己参照的に導出した assertion は provider contract の証拠と認めない。CI の実行時刻は補助証拠に留め、**CI の複数時刻実行・時刻別 matrix job・sleep/wait・現在時刻依存テストは導入しない**(検出確率を上げるだけで非決定性が残るため)。full pytest のローカル必須化も行わない(§4 の方針は不変)。あわせて .github/PULL_REQUEST_TEMPLATE.md と tests/unit/test_time_semantics_guard.py(registry と registry 自身の健全性検証)を追加した。**判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない**(開発運用ゲートの追加のみ) |
| 2026-09-04 | §2.5「指示プロトコル(B')」を新設(Issue #122)。複数の AI エージェントへ並行して作業を依頼する際、どの指示に対する回答かが曖昧になり、古い指示への回答を次工程の根拠にしてしまう事故を防ぐための統制。(1)指示側は全作業指示へ一意な `INSTRUCTION_ID`(`<ASSIGNEE>-<YYYYMMDD>-<連番>`)を付与し、作業者は回答冒頭へ同一 ID・ASSIGNEE・INSTRUCTION_STATUS を記載する。ID が無い回答・別 ID の回答・撤回済み ID への回答は自動的には次工程の根拠にせず、まず対応関係を確認する。(2)指示状態を `PENDING` / `ANSWERED` / `WITHDRAWN` で管理する。(3)**直列化は作業者ごと**とする(`PER_WORKER_SERIALIZATION=YES` / `GLOBAL_SERIALIZATION=NO`)。同一作業者の直前の通常指示が PENDING の間は次の通常指示を追加しないが、**他の作業者の PENDING は新規指示の発行を妨げない**。「一方が作業中なら他方にも指示できない」という誤読を避けるため、別 worker への並行指示可の例を明記した。(4)緊急時のみ `EMERGENCY=YES` / `SUPERSEDES` / `PREVIOUS_INSTRUCTION_STATUS=WITHDRAWN` を明記して差し替えてよく、撤回済み指示への遅延回答は有効な完了報告として扱わない。差し替えの例も明記した。(5)複数の古い指示の結果を 1 つの回答へ混在させない。あわせて CLAUDE.md へ入口となる記載を追加した。**本節は AI への作業指示の統制であり、9節(grouped release)・10節(人間承認の境界)の要求はいずれも緩和していない**(merge / Production deploy / ChangeSet / manual invoke 等の人間承認は INSTRUCTION_ID の有無にかかわらず従来どおり必要)。コード・Production 挙動の変更なし |
| 2026-09-04 | §4「ローカルテスト方針」を明確化(Issue #122)。`LOCAL_FULL_PYTEST_DEFAULT = FORBIDDEN` / `FULL_SUITE_AUTHORITY = PR_CI` を明示した。従来も「理由なく local full suite を毎回実行しない」としていたが、既定の可否が曖昧で、各 worker が同じ 20 分規模の全体テストを繰り返す運用が残っていた。**全体回帰の正本は PR CI** とし、ローカルでは targeted tests / related regression / ruff / mypy に絞る。**品質基準を下げるルールではなく、ローカルは狭く速く・CI は広く、という分担である。**そのうえで full suite の local 実行を完全禁止にはせず、test order dependency / global state pollution / fixture lifecycle / import-time side effect / collection の問題 / CI の full-suite failure 再現 / 共通 test infrastructure の変更 / suite-wide interaction の確認が主目的、といった **suite 全体でしか観測できない具体的理由**がある場合の例外とし、`LOCAL_FULL_PYTEST_EXCEPTION = YES` と `REASON` の明示を求める。「念のため」「安全のため」「一応」は何を確認したいのかを述べていないため理由として認めない。10節(人間承認の境界)・11節(DOC_ONLY_CHANGE)は変更していない。コード・Production 挙動の変更なし |
| 2026-09-04 | §6.5「Issue state の同期(F')」を新設(Issue #157)。GitHub を SSoT としながら、変化する current state を**いつ・誰が・どの形式で書き戻すか**が未定義であったため、古い Issue 記述から実装状態を誤認したまま次の作業指示が出る事故が繰り返し発生していた。実際に、Issue 側は実装未着手を示す一方で remote branch には実装済み commit が push されており、新規実装として指示が出かけた事例がある。**「handoff コメントを丁寧に書く」だけでは解消しない**ため、WRITE 側と READ 側の双方を統制する。(1)writeback の trigger を handoff ではなく **state transition** とし、`PHASE_*` / `IMPLEMENTATION_*` / `BRANCH_PUSHED` / `PR_*` / `MAIN_CI_*` / `PRODUCTION_*` / `BLOCKED` / `HUMAN_DECISION_*` / `OWNER_CHANGE` / `HANDOFF` を列挙した(`HANDOFF` は trigger の1つにすぎない)。(2)Issue のノイズ化を避けるため trigger を `ASSIGNMENT_VISIBLE` と `BATCHABLE` へ二分し、原則 **1 作業単位 1 snapshot**へ集約する一方、他 worker が現況を誤判断しうる transition は次の作業割当より前に必ず durable 化する(`COMMENT_SPAM_MINIMIZED` と `STALE_STATE_WINDOW_BOUNDED` の両立)。(3)機械可読な `ISSUE_STATE_SNAPSHOT` contract を定義した。**手動連番の `STATE_VERSION` は TARO / JIRO の並行 write で番号を取り合うため採用せず**、`STATE_ID = <YYYYMMDDTHHMMSSffffffZ>-<ACTOR>-<INSTRUCTION_ID|MANUAL>-<NONCE>` とした。**collision resistance は独立生成の NONCE が担い、timestamp / ACTOR / INSTRUCTION_ID 単独の一意性へは依存させない。** INSTRUCTION_ID の「必ず一意」は運用規律にすぎず(本プロジェクトでは既に instruction ID collision が発生している)、timestamp + ACTOR も同一 ACTOR が同一 microsecond 内に 2 つの snapshot を生成すれば衝突するため、いずれも一意性の根拠にしない。NONCE は UUID4 由来等の独立生成値とし、**timestamp / INSTRUCTION_ID / ACTOR からの決定論的導出は禁止**する(元の要素が衝突した時に同時に衝突し、耐性が増えないため)。要求するのは `collision-resistant unique identifier` であり絶対一意の数学的保証ではない。timestamp を microseconds まで固定桁で保持するのは監査時の可読性のためであり一意性のためではなく、INSTRUCTION_ID は correlation 情報として保持する。timestamp prefix は識別・correlation・監査補助に用い、**STATE_ID の辞書順を「最新 snapshot」判定の唯一の truth source にしない**(`LATEST_SNAPSHOT_TRUTH_SOURCE = GITHUB_COMMENT_ORDER`)。`SUPERSEDES_STATE_ID` の不一致は並行 write の検出シグナルとして扱い、**不一致 = 必ず不正とはしない**(reconciliation で current state を再構成する)。旧 snapshot は append-only で保持する。Production 未到達 Issue のために `N/A` を許容し、未確認と該当なしを区別する。(4)割当前の **Assignment Read Barrier**(10項目)を定義し、applicability により Production / branch / PR が該当しない Issue へ無駄な確認を強制しない。ただし implementation state を判断する Issue では Issue / labels / snapshot / 後続コメント / PR / **remote branch** の確認を原則必須とした。(5)`ASSIGNMENT_BASELINE` を指示へ持たせる(該当なしは `N/A` 可、ただし**未記載は不可**)。(6)`ISSUE_STATE_FRESHNESS_GATE = FAIL` の間は新規 implementation 指示を禁止し、先に read-only reconciliation を行う。P0 Production incident に限り read barrier を最小確認へ縮小する例外を設けたが、**Human Gate / merge 承認 / Production approval / exact ChangeSet approval / deploy 実行者 / failure injection 禁止 / release-blocker lifecycle はいずれも緩和しない**(読む手間を減らす例外であり承認を飛ばす例外ではない)。(7)`WORK_COMPLETE = TECHNICAL_WORK_COMPLETE AND REQUIRED_SSOT_WRITEBACK_COMPLETE` とし、state が変化した場合または既存記載が stale と判明した場合にのみ writeback を要求する(read-only 調査へ不要な snapshot を強制しない)。(8)handoff を「次担当への補足情報」へ再定義し、current state の主要記録は snapshot とした。(9)USER による merge 等の後は `NEXT_CHATGPT_GATE_OWNS_RECONCILIATION = YES` とし、「誰かがやるだろう」を禁止した。(10)drift audit を定義し、P0/P1 open Issue を必須、P2/P3 は queue 候補・branch/PR 保有・human decision 待ちのみとして**全件重走査を行わない**(Stabilization Sprint の速度を落とさない)。証拠の採用順序は chatgpt_collaboration_protocol.md 3.6節を正本として参照し複製していない。あわせて §5 / §6 へ正本の所在を1行ずつ追加した。**docs のみの変更であり、判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない** |
| 2026-09-04 | §6.5.3 の `SUPERSEDES_STATE_ID` へ legacy predecessor の migration 規則を追加(Issue #157 follow-up)。STATE_ID contract の運用開始前に書かれた status comment には `STATE_ID` が無く、そこから最初の新形式 snapshot へ移る際に `SUPERSEDES_STATE_ID = NONE` とするしかなかった。しかし `NONE` では**先行 snapshot が存在しないのか、存在するが legacy 形式なのかを区別できず**、移行直後の 1 件だけ `SUPERSEDES_STATE_ID` による parallel write / race / stale read の検出が成立しない欠落があった(Issue #52 の実運用で判明)。そこで 3 つの場合を分け、先行なし = `NONE`、先行あり & STATE_ID あり = 実際の predecessor STATE_ID、**先行あり & STATE_ID なし = `LEGACY_NO_STATE_ID` とし、この場合のみ `SUPERSEDES_SNAPSHOT_URL`(predecessor の実際のコメント URL)を必須**とした。STATE_ID で辿れない 1 点をコメント URL で監査可能にするためであり、`LEGACY_NO_STATE_ID` は「ID が分からない」ではなく「predecessor は存在し、STATE_ID contract より前のものである」という明確な semantics を持つ。あわせて migration の禁止事項(legacy snapshot の書き換え / STATE_ID の後付け / 推測による fake・inferred STATE_ID の生成 / predecessor URL の捏造 / predecessor が STATE_ID を持つのに LEGACY_NO_STATE_ID を使う / predecessor が無いのに LEGACY_NO_STATE_ID を使う)を明文化し、`LEGACY_NO_STATE_ID` は**初回移行の 1 件にのみ使用可能**で以後は通常の supersession へ戻ることを定めた。`LEGACY_MIGRATION_RULE_EFFECTIVE_FROM` を本規則の追加以降とし、**過去へ遡及適用しない**(本規則より前に `NONE` で記録された移行時 snapshot は当時の contract に従った historical evidence としてそのまま保持し、辻褄合わせのために書き換えない)。`LATEST_SNAPSHOT_TRUTH_SOURCE = GITHUB_COMMENT_ORDER` と append-only 原則は不変。canonical rule は本節にのみ置き、CLAUDE.md / chatgpt_collaboration_protocol.md へ複製していない。Human Gate / merge 承認 / Production approval / deploy 実行者 / release-blocker lifecycle はいずれも変更していない。**docs のみの変更であり、コード・Production 挙動の変更なし** |
| 2026-09-05 | label の 4軸表記を 5軸(Issue Type / Priority / Severity / Release Blocker / Progress Status)へ同期し、9.5節へ「Priority を読み直す時点」を新設した(#122)。Priority は起票時から永久固定ではなく、Issue 起票時 / duplicate check 後 / Phase A 完了時 / Production evidence 取得時 / Action delta 判明時 / notification delta 判明時 / Production reachability 判明時 / remediation で主要 impact が消えた時 / worker assignment queue 決定時に fresh に読み直す。再評価必須の 7 トリガーと `WORKER_PRIORITY_LABEL_WRITE_OWNER = ACTOR_WHO_CHANGED_PRIORITY`、判定不能時の `PRIORITY_RECONCILIATION_REQUIRED` を定めた。**Priority の判定基準そのものは issue_label_policy.md §4 が正本であり本文書へ複製していない。** Priority 変更を理由に Progress Status を巻き戻さない。既存の lane / WIP / 指示プロトコル / テスト方針 / state 同期 / 人間承認の境界は変更していない。コード・Production 挙動の変更なし |
| 2026-09-05 | 9.5節の Priority 再評価トリガーへ非機能側の 7 項目(security exposure / privacy exposure / data recoverability / cost anomaly / capacity exhaustion / compliance requirement / compensating control の変化)を追加した(#122)。**判定基準そのものは issue_label_policy.md §4.13〜§4.21 が正本であり本文書へ複製していない。** 既存の再評価時点・`WORKER_PRIORITY_LABEL_WRITE_OWNER`・`PRIORITY_RECONCILIATION_REQUIRED` は変更していない。コード・Production 挙動の変更なし |
| 2026-09-05 | Severity 軸の廃止(#122)に伴い、label モデルの表記を 4軸(Issue Type / Priority / Release Blocker / Progress Status)へ同期し、6.5.3 の ISSUE_STATE_SNAPSHOT contract の必須項目から `SEVERITY` を削除して `STATUS` を加えた。**過去の snapshot は一切編集しておらず、そこに残る `SEVERITY = ...` は履歴として有効である。** 互換用の `SEVERITY = RETIRED` 等も新規必須にはしない。**Severity 廃止の理由・retired label の扱い・再導入条件の正本は issue_label_policy.md §5 であり本文書へ複製していない。** 既存の lane / WIP / 指示プロトコル / テスト方針 / state 同期 / Priority 再評価 / 人間承認の境界は変更していない。コード・Production 挙動の変更なし |
| 2026-09-06 | §2.6「機能領域ベースの WIP モデル(DOMAIN_WIP_RULE_V1)」を新設(Issue #177)。§2 の担当者単位 WIP は「同時に壊れる範囲の最小化」には有効だが、互いに無関係な機能領域まで直列化する一方、**Git では検出できない衝突(同じ判定契約・永続契約を別ファイルから同時に変える / 共通 module 経由で他領域が壊れる)を防げていなかった**。実際に Issue #140 は 1 ファイルのみの変更でありながら、その module の呼び出し元は買い判定と保有判断の 2 領域にまたがっており、ファイル重複だけを見る判定では並行可能と誤認する。そこで並行可否を**ファイルの重複ではなく機能領域の重複**で判定する。(1)R1〜R6 を定め、領域ごとに code WIP = 1、異なる領域は並行可、複数領域を触る Issue は必要な領域を同時取得、SHARED は LOCK_LEVEL に従う、`MAX_CONCURRENT_CODE_WIP_PER_WORKER = 1` は維持(並行度は作業者を増やして得るものであり 1 人の掛け持ちでは得ない)、read-only 作業は code WIP を消費しない、とした。(2)着手可否を `DOMAIN_WIP_AVAILABLE` / `FILE_OVERLAP_ACCEPTABLE` / `SHARED_CONTRACT_OVERLAP_ACCEPTABLE` / `DEPENDENCY_ORDER_CLEAR` / `BASE_MAIN_COMPATIBLE` の 5 条件で判定し、1 つでも判定できなければ着手不可とした(「不明」は「可」ではない)。(3)`DOMAIN_WIP_DECLARATION` の書式を定めた。(4)SHARED の `LOCK_LEVEL` を 3 段階とし、**LEVEL_1 を `ADDITIVE_ONLY` ではなく `ADDITIVE_AND_BACKWARD_COMPATIBLE`** と定義した。追加 diff であることは semantic compatibility を保証せず、とくに enum 値の追加は網羅的な分岐・対応表・入力バリデーション・直列化・永続データの読み手・外部の利用者を壊し得るためである。LEVEL_1 の主張には consumer behavior / serialization / validation / persisted read / exhaustive consumer impact の 5 つの実測証拠を要求し、1 つでも UNKNOWN なら LEVEL_1 を禁止する。enum 値追加の既定は LEVEL_2、optional な永続 field 追加も LEVEL_2 として調査を開始し reader/serialization 互換を実測できた場合のみ LEVEL_1 とする。分類不能は LEVEL_3 の fail-closed。**「diff が追加だけだから LEVEL_1」という判定を明示的に禁止した。**(5)解放条件を `PR_MERGED AND MAIN_CI_PASS AND MAIN_HEAD_EXACT AND NO_CORRECTIVE_CODE_WIP_REQUIRED` とし、PR 作成時・review PASS 時には解放しないこと、解放は Production 反映・Production 検証・Issue close のいずれも意味しないことを明記した。(6)scope 拡大時の停止義務、(7)P0 割り込み手順(掲示なしの割り込みは認めない。P1 以下は既存 WIP を強制中断しない)、(8)main 追随と re-review 必須条件、(9)現在の WIP 保持状況の SSoT を**各 Issue の DOMAIN_WIP_DECLARATION + 最新 ISSUE_STATE_SNAPSHOT** とし専用管理 Issue も docs 管理も作らないこと(人間承認 H4)、(10)発効の境界と周知項目を定めた。**本節は設計として確定しているが発効していない**(`DOMAIN_WIP_MODEL_ACTIVE = NO` / `CURRENT_WIP_RULE = ISSUE_122`)。docs が main に入っただけでは有効にならず、周知と人間による明示的な発効宣言を要する。発効後も進行中の作業へ遡及適用しない。領域・機能・共通部品の一覧は docs/functional_domains.md が正本であり本文書へ複製していない。既存の §2 / §2.5 / §3〜§11、人間承認の境界、grouped release、release-blocker lifecycle はいずれも変更・緩和していない。**docs のみの変更であり、判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない** |
| 2026-09-06 | 6.5.3節の ISSUE_STATE_SNAPSHOT contract へ `RESIDUAL_WORK_UNITS` / `UNIQUE_PROGRESS_STATUS_COUNT` / `ISSUE_SPLIT_REQUIRED` / `SPLIT_TARGET_ISSUES` の 4 項目を追加し、6.5.4節の Assignment Read Barrier へ確認項目 11「残作業単位と Progress Status の整合」(G1)を、6.5.9節の post-merge reconciliation へ G3「final status transition より前に split を判断する」を追加した(Issue #181)。Progress Status は Issue 全体を表す単一 label であるため、1 つの Issue の中で独立した作業単位が異なるライフサイクル位置を持つと、どの label を付けても実態と食い違う(Issue #20 で実際に発生)。label は単一値しか持てず「分割が必要な状態を検出したのか、検出したうえで不要と判断したのか」を区別できないため、判定結果を snapshot 側へ残す。read barrier で `UNIQUE_PROGRESS_STATUS_COUNT > 1` を検出した場合は `STATUS_RECONCILIATION_REQUIRED` とし、`ISSUE_STATE_FRESHNESS_GATE = FAIL` と同じ扱いで implementation assignment を出さず、先に split の要否を判断する(勝手に label を変えない)。post-merge は残作業が最も明確になる時点であるため、merge した成果物だけを見て `status:マージ済` へ進めて未実装の作業単位を不可視にしないことを明記した。**判定ルールそのもの(`ONE_ISSUE_ONE_PROGRESS_LIFECYCLE`、`UNIQUE_PROGRESS_STATUS_COUNT > 1` による split 判定、依存関係が判定を上書きしないこと、分割手続き、既存 Issue への lazy 適用)の正本は issue_label_policy.md §7.3 であり、本文書へ複製していない。** 既存の STATE_ID 方式・append-only 原則・証拠の採用順序・applicability・ASSIGNMENT_BASELINE・State Freshness Gate・P0 例外・Work Complete Gate・handoff の責務・drift audit・lane / WIP 制限・指示プロトコル・テスト方針・人間承認の境界はいずれも変更していない。docs のみの変更であり、コード・Production 挙動の変更なし |
| 2026-09-06 | 運用ルールの欠落を補い、集約 read model の例外を追加した(Issue #184)。(1)§2.5.3 へ「指示の直列化」と §2.6 の code WIP モデルが**直交する**ことを明記した(`PER_WORKER_INSTRUCTION_SERIALIZATION != PER_WORKER_CODE_WIP_MODEL`)。#177 の発効により「WIP」という語が 2 つの意味を持つようになり、「領域が空いているから次の指示を出してよい」「1 人 1 code WIP だから指示も 1 本のはず」という双方向の誤読が起こりうるため。片方が空いてももう片方の制約は解除されない。(2)§2.5.5 の報告フォーマットについて、冒頭 3 行のみを本文書に残し、報告の構造(NORMAL / FORENSIC / `BASELINE_INVARIANTS` / 一括コピー可能性 / `AUTHORIZED_PHASES`)の正本を新設した docs/ai_operation_message_contract.md への参照に置き換えた(本文を複製していない)。(3)§6.5.4 へ **fresh-read の対象**を明記した(`FRESH_READ_SOURCE = CURRENT_REMOTE_SSOT` / `LOCAL_WORKTREE_IS_NOT_SSOT` / `STALE_FEATURE_BRANCH_IS_NOT_FRESH_READ`)。既存の規則は記憶・会話要約・古い Issue 記述を禁じていたが、**手元の作業 branch の docs を現行ルールとして読むこと**を禁じていなかった。実際に #177 の作業 branch 上で #181 改訂前の issue_label_policy §7.3 を参照した事例が発生している。(4)同じく §6.5.4 へ **取得が途中で打ち切られた場合**の規則を追加した(`RETRIEVAL_TRUNCATED -> FRESH_READ_COMPLETE = NO`)。コメント一覧や snapshot の取得は件数が増えると打ち切られることがあり、途中までしか読んでいない状態を「読み終えた」と扱うと #157 が解決した stale state 事故と同型の見落としが起きる。latest state を再構成するまで assignment 判断を行わない。(5)§9.5 へ `OPPORTUNISTIC_FIX_FORBIDDEN` / `FIX_FIRST_ISSUE_LATER = FORBIDDEN` を追加した。scope 外の不具合はその場で直さず、duplicate check -> Issue 化 -> lifecycle とする。**P0 であっても「先に直して Issue は後」は認めない**(P0 は Issue を省略してよいという意味ではなく、Issue を作ったうえで §2.6.8 の割り込み手順で最優先に着手してよいという意味である)。Production の緊急停止操作は運用操作であり本節の対象ではない。(6)§2.6.9 に **人間の承認による限定的な policy 変更**を反映した。従来の「専用の WIP 管理 Issue を作らない」を、`CACHE_ONLY` / `READ_MODEL_IS_SSOT = NO` / `INDEX_ONLY` の集約 read model に限って認める形へ改めた。**`WIP_STATE_SSOT`(各 Issue の宣言 + 最新 snapshot)と「docs 側に現況表を置かない」は変更していない。** read model は SSoT の複製ではなく索引であり、不一致時は Issue snapshot が勝ち(`READ_MODEL_MISMATCH -> ISSUE_SNAPSHOTS_WIN -> REBUILD_REQUIRED`)、stale なら索引としても使わず全走査へフォールバックする。`DOMAIN_WIP_READ_MODEL != ASSIGNMENT_READ_BARRIER_BYPASS` を明記し、lock 取得の確定は必ず名指しされた Issue の宣言と snapshot を fresh に読んで行う。この例外は Issue #184 の発効をもって有効になり、それまでは tracking Issue を作成しない。**既存の Human Gate / merge 承認 / Production approval / exact ChangeSet approval / grouped release / release-blocker lifecycle / label 4 軸 / §2.6 の取得ゲートと解放条件はいずれも変更・緩和していない。** docs のみの変更であり、判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない |
| 2026-09-06 | §2 / §2.6 / §2.6.10 の発効状態を現況へ同期した(Issue #184)。2.6節は 2026-09-06 02:27 JST に人間の承認により発効しているが、本文には `DOMAIN_WIP_MODEL_ACTIVE = NO` /「まだ発効していない」という**発効前の記述が残っており、現況と矛盾していた**。`CURRENT_WIP_RULE = DOMAIN_WIP_RULE_V1` と `EFFECTIVE_FROM` へ更新し、§2 の案内も「2.6節が正本であり既に発効している」へ改めた(§2 の `MAX_CONCURRENT_CODE_WIP_PER_WORKER = 1` は 2.6 の R5 として維持される)。あわせて `ACTIVATION_STATE_SSOT = Issue #177 の最新の durable な activation 記録` を定め、**静的な文書を、変わりうる発効状態の唯一の根拠にしない**ことを明記した(試行の結果として人間の判断で §2 のルールへ戻ることもありうるため。2.6.10 の巻き戻し規定)。試行期間の終了は期間の経過だけでは成立せず、continue / amend / rollback の判断をもって確定することも明記した。**R1〜R6・取得ゲート・掲示書式・LOCK_LEVEL・解放条件・scope 拡大・P0 割り込み・main 追随・WIP の SSoT・周知項目はいずれも変更していない。** 変更履歴に残る発効前の記述は当時の記録であり、書き換えていない。コード・Production 挙動の変更なし |
