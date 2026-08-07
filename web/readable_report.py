"""行业研判页：将公开搜索资料整理为结构化内容，再渲染为独立 HTML 页面。"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from typing import Any


SECTION_BLUEPRINT = (
    ("facts", "01 / 事实线索", "这个行业现在发生了什么"),
    ("business_map", "02 / 生意构成", "这个行业由哪些产品、场景或业务组成"),
    ("market_samples", "03 / 市场样本", "公开资料里的人在怎么做"),
    ("factory_relevance", "04 / 工厂相关", "制造、供应链与进入门槛线索"),
    ("hypotheses", "05 / 值得验证", "公开资料支持哪些假设，而非结论"),
    ("uncertainty", "06 / 不能下结论", "哪些信息不足、矛盾或需要警惕"),
    ("next_questions", "07 / 下一步要问", "继续调研时最该补的资料"),
)

BLOCKED_SOURCE_MARKERS = (
    "xpicvid", "ctrip", "youtube", "google.", "deepseek.com", "aydvjch",
    "porn", "成人视频", "情色", "hotel", "酒店",
)

# 搜索的是“资料类型”，不是预设某个行业一定有增长、爆款或出口机会。
READABLE_QUERIES = {
    "事实线索": ("2026 最新 新闻 发布 动态", "市场 数据 报告 统计", "政策 标准 监管"),
    "产品与业务": ("产品 类型 应用 场景 案例", "新品 发布 评测 众筹", "解决方案 服务 商业模式"),
    "市场样本": ("品牌 平台 卖家 案例", "渠道 上架 销售 采购", "用户 客户 评价 需求"),
    "制造线索": ("供应链 工厂 OEM ODM", "材料 工艺 零部件 生产", "认证 质量 专利 合规"),
    "争议与限制": ("风险 问题 投诉 召回", "竞争 价格战 同质化", "政策 限制 侵权 失败案例"),
}
READABLE_QUALITY_SITES = ("36kr.com", "huxiu.com", "jiemian.com", "cifnews.com", "amz123.com", "1688.com")


def _industry_keywords(industry: str) -> list[str]:
    """生成可解释的行业关键词，用于剔除搜索引擎混入的无关页面。"""
    compact = re.sub(r"\s+", "", industry.lower())
    keywords = [compact]
    for part in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", compact):
        if len(part) >= 2:
            keywords.append(part)
            if len(part) >= 4:
                keywords.extend(part[index:index + 2] for index in range(len(part) - 1))
    return list(dict.fromkeys(keyword for keyword in keywords if len(keyword) >= 2))


def _relevant_source(result: dict[str, str], keywords: list[str]) -> bool:
    """只把真正提到行业的页面交给模型，避免“搜索结果污染报告”。"""
    title = str(result.get("title", "")).lower()
    snippet = str(result.get("snippet", "")).lower()
    url = str(result.get("url", "")).lower()
    combined = f"{title}\n{snippet}"
    if any(marker in f"{combined}\n{url}" for marker in BLOCKED_SOURCE_MARKERS):
        return False
    title_matches = sum(keyword in title for keyword in keywords)
    text_matches = sum(keyword in combined for keyword in keywords)
    # 标题必须能对上行业关键词；只在 URL 中命中的网页容易是无关跳转或广告页。
    return title_matches > 0 or text_matches >= 2


def _text(value: Any, fallback: str = "资料不足，建议继续核实。") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _bounded_int(value: Any, default: int = 3) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return default


def _json_from_response(raw: str) -> dict[str, Any]:
    """兼容模型偶尔包裹在 Markdown 代码块中的 JSON。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            candidate, _ = decoder.raw_decode(cleaned[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    raise ValueError("模型没有返回可用的结构化内容")


def _normalise_cards(value: Any, valid_source_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    cards: list[dict[str, Any]] = []
    for item in value[:4]:
        if isinstance(item, dict):
            references = item.get("sources", [])
            references = references if isinstance(references, list) else []
            cards.append({
                "title": _text(item.get("title"), "待确认"),
                "text": _text(item.get("text")),
                "sources": [str(item) for item in references[:2] if str(item) in valid_source_ids],
            })
        elif isinstance(item, str) and item.strip():
            cards.append({"title": "要点", "text": item.strip(), "sources": []})
    return cards


def normalise_content(
    payload: dict[str, Any], industry: str, sources: list[dict[str, str]], valid_source_ids: set[str] | None = None
) -> dict[str, Any]:
    """把模型的轻微格式偏差收敛为渲染器需要的安全、完整结构。"""
    source_count = len(sources)
    valid_source_ids = valid_source_ids or {str(source.get("id")) for source in sources}
    blueprint_by_id = {section_id: (eyebrow, fallback_title) for section_id, eyebrow, fallback_title in SECTION_BLUEPRINT}
    sections = []
    seen_section_ids: set[str] = set()
    # 模型按“资料实际支持的顺序”返回 4–7 个模块；不强制把所有模块填满。
    for item in payload.get("sections", []) if isinstance(payload.get("sections"), list) else []:
        if not isinstance(item, dict):
            continue
        section_id = str(item.get("id", ""))
        if section_id not in blueprint_by_id or section_id in seen_section_ids:
            continue
        eyebrow, fallback_title = blueprint_by_id[section_id]
        has_substance = item.get("summary") or item.get("analysis") or item.get("cards")
        if not has_substance:
            continue
        seen_section_ids.add(section_id)
        sections.append(
            {
                "id": section_id,
                "eyebrow": eyebrow,
                "title": _text(item.get("title"), fallback_title),
                "summary": _text(item.get("summary")),
                "analysis": _text(item.get("analysis"), "本板块公开资料有限，建议结合平台后台、报价和一线访谈继续验证。"),
                "sources": [str(source_id) for source_id in item.get("sources", [])[:4] if str(source_id) in valid_source_ids] if isinstance(item.get("sources"), list) else [],
                "cards": _normalise_cards(item.get("cards"), valid_source_ids) or [{"title": "资料提示", "text": "公开资料暂不足以形成可靠结论。", "sources": []}],
            }
        )

    if not sections:
        sections = [{
            "id": "uncertainty", "eyebrow": "06 / 不能下结论", "title": "公开资料暂不足以形成行业判断",
            "summary": "本次搜索没有取得足够可靠且相关的公开资料。",
            "analysis": "不建议把搜索摘要当作经营结论。下一步应补充可核实的产品链接、供应商报价、平台后台或客户访谈资料。",
            "sources": [], "cards": [{"title": "资料提示", "text": "资料不足，建议继续核实。", "sources": []}],
        }]

    raw_signals = payload.get("signals", [])
    signal_defaults = (("证据密度", "yellow"), ("制造相关度", "yellow"), ("信息缺口", "red"))
    signals = []
    for index, (name, status) in enumerate(signal_defaults):
        item = raw_signals[index] if isinstance(raw_signals, list) and index < len(raw_signals) and isinstance(raw_signals[index], dict) else {}
        actual_status = item.get("status") if item.get("status") in {"green", "yellow", "red"} else status
        signals.append(
            {
                "name": _text(item.get("name"), name),
                "status": actual_status,
                "value": _text(item.get("value"), "待判断"),
                "note": _text(item.get("note"), "基于公开资料的初步信号"),
            }
        )

    heat = []
    for item in payload.get("export_heat", []) if isinstance(payload.get("export_heat"), list) else []:
        if isinstance(item, dict):
            heat.append({"country": _text(item.get("country"), "待确认"), "strength": _bounded_int(item.get("strength")), "note": _text(item.get("note"))})
    if not heat:
        heat = [{"country": "暂无可靠国家分布", "strength": 1, "note": "需要补充贸易或平台数据"}]

    return {
        "industry": industry,
        "headline": _text(payload.get("headline"), f"{industry}：公开资料能说明什么，仍缺什么"),
        "subheadline": _text(payload.get("subheadline"), "基于本次检索到的公开网页线索整理，不替代报价、客户访谈或经营数据"),
        "decision": _text(payload.get("decision"), "当前资料可用于形成初步认识，但不足以替代实际报价、平台后台数据或一线客户验证。"),
        "signals": signals,
        "export_heat": heat[:5],
        "sections": sections,
        "source_count": source_count,
        "sources": sources[:42],
    }


def _select_evidence_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个决策板块都有资料进入模型，避免靠列表顺序挤掉后面的维度。"""
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for topic in READABLE_QUERIES:
        for source in (item for item in sources if item.get("topic") == topic):
            if len([item for item in selected if item.get("topic") == topic]) >= 4:
                break
            selected.append(source)
            selected_ids.add(source["id"])
    for source in sources:
        if len(selected) >= 30:
            break
        if source["id"] not in selected_ids:
            selected.append(source)
            selected_ids.add(source["id"])
    return selected


def _select_deep_read_sources(evidence_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """深读同样按板块均衡分配，六类问题各至少取一篇。"""
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for topic in READABLE_QUERIES:
        topic_sources = [item for item in evidence_sources if item.get("topic") == topic]
        for source in topic_sources[:2]:
            selected.append(source)
            selected_ids.add(source["id"])
    for source in evidence_sources:
        if len(selected) >= 12:
            break
        if source["id"] not in selected_ids:
            selected.append(source)
            selected_ids.add(source["id"])
    return selected[:12]


def generate_content(
    client: Any,
    model: str,
    industry: str,
    search_web: Callable[[str, int], list[dict[str, str]]],
    fetch_content: Callable[[str], str],
    fetch_trade_data: Callable[[str], str],
) -> dict[str, Any]:
    """阶段一：搜索资料，再让模型只输出内容 JSON。"""
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    industry_keywords = _industry_keywords(industry)
    search_deadline = time.monotonic() + 90

    def add_result(topic: str, result: dict[str, str]) -> None:
        if len(sources) >= 42:
            return
        url = str(result.get("url", "")).strip()
        if (
            not url
            or url in seen_urls
            or not url.startswith(("http://", "https://"))
            or not _relevant_source(result, industry_keywords)
        ):
            return
        seen_urls.add(url)
        sources.append({
            "id": f"S{len(sources) + 1}", "topic": topic,
            "title": _text(result.get("title"), "未命名资料"),
            "snippet": _text(result.get("snippet"), "搜索结果未提供摘要"),
            "url": url,
        })

    # 沿用快速白皮书：每个维度多组关键词，而不是“一题一搜”。
    for topic, queries in READABLE_QUERIES.items():
        for query in queries:
            if time.monotonic() > search_deadline or len(sources) >= 42:
                break
            for result in search_web(f"{industry} {query}", max_results=4):
                add_result(topic, result)

    for site in READABLE_QUALITY_SITES[:4]:
        if time.monotonic() > search_deadline or len(sources) >= 42:
            break
        for result in search_web(f"site:{site} {industry}", max_results=3):
            add_result("补充资料", result)

    evidence_sources = _select_evidence_sources(sources)
    deep_sources = _select_deep_read_sources(evidence_sources)
    deep_source_ids = {source["id"] for source in deep_sources}
    for source in sources:
        source["deep_read"] = source["id"] in deep_source_ids

    page_text = []
    for source in deep_sources:
        text = fetch_content(source["url"])
        if text:
            page_text.append(f"[{source['id']}] {source['url']}\n{text[:1000]}")
    trade_text = fetch_trade_data(industry)
    source_text = "\n\n".join(
        f"[{source['id']}] [{source['topic']}] {source['title'][:140]}\n{source['snippet'][:260]}\n{source['url']}" for source in evidence_sources
    )
    evidence = (trade_text + "\n\n" + source_text + "\n\n" + "\n---\n".join(page_text)).strip()

    prompt = f"""你是行业公开资料整合分析师。请根据以下公开资料，为「{industry}」制作一份给工厂老板阅读的“行业研判页”。

这不是经营指令书：你不了解该工厂的真实成本、产能、客户和报价，不能替老板下“应该投产/应该赚钱”的结论。你的任务是把已搜到的公开资料整理为：能确认的事实、这些事实可能意味着什么、以及还不能下结论的地方。

只能使用资料中明确出现的事实和链接；不确定就写“资料不足，建议验证”，绝不能编造数字、国家、产品、价格、销量、利润或来源。不要输出 Markdown，不要输出解释，只输出一个合法 JSON 对象。

JSON 必须符合：
{{
  "headline": "一句基于资料的研判标题",
  "subheadline": "一句说明本页覆盖范围的副标题",
  "decision": "一句当前研究结论：说明能确认什么和主要限制",
  "signals": [
    {{"name":"证据密度","status":"green|yellow|red","value":"较充分/有限/不足","note":"不超过32字"}},
    {{"name":"制造相关度","status":"green|yellow|red","value":"直接相关/部分相关/线索少","note":"..."}},
    {{"name":"信息缺口","status":"green|yellow|red","value":"较少/较多/很大","note":"..."}}
  ],
  "sections": [
    {{"id":"facts|business_map|market_samples|factory_relevance|hypotheses|uncertainty|next_questions","title":"...","summary":"不超过100字","analysis":"180到260字的完整分析段落","sources":["S1","S2"],"cards":[{{"title":"...","text":"不超过120字","sources":["S1","S2"]}}]}}
  ]
}}

sections 只能从上述 7 个 id 中选择 4–7 个，顺序由本次资料决定：有足够事实支撑才写，不要为了凑全套框架硬写空模块。每个板块必须有一段 180-260 字的 analysis，先讲资料支持的事实，再说明它可能意味着什么或还不能说明什么，不能重复卡片标题。每个板块最多 3 张卡片。section 和每张卡片只要出现事实、数字、平台、产品、国家或案例，就必须在 sources 字段填入真正支持该说法的来源编号；资料不足时保留空数组，不能猜编号。

研究资料（已按资料类型均衡挑选；标有“深读”的资料正文更完整）：
{evidence}"""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.35,
        max_tokens=3500,
    )
    content = normalise_content(
        _json_from_response(response.choices[0].message.content), industry, sources,
        {source["id"] for source in evidence_sources},
    )
    content["evidence_count"] = len(evidence_sources)
    content["deep_read_count"] = len(deep_sources)
    return content


def _render_citations(card: dict[str, Any]) -> str:
    source_ids = card.get("sources", [])
    if not source_ids:
        return '<span class="no-citation">资料待核实</span>'
    return '<span class="citations">' + " ".join(
        f'<a href="#source-{html.escape(source_id, quote=True)}">[{html.escape(source_id)}]</a>'
        for source_id in source_ids
    ) + '</span>'


def _render_cards(cards: list[dict[str, Any]]) -> str:
    return "".join(
        f'<article class="insight-card"><h3>{html.escape(card["title"])}</h3><p>{html.escape(card["text"])}</p>{_render_citations(card)}</article>'
        for card in cards
    )


def _section_frame(section: dict[str, Any], body: str) -> str:
    return f'''<section id="{section["id"]}" class="section section-{section["id"]}">
  <div class="section-head"><span>{html.escape(section["eyebrow"])}</span><h2>{html.escape(section["title"])}</h2></div>
  <p class="section-summary">{html.escape(section["summary"])}</p>
  <p class="section-analysis">{html.escape(section["analysis"])}</p>{_render_citations(section)}{body}
</section>'''


def _render_section(section: dict[str, Any]) -> str:
    """六个问题使用六种信息布局，避免整页都是同一种卡片。"""
    cards = section["cards"]
    section_id = section["id"]
    if section_id == "facts":
        body = '<div class="market-layout"><div class="evidence-stack">' + _render_cards(cards[:2]) + '</div>'
        if len(cards) > 2:
            body += f'<aside class="market-poster"><small>MARKET / EVIDENCE</small><b>{html.escape(cards[2]["title"])}</b><p>{html.escape(cards[2]["text"])}</p>{_render_citations(cards[2])}</aside>'
        body += '</div>'
    elif section_id == "business_map":
        body = '<div class="product-notes">' + "".join(
            f'<article class="product-note note-{index}"><span>0{index + 1}</span><h3>{html.escape(card["title"])}</h3><p>{html.escape(card["text"])}</p>{_render_citations(card)}</article>'
            for index, card in enumerate(cards)
        ) + '</div>'
    elif section_id == "factory_relevance":
        body = '<div class="price-flow">' + "".join(
            f'<article class="price-stop"><span>STEP {index + 1}</span><b>{html.escape(card["title"])}</b><p>{html.escape(card["text"])}</p>{_render_citations(card)}</article>{"<i class=\"flow-arrow\">→</i>" if index < len(cards) - 1 else ""}'
            for index, card in enumerate(cards)
        ) + '</div>'
    elif section_id == "market_samples":
        body = '<div class="channel-route">' + "".join(
            f'<article class="route-stop"><b>{index + 1:02d}</b><div><h3>{html.escape(card["title"])}</h3><p>{html.escape(card["text"])}</p>{_render_citations(card)}</div></article>'
            for index, card in enumerate(cards)
        ) + '</div>'
    elif section_id == "hypotheses":
        body = '<div class="buyer-map">' + "".join(
            f'<article class="buyer-node"><span>买家线索</span><h3>{html.escape(card["title"])}</h3><p>{html.escape(card["text"])}</p>{_render_citations(card)}</article>'
            for card in cards
        ) + '</div>'
    else:  # uncertainty / next_questions
        body = '<div class="risk-wall">' + "".join(
            f'<article class="risk-item"><b>!</b><div><h3>{html.escape(card["title"])}</h3><p>{html.escape(card["text"])}</p>{_render_citations(card)}</div></article>'
            for card in cards
        ) + '</div>'
    return _section_frame(section, body)


def _signal_score(status: str) -> int:
    return {"green": 78, "yellow": 52, "red": 29}.get(status, 45)


def _signal_label(status: str) -> str:
    return {"green": "线索较充分", "yellow": "需要核实", "red": "缺口明显"}.get(status, "资料不足")


def _heat_label(strength: int) -> str:
    return {1: "少量提及", 2: "少量提及", 3: "多次提及", 4: "多次提及", 5: "多次提及"}.get(strength, "待验证")


def render_html(content: dict[str, Any]) -> str:
    """阶段二：只由 Python 负责把 JSON 填进固定版式。"""
    signal_html = "".join(
        f'<article class="signal signal-{signal["status"]}"><span>{html.escape(signal["name"])}</span><strong>{html.escape(signal["value"])}</strong><p>{html.escape(signal["note"])}</p></article>'
        for signal in content["signals"]
    )
    signal_chart_html = "".join(
        f'<div class="signal-row"><span>{html.escape(signal["name"])}</span><div class="signal-track"><i class="signal-fill signal-{signal["status"]}" style="width:{_signal_score(signal["status"])}%"></i></div><b>{_signal_label(signal["status"])}</b></div>'
        for signal in content["signals"]
    )
    validation_html = "".join(
        f'<article class="validation-item"><span>0{index + 1}</span><div><h3>{html.escape(signal["name"])}</h3><p>当前状态：{html.escape(signal["value"])}。{html.escape(signal["note"])}</p></div></article>'
        for index, signal in enumerate(content["signals"])
    )
    image_html = "".join(
        f'<figure class="scene scene-{index}"><img src="{html.escape(image["url"], quote=True)}" alt="{html.escape(image.get("title", content["industry"]))}" loading="lazy"><figcaption>{html.escape(image.get("title", "行业参考图"))}</figcaption></figure>'
        for index, image in enumerate(content.get("images", [])[:2], start=1)
        if isinstance(image, dict) and image.get("url")
    )
    product_section = next((section for section in content["sections"] if section["id"] == "business_map"), None)
    visual_context = ""
    if product_section:
        visual_context = (
            f'<div class="visual-copy"><span>VISUAL / CONTEXT</span><h2>{html.escape(product_section["title"])}</h2>'
            f'<p>{html.escape(product_section["summary"])}</p><small>图片用于理解行业产品与场景，不作为销量、价格或市场规模证据。</small></div>'
        )
    visual_board_html = (
        f'<section class="visual-board">{visual_context}<div class="visual-gallery">{image_html}</div></section>'
        if image_html else ""
    )
    sections_html = "".join(_render_section(section) for section in content["sections"])
    sources_html = "".join(
        f'<li id="source-{html.escape(source["id"], quote=True)}"><span class="source-meta">{html.escape(source.get("topic", "资料"))}{" · 深读" if source.get("deep_read") else ""}</span><a href="{html.escape(source["url"], quote=True)}" target="_blank" rel="noopener noreferrer">[{html.escape(source["id"])}] {html.escape(source["title"])}</a></li>'
        for source in content["sources"]
    ) or "<li>本次未取得可引用的公开来源。</li>"
    title = html.escape(content["industry"])
    headline = html.escape(content["headline"])
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}｜行业研判页</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700;900&family=Noto+Serif+SC:wght@600;700;900&family=Roboto+Mono:wght@500;700&display=swap');
:root{{--paper:#f2efe5;--paper-2:#e8e3d6;--white:#fffdf7;--ink:#171713;--coral:#ff6846;--lime:#d9f650;--sun:#ffc933;--sky:#9fd8ff;--line:#1c1c1c;--muted:#67665f}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;color:var(--ink);font-family:'Noto Sans SC',sans-serif;line-height:1.65;background:radial-gradient(circle at 12% 6%,rgba(217,246,80,.28),transparent 18rem),linear-gradient(rgba(23,23,19,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(23,23,19,.035) 1px,transparent 1px),var(--paper);background-size:auto,48px 48px,48px 48px,auto}}.wrap{{max-width:1020px;margin:auto;padding:0 38px}}nav{{position:sticky;top:0;z-index:5;background:rgba(242,239,229,.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--ink)}}nav .wrap{{min-height:58px;display:flex;align-items:center;justify-content:space-between;font:700 12px 'Roboto Mono',monospace;letter-spacing:.08em}}nav a{{color:inherit;text-decoration:none}}.hero{{position:relative;padding:92px 0 68px;overflow:hidden;border-bottom:1px solid var(--ink)}}.hero::before{{content:'TREND';position:absolute;right:-1vw;bottom:-1.5rem;color:rgba(23,23,19,.045);font:900 clamp(6rem,17vw,14rem)/.75 'Noto Sans SC',sans-serif;pointer-events:none}}.eyebrow{{color:var(--ink);font:700 12px 'Roboto Mono',monospace;letter-spacing:.12em}}h1{{margin:14px 0 22px;font:900 clamp(42px,7vw,76px)/.96 'Noto Serif SC',serif;letter-spacing:-.065em;max-width:760px}}h1::after{{display:none}}h1 span{{display:inline;border-bottom:5px solid var(--coral)}}.hero-headline{{max-width:660px;margin:0 0 16px!important;font:700 clamp(19px,2.4vw,28px)/1.35 'Noto Serif SC',serif!important;color:var(--coral)!important}}.hero-grid{{position:relative;display:grid;grid-template-columns:1.35fr .65fr;gap:34px;align-items:end;z-index:1}}.hero p{{max-width:660px;margin:0;font-size:17px;color:var(--muted)}}.decision{{border:1px solid var(--ink);border-left:6px solid var(--coral);padding:16px 18px!important;background:var(--white);font-weight:700!important;color:var(--ink)!important;box-shadow:7px 7px 0 var(--lime)}}.signal-band{{padding:28px 0;background:transparent}}.signals{{display:grid;grid-template-columns:repeat(3,1fr);background:var(--ink);border:1px solid var(--ink);box-shadow:8px 8px 0 var(--ink)}}.signal{{min-height:166px;padding:24px;border-right:1px solid rgba(255,255,255,.25);background:transparent;color:var(--white)}}.signal span,.section-head span{{font:700 11px 'Roboto Mono',monospace;letter-spacing:.09em;text-transform:uppercase}}.signal strong{{display:block;margin:11px 0 7px;font:500 clamp(26px,3vw,40px)/1 'Roboto Mono',monospace;letter-spacing:-.07em}}.signal p{{margin:0;font-size:13px;color:rgba(255,255,255,.6)}}.signal-green strong{{color:var(--lime)}}.signal-yellow strong{{color:var(--sun)}}.signal-red strong{{color:var(--coral)}}.data-lenses{{padding:50px 0 34px;border-bottom:1px solid var(--ink)}}.data-lenses h2{{margin:0;font:900 clamp(30px,5vw,54px)/1 'Noto Serif SC',serif;letter-spacing:-.06em}}.heat-grid{{display:flex;gap:22px;flex-wrap:wrap;margin-top:28px}}.heat-item{{width:150px;text-align:center}}.heat-circle{{width:100px;height:100px;border-radius:50%;margin:auto;display:grid;place-content:center;border:1px solid var(--ink);background:var(--sky);box-shadow:8px 8px 0 var(--coral)}}.heat-circle b{{font:900 34px/1 'Roboto Mono',monospace}}.heat-circle small{{font:700 11px 'Roboto Mono',monospace}}.heat-1{{opacity:.42}}.heat-2{{opacity:.56}}.heat-3{{opacity:.7}}.heat-4{{opacity:.84}}.heat-5{{opacity:1;background:var(--lime)}}.heat-item h3{{margin:14px 0 2px;font-size:15px}}.heat-item p{{margin:0;font-size:12px;color:var(--muted)}}.data-board{{padding:28px max(38px,calc((100vw - 944px)/2)) 64px;display:grid;grid-template-columns:1fr 1fr;gap:20px;border-bottom:1px solid var(--ink)}}.data-card{{padding:22px;border:1px solid var(--ink);background:var(--white);box-shadow:6px 6px 0 var(--ink)}}.data-card h3{{margin:0 0 18px;font:800 19px 'Noto Serif SC',serif}}.data-card p{{margin:0 0 18px;color:var(--muted);font-size:12px}}.signal-row,.export-row{{display:grid;grid-template-columns:56px 1fr 64px;gap:10px;align-items:center;margin:11px 0;font:700 12px 'Roboto Mono',monospace}}.signal-track,.export-row>div{{height:12px;background:var(--paper-2);border:1px solid var(--ink)}}.signal-fill,.export-row i{{display:block;height:100%;background:var(--lime)}}.signal-fill.signal-yellow{{background:var(--sun)}}.signal-fill.signal-red{{background:var(--coral)}}.export-row i{{background:var(--sky)}}.signal-row b,.export-row b{{text-align:right;font-size:10px;white-space:nowrap}}.visual-board{{padding:68px max(38px,calc((100vw - 944px)/2));display:grid;grid-template-columns:1.2fr .8fr;gap:22px;border-bottom:1px solid var(--ink)}}.scene{{position:relative;min-height:270px;margin:0;overflow:hidden;border:1px solid var(--ink);background:var(--paper-2);box-shadow:8px 8px 0 var(--coral)}}.scene-2{{margin-top:42px;box-shadow:8px 8px 0 var(--ink)}}.scene img{{display:block;width:100%;height:100%;min-height:270px;object-fit:cover;filter:saturate(.92) contrast(1.04)}}.scene figcaption{{position:absolute;left:0;bottom:0;max-width:85%;padding:8px 11px;background:var(--lime);border-top:1px solid var(--ink);border-right:1px solid var(--ink);font:700 11px 'Roboto Mono',monospace}}.section{{padding:70px max(38px,calc((100vw - 944px)/2));border-bottom:1px solid var(--ink)}}.section:nth-of-type(even){{background:rgba(232,227,214,.72)}}.section-head{{display:flex;gap:18px;align-items:baseline}}.section-head span{{color:var(--coral)}}.section-head h2{{margin:0;font:900 clamp(32px,6vw,64px)/.94 'Noto Serif SC',serif;letter-spacing:-.07em}}.section-summary{{max-width:760px;padding-left:18px;border-left:5px solid var(--coral);font-size:18px;font-weight:700}}.card-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:28px}}.insight-card{{border:1px solid var(--ink);padding:20px;background:var(--white);box-shadow:7px 7px 0 var(--ink)}}.insight-card:nth-child(2){{background:var(--lime)}}.section-pricing{{color:var(--white);background:radial-gradient(circle at 82% 22%,rgba(159,216,255,.24),transparent 25rem),var(--ink)!important}}.section-pricing .section-head span{{color:var(--lime)}}.section-pricing .section-summary{{border-left-color:var(--lime)}}.section-pricing .insight-card{{background:rgba(255,255,255,.07);color:var(--white);box-shadow:7px 7px 0 var(--coral)}}.section-pricing .insight-card:nth-child(2){{background:var(--coral);color:var(--ink)}}.insight-card h3{{margin:0 0 10px;font-size:17px;line-height:1.3}}.insight-card p{{margin:0;font-size:14px}}.sources{{padding:48px 0 76px}}.sources h2{{font:900 32px 'Noto Serif SC',serif}}.sources p,.sources li{{font-size:13px;color:var(--muted)}}.sources a{{color:var(--ink);text-decoration-thickness:1px;text-underline-offset:3px}}footer{{background:var(--ink);color:var(--paper);padding:22px 0;font:700 11px 'Roboto Mono',monospace;letter-spacing:.06em}}@media(max-width:680px){{.wrap{{padding:0 18px}}.hero{{padding-top:56px}}.hero-grid,.card-grid,.data-board,.visual-board{{grid-template-columns:1fr}}.signals{{grid-template-columns:1fr;box-shadow:5px 5px 0 var(--ink)}}.signal{{min-height:122px;border-right:0;border-bottom:1px solid rgba(255,255,255,.25)}}.section,.data-board,.visual-board{{padding:46px 18px}}.scene-2{{margin-top:0}}.section-head{{display:block}}.section-head h2{{margin-top:9px}}.heat-grid{{justify-content:space-between;gap:14px}}.heat-item{{width:calc(50% - 8px)}}}}
</style><style>
.market-layout{{display:grid;grid-template-columns:1.1fr .9fr;gap:22px;margin-top:30px}}.evidence-stack{{display:grid;gap:14px}}.evidence-stack .insight-card{{box-shadow:5px 5px 0 var(--ink)}}.market-poster{{min-height:250px;padding:26px;background:var(--lime);border:1px solid var(--ink);box-shadow:8px 8px 0 var(--coral);display:flex;flex-direction:column;justify-content:flex-end}}.market-poster small,.product-note span,.price-stop span,.buyer-node span{{font:700 10px 'Roboto Mono',monospace;letter-spacing:.08em}}.market-poster b{{display:block;margin:12px 0;font:900 28px/1.05 'Noto Serif SC',serif}}.market-poster p{{margin:0;font-size:14px}}.product-notes{{display:grid;grid-template-columns:1.25fr .75fr .75fr;gap:14px;margin-top:30px}}.product-note{{min-height:215px;padding:20px;border:1px solid var(--ink);background:var(--white)}}.product-note.note-0{{background:var(--sky);box-shadow:8px 8px 0 var(--ink)}}.product-note.note-1{{margin-top:28px;background:var(--lime)}}.product-note.note-2{{margin-bottom:28px;background:var(--white)}}.product-note h3,.route-stop h3,.buyer-node h3,.risk-item h3{{margin:17px 0 10px;font-size:19px;line-height:1.2}}.product-note p,.route-stop p,.buyer-node p,.risk-item p{{margin:0;font-size:13px}}.price-flow{{display:flex;align-items:stretch;gap:10px;margin-top:30px;padding:25px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.3)}}.price-stop{{flex:1;padding:17px;border:1px solid rgba(255,255,255,.35);background:rgba(255,255,255,.06)}}.price-stop b{{display:block;margin:11px 0;font-size:18px}}.price-stop p{{margin:0;color:rgba(255,255,255,.72);font-size:13px}}.flow-arrow{{align-self:center;color:var(--lime);font:900 27px/1 'Roboto Mono',monospace}}.channel-route{{display:grid;gap:0;margin-top:30px;border-top:2px solid var(--ink)}}.route-stop{{display:grid;grid-template-columns:80px 1fr;gap:20px;padding:20px 0;border-bottom:1px solid var(--ink)}}.route-stop>b{{display:grid;place-items:center;width:48px;height:48px;background:var(--coral);border:1px solid var(--ink);font:900 17px 'Roboto Mono',monospace}}.route-stop:nth-child(2)>b{{background:var(--lime)}}.route-stop:nth-child(3)>b{{background:var(--sky)}}.buyer-map{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:30px}}.buyer-node{{min-height:210px;padding:22px;border:1px solid var(--ink);border-radius:50% 50% 8px 8px;background:var(--white);box-shadow:5px 5px 0 var(--ink)}}.buyer-node:nth-child(2){{margin-top:38px;background:var(--sky)}}.buyer-node:nth-child(3){{background:var(--lime)}}.risk-wall{{display:grid;gap:0;margin-top:30px;border:1px solid var(--coral);background:#2a211e}}.risk-item{{display:grid;grid-template-columns:64px 1fr;gap:18px;padding:21px;color:var(--white);border-bottom:1px solid rgba(255,255,255,.22)}}.risk-item:last-child{{border-bottom:0}}.risk-item>b{{display:grid;place-items:center;width:42px;height:42px;background:var(--coral);color:var(--ink);border:1px solid var(--ink);font:900 25px/1 'Noto Serif SC',serif}}.risk-item p{{color:rgba(255,255,255,.72)}}
@media(max-width:680px){{.market-layout,.product-notes,.buyer-map{{grid-template-columns:1fr}}.product-note.note-1,.buyer-node:nth-child(2){{margin-top:0}}.product-note.note-2{{margin-bottom:0}}.price-flow{{display:grid}}.flow-arrow{{justify-self:center;transform:rotate(90deg)}}.market-poster{{min-height:190px}}}}
.citations,.no-citation{{display:block;margin-top:13px;font:700 10px 'Roboto Mono',monospace;letter-spacing:.04em}}.citations a{{display:inline-block;margin-right:5px;padding:2px 5px;color:var(--ink);background:var(--lime);border:1px solid var(--ink);text-decoration:none}}.citations a:hover{{background:var(--coral)}}.no-citation{{color:var(--muted)}}.section-pricing .citations a{{background:var(--lime);color:var(--ink)}}.section-pricing .no-citation{{color:rgba(255,255,255,.55)}}.visual-board{{grid-template-columns:.72fr 1.28fr;align-items:center;background:var(--paper-2)}}.visual-copy{{padding:22px 12px 22px 0}}.visual-copy>span{{font:700 10px 'Roboto Mono',monospace;letter-spacing:.1em;color:var(--coral)}}.visual-copy h2{{margin:14px 0;font:900 clamp(28px,4vw,46px)/1 'Noto Serif SC',serif;letter-spacing:-.06em}}.visual-copy p{{margin:0 0 16px;font-size:16px;font-weight:700}}.visual-copy small{{display:block;padding-top:12px;border-top:1px solid var(--ink);font-size:11px;color:var(--muted)}}.visual-gallery{{display:grid;grid-template-columns:1.25fr .75fr;gap:18px}}.visual-gallery .scene{{min-width:0}}.visual-gallery .scene-2{{margin-top:36px}}@media(max-width:680px){{.visual-copy{{padding:0}}.visual-gallery{{grid-template-columns:1fr}}.visual-gallery .scene-2{{margin-top:0}}}}
</style><style>
.section-analysis{{max-width:820px;margin:22px 0 0;font-size:16px;line-height:1.9;color:#35342f}}.section-pricing .section-analysis{{color:rgba(255,255,255,.82)}}.source-meta{{display:inline-block;margin-right:7px;padding:1px 5px;background:var(--paper-2);font:700 10px 'Roboto Mono',monospace;color:var(--muted)}}
</style><style>
.validation-card{{padding:22px;border:1px solid var(--ink);background:var(--lime);box-shadow:6px 6px 0 var(--ink)}}.validation-card>span{{font:700 10px 'Roboto Mono',monospace;letter-spacing:.1em}}.validation-card>h3{{margin:10px 0;font:900 30px/1 'Noto Serif SC',serif;letter-spacing:-.05em}}.validation-card>p{{margin:0 0 15px;font-size:13px}}.validation-item{{display:grid;grid-template-columns:34px 1fr;gap:10px;padding:11px 0;border-top:1px solid rgba(23,23,19,.4)}}.validation-item>span{{font:700 12px 'Roboto Mono',monospace}}.validation-item h3{{margin:0 0 3px;font-size:14px}}.validation-item p{{margin:0;font-size:12px;line-height:1.55}}
</style></head><body>
<nav><div class="wrap"><span>PUBLIC EVIDENCE / INDUSTRY BRIEF</span><a href="#sources">查看资料来源 ↓</a></div></nav>
<header class="hero"><div class="wrap"><div class="hero-grid"><div><div class="eyebrow">行业研判页 / 收集 {content["source_count"]} 条 · 入模 {content.get("evidence_count", content["source_count"])} 条 · 深读 {content.get("deep_read_count", 0)} 条</div><h1 data-shadow="{title}"><span>{title}</span></h1><p class="hero-headline">{headline}</p><p>{html.escape(content["subheadline"])}</p></div><p class="decision">{html.escape(content["decision"])}</p></div></div></header>
<main><section class="signal-band"><div class="wrap"><div class="signals">{signal_html}</div></div></section><section class="data-board"><article class="data-card"><h3>本次资料状态</h3><p>它衡量的是这次公开检索能支撑到什么程度，不代表市场规模、预测或经营结论。</p>{signal_chart_html}</article><article class="validation-card"><span>RESEARCH NEXT</span><h3>接下来要补什么</h3><p>把资料线索变成经营判断前，优先补足最关键的信息缺口，而不是先接受一个预设结论。</p>{validation_html}</article></section>{visual_board_html}{sections_html}<section id="sources" class="sources"><div class="wrap"><h2>资料来源</h2><p>本页仅基于本次搜索到的公开网页资料整理；没有找到可靠支撑的地方会明确保留“待核实”。</p><ol>{sources_html}</ol></div></section></main>
<footer><div class="wrap">TREND_GRAB · 行业研判页 · 公开资料整合，不替代报价、客户访谈或实际经营数据</div></footer></body></html>'''
