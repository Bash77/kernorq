#requires -version 5
# Kernorq -> Cloud Run deployment (PowerShell)
# Prereq (one-time, after billing is attached to the project):
#   gcloud services enable run.googleapis.com secretmanager.googleapis.com `
#       cloudbuild.googleapis.com artifactregistry.googleapis.com
#   gcloud artifacts repositories create kernorq --repository-format=docker `
#       --location=us-central1
#
# Provide the key via env var (never hard-coded):
#   $env:GOOGLE_API_KEY = "<your key>"
# Then run this script.

$ErrorActionPreference = "Stop"

$gc = "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
if (-not (Test-Path $gc)) { $gc = "gcloud" }

$PROJECT = "all-things-agentic-bash77"
$REGION = "us-central1"
$SERVICE = "kernorq"
$REPO = "kernorq"
$SECRET = "kernorq-google-api-key"
$IMAGE = "$REGION-docker.pkg.dev/$PROJECT/$REPO/${SERVICE}:latest"

if (-not $env:GOOGLE_API_KEY) {
    Write-Error "GOOGLE_API_KEY env var is required (it is never stored in this repo)."
    exit 1
}

Write-Host "== Project/region =="
& $gc config set project $PROJECT
& $gc config set run/region $REGION

Write-Host "== Create Secret Manager secret + version (value from env) =="
# gcloud secrets describe returns NOT_FOUND on first run -- treat as expected "create" path, not fatal
$oldEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$describeOutput = & $gc secrets describe $SECRET --project=$PROJECT 2>&1
$describeExit = $LASTEXITCODE
$ErrorActionPreference = $oldEAP
if ($describeExit -ne 0) {
    Write-Host "Secret $SECRET not found (exit $describeExit) -- creating..."
    Write-Host "Running: gcloud secrets create $SECRET --replication-policy=automatic --project=$PROJECT"
    $oldEAP2 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $createOutput = & $gc secrets create $SECRET --replication-policy=automatic --project=$PROJECT 2>&1
    $createExit = $LASTEXITCODE
    $ErrorActionPreference = $oldEAP2
    if ($createExit -ne 0) {
        Write-Host "gcloud secrets create failed (exit $createExit):"
        Write-Host ($createOutput | Out-String)
        Write-Error "Failed to create secret $SECRET (exit $createExit) -- not proceeding to versions add"
        exit 1
    }
    Write-Host "Secret $SECRET create succeeded -- verifying..."
    $ErrorActionPreference = "Continue"
    $verifyOutput = & $gc secrets describe $SECRET --project=$PROJECT 2>&1
    $verifyExit = $LASTEXITCODE
    $ErrorActionPreference = $oldEAP2
    if ($verifyExit -ne 0) {
        Write-Host "Verification gcloud secrets describe failed (exit $verifyExit):"
        Write-Host ($verifyOutput | Out-String)
        Write-Error "Secret $SECRET not found after create -- aborting before versions add"
        exit 1
    }
    Write-Host "Verified: $verifyOutput"
} else {
    Write-Host "Secret $SECRET already exists -- adding new version..."
    Write-Host "Existing: $describeOutput"
}
Write-Host "Adding secret version from env (value never logged)..."
$oldEAP3 = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$versionOutput = $env:GOOGLE_API_KEY | & $gc secrets versions add $SECRET --data-file=- --project=$PROJECT 2>&1
$versionExit = $LASTEXITCODE
$ErrorActionPreference = $oldEAP3
if ($versionExit -ne 0) {
    Write-Host "gcloud secrets versions add failed (exit $versionExit):"
    Write-Host ($versionOutput | Out-String)
    Write-Error "Failed to add secret version (exit $versionExit)"
    exit 1
}
Write-Host "Secret version added."

Write-Host "== Grant Cloud Run runtime access to the secret =="
$PROJECT_NUMBER = & $gc projects describe $PROJECT --format="value(projectNumber)"
& $gc secrets add-iam-policy-binding $SECRET `
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" `
    --role="roles/secretmanager.secretAccessor"

Write-Host "== Build via Cloud Build =="
& $gc builds submit --tag $IMAGE .

Write-Host "== Deploy to Cloud Run =="
& $gc run deploy $SERVICE `
    --image=$IMAGE `
    --region=$REGION `
    --platform=managed `
    --allow-unauthenticated `
    --min-instances=0 `
    --max-instances=2 `
    --set-secrets="GOOGLE_API_KEY=${SECRET}:latest"

Write-Host "== Service URL =="
& $gc run services describe $SERVICE --region=$REGION --format="value(status.url)"
