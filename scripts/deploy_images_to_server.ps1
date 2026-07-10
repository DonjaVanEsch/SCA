$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

$archive = Join-Path $env:TEMP "pqc_images.tar.gz"
Write-Host "Archiving images/ ..."
tar -czf $archive images

Write-Host "Uploading ..."
scp -o BatchMode=yes $archive pqc-sca:~/images.tar.gz

Write-Host "Extracting on server ..."
ssh -o BatchMode=yes pqc-sca "tar -xzf ~/images.tar.gz -C ~/SCA && rm ~/images.tar.gz && find ~/SCA/images -type f | wc -l"

Remove-Item $archive
Pop-Location
