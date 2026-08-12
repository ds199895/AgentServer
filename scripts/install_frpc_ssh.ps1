#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$')]
    [string]$DeviceId,

    [Parameter(Mandatory = $true)]
    [ValidateRange(20000, 29999)]
    [int]$RemotePort,

    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$SshUser = $env:USERNAME,
    [ValidatePattern('^[A-Za-z0-9.:-]+$')]
    [string]$Server = '101.43.103.46',
    [int]$ServerPort = 7000,
    [string]$FrpVersion = '0.69.0',
    [SecureString]$FrpToken
)

$ErrorActionPreference = 'Stop'
$AgentServerPublicKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAntuN6lYuNHu8i69zyvGFUlRPm+QL/Ek9ntvubJLqyM agentserver-fleet'

if ($FrpVersion -ne '0.69.0') {
    throw '此安装器只包含 frp 0.69.0 的官方校验值。'
}

$OtherFrpc = Get-Process -Name frpc -ErrorAction SilentlyContinue
$ManagedService = Get-Service -Name 'AgentServerFrpc' -ErrorAction SilentlyContinue
if ($OtherFrpc -and -not $ManagedService) {
    throw '检测到已有 frpc。为保证每台设备只有一个 frpc，请先合并配置或停止旧服务。'
}

if (-not $FrpToken) {
    $FrpToken = Read-Host '请输入 FRP token（输入不会显示）' -AsSecureString
}
$TokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($FrpToken)
try {
    $PlainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($TokenPointer)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TokenPointer)
}
if ([string]::IsNullOrWhiteSpace($PlainToken)) {
    throw 'FRP token 不能为空。'
}

$Architecture = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'amd64' }
    'ARM64' { 'arm64' }
    default { throw "不支持的 Windows CPU 架构: $env:PROCESSOR_ARCHITECTURE" }
}
$Archive = "frp_${FrpVersion}_windows_${Architecture}.zip"
$ExpectedSha = switch ($Architecture) {
    'amd64' { '0e38f6dbe7761d648ca5c6ee323b7309544f48c01e9476f553902f3bc0949089' }
    'arm64' { 'ce49e6ad107005ad148974990ba30b2a7c097fb874b5f95897b8b32c6fc79e07' }
}

$InstallDir = Join-Path $env:ProgramFiles 'AgentServer\frp'
$ConfigDir = Join-Path $env:ProgramData 'AgentServer\frp'
$TempDir = Join-Path ([IO.Path]::GetTempPath()) ("agentserver-frp-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $InstallDir, $ConfigDir, $TempDir | Out-Null

try {
    $ArchivePath = Join-Path $TempDir $Archive
    $DownloadUrl = "https://github.com/fatedier/frp/releases/download/v${FrpVersion}/${Archive}"
    Write-Host "下载 $DownloadUrl"
    Invoke-WebRequest -UseBasicParsing -Uri $DownloadUrl -OutFile $ArchivePath
    $ActualSha = (Get-FileHash -Algorithm SHA256 $ArchivePath).Hash.ToLowerInvariant()
    if ($ActualSha -ne $ExpectedSha) {
        throw 'SHA-256 校验失败。'
    }
    Expand-Archive -Path $ArchivePath -DestinationPath $TempDir -Force
    $ExtractedDir = Join-Path $TempDir "frp_${FrpVersion}_windows_${Architecture}"
    Copy-Item (Join-Path $ExtractedDir 'frpc.exe') (Join-Path $InstallDir 'frpc.exe') -Force
} finally {
    Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
}

$TokenPath = Join-Path $ConfigDir 'token'
$ConfigPath = Join-Path $ConfigDir 'frpc.toml'
[IO.File]::WriteAllText($TokenPath, $PlainToken + [Environment]::NewLine, (New-Object Text.UTF8Encoding $false))
$PlainToken = $null

$Config = @"
clientID = "$DeviceId"
user = "$DeviceId"
serverAddr = "$Server"
serverPort = $ServerPort
loginFailExit = false

auth.method = "token"
auth.tokenSource.type = "file"
auth.tokenSource.file.path = "$($TokenPath.Replace('\', '\\'))"

transport.tls.enable = true

[[proxies]]
name = "ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = $RemotePort

[proxies.annotations]
device_id = "$DeviceId"
ssh_user = "$SshUser"
service = "ssh"
"@
[IO.File]::WriteAllText($ConfigPath, $Config, (New-Object Text.UTF8Encoding $false))
& icacls.exe $ConfigDir /inheritance:r /grant 'Administrators:(OI)(CI)F' /grant 'SYSTEM:(OI)(CI)F' | Out-Null

& (Join-Path $InstallDir 'frpc.exe') verify -c $ConfigPath
if ($LASTEXITCODE -ne 0) { throw 'frpc 配置校验失败。' }

$SshCapability = Get-WindowsCapability -Online | Where-Object Name -Like 'OpenSSH.Server*'
if (-not $SshCapability) { throw '当前 Windows 版本不提供 OpenSSH.Server capability。' }
if ($SshCapability.State -ne 'Installed') {
    Write-Host '安装 Windows OpenSSH Server...'
    Add-WindowsCapability -Online -Name $SshCapability.Name | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd

$UserInfo = Get-LocalUser -Name $SshUser
$IsAdministrator = $false
try {
    $AdminMembers = Get-LocalGroupMember -Group 'Administrators' -ErrorAction Stop
    $IsAdministrator = $AdminMembers.SID -contains $UserInfo.SID
} catch {
    $IsAdministrator = $SshUser -eq 'Administrator'
}

if ($IsAdministrator) {
    $AuthorizedKeys = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
    New-Item -ItemType File -Force -Path $AuthorizedKeys | Out-Null
    $ExistingKeys = Get-Content $AuthorizedKeys -ErrorAction SilentlyContinue
    if ($ExistingKeys -notcontains $AgentServerPublicKey) {
        Add-Content -Path $AuthorizedKeys -Value $AgentServerPublicKey
    }
    & icacls.exe $AuthorizedKeys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
} else {
    $UserHome = Join-Path 'C:\Users' $SshUser
    if (-not (Test-Path $UserHome)) { throw "找不到用户主目录: $UserHome" }
    $SshDir = Join-Path $UserHome '.ssh'
    $AuthorizedKeys = Join-Path $SshDir 'authorized_keys'
    New-Item -ItemType Directory -Force -Path $SshDir | Out-Null
    New-Item -ItemType File -Force -Path $AuthorizedKeys | Out-Null
    $ExistingKeys = Get-Content $AuthorizedKeys -ErrorAction SilentlyContinue
    if ($ExistingKeys -notcontains $AgentServerPublicKey) {
        Add-Content -Path $AuthorizedKeys -Value $AgentServerPublicKey
    }
    & icacls.exe $SshDir /inheritance:r /grant "${SshUser}:(OI)(CI)F" /grant 'SYSTEM:(OI)(CI)F' | Out-Null
}

$ServiceName = 'AgentServerFrpc'
$BinaryPath = '"' + (Join-Path $InstallDir 'frpc.exe') + '" -c "' + $ConfigPath + '"'
$ExistingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($ExistingService) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & sc.exe config $ServiceName binPath= $BinaryPath start= auto | Out-Null
} else {
    New-Service -Name $ServiceName -BinaryPathName $BinaryPath -DisplayName 'AgentServer FRP SSH Tunnel' -StartupType Automatic | Out-Null
}
Start-Service -Name $ServiceName

Write-Host ''
Write-Host '安装完成' -ForegroundColor Green
Write-Host "设备 ID: $DeviceId"
Write-Host "代理名称: ${DeviceId}.ssh"
Write-Host "SSH 用户: $SshUser"
Write-Host "服务器入口: ${Server}:$RemotePort"
Write-Host '返回 AgentServer 页面，等待约 15 秒后点击“同步 FRP”。'
