[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Interpreter,
    [string]$RuntimeFile
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { throw 'Explicit project root required.' }
$activationProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $activationProjectRoot -PathType Container)) { throw 'Project root must be a directory.' }
$activationRuntime = $null
if ($RuntimeFile) {
    # Existing legacy runtime records are opt-in; never silently read an old
    # project's receipt or select an inherited PATH alias.
    $activationRuntime = Get-Content -LiteralPath $RuntimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
}
if (-not $Interpreter) {
    if ($activationRuntime) { $Interpreter = $activationRuntime.python }
    else {
        foreach ($activationCandidate in @('.venv\Scripts\python.exe','venv\Scripts\python.exe')) {
            $activationCandidate = Join-Path $activationProjectRoot $activationCandidate
            if (Test-Path -LiteralPath $activationCandidate -PathType Leaf) { $Interpreter = $activationCandidate; break }
        }
    }
}
if ([string]::IsNullOrWhiteSpace($Interpreter)) { throw 'No existing project interpreter; pass -Interpreter with an explicit executable.' }
$activationInterpreter = (Resolve-Path -LiteralPath $Interpreter -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $activationInterpreter -PathType Leaf)) { throw 'Interpreter must be an existing file.' }
$activationPrefix = @((Split-Path -Parent $activationInterpreter))
$activationBase = $activationInterpreter
if ($activationRuntime) {
    foreach ($activationRequired in @('node','uv','basePython')) {
        if ([string]::IsNullOrWhiteSpace($activationRuntime.$activationRequired)) { throw "Runtime field required: $activationRequired" }
    }
    if (-not (Test-Path -LiteralPath $activationRuntime.node -PathType Container)) { throw 'Node runtime directory missing.' }
    if (-not (Test-Path -LiteralPath $activationRuntime.uv -PathType Leaf)) { throw 'uv executable missing.' }
    if (-not (Test-Path -LiteralPath $activationRuntime.basePython -PathType Leaf)) { throw 'Base interpreter missing.' }
    $activationPrefix += @($activationRuntime.node, (Split-Path -Parent $activationRuntime.uv))
    $activationBase = (Resolve-Path -LiteralPath $activationRuntime.basePython).Path
}
# Validation precedes the two process-local mutations. No registry/global PATH,
# environment creation, managed-base edit, or installation occurs.
$env:Path = (($activationPrefix | Select-Object -Unique) -join ';') + ';' + $env:Path
$env:UV_PYTHON = $activationBase
Write-Output 'Selected runtime paths activated for this PowerShell process; preflight measures readiness separately.'
