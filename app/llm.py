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

import httpx

from . import config, db, settings

log = logging.getLogger(__name__)

_TIMEOUT = 60.0      # 单次 chat completions 超时（秒）
_CACHE_TTL = 86400.0  # AI 结果缓存（1 天，与搜索结果缓存一致）
_MASK_MARK = "••••"   # API Key 回显打码标记（含此标记的提交视为"未修改"）
REVIEW_SKILL_VERSION = 1
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


async def chat(messages: list[dict], *, temperature: float = 0.2) -> str:
    """调用 OpenAI 兼容 chat completions，返回首条回复文本。"""
    base = settings.get("LLM_API_BASE").strip().rstrip("/")
    url = f"{base}/chat/completions"
    payload = {
        "model": settings.get("LLM_MODEL").strip(),
        "messages": messages,
        "temperature": temperature,
    }
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


# ---- AI 精选：候选筛选 / 评分重排 / 去重分组 / 标题解析 ----

REVIEW_SKILL = """你是网盘影视资源搜索结果的甄别助手。根据目标影片信息与候选资源列表，完成四项任务：
1. 筛选：剔除与目标影片无关、或疑似垃圾内容（预告片/花絮/广告/凑数压缩包/挂羊头卖狗肉）的候选；
2. 评分：对保留候选给出 0~100 的推荐分，综合考量片名匹配度、清晰度规格、资源完整性（集数/体积）、发布时间与存活状态；
3. 去重：内容实质相同的候选归为一组，每组仅推荐一个最佳候选（其余标记 dup_of 指向组内最佳的 index）；
4. 解析：从候选标题中提取影片名（中文优先）、年份、季数。

只输出 JSON，不要输出任何其他内容，格式：
{"items":[{"index":0,"keep":true,"score":85,"dup_of":null,"reason":"一句话中文理由","title":"影片名","year":2023,"season":1}]}
字段说明：
- index：输入 candidates 数组下标，必须逐一覆盖所有候选；
- keep：false 表示剔除（此时 reason 必填说明原因）；
- score：0~100 整数，被剔除的候选可为 null；
- dup_of：与哪个候选重复（填对方的 index），自身是组内最佳或无重复时为 null；
- title/year/season：从标题解析，无法判断时为 null。"""


async def review_candidates(cands: list[dict], *, keyword: str = "",
                            title: str = "", season: int | None = None) -> list[dict]:
    """AI 精选：返回与输入对应的规整结果列表（见 _validate_review）。

    输入仅候选元数据；结果按 skill 版本 + 输入内容哈希缓存 1 天。"""
    payload = {"keyword": keyword, "title": title, "season": season,
               "candidates": cands}
    cache_key = ("llm:review:%d:%s" % (
        REVIEW_SKILL_VERSION, hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
            .encode("utf-8")).hexdigest()))
    if (cached := db.get_cache(config.DB_PATH, cache_key)) is not None:
        log.info("AI 精选命中缓存 %s", cache_key)
        return cached
    ctx = []
    if title:
        ctx.append(f"影片：{title}")
    if season is not None:
        ctx.append(f"目标季数：第 {season} 季")
    if keyword:
        ctx.append(f"搜索关键词：{keyword}")
    user = ("目标影片信息：{}\n\n候选列表（JSON 数组，index 即数组下标）：\n{}"
            "\n\n请按 skill 要求输出 JSON。").format(
        "；".join(ctx) or "（无，仅凭候选标题判断）",
        json.dumps(cands, ensure_ascii=False))
    raw = await chat([{"role": "system", "content": REVIEW_SKILL},
                      {"role": "user", "content": user}])
    items = _validate_review(_extract_json(raw), len(cands))
    if not items:
        raise LLMError("模型输出的结果列表为空或全部无效")
    db.set_cache(config.DB_PATH, cache_key, items, _CACHE_TTL)
    return items


def _validate_review(data, n: int) -> list[dict]:
    """校验模型输出：下标越界/类型不符的条目丢弃，字段截断规整。"""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    out: list[dict] = []
    for it in data["items"]:  # 不预设条数上限：越界下标由下方校验过滤
        if not isinstance(it, dict):
            continue
        idx = _int_or_none(it.get("index"))
        if idx is None or not (0 <= idx < n):
            continue
        dup = _int_or_none(it.get("dup_of"))
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
