Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "  [OK] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "  [ATTENTION] $Message" -ForegroundColor Yellow
}

function Test-CommandAvailable {
    param([string]$CommandName)
    return [bool](Get-Command $CommandName -ErrorAction SilentlyContinue)
}

function Get-CommandVersionLine {
    param(
        [string]$CommandName,
        [string[]]$VersionArgs = @("--version")
    )
    if (-not (Test-CommandAvailable $CommandName)) {
        return "non trouvé"
    }

    try {
        $output = & $CommandName @VersionArgs 2>$null
        if ($output) {
            return ($output | Select-Object -First 1).ToString().Trim()
        }
    }
    catch {
        return "version indisponible"
    }

    return "version indisponible"
}

function Ensure-WingetPackage {
    param(
        [string]$PackageId,
        [string]$DisplayName,
        [string]$CommandName
    )

    if (Test-CommandAvailable $CommandName) {
        Write-Ok "$DisplayName déjà présent."
        return
    }

    Write-Warn "$DisplayName non détecté. Installation via winget..."
    & winget install --id $PackageId --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Échec de l'installation winget pour $DisplayName ($PackageId)."
    }

    if (Test-CommandAvailable $CommandName) {
        Write-Ok "$DisplayName installé."
    }
    else {
        Write-Warn "$DisplayName installé mais non disponible dans la session courante. Rouvrez un terminal si nécessaire."
    }
}

function Invoke-RepoCommand {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$ErrorMessage
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $ErrorMessage
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

Write-Host "Installation Minavio (Windows, PowerShell 5.1+)" -ForegroundColor Magenta
Write-Host "Dossier du dépôt : $repoRoot"

if (-not (Test-CommandAvailable "winget")) {
    throw "winget est requis. Installez/activez l'App Installer Microsoft Store puis relancez ce script."
}

Write-Step "Vérification des dépendances système via winget"
Ensure-WingetPackage -PackageId "Astral-sh.uv" -DisplayName "uv (Astral)" -CommandName "uv"
Ensure-WingetPackage -PackageId "Gyan.FFmpeg" -DisplayName "FFmpeg" -CommandName "ffmpeg"
Ensure-WingetPackage -PackageId "OpenJS.NodeJS.LTS" -DisplayName "Node.js LTS" -CommandName "node"

$ollamaInstalled = Test-CommandAvailable "ollama"
if ($ollamaInstalled) {
    Write-Ok "Ollama déjà présent."
}
else {
    Write-Host ""
    $installOllama = Read-Host "Ollama est optionnel. Voulez-vous l'installer maintenant via winget ? (o/N)"
    if ($installOllama -match "^(o|oui|y|yes)$") {
        Ensure-WingetPackage -PackageId "Ollama.Ollama" -DisplayName "Ollama" -CommandName "ollama"
        $ollamaInstalled = Test-CommandAvailable "ollama"
    }
    else {
        Write-Warn "Ollama ignoré."
    }
}

Write-Step "Installation des dépendances Python (uv sync --all-extras)"
Invoke-RepoCommand -Executable "uv" -Arguments @("sync", "--all-extras") -ErrorMessage "Échec de 'uv sync --all-extras'."

$motionDir = Join-Path $repoRoot "motion"
if (-not (Test-Path $motionDir)) {
    throw "Dossier introuvable : $motionDir"
}

Write-Step "Installation des dépendances motion (npm install)"
Push-Location $motionDir
try {
    Invoke-RepoCommand -Executable "npm" -Arguments @("install") -ErrorMessage "Échec de 'npm install' dans motion/."
}
finally {
    Pop-Location
}

if ($ollamaInstalled) {
    Write-Host ""
    $pullModels = Read-Host "Télécharger les modèles Ollama qwen3:30b-a3b et qwen3-vl:8b ? (plusieurs Go) (o/N)"
    if ($pullModels -match "^(o|oui|y|yes)$") {
        Write-Step "Téléchargement des modèles Ollama"
        Invoke-RepoCommand -Executable "ollama" -Arguments @("pull", "qwen3:30b-a3b") -ErrorMessage "Échec de 'ollama pull qwen3:30b-a3b'."
        Invoke-RepoCommand -Executable "ollama" -Arguments @("pull", "qwen3-vl:8b") -ErrorMessage "Échec de 'ollama pull qwen3-vl:8b'."
    }
    else {
        Write-Warn "Téléchargement des modèles Ollama ignoré."
    }
}

Write-Step "Récapitulatif"
Write-Host ("uv      : {0}" -f (Get-CommandVersionLine -CommandName "uv"))
Write-Host ("ffmpeg  : {0}" -f (Get-CommandVersionLine -CommandName "ffmpeg"))
Write-Host ("node    : {0}" -f (Get-CommandVersionLine -CommandName "node"))
if (Test-CommandAvailable "ollama") {
    Write-Host ("ollama  : {0}" -f (Get-CommandVersionLine -CommandName "ollama"))
}
else {
    Write-Host "ollama  : non installé (optionnel)"
}

Write-Host ""
Write-Host "Installation terminée. Pour lancer l'interface Minavio :" -ForegroundColor Green
Write-Host "  scripts/minavio-ui.bat"
