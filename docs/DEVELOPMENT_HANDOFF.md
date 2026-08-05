# VoxCraft辞書開発 引継ぎ（Phase 2最少実用版）

最終更新: 2026-08-02 / 対象: `0.10.0-beta.1`

## 到達点

Phase 1A（永続形式と互換移行）、Phase 1B（セッション固定スナップショット）、Phase 2の最少実用範囲を実装済みです。

- 正規データは `server/dictionaries/profiles/*.json` と `sets.json`。
- 旧 `userdict.json` は削除せず、初回だけ `common.json` へ移行する。
- 後勝ちプロファイル合成、単一パス最長一致、意味内容リビジョン、診断を実装した。
- WebSocket録音開始、REST再変換、音声復旧に `dictionarySetId` を通す。
- 録音セッションとキュー済み認識は不変スナップショットを保持する。
- 接続先URLごとのセット選択、適用中セット名、選択範囲／再変換からの1件登録を実装した。
- 1件登録は楽観的ロック、競合409、冪等処理、検証、`.json.bak`、atomic replaceを使う。

## 主要ファイル

- `server/dictionary_registry.py`: スキーマ、検証、セット合成、リビジョン、1件登録。
- `server/userdict.py`: 旧API互換とレジストリのファサード。
- `server/main.py`: 辞書REST APIとWebSocketのスナップショット固定。
- `server/dictionaries/README.md`: JSONスキーマと合成規則。
- `plugin/dict.ts`: カタログ／登録API、セット選択・クイック登録・旧一覧編集UI。
- `plugin/settings.ts`: URL別セット選択の永続化と設定画面。
- `plugin/main.ts`: 各操作へのセット伝播、適用中表示、登録フロー。
- `plugin/ws.ts`: `started` 辞書メタデータ。
- `.github/workflows/release.yml`: タグをビルドしBRAT用Release資産を作る。

## API・プロトコル

- `GET /dictionaries`: プロファイルとセットのカタログ。セットには `writableProfile`、`revision`、`profileRevisions`、診断を含む。
- `GET /dictionaries/{profile_id}`: 検証済みプロファイル本文。
- `POST /dictionaries/validate`: 保存なし検証。
- `POST /dictionaries/{profile_id}/entries`: `observed`, `output`, 任意の `expectedRevision`, `hotword`, `priority`, `note`。競合は409。
- `POST /dictionaries/{profile_id}/symbols`: `observed`, `output`, 任意の `expectedRevision`。記号語（単独チャンク一致）を1件追加する。置換と違いチャンク全体一致でしか効かないので1文字キーを許す。競合は409。
- `POST /reconvert`, `POST /recognize`: `dictionarySetId` を受け、辞書メタデータを返す。
- WebSocket `start`: `dictionarySetId` を受ける。`started` はセットID／名前／リビジョン／構成プロファイル／登録先／診断を返す。

`sets.json` の後ろのプロファイルほど具体的で、同じキーを上書きします。クイック登録先は
`writableProfile`。省略時は最後のプロファイルです。

## 検証コマンド

```powershell
# リポジトリルート
python server/test_dictionary_registry.py
.\server\.venv\Scripts\python.exe server/test_dictionary_session.py

# サーバーの全自己実行テスト
Get-ChildItem server -Filter 'test_*.py' | ForEach-Object {
  & .\server\.venv\Scripts\python.exe $_.FullName
  if ($LASTEXITCODE -ne 0) { throw "failed: $($_.Name)" }
}

# Obsidian型定義側の既知HistoryHandler不整合だけ除外して、自コードを型検査
.\plugin\node_modules\.bin\tsc.cmd --noEmit --skipLibCheck -p plugin/tsconfig.json

# リポジトリルートへBRAT成果物を同期する本番ビルド
cd plugin
$env:OBSIDIAN_PLUGIN_DIR = ".."
npm ci
npm run build
```

実機確認は `docs/BRAT_TESTING.md` を使います。

## 次に進める候補

Phase 2完全版として残している主項目:

1. JSON/CSV/TSVのインポート、エクスポート、dry-run差分表示。
2. プロファイル作成・複製・無効化、セット構成編集の管理UI。
3. 登録候補キュー（低信頼・未確定）と承認フロー、使用回数・最終使用日時。
4. プロファイル単位の履歴一覧とUIからのロールバック（現状は直前 `.bak` のみ）。
5. サーバー書き込みAPIの認証／Tailscale ACL前提の明文化。
6. 辞書精度評価用コーパスと、変更前後の認識・置換回帰レポート。

Phase 3候補は自動提案、共有辞書同期、評価指標に基づく昇格・整理です。まず実地検証で
「登録頻度」「セット切替頻度」「競合・誤置換」を記録してからUI範囲を決めるのが安全です。

## 判断上の注意

- 進行中セッションの辞書をホットリロードしない。不変性は再現性と長時間文字起こしの整合に必要。
- クイック登録で既存キーを黙って更新しない。変更UIは差分確認付きで別に設計する。
- 旧 `/dict` は共通辞書・記号語の互換編集用。ジャンル辞書の主管理UIへ拡張しない。
- BRATはプラグインしか配布しない。サーバー更新とのバージョン差を検証時に必ず確認する。
