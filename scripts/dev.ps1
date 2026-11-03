<#
.SYNOPSIS
    Starts the local Abteilo development stack on Windows with one command.

.DESCRIPTION
    1. Starts PostgreSQL in WSL and keeps WSL awake while the stack runs.
    2. Starts Ollama if it is not already serving, and checks the required models.
    3. Applies database migrations (a no-op when already at head).
    4. Starts the API (uvicorn --reload, :8000) and the web app (Vite, :5173) in this terminal.
    5. Opens the browser. Press Enter (or Ctrl+C) here to stop everything this script started.

    Configuration comes from ../.env, exactly as for a manual start. PostgreSQL and Ollama are left
    running when you stop; only the API, the web app and the WSL keep-alive are stopped.

.EXAMPLE
    .\scripts\dev.ps1
    .\scripts\dev.ps1 -Seed          # first run on an empty database: create the BINA demo company
    .\scripts\dev.ps1 -ResetDemoPassword   # sign-in fails: set all demo accounts to ABTEILO_DEMO_PASSWORD
    .\scripts\dev.ps1 -NoBrowser -WslDistro Ubuntu-24.04
#>
param(
    [string]$WslDistro = "Ubuntu",
    [switch]$Seed,
    [switch]$ResetDemoPassword,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root     = Split-Path -Parent $PSScriptRoot
$Backend  = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Python   = Join-Path $Backend ".venv\Scripts\python.exe"
$Models   = @("qwen2.5:7b", "bge-m3")

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "    $text" -ForegroundColor Green }
function Fail($text) { Write-Host "    $text" -ForegroundColor Red; exit 1 }

function Wait-Http($url, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-Tree($proc) {
    if ($proc -and -not $proc.HasExited) { & taskkill.exe /T /F /PID $proc.Id 2>$null | Out-Null }
}

# --- prerequisites -------------------------------------------------------------------------------
Step "Checking prerequisites"
if (-not (Test-Path (Join-Path $Root ".env"))) { Fail "Missing .env. Copy .env.example to .env and fill in the database URLs." }
if (-not (Test-Path $Python)) {
    Fail "Missing backend venv. Run: cd backend; python -m venv .venv; .venv\Scripts\pip install -e "".[dev]"""
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Fail "npm not found. Install Node.js LTS." }
if (-not (Test-Path (Join-Path $Frontend "node_modules"))) {
    Step "Installing frontend dependencies (first run)"
    Push-Location $Frontend
    try { & npm install; if ($LASTEXITCODE -ne 0) { Fail "npm install failed." } } finally { Pop-Location }
}
Ok "ok"

$started = @()
try {
    # --- PostgreSQL in WSL ------------------------------------------------------------------------
    Step "Starting PostgreSQL in WSL ($WslDistro)"
    & wsl.exe -d $WslDistro -u root -- service postgresql start | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Could not start PostgreSQL in WSL distro '$WslDistro'." }
    # WSL shuts down when idle, which takes PostgreSQL with it; hold it open while the stack runs.
    $keepAlive = Start-Process wsl.exe -ArgumentList "-d", $WslDistro, "--", "sleep", "infinity" `
                                       -WindowStyle Hidden -PassThru
    $started += $keepAlive
    Ok "running (WSL kept awake)"

    # --- Ollama -----------------------------------------------------------------------------------
    Step "Checking Ollama"
    if (-not (Wait-Http "http://127.0.0.1:11434/api/tags" 1)) {
        $ollama = Get-Command ollama -ErrorAction SilentlyContinue
        if (-not $ollama) { Fail "Ollama is not installed or not on PATH (https://ollama.com)." }
        Start-Process $ollama.Source -ArgumentList "serve" -WindowStyle Hidden | Out-Null
        if (-not (Wait-Http "http://127.0.0.1:11434/api/tags" 30)) { Fail "Ollama did not start." }
    }
    $installed = (Invoke-RestMethod "http://127.0.0.1:11434/api/tags").models | ForEach-Object { $_.name }
    foreach ($m in $Models) {
        if (-not ($installed | Where-Object { $_ -eq $m -or $_ -eq "$m`:latest" })) {
            Write-Host "    Model $m is missing; the assistant will not work until you run: ollama pull $m" -ForegroundColor Yellow
        }
    }
    Ok "serving"

    # --- database migrations (and optional demo seed) ---------------------------------------------
    Push-Location $Backend
    try {
        Step "Applying database migrations"
        & $Python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { Fail "Migrations failed (is the database reachable with the URLs in .env?)." }
        if ($Seed) {
            Step "Seeding the demo company"
            & $Python -m abteilo.seed
        }
        if ($ResetDemoPassword) {
            Step "Setting every demo account's password to ABTEILO_DEMO_PASSWORD from .env"
            & $Python -m abteilo.reset_password --demo
            if ($LASTEXITCODE -ne 0) { Fail "Password reset failed." }
        }
    } finally { Pop-Location }

    # --- API and web app (both log into this terminal, e.g. the VS Code terminal) ------------------
    Step "Starting the API on http://127.0.0.1:8000"
    $api = Start-Process $Python -WorkingDirectory $Backend -NoNewWindow -PassThru `
        -ArgumentList "-m", "uvicorn", "abteilo.main:app", "--reload", "--host", "127.0.0.1", "--port", "8000"
    $started += $api
    if (-not (Wait-Http "http://127.0.0.1:8000/readyz" 60)) { Fail "The API did not become ready; see its log above." }
    Ok "ready"

    Step "Starting the web app on http://localhost:5173"
    $web = Start-Process cmd.exe -WorkingDirectory $Frontend -NoNewWindow -PassThru -ArgumentList "/c", "npm run dev"
    $started += $web
    if (-not (Wait-Http "http://localhost:5173" 60)) { Fail "The web app did not start; see its log above." }
    Ok "ready"

    if (-not $NoBrowser) { Start-Process "http://localhost:5173" }

    Write-Host ""
    Write-Host "Abteilo is running:  http://localhost:5173" -ForegroundColor Green
    Write-Host "Demo sign-in (BINA GmbH). Password: the ABTEILO_DEMO_PASSWORD value in .env" -ForegroundColor Green
    Write-Host "  owner@bina.example          Owner (company console, all departments)"
    Write-Host "  lena.weber@bina.example     Engineering Lead"
    Write-Host "  jonas.becker@bina.example   Mechanical Engineer"
    Write-Host "  sara.koch@bina.example      Finance Analyst"
    Write-Host "  mila.hoffmann@bina.example  HR Specialist"
    Write-Host "  tim.schulz@bina.example     IT Administrator"
    Write-Host "  eva.richter@bina.example    Security Officer"
    Write-Host ""
    Write-Host "Sign-in rejected? Stop, then run: .\run-dev.cmd -ResetDemoPassword"
    Write-Host ""
    Write-Host "Keep this terminal open while you use the app. API and web logs follow below. Press Enter (or Ctrl+C) to stop the API, the web app and the WSL keep-alive."
    [void](Read-Host)
}
finally {
    Step "Stopping"
    foreach ($p in $started) { Stop-Tree $p }
    Ok "API and web app stopped. PostgreSQL and Ollama are left running."
}
