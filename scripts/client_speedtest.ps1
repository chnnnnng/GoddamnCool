# 客户端本地测速：从 addon 取一条播放直链，在你自己的电脑上直连下载测速。
# 用法: powershell -File client_speedtest.ps1 [-Addon "http://47.117.135.164"]
param(
    [string]$Addon = "http://47.117.135.164"
)

$StreamUrl = "$Addon/stream/series/tt5925192%3A1%3A12.json"   # 余罪 S01E12

Write-Host "从 $StreamUrl 获取播放直链 ..."
$stream = Invoke-RestMethod -Uri $StreamUrl
$s = $stream.streams[0]
$url = $s.url
$h = $s.behaviorHints.proxyHeaders.request
$domain = ([uri]$url).Host
Write-Host "直链域名: $domain"

Write-Host ""
Write-Host "== 1) Simulated Stremio client (with Cookie/UA, 15s sampling) =="
curl.exe -sL -o NUL --max-time 15 `
  -H "Cookie: $($h.Cookie)" -H "User-Agent: $($h.'User-Agent')" -H "Referer: $($h.Referer)" `
  -w "Speed: %{speed_download} B/s`n" $url

Write-Host ""
Write-Host "== 2) Direct without Cookie (15s sampling) =="
curl.exe -sL -o NUL --max-time 15 -w "Speed: %{speed_download} B/s`n" $url

Write-Host ""
Write-Host "Reference: 1080p streaming needs about 2-5 MB/s, 720p about 1-2.5 MB/s."
