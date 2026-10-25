"""大语言模型（LLM）高级功能：搜索结果 AI 精选与无标题文件名兜底解析。

管理员在 /admin 配置 OpenAI 兼容 API（LLM_API_BASE / LLM_API_KEY /
LLM_MODEL）后启用；三项任一为空即视为未配置，所有 AI 端点返回 501、
前端隐藏入口，功能自动退回基础（规则）实现。

设计要点（控制 API 开销与隐私）：
- 只发送"小而结构化"的输入：候选元数据（标题/来源/发布时间/存活状态）或
  无标题文件名及其上下文；绝不发送用户网盘文件列表（隐私且量大易变）；
- 结果缓存（kv_cache，key 含 skill 版本 + 输入内容哈希，TTL 1 天）：
  相同输入不重复调用；变化频繁的内容（用户网盘目录等）不作为模型输入；
- skill 提示词集中在本模块并带版本号，要求模型仅输出 JSON；调整提示词
  或输出结构时提升版本号即可让旧缓存自然失效；
- 模型输出经严格校验（类型/下标越界/截断）后才返回给前端。
"""
import hashlib
import json
import logging
import os
import re

import httpx

from . import config, db, settings

log = logging.getLogger(__name__)

_TIMEOUT = 180.0     # 单次 chat completions 超时（秒）——精选+解析合并任务
                     # 输入/输出都大（30 候选 × 40 文件规范名），60s 会超时
_CACHE_TTL = 86400.0  # AI 结果缓存（1 天，与搜索结果缓存一致）
_MASK_MARK = "••••"   # API Key 回显打码标记（含此标记的提交视为"未修改"）
REVIEW_SKILL_VERSION = 2  # v2：精选+文件名解析合并为一次调用（升级使旧缓存失效）
PARSE_SKILL_VERSION = 1


class LLMError(Exception):
    """LLM 调用/输出解析失败（路由层转 502）。"""


def configured() -> bool:
    """管理员是否已完整配置 LLM API（三项全部非空）。"""
    return bool(settings.get("LLM_API_BASE").strip()
                and settings.get("LLM_API_KEY").strip()
                and settings.get("LLM_MODEL").strip())


def masked_key(key: str) -> str:
    """API Key 回显打码：未配置返回空串，已配置仅暴露末 4 位便于辨认。"""
    key = (key or "").strip()
    return f"{_MASK_MARK}{key[-4:]}" if key else ""


def is_masked(value: str) -> bool:
    """提交的 Key 是否为打码占位值（管理端保存时跳过，避免覆盖真实 Key）。"""
    return _MASK_MARK in (value or "")


def _chat_payload(model: str, messages: list[dict], temperature: float) -> dict:
    """构造请求体。Qwen3 等思考型模型关闭思考链（DashScope 兼容模式参数
    enable_thinking=false，响应更快、token 更省）；其他 provider 不认该参数
    会 400，故仅按模型名软启用。"""
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if "qwen" in model.lower():
        payload["enable_thinking"] = False
    return payload


async def chat(messages: list[dict], *, temperature: float = 0.2) -> str:
    """调用 OpenAI 兼容 chat completions，返回首条回复文本。"""
    base = settings.get("LLM_API_BASE").strip().rstrip("/")
    url = f"{base}/chat/completions"
    payload = _chat_payload(settings.get("LLM_MODEL").strip(), messages,
                            temperature)
    headers = {"Authorization": f"Bearer {settings.get('LLM_API_KEY').strip()}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise LLMError(f"请求失败: {e}") from e
    if resp.status_code != 200:
        raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError(f"响应格式异常: {e}") from e
    if not (content or "").strip():
        raise LLMError("模型返回为空")
    return content


def _extract_json(text: str):
    """从模型回复提取 JSON（容忍 ```json 围栏与前后闲话），失败抛 LLMError。"""
    text = (text or "").strip()
    if text.startswith("```"):  # 剥掉 markdown 围栏
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    raise LLMError("模型未返回有效 JSON")


def _int_or_none(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


# ---- 文件名合并：最长公共前缀（LCP）压缩 ----

def _longest_common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    ref = strings[0]
    max_len = len(ref)
    for s in strings[1:]:
        while max_len > 0 and not s.startswith(ref[:max_len]):
            max_len -= 1
        if max_len == 0:
            return ""
    return ref[:max_len]


def compact_file_paths_lcp(paths: list[str]) -> tuple[list[str], list[list[int]]]:
    """简单 LCP 压缩：整组 paths 找最长公共前缀（≥5 字符），其余枚举为后缀。

    跨多季的大合集直接整体返回一条压缩模板（AI 看 {{...}} 就懂）。
    若 LCP 太短，则按 LCP 贪心分桶，每桶各自压缩。"""
    if not paths:
        return [], []
    if len(paths) == 1:
        return paths, [[0]]

    def _try_all(idxs: list[int]) -> tuple[str, str] | None:
        sub = [paths[i] for i in idxs]
        lcp = _longest_common_prefix(sub)
        if len(lcp) < 5:
            return None
        variants = [p[len(lcp):] for p in sub]
        if len(set(variants)) == 1:
            return None  # 实际上没有变化部分
        return lcp, variants

    compact: list[str] = []
    mapping: list[list[int]] = []

    def _process(idxs: list[int]) -> None:
        if len(idxs) < 2:
            for i in idxs:
                compact.append(paths[i])
                mapping.append([i])
            return
        # 1. 整体尝试
        r = _try_all(idxs)
        if r:
            lcp, variants = r
            compact.append(lcp + "{{" + ", ".join(variants) + "}}")
            mapping.append(idxs)
            return
        # 2. 贪心分桶：按 LCP 聚类
        remaining = list(idxs)
        while remaining:
            anchor = remaining.pop(0)
            bucket = [anchor]
            changed = True
            while changed and remaining:
                changed = False
                for i in remaining[:]:
                    r2 = _try_all(bucket + [i])
                    if r2:
                        bucket.append(i)
                        remaining.remove(i)
                        changed = True
                        break
            # bucket 处理
            if len(bucket) >= 2:
                r3 = _try_all(bucket)
                if r3:
                    lcp, variants = r3
                    compact.append(lcp + "{{" + ", ".join(variants) + "}}")
                    mapping.append(bucket)
                    continue
            for i in bucket:
                compact.append(paths[i])
                mapping.append([i])

    _process(list(range(len(paths))))
    return compact, mapping


# ---- AI 两阶段：SKIM（概要甄选）+ DEEP PARSE（详细元数据） ----

SKIM_SKILL_VERSION = 1
SKIM_SKILL = """你是网盘影视资源搜索结果的甄别助手。根据目标影片信息与候选资源的概要，决定每个候选是否值得进一步深入解析。

## 输入
每个候选含 title（分享标题）、video_count（视频数）、total_size_h（总体积）、
files_sample（部分条真实文件路径样例，展示资源特征）。

## 任务
对每个候选输出：
- keep: true/false — 此候选是否与目标影片匹配且值得进一步解析
- score: 0~100 — 匹配度 + 资源质量综合分（keep=false 可为 null）
- reason: 一句话中文理由
- title: 从候选标题推断的影片名（中文优先，无法判断时 null）

## 甄选原则
1. 剔除与目标影片**无关**的（如标题为不同片名、明显是花絮/预告片/电影版但目标是剧集）
2. 剔除疑似**垃圾**（凑数压缩包、广告、挂羊头卖狗肉）
3. **当搜索目标未指定具体季数时，单季条目不视为不匹配**，正常评分即可，但全季条目优先
4. 质量维度：清晰度、集数完整度、体积合理性、来源可靠性
5. 评分倾向：片名高度匹配 + 资源完整 → 90+；片名匹配但资源中等 → 70~80；存疑 → 50~60；明显不匹配 → keep=false

## 输出
只输出 JSON，不要其他文字：
{"items":[{"index":0,"keep":true,"score":90,"reason":"标题高度匹配，1080p 全季","title":"绝命毒师"}]}
- index：输入数组下标，逐一覆盖
- keep：false 表示不值得进一步解析
- score：keep=true 必填 0~100 整数；keep=false 可为 null
- reason：十分精简的决策考量原因
"""

DEEP_PARSE_SKILL_VERSION = 2
DEEP_PARSE_SKILL = """你是网盘影视资源文件名解析助手。输入多个选定候选的文件路径数组（已用最长公共前缀压缩为模板），对每一条模板输出结构化元数据。

## 输入格式
candidates 是一个 JSON 数组，每个元素含：
  - title：候选分享标题
  - paths：该候选的压缩模板数组，如：
      "{{S01E01, S01E02, S01E03}}.中英字幕.BDrip.1080p-人人影视.mp4"
    变化部分用 {{a,b,c}} 枚举。

## 输出
对每个候选按输入顺序（index 即输入 candidates 数组下标）输出 files 数组，
每一条模板返回：
- ci: paths 数组下标（0 起，必须严格对应）
- title: 推断的影片中文名
- season: 季数（整数或 null）
- episode: 集数（整数或 null；若模板枚举多集可填 null，后端逐枚举项展开）
- year: 年份（整数或 null）
- resolution: 如 "2160p" / "1080p" / "720p" / null
- source: 如 "WEB-DL" / "BluRay" / "WebRip" / "DVDRip" / null
- codec: 如 "H.265" / "H.264" / null
- audio: 如 "Atmos" / "DDP5.1" / null
- group: 发布组名（如 "人人影视" / "CHD"）或 null

⚠️ 不要输出 canonical 规范名字符串——服务端自动拼接。
只输出 JSON，不要其他文字：
{"items":[{"index":0,"files":[{"ci":0,"title":"绝命毒师","season":1,"episode":null,"year":null,"resolution":"1080p","source":"BDrip","codec":null,"audio":null,"group":"人人影视"}]}]}
"""


async def skim_candidates(cands_summary: list[dict], *, keyword: str,
                           title: str, season: int | None = None) -> list[dict]:
    """阶段 1：概要甄选。

    cands_summary 每项含 title, video_count, total_size_h, files_sample(最多3条 path)。
    返回 [{"index", "keep", "score", "reason", "title"}, ...] 对应输入数组。"""
    if not cands_summary:
        return []
    payload = {"keyword": keyword, "title": title, "season": season,
               "candidates": cands_summary}
    cache_key = ("llm:skim:%d:%s" % (
        SKIM_SKILL_VERSION, hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
            .encode("utf-8")).hexdigest()))
    if (cached := db.get_cache(config.DB_PATH, cache_key)) is not None:
        log.info("SKIM 命中缓存 %s", cache_key)
        return cached
    ctx = []
    if title:
        ctx.append(f"影片：{title}")
    if season is not None:
        ctx.append(f"目标季数：第 {season} 季")
    if keyword:
        ctx.append(f"搜索关键词：{keyword}")
    user = ("目标影片：{}\n\n候选概要（JSON 数组，index 即下标，"
            "files_sample 是每条分享 2~3 条真实文件路径样例，展示资源特征）：\n{}"
            "\n\n请按 skill 要求输出 JSON。").format(
        "；".join(ctx) or "（无，仅凭候选标题判断）",
        json.dumps(cands_summary, ensure_ascii=False))
    raw = await chat([{"role": "system", "content": SKIM_SKILL},
                      {"role": "user", "content": user}])
    data = _extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise LLMError("skim 输出格式异常")
    n = len(cands_summary)
    out: list[dict] = []
    for it in data["items"]:
        if not isinstance(it, dict):
            continue
        idx = _int_or_none(it.get("index"))
        if idx is None or not (0 <= idx < n):
            continue
        out.append({
            "index": idx,
            "keep": it.get("keep") is not False,
            "score": _int_or_none(it.get("score")),
            "reason": str(it.get("reason") or "")[:200],
            "title": str(it.get("title") or "")[:200],
        })
    # 补齐漏掉的候选（默认 keep=false）
    seen = {o["index"] for o in out}
    for i in range(n):
        if i not in seen:
            out.append({"index": i, "keep": False, "score": None, "reason": "", "title": None})
    out.sort(key=lambda o: o["index"])
    db.set_cache(config.DB_PATH, cache_key, out, _CACHE_TTL)
    return out


async def parse_candidates_deep(
    cands_detail: list[dict], *, keyword: str, title: str,
    season: int | None = None) -> list[dict]:
    """DEEP PARSE：一次性请求解析多个候选的所有 paths 模板。

    cands_detail 每项含 title + paths(LCP 压缩后的路径数组)。
    返回 [{"index": 输入下标, "title": ..., "files": [{"ci","title","season",...}]}]"""
    if not cands_detail:
        return []
    payload = {"keyword": keyword, "title": title, "season": season,
               "candidates": cands_detail}
    cache_key = ("llm:deep:%d:%s" % (
        DEEP_PARSE_SKILL_VERSION, hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
            .encode("utf-8")).hexdigest()))
    if (cached := db.get_cache(config.DB_PATH, cache_key)) is not None:
        log.info("DEEP_PARSE 命中缓存 %s", cache_key)
        return cached

    n = len(cands_detail)
    user = ("目标影片：{}\n\n选定候选文件路径模板数组（JSON）：\n{}"
            "\n\n请解析每一条模板并输出 JSON items 数组。").format(
        f"{title or keyword}" + (f" 第 {season} 季" if season else ""),
        json.dumps(cands_detail, ensure_ascii=False))
    raw = await chat([{"role": "system", "content": DEEP_PARSE_SKILL},
                      {"role": "user", "content": user}])
    data = _extract_json(raw)

    out: list[dict] = []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        # 规整：对每个输入 cands_detail[i] 找对应 items 条目
        for ci in range(n):
            cand = cands_detail[ci]
            expected_paths = cand.get("paths") or cand.get("files") or []
            entry: dict | None = None
            for it in data["items"]:
                if isinstance(it, dict) and _int_or_none(it.get("index")) == ci:
                    entry = it
                    break
            files_out: list[dict] = []
            if entry and isinstance(entry.get("files"), list):
                seen = set()
                for pf in entry["files"]:
                    if not isinstance(pf, dict):
                        continue
                    pf_ci = _int_or_none(pf.get("ci"))
                    if pf_ci is None or pf_ci in seen:
                        continue
                    if not (0 <= pf_ci < len(expected_paths)):
                        continue
                    seen.add(pf_ci)
                    files_out.append({
                        "ci": pf_ci,
                        "i": pf_ci,
                        "title": str(pf.get("title") or "").strip()[:100] or None,
                        "season": _int_or_none(pf.get("season")),
                        "episode": _int_or_none(pf.get("episode")),
                        "year": _int_or_none(pf.get("year")),
                        "resolution": str(pf.get("resolution") or "").strip()[:30] or None,
                        "source": str(pf.get("source") or "").strip()[:30] or None,
                        "codec": str(pf.get("codec") or "").strip()[:30] or None,
                        "audio": str(pf.get("audio") or "").strip()[:30] or None,
                        "group": str(pf.get("group") or "").strip()[:30] or None,
                    })
            out.append({"index": ci, "title": cand.get("title"), "files": files_out})
    else:
        # 模型输出异常 → 全空
        out = [{"index": ci, "title": c.get("title"), "files": []}
               for ci, c in enumerate(cands_detail)]

    db.set_cache(config.DB_PATH, cache_key, out, _CACHE_TTL)
    return out


# ---- 兼容前端 AI-review 端点：两阶段一体化封装 ----

async def review_candidates(cands: list[dict], *, keyword: str = "",
                            title: str = "", season: int | None = None) -> list[dict]:
    """前端 ai-review 端点入口：SKIM 概要甄选 → 选 keep=true 候选 →
    DEEP PARSE 批量解析 → 返回规整 items（格式同旧版，前端无需改）。"""
    # 1. SKIM：所有候选概要
    summary = []
    for c in cands:
        fs = c.get("files") or []
        summary.append({
            "title": c.get("title"),
            "video_count": c.get("video_count"),
            "total_size_h": c.get("total_size_h"),
            "files_sample": [f.get("path") or f.get("name") or "" for f in fs[:3]],
        })
    skims = await skim_candidates(summary, keyword=keyword, title=title, season=season)

    # 2. 挑 keep=true 且有 files 的候选，全部走 DEEP PARSE（批量一次请求）
    deep_send: list[dict] = []
    deep_meta: list[tuple[int, int]] = []  # (deep_idx, 原 cands 下标)
    for send_i, sk in enumerate(skims):
        if not sk["keep"]:
            continue
        c = cands[send_i]
        fs = c.get("files") or []
        if not fs:
            continue
        paths = [f.get("path") or f.get("name") or "" for f in fs]
        deep_send.append({"title": c.get("title"), "paths": paths})
        deep_meta.append((len(deep_send) - 1, send_i))

    deep_results: list[dict] = []
    if deep_send:
        try:
            deep_results = await parse_candidates_deep(
                deep_send, keyword=keyword, title=title, season=season)
        except LLMError:
            deep_results = []

    # 3. 规整成前端期望的 items 格式（含 files[].i 字段）
    deep_by_idx = {(dm[0]): dm[1] for dm in deep_meta}  # deep_idx → cands_idx
    out: list[dict] = []
    for sk in skims:
        ci = sk["index"]
        c = cands[ci]
        keep = sk["keep"]
        item = {
            "index": ci,
            "keep": keep,
            "score": sk["score"],
            "dup_of": None,
            "reason": sk["reason"],
            "title": sk["title"],
            "year": None,
            "season": None,
            "files": [],
        }
        if keep:
            # 找 deep_result
            for dm0, dm1 in deep_meta:
                if dm1 == ci:
                    for dr in deep_results:
                        if dr["index"] == dm0:
                            for pf in dr.get("files", []):
                                # 补上前端需要的 .i 字段（= ci，下标）
                                item["files"].append({**pf, "i": pf.get("ci")})
                            break
                    break
        out.append(item)
    return out


def _validate_review(data, n: int,
                     files_counts: list[int] | None = None) -> list[dict]:
    """校验模型输出（v3：files[].ci 对应紧凑后数组下标，
    字段为结构化元数据而非 canonical 字符串）。"""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    out: list[dict] = []
    for it in data["items"]:
        if not isinstance(it, dict):
            continue
        idx = _int_or_none(it.get("index"))
        if idx is None or not (0 <= idx < n):
            continue
        dup = _int_or_none(it.get("dup_of"))
        files: list[dict] = []
        fcount = files_counts[idx] if files_counts and idx < len(files_counts) else 0
        if fcount and isinstance(it.get("files"), list):
            for pf in it["files"]:
                if not isinstance(pf, dict):
                    continue
                ci = _int_or_none(pf.get("ci"))
                if ci is None or not (0 <= ci < fcount):
                    # 兼容 v2：ai-review 接口下游用，fallback i 字段
                    ci = _int_or_none(pf.get("i"))
                if ci is None or not (0 <= ci < fcount):
                    continue
                audio = pf.get("audio")
                group = pf.get("group")
                files.append({
                    "ci": ci,
                    "i": ci,  # 向后兼容
                    "title": (str(pf["title"]).strip()[:100]
                              if pf.get("title") else None),
                    "season": _int_or_none(pf.get("season")),
                    "episode": _int_or_none(pf.get("episode")),
                    "year": _int_or_none(pf.get("year")),
                    "resolution": (str(pf["resolution"]).strip()[:30]
                                   if pf.get("resolution") else None),
                    "source": (str(pf["source"]).strip()[:30]
                               if pf.get("source") else None),
                    "codec": (str(pf["codec"]).strip()[:30]
                              if pf.get("codec") else None),
                    "audio": str(audio).strip()[:30] if audio else None,
                    "group": str(group).strip()[:30] if group else None,
                    # canonical 字段已不再由 AI 返回，但下游仍可能读（fallback）
                    "canonical": (str(pf["canonical"]).strip()[:300]
                                  if pf.get("canonical") else None),
                })
        out.append({
            "index": idx,
            "keep": it.get("keep") is not False,
            "score": _int_or_none(it.get("score")),
            "dup_of": dup if dup is not None and 0 <= dup < n and dup != idx
                      else None,
            "reason": str(it.get("reason") or "")[:200],
            "title": str(it.get("title"))[:200] if it.get("title") else None,
            "year": _int_or_none(it.get("year")),
            "season": _int_or_none(it.get("season")),
            "files": files,
        })
    return out


# ---- AI 兜底解析：无标题文件名 → 影片名/季/集 ----

PARSE_SKILL = """你是影视资源文件名解析助手。输入是一批用规则解析不出影片标题的文件名（如纯集数编号 SE03.11、第11集、06.mp4），并附带可能的上下文（搜索关键词/分享标题/所在文件夹名，可能为空字符串）。
对每个文件名，结合上下文尽力推断：
- title：影片/剧集名（中文优先，去掉季集、规格、发布组、广告等噪声）；
- season/episode：季数与集数（从文件名或上下文推断，如文件夹名"第三季"+文件名"06"表示 S03E06）；
- confident：对 title 的把握（有明确依据为 true，仅靠猜测为 false）。

只输出 JSON，不要输出任何其他内容，格式：
{"items":[{"index":0,"title":"狂飙","season":3,"episode":6,"confident":true}]}
字段说明：index 为输入数组下标，必须逐一覆盖；无法推断 title 时填 null（season/episode 能推断仍需填写）。"""


async def parse_names(items: list[dict]) -> list[dict]:
    """AI 兜底解析：items 为 [{name, search_title, share_title, folder}]，
    返回 [{index, title, season, episode, confident}]（结果缓存 1 天）。"""
    payload = {"names": items}
    cache_key = ("llm:parse:%d:%s" % (
        PARSE_SKILL_VERSION, hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
            .encode("utf-8")).hexdigest()))
    if (cached := db.get_cache(config.DB_PATH, cache_key)) is not None:
        log.info("AI 文件名解析命中缓存 %s", cache_key)
        return cached
    user = ("文件名列表（JSON 数组，index 即数组下标）：\n{}\n\n"
            "请按 skill 要求输出 JSON。").format(
        json.dumps(items, ensure_ascii=False))
    raw = await chat([{"role": "system", "content": PARSE_SKILL},
                      {"role": "user", "content": user}])
    parsed = _validate_parse(_extract_json(raw), len(items))
    if not parsed:
        raise LLMError("模型输出的结果列表为空或全部无效")
    db.set_cache(config.DB_PATH, cache_key, parsed, _CACHE_TTL)
    return parsed


def _validate_parse(data, n: int) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    out: list[dict] = []
    for it in data["items"]:  # 不预设条数上限：越界下标由下方校验过滤
        if not isinstance(it, dict):
            continue
        idx = _int_or_none(it.get("index"))
        if idx is None or not (0 <= idx < n):
            continue
        out.append({
            "index": idx,
            "title": str(it.get("title"))[:200] if it.get("title") else None,
            "season": _int_or_none(it.get("season")),
            "episode": _int_or_none(it.get("episode")),
            "confident": it.get("confident") is True,
        })
    return out
