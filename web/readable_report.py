"""工厂接单研判页：将公开搜索资料整理为结构化内容，再渲染为独立 HTML 页面。"""

from __future__ import annotations

import html
import json
import math
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlparse


SECTION_BLUEPRINT = (
    ("overview", "01 / 行业概述", "市场、规模、产业链与工厂位置"),
    ("history", "02 / 发展路径", "品类起源、阶段与关键节点"),
    ("hot_topics", "03 / 近期热点", "订单、爆款与正在发生的事"),
    ("competition", "04 / 竞争格局", "玩家、代工、渠道与利润分布"),
    ("players", "05 / 头部品牌与店铺", "头部玩家、淘宝/1688头部店、跨境卖家"),
    ("cost_profit", "06 / 成本与利润", "价格带、成本结构、利润在哪"),
    ("supply_chain", "07 / 产业带与供应链", "产地、供应链、代工能力"),
    ("channels", "08 / 渠道与订单", "谁在给单、走什么渠道"),
    ("barriers", "09 / 认证与门槛", "认证、资质、工艺门槛"),
    ("trends", "10 / 趋势预测", "接下来会怎么变、什么款有空间"),
    ("risks", "11 / 风险与不确定", "接单与走量的风险、缺口"),
    ("next", "12 / 下一步验证", "接单前最该补的资料"),
)

BLOCKED_SOURCE_MARKERS = (
    "xpicvid", "ctrip", "youtube", "google.", "deepseek.com", "aydvjch",
    "porn", "成人视频", "情色", "hotel", "酒店",
)

READABLE_QUERIES = {
    "订单需求": ("订单 采购 需求 2026 最新", "热卖 爆款 销量 排名", "批发 1688 义乌 厂家 采购"),
    "产品与价格": ("产品 款式 材质 工艺 规格", "批发价 出厂价 成本 零售价", "新品 爆款 参数 起订量"),
    "买家与渠道": ("采购商 跨境卖家 外贸公司 OEM ODM", "TikTok 亚马逊 速卖通 1688 采购", "展会 批发市场 档口 订单"),
    "制造门槛": ("工厂 产能 设备 模具 生产线", "认证 CE ASTM 检测 合规", "专利 侵权 质量 退货"),
    "风险与缺口": ("风险 问题 投诉 召回", "价格战 同质化 库存 滞销", "政策 限制 失败案例"),
}
READABLE_QUALITY_SITES = (
    "36kr.com", "huxiu.com", "jiemian.com", "cifnews.com", "amz123.com", "1688.com",
    "yiwugo.com", "aliexpress.com", "made-in-china.com", "globalsources.com",
)
STORE_SEARCH_QUERIES = (
    "淘宝 头部店铺 综合排行",
    "1688 头部店铺 排行",
    "淘宝 店铺 销量 排行",
)


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


def _normalise_data_points(value: Any, valid_source_ids: set[str]) -> list[dict[str, Any]]:
    """把模型给出的关键数据点收敛为表格需要的结构。"""
    if not isinstance(value, list):
        return []
    points: list[dict[str, Any]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), "")
        number = _text(item.get("value"), "")
        if not label or not number:
            continue
        sources = [str(source) for source in item.get("sources", []) if str(source) in valid_source_ids][:2]
        points.append({
            "label": label[:32],
            "value": number[:80],
            "note": _text(item.get("note"), "")[:40],
            "sources": sources,
        })
    return points


def _normalise_image_queries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    queries: list[str] = []
    for item in value[:3]:
        query = str(item).strip()
        if 2 <= len(query) <= 40 and query not in queries:
            queries.append(query)
    return queries


def _normalise_chart(value: Any, valid_source_ids: set[str]) -> dict[str, Any] | None:
    """只保留数据完整、口径清楚的图表；没有把握就不渲染。"""
    if not isinstance(value, dict):
        return None
    chart_type = value.get("type")
    if chart_type not in {"bar", "donut"}:
        return None
    labels = value.get("labels")
    values = value.get("values")
    if not isinstance(labels, list) or not isinstance(values, list):
        return None
    if not 2 <= len(labels) <= 10 or len(labels) != len(values):
        return None
    numbers: list[float] = []
    for item in values:
        try:
            number = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0:
            return None
        numbers.append(number)
    if chart_type == "donut" and abs(sum(numbers) - 100) > 1:
        return None
    unit = _text(value.get("unit"), "")
    if not unit:
        return None
    if ("亿美元" in unit and "亿元" in unit) or ("美元" in unit and "人民币" in unit):
        return None
    labels_text = " ".join(str(label) for label in labels)
    conflicting_pairs = (
        ("销量", "销售额"),
        ("数量", "金额"),
        ("件", "美元"),
        ("件", "万元"),
        ("元", "美元"),
    )
    if any(a in labels_text and b in labels_text for a, b in conflicting_pairs):
        return None
    sources = [str(source) for source in value.get("sources", []) if str(source) in valid_source_ids][:2]
    return {
        "type": chart_type,
        "title": _text(value.get("title"), "关键数据")[:40],
        "labels": [str(label)[:40] for label in labels],
        "values": numbers,
        "unit": unit[:20],
        "note": _text(value.get("note"), "")[:80],
        "sources": sources,
    }


def _json_from_response(raw: str) -> dict[str, Any]:
    """兼容模型偶尔包裹在 Markdown 代码块中的 JSON。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            candidate, _ = decoder.raw_decode(cleaned[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and any(key in candidate for key in ("headline", "sections", "decision")):
            return candidate
    raise ValueError("模型没有返回可用的结构化内容")


def _normalise_cards(value: Any, valid_source_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    cards: list[dict[str, Any]] = []
    for item in value[:5]:
        if isinstance(item, dict):
            references = item.get("sources", [])
            references = references if isinstance(references, list) else []
            cards.append({
                "title": _text(item.get("title"), "待确认"),
                "text": _text(item.get("text")),
                "sources": [str(item) for item in references[:3] if str(item) in valid_source_ids],
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
    blueprint_order = {section_id: index for index, (section_id, _, _) in enumerate(SECTION_BLUEPRINT)}
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
                "sources": [str(source_id) for source_id in item.get("sources", [])[:12] if str(source_id) in valid_source_ids] if isinstance(item.get("sources"), list) else [],
                "data_points": _normalise_data_points(item.get("data_points"), valid_source_ids),
                "chart": _normalise_chart(item.get("chart"), valid_source_ids),
                "image_queries": _normalise_image_queries(item.get("image_queries")),
                "cards": _normalise_cards(item.get("cards"), valid_source_ids) or [{"title": "资料提示", "text": "公开资料暂不足以形成可靠结论。", "sources": []}],
            }
        )
    sections.sort(key=lambda section: blueprint_order.get(section["id"], 999))

    if not sections:
        sections = [{
            "id": "risks", "eyebrow": "06 / 风险与缺口", "title": "公开资料暂不足以判断接单机会",
            "summary": "本次搜索没有取得足够可靠且相关的接单资料。",
            "analysis": "不建议把搜索摘要当作接单结论。下一步应补充可核实的产品链接、供应商报价、平台后台或客户访谈资料。",
            "sources": [], "data_points": [], "chart": None, "image_queries": [],
            "cards": [{"title": "资料提示", "text": "资料不足，建议继续核实。", "sources": []}],
        }]

    heat = []
    for item in payload.get("export_heat", []) if isinstance(payload.get("export_heat"), list) else []:
        if isinstance(item, dict):
            heat.append({"country": _text(item.get("country"), "待确认"), "strength": _bounded_int(item.get("strength")), "note": _text(item.get("note"))})
    if not heat:
        heat = [{"country": "暂无可靠国家分布", "strength": 1, "note": "需要补充贸易或平台数据"}]

    return {
        "industry": industry,
        "headline": _text(payload.get("headline"), f"{industry}：现在能接什么单，还缺什么验证"),
        "subheadline": _text(payload.get("subheadline"), "面向代工、走量和批发工厂的公开资料研判"),
        "decision": _text(payload.get("decision"), "当前资料可判断订单、价格和门槛的大方向，但接单前仍需核实具体报价、认证和客户条件。"),
        "signals": [],
        "export_heat": heat[:5],
        "sections": sections,
        "source_count": source_count,
        "sources": sources[:60],
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


def _text_tokens(text: str) -> tuple[set[str], set[str], set[str]]:
    lowered = text.lower()
    numbers = set(re.findall(r"\d+(?:\.\d+)?", text))
    units = set(re.findall(r"(?:人民币|美元|英镑|欧元|日元|元|万元|亿元|usd|rmb|eur|gbp)", lowered))
    keywords = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z]{3,}", lowered))
    return numbers, units, keywords


def _source_supports_claim(claim_text: str, source_text: str) -> bool:
    claim_numbers, claim_units, claim_keywords = _text_tokens(claim_text)
    source_numbers, source_units, source_keywords = _text_tokens(source_text)
    if claim_numbers and claim_numbers & source_numbers:
        return True
    if claim_units and claim_units & source_units:
        return True
    return bool(claim_keywords & source_keywords)


def _clean_inline_sources(text: str, valid_ids: set[str]) -> str:
    """删掉正文里已被校验删除的 [S#]，避免正文引用和来源按钮不一致。"""
    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(0)[1:-1] in valid_ids else ""
    return re.sub(r"\[S\d+\]", replace, text)


def _prune_unsupported_sources(
    content: dict[str, Any],
    sources: list[dict[str, Any]],
    deep_text_by_id: dict[str, str],
) -> dict[str, Any]:
    """只保留来源文本里能找到对应数字或关键词的引用，避免模型硬挂来源。"""
    evidence_by_id = {
        source["id"]: " ".join([
            str(source.get("title", "")),
            str(source.get("snippet", "")),
            deep_text_by_id.get(source["id"], ""),
        ])
        for source in sources
    }

    def keep_sources(candidate_ids: list[str], claim_text: str) -> list[str]:
        kept = []
        for source_id in candidate_ids:
            source_text = evidence_by_id.get(source_id, "")
            if _source_supports_claim(claim_text, source_text):
                kept.append(source_id)
        return kept

    for section in content.get("sections", []):
        section_text = " ".join([
            section.get("title", ""),
            section.get("summary", ""),
            section.get("analysis", ""),
        ])
        section["sources"] = keep_sources(section.get("sources", []), section_text)
        valid_ids = set(section["sources"])
        kept_points = []
        for point in section.get("data_points", []):
            point["sources"] = keep_sources(
                point.get("sources", []),
                " ".join([point.get("label", ""), point.get("value", ""), point.get("note", "")]),
            )
            if not point["sources"]:
                continue
            kept_points.append(point)
            valid_ids.update(point["sources"])
        section["data_points"] = kept_points
        chart = section.get("chart")
        if chart:
            chart["sources"] = keep_sources(
                chart.get("sources", []),
                " ".join([chart.get("title", ""), " ".join(chart.get("labels", [])), " ".join(str(v) for v in chart.get("values", [])), chart.get("unit", ""), chart.get("note", "")]),
            )
            if not chart["sources"]:
                section["chart"] = None
                chart = None
            else:
                valid_ids.update(chart["sources"])
        for card in section.get("cards", []):
            card["sources"] = keep_sources(
                card.get("sources", []),
                " ".join([card.get("title", ""), card.get("text", "")]),
            )
            valid_ids.update(card["sources"])

        def add_inline_ids(text: str, claim_text: str) -> None:
            for match in re.finditer(r"\[S(\d+)\]", str(text)):
                source_id = f"S{match.group(1)}"
                if source_id in evidence_by_id and _source_supports_claim(claim_text, evidence_by_id[source_id]):
                    valid_ids.add(source_id)

        add_inline_ids(section.get("summary", ""), section_text)
        add_inline_ids(section.get("analysis", ""), section_text)
        for card in section.get("cards", []):
            card_text = " ".join([card.get("title", ""), card.get("text", "")])
            add_inline_ids(card.get("text", ""), card_text)
        for card in section.get("cards", []):
            card_inline_ids = {f"S{match}" for match in re.findall(r"\[S(\d+)\]", str(card.get("text", "")))}
            for source_id in card_inline_ids:
                if source_id in valid_ids and source_id not in card["sources"]:
                    card["sources"].append(source_id)

        ordered_ids = []
        for source_id in section.get("sources", []) + [
            source_id
            for point in section.get("data_points", [])
            for source_id in point.get("sources", [])
        ] + [
            source_id
            for card in section.get("cards", [])
            for source_id in card.get("sources", [])
        ] + (chart.get("sources", []) if chart else []):
            if source_id in valid_ids and source_id not in ordered_ids:
                ordered_ids.append(source_id)
        for source_id in valid_ids:
            if source_id not in ordered_ids:
                ordered_ids.append(source_id)
        section["sources"] = ordered_ids
        section["summary"] = _clean_inline_sources(str(section.get("summary", "")), valid_ids)
        section["analysis"] = _clean_inline_sources(str(section.get("analysis", "")), valid_ids)
        for card in section.get("cards", []):
            card["text"] = _clean_inline_sources(str(card.get("text", "")), valid_ids)
    return content


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

    for site in READABLE_QUALITY_SITES[:6]:
        if time.monotonic() > search_deadline or len(sources) >= 42:
            break
        for result in search_web(f"site:{site} {industry}", max_results=3):
            add_result("补充资料", result)

    # 保底平台货源页：即使搜索引擎质量差，也至少让模型拿到可核实的批发/货源入口。
    platform_pages = [
        ("订单需求", "1688", f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote(industry)}"),
        ("订单需求", "1688 穿戴甲", f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote('穿戴甲')}"),
        ("订单需求", "1688 厂家", f"https://s.1688.com/company/company_search.htm?keywords={quote(industry)}"),
        ("产品与价格", "1688 美甲片", f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote('美甲片')}"),
        ("产品与价格", "1688 儿童穿戴甲", f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote('儿童穿戴甲')}"),
        ("产品与价格", "淘宝", f"https://s.taobao.com/search?q={quote(industry)}"),
        ("产品与价格", "淘宝 穿戴甲", f"https://s.taobao.com/search?q={quote('穿戴甲')}"),
        ("买家与渠道", "义乌购", f"https://www.yiwugo.com/search/all.html?kw={quote(industry)}"),
        ("买家与渠道", "义乌购 穿戴甲", f"https://www.yiwugo.com/search/all.html?kw={quote('穿戴甲')}"),
        ("买家与渠道", "速卖通", f"https://www.aliexpress.com/wholesale?SearchText={quote('press on nails')}"),
        ("买家与渠道", "阿里国际", f"https://www.alibaba.com/trade/search?SearchText={quote('press on nails')}"),
        ("制造门槛", "中国制造网", f"https://www.made-in-china.com/multi-search/search1/{quote('press on nails')}/"),
        ("制造门槛", "环球资源", f"https://www.globalsources.com/search/{quote('press on nails')}/"),
        ("买家与渠道", "亚马逊", f"https://www.amazon.com/s?k={quote('press on nails')}"),
    ]

    def add_platform_source(topic: str, name: str, url: str) -> None:
        if len(sources) >= 42 or url in seen_urls:
            return
        seen_urls.add(url)
        sources.append({
            "id": f"S{len(sources) + 1}", "topic": topic,
            "title": f"{name} {industry} 货源/搜索页",
            "snippet": "平台公开货源或搜索聚合页，需要抓取页面核实价格、规格和供应商。",
            "url": url,
        })

    for topic, name, url in platform_pages:
        add_platform_source(topic, name, url)

    evidence_sources = _select_evidence_sources(sources)
    deep_sources = _select_deep_read_sources(evidence_sources)
    deep_source_ids = {source["id"] for source in deep_sources}
    for source in sources:
        source["deep_read"] = source["id"] in deep_source_ids

    page_text = []
    deep_text_by_id: dict[str, str] = {}
    for source in deep_sources:
        text = fetch_content(source["url"])
        if text:
            deep_text_by_id[source["id"]] = text[:1000]
            page_text.append(f"[{source['id']}] {source['url']}\n{text[:1000]}")
    trade_text = fetch_trade_data(industry)
    source_text = "\n\n".join(
        f"[{source['id']}] [{source['topic']}] {source['title'][:140]}\n{source['snippet'][:260]}\n{source['url']}" for source in evidence_sources
    )
    evidence = (trade_text + "\n\n" + source_text + "\n\n" + "\n---\n".join(page_text)).strip()

    prompt = f"""你是服务于义乌及产业带工厂老板的产业情报分析师，读者主要做代工（OEM/ODM）或走量批发。请根据以下公开资料，为「{industry}」制作一份《工厂接单研判页》。

只写对工厂接单、代工、走量批发有用的内容：现在有什么订单信号、什么款好做、价格和利润在哪、谁在给单、接单要什么门槛、有哪些风险。可以提及资料里真实出现的头部品牌、淘宝/1688头部店铺、跨境卖家，但必须来自资料并有来源；不要写品牌营销故事或 DTC 运营，只写它们对工厂接单的含义（谁在卖、卖什么、谁能给单）。

这不是经营指令书：你不了解该工厂的真实成本、产能、客户和报价，不能替老板下“应该投产/应该赚钱”的结论。只能使用资料中明确出现的事实和链接；不确定就写“资料不足，建议验证”，绝不能编造数字、国家、产品、价格、销量、利润或来源。不要输出 Markdown，不要输出解释，只输出一个合法 JSON 对象。

JSON 必须符合：
{{
  "headline": "一句基于资料的工厂接单判断",
  "subheadline": "一句说明本页覆盖范围",
  "decision": "一句给工厂老板的结论：能确认什么、主要限制是什么",
  "sections": [
    {{
      "id":"overview|history|hot_topics|competition|players|cost_profit|supply_chain|channels|barriers|trends|risks|next",
      "title":"...",
      "summary":"不超过45字的判断句，只给结论，不罗列数据",
      "analysis":"300到500字，分成2-4个短段落，用具体内容展开",
      "data_points":[
        {{"label":"数据名称（不超过16字）","value":"具体数值","note":"口径或补充说明（不超过20字）","sources":["S1"]}}
      ],
      "chart":{{
        "type":"bar|donut",
        "title":"图表标题（不超过18字）",
        "labels":["..."],
        "values":[1,2,3],
        "unit":"单位",
        "note":"数据口径或限制（不超过32字）",
        "sources":["S1"]
      }},
      "sources":["S1","S2"],
      "image_queries":["具体产品/场景搜索词1","具体产品/场景搜索词2"],
      "cards":[{{"title":"...","text":"150到200字，写具体内容","sources":["S1","S2"]}}]
    }}
  ]
}}

反重复铁律：同一事实和同一组数字只允许出现一次。data_points 是结构化数据点，图表只画 data_points 里的数据；summary 只写判断句，不复述完整数字；analysis 最多解释一次关键数据，不要逐条重列；cards 只放新增案例、细节或解读，不重写 data_points 已列出的内容。跨板块也遵守：同一数据只在其最相关的板块作为主信息，其他板块用“如前所述”带过，不要再次写全数字。

工作顺序必须是：先阅读研究资料，再写事实和分析，最后从资料里挑来源编号。禁止先写内容再反向找来源，禁止凭印象补来源。资料里没有的事实不要写，资料里有的数字、价格、案例才允许进入正文并挂上对应编号。

sections 从上述 12 个 id 中选择 6-11 个，顺序由本次资料决定，优先保留资料里有具体数据和结论的章节，不要为了框架硬凑，也不要因为框架限制丢掉有价值内容：overview=行业概述（市场、规模、产业链、工厂位置），history=发展路径（起源、阶段、关键节点），hot_topics=近期热点（订单、爆款、案例），competition=竞争格局（玩家、代工、渠道、利润），players=头部品牌与店铺（头部玩家、淘宝/1688头部店、跨境卖家），cost_profit=成本与利润（价格带、成本结构、利润在哪），supply_chain=产业带与供应链（产地、供应链、代工能力），channels=渠道与订单（谁在给单、走什么渠道），barriers=认证与门槛（认证、资质、工艺门槛），trends=趋势预测（短期/中期、什么款有空间），risks=风险与不确定，next=下一步验证。用白皮书里的具体内容，不要过度压缩；表述像给工厂老板讲人话，不要书面报告腔；保留具体数字、案例、价格、工厂线索。每个板块的 analysis 必须正面回应 title，分成 2-4 个短段落，段落之间用空行分隔；每段先给判断，再给资料里的支撑，不要写成一整段；总长度 300-500 字。每个板块根据自己正文里的具体产品、款式、场景、案例，写 1-2 个图片搜索词到 image_queries；不能只写行业名或板块名，没有把握就返回 []。每个板块最多 5 张卡片；cards 的 text 必须直接解释卡片 title，用 150-200 字展开具体内容；写具体可用的接单线索，例如订单类型、起订量、价格带、认证要求、买家渠道，不要写“品牌通过 DTC 进入市场”这类老板不关心的内容。data_points 只有该板块有明确可引用数据时才写，3-8 条，没有数据就返回 []；value 必须和来源中的原始口径一致，不能换算、不能补造。chart 可选：只能用于同一指标、同一单位的对比（例如不同年份的同一市场规模）；不要把销量和销售额、数量和金额、不同币种混在一张图。没有可比较的同口径数据就省略 chart，data_points 仍然展示。labels 和 values 数量一致（2-10），donut 的 values 合计需接近 100，没有把握就省略 chart。section、data_points、chart 和每张卡片只要出现事实、数字、平台、产品、国家或案例，就必须在 sources 字段填入真正支持该说法的来源编号；资料不足时保留空数组，不能猜编号。每个来源必须直接支撑它被挂上的那条事实；宁可少挂，不要为了看起来资料充足而硬挂不相关来源。如果某来源只提到行业整体、没有支撑这条具体数据或案例，不要放进去。

研究资料（已按订单、产品、渠道、门槛、风险均衡挑选；标有“深读”的资料正文更完整）：
{evidence}"""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.35,
        max_tokens=12000,
    )
    content = normalise_content(
        _json_from_response(response.choices[0].message.content), industry, sources,
        {source["id"] for source in evidence_sources},
    )
    content = _prune_unsupported_sources(content, sources, deep_text_by_id)
    content["evidence_count"] = len(evidence_sources)
    content["deep_read_count"] = len(deep_sources)
    return content


def _parse_deep_sources(deep_text: str) -> tuple[list[dict[str, Any]], dict[str, str], str]:
    """把深度报告里的行内 URL 引用转成 S1..Sn，并把报告替换为带编号的证据文本。"""
    citation_re = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
    sources: list[dict[str, Any]] = []
    contexts_by_url: dict[str, list[str]] = {}
    url_to_sid: dict[str, str] = {}
    evidence_parts: list[str] = []
    pos = 0

    for match in citation_re.finditer(deep_text):
        label = match.group(1).strip()
        url = match.group(2)
        start = max(0, match.start() - 350)
        end = min(len(deep_text), match.end() + 350)
        context = re.sub(r"\s+", " ", deep_text[start:end]).strip()[:600]

        if url not in url_to_sid:
            sid = f"S{len(sources) + 1}"
            url_to_sid[url] = sid
            host = urlparse(url).netloc or url
            before = re.sub(r"\[[^\]]*\]\(https?://[^)\s]+\)", "", deep_text[max(0, match.start() - 120):match.start()])
            before = re.sub(r"\s+", " ", before).strip()
            sentence = re.split(r"[。！？；]", before)[-1].strip()
            title = label if label and label != "↗" else (sentence[:60] or host)
            title = re.sub(r"[#*`>]+", "", title).strip() or host
            if len(title) > 60:
                title = title[:60] + "…"
            sources.append({
                "id": sid, "url": url, "title": title,
                "snippet": context, "topic": "深度报告", "deep_read": True,
            })
            contexts_by_url[url] = [context]
        else:
            sid = url_to_sid[url]
            contexts_by_url[url].append(context)

        evidence_parts.append(deep_text[pos:match.start()] + f"[{sid}]")
        pos = match.end()

    evidence_parts.append(deep_text[pos:])
    deep_text_by_id = {
        source["id"]: "\n".join(contexts_by_url.get(source["url"], [source["snippet"]]))
        for source in sources
    }
    return sources, deep_text_by_id, "".join(evidence_parts)


def generate_from_deep_report(
    client: Any,
    model: str,
    industry: str,
    deep_text: str,
    search_web: Callable[[str, int], list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    """阶段一（深度报告版）：从已有深度报告里抽取老板版内容，不重新做浅层搜索。"""
    sources, deep_text_by_id, evidence_text = _parse_deep_sources(deep_text)
    if not sources:
        sources = [{
            "id": "S1", "url": "", "title": "深度研究报告",
            "snippet": deep_text[:400], "topic": "深度报告", "deep_read": True,
        }]
        deep_text_by_id = {"S1": deep_text}
        evidence_text = deep_text

    store_sources: list[dict[str, Any]] = []
    store_evidence_parts: list[str] = []
    if search_web:
        seen_store_urls: set[str] = set()
        industry_compact = re.sub(r"\s+", "", industry.lower())

        def add_store_source(item: dict[str, Any], topic: str) -> None:
            url = str(item.get("url", "")).strip()
            if not url or url in seen_store_urls:
                return
            title = str(item.get("title", "")).strip() or "店铺来源"
            snippet = str(item.get("snippet", "")).strip()
            combined = f"{title}\n{snippet}\n{url}".lower()
            if industry_compact and industry_compact not in combined and not any(
                marker in combined for marker in ("淘宝", "1688", "店铺", "排行", "卖家")
            ):
                return
            seen_store_urls.add(url)
            source_id = f"S{len(sources) + len(store_sources) + 1}"
            store_sources.append({
                "id": source_id, "url": url, "title": title,
                "snippet": snippet, "topic": topic, "deep_read": False,
            })
            store_evidence_parts.append(f"[{source_id}] [{topic}] {title}\n{snippet}\n{url}")

        for query in STORE_SEARCH_QUERIES:
            for result in search_web(f"{industry} {query}", 5):
                add_store_source(result, "淘宝/1688头部店铺")
        sources.extend(store_sources)
        for source in store_sources:
            deep_text_by_id[source["id"]] = source["snippet"]

    evidence = evidence_text[:60000]
    store_evidence = "\n\n".join(store_evidence_parts)
    if store_evidence:
        evidence += "\n\n---\n\n淘宝/1688 头部店铺搜索结果（用于 players 板块，编号延续为 [S#]）：\n" + store_evidence
    prompt = f"""你是服务于义乌及产业带工厂老板的产业情报分析师，读者主要做代工（OEM/ODM）或走量批发。以下是一份已经完成的「{industry}」深度研究报告，引用已经替换成 [S#] 编号。请只从这份深度报告里选取对工厂接单、代工、走量批发真正有用的内容，做成《工厂接单研判页》。

深度报告通常按五章组织：行业概述、发展路径、近期热点、竞争格局、趋势预测。请以这五章为骨架组织老板版：行业概述转成“市场、规模、产业链与工厂位置”，发展路径转成“品类怎么走到今天、哪些节点影响现在”，近期热点转成“订单、爆款与正在发生的事”，竞争格局转成“玩家、代工、渠道与利润分布”，趋势预测转成“接下来会怎么变、什么款有空间”，再补充风险、下一步验证和头部品牌与店铺。不要新增报告里没有的事实，不要写品牌营销策略、DTC 运营、消费者品牌故事或宏观叙事，除非它们直接关系到拿单、代工、批发、价格、认证或供应链。可以提及深度报告里真实出现的头部品牌、淘宝/1688头部店铺、跨境卖家，也可以使用下方补充的 [S#] 淘宝/1688店铺搜索结果，但必须来自资料并有来源；只写它们对工厂接单的含义。

这不是经营指令书：你不了解该工厂的真实成本、产能、客户和报价，不能替老板下“应该投产/应该赚钱”的结论。只能使用深度报告中明确出现的事实和 [S#] 编号；不确定就写“资料不足，建议验证”，绝不能编造数字、国家、产品、价格、销量、利润或来源。不要输出 Markdown，不要输出解释，只输出一个合法 JSON 对象。

JSON 必须符合：
{{
  "headline": "一句基于深度报告的工厂接单判断",
  "subheadline": "一句说明本页覆盖范围",
  "decision": "一句给工厂老板的结论：能确认什么、主要限制是什么",
  "sections": [
    {{
      "id":"overview|history|hot_topics|competition|players|cost_profit|supply_chain|channels|barriers|trends|risks|next",
      "title":"...",
      "summary":"不超过45字的判断句，只给结论，不罗列数据",
      "analysis":"300到500字，分成2-4个短段落，用具体内容展开",
      "data_points":[
        {{"label":"数据名称（不超过16字）","value":"具体数值","note":"口径或补充说明（不超过20字）","sources":["S1"]}}
      ],
      "chart":{{
        "type":"bar|donut",
        "title":"图表标题（不超过18字）",
        "labels":["..."],
        "values":[1,2,3],
        "unit":"单位",
        "note":"数据口径或限制（不超过32字）",
        "sources":["S1"]
      }},
      "sources":["S1","S2"],
      "image_queries":["具体产品/场景搜索词1","具体产品/场景搜索词2"],
      "cards":[{{"title":"...","text":"150到200字，写具体内容","sources":["S1","S2"]}}]
    }}
  ]
}}

反重复铁律：同一事实和同一组数字只允许出现一次。data_points 是结构化数据点，图表只画 data_points 里的数据；summary 只写判断句，不复述完整数字；analysis 最多解释一次关键数据，不要逐条重列；cards 只放新增案例、细节或解读，不重写 data_points 已列出的内容。跨板块也遵守：同一数据只在其最相关的板块作为主信息，其他板块用“如前所述”带过，不要再次写全数字。

工作顺序必须是：先阅读深度报告，再写事实和分析，最后从报告里挑 [S#] 编号。禁止先写内容再反向找来源，禁止凭印象补来源。报告里没有的事实不要写，报告里有的数字、价格、案例才允许进入正文并挂上对应编号。

sections 从上述 12 个 id 中选择 6-11 个，顺序由深度报告内容决定，优先保留报告里有具体数据和结论的章节，不要为了框架硬凑，也不要因为框架限制丢掉有价值内容：overview=行业概述（市场、规模、产业链、工厂位置），history=发展路径（起源、阶段、关键节点），hot_topics=近期热点（订单、爆款、案例），competition=竞争格局（玩家、代工、渠道、利润），players=头部品牌与店铺（头部玩家、淘宝/1688头部店、跨境卖家），cost_profit=成本与利润（价格带、成本结构、利润在哪），supply_chain=产业带与供应链（产地、供应链、代工能力），channels=渠道与订单（谁在给单、走什么渠道），barriers=认证与门槛（认证、资质、工艺门槛），trends=趋势预测（短期/中期、什么款有空间），risks=风险与不确定，next=下一步验证。用深度报告里的具体内容，不要过度压缩；表述像给工厂老板讲人话，不要书面报告腔；保留具体数字、案例、价格、工厂线索。每个板块的 analysis 必须正面回应 title，分成 2-4 个短段落，段落之间用空行分隔；每段先给判断，再给报告里的支撑，不要写成一整段；总长度 300-500 字。每个板块根据自己正文里的具体产品、款式、场景、案例，写 1-2 个图片搜索词到 image_queries；不能只写行业名或板块名，没有把握就返回 []。每个板块最多 5 张卡片；cards 的 text 必须直接解释卡片 title，用 150-200 字展开具体内容；写具体可用的接单线索，例如订单类型、起订量、价格带、认证要求、买家渠道，不要写“品牌通过 DTC 进入市场”这类老板不关心的内容。data_points 只有该板块有明确可引用数据时才写，3-8 条，没有数据就返回 []；value 必须和报告中的原始口径一致，不能换算、不能补造。chart 可选：只能用于同一指标、同一单位的对比（例如不同年份的同一市场规模）；不要把销量和销售额、数量和金额、不同币种混在一张图。没有可比较的同口径数据就省略 chart，data_points 仍然展示。labels 和 values 数量一致（2-10），donut 的 values 合计需接近 100，没有把握就省略 chart。section、data_points、chart 和每张卡片只要出现事实、数字、平台、产品、国家或案例，就必须在 sources 字段填入真正支持该说法的 [S#] 编号；资料不足时保留空数组，不能猜编号。每个来源必须直接支撑它被挂上的那条事实；宁可少挂，不要为了看起来资料充足而硬挂不相关来源。

深度报告（已替换为 [S#] 编号）：
{evidence}"""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.35,
        max_tokens=12000,
    )
    content = normalise_content(
        _json_from_response(response.choices[0].message.content), industry, sources,
        {source["id"] for source in sources},
    )
    existing_source_ids = {source["id"] for source in content["sources"]}
    content["sources"] = content["sources"] + [
        source for source in store_sources if source["id"] not in existing_source_ids
    ]
    content = _prune_unsupported_sources(content, sources, deep_text_by_id)
    content["evidence_count"] = len(sources)
    content["deep_read_count"] = len(sources)
    return content


def _source_link(source_id: str, source_map: dict[str, dict[str, Any]]) -> str:
    source = source_map.get(source_id)
    if source and source.get("url"):
        return (
            f'<a href="{html.escape(source["url"], quote=True)}" target="_blank" rel="noopener noreferrer" '
            f'title="{html.escape(source.get("title", ""), quote=True)}">'
            f'[{html.escape(source_id, quote=True)}]</a>'
        )
    return f'<span class="source-ref">[{html.escape(source_id, quote=True)}]</span>'


def _render_citations(card: dict[str, Any], source_map: dict[str, dict[str, Any]]) -> str:
    source_ids = card.get("sources", [])
    if not source_ids:
        return '<span class="no-citation">来源待核实</span>'
    return '<span class="citations">' + " ".join(_source_link(source_id, source_map) for source_id in source_ids) + '</span>'


def _render_inline_sources(text: str, source_map: dict[str, dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        source_id = match.group(0)[1:-1]
        source = source_map.get(source_id)
        if source and source.get("url"):
            return (
                f'<a class="inline-source" href="{html.escape(source["url"], quote=True)}" '
                f'target="_blank" rel="noopener noreferrer" '
                f'title="{html.escape(source.get("title", ""), quote=True)}">[{html.escape(source_id, quote=True)}]</a>'
            )
        return match.group(0)
    return re.sub(r"\[S\d+\]", replace, text)


def _render_analysis(text: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    if not blocks:
        return ""
    parts: list[str] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if any(line.startswith(("-", "•", "1.", "2.", "3.")) for line in lines):
            parts.append("<ul>" + "".join(f"<li>{line.lstrip('-•0123456789. ')}</li>" for line in lines) + "</ul>")
        else:
            parts.append("<p>" + " ".join(lines) + "</p>")
    return "".join(parts)


def _render_data_points(data_points: list[dict[str, Any]], source_map: dict[str, dict[str, Any]]) -> str:
    if not data_points:
        return ""
    rows = []
    for point in data_points:
        citations = _render_citations({"sources": point.get("sources", [])}, source_map)
        note = f'<span class="data-note">{html.escape(point["note"])}</span>' if point.get("note") else ""
        rows.append(
            f'<tr><td class="data-label">{html.escape(point["label"])}</td>'
            f'<td class="data-value">{html.escape(point["value"])}</td>'
            f'<td>{note}{citations}</td></tr>'
        )
    return (
        '<div class="data-panel"><table class="data-table"><thead>'
        '<tr><th>数据点</th><th>数值</th><th>口径 / 来源</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _render_chart(chart: dict[str, Any] | None, source_map: dict[str, dict[str, Any]]) -> str:
    if not chart:
        return ""
    title = html.escape(chart["title"])
    unit = html.escape(chart.get("unit", ""))
    note = html.escape(chart.get("note", ""))
    note_html = f'<p class="chart-note">{note}</p>' if note else ""
    chart_links = []
    for source_id in chart.get("sources", []):
        chart_links.append(_source_link(source_id, source_map))
    chart_sources = " ".join(chart_links)
    sources_html = f'<p class="chart-sources">来源：{chart_sources}</p>' if chart_sources else ""
    if chart["type"] == "donut":
        colors = ("var(--coral)", "var(--sky)", "var(--lime)", "var(--sun)")
        stops = []
        cumulative = 0.0
        for index, value in enumerate(chart["values"]):
            start = cumulative
            cumulative += value
            stops.append(f"{colors[index % len(colors)]} {start:.2f}% {cumulative:.2f}%")
        legend = "".join(
            f'<span><b style="background:{colors[index % len(colors)]}"></b>'
            f'{html.escape(str(chart["labels"][index]))} {html.escape(str(value))}{unit}</span>'
            for index, value in enumerate(chart["values"])
        )
        return (
            f'<div class="chart-panel"><h3>{title}</h3>{note_html}'
            f'<div class="chart-donut"><div class="chart-donut-ring" style="background:conic-gradient({", ".join(stops)})"></div>'
            f'<div class="chart-legend">{legend}</div></div>{sources_html}</div>'
        )
    max_value = max(chart["values"]) or 1
    rows = []
    for index, value in enumerate(chart["values"]):
        width = min(100.0, value / max_value * 100)
        warn = " warn" if value == max_value else ""
        rows.append(
            f'<div class="chart-row"><span>{html.escape(str(chart["labels"][index]))}</span>'
            f'<div class="chart-track"><i class="chart-fill{warn}" style="width:{width:.1f}%"></i></div>'
            f'<b>{html.escape(str(value))}{unit}</b></div>'
        )
    return f'<div class="chart-panel"><h3>{title}</h3>{note_html}<div class="chart-bars">{"".join(rows)}</div>{sources_html}</div>'


def _render_cards(cards: list[dict[str, Any]], source_map: dict[str, dict[str, Any]]) -> str:
    return "".join(
        f'<article class="insight-card"><h3>{html.escape(card["title"])}</h3><p>{_render_inline_sources(html.escape(card["text"]), source_map)}</p>{_render_citations(card, source_map)}</article>'
        for card in cards
    )


def _render_section_image(section: dict[str, Any]) -> str:
    images = section.get("images", [])
    if not images:
        return ""
    image = images[0]
    if not image.get("url"):
        return ""
    media = (
        f'<figure class="section-media"><img src="{html.escape(image["url"], quote=True)}" '
        f'alt="{html.escape(image.get("title", section["title"]), quote=True)}" loading="lazy">'
        f'<figcaption>{html.escape(image.get("title", "行业参考图"))}</figcaption></figure>'
    )
    source_url = image.get("source")
    if source_url:
        return (
            f'<a class="section-media-link" href="{html.escape(source_url, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">{media}</a>'
        )
    return media


def _section_frame(section: dict[str, Any], body: str, source_map: dict[str, dict[str, Any]]) -> str:
    return f'''<section id="{section["id"]}" class="section section-{section["id"]}">
  <div class="section-head"><span>{html.escape(section["eyebrow"])}</span><h2>{html.escape(section["title"])}</h2></div>
  <p class="section-summary">{_render_inline_sources(html.escape(section["summary"]), source_map)}</p>
  <div class="section-analysis">{_render_analysis(_render_inline_sources(html.escape(section["analysis"]), source_map))}</div>{_render_citations(section, source_map)}{body}
</section>'''


def _render_section(section: dict[str, Any], source_map: dict[str, dict[str, Any]], verify_html: str = "") -> str:
    """不同接单板块使用不同信息布局，避免整页都是同一种卡片。"""
    cards = section["cards"]
    section_id = section["id"]
    structured = _render_section_image(section) + _render_chart(section.get("chart"), source_map) + _render_data_points(section.get("data_points", []), source_map)
    if section_id == "overview":
        body = structured + '<div class="market-layout"><div class="evidence-stack">' + _render_cards(cards[:2], source_map) + '</div>'
        if len(cards) > 2:
            body += f'<aside class="market-poster"><small>OVERVIEW / SIGNAL</small><b>{html.escape(cards[2]["title"])}</b><p>{html.escape(cards[2]["text"])}</p>{_render_citations(cards[2], source_map)}</aside>'
        body += '</div>'
    elif section_id in {"hot_topics", "players"}:
        body = structured + '<div class="product-notes">' + "".join(
            f'<article class="product-note note-{index}"><span>0{index + 1}</span><h3>{html.escape(card["title"])}</h3><p>{html.escape(card["text"])}</p>{_render_citations(card, source_map)}</article>'
            for index, card in enumerate(cards)
        ) + '</div>'
        if section_id == "players" and verify_html:
            body += f'<div class="verify-zone">{verify_html}</div>'
    elif section_id in {"competition", "cost_profit"}:
        body = structured + '<div class="price-flow">' + "".join(
            f'<article class="price-stop"><span>STEP {index + 1}</span><b>{html.escape(card["title"])}</b><p>{html.escape(card["text"])}</p>{_render_citations(card, source_map)}</article>{"<i class=\"flow-arrow\">→</i>" if index < len(cards) - 1 else ""}'
            for index, card in enumerate(cards)
        ) + '</div>'
    elif section_id in {"history", "trends", "supply_chain", "channels"}:
        body = structured + '<div class="channel-route">' + "".join(
            f'<article class="route-stop"><b>{index + 1:02d}</b><div><h3>{html.escape(card["title"])}</h3><p>{html.escape(card["text"])}</p>{_render_citations(card, source_map)}</div></article>'
            for index, card in enumerate(cards)
        ) + '</div>'
    else:  # barriers / risks / next
        body = structured + '<div class="risk-wall">' + "".join(
            f'<article class="risk-item"><b>!</b><div><h3>{html.escape(card["title"])}</h3><p>{html.escape(card["text"])}</p>{_render_citations(card, source_map)}</div></article>'
            for card in cards
        ) + '</div>'
    return _section_frame(section, body, source_map)


def _signal_score(status: str) -> int:
    return {"green": 78, "yellow": 52, "red": 29}.get(status, 45)


def _signal_label(status: str) -> str:
    return {"green": "线索较充分", "yellow": "需要核实", "red": "缺口明显"}.get(status, "资料不足")


def _heat_label(strength: int) -> str:
    return {1: "少量提及", 2: "少量提及", 3: "多次提及", 4: "多次提及", 5: "多次提及"}.get(strength, "待验证")


def render_html(content: dict[str, Any]) -> str:
    """阶段二：只由 Python 负责把 JSON 填进固定版式。"""
    for section in content.get("sections", []):
        section.setdefault("analysis", section.get("summary", ""))
        section.setdefault("cards", [])
        section.setdefault("sources", [])
        section.setdefault("data_points", [])
        section.setdefault("chart", None)
        section.setdefault("image_queries", [])
    has_section_images = any(section.get("images") for section in content["sections"])
    def render_scene(image: dict[str, Any], index: int) -> str:
        figure = (
            f'<figure class="scene scene-{index}"><img src="{html.escape(image["url"], quote=True)}" '
            f'alt="{html.escape(image.get("title", content["industry"]))}" loading="lazy">'
            f'<figcaption>{html.escape(image.get("title", "行业参考图"))}</figcaption></figure>'
        )
        source_url = image.get("source")
        if source_url:
            return (
                f'<a class="scene-link" href="{html.escape(source_url, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{figure}</a>'
            )
        return figure
    image_html = "".join(
        render_scene(image, index)
        for index, image in enumerate(content.get("images", [])[:4], start=1)
        if isinstance(image, dict) and image.get("url")
    )
    product_section = next((section for section in content["sections"] if section["id"] == "overview"), None)
    visual_context = ""
    if product_section:
        visual_context = (
            f'<div class="visual-copy"><span>VISUAL / CONTEXT</span><h2>{html.escape(product_section["title"])}</h2>'
            f'<p>{html.escape(product_section["summary"])}</p><small>图片用于理解行业产品与场景，不作为销量、价格或市场规模证据。</small></div>'
        )
    visual_board_html = (
        f'<section class="visual-board">{visual_context}<div class="visual-gallery">{image_html}</div></section>'
        if image_html and not has_section_images else ""
    )
    source_map = {source["id"]: source for source in content["sources"]}
    taobao_shop_url = f"https://s.taobao.com/search?q={quote(content['industry'])}&tab=shop"
    taobao_item_url = f"https://s.taobao.com/search?q={quote(content['industry'])}&tab=all"
    ali1688_url = f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote(content['industry'])}"
    verify_links = (
        f'<a class="verify-link" href="{html.escape(taobao_shop_url, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">淘宝按店铺核实 →</a>'
        f'<a class="verify-link verify-link-alt" href="{html.escape(taobao_item_url, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">淘宝按宝贝核实 →</a>'
        f'<a class="verify-link verify-link-alt" href="{html.escape(ali1688_url, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">1688按店铺核实 →</a>'
    )
    home_link = '<a class="nav-btn nav-btn-alt" href="/">返回重新生成</a>'
    sections_html = "".join(
        _render_section(section, source_map, verify_links if section["id"] == "players" else "")
        for section in content["sections"]
    )
    sources_html = "".join(
        f'<li><span class="source-meta">{html.escape(source.get("topic", "资料"))}{" · 深读" if source.get("deep_read") else ""}</span>'
        f'<span class="source-title">[{html.escape(source["id"])}] {html.escape(source["title"])}</span></li>'
        for source in content["sources"]
    ) or "<li>本次未取得可引用的公开来源。</li>"
    title = html.escape(content["industry"])
    headline = html.escape(content["headline"])
    deep_report_url = content.get("deep_report_url", "")
    deep_report_link = (
        f'<a class="nav-btn" href="{html.escape(deep_report_url, quote=True)}" target="_blank" rel="noopener noreferrer">查看原始深度报告</a>'
        if deep_report_url else ""
    )
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}｜工厂接单研判页</title>
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
</style><style>
.data-panel{{margin:22px 0 0;border:1px solid var(--ink);background:var(--white);box-shadow:5px 5px 0 var(--ink)}}.data-table{{width:100%;border-collapse:collapse;font-size:13px}}.data-table th{{padding:10px 12px;background:var(--paper-2);border-bottom:1px solid var(--ink);font:700 10px 'Roboto Mono',monospace;text-align:left;letter-spacing:.06em}}.data-table td{{padding:10px 12px;border-bottom:1px solid rgba(23,23,19,.16);vertical-align:top}}.data-table tr:last-child td{{border-bottom:0}}.data-label{{font-weight:700}}.data-value{{font:700 15px 'Roboto Mono',monospace;white-space:nowrap}}.data-note{{display:block;color:var(--muted);font-size:12px}}.chart-panel{{margin:22px 0 0;padding:18px;border:1px solid var(--ink);background:var(--white);box-shadow:6px 6px 0 var(--ink)}}.chart-panel h3{{margin:0 0 4px;font:800 18px 'Noto Serif SC',serif}}.chart-note{{margin:0 0 14px;font-size:11px;color:var(--muted)}}.chart-sources{{margin:12px 0 0;font:700 10px 'Roboto Mono',monospace;color:var(--muted)}}.chart-sources a{{margin-left:4px;padding:2px 4px;background:var(--lime);border:1px solid var(--ink);color:var(--ink);text-decoration:none}}.chart-bars{{display:grid;gap:9px}}.chart-row{{display:grid;grid-template-columns:minmax(76px,120px) 1fr 88px;gap:10px;align-items:center;font:700 11px 'Roboto Mono',monospace}}.chart-track{{height:18px;background:var(--paper-2);border:1px solid var(--ink)}}.chart-fill{{display:block;height:100%;background:var(--lime)}}.chart-row:nth-child(even) .chart-fill{{background:var(--sky)}}.chart-fill.warn{{background:var(--coral)}}.chart-donut{{display:grid;grid-template-columns:140px 1fr;gap:20px;align-items:center}}.chart-donut-ring{{width:128px;height:128px;border-radius:50%;border:1px solid var(--ink)}}.chart-legend{{display:grid;gap:8px;font-size:12px}}.chart-legend span b{{display:inline-block;width:10px;height:10px;margin-right:6px;border:1px solid var(--ink)}}@media(max-width:680px){{.chart-row{{grid-template-columns:1fr 1fr}}.chart-row .chart-track{{grid-column:1/-1}}.chart-donut{{grid-template-columns:1fr}}.chart-donut-ring{{margin:auto}}.data-value{{white-space:normal}}}}
</style><style>
.visual-gallery{{grid-template-columns:repeat(2,1fr)!important;gap:16px!important}}.visual-gallery .scene{{min-height:210px!important}}.visual-gallery .scene img{{min-height:210px!important}}@media(max-width:680px){{.visual-gallery{{grid-template-columns:1fr!important}}.visual-gallery .scene{{min-height:200px!important}}}}
</style><style>
:root{{--muted:#4f4e48!important}}.price-flow{{background:var(--white)!important;border:1px solid var(--ink)!important;box-shadow:6px 6px 0 var(--ink)!important}}.price-stop{{background:var(--white)!important;border:1px solid var(--ink)!important}}.price-stop p{{color:#35342f!important}}.flow-arrow{{color:var(--coral)!important}}.risk-item p{{color:rgba(255,255,255,.88)!important}}.citations,.no-citation,.chart-sources,.source-meta{{font-size:11px!important}}.citations a{{padding:3px 7px!important;font-size:11px!important}}.source-meta{{color:var(--ink)!important;background:var(--white)!important;border:1px solid var(--ink)!important}}.data-table th{{font-size:11px!important}}.source-ref{{color:var(--muted)!important}}.data-table .citations{{display:inline-block!important;margin:4px 0 0!important}}.chart-sources a{{padding:3px 6px!important}}.section-pricing .price-flow,.section-pricing .price-stop,.section-pricing .price-stop span,.section-pricing .price-stop b,.section-pricing .price-stop p{{color:var(--ink)!important}}.section-pricing .flow-arrow{{color:var(--coral)!important}}.section-pricing .chart-panel,.section-pricing .data-panel,.section-pricing .data-table{{color:var(--ink)!important}}.section-pricing .data-note,.section-pricing .chart-note,.section-pricing .chart-sources{{color:var(--muted)!important}}
</style><style>
.section-media{{max-width:560px;margin:24px 0 0;border:1px solid var(--ink);background:var(--white);box-shadow:6px 6px 0 var(--coral)}}.section-media img{{display:block;width:100%;max-height:320px;object-fit:cover}}.section-media figcaption{{padding:8px 12px;background:var(--white);border-top:1px solid var(--ink);font:700 11px 'Roboto Mono',monospace;color:var(--ink)}}.section:nth-of-type(even) .section-media{{margin-left:auto}}.source-title{{display:block;margin-top:3px;color:var(--ink)!important}}.sources li{{margin:0 0 12px}}.citations a{{max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle}}
</style><style>
.inline-source{{display:inline-block;margin:0 2px;padding:1px 4px;background:var(--lime);border:1px solid var(--ink);color:var(--ink);font:700 10px 'Roboto Mono',monospace;text-decoration:none;white-space:nowrap}}.inline-source:hover{{background:var(--coral)}}
</style><style>
.section-media-link{{display:block;text-decoration:none;color:inherit}}.section-media-link:hover .section-media{{box-shadow:8px 8px 0 var(--ink)}}.scene-link{{display:block;text-decoration:none;color:inherit}}.section-analysis p{{margin:0 0 12px}}.section-analysis ul{{margin:12px 0 0;padding-left:18px}}.section-analysis li{{margin:0 0 6px}}
</style><style>
.nav-links{{display:flex;gap:14px;align-items:center}}.nav-links a{{color:inherit;text-decoration:none;white-space:nowrap}}.nav-btn{{display:inline-flex;align-items:center;padding:8px 13px;border:1px solid var(--ink);background:var(--sky);color:var(--ink)!important;font:800 12px 'Noto Sans SC',sans-serif;text-decoration:none;box-shadow:4px 4px 0 var(--ink)}}.nav-btn-alt{{background:var(--lime)}}.nav-btn:hover{{background:var(--coral)}}.decision-zone{{display:grid;gap:12px;justify-items:start}}.verify-zone{{display:flex;gap:10px;flex-wrap:wrap}}.verify-link{{display:inline-flex;align-items:center;padding:10px 14px;border:1px solid var(--ink);background:var(--coral);color:var(--ink);font:800 14px 'Noto Sans SC',sans-serif;text-decoration:none;box-shadow:5px 5px 0 var(--ink)}}.verify-link-alt{{background:var(--sky)}}.verify-link:hover{{background:var(--lime)}}
</style><style>
.sources ol{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin:28px 0 0;padding:0;list-style:none}}.sources li{{margin:0!important;padding:14px 16px;border:1px solid var(--ink);border-left:5px solid var(--coral);background:var(--white);box-shadow:5px 5px 0 var(--paper-2)}}.source-meta{{background:var(--lime)!important;border:1px solid var(--ink)!important;color:var(--ink)!important}}@media(max-width:680px){{.sources ol{{grid-template-columns:1fr}}.nav-links{{gap:8px}}.nav-btn{{padding:7px 10px;font-size:11px}}}}
</style></head><body>
<nav><div class="wrap"><span>FACTORY BRIEF / 工厂接单研判</span><div class="nav-links">{home_link}{deep_report_link}<a class="nav-btn nav-btn-alt" href="#sources">查看资料来源 ↓</a></div></div></nav>
<header class="hero"><div class="wrap"><div class="hero-grid"><div><div class="eyebrow">工厂接单研判页</div><h1 data-shadow="{title}"><span>{title}</span></h1><p class="hero-headline">{headline}</p><p>{html.escape(content["subheadline"])}</p></div><div class="decision-zone"><p class="decision">{html.escape(content["decision"])}</p></div></div></div></header>
<main>{visual_board_html}{sections_html}<section id="sources" class="sources"><div class="wrap"><h2>资料来源</h2><p>本页基于公开资料整理；没有可靠支撑的地方会明确保留“待核实”。</p><ol>{sources_html}</ol></div></section></main>
<footer><div class="wrap">TREND_GRAB · 工厂接单研判 · 接单前仍需核实价格、认证和客户条件</div></footer></body></html>'''
