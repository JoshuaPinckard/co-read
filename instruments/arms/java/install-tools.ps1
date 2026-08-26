[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$toolsRoot = Join-Path $projectRoot 'tools\java'
$downloads = Join-Path $toolsRoot 'downloads'
$jdks = Join-Path $toolsRoot 'jdks'

function Assert-ZipInstallMatches {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Installed,
        [Parameter(Mandatory = $true)][string]$DownloadsRoot
    )
    if (-not (Test-Path -LiteralPath $Installed -PathType Container)) {
        throw "installed directory is absent: $Installed"
    }
    $stage = Join-Path $DownloadsRoot ('.verify-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    try {
        Expand-Archive -LiteralPath $Archive -DestinationPath $stage
        $roots = @(Get-ChildItem -LiteralPath $stage -Force)
        if ($roots.Count -ne 1 -or -not $roots[0].PSIsContainer) {
            throw "${Archive}: archive did not contain one root directory"
        }
        $archiveRoot = $roots[0].FullName
        $expected = @{}
        foreach ($file in Get-ChildItem -LiteralPath $archiveRoot -Recurse -File) {
            $relative = $file.FullName.Substring($archiveRoot.Length).TrimStart('\', '/').Replace('\', '/')
            $expected[$relative] = [pscustomobject]@{
                Length = $file.Length
                Sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
            }
        }
        $observed = @{}
        $installedFull = (Resolve-Path -LiteralPath $Installed).Path
        foreach ($file in Get-ChildItem -LiteralPath $installedFull -Recurse -File) {
            $relative = $file.FullName.Substring($installedFull.Length).TrimStart('\', '/').Replace('\', '/')
            $observed[$relative] = [pscustomobject]@{
                Length = $file.Length
                Sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
            }
        }
        if ($expected.Count -ne $observed.Count) {
            throw "${Archive}: installed file count differs ($($observed.Count) vs $($expected.Count))"
        }
        foreach ($relative in $expected.Keys) {
            if (-not $observed.ContainsKey($relative)) {
                throw "${Archive}: installed file is missing: $relative"
            }
            if ($expected[$relative].Length -ne $observed[$relative].Length -or
                $expected[$relative].Sha256 -ne $observed[$relative].Sha256) {
                throw "${Archive}: installed bytes differ: $relative"
            }
        }
    }
    finally {
        $resolvedStage = [System.IO.Path]::GetFullPath($stage)
        $resolvedDownloads = [System.IO.Path]::GetFullPath($DownloadsRoot)
        if ((Split-Path -Parent $resolvedStage) -ne $resolvedDownloads) {
            throw "unsafe verification cleanup target: $resolvedStage"
        }
        if (Test-Path -LiteralPath $resolvedStage) {
            Remove-Item -LiteralPath $resolvedStage -Recurse -Force
        }
    }
}

$artifacts = @(
    [pscustomobject]@{
        Name = 'javaparser-core-3.28.2.jar'
        Url = 'https://repo.maven.apache.org/maven2/com/github/javaparser/javaparser-core/3.28.2/javaparser-core-3.28.2.jar'
        Bytes = 1493312
        Sha256 = 'b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd'
        Destination = Join-Path $toolsRoot 'javaparser-core-3.28.2.jar'
        ExtractTo = $null
    },
    [pscustomobject]@{
        Name = 'OpenJDK25U-jdk_x64_windows_hotspot_25.0.4.1_1.zip'
        Url = 'https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.4.1%2B1/OpenJDK25U-jdk_x64_windows_hotspot_25.0.4.1_1.zip'
        Bytes = 141167264
        Sha256 = '00c847d804f4a78e9f04f2683faf14fed898535b177b7fc704486cb0284e9283'
        Destination = Join-Path $downloads 'OpenJDK25U-jdk_x64_windows_hotspot_25.0.4.1_1.zip'
        ExtractTo = Join-Path $jdks 'temurin-25.0.4.1+1'
    },
    [pscustomobject]@{
        Name = 'OpenJDK11U-jdk_x64_windows_hotspot_11.0.32_9.zip'
        Url = 'https://github.com/adoptium/temurin11-binaries/releases/download/jdk-11.0.32%2B9/OpenJDK11U-jdk_x64_windows_hotspot_11.0.32_9.zip'
        Bytes = 199447338
        Sha256 = 'c1a7c406b72fbbd30417d4b0fcf5cccd9318f15fd269f84fcece38d70b21e181'
        Destination = Join-Path $downloads 'OpenJDK11U-jdk_x64_windows_hotspot_11.0.32_9.zip'
        ExtractTo = Join-Path $jdks 'temurin-11.0.32+9'
    },
    [pscustomobject]@{
        Name = 'OpenJDK8U-jdk_x64_windows_hotspot_8u502b07.zip'
        Url = 'https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u502-b07/OpenJDK8U-jdk_x64_windows_hotspot_8u502b07.zip'
        Bytes = 106454869
        Sha256 = '3f193fe5e36409c564eb3b7668cb33cab96aa5879d9b284f25f8653e993b1c49'
        Destination = Join-Path $downloads 'OpenJDK8U-jdk_x64_windows_hotspot_8u502b07.zip'
        ExtractTo = Join-Path $jdks 'temurin-8u502-b07'
    },
    [pscustomobject]@{
        Name = 'apache-maven-3.9.16-bin.zip'
        Url = 'https://downloads.apache.org/maven/maven-3/3.9.16/binaries/apache-maven-3.9.16-bin.zip'
        Bytes = 9395475
        Sha256 = '5af3b743dd8b876b5c45da33b676251e5f1687712644abb4ee519ca56e1d89ce'
        Destination = Join-Path $downloads 'apache-maven-3.9.16-bin.zip'
        ExtractTo = Join-Path $toolsRoot 'apache-maven-3.9.16'
    }
)

New-Item -ItemType Directory -Force -Path $toolsRoot, $downloads, $jdks | Out-Null

foreach ($artifact in $artifacts) {
    $destination = $artifact.Destination
    if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
        Write-Host "Downloading $($artifact.Name)"
        Invoke-WebRequest -Uri $artifact.Url -OutFile $destination -UseBasicParsing
    }
    $item = Get-Item -LiteralPath $destination
    if ($item.Length -ne $artifact.Bytes) {
        throw "$($artifact.Name): expected $($artifact.Bytes) bytes, observed $($item.Length)"
    }
    $observed = (Get-FileHash -Algorithm SHA256 -LiteralPath $destination).Hash.ToLowerInvariant()
    if ($observed -ne $artifact.Sha256) {
        throw "$($artifact.Name): SHA-256 mismatch: $observed"
    }
    Write-Host "Verified $($artifact.Name) $observed"

    if ($null -ne $artifact.ExtractTo -and -not (Test-Path -LiteralPath $artifact.ExtractTo)) {
        $stage = Join-Path $downloads ('.extract-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $stage | Out-Null
        Expand-Archive -LiteralPath $destination -DestinationPath $stage
        $roots = @(Get-ChildItem -LiteralPath $stage -Force)
        if ($roots.Count -ne 1 -or -not $roots[0].PSIsContainer) {
            throw "$($artifact.Name): archive did not contain one root directory"
        }
        Move-Item -LiteralPath $roots[0].FullName -Destination $artifact.ExtractTo
        Remove-Item -LiteralPath $stage
        Write-Host "Extracted $($artifact.Name) to $($artifact.ExtractTo)"
    }
    if ($null -ne $artifact.ExtractTo) {
        Assert-ZipInstallMatches -Archive $destination -Installed $artifact.ExtractTo -DownloadsRoot $downloads
        Write-Host "Verified installed tree for $($artifact.Name)"
    }
}

$mavenSha512 = 'ed41650d42485cfc243fad22158caf9cbb5dc408ce7a09ddb94dd42a019de929ca43065bfa450612cf12bf78b5cafa3884b96c090de326ff590448c933454af3'
$mavenArchive = Join-Path $downloads 'apache-maven-3.9.16-bin.zip'
$observedMavenSha512 = (Get-FileHash -Algorithm SHA512 -LiteralPath $mavenArchive).Hash.ToLowerInvariant()
if ($observedMavenSha512 -ne $mavenSha512) {
    throw "apache-maven-3.9.16-bin.zip: official SHA-512 mismatch: $observedMavenSha512"
}
Write-Host "Verified Apache Maven official SHA-512 $observedMavenSha512"
