# 管理者の権限が無いPCに、GitHub CLI（gh）を入れる。
#
# gh は「PRを作る」ためだけに使う開発用の道具。担当者のPCには要らない。
# 会社のPCでは、インストーラーが管理者の許可を求めて進めないことがあるので、
# ① まず winget（許可が要るが速い）→ ② 駄目なら zip を展開するだけ（許可不要）
# の順で試す。
#
# 置き場所（②のとき）： %LOCALAPPDATA%\Programs\gh
# 展開したあと、そのフォルダを自分のPATHに登録する（次に開く画面から gh が使える）。

$ErrorActionPreference = "Stop"
$dest = Join-Path $env:LOCALAPPDATA "Programs\gh"

function Test-Gh {
    try { & gh --version | Out-Null; return $true } catch { return $false }
}

if (Test-Gh) {
    Write-Host "  すでに入っています。"
    gh --version
    exit 0
}

# ① winget（あれば、こちらのほうが後々の更新も楽）
try {
    & winget --version | Out-Null
    Write-Host "  winget で入れてみます（許可を求められたら「はい」を押してください）..."
    & winget install --id GitHub.cli -e --source winget --accept-source-agreements --accept-package-agreements
    $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("PATH", "User")
    if (Test-Gh) {
        Write-Host "  [OK] gh を用意しました。"
        exit 0
    }
    Write-Host "  winget では入りませんでした。展開して使う形でやり直します。"
}
catch {
    Write-Host "  winget が使えないので、展開して使う形で入れます。"
}

# ② zip を展開するだけ（レジストリも Program Files も触らないので許可が要らない）
try {
    $rel = Invoke-RestMethod "https://api.github.com/repos/cli/cli/releases/latest" `
                             -Headers @{ "User-Agent" = "enkan-ai" }
    $asset = $rel.assets | Where-Object { $_.name -like "gh_*_windows_amd64.zip" } |
             Select-Object -First 1
    if (-not $asset) { throw "windows 用の zip が見つかりませんでした" }

    $out = Join-Path $env:TEMP $asset.name
    Write-Host "  ダウンロードしています（$($asset.name)）..."
    Invoke-WebRequest $asset.browser_download_url -OutFile $out -UseBasicParsing

    Write-Host "  展開しています..."
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Expand-Archive -Path $out -DestinationPath $dest -Force
    Remove-Item $out -ErrorAction SilentlyContinue

    # zip の中は gh_<版>_windows_amd64in\gh.exe。版が変わるので探して見つける
    $exe = Get-ChildItem $dest -Recurse -Filter "gh.exe" | Select-Object -First 1
    if (-not $exe) { throw "展開できませんでした（gh.exe が見当たりません）" }

    # 次に開く画面からも gh と打てるように、自分のPATHに残す
    $binDir = $exe.Directory.FullName
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not $userPath) { $userPath = "" }
    if ($userPath -notlike "*$binDir*") {
        [Environment]::SetEnvironmentVariable("PATH", ($userPath.TrimEnd(";") + ";" + $binDir), "User")
        Write-Host "  PATH に登録しました。"
    }

    & $exe.FullName --version
    Write-Host "  [OK] gh を用意しました。"
    exit 0
}
catch {
    Write-Host "  [NG] $($_.Exception.Message)"
    exit 1
}
