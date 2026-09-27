"""Global situation overview — one extra AI pass over the selected digest items.

Local extension (not part of upstream Horizon). It asks the model for a
cross-cutting "situation summary" aimed at a reader who needs a global,
whole-picture view with international-business relevance, then appends that
section to the rendered daily digest.

Design constraints:
- Never fatal. Any failure returns the digest unchanged.
- Uses the project's own AI client so token accounting, retries and the
  temperature/token capability fallbacks keep working.
- DeepSeek and most OpenAI-compatible providers are called in JSON mode by the
  shared client, so the prompt asks for a JSON object and the text is unwrapped
  here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ..models import AIConfig, ContentItem
from .client import AIClient, create_ai_client

_MAX_ITEMS = 24
_MAX_CHARS_PER_ITEM = 600
_MAX_TOTAL_CHARS = 12000
_MAX_TOKENS = 8192
_TEMPERATURE = 0.4

_SYSTEM_ZH = (
    "你是一位全球宏观与地缘经济分析师，为一位国际商务专业的读者撰写每日「全球局势总结」。\n"
    "你的职责不是复述单条新闻，而是把当天分散的资讯编织成一张图：主线是什么、彼此如何传导、"
    "对不同地区与行业意味着什么、接下来该盯什么信号。\n"
    "硬性要求：\n"
    "1. 只使用给定材料。材料里没有的数字、因果、预测一律不写，绝不补全或想象。\n"
    "2. 严格区分「已发生的事实」「官方表态或提议」「分析性推断」，是推断就标注为推断。\n"
    "3. 保持中立：不站队、不做投资建议、不断言必然结果。\n"
    "4. 用简体中文。不要写开场白、客套话或\"综上所述\"。"
)

_SYSTEM_EN = (
    "You are a global macro and geoeconomics analyst writing a daily "
    '"Global Situation Overview" for a reader in international business.\n'
    "Your job is not to restate headlines but to weave the day's scattered items "
    "into one picture: what the main threads are, how they transmit into each other, "
    "what they mean for different regions and industries, and which signals to watch.\n"
    "Hard rules:\n"
    "1. Use only the supplied material. Never invent numbers, causal claims or forecasts.\n"
    "2. Clearly separate facts, official statements or proposals, and analytical inference; "
    "label inference as inference.\n"
    "3. Stay neutral: no advocacy, no investment advice, no claims of inevitable outcomes.\n"
    "4. Write in English. No preamble, no pleasantries, no summary-of-the-summary."
)

_USER_ZH = """今天是 {date}。以下是当天入选的重要资讯（评分 / 类别 / 标题 / 摘要）：

{digest}

请写一份「全球局势总结」，按下面五节组织，用小标题分段：

### 一、整体判断
2-3 句话概括今天全球局势的主导特征（不要罗列新闻标题）。

### 二、主线
3-5 条当天最重要的线索。每条 1-2 句，写清「发生了什么、正在往哪个方向走、卡在什么变量上」。

### 三、传导与联动
2-3 个具体联系，写清机制（例如：某国政策 → 大宗商品价格 → 某地区制造业成本 → 订单流向）。
只写材料支持的链条；如果今天的线索确实彼此独立，就直接说明这一点。

### 四、对国际商务的含义
3-5 条，覆盖其中真正相关的维度：贸易流向、供应链布局、跨境投资、监管与合规、汇率与市场准入。
每条一句话，指明受影响的主体和方向。

### 五、接下来盯什么
2-3 个可验证的观察点，要具体：某个会议、某项数据发布、某个截止日期。

全文 500-800 字。不要重复罗列新闻标题，不要写空话套话。

只输出 JSON，格式为 {{"overview": "上面的 Markdown 正文"}}，不要输出代码围栏或其它字段。"""

_USER_EN = """Today is {date}. Below are the selected items of the day (score / category / title / summary):

{digest}

Write a "Global Situation Overview" with these five sections, using small headings:

### 1. Overall read
2-3 sentences on what dominates the global picture today. Do not list headlines.

### 2. Main threads
3-5 of the day's most important threads. For each, 1-2 sentences: what happened, which way it is moving, and which variable it hinges on.

### 3. Transmission and linkages
2-3 concrete connections with the mechanism spelled out (e.g. policy -> commodity price -> manufacturing cost -> order flows). Only chains the material supports; if today's threads are genuinely independent, say so.

### 4. What it means for international business
3-5 bullets covering only the relevant dimensions: trade flows, supply-chain location, cross-border investment, regulation and compliance, FX and market access. One sentence each, naming who is affected and in which direction.

### 5. What to watch next
2-3 verifiable watch points: a specific meeting, data release, or deadline.

500-800 words. Do not restate headlines. No filler.

Output JSON only, shaped as {{"overview": "the Markdown body above"}}, with no code fences and no other fields."""


def _profile_id(item: ContentItem) -> str:
    if item.processing:
        return item.processing.classification.profile
    return item.profile if isinstance(item.profile, str) else "unclassified"


def _profile_label(
    item: ContentItem, language: str, profile_names: Dict[str, Dict[str, str]]
) -> str:
    profile_id = _profile_id(item)
    names = profile_names.get(profile_id, {})
    return names.get(language) or names.get("default") or profile_id


def _item_digest(
    items: List[ContentItem],
    language: str,
    profile_names: Dict[str, Dict[str, str]],
) -> str:
    """Build a bounded plain-text digest of the selected items."""
    lines: List[str] = []
    used = 0
    for item in items[:_MAX_ITEMS]:
        artifact = item.processing.artifacts.get(language) if item.processing else None
        analysis = item.processing.analysis if item.processing else None

        score = (
            analysis.score
            if analysis and analysis.score is not None
            else "?"
        )
        title = (artifact.title if artifact else None) or item.title

        body = ""
        if artifact:
            primary = next((block for block in artifact.blocks if block.primary), None)
            body = primary.content if primary else ""
        if not body and analysis and analysis.summary:
            body = analysis.summary
        body = re.sub(r"\s+", " ", body).strip()[:_MAX_CHARS_PER_ITEM]

        entry = (
            f"- [{score}/10] ({_profile_label(item, language, profile_names)}) {title}\n"
            f"  {body}"
        )
        if used + len(entry) > _MAX_TOTAL_CHARS:
            break
        lines.append(entry)
        used += len(entry)
    return "\n".join(lines)


def _unwrap_json(text: str) -> Optional[str]:
    """Pull the overview Markdown out of the model response.

    The shared AI client runs providers like DeepSeek in JSON mode, so a normal
    response looks like {"overview": "..."}. A long body can still be cut off by
    the token cap, leaving invalid JSON; salvage the partial Markdown rather than
    dumping raw JSON scaffolding into the reader's report.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    # 1) Well-formed JSON.
    payload: Any = None
    try:
        payload = json.loads(cleaned)
    except (ValueError, TypeError):
        payload = None
    if payload is not None:
        if isinstance(payload, dict):
            for key in ("overview", "summary", "text", "content"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in payload.values():
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None
        if isinstance(payload, str):
            return payload.strip() or None
        return None

    # 2) Salvage a truncated object: {"overview": "<partial markdown...>
    match = re.search(r'"overview"\s*:\s*"', cleaned)
    if match:
        body = cleaned[match.end():]
        body = re.sub(r'"\s*\}?\s*$', "", body)
        try:
            return (json.loads('"' + body + '"') or "").strip() or None
        except (ValueError, TypeError):
            body = (
                body.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
            return body.strip() or None

    # 3) Anything left that still looks like JSON scaffolding is unusable.
    if cleaned[0] in "{[":
        return None
    return cleaned


async def generate_situation_overview(
    client: AIClient,
    items: List[ContentItem],
    *,
    language: str = "zh",
    date: str = "",
    profile_names: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[str]:
    """Return the overview Markdown, or None when it cannot be produced."""
    if not items:
        return None
    names = profile_names or {}
    digest = _item_digest(items, language, names)
    if not digest.strip():
        return None

    is_zh = str(language).lower().startswith("zh")
    system = _SYSTEM_ZH if is_zh else _SYSTEM_EN
    template = _USER_ZH if is_zh else _USER_EN
    user = template.format(date=date, digest=digest)

    try:
        raw = await client.complete(
            system, user, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS
        )
    except Exception:
        return None
    return _unwrap_json(raw or "")


async def append_situation_overview(
    summary: str,
    items: List[ContentItem],
    *,
    language: str = "zh",
    date: str = "",
    profile_names: Optional[Dict[str, Dict[str, str]]] = None,
    ai_config: Optional[AIConfig] = None,
    console: Any = None,
) -> str:
    """Append the overview section to a rendered digest. Never raises."""
    if ai_config is None:
        return summary
    try:
        client = create_ai_client(ai_config)
        overview = await generate_situation_overview(
            client,
            items,
            language=language,
            date=date,
            profile_names=profile_names,
        )
    except Exception:
        return summary

    if not overview:
        return summary

    title = "全球局势总结" if str(language).lower().startswith("zh") else "Global Situation Overview"
    if console is not None:
        console.print(f"   -> Situation overview appended ({len(overview)} chars)")
    return summary.rstrip() + f"\n\n---\n\n## {title}\n\n{overview.strip()}\n"
