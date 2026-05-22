$ErrorActionPreference = "Stop"

$localPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$python = Get-Command python -ErrorAction SilentlyContinue
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue

if (Test-Path -LiteralPath $localPython) {
    & $localPython paper_ai.py @args
    exit $LASTEXITCODE
}

if ($python) {
    & $python.Source paper_ai.py @args
    exit $LASTEXITCODE
}

if ($pyLauncher) {
    & $pyLauncher.Source paper_ai.py @args
    exit $LASTEXITCODE
}

Write-Error "Python hittades inte. Installera Python 3.10+ eller kor med en full sokvag till python.exe."
