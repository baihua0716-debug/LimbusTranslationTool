param(
  [int]$Port = 8765
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
python .\app.py --port $Port
