param([string]$AppRoot = $PSScriptRoot)

$ErrorActionPreference = 'Stop'
function Get-NoseOutSha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}
# PowerShell 7에서 Windows PowerShell을 실행해도 같은 버전의 서명 검사 모듈을 사용한다.
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$AppRoot = [IO.Path]::GetFullPath($AppRoot)
if (-not (Test-Path -LiteralPath (Join-Path $AppRoot '_internal'))) {
    throw 'NoseOut 폴더에 있는 런타임 준비 도구를 실행하세요.'
}
if (-not [Environment]::Is64BitProcess) {
    throw '64비트 PowerShell에서 실행하세요.'
}
$systemDirectory = [Environment]::SystemDirectory
foreach ($name in @('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll')) {
    $runtimePath = Join-Path $systemDirectory $name
    $version = if (Test-Path -LiteralPath $runtimePath) {
        [Diagnostics.FileVersionInfo]::GetVersionInfo($runtimePath)
    } else { $null }
    if (-not $version -or $version.FileMajorPart -lt 14 -or
        ($version.FileMajorPart -eq 14 -and $version.FileMinorPart -lt 44)) {
        Write-Host '먼저 Microsoft Visual C++ 2015–2022 x64 런타임을 공식 사이트에서 설치한 뒤 다시 실행하세요.'
        Write-Host 'https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist'
        exit 1
    }
}
$manifest = Get-Content -LiteralPath (Join-Path $AppRoot 'runtime-setup.json') -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($alias in $manifest.local_aliases) {
    if ($alias.system_name -ne 'msvcp140.dll' -or
        $alias.relative_path -notlike '_internal/numpy.libs/msvcp140-*.dll') {
        throw '허용한 런타임 별칭이 아닙니다.'
    }
    $source = Join-Path $systemDirectory $alias.system_name
    $signature = Get-AuthenticodeSignature -LiteralPath $source
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
        throw '설치된 런타임의 Microsoft 서명을 확인하지 못했습니다.'
    }
    $target = [IO.Path]::GetFullPath((Join-Path $AppRoot $alias.relative_path))
    $prefix = $AppRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath (Split-Path -Parent $target))) {
        throw '앱 내부 런타임 경로가 아닙니다.'
    }
    if (-not (Test-Path -LiteralPath $target)) {
        # NumPy wheel이 참조하는 별칭만 사용자 컴퓨터의 정식 런타임으로 준비한다.
        # 이 파일은 공개 패키지에 포함하지 않으며 시스템 DLL은 수정하지 않는다.
        Copy-Item -LiteralPath $source -Destination $target
    } elseif ((Get-NoseOutSha256 $source) -ne (Get-NoseOutSha256 $target)) {
        throw '기존 별칭이 설치된 런타임과 달라 덮어쓰지 않았습니다.'
    }
}
Write-Host '런타임 준비가 끝났습니다. NoseOut.exe를 실행하세요.'
