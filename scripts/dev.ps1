# Developer entry point for the Text2SQL project.
#
#   powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 check
#   powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 test
#   powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 ask "按渠道统计用户数"
#   powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 acceptance
#
# It always uses the project virtual environment (.venv) and, when running
# inside the DSH file sandbox, routes temporary directories into the workspace
# and loads scripts/sandbox_site/sitecustomize.py (the sandbox denies writes to
# directories created by tempfile.mkdtemp / mode-0700 mkdir).

param(
    [Parameter(Position = 0)]
    [ValidateSet("check", "test", "acceptance", "ask", "catalog", "validate", "install", "shell")]
    [string]$Task = "check",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "找不到虚拟环境 $Python，请先运行 scripts/setup_env.ps1"
}

# Keep temp files inside the workspace so the file sandbox can write to them.
$TempRoot = Join-Path $RepoRoot ".tmp"
New-Item -ItemType Directory -Force $TempRoot | Out-Null
$env:TEMP = $TempRoot
$env:TMP = $TempRoot
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = Join-Path $RepoRoot "scripts\sandbox_site"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Push-Location $RepoRoot
try {
    switch ($Task) {
        "check" { & $Python -m text2sql check @Rest }
        "catalog" { & $Python -m text2sql catalog @Rest }
        "validate" { & $Python -m text2sql validate @Rest }
        "ask" { & $Python -m text2sql ask @Rest }
        "test" { & $Python -m pytest -q @Rest }
        "acceptance" { & $Python -m pytest tests/test_acceptance.py -v @Rest }
        "install" { & $Python -m pip install @Rest }
        "shell" { & $Python @Rest }
    }
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
