param(
    [string]$AppRoot = '',
    [string]$ArchivePath = ''
)

$ErrorActionPreference = 'Stop'
function Get-NoseOutSha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}
if (-not $AppRoot) {
    $AppRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'ffmpeg-download.json')) {
        $PSScriptRoot
    } else { Split-Path -Parent $PSScriptRoot }
}
$AppRoot = [IO.Path]::GetFullPath($AppRoot)
$manifestPath = Join-Path $PSScriptRoot 'ffmpeg-download.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    $manifestPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'licenses/ffmpeg-download.json'
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$target = Join-Path $AppRoot ('vendor/ffmpeg/' + $manifest.archive_root)
if (Test-Path -LiteralPath $target) {
    throw '이 버전의 FFmpeg 폴더가 이미 있습니다. 기존 파일은 덮어쓰지 않습니다.'
}
$uri = [Uri]$manifest.url
if ($uri.Scheme -ne 'https' -or $uri.Host -ne 'github.com' -or
    -not $uri.AbsolutePath.StartsWith('/BtbN/FFmpeg-Builds/releases/download/')) {
    throw '허용한 공식 다운로드 주소가 아닙니다.'
}
Write-Host 'FFmpeg를 공식 BtbN 배포처에서 별도로 받습니다. 자동 실행하거나 시스템 설정을 바꾸지 않습니다.'
Write-Host ('라이선스: ' + $manifest.license + ' / ' + $manifest.upstream_license)
$temporary = Join-Path $AppRoot ('.ffmpeg-download-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $archive = Join-Path $temporary 'ffmpeg.zip'
    if ($ArchivePath) {
        Copy-Item -LiteralPath $ArchivePath -Destination $archive
    } else {
        Invoke-WebRequest -Uri $uri.AbsoluteUri -OutFile $archive -UseBasicParsing
    }
    if ((Get-NoseOutSha256 $archive) -ne $manifest.sha256) {
        throw '다운로드 파일의 해시가 다릅니다. 설치하지 않았습니다.'
    }
    $expanded = Join-Path $temporary 'expanded'
    # 신뢰한 해시를 확인한 뒤에도 압축 파일의 경로가 임시 폴더를 벗어나지 않는지 검사한다.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $prefix = [IO.Path]::GetFullPath($expanded) + [IO.Path]::DirectorySeparatorChar
        foreach ($entry in $zip.Entries) {
            $resolved = [IO.Path]::GetFullPath((Join-Path $expanded $entry.FullName))
            if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw '압축 파일에 허용하지 않은 경로가 있습니다.'
            }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $expanded)
    $source = Join-Path $expanded $manifest.archive_root
    foreach ($binary in $manifest.binaries) {
        $path = Join-Path $source ('bin/' + $binary.file)
        if ((Get-NoseOutSha256 $path) -ne $binary.sha256) {
            throw '압축을 푼 실행 파일의 해시가 다릅니다.'
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $source 'LICENSE.txt'))) {
        throw '공식 패키지의 라이선스 파일이 없습니다.'
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    if (Test-Path -LiteralPath $target) { throw '대상 폴더가 생겨 설치를 중단했습니다.' }
    Move-Item -LiteralPath $source -Destination $target
    Write-Host '완료했습니다. 이제 음성·영상 분석을 사용할 수 있습니다.'
} finally {
    # 삭제 대상은 이 실행이 만든, 앱 폴더 바로 아래의 고유한 임시 폴더 하나뿐이다.
    $resolvedTemporary = [IO.Path]::GetFullPath($temporary)
    if ((Split-Path -Parent $resolvedTemporary) -ne $AppRoot -or
        -not (Split-Path -Leaf $resolvedTemporary).StartsWith('.ffmpeg-download-')) {
        throw '임시 폴더 경로 검사에 실패해 정리하지 않았습니다.'
    }
    if (Test-Path -LiteralPath $resolvedTemporary) {
        Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force
    }
}
