# 管理者の権限が無いPC向けに、Git を「自分のユーザーだけ」に入れる。
#
# 会社のPCでは「このアプリが変更を加えることを許可しますか？」に
# 答えられない（管理者のIDを求められる）ことがある。
# Git for Windows のインストーラーは、自分のフォルダだけに入れるモードを
# 持っているので、そちらで入れる。PCの他の利用者には影響しない。

$ErrorActionPreference = "Stop"

try {
    Write-Host "  Git の最新版を探しています..."
    $rel = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest" `
                             -Headers @{ "User-Agent" = "enkan-ai" }
    $asset = $rel.assets | Where-Object { $_.name -like "Git-*-64-bit.exe" } | Select-Object -First 1
    if (-not $asset) { throw "インストーラーが見つかりませんでした" }

    $out = Join-Path $env:TEMP $asset.name
    Write-Host "  ダウンロードしています（$($asset.name)）..."
    Invoke-WebRequest $asset.browser_download_url -OutFile $out -UseBasicParsing

    Write-Host "  インストールしています（自分のユーザーだけ・数分かかります）..."
    # /CURRENTUSER … 自分のフォルダだけに入れる（管理者の許可が不要）
    $p = Start-Process $out -Wait -PassThru -ArgumentList @(
        "/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-", "/CURRENTUSER",
        "/COMPONENTS=gitlfs,assoc_sh"
    )
    Remove-Item $out -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { throw "インストーラーが $($p.ExitCode) で終了しました" }

    Write-Host "  [OK] Git を入れました。"
    exit 0
}
catch {
    Write-Host "  [NG] $($_.Exception.Message)"
    exit 1
}
