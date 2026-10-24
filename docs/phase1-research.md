# 阶段 1 调研报告：Stremio Cloud Drive Add-on

调研日期：2026-10-24 ｜ 状态：文档与接口调研完成，运行时验证待真实凭证

## 1. 调研对象与维护状态

### 1.1 OpenList（OpenListTeam/OpenList）
- AList 反信任危机社区分叉，Go，AGPL-3.0，24.9k★，最近推送 2026-10-03（活跃）。
- 夸克驱动位于 `drivers/quark_uc/`，维护者为 Cp0204（quark-auto-save 作者，端点实现权威度高）。
- 关键实现（util.go 已核实）：
  - 请求基座：`{api base}/1/clouddrive` + 查询参数 `pr=ucpro&fr=pc`，Cookie 鉴权；
  - 文件列表：`GET /file/sort`，参数 `pdir_fid/_size/_fetch_total/fetch_all_file`，按 `metadata.total` 翻页；
  - 下载直链：`POST /file/download`，body `{"fids":[fid]}`，取 `data[0].download_url`，
    且返回 Link 时附带 `Cookie/Referer/User-Agent` 头 —— 说明直链访问依赖请求头；
  - 转码播放：`POST /file/v2/play/project`，`{"fid","resolutions":"low,normal,high,super,2k,4k","supports":"fmp4_av,m3u8,dolby_vision"}`
    → 返回 fmp4/HLS(m3u8) 直链（不依赖 Cookie 头，利于客户端直连播放）；
  - `__puus`：每次响应的 Set-Cookie 需回写保存（风控/限流对抗，关键工程细节）。

### 1.2 quark-auto-save（Cp0204）
- Python，AGPL-3.0，3.1k★，最近推送 2026-07-09（活跃）。
- `quark_auto_save.py` 中的 `Quark` 类（约 52KB）覆盖：cookie 解析（kps/sign/vcode 移动端参数）、
  分享页 token、分享文件列表、提交转存、`GET /1/clouddrive/task?task_id&retry_index` 轮询
  （status：1=进行中 2=成功 3=失败）、下载直链、`MagicRename` 剧集正则（S/E 提取可复用到 MovieMatcher）。
- `app/sdk/pansou.py`：外部聚合搜索 SDK —— `GET {server}/api/search?kw=...&cloud_types=["quark"]&res=merge`
  → 返回 `[{shareurl, taskname, content, datetime, source}]`，即阶段 3 ResourceSearchProvider 的首选参考。

### 1.3 Stremio Add-on 协议（stremio-core / addon-sdk 文档核实）
- 路由：`/{resource}/{type}/{id}.json`；Stream：`GET /stream/movie/tt1254207.json`，
  剧集为 `tt1254207:1:2`（season:episode）→ `{"streams":[...]}`。
- Stream 对象：`url / name / title / description / behaviorHints{notWebReady, proxyHeaders,
  bingeGroup, filename, videoSize}`；`proxyHeaders` 仅 Stremio 桌面端内置 server 生效。
- 结论：协议为纯 HTTP+JSON，**纯 Python（FastAPI）可直接实现**，无需 Node.js/官方 SDK。

## 2. 技术决策
1. 单服务纯 Python：FastAPI + httpx(async) + SQLite；不引入 Node.js。
2. 许可证：本项目采用 AGPL-3.0；对参考项目仅做端点/流程级 clean-room 重写。
3. 播放双轨：优先转码 HLS 直链（无头依赖）；原画直链用 `proxyHeaders+notWebReady`。
4. 外部检索：阶段 3 接入自托管 PanSou，统一 `ResourceSearchProvider` 抽象。

## 3. 已知风险
- 所有夸克接口均为非官方私有接口，可能随版本变更；已在设计中以 Provider 隔离。
- Cookie 登录态无官方 OAuth，存在失效/风控；需 `__puus` 回写、失效检测与用户重授权。
- 原画直链依赖请求头，Web 客户端无法带头 → 需实测；不可行则退 HLS 转码或受控代理。
- PanSou 类聚合源质量参差，仅作候选展示，不承诺可用性。

## 4. 最小链路验证方案（本阶段核心任务）
链路：个人网盘文件 → 可播放 URL → Stremio 客户端播放
1. `python scripts/poc_quark_verify.py verify`：真实 Cookie 验证 登录→列目录/搜索→取直链→
   Range 请求/响应头检查（HTTP 状态、Content-Type/Length、206 支持、重定向）。
2. `python scripts/poc_quark_verify.py serve`：起最小 Stremio Add-on（manifest + 静态 stream，
   URL 来自第 1 步验证过的直链），按 README 步骤在 Stremio 客户端实测播放与拖动进度条。
3. 记录实测结果回填本报告第 5 节。

## 5. 实测结果（2026-10-24，真实账号验证）

- [x] Cookie 有效性 — 登录态有效，列目录/搜索正常
- [x] 文件搜索可用 — `file/search` 命中目标视频（余罪.S01E12.2016.1080P WEB-DL.mp4，831MB）
- [x] 原画直链获取 — `POST /file/download` 返回 `dl-pc-sz.drive.quark.cn` 签名直链
- [ ] ~~原画直链可直连（无头访问）~~ — **实测失败**：无头访问返回 262 字节 HTML 错误页；
      **必须携带 Cookie(`__puus`)+Referer+UA**，带头后 206 Partial Content + `video/mp4` ✅
      → 结论：播放必须走 Stremio `behaviorHints.proxyHeaders`（桌面端/TV 可用，Web 端不可用）或受控代理
- [x] Range 请求返回 206 — 带头请求 `Range: bytes=0-1023` 返回 206 且 Content-Length 正确
- [ ] 转码 HLS 直链可直连 — **未通过**：`POST /file/v2/play/project` 恒返回
      `400 code=14018 plf_invalid`（platform 校验拒绝）。已排除变量：drive-pc/drive 双基址、
      OpenList 精确 UA（Electron 18.3.5.4）、纯 Chrome UA、Origin/Referer、plf/plat/fr 参数、
      resolutions 数组化 —— 全部失败。OpenList 同参数组合亦复现，疑为接口近期变更，转"未解决问题"
- [ ] Stremio 桌面端实际播放（直链 + proxyHeaders）— 待用户实测
      （POC Addon 已就绪：`poc_quark_verify.py serve` → `stremio://127.0.0.1:7001/manifest.json`）
- [ ] 进度拖动正常 — 待用户实测（Range 206 已确认，理论可行）

### 未解决问题
1. `plf_invalid`：转码 HLS 端点被平台校验拦截，暂无法获得无头可播的 HLS 直链。
   后续路径：a) 浏览器抓包网页播放器的真实请求对比差异；b) 评估夸克官方开放平台
   `open-api-drive.quark.cn`（但 OpenList 标记 OnlyProxy，直连性存疑）；c) 接受直链+proxyHeaders 方案。
2. 夸克开放平台 API 仅支持 OAuth 应用接入且输出需代理，对个人部署门槛高，暂缓。