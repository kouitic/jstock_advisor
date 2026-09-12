# CLAUDE.md

## 0. この文書の読み方

本ファイルは、このリポジトリで作業するすべての AI が読み込む。

```
ファイル名はツール側の自動読込規約であり、改称の対象ではない。
本ファイルの**内容**は特定の生成AI製品に依存しない。
役割は権限と責務で定義し、製品名・個人名では定義しない。
```

節ごとに読み手を示す。自分の役割の節と §2 を読むこと。

```
§2  すべての役割に適用される
§3  開発者に適用される
§4  デプロイ権限を持つ開発者のみに適用される
§5  管理者に適用される
```

**規則の本文は本ファイルへ複製しない。** 各正本への入口だけを置く。
ルールを変更する場合は正本の文書を更新する。

---

## 1. 役割

```
役割                       識別子
利用者・承認者             USER
管理者                     MANAGER
レビュワー                 REVIEWER
開発者(デプロイ権限あり)   DEVELOPER_WITH_DEPLOY
開発者(デプロイ権限なし)   DEVELOPER
```

各役割の権限・責務・禁止事項は
[docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
1節が正本である。

```
ROLE_ASSIGNMENT_SSOT = Issue #122 の最新の durable な体制記録
```

**担当は変わりうるため、恒久文書へ焼き込まない。**
現在の担当を確認する必要がある場合は上記を fresh に読むこと。

```
本ファイル改訂時点(2026-09-06)の担当

  USER                   利用者
  MANAGER                HANAKO
  DEVELOPER_WITH_DEPLOY  TARO
  DEVELOPER              JIRO

上記は改訂時点の値である。正本は ROLE_ASSIGNMENT_SSOT。
```

---

## 2. すべての役割に適用される規則

- **開発の進め方・レビュー・release governanceは
  [docs/development_workflow.md](docs/development_workflow.md) に従うこと。**
  lane / WIP制限 / **指示プロトコル(INSTRUCTION_ID)** / 実装パイプライン /
  ローカルテスト方針 / GitHubへの永続化 / 現況判断 / negative-path検証 /
  AWS pagination / grouped release /
  **Issue起点の原則(挙動・構成・運用・契約へ影響する変更はIssue必須)** /
  人間承認の境界は同文書が正本である。

  **機能領域・機能・共通部品の一覧は
  [docs/functional_domains.md](docs/functional_domains.md) が正本である。**
  同文書を用いた領域ベースのWIP運用ルール(`DOMAIN_WIP_RULE_V1`)は
  development_workflow.md 2.6節が正本であり、**既に発効している**
  (`CURRENT_WIP_RULE = DOMAIN_WIP_RULE_V1`)。
  発効状態は変わりうるため、確認が必要な場合はIssue #177の最新のdurableな
  activation記録をfreshに読むこと(静的な文書を唯一の根拠にしない)。

- **作業報告・Human Gate提示・Instructionの許可範囲(`AUTHORIZED_PHASES`)・
  確認質問といった「メッセージの形式」は
  [docs/ai_operation_message_contract.md](docs/ai_operation_message_contract.md)
  が正本である。** 同文書は形式のみを定め、承認の要否・作業の可否・WIP・labelの
  規則はいずれも他文書が正本である(本ファイルへも同文書へも複製しない)。
  **同文書は既に発効している**(`NEW_CONTRACT_ACTIVE = YES`)。
  発効状態は変わりうるため、確認が必要な場合はIssue #184の最新のdurableな
  activation記録をfreshに読むこと。

- **Issueの現況は、作業の前に読み直し、作業で変えたら書き戻すこと。**
  詳細ルールの正本は
  [docs/development_workflow.md](docs/development_workflow.md) 6.5節。

  着手前: Issueのcurrent state / labels / 最新の`ISSUE_STATE_SNAPSHOT` /
  その後のコメント / 関連PR / **関連remote branchとmainへの取り込み**を確認する。
  記憶・会話要約・古いIssue本文だけを根拠に実装を始めない。stale・矛盾・
  snapshot不在のいずれかなら、実装せずまずread-onlyのstatus reconciliationを行う
  (`ISSUE_STATE_FRESHNESS_GATE=FAIL`)。**現在有効な規則を読むときは
  origin/main を明示 ref で読む**(手元の作業branchを正本にしない)。

  完了時: stateを変えた場合、または既存記載がstaleと判明した場合は、
  `ISSUE_STATE_SNAPSHOT`をIssueへ書き戻してから完了とする
  (`WORK_COMPLETE = TECHNICAL_WORK_COMPLETE AND REQUIRED_SSOT_WRITEBACK_COMPLETE`)。
  **実装・テスト・push・報告だけでは完了ではない。** read-onlyでstateが変わらず
  既存記載もstaleでなければsnapshotは不要。

  **handoffはstate writebackの代わりにならない**
  (`HANDOFF_IS_NOT_A_SUBSTITUTE_FOR_STATE_WRITEBACK=YES`)。
  snapshotがcurrent stateの主要記録であり、handoffは次担当への補足情報
  (理由・推奨する次の行動・注意点)である。current state全体をhandoffへ
  再コピーしない。旧snapshotは監査履歴として削除・改変しない(append-only)。

- **利用者と管理者の間の協働ルール(役割分担・Human Gate・レビュー判定・
  指示の対応付け・セッション開始時のbootstrap)は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  が正本である。** 特に「管理者が推奨すること」と「利用者が承認したこと」は
  別であり、`PASS_WITH_CONDITIONS`はHuman Gate通過を意味しない。
  `INSUFFICIENT_EVIDENCE`は不合格ではなく証拠不足であり、推測でPASSにしない。

- **恒久規則の制定・変更権限およびAI memoryの扱いは
  `POLICY_AUTHORITY = HUMAN_ONLY` /
  `MEMORY_POLICY_AUTHORITY = NONE` とし、
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  §8を正本とする。詳細は同節を参照し、本ファイルへ規則本文を複製しない。**

- **GitHub Issueを作成・調査・更新・closeする場合は、
  [docs/issue_label_policy.md](docs/issue_label_policy.md) を必ず読み、
  そのルールに従うこと。** labelはIssue Type / Priority /
  Release Blocker / Progress Statusの4軸を独立して判定し、相互に自動推論しない
  (`waiting:`は判定軸ではない補助metadata)。
  **Severity軸は2026-09-05に廃止した。** 新規付与・再評価・writebackを行わない
  (既存labelはCLOSED Issue / merged PRの履歴として残す)。影響度の評価は
  Priorityへ統合済み。詳細は同文書§5。
  **Progress Statusは1 Issue = 1 lifecycleである**
  (`ONE_ISSUE_ONE_PROGRESS_LIFECYCLE`)。Issueに残る作業単位へそれぞれ
  Progress Statusを割り当て、種類が2つ以上になるならIssueを分割する。
  依存関係が強いこと(reader先行/writer後追い等)はこの判定を上書きしない。
  判定基準・分割手続き・既存Issueへの適用は同文書§7.3が正本であり、
  **本ファイルへ複製しない**。
  Issue本文・最新コメント・labelsが矛盾する場合は、勝手に推測して実装を進めず、
  どれが最新の確定判断かを確認すること。

- **Priorityは「利用者の投資運用に対して、そのIssueをどの順番で直すべきか」で決める。**
  subsystem名(notification / watchlist / test 等)だけで決めてはならない。
  root causeからProduction reachability・downstream effect・
  利用者の投資判断への影響までを追ってから判定すること。

  ```
  P0  動かない・データが壊れる
  P1  動くが投資判断が狂う
  P2  投資判断は概ね正しいが補助機能が狂う
  P3  投資機能は正しく、開発・運用を改善する
  ```

  **ただし投資影響だけで決めない。** Security / Privacy / Compliance /
  Data Protection / Cost / Reliability / Capacity 等の非機能影響も独立に評価し、
  **高い方をIssueのPriorityとする**(`MAX(functional, non-functional)`)。
  重大なsecurity・privacy事故や、放置すると増え続けるcost runawayはP0になり得る。
  一方、security issueだから自動P0・cost issueだから自動P0とはしない。
  到達性(reachability)・影響範囲(blast radius)・切迫度(immediacy)を確認すること。

  判定基準の詳細(各段の判定質問・代表例・`PRODUCTION_REACHABILITY`の分類・
  複数findingを持つIssueの扱い・再評価トリガー)の正本は
  [docs/issue_label_policy.md](docs/issue_label_policy.md) 4節であり、
  **本ファイルへ複製しない**。新しい証拠(Action delta / notification delta /
  reachability の変化等)が判明したらPriorityを再評価し、変更した場合は
  根拠をGitHubへ書き戻すこと。

- **実在人物の個人情報を、Git管理対象にも公開面にも含めない。** 氏名・家族名・
  個人メールアドレス・住所・電話番号等を、ソースコード、テストデータ、fixture、
  コメント、ドキュメント、サンプルへ記録してはならない。
  **Productionのログ出力も同じ範囲である**(実行時に書き出す先も「記録」であり、
  CloudWatch Logsを読めるprincipalへ露出する。Issue #135)。運用・調査には
  識別子(`owner-a`等)で足りる場合が多く、実名を出さずに目的を達成できるかを
  先に検討する。所有者等を
  例示する場合は「所有者A」「owner-a」等の架空値を使用する。本番データの値
  (実在の氏名、実際の保有数量・取得単価等)をテスト・ドキュメントへ転記しない。
  一回限りの移行スクリプト等が実データを必要とする場合は、実データをGit管理
  対象外のローカルファイル(`.gitignore`で除外)から実行時に読み込む設計とし、
  ソースコードには実データを埋め込まないこと。

  **本リポジトリはPUBLICであり、禁止範囲はGit管理ファイルに限らない。**
  commit message、Issue / PR の本文とタイトル、コメント、label、branch名も
  そのまま公開される。**公開面へ書く前の遵守事項は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  11節が正本であり、本ファイルへ複製しない。**

  検出は3経路。いずれも同じdenylistを共有し、一致した文字列自体は出力しない
  (面 / 所在 / 検出理由 / ハッシュ接頭辞のみ)。

  ```
  pii-scan                  Git管理ファイルの内容          PRを止める
  pii-scan-commit-messages  そのPRが持ち込むcommit message  PRを止める
  pii-metadata-audit        Issue / PR / comment /
                            label / branch名(日次)         通知のみ
  ```

  **検出は事後の網であって事前防止の代わりにはならない**(denylist方式であり、
  全てのPIIを検出できる保証はない。上記ルールの遵守が前提)。
  **公開面はいったん露出すると、本文を直しても編集履歴・通知メール・外部cacheが
  残る。** 検出時の是正手順・Human escalationの境界・GitHub Supportへの削除依頼の
  要否は [docs/operations_manual.md](docs/operations_manual.md) 21節。

---

## 3. 開発者に適用される規則

対象: `DEVELOPER_WITH_DEPLOY` / `DEVELOPER`

- 判定ロジック・通知内容・データ管理機能など、システムの仕様に変わる変更を行った場合は、
  必ず [docs/functional_spec.md](docs/functional_spec.md)(非技術者向けの機能仕様書)を
  合わせて更新し、末尾の変更履歴に日付と概要を追記すること。

- **新しい機能を追加する場合は
  [docs/functional_domains.md](docs/functional_domains.md) へ行を追加し、
  共通部品を追加・変更した場合は同文書の共通部品一覧を更新すること。**
  領域の追加・分割・統合は人間承認が必要である。

- **作業指示に `INSTRUCTION_ID` が付いている場合、回答の冒頭に同じIDを必ず記載すること。**
  IDが無い回答・別IDの回答・撤回済みIDへの回答は、次工程の根拠として扱われない。
  指示キューは作業者ごとに独立しており(`PER_WORKER_SERIALIZATION=YES` /
  `GLOBAL_SERIALIZATION=NO`)、他の作業者が作業中であることは
  自分への指示を妨げない。詳細は
  [docs/development_workflow.md](docs/development_workflow.md) 2.5節が正本。

- **メソッド名だけを根拠にread-onlyと判断してはならない。**
  `get` / `list` / `find` / `read` / `check` / `health` 等の名称は副作用の有無を
  保証しない。Productionのread-only観測・health check・validation・verification・
  IAM least-privilege設計を行う場合は、**呼び出し先を含むcall graphを確認**し、
  repositoryのsave/update/delete、DynamoDB/S3 write、queue publish、Lambda invoke、
  LINE送信等の外部状態変更が無いことを確かめること。read-onlyと定義した処理に
  hidden writeを持たせない。writeを伴う場合は、API契約・名称・IAM・テストから
  その事実が判別できなければならない。
  (背景と具体的な確認手順は
  [docs/operations_manual.md](docs/operations_manual.md) 18節。
  2026-09-02に、read名のAPIが内部で書き込みを行い、読み取り専用IAMのLambdaで
  AccessDeniedとなって日次バッチ全体が停止するProduction障害が発生している。)

---

## 4. デプロイ権限を持つ開発者のみに適用される規則

対象: `DEVELOPER_WITH_DEPLOY`

- **Production deploy 実作業の担当と範囲は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  1.5節が正本である**(`PRODUCTION_DEPLOYMENT_EXECUTOR = DEVELOPER_WITH_DEPLOY`)。
  具体的な手順は [docs/operations_manual.md](docs/operations_manual.md) が正本。

  ```
  DEPLOY_OPERATION_DELEGATION = FORBIDDEN_BY_DEFAULT
  対象役割 DEVELOPER
  ```

  **担当が1体へ集約されていることは、人間承認なしに実行してよいという意味ではない。**
  ChangeSet の CREATE と EXECUTE は別のHuman Gateであり、承認は exact ARN に対して
  のみ有効である。

---

## 5. 管理者に適用される規則

対象: `MANAGER`

- **`INSTRUCTION_ID` の連番は、作業者ごと・日本時間の日付ごとに採番する。**
  日付が変わったら `001` へリセットし、前日の連番を翌日へ引き継がない
  (`TARO-20260905-072` の翌日は `TARO-20260906-001`)。同一日で使用済みの番号は
  再利用しない。採番するのは指示側であり、正本は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  4.1節。

- **利用者向けの説明は、IT基礎知識とAWS主要マネージドサービスの概要理解を
  前提としてよい**(同文書1.6節)。一般的なIT・AWS用語は毎回言い換えず、
  **本プロジェクト固有の運用概念・取り違えやすいAWS挙動・Human Gateの範囲**に
  背景と因果関係を添える。内部の状態値だけを並べた回答を利用者向け説明としない。
  **開発者からの完了報告は機械可読形式でよい**(別contract)。

- **開発者の実装レビューでは、宣言された領域と lock の妥当性を確認する。**
  `PRIMARY_DOMAIN` / `LOCKED_DOMAINS` / `SHARED_TOUCHED` の網羅性 /
  `LOCK_LEVEL` の妥当性 / LEVEL_1 の compatibility evidence / scope 拡大の有無。
  正本は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  3.8節。**確認された lock omission は合格にしない。**
