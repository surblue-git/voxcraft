# VoxCraft 認識サーバー 起動スクリプト（Windows / PowerShell）
#
# 初回:
#   cd server
#   python -m venv .venv
#   .\.venv\Scripts\Activate.ps1
#   pip install -r requirements.txt
# 以降:
#   .\run.ps1
#
# 環境変数で挙動を変えられる（config.py 参照）。例:
#   $env:VOXCRAFT_DEVICE = "cuda"; $env:VOXCRAFT_COMPUTE_TYPE = "float16"
#   $env:VOXCRAFT_MODEL  = "large-v3"

$ErrorActionPreference = "Stop"

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    & .\.venv\Scripts\Activate.ps1
}

$h = if ($env:VOXCRAFT_HOST) { $env:VOXCRAFT_HOST } else { "0.0.0.0" }
$p = if ($env:VOXCRAFT_PORT) { $env:VOXCRAFT_PORT } else { "8760" }

# 日本語の診断ログが cp932 で文字化けしないようにする（autostart.cmd と同じ）。
$env:PYTHONIOENCODING = "utf-8"

# 注意: こちらはログファイルを作らない。出力はこのコンソールにだけ出る。
# server.log に残したいときは autostart.cmd を使うこと（起動ごとに切り捨て）。
python -m uvicorn main:app --host $h --port $p
