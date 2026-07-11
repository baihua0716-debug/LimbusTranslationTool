param(
  [string]$Name = "LimbusTranslationTool"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

python -m pip install --upgrade pyinstaller pywebview

if (Test-Path ".\build") {
  Remove-Item ".\build" -Recurse -Force
}

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name $Name `
  --hidden-import webview.platforms.edgechromium `
  --add-data "static;static" `
  .\app.py

New-Item -ItemType Directory -Force -Path ".\release" | Out-Null
if (Test-Path ".\release\user_data") {
  Remove-Item ".\release\user_data" -Recurse -Force
}
Copy-Item ".\dist\$Name.exe" ".\release\$Name.exe" -Force
Copy-Item ".\README_PLAYER.md" ".\release\README.txt" -Force

Compress-Archive -Path ".\release\$Name.exe", ".\release\README.txt" -DestinationPath ".\release\$Name.zip" -Force

Write-Host ""
Write-Host "Build complete: release\$Name.zip"
Write-Host "Players can unzip it and double-click $Name.exe."
