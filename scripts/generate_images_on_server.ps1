param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("dotnet", "go", "java", "node", "php", "python")]
    [string]$Lang
)

$ErrorActionPreference = "Stop"

Write-Host "Generating '$Lang' images on server ..."
ssh -o BatchMode=yes pqc-sca "cd ~/SCA && source .venv/bin/activate && python scripts/generate_images.py --lang $Lang"
