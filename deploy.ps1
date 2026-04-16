# deploy.ps1 - Full Azure deployment for User Tracking Tool
# Run once for initial setup. Safe to re-run - all commands are idempotent.
#
# Usage (from repo root):
#   .\deploy.ps1
#
# Re-deploy after code changes only:
#   az webapp up --name <APP_NAME> --resource-group <RESOURCE_GROUP>

$ErrorActionPreference = 'Stop'

# -- CONFIGURATION - edit these before running -----------------------------------
$RESOURCE_GROUP = "telemetry-rg"
$LOCATION       = "eastus"
$STORAGE_NAME   = "telemetrystorage123"     # globally unique, lowercase, 3-24 chars
$APP_NAME       = "telemetry-dashboard123"  # globally unique - becomes <name>.azurewebsites.net
$SKU            = "F1"                      # F1=Free (testing, sleeps after 20min idle)
#                                           # B1=Basic ~$13/mo (production - needs quota increase at portal.azure.com)
# --------------------------------------------------------------------------------

$CONTAINER_NAME = "agent-releases"
$APP_URL        = "https://$APP_NAME.azurewebsites.net"

# Helper - stops the script if the last az CLI command failed
function Check-Az($step) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host "      FAILED at $step (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  User Tracking Tool - Azure Deployment  " -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  Resource group : $RESOURCE_GROUP"
Write-Host "  Location       : $LOCATION"
Write-Host "  Storage        : $STORAGE_NAME"
Write-Host "  App Service    : $APP_NAME ($SKU)"
Write-Host ""

# [1/8] Azure login
Write-Host "[1/8] Signing in to Azure..." -ForegroundColor Cyan
az login --only-show-errors
Check-Az "login"

# [2/8] Resource group
Write-Host "[2/8] Creating resource group..." -ForegroundColor Cyan
az group create --name $RESOURCE_GROUP --location $LOCATION --only-show-errors | Out-Null
Check-Az "resource group"
Write-Host "      OK" -ForegroundColor Green

# [3/8] Storage account + capture connection string for later steps
Write-Host "[3/8] Creating storage account..." -ForegroundColor Cyan
az storage account create --name $STORAGE_NAME --resource-group $RESOURCE_GROUP --location $LOCATION --sku Standard_LRS --kind StorageV2 --only-show-errors | Out-Null
Check-Az "storage account create"
$CONN_STR = az storage account show-connection-string --name $STORAGE_NAME --resource-group $RESOURCE_GROUP --query connectionString -o tsv
Check-Az "show-connection-string"
Write-Host "      OK - connection string captured" -ForegroundColor Green

# [4/8] Build agent EXE (skip if already built)
Write-Host "[4/8] Building agent EXE..." -ForegroundColor Cyan
if (-not (Test-Path "dist\telemetry_agent.exe")) {
    & "user-track\Scripts\pyinstaller.exe" telemetry_agent.spec
    Check-Az "pyinstaller"
} else {
    Write-Host "      Already built - skipping" -ForegroundColor Gray
}

# [5/8] Upload EXE to Blob Storage
# --connection-string is required - without it az CLI tries to query account keys
# via ARM which may fail on some subscription types.
Write-Host "[5/8] Uploading agent EXE to Blob Storage..." -ForegroundColor Cyan
az storage container create --name $CONTAINER_NAME --connection-string $CONN_STR --public-access blob --only-show-errors | Out-Null
Check-Az "container create"
az storage blob upload --connection-string $CONN_STR --container-name $CONTAINER_NAME --name telemetry_agent.exe --file "dist\telemetry_agent.exe" --overwrite --only-show-errors | Out-Null
Check-Az "blob upload"
$BLOB_URL = az storage blob url --connection-string $CONN_STR --container-name $CONTAINER_NAME --name telemetry_agent.exe -o tsv
Write-Host "      OK - $BLOB_URL" -ForegroundColor Green

# [6/8] Create the App Service plan + blank web app (no code yet)
# SCM_DO_BUILD_DURING_DEPLOYMENT must be set BEFORE the first code push
# so Oryx runs pip install. Creating the app first lets us set it in time.
# NOTE: If this fails with "quota" - change $SKU to "F1" above.
Write-Host "[6/8] Creating App Service (no code yet)..." -ForegroundColor Cyan
az appservice plan create --name "$APP_NAME-plan" --resource-group $RESOURCE_GROUP --location $LOCATION --sku $SKU --is-linux --only-show-errors | Out-Null
Check-Az "appservice plan create"
az webapp create --name $APP_NAME --resource-group $RESOURCE_GROUP --plan "$APP_NAME-plan" --runtime "PYTHON:3.11" --only-show-errors | Out-Null
Check-Az "webapp create"
Write-Host "      OK" -ForegroundColor Green

# [7/8] Set env vars + startup command BEFORE pushing code
# SCM_DO_BUILD_DURING_DEPLOYMENT=true tells Oryx to run pip install during zip deploy
Write-Host "[7/8] Setting environment variables and startup command..." -ForegroundColor Cyan
az webapp config appsettings set --name $APP_NAME --resource-group $RESOURCE_GROUP --settings AZURE_STORAGE_CONNECTION_STRING="$CONN_STR" AGENT_DOWNLOAD_URL="$BLOB_URL" SCM_DO_BUILD_DURING_DEPLOYMENT="true" --only-show-errors | Out-Null
Check-Az "appsettings"
az webapp config set --name $APP_NAME --resource-group $RESOURCE_GROUP --startup-file "uvicorn backend.main:app --host 0.0.0.0 --port 8000" --only-show-errors | Out-Null
Check-Az "startup-file"
Write-Host "      OK" -ForegroundColor Green

# [8/8] Deploy code - Oryx now runs pip install because SCM flag is already set
Write-Host "[8/8] Deploying code - this takes ~2-3 min..." -ForegroundColor Cyan
az webapp up --name $APP_NAME --resource-group $RESOURCE_GROUP --runtime "PYTHON:3.11" --only-show-errors
Check-Az "webapp up"
Write-Host "      OK" -ForegroundColor Green

# -- Done ------------------------------------------------------------------------
Write-Host ""
Write-Host "=========================================" -ForegroundColor Green
Write-Host "  Deployment complete!" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
Write-Host "  Dashboard : $APP_URL" -ForegroundColor Green
Write-Host "  Health    : $APP_URL/api/health" -ForegroundColor Green
Write-Host ""
Write-Host "  Install agent on Windows clients (run as Admin):" -ForegroundColor Yellow
Write-Host "  powershell -ExecutionPolicy Bypass -Command `"irm '$APP_URL/install-script' | iex`"" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Re-deploy after code changes:" -ForegroundColor Gray
Write-Host "  az webapp up --name $APP_NAME --resource-group $RESOURCE_GROUP" -ForegroundColor Gray
Write-Host ""
