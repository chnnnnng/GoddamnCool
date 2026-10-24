#!/usr/bin/env bash
# 客户端本地测速：从 addon 取一条播放直链，在你自己的电脑上直连下载测速。
# 用法: ./client_speedtest.sh [addon地址，默认 http://47.117.135.164]
# 依赖: curl、python3
set -euo pipefail

ADDON="${1:-http://47.117.135.164}"
STREAM_URL="$ADDON/stream/series/tt5925192%3A1%3A12.json"   # 余罪 S01E12

echo "从 $STREAM_URL 获取播放直链 ..."
json=$(curl -sf "$STREAM_URL")
url=$(printf '%s' "$json" | python3 -c "import sys,json;print(json.load(sys.stdin)['streams'][0]['url'])")
hdr=$(printf '%s' "$json" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin)['streams'][0]['behaviorHints']['proxyHeaders']['request']))")
cookie=$(printf '%s' "$hdr" | python3 -c "import sys,json;print(json.load(sys.stdin)['Cookie'])")
ua=$(printf '%s' "$hdr" | python3 -c "import sys,json;print(json.load(sys.stdin)['User-Agent'])")
echo "直链域名: $(printf '%s' "$url" | sed -E 's#https?://([^/]+)/.*#\1#')"

echo
echo "== 1) 模拟 Stremio 客户端直连（带 Cookie/UA，10 秒采样）=="
curl -sL -o /dev/null --max-time 10 \
  -H "Cookie: $cookie" -H "User-Agent: $ua" -H "Referer: https://pan.quark.cn/" \
  -w 'speed|%{speed_download}\n' "$url" | awk -F'|' '{printf "下载速度: %.0f KB/s\n", $2/1024}'

echo
echo "== 2) 纯直连（无 Cookie，10 秒采样）=="
curl -sL -o /dev/null --max-time 10 \
  -w 'speed|%{speed_download}\n' "$url" | awk -F'|' '{printf "下载速度: %.0f KB/s\n", $2/1024}'

echo
echo "参考：流媒体 1080p 约需 2-5 MB/s，720p 约 1-2.5 MB/s。"
