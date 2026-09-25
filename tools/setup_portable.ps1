# K-DayTrader 휴대용 Python 준비 스크립트 (Windows)
# 시스템에 Python 을 설치하지 않고, 프로그램 폴더의 runtime\python 에 공식 "임베디드 Python" 을 받아 사용한다.
param([Parameter(Mandatory = $true)][string]$Root)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # 다운로드 속도 향상
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

function Fail($msg) {
    Write-Host ""
    Write-Host "[실패] $msg" -ForegroundColor Red
    Write-Host "인터넷 연결(회사 방화벽/프록시)을 확인한 뒤 다시 실행하세요." -ForegroundColor Yellow
    Write-Host "계속 실패하면 https://www.python.org/downloads/ 에서 Python 3.12 를 설치해도 됩니다 (Add python.exe to PATH 체크)."
    exit 1
}

$rt = Join-Path $Root 'runtime\python'
$marker = Join-Path $rt '.kdt-ready'
if (Test-Path $marker) { exit 0 }
if (Test-Path $rt) { Remove-Item -Recurse -Force $rt }   # 이전에 중단된 설치 정리
New-Item -ItemType Directory -Force -Path $rt | Out-Null

Write-Host ""
Write-Host "==================================================="
Write-Host "  Python 이 없어 휴대용 Python 을 준비합니다 (최초 1회)"
Write-Host "  설치 위치: $rt"
Write-Host "  시스템 설치/관리자 권한 불필요, 약 2~5분 소요"
Write-Host "==================================================="

# 1) 임베디드 Python 다운로드
$arch = 'amd64'
$zip = Join-Path $env:TEMP 'kdt-python-embed.zip'
$ok = $false
foreach ($v in @('3.12.10', '3.12.9', '3.12.8', '3.11.9')) {
    $url = "https://www.python.org/ftp/python/$v/python-$v-embed-$arch.zip"
    try {
        Write-Host "[1/4] Python $v 내려받는 중… $url"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        $ok = $true
        break
    } catch { Write-Host "      실패: $($_.Exception.Message)" }
}
if (-not $ok) { Fail "Python 을 내려받지 못했습니다." }
Expand-Archive -Path $zip -DestinationPath $rt -Force
Remove-Item $zip -Force -ErrorAction SilentlyContinue

# 2) site-packages 와 프로그램 폴더를 모듈 경로에 추가 (._pth 파일)
$pth = Get-ChildItem -Path $rt -Filter 'python*._pth' | Select-Object -First 1
if (-not $pth) { Fail "._pth 파일을 찾지 못했습니다." }
$lines = Get-Content $pth.FullName | ForEach-Object { if ($_ -match '^\s*#\s*import site') { 'import site' } else { $_ } }
$lines = @($lines) + @('Lib\site-packages', '..\..')
Set-Content -Path $pth.FullName -Value $lines -Encoding ASCII

$py = Join-Path $rt 'python.exe'

# 3) pip 설치
Write-Host "[2/4] pip 설치 중…"
$getpip = Join-Path $rt 'get-pip.py'
try { Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getpip -UseBasicParsing } catch { Fail "get-pip.py 를 내려받지 못했습니다: $($_.Exception.Message)" }
& $py $getpip --no-warn-script-location --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "pip 설치에 실패했습니다." }
Remove-Item $getpip -Force -ErrorAction SilentlyContinue

# 4) 프로그램에 필요한 패키지 설치
Write-Host "[3/4] 필요한 패키지 설치 중… (numpy, pandas 등, 수 분 소요)"
& $py -m pip install --no-warn-script-location --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail "패키지 설치에 실패했습니다." }

Write-Host "[4/4] 확인 중…"
& $py -c "import numpy, pandas, yaml, requests, rich, websockets, tzdata; print('OK')"
if ($LASTEXITCODE -ne 0) { Fail "설치 확인에 실패했습니다." }

Set-Content -Path $marker -Value (Get-Date -Format s) -Encoding ASCII
Write-Host "휴대용 Python 준비 완료. 다음부터는 바로 실행됩니다." -ForegroundColor Green
exit 0
