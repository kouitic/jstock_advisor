# CLAUDE.md

- 判定ロジック・通知内容・データ管理機能など、システムの仕様に変わる変更を行った場合は、
  必ず [docs/functional_spec.md](docs/functional_spec.md)(非技術者向けの機能仕様書)を
  合わせて更新し、末尾の変更履歴に日付と概要を追記すること。

- **開発の進め方・レビュー・release governanceは
  [docs/development_workflow.md](docs/development_workflow.md) に従うこと。**
  lane / WIP制限 / **指示プロトコル(INSTRUCTION_ID)** / 実装パイプライン /
  ローカルテスト方針 / GitHubへの永続化 / 現況判断 / negative-path検証 /
  AWS pagination / grouped release /
  **Issue起点の原則(挙動・構成・運用・契約へ影響する変更はIssue必須)** /
  人間承認の境界は同文書が正本である。
  (詳細を本ファイルへ複製しない。ルールを変更する場合は同文書を更新する。)

  **機能領域・機能・共通部品の一覧は
  [docs/functional_domains.md](docs/functional_domains.md) が正本である。**
  新しい機能を追加する場合は同文書へ行を追加し、共通部品を追加・変更した
  場合は同文書の共通部品一覧を更新すること。領域の追加・分割・統合は
  人間承認が必要である。同文書を用いた領域ベースのWIP運用ルール
  (`DOMAIN_WIP_RULE_V1`)は development_workflow.md 2.6節が正本であり、
  **本ファイル作成時点では発効していない**(`DOMAIN_WIP_MODEL_ACTIVE = NO`。
  有効なWIPルールは同文書2節)。

- **作業指示に `INSTRUCTION_ID` が付いている場合、回答の冒頭に同じIDを必ず記載すること。**
  IDが無い回答・別IDの回答・撤回済みIDへの回答は、次工程の根拠として扱われない。
  指示キューは作業者ごとに独立しており(`PER_WORKER_SERIALIZATION=YES` /
  `GLOBAL_SERIALIZATION=NO`)、他の作業者が作業中であることは
  自分への指示を妨げない。詳細は
  [docs/development_workflow.md](docs/development_workflow.md) 2.5節が正本。

- **Issueの現況は、作業の前に読み直し、作業で変えたら書き戻すこと。**
  詳細ルールの正本は
  [docs/development_workflow.md](docs/development_workflow.md) 6.5節。

  着手前: Issueのcurrent state / labels / 最新の`ISSUE_STATE_SNAPSHOT` /
  その後のコメント / 関連PR / **関連remote branchとmainへの取り込み**を確認する。
  記憶・会話要約・古いIssue本文だけを根拠に実装を始めない。stale・矛盾・
  snapshot不在のいずれかなら、実装せずまずread-onlyのstatus reconciliationを行う
  (`ISSUE_STATE_FRESHNESS_GATE=FAIL`)。

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

- **`INSTRUCTION_ID` の連番は、作業者ごと・日本時間の日付ごとに採番する。**
  日付が変わったら `001` へリセットし、前日の連番を翌日へ引き継がない
  (`TARO-20260905-072` の翌日は `TARO-20260906-001`)。同一日で使用済みの番号は
  再利用しない。採番するのは指示側であり、正本は
  [docs/chatgpt_collaboration_protocol.md](docs/chatgpt_collaboration_protocol.md)
  4.1節。
  なお**ユーザー向けの説明は、IT基礎知識とAWS主要マネージドサービスの概要理解を
  前提としてよい**(同文書1.6節)。一般的なIT・AWS用語は毎回言い換えず、
  **本プロジェクト固有の運用概念・取り違えやすいAWS挙動・Human Gateの範囲**に
  背景と因果関係を添える。内部の状態値だけを並べた回答をユーザー向け説明としない。
  **作業AIからの完了報告は従来どおり機械可読形式でよい**(別contract)。

- **ユーザーとChatGPTの間の協働ルール(役割分担・Human Gate・レビュー判定・
  指示の対応付け・セッション開始時のbootstrap)は
  [docs/chatgpt_collaboration_protocol.md](docs/chatgpt_collaboration_protocol.md)
  が正本である。** 特に「ChatGPTが推奨すること」と「ユーザーが承認したこと」は
  別であり、`PASS_WITH_CONDITIONS`はHuman Gate通過を意味しない。
  `INSUFFICIENT_EVIDENCE`は不合格ではなく証拠不足であり、推測でPASSにしない。

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
  (同文書はAI非依存のリポジトリ運用ポリシーであり、本ファイルはその入口に過ぎない。
  ルールを変更する場合は同文書を更新する。)

- **Priorityは「ユーザーの投資運用に対して、そのIssueをどの順番で直すべきか」で決める。**
  subsystem名(notification / watchlist / test 等)だけで決めてはならない。
  root causeからProduction reachability・downstream effect・
  ユーザーの投資判断への影響までを追ってから判定すること。

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

- **実在人物の個人情報をGit管理対象へ含めない。** 氏名・家族名・個人メール
  アドレス・住所・電話番号等を、ソースコード、テストデータ、fixture、コメント、
  ドキュメント、サンプル、コミットメッセージへ記録してはならない。所有者等を
  例示する場合は「所有者A」「owner-a」等の架空値を使用する。本番データの値
  (実在の氏名、実際の保有数量・取得単価等)をテスト・ドキュメントへ転記しない。
  一回限りの移行スクリプト等が実データを必要とする場合は、実データをGit管理
  対象外のローカルファイル(`.gitignore`で除外)から実行時に読み込む設計とし、
  ソースコードには実データを埋め込まないこと。CIのPIIスキャン
  (`scripts/scan_for_pii.py`、`.github/workflows/ci.yml`の`pii-scan`ジョブ)が
  既知の実在人物名を検知した場合はビルドを失敗させる(denylist方式であり、
  全てのPIIを検出できる保証はない。上記ルールの遵守が前提)。
