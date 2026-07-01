# Docker Cleanup Script
# Verwijdert gestopte containers, dangling images, ongebruikte netwerken,
# build cache en optioneel volumes.
# Gebruik: .\docker-cleanup.ps1 [-Full] [-DryRun]

param(
    [switch]$Full,    # Verwijder ook alle ongebruikte images (niet alleen dangling)
    [switch]$DryRun   # Laat zien wat verwijderd zou worden, doe niets
)

function Write-Header($text) {
    Write-Host "`n=== $text ===" -ForegroundColor Cyan
}

function Format-Bytes($bytes) {
    if ($bytes -ge 1GB) { return "{0:N2} GB" -f ($bytes / 1GB) }
    if ($bytes -ge 1MB) { return "{0:N2} MB" -f ($bytes / 1MB) }
    return "{0:N2} KB" -f ($bytes / 1KB)
}

# --- Huidige schijfruimte ---
Write-Header "Docker schijfruimte (voor)"
docker system df

if ($DryRun) {
    Write-Host "`n[DRY RUN] Geen wijzigingen aangebracht." -ForegroundColor Yellow

    Write-Header "Zou verwijderen: gestopte containers"
    docker ps -a --filter "status=exited" --filter "status=created" --format "  {{.ID}}  {{.Image}}  {{.Status}}"

    Write-Header "Zou verwijderen: dangling images"
    docker images --filter "dangling=true" --format "  {{.ID}}  {{.Repository}}:{{.Tag}}  {{.Size}}"

    if ($Full) {
        Write-Header "Zou verwijderen: alle ongebruikte images"
        docker images --format "  {{.ID}}  {{.Repository}}:{{.Tag}}  {{.Size}}"
    }

    Write-Header "Zou verwijderen: ongebruikte volumes"
    docker volume ls --filter "dangling=true" --format "  {{.Name}}"

    exit 0
}

# --- Gestopte containers ---
Write-Header "Gestopte containers verwijderen"
$containers = docker ps -aq --filter "status=exited" --filter "status=created"
if ($containers) {
    docker rm $containers
    Write-Host "Verwijderd." -ForegroundColor Green
} else {
    Write-Host "Niets te verwijderen." -ForegroundColor Gray
}

# --- Dangling images (altijd) ---
Write-Header "Dangling images verwijderen (<none>:<none>)"
$dangling = docker images -q --filter "dangling=true"
if ($dangling) {
    docker rmi $dangling
    Write-Host "Verwijderd." -ForegroundColor Green
} else {
    Write-Host "Niets te verwijderen." -ForegroundColor Gray
}

# --- Alle ongebruikte images (alleen bij -Full) ---
if ($Full) {
    Write-Header "Alle ongebruikte images verwijderen (-Full)"
    docker image prune -af
}

# --- Build cache ---
Write-Header "Build cache verwijderen"
docker builder prune -af

# --- Ongebruikte netwerken ---
Write-Header "Ongebruikte netwerken verwijderen"
docker network prune -f

# --- Volumes (alleen bij -Full) ---
if ($Full) {
    Write-Header "Ongebruikte volumes verwijderen (-Full)"
    docker volume prune -f
}

# --- Schijfruimte na ---
Write-Header "Docker schijfruimte (na)"
docker system df

Write-Host "`nKlaar." -ForegroundColor Green
if (-not $Full) {
    Write-Host "Tip: gebruik -Full om ook ongebruikte images en volumes te verwijderen." -ForegroundColor DarkGray
}
