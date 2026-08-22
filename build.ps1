$ErrorActionPreference = 'Stop'
$env:GREMLINS_BUILD_NAME = 'Gremlins_assistance'
$exePath = Join-Path 'dist' ($env:GREMLINS_BUILD_NAME + '.exe')
$resolvedExePath = if (Test-Path -LiteralPath $exePath) {
    (Resolve-Path -LiteralPath $exePath).Path
} else {
    Join-Path (Resolve-Path -LiteralPath 'dist').Path ($env:GREMLINS_BUILD_NAME + '.exe')
}
Get-Process | Where-Object { $_.Path -eq $resolvedExePath } | Stop-Process -Force
if (Test-Path -LiteralPath $exePath) {
    Remove-Item -LiteralPath $exePath -Force
}
python -m PyInstaller --clean --noconfirm Gremlins_assistance.spec
Set-Content -LiteralPath 'dist\latest.txt' -Value $exePath -Encoding ascii
Write-Host "Built $exePath"
