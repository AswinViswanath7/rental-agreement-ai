$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "RentEase virtual environment not found. Create it and install requirements.txt first."
}

$secureKey = Read-Host "Enter your replacement NVIDIA API key (input is hidden)" -AsSecureString
$keyPointer = [IntPtr]::Zero

try {
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $env:NVIDIA_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    $env:LLM_PROVIDER = "nvidia"

    & $python -m flask --app app run --host 127.0.0.1 --port 5000 --no-debugger --no-reload
    if ($LASTEXITCODE -ne 0) {
        throw "RentEase exited with code $LASTEXITCODE."
    }
}
finally {
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    $secureKey.Dispose()
    Remove-Item Env:NVIDIA_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:LLM_PROVIDER -ErrorAction SilentlyContinue
}
