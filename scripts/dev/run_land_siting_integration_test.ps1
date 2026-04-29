[CmdletBinding()]
param(
    [ValidateSet("full", "smoke", "dry-run")]
    [string]$Mode = "full",

    [string]$Scenario = "HT35_zonal_constrained",

    [int]$Cores = 8,

    [string]$EnvPath = $env:PYPSA_GB_ENV,

    [switch]$RebuildLandPotential,

    [switch]$UseBundledPotential = $true,

    [string]$CondaEnvName = "pypsa-gb"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $RepoRoot

$TechnicalPotentialTarget = "resources/land/technical_potential_Zonal.csv"
$BundledTechnicalPotential = "data/land/examples/technical_potential_Zonal.csv"
$SolvedTarget = "resources/network/${Scenario}_solved.nc"
$LocationReportTarget = "resources/analysis/${Scenario}_location_constraint_report.csv"

$LandSitingUnitTests = @(
    (Join-Path $RepoRoot "tests/unit/test_apply_technical_potential_constraints.py"),
    (Join-Path $RepoRoot "tests/unit/test_future_capacity_candidates.py"),
    (Join-Path $RepoRoot "tests/unit/test_future_nuclear_candidates.py"),
    (Join-Path $RepoRoot "tests/unit/test_location_constraint_report.py")
)

function Assert-File {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if (!(Test-Path -LiteralPath $Path)) {
        throw "$Description was not found at '$Path'."
    }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string[]]$Command
    )

    Write-Host ""
    Write-Host "==> $Name" -ForegroundColor Cyan
    Write-Host ("    " + ($Command -join " "))

    $exe = $Command[0]
    $args = @()
    if ($Command.Count -gt 1) {
        $args = $Command[1..($Command.Count - 1)]
    }

    & $exe @args
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

$UseDirectEnv = -not [string]::IsNullOrWhiteSpace($EnvPath)
$ToolPaths = @{}

if ($UseDirectEnv) {
    $ToolPaths["python"] = Join-Path $EnvPath "python.exe"
    $ToolPaths["snakemake"] = Join-Path $EnvPath "Scripts/snakemake.exe"
    $ToolPaths["pytest"] = Join-Path $EnvPath "Scripts/pytest.exe"

    foreach ($toolName in $ToolPaths.Keys) {
        Assert-File -Path $ToolPaths[$toolName] -Description $toolName
    }
} else {
    $condaCommand = Get-Command conda -ErrorAction SilentlyContinue
    if ($null -eq $condaCommand) {
        throw "Conda was not found on PATH. Activate pypsa-gb first, or pass -EnvPath C:\path\to\env."
    }
}

function Get-ToolCommand {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("python", "snakemake", "pytest")]
        [string]$Tool,
        [string[]]$Arguments = @()
    )

    if ($UseDirectEnv) {
        return @($ToolPaths[$Tool]) + $Arguments
    }

    return @("conda", "run", "-n", $CondaEnvName, $Tool) + $Arguments
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string[]]$Arguments
    )

    Invoke-External -Name $Name -Command (Get-ToolCommand -Tool "python" -Arguments $Arguments)
}

function Invoke-Pytest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string[]]$Arguments
    )

    Invoke-External -Name $Name -Command (Get-ToolCommand -Tool "pytest" -Arguments $Arguments)
}

function Invoke-Snakemake {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string[]]$Arguments
    )

    Invoke-External -Name $Name -Command (Get-ToolCommand -Tool "snakemake" -Arguments $Arguments)
}

function Ensure-TechnicalPotential {
    if ($RebuildLandPotential) {
        Invoke-Snakemake -Name "Build land technical-potential CSV from raw inputs" -Arguments @(
            "--cores", "$Cores",
            $TechnicalPotentialTarget
        )
        return
    }

    if (Test-Path -LiteralPath $TechnicalPotentialTarget) {
        Write-Host ""
        Write-Host "==> Technical-potential CSV already exists" -ForegroundColor Cyan
        Write-Host "    $TechnicalPotentialTarget"
        return
    }

    if (!$UseBundledPotential) {
        throw "Missing $TechnicalPotentialTarget and bundled fallback is disabled. Rerun with -UseBundledPotential or -RebuildLandPotential."
    }

    Assert-File -Path $BundledTechnicalPotential -Description "Bundled technical-potential fixture"
    New-Item -ItemType Directory -Force -Path (Split-Path -Path $TechnicalPotentialTarget -Parent) | Out-Null
    Copy-Item -LiteralPath $BundledTechnicalPotential -Destination $TechnicalPotentialTarget -Force

    Write-Host ""
    Write-Host "==> Copied bundled technical-potential fixture" -ForegroundColor Cyan
    Write-Host "    Source: $BundledTechnicalPotential"
    Write-Host "    Target: $TechnicalPotentialTarget"
    Write-Host "    Note: this tests downstream integration, not the raw GIS land pipeline."
}

Write-Host "Land-siting integration test"
Write-Host "Repo:     $RepoRoot"
Write-Host "Scenario: $Scenario"
Write-Host "Mode:     $Mode"
Write-Host "Cores:    $Cores"

Invoke-Python -Name "Config validation" -Arguments @("config/config_loader.py", "--validate")

if ($Mode -in @("full", "smoke")) {
    Invoke-Pytest -Name "Targeted land-siting unit tests" -Arguments ($LandSitingUnitTests + @("-q"))
}

Ensure-TechnicalPotential

if ($Mode -eq "dry-run") {
    Invoke-Snakemake -Name "Dry-run constrained solved target" -Arguments @(
        "-n",
        "--cores", "$Cores",
        $SolvedTarget,
        "--config", "scenario=$Scenario"
    )
    Invoke-Snakemake -Name "Dry-run location-constraint report target" -Arguments @(
        "-n",
        "--cores", "$Cores",
        $LocationReportTarget,
        "--config", "scenario=$Scenario"
    )
    return
}

if ($Mode -eq "smoke") {
    Invoke-Snakemake -Name "Dry-run constrained solved target" -Arguments @(
        "-n",
        "--cores", "$Cores",
        $SolvedTarget,
        "--config", "scenario=$Scenario"
    )
    return
}

Invoke-Snakemake -Name "Full constrained solved workflow" -Arguments @(
    "--cores", "$Cores",
    $SolvedTarget,
    "--config", "scenario=$Scenario"
)

Invoke-Snakemake -Name "Generate location-constraint report" -Arguments @(
    "--cores", "$Cores",
    $LocationReportTarget,
    "--config", "scenario=$Scenario"
)

Write-Host ""
Write-Host "Land-siting integration test completed." -ForegroundColor Green
Write-Host "Solved target: $SolvedTarget"
Write-Host "Location report: $LocationReportTarget"
