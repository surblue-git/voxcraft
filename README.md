# VoxCraft — 日本語長文 音声入力ツール

自宅PCで動くローカルWhisperを使い、Obsidian上で日本語の長文を「話すだけ」で書くためのツール。
Windows(Desktop) と Android(Obsidian Mobile) の両方で同じプラグインが動く。

## 特徴（設計方針）

- **無料・ローカル認識**: 自宅PCで faster-whisper（既定 `kotoba-tech/kotoba-whisper-v2.0-faster`）。クラウド不要。
- **沈黙で切れない待機**: 停止はユーザー操作か「入力終了」の発話のみ。どれだけ黙っても待機は解除されない。
- **擬似リアルタイム**: 息継ぎ（既定0.8秒）ごとに確定し、カーソル位置へ追記。Whisperが句読点を自動付与。
- **勝手な半角スペースなし**: 日本語と英数字の間の半角スペースを後処理で除去（英単語間の空白は保持）。
- **音声コマンド**: 「取り消し」「改行」「AをBに修正」「変換戻し」「入力終了」。誤爆防止にプレフィックス語を設定可能。
- **変換戻し**: 直前の入力の読みを復元し、Google CGI API（無料）で文節ごとの変換候補を取得。手／音声（「3番」）で選択。

## 構成

```
voxcraft/
├─ server/   Python 認識サーバー（自宅PC常駐）
└─ plugin/   Obsidian プラグイン（録音・挿入・UI）
```

クライアント（プラグイン）は録音とUIだけで軽い。認識はすべてサーバー側。

## セットアップ

### 1. 認識サーバー（自宅PC）

Python 3.10–3.12 推奨（faster-whisper / onnxruntime のホイール都合。3.13+ は未対応の場合あり）。

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
.\run.ps1
```

初回起動時にモデルを自動ダウンロード。GPUがあれば `$env:VOXCRAFT_DEVICE="cuda"; $env:VOXCRAFT_COMPUTE_TYPE="float16"`。
動作確認: ブラウザで `http://localhost:8760/health` が `{"ready": true, ...}` を返せばOK。

### 2. Obsidian プラグイン

#### 方法A: BRAT でインストール（推奨・更新も自動）

1. Obsidianのコミュニティプラグインで **BRAT**（Obsidian42 - BRAT）をインストールして有効化。
2. コマンドパレット →「BRAT: Add a beta plugin for testing」。
3. リポジトリに `surblue-git/voxcraft` を入力して追加。
4. **リポジトリがプライベートの場合**は、BRATの設定で GitHub の Personal Access Token（`repo` 読み取り権限）を登録しておく。公開リポジトリなら不要。
5. インストール後、「設定 → コミュニティプラグイン」でVoxCraftを有効化。

以降、新しいリリースを出すとBRATが自動で更新してくれる。

#### 方法B: 手動ビルドで配置

```powershell
cd plugin
npm install
# 出力先を自分のVaultに合わせる（既定は esbuild.config.mjs 参照）
$env:OBSIDIAN_PLUGIN_DIR = "<Vault>/.obsidian/plugins/voxcraft"
npm run build
```

どちらの方法でも、Obsidianの「設定 → コミュニティプラグイン」でVoxCraftを有効化し、
設定タブで **認識サーバー URL** を指定する（Desktopは `ws://localhost:8760/ws`）。

#### リリースの出し方（BRAT更新用）

`plugin/manifest.json` の `version` を上げ、同じ番号のタグを push すると、
GitHub Actions が自動でビルドしてリリースを作る（`.github/workflows/release.yml`）。

```powershell
git tag 0.2.0
git push origin 0.2.0
```

### 3. Android（Obsidian Mobile）

1. PCとスマホに [Tailscale](https://tailscale.com/) を入れて同じネットワークに。
2. サーバーURLを `ws://<PCのTailscale IP>:8760/ws` に設定。
3. あとはDesktopと同じ。認識はPCで走るので端末性能に依存しない。

## 使い方

- リボンのマイクアイコン、またはコマンド「音声入力の開始/停止」で録音トグル。
- 話すと息継ぎごとにカーソル位置へ文章が追記される。
- 「入力終了」と言うか、もう一度アイコンを押すと停止。
- 誤変換は「〇〇を△△に修正」。言い直しの候補が欲しいときは「変換戻し」→候補を数字/クリック/「3番」で選択。

## 主な設定（環境変数, `server/config.py`）

| 変数 | 既定 | 説明 |
|---|---|---|
| `VOXCRAFT_MODEL` | kotoba-whisper-v2.0-faster | 認識モデル。`large-v3` / `small` 等に変更可 |
| `VOXCRAFT_DEVICE` | cpu | `cuda` でGPU |
| `VOXCRAFT_SILENCE_SEC` | 0.8 | 息継ぎ確定の無音長（秒） |
| `VOXCRAFT_MAX_CHUNK_SEC` | 12.0 | チャンク強制確定の上限（秒） |
| `VOXCRAFT_STRIP_SPACE` | 1 | 日本語/英数字間スペース除去 |
| `VOXCRAFT_SYMBOLS` | 1 | 記号読み上げ変換 |
| `VOXCRAFT_GOOGLE_CGI` | 1 | 変換戻しにGoogle CGI APIを使う |

## テスト

```powershell
cd server
python test_postproc.py     # 後処理の単体テスト（依存なしで実行可）
```

## 既知の割り切り

- 完全な文字単位リアルタイムではなく「息継ぎごとに1〜2秒遅れで確定」。
- Google CGI API は無料・非公式（個人利用前提）。オフライン時は読みのみで候補は限定的。
- 漢字→読みの復元にはサーバーに `sudachipy` が必要（無い場合はカタカナのみ読み化）。
```
