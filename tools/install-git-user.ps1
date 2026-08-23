# 管理者の権限が無いPCに、Git を入れる（インストールではなく「展開」する）。
#
# 会社のPCでは、インストーラーが管理者の許可を求めて進めないことがある。
# Git for Windows には PortableGit という、展開するだけで使える形が用意されている。
# レジストリもProgram Filesも触らないので、許可を求められない。
#
# 置き場所： %LOCALAPPDATA%\Programs\PortableGit
# 展開したあと、そのフォルダを自分のPATHに登録する（次に開く画面から git が使える）。

$ErrorActionPreference = "Stop"
$dest = Join-Path $env:LOCALAPPDATA "Programs\PortableGit"

try {
    if (Test-Path (Join-Path $dest "cmd\git.exe")) {
        Write-Host "  すでに $dest にあります。"
    }
    else {
        Write-Host "  Git（展開して使う版）を探しています..."
        $rel = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest" `
                                 -Headers @{ "User-Agent" = "enkan-ai" }
        $asset = $rel.assets | Where-Object { $_.name -like "PortableGit-*-64-bit.7z.exe" } |
                 Select-Object -First 1
        if (-not $asset) { throw "PortableGit が見つかりませんでした" }

        $out = Join-Path $env:TEMP $asset.name
        Write-Host "  ダウンロードしています（$($asset.name)）..."
        Invoke-WebRequest $asset.browser_download_url -OutFile $out -UseBasicParsing

        Write-Host "  展開しています（数分かかります）..."
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        # 7z の自己解凍書庫。-o で展開先、-y で確認なし
        $p = Start-Process $out -Wait -PassThru -ArgumentList @("-o`"$dest`"", "-y")
        Remove-Item $out -ErrorAction SilentlyContinue
        if (-not (Test-Path (Join-Path $dest "cmd\git.exe"))) {
            throw "展開できませんでした（終了コード $($p.ExitCode)）"
        }
    }

    # 次に開く画面からも git と打てるように、自分のPATHに残す
    $cmdDir = Join-Path $dest "cmd"
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not $userPath) { $userPath = "" }
    if ($userPath -notlike "*$cmdDir*") {
        [Environment]::SetEnvironmentVariable("PATH", ($userPath.TrimEnd(";") + ";" + $cmdDir), "User")
        Write-Host "  PATH に登録しました。"
    }

    & (Join-Path $cmdDir "git.exe") --version
    Write-Host "  [OK] Git を用意しました。"
    exit 0
}
catch {
    Write-Host "  [NG] $($_.Exception.Message)"
    exit 1
}
