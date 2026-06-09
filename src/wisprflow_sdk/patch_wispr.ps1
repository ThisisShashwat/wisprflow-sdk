# patch_wispr.ps1
# Run this once after every Wispr Flow update.
# - Auto-detects the latest app version directory
# - Backs up app.asar before touching anything
# - Injects runtime config logger (modelId, url, apiKey, environment)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── 1. Find latest app-x.x.x directory ───────────────────────────────────────

$wisprBase = "$env:LOCALAPPDATA\WisprFlow"
$appDir = Get-ChildItem -Path $wisprBase -Directory -Filter "app-*" |
          Sort-Object Name -Descending |
          Select-Object -First 1 -ExpandProperty FullName

if (-not $appDir) {
    Write-Error "Could not find any app-x.x.x directory under $wisprBase"
    exit 1
}

Write-Host "Found app dir: $appDir"

$asarPath    = "$appDir\resources\app.asar"
$backupPath  = "$appDir\resources\app.asar.backup"
$extractDir  = "$appDir\resources\app_extracted"
$runtimeJson = "$wisprBase\wispr_runtime.json"

# ── 2. Stop Wispr ─────────────────────────────────────────────────────────────

Write-Host "Stopping Wispr Flow..."
Stop-Process -Name "Wispr Flow"  -Force -ErrorAction SilentlyContinue
Stop-Process -Name "WisprFlow"   -Force -ErrorAction SilentlyContinue
Stop-Process -Name "WisprHelper" -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# ── 3. Backup ─────────────────────────────────────────────────────────────────

if (-not (Test-Path $asarPath)) {
    Write-Error "app.asar not found at $asarPath"
    exit 1
}

Write-Host "Backing up app.asar..."
Copy-Item $asarPath $backupPath -Force
Write-Host "Backup saved to: $backupPath"

# ── 4. Extract ────────────────────────────────────────────────────────────────

if (Test-Path $extractDir) {
    Remove-Item $extractDir -Recurse -Force
}

Write-Host "Extracting asar..."
npx --yes asar extract $asarPath $extractDir

# ── 5. Inject runtime logger ──────────────────────────────────────────────────

# Escaping for JS string inside PowerShell — use forward slashes in the path
$runtimeJsonJs = $runtimeJson.Replace('\', '/')

$pattern     = '=this.getDesiredGrpcModelInfo()'
$injection   = '=this.getDesiredGrpcModelInfo();try{require("fs").writeFileSync("' + $runtimeJsonJs + '",JSON.stringify({modelId:i,environment:a,url:o,apiKey:Ct.Fo}))}catch(_e){}'
$files   = Get-ChildItem -Path $extractDir -Recurse -Include "*.js"
$patched = $false

foreach ($f in $files) {
    $content = [System.IO.File]::ReadAllText($f.FullName)
    if ($content.Contains($pattern)) {
        Write-Host "Patching: $($f.Name)"
        $content = $content.Replace($pattern, $injection)
        [System.IO.File]::WriteAllText($f.FullName, $content, (New-Object System.Text.UTF8Encoding $false))
        $patched = $true
        break
    }
}

if (-not $patched) {
    Write-Host ""
    Write-Host "ERROR: Injection pattern not found. Wispr may have changed their code." -ForegroundColor Red
    Write-Host "Restoring backup and aborting..." -ForegroundColor Yellow
    Copy-Item $backupPath $asarPath -Force
    Remove-Item $extractDir -Recurse -Force
    exit 1
}

# ── 6. Repack ─────────────────────────────────────────────────────────────────

Write-Host "Repacking asar..."
npx asar pack $extractDir $asarPath
Remove-Item $extractDir -Recurse -Force

# ── 7. Done ───────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "Patch complete!" -ForegroundColor Green
Write-Host "App dir    : $appDir"
Write-Host "Backup     : $backupPath"
Write-Host "Runtime cfg: $runtimeJson (written on first dictation)"
Write-Host ""
Write-Host "Launch Wispr Flow and do one dictation to populate wispr_runtime.json."
