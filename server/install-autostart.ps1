# VoxCraft 認識サーバーを Windows ログオン時に自動起動する（登録／解除）。
#
#   .\install-autostart.ps1            # 登録
#   .\install-autostart.ps1 -Uninstall # 解除
#   .\install-autostart.ps1 -Status    # 現在の状態を表示
#
# 管理者権限は不要。スタートアップフォルダにショートカットを置くだけなので、
# 解除はショートカットを消すだけ（システム設定は一切変更しない）。

param(
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs = Join-Path $here "autostart.vbs"
$startup = [Environment]::GetFolderPath("Startup")
$link = Join-Path $startup "VoxCraft ASR Server.lnk"

if ($Status) {
    if (Test-Path $link) {
        Write-Host "自動起動: 登録済み"
        Write-Host "  ショートカット: $link"
    } else {
        Write-Host "自動起動: 未登録"
    }
    $port = if ($env:VOXCRAFT_PORT) { $env:VOXCRAFT_PORT } else { "8760" }
    $listening = (netstat -ano | Select-String ":$port\s" | Select-String "LISTENING")
    if ($listening) {
        Write-Host "サーバー: 起動中 (port $port)"
    } else {
        Write-Host "サーバー: 停止中 (port $port)"
    }
    return
}

if ($Uninstall) {
    if (Test-Path $link) {
        Remove-Item $link -Force
        Write-Host "自動起動を解除しました: $link"
    } else {
        Write-Host "自動起動は登録されていません。"
    }
    return
}

if (-not (Test-Path $vbs)) {
    throw "autostart.vbs が見つかりません: $vbs"
}

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = "wscript.exe"
$sc.Arguments = '"{0}"' -f $vbs
$sc.WorkingDirectory = $here
$sc.Description = "VoxCraft 認識サーバーをログオン時にバックグラウンド起動する"
$sc.Save()

Write-Host "自動起動を登録しました。次回ログオンから有効です。"
Write-Host "  ショートカット: $link"
Write-Host "  起動対象      : $vbs"
Write-Host ""
Write-Host "今すぐ起動する場合: .\autostart.vbs をダブルクリック（またはこの後の案内どおり）"
Write-Host "解除する場合      : .\install-autostart.ps1 -Uninstall"
