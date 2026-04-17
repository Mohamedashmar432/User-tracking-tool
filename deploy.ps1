# ============================================================
#  deploy.ps1 — Deploy telemetry backend to Azure App Service
#
#  EDIT THE FOUR VARIABLES BELOW BEFORE RUNNING.
# ============================================================

$RESOURCE_GROUP   = "YOUR_RESOURCE_GROUP"
$APP_NAME         = "YOUR_APP_SERVICE_NAME"
$STORAGE_ACCOUNT  = "YOUR_STORAGE_ACCOUNT_NAME"     # Azure Storage for telemetry data
$BLOB_CONTAINER   = "agent-releases"                # Container for agent EXE downloads

# ── Secrets — fill these in or pass as env vars ──────────────────────────────
# Leave a value as "" to skip setting that env var (useful for incremental updates)
$AGENT_API_KEY   = ""   # Secret for agents posting to /ingest
$ADMIN_API_KEY   = ""   # X-API-Key fallback for curl/scripts
$JWT_SECRET      = ""   # HMAC secret for JWT tokens (use a long random string)
$ADMIN_PASSWORD  = ""   # Password for auto-created admin account (default: admin@123)

# ── Validate ─────────────────────────────────────────────────────────────────
if ($RESOURCE_GROUP -eq "YOUR_RESOURCE_GROUP") {
    Write-Error "Edit the RESOURCE_GROUP, APP_NAME, and STORAGE_ACCOUNT variables at the top of deploy.ps1 before running."
    exit 1
}

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== Telemetry Backend Deploy ===" -ForegroundColor Cyan
Write-Host "Resource Group : $RESOURCE_GROUP"
Write-Host "App Service    : $APP_NAME"
Write-Host "Storage Acct   : $STORAGE_ACCOUNT"
Write-Host ""

# ── 1. Get storage connection string ─────────────────────────────────────────
Write-Host "[1/4] Fetching storage connection string..." -ForegroundColor Cyan
$AZURE_STORAGE_CONNECTION_STRING = az storage account show-connection-string `
    --name $STORAGE_ACCOUNT `
    --resource-group $RESOURCE_GROUP `
    --query connectionString --output tsv
Write-Host "      OK" -ForegroundColor Green

# ── 2. Set App Service env vars ───────────────────────────────────────────────
Write-Host "[2/4] Configuring App Service settings..." -ForegroundColor Cyan

$settings = @(
    "AZURE_STORAGE_CONNECTION_STRING=$AZURE_STORAGE_CONNECTION_STRING"
    "SCM_DO_BUILD_DURING_DEPLOYMENT=true"
)

# Only include secrets the user has filled in
if ($AGENT_API_KEY)  { $settings += "AGENT_API_KEY=$AGENT_API_KEY" }
if ($ADMIN_API_KEY)  { $settings += "ADMIN_API_KEY=$ADMIN_API_KEY" }
if ($JWT_SECRET)     { $settings += "JWT_SECRET=$JWT_SECRET" }
if ($ADMIN_PASSWORD) { $settings += "ADMIN_PASSWORD=$ADMIN_PASSWORD" }

# Build AGENT_DOWNLOAD_URL from storage account + container
$agentBlobUrl = "https://$STORAGE_ACCOUNT.blob.core.windows.net/$BLOB_CONTAINER/telemetry_agent.exe"
$settings += "AGENT_DOWNLOAD_URL=$agentBlobUrl"

az webapp config appsettings set `
    --name $APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --settings @settings `
    --output none
Write-Host "      OK" -ForegroundColor Green

# ── 3. Deploy code ────────────────────────────────────────────────────────────
Write-Host "[3/4] Deploying code (az webapp up)..." -ForegroundColor Cyan
az webapp up --name $APP_NAME --resource-group $RESOURCE_GROUP --runtime "PYTHON:3.11"
Write-Host "      OK" -ForegroundColor Green

# ── 4. Upload agent EXE (if built) ───────────────────────────────────────────
$exePath = "dist\telemetry_agent.exe"
if (Test-Path $exePath) {
    Write-Host "[4/4] Uploading agent EXE to Blob Storage..." -ForegroundColor Cyan

    # Ensure the container exists
    az storage container create `
        --name $BLOB_CONTAINER `
        --account-name $STORAGE_ACCOUNT `
        --public-access blob `
        --output none

    az storage blob upload `
        --account-name $STORAGE_ACCOUNT `
        --container-name $BLOB_CONTAINER `
        --name "telemetry_agent.exe" `
        --file $exePath `
        --overwrite `
        --output none
    Write-Host "      Uploaded: $agentBlobUrl" -ForegroundColor Green
} else {
    Write-Host "[4/4] Skipping EXE upload — dist\telemetry_agent.exe not found." -ForegroundColor Yellow
    Write-Host "      Build it first: user-track\Scripts\pyinstaller.exe telemetry_agent.spec" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Deploy complete!" -ForegroundColor Green
Write-Host "Dashboard: https://$APP_NAME.azurewebsites.net" -ForegroundColor Cyan
Write-Host ""
Write-Host "Verify: curl https://$APP_NAME.azurewebsites.net/api/health" -ForegroundColor Gray
Write-Host ""
