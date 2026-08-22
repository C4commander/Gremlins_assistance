param(
    [int]$IntervalSeconds = 2
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateFile = Join-Path $root '.watch-build-state'
$patterns = @('*.py', '*.spec', 'requirements.txt')

function Get-SourceStamp {
    $files = Get-ChildItem -LiteralPath $root -File |
        Where-Object { $patterns -contains $_.Name -or $_.Extension -eq '.py' -or $_.Extension -eq '.spec' }
    $latest = $files | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if (-not $latest) {
        return 'none'
    }
    return ('{0:o}' -f $latest.LastWriteTimeUtc)
}

function Build-App {
    Write-Host ''
    Write-Host 'Building exe...'
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
    Set-Content -LiteralPath (Join-Path $root 'dist\latest.txt') -Value $exePath -Encoding ascii
    Get-SourceStamp | Set-Content -LiteralPath $stateFile -Encoding ascii
    Write-Host "Build finished: $exePath"
}

Build-App

while ($true) {
    Start-Sleep -Seconds $IntervalSeconds
    $current = Get-SourceStamp
    $last = if (Test-Path $stateFile) { Get-Content -LiteralPath $stateFile -Raw } else { '' }
    if ($current -ne $last) {
        Build-App
    }
}
