# images/ and images_clients/ are excluded -- generated Dockerfile contexts
# regenerate server-side (generate_images.py / generate_client_images.py),
# never shipped over the wire. net_signal.py lives at the project root (a
# top-level `import net_signal` in manager.py, not under scripts/) so it
# must be listed explicitly below -- omitting it once already took down the
# live server with a ModuleNotFoundError on startup.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

python (Join-Path $PSScriptRoot "check_deploy_safety.py")
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw "Deploy safety check failed -- see above. Aborting push to server."
}

$archive = Join-Path $env:TEMP "pqc_deploy.tar.gz"
tar -czf $archive `
    --exclude=images `
    --exclude=images_clients `
    --exclude=.venv `
    --exclude=.git `
    --exclude=.claude `
    --exclude=__pycache__ `
    --exclude="pqc_manager.db*" `
    --exclude=dashboard_settings.json `
    manager.py dashboard.py db.py net_signal.py static scripts CONTEXT.md .gitignore

scp -o BatchMode=yes $archive pqc-sca:~/deploy.tar.gz
ssh -o BatchMode=yes pqc-sca "tar -xzf ~/deploy.tar.gz -C ~/SCA && rm ~/deploy.tar.gz && sudo systemctl restart pqc-dashboard && systemctl status pqc-dashboard --no-pager"

Remove-Item $archive
Pop-Location
