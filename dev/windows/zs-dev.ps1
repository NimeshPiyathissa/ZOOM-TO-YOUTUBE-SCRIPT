<#
.SYNOPSIS
  Local Windows launcher for the Zoom-to-YouTube dashboard + EduMix (MJPhysics) page.

.DESCRIPTION
  Runs the SAME app from this repository on 127.0.0.1 (no VPS, no WSL, no AWS). A real Chrome/Edge window opens on
  your desktop for the MJPhysics site; you sign in there yourself (phone number + OTP). The Oxblood EduMix page
  mirrors that window and shows live status. EduMix capture / YouTube broadcasting stay DISABLED.

  Everything lives under  %LOCALAPPDATA%\ZoomToYT-Dev  (override with ZS_DEV_HOME): virtualenv, database, logs and
  the isolated browser profile. Nothing is written into the git repository and no production setting is touched.

.EXAMPLE
  .\zs-dev.cmd setup        # one time: virtualenv + dependencies
  .\zs-dev.cmd start -Open  # start the dashboard and open it in your default browser
  .\zs-dev.cmd status | stop | restart | logs | doctor | test
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet("help", "setup", "start", "stop", "restart", "status", "logs", "doctor", "reset-password", "reset-session", "test", "open", "clean")]
  [string]$Command = "help",
  [int]$Port = 0,               # 0 = first free port from 8765
  [int]$ChromePort = 9223,      # DevTools port of the local browser window (127.0.0.1 only)
  [switch]$Open,                # start: open the dashboard in your default browser
  [switch]$Follow,              # logs: keep following
  [switch]$Force                # reset-session / clean: skip the confirmation
)

$ErrorActionPreference = "Stop"
$Repo   = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Home_  = if ($env:ZS_DEV_HOME) { $env:ZS_DEV_HOME } else { Join-Path $env:LOCALAPPDATA "ZoomToYT-Dev" }
$Venv   = Join-Path $Home_ "venv"
$VPy    = Join-Path $Venv "Scripts\python.exe"
$Server = Join-Path $PSScriptRoot "dev_server.py"
$Run    = Join-Path $Home_ "run"
$StateF = Join-Path $Run "state.json"
$LogDir = Join-Path $Home_ "logs"
$Profile = Join-Path $Home_ "chrome-profiles"

function Say($m)  { Write-Host $m }
function Ok($m)   { Write-Host ("  [OK] " + $m) -ForegroundColor Green }
function Warn($m) { Write-Host ("  [!!] " + $m) -ForegroundColor Yellow }
function Fail($m) { Write-Host ("  [XX] " + $m) -ForegroundColor Red }

function Get-State {
  if (-not (Test-Path $StateF)) { return $null }
  try { return (Get-Content $StateF -Raw | ConvertFrom-Json) } catch { return $null }
}
function Test-Pid($p) { if (-not $p) { return $false }; return [bool](Get-Process -Id $p -ErrorAction SilentlyContinue) }
function Test-Health($port) {
  try { $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/health" -f $port) -UseBasicParsing -TimeoutSec 3; return ($r.StatusCode -eq 200) } catch { return $false }
}
function Test-Running { $s = Get-State; return ($s -and (Test-Pid $s.pid)) }

function Find-Python {
  # prefer a real 3.10+ interpreter; the py launcher first, then PATH
  foreach ($c in @(@("py", "-3.12"), @("py", "-3.11"), @("py", "-3.10"), @("py", "-3"), @("python"))) {
    try {
      $exe = $c[0]; $a = @(); if ($c.Count -gt 1) { $a = $c[1..($c.Count - 1)] }
      $v = & $exe @a -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
      if ($LASTEXITCODE -eq 0 -and $v -match "^3\.(\d+)$" -and [int]$Matches[1] -ge 10) {
        $path = & $exe @a -c "import sys;print(sys.executable)" 2>$null
        if ($path) { return $path.Trim() }
      }
    } catch { }
  }
  return $null
}

function Invoke-Py {
  param([string[]]$PyArgs)
  $env:PYTHONUNBUFFERED = "1"; $env:PYTHONIOENCODING = "utf-8"; $env:ZS_DEV_HOME = $Home_
  # show the program's output on the console (Out-Host) so only the exit code is returned to the caller
  & $VPy $Server @PyArgs | Out-Host
  return $LASTEXITCODE
}

function Cmd-Help {
  Get-Help $PSCommandPath -Detailed | Out-String | Write-Host
  Say @"
Commands
  setup           one time: create the virtualenv under $Home_ and install dependencies
  start [-Open]   start the dashboard on 127.0.0.1 (prints the URL; -Open opens it in your default browser)
  stop            stop the dashboard and close ONLY the EduMix browser window (your own Chrome is never touched)
  restart         stop + start
  status          is it running? URL, browser window, ports
  logs [-Follow]  show (or follow) the redacted diagnostic log
  doctor          run diagnostics (dependencies, Chrome, ports, capture flag, paths)
  reset-password  generate a new local admin password (shown once)
  reset-session   forget the saved MJPhysics login (deletes the isolated browser profile; you will log in again)
  test            run the automated test-suite with the virtualenv
  open            open the running dashboard in your default browser
  clean           remove everything under $Home_ (asks first)
"@
}

function Cmd-Setup {
  Say "Setting up the local environment in $Home_"
  $py = Find-Python
  if (-not $py) { Fail "Python 3.10+ was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run setup again."; exit 1 }
  Ok "Python: $py"
  New-Item -ItemType Directory -Force -Path $Home_, $LogDir, $Run | Out-Null
  if (-not (Test-Path $VPy)) { & $py -m venv $Venv; if ($LASTEXITCODE -ne 0) { Fail "could not create the virtualenv"; exit 1 } }
  Ok "virtualenv: $Venv"
  & $VPy -m pip install --disable-pip-version-check --quiet --upgrade pip
  Say "Installing dependencies (a few minutes the first time)..."
  & $VPy -m pip install --disable-pip-version-check --quiet -r (Join-Path $Repo "dashboard\requirements-dev.txt")
  if ($LASTEXITCODE -ne 0) { Fail "pip install failed - see the messages above"; exit 1 }
  Ok "dependencies installed"
  $chrome = $null
  foreach ($p in @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")) {
    if (Test-Path $p) { $chrome = $p; break }
  }
  if ($chrome) { Ok "browser: $chrome" } else { Warn "Chrome/Edge not found - install Google Chrome or set ZS_CHROME_PATH" }
  Say "`nNext:  .\zs-dev.cmd start -Open"
}

function Cmd-Start {
  if (-not (Test-Path $VPy)) { Warn "Not set up yet - running setup first."; Cmd-Setup }
  if (Test-Running) { $s = Get-State; Ok ("already running: " + $s.url); if ($Open) { Start-Process $s.url }; return }
  New-Item -ItemType Directory -Force -Path $LogDir, $Run | Out-Null
  # create the admin login on first start (the password is printed here, once - the server never prints it)
  $rc = Invoke-Py @("--init-admin", "--chrome-port", $ChromePort)
  if ($rc -ne 0) { Fail "could not initialise the local admin user"; exit 1 }
  $env:PYTHONUNBUFFERED = "1"; $env:PYTHONIOENCODING = "utf-8"; $env:ZS_DEV_HOME = $Home_
  $out = Join-Path $LogDir "server.out"; $err = Join-Path $LogDir "server.err"
  # the repo path may contain spaces: quote the script path for Start-Process
  $argList = @(('"' + $Server + '"'), "--serve", "--port", $Port, "--chrome-port", $ChromePort)
  $null = Start-Process -FilePath $VPy -ArgumentList $argList -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
  Say "Starting the dashboard..."
  $s = $null
  for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    $s = Get-State
    if ($s -and (Test-Pid $s.pid) -and (Test-Health $s.port)) { break }
    $s = $null
  }
  if (-not $s) {
    Fail "The dashboard did not come up. Last lines of its error output:"
    if (Test-Path $err) { Get-Content $err -Tail 15 | ForEach-Object { Say ("    " + $_) } }
    Say "Run  .\zs-dev.cmd doctor  for diagnostics."
    exit 1
  }
  Say ""
  Ok ("Dashboard running:  " + $s.url)
  Say "     Log in as 'admin' (password printed above on first start; 'reset-password' makes a new one)."
  Say "     Open the EduMix page, paste your MJPhysics class URL, press Open class."
  Say "     A Chrome window with the real site opens - sign in THERE (phone number, then OTP). Capture/YouTube stay disabled."
  if ($Open) { Start-Process $s.url }
}

function Cmd-Stop {
  $s = Get-State
  if ($s -and (Test-Pid $s.pid)) {
    & taskkill.exe /PID $s.pid /T /F | Out-Null     # the server and its children only
    Ok "dashboard stopped (pid $($s.pid))"
  } else { Say "  dashboard was not running" }
  Remove-Item $StateF -ErrorAction SilentlyContinue
  if (Test-Path $VPy) { $null = Invoke-Py @("--stop-browser", "--chrome-port", $ChromePort) }
}

function Cmd-Status {
  $s = Get-State
  if ($s -and (Test-Pid $s.pid)) {
    $h = Test-Health $s.port
    if ($h) { Ok ("running: " + $s.url + "   (pid $($s.pid))") } else { Warn "process is up but /health is not answering - see 'logs'" }
    $dt = $false; try { $null = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/json/version" -f $s.chrome_port) -UseBasicParsing -TimeoutSec 2; $dt = $true } catch { }
    if ($dt) { Ok ("EduMix browser window: open (DevTools 127.0.0.1:{0})" -f $s.chrome_port) } else { Say ("  EduMix browser window: not open (opens when you press 'Open class')") }
  } else { Say "  not running   (start it with: .\zs-dev.cmd start -Open)" }
  Say "  home: $Home_"
}

function Cmd-Logs {
  $f = Join-Path $LogDir "dashboard.log"
  if (-not (Test-Path $f)) { Say "no log yet ($f)"; return }
  if ($Follow) { Get-Content $f -Tail 40 -Wait } else { Get-Content $f -Tail 80 }
}

function Cmd-Doctor {
  Say "Windows / PowerShell checks"
  Ok ("PowerShell " + $PSVersionTable.PSVersion + " on " + [Environment]::OSVersion.VersionString)
  Ok "repo: $Repo"
  $branch = ""; try { $branch = (& git -C $Repo rev-parse --abbrev-ref HEAD 2>$null) } catch { }
  if ($branch) { if ($branch -eq "feature/edumix-live") { Ok "git branch: $branch" } else { Warn "git branch is '$branch' (expected feature/edumix-live)" } }
  if (-not (Test-Path $VPy)) { Warn "virtualenv missing - run: .\zs-dev.cmd setup"; return }
  Ok "virtualenv: $Venv"
  foreach ($p in @($ChromePort)) {
    $u = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($u) { Say "  DevTools port $p is in use (normal while the EduMix window is open)" } else { Ok "DevTools port $p free" }
  }
  Say "`nApplication checks"
  $null = Invoke-Py @("--doctor", "--chrome-port", $ChromePort)
}

function Cmd-ResetPassword {
  if (-not (Test-Path $VPy)) { Fail "run setup first"; exit 1 }
  $null = Invoke-Py @("--init-admin", "--reset", "--chrome-port", $ChromePort)
}

function Cmd-ResetSession {
  if (-not $Force) {
    $a = Read-Host "This forgets your saved MJPhysics login (you will have to sign in with OTP again). Type YES to continue"
    if ($a -ne "YES") { Say "cancelled"; return }
  }
  if (Test-Path $VPy) { $null = Invoke-Py @("--stop-browser", "--chrome-port", $ChromePort) }
  if (Test-Path $Profile) { Remove-Item $Profile -Recurse -Force; Ok "browser profile deleted" } else { Say "  no saved session" }
}

function Cmd-Test {
  if (-not (Test-Path $VPy)) { Fail "run setup first"; exit 1 }
  Push-Location (Join-Path $Repo "dashboard")
  try { & $VPy -m pytest tests -q -p no:warnings; exit $LASTEXITCODE } finally { Pop-Location }
}

function Cmd-Open { $s = Get-State; if ($s -and (Test-Pid $s.pid)) { Start-Process $s.url } else { Fail "not running (start it first)" } }

function Cmd-Clean {
  if (-not $Force) {
    $a = Read-Host "This deletes $Home_ (virtualenv, local database, logs, saved MJPhysics login). Type YES to continue"
    if ($a -ne "YES") { Say "cancelled"; return }
  }
  Cmd-Stop
  Remove-Item $Home_ -Recurse -Force -ErrorAction SilentlyContinue
  Ok "removed $Home_"
}

switch ($Command) {
  "help"           { Cmd-Help }
  "setup"          { Cmd-Setup }
  "start"          { Cmd-Start }
  "stop"           { Cmd-Stop }
  "restart"        { Cmd-Stop; Start-Sleep -Seconds 1; Cmd-Start }
  "status"         { Cmd-Status }
  "logs"           { Cmd-Logs }
  "doctor"         { Cmd-Doctor }
  "reset-password" { Cmd-ResetPassword }
  "reset-session"  { Cmd-ResetSession }
  "test"           { Cmd-Test }
  "open"           { Cmd-Open }
  "clean"          { Cmd-Clean }
}
