[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Topic = "",

    [switch]$NoOpen,

    [Alias("help", "h")]
    [switch]$ShowHelp
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

if ($ShowHelp -or $Topic -eq "/?") {
    Write-Host "用法："
    Write-Host "  双击 start-report.cmd 后按提示操作"
    Write-Host '  start-report.cmd "Pilbara 锂矿"'
    Write-Host '  powershell -ExecutionPolicy Bypass -File .\start-report.ps1 -Topic "铜价走势" -NoOpen'
    exit 0
}

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Read-SecretText {
    param([string]$Prompt)

    $SecureValue = Read-Host $Prompt -AsSecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

function Initialize-EnvironmentFile {
    $EnvPath = Join-Path $ProjectRoot ".env"
    $TemplatePath = Join-Path $ProjectRoot ".env.example"
    $NeedsKey = $true

    if (-not [string]::IsNullOrWhiteSpace($env:LLM_API_KEY) -and
        $env:LLM_API_KEY -ne "your-api-key-here") {
        $NeedsKey = $false
    }

    if (Test-Path -LiteralPath $EnvPath) {
        $Existing = Get-Content -Raw -LiteralPath $EnvPath -Encoding UTF8
        $Match = [regex]::Match($Existing, "(?m)^LLM_API_KEY=(.+)$")
        if ($Match.Success -and $Match.Groups[1].Value.Trim() -notin @("", "your-api-key-here")) {
            $NeedsKey = $false
        }
    }
    elseif (Test-Path -LiteralPath $TemplatePath) {
        Copy-Item -LiteralPath $TemplatePath -Destination $EnvPath
    }
    else {
        @(
            "LLM_API_KEY=your-api-key-here"
            "LLM_BASE_URL=https://api.deepseek.com"
            "LLM_MODEL=deepseek-chat"
            "NEWS_DAYS_DEFAULT=1"
            "DEFAULT_REPORT_URL="
        ) | Set-Content -LiteralPath $EnvPath -Encoding UTF8
    }

    if (-not $NeedsKey) {
        return
    }

    Write-Host "首次运行需要 DeepSeek API Key；输入内容不会显示在屏幕上。" -ForegroundColor Yellow
    $ApiKey = Read-SecretText "DeepSeek API Key"
    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        throw "未输入 DeepSeek API Key，无法继续。"
    }

    $Content = Get-Content -Raw -LiteralPath $EnvPath -Encoding UTF8
    if ($Content -match "(?m)^LLM_API_KEY=.*$") {
        $Content = [regex]::Replace(
            $Content,
            "(?m)^LLM_API_KEY=.*$",
            "LLM_API_KEY=$ApiKey",
            1
        )
    }
    else {
        $Content = "LLM_API_KEY=$ApiKey`r`n$Content"
    }
    Set-Content -LiteralPath $EnvPath -Value $Content -Encoding UTF8 -NoNewline
}

function Get-UvExecutable {
    $Installed = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $Installed) {
        return $Installed.Source
    }

    $ToolsDirectory = Join-Path $ProjectRoot ".tools"
    $UvExecutable = Join-Path $ToolsDirectory "uv.exe"
    if (Test-Path -LiteralPath $UvExecutable) {
        return $UvExecutable
    }

    Write-Step "首次运行：下载便携版 uv"
    New-Item -ItemType Directory -Path $ToolsDirectory -Force | Out-Null
    $Architecture = $env:PROCESSOR_ARCHITECTURE
    if ($Architecture -eq "ARM64") {
        $Target = "aarch64-pc-windows-msvc"
    }
    else {
        $Target = "x86_64-pc-windows-msvc"
    }
    $Version = "0.12.3"
    $DownloadUrl = "https://github.com/astral-sh/uv/releases/download/$Version/uv-$Target.zip"
    $ZipPath = Join-Path $ToolsDirectory "uv-download.zip"
    $ExtractPath = Join-Path $ToolsDirectory "uv-download"

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath -UseBasicParsing
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractPath -Force
    $DownloadedUv = Get-ChildItem -LiteralPath $ExtractPath -Filter "uv.exe" -Recurse |
        Select-Object -First 1
    if ($null -eq $DownloadedUv) {
        throw "uv 下载成功，但压缩包中没有找到 uv.exe。"
    }
    Copy-Item -LiteralPath $DownloadedUv.FullName -Destination $UvExecutable -Force
    return $UvExecutable
}

try {
    Write-Host "矿权日报 Agent 启动器" -ForegroundColor Green
    Write-Host "项目目录：$ProjectRoot"

    Initialize-EnvironmentFile
    $Uv = Get-UvExecutable

    if ([string]::IsNullOrWhiteSpace($Topic)) {
        $Topic = Read-Host "日报主题（直接回车使用 Pilbara 锂矿）"
    }
    if ([string]::IsNullOrWhiteSpace($Topic)) {
        $Topic = "Pilbara 锂矿"
    }

    $env:UV_LINK_MODE = "copy"
    Write-Step "准备 Python 3.12 和项目依赖（首次运行耗时较长）"
    & $Uv sync --locked --no-dev
    if ($LASTEXITCODE -ne 0) {
        throw "依赖安装失败，退出码：$LASTEXITCODE"
    }

    Write-Step "生成“$Topic”日报"
    & $Uv run --locked --no-dev mining-daily-agent $Topic
    if ($LASTEXITCODE -ne 0) {
        throw "日报生成失败，退出码：$LASTEXITCODE"
    }

    $LatestReport = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "reports") -Filter "*.md" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $LatestReport) {
        throw "程序已结束，但 reports 目录中没有找到日报。"
    }

    Write-Host "`n日报路径：$($LatestReport.FullName)" -ForegroundColor Green
    if (-not $NoOpen) {
        Start-Process -FilePath $LatestReport.FullName
    }
}
catch {
    Write-Host "`n错误：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

exit 0
