# 管理者の権限が無いPC向けに、Python を「自分のユーザーだけ」に入れる。
#
# python.org のインストーラーは InstallAllUsers=0 を渡すと、
# 自分のフォルダだけに入る（管理者の許可が不要）。
# PrependPath=1 で PATH にも入れるので、あとから python と打てば動く。

$ErrorActionPreference = "Stop"
$version = "3.12.10"          # 動作確認済みの版

try {
    $url = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
    $out = Join-Path $env:TEMP "python-$version-amd64.exe"

    Write-Host "  ダウンロードしています（Python $version）..."
    Invoke-WebRequest $url -OutFile $out -UseBasicParsing

    Write-Host "  インストールしています（自分のユーザーだけ・数分かかります）..."
    $p = Start-Process $out -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1", "Include_test=0"
    )
    Remove-Item $out -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { throw "インストーラーが $($p.ExitCode) で終了しました" }

    Write-Host "  [OK] Python を入れました。"
    exit 0
}
catch {
    Write-Host "  [NG] $($_.Exception.Message)"
    exit 1
}
