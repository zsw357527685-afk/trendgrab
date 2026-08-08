#!/usr/bin/env python3
"""
trend_grab Web — 行业白皮书一键生成

启动: python web/server.py
访问: http://localhost:8001
"""

import json
import os
import re
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel

try:
    from readable_report import generate_content as generate_readable_content
    from readable_report import generate_from_deep_report
    from readable_report import render_html as render_readable_html
except ImportError:  # 支持以 `uvicorn web.server:app` 方式启动
    from web.readable_report import generate_content as generate_readable_content
    from web.readable_report import generate_from_deep_report
    from web.readable_report import render_html as render_readable_html

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

app = FastAPI(title="trend_grab", version="2.34.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── LLM 配置 ─────────────────────────────────────────────
LLM_BASE = os.getenv("LLM_BASE_URL", os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1"))
LLM_KEY = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

client = OpenAI(base_url=LLM_BASE, api_key=LLM_KEY) if LLM_KEY else None

# ── 搜索维度 ─────────────────────────────────────────────
DIMENSIONS = {
    "market": [
        "2026年 市场规模 数据 统计 最新",
        "销售额 销量 增长 2026 同比",
        "市场份额 占有率 排名 2026",
        "子品类 细分 占比 增速",
        "消费者 人群 画像 购买力 2026",
        "market size revenue 2026 data statistics",
        "行业 产值 规模 2026 分析",
    ],
    "hot": [
        "2026年7月 最新 热点 新闻",
        "2026年8月 最新 动态 事件",
        "TikTok 爆款 新品 走红 热销 2026",
        "亚马逊 热卖 排名 销量 2026",
        "出海 跨境 热销 案例 2026",
        "融资 投资 新品牌 崛起 2026",
        "trending viral 2026 latest news",
        "社交 媒体 热议 走红 2026",
    ],
    "yiwu": [
        "义乌 工厂 生产 出货 2026",
        "1688 批发 价格 热销 近期",
        "义乌 国际商贸城 档口 商户 2026",
        "义乌 产业带 供应链 成本 利润",
        "义乌 跨境 外贸 出口 卖家 2026",
        "澄海 玩具 工厂 生产 出货",
        "产业带 制造 产能 代工",
    ],
    "history": [
        "发展历程 起源 演变 阶段",
        "关键节点 里程碑 转折 事件",
        "历史 回顾 变迁 进化",
        "history origin evolution development",
        "起源 什么时候 最早 发明",
    ],
    "competition": [
        "头部品牌 排行 十大 竞争 2026",
        "品牌 对比 优缺点 模式 利润",
        "供应链 上游 下游 成本 价格",
        "厂家 代工 OEM ODM 工厂",
        "毛利率 净利率 成本结构 赚钱",
        "top brands ranking 2026 best sellers",
        "竞争 格局 梯队 差异化",
    ],
    "supply_chain": [
        "产业带 集散地 生产基地 在哪里",
        "工厂 产能 代工 OEM 供应商 厂家",
        "原材料 核心零部件 上游 供应链",
        "批发价 1688 价格 成本 出厂价",
        "制造工艺 生产线 设备 模具 技术",
        "国内销售 渠道 代理 分销 零售",
        "供应链 上游 中游 下游 分布",
        "产区 产地 制造 集聚 在哪里",
    ],
    "trends": [
        "2026 2027 趋势 方向 预测",
        "AI 智能化 技术 创新 应用 案例",
        "合规 新国标 认证 检测 政策",
        "3D打印 数字化 升级 改造",
        "消费 趋势 变化 Z世代 需求",
        "future trends 2026 2027 forecast",
        "材料 工艺 创新 升级",
    ],
}

# ── 深度研究专用搜索维度（更聚焦、更精准）──
# ── 深度研究：每章独立搜索 + 系统提示 ──
DEEP_CITE = "引用数据时必须用 [↗](URL) 格式标注来源，禁止编造链接。研究材料中每条数据都有真实URL，直接复制使用。全章来源标注不少于10处，均匀分布，不集中堆在开头。"

DEEP_CHAPTERS = [
    {
        "title": "竞争与格局",
        "system": f"你正在撰写一份行业深度报告的第二章节。这份报告共七章，你只写第二章。开头不要写'第二章'这个编号，直接从内容开始。语气和风格与整份报告保持一致。{DEEP_CITE} 基于研究材料，分析当前行业竞争态势：哪些参与者在增长、哪些在收缩，市场份额在向谁集中。不少于2000字。",
        "dims": ["winners", "competition"],
    },
    {
        "title": "成本与利润",
        "system": f"你正在撰写一份行业深度报告的第三章节。这份报告共七章，你只写第三章。开头不要写'第三章'这个编号，直接从内容开始。语气和风格与前两章保持一致。{DEEP_CITE} 基于研究材料，梳理这个行业的价值分布：从原材料到消费者各环节的成本利润情况，产业集聚地，近年变化。不少于2000字。",
        "dims": ["profit", "supply_chain"],
    },
    {
        "title": "变化与驱动",
        "system": f"你正在撰写一份行业深度报告的第四章节。这份报告共七章，你只写第四章。开头不要写'第四章'这个编号，直接从内容开始。语气和风格与前三章保持一致。{DEEP_CITE} 基于研究材料，分析正在推动行业变化的因素：技术、政策、消费习惯、渠道等。说明现状和可能走向。不少于2000字。",
        "dims": ["triggers", "trends"],
    },
    {
        "title": "机会与路径",
        "system": f"你正在撰写一份行业深度报告的第五章节。这份报告共七章，你只写第五章。开头不要写'第五章'这个编号，直接从内容开始。语气和风格与前四章保持一致。{DEEP_CITE} 基于研究材料，梳理不同背景的参与者可能的机会。不同类型各自有什么优势和约束，选择什么路径更匹配自身条件。不少于2000字。",
        "dims": ["winners", "profit"],
    },
    {
        "title": "风险与不确定",
        "system": f"你正在撰写一份行业深度报告的第六章节。这份报告共七章，你只写第六章。开头不要写'第六章'这个编号，直接从内容开始。语气和风格与前五章保持一致。{DEEP_CITE} 基于研究材料，列出这个行业当前面临的主要风险和不确定性。每个风险说明已有先例或信号。不少于1500字。",
        "dims": ["risk", "triggers"],
    },
    {
        "title": "产业带与供应链",
        "system": f"你正在撰写一份行业深度报告的第七章节。这份报告共七章，你只写第七章。开头不要写'第七章'这个编号，直接从内容开始。{DEEP_CITE} 分两部分写。第一部分讲供应链全貌：上游在哪里谁在做，中游集中在哪些产业带（从搜索结果找真实地名），下游走什么渠道。第二部分站在义乌从业者的角度分析：如果已经在做这个品类，供应链上有什么变化需要注意；如果准备入局，门槛在哪、切入点在哪。不预设读者做跨境还是做国内，工厂还是贸易还是电商，都覆盖到。不少于2500字。",
        "dims": ["supply_chain", "yiwu", "profit"],
    },
]

DEEP_DIMENSIONS = {
    "winners": [
        "头部品牌 排行 十大 排名 2026",
        "融资 投资 新品牌 崛起 独角兽",
        "销量冠军 爆款 热卖 排行榜",
        "关店 倒闭 亏损 裁员 2026",
        "top brands market share revenue 2026",
    ],
    "profit": [
        "成本 批发价 出厂价 1688 价格",
        "毛利率 净利率 利润 赚钱 盈利",
        "物流 仓储 运费 履约成本",
        "cost breakdown profit margin pricing",
    ],
    "triggers": [
        "政策 新规 监管 标准 2026",
        "原材料 涨价 跌价 价格波动",
        "技术 突破 创新 颠覆 新工艺",
        "大厂 入局 跨界 竞争 冲击",
        "industry disruption regulation 2026",
    ],
    "risk": [
        "召回 投诉 维权 官司 纠纷",
        "专利 侵权 抄袭 知识产权",
        "泡沫 崩盘 过剩 库存 滞销",
        "安全 事故 质量问题 曝光",
    ],
}

# 通用高质量源：对所有行业追加 site: 搜索，不按行业分标签
QUALITY_SITES = [
    "36kr.com", "huxiu.com", "zhihu.com", "jiemian.com",
    "thepaper.cn", "sohu.com", "163.com",
    "baijing.cn", "cifnews.com", "amz123.com",
    "yiwugo.com", "1688.com",
]

SKIP_DOMAINS = {
    "fxbaogao.com", "baogao.com", "doc.51baogao.cn", "51baogao.cn",
    "chinabgao.com", "chyxx.com", "docin.com", "doc88.com",
    "max.book118.com", "book118.com", "doc88.com",
    "service-now.com", "vimeo.com", "guides.lib.berkeley.edu",
    "uscanyin.com", "moomoo.com", "10jqka.com.cn",
    "ifanswang.com", "toutiao.com", "instagram.com",
}

# ── 搜索引擎 ─────────────────────────────────────────────
def search_web(query: str, max_results: int = 5) -> list[dict]:
    """多引擎搜索：DuckDuckGo + Bing，合并去重后再返回。"""
    results: list[dict] = []
    seen_urls: set[str] = set()

    def add_entries(entries: list[dict]) -> None:
        for item in entries:
            url = str(item.get("url", "")).strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append(item)

    # 引擎1: DDGS（新版库），单次不超8秒
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            add_entries([
                {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
                for r in ddgs.text(query, max_results=max_results)
            ])
    except Exception:
        pass

    # 引擎2: Bing，中文查询的补充来源
    try:
        response = httpx.get(
            "https://www.bing.com/search",
            params={"q": query, "setlang": "zh-hans", "cc": "cn", "count": max_results * 2},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=8,
            follow_redirects=True,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for item in soup.select("li.b_algo")[: max_results * 2]:
            link = item.select_one("h2 a")
            if not link:
                continue
            caption = item.select_one(".b_caption p")
            add_entries([{
                "title": link.get_text(" ", strip=True),
                "url": link.get("href", ""),
                "snippet": caption.get_text(" ", strip=True) if caption else "",
            }])
    except Exception:
        pass

    return results[:max_results] if results else [{"title": "搜索受限", "url": "", "snippet": "请稍后重试"}]


def search_images(query: str, max_results: int = 2) -> list[dict]:
    """为工厂接单研判页取行业实景配图；失败时不影响研究正文。"""
    try:
        from ddgs import DDGS
        results = []
        with DDGS() as ddgs:
            for item in ddgs.images(query, max_results=max_results, safesearch="moderate"):
                image_url = str(item.get("thumbnail") or item.get("image") or "").strip()
                if image_url.startswith(("http://", "https://")):
                    results.append({
                        "url": image_url,
                        "title": str(item.get("title") or "行业参考图"),
                        "source": str(item.get("url") or ""),
                    })
        return results
    except Exception:
        return []


def _save_readable_images(safe: str, images: list[dict], limit: int = 4) -> list[dict]:
    """将搜索到的小图保存到本地，避免独立页面因外链失效而留白。"""
    asset_dir = PROJECT_ROOT / "output" / "readable" / "assets" / safe
    asset_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for index, image in enumerate(images[:limit], start=1):
        try:
            response = httpx.get(image["url"], timeout=8, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; trend_grab/2.0)"})
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("image/") or len(response.content) > 5 * 1024 * 1024:
                continue
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(content_type.split(";")[0], ".jpg")
            filename = f"scene-{index}{suffix}"
            (asset_dir / filename).write_bytes(response.content)
            saved.append({
                "url": f"/readable-assets/{safe}/{filename}",
                "title": image["title"],
                "source": image["source"],
            })
        except Exception:
            continue
    return saved


READABLE_SECTION_IMAGE_QUERIES = {
    "overview": "行业 产品 工厂 市场",
    "history": "行业 发展 历史 里程碑",
    "hot_topics": "爆款 热销 订单 案例",
    "competition": "品牌 工厂 代工 供应链",
    "players": "头部品牌 店铺 爆款 卖家",
    "cost_profit": "成本 价格 利润 报价",
    "supply_chain": "产业带 供应链 工厂 产地",
    "channels": "渠道 平台 采购 批发",
    "barriers": "认证 检测 合规 质量",
    "trends": "新品 趋势 工艺 材料",
    "risks": "风险 质量问题 投诉",
    "next": "展会 工厂 订单 客户",
}


def _image_relevant(image: dict, industry: str, query: str) -> bool:
    """过滤明显无关的图片：标题、出处页或图片 URL 至少要和行业或查询词相关。"""
    text = " ".join([
        str(image.get("title", "")),
        str(image.get("source", "")),
        str(image.get("url", "")),
    ]).lower()
    industry_compact = re.sub(r"\s+", "", industry.lower())
    if len(industry_compact) >= 2 and industry_compact in text:
        return True
    for token in re.findall(r"[a-z0-9]{3,}", query.lower()):
        if token in text:
            return True
    for segment in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(segment) >= 2 and segment in text:
            return True
    return False


def _attach_readable_section_images(content: dict, search_images, save_images) -> dict:
    """生成内容后按板块补图：每节一张，分散到页面而不是堆在顶部。"""
    safe = re.sub(r'[\\/:*?"<>|]', '_', content["industry"])[:80]
    sections = content.get("sections", [])

    def load_one(section):
        try:
            queries = section.get("image_queries") or [READABLE_SECTION_IMAGE_QUERIES.get(section.get("id"), "工厂 产品")]
            candidates = []
            seen_urls = set()
            for query in queries[:2]:
                search_query = f"{content['industry']} {section.get('title', '')} {query}"
                for image in search_images(search_query, 4):
                    url = image.get("url", "")
                    if url and url not in seen_urls and _image_relevant(image, content["industry"], search_query):
                        seen_urls.add(url)
                        candidates.append(image)
            return section, save_images(f"{safe}_{section.get('id', 'section')}", candidates, 1)
        except Exception:
            return section, []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(load_one, section) for section in sections]
        for future in as_completed(futures):
            section, saved = future.result()
            section["images"] = saved
    return content


def search_news(query: str, max_results: int = 5) -> list[dict]:
    """新闻搜索，获取最新信息"""
    try:
        from ddgs import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("body", ""),
                })
        if results:
            return results
    except Exception:
        pass
    try:
        from duckduckgo_search import DDGS as OldDDGS
        with OldDDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("body", ""),
                })
    except Exception:
        pass
    return results


def fetch_content(url: str, timeout: int = 8) -> str:
    """抓取页面文本"""
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                       headers={"User-Agent": "Mozilla/5.0 (compatible; trend_grab/2.0)"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [ln.strip() for ln in text.split("\n") if len(ln.strip()) > 30]
        return "\n".join(lines[:200])
    except Exception:
        return ""


# ── 报告生成 ─────────────────────────────────────────────

WHITE_PAPER_SYSTEM = """你是一个长期跟踪消费品行业的研究者。为义乌产业带的从业者撰写行业白皮书。

## 来源标注（最高优先级，每条数据必标）

研究材料中每条数据都附带了真实 URL。引用数据时直接复制那个 URL，在句尾写 `[↗](URL)`。禁止编造链接。每个数字、价格、案例后都要跟来源链接。全篇每个章节都必须有来源标注。

## 核心要求：不能飘在空中

这份白皮书的读者是义乌工厂老板、档口经营者、跨境卖家。写的时候尽量落地：研究材料里有供应链细节就展开（工厂模式、成本结构、渠道玩法），有价格就写具体数字，有工艺就讲清楚。材料里没有的不要硬编。如果一段话全是宏观判断没有具体信息，删掉或重写。

## 写作铁律（首条最重要）
- **必须**用 ### 小标题拆分每章为3-5个小节。这是硬性要求，不是建议。每章看到三个以上 ### 才是合格。小标题写具体判断句如"美国占出口六成但利润被品牌方抽走"，不写"市场概况""竞争分析"等空洞词。
- 禁止Markdown加粗（**），用 ### 做视觉分层。
- 禁止用破折号（——）。
- 删掉"随着时代发展""这一趋势值得关注""这说明""因此"等空话和过度总结句。
- 缩小叙事半径，段落长短交替。
- 品牌化是方向之一不是主线。语气平实。不确定的地方说"还需要验证"。

## 结构原则
- **一事实一位置**：同一个公司背景、产品参数、市场数据，只在最相关的章节完整讲述一次。其他章节需要引用时用"如前所述"一带而过。但同一事实的不同角度分析（竞争视角、成本视角、风险视角）可以在不同章节展开。
- **章节挂钩**：每章结尾用一两句话点明"这引出了什么问题"，下一章开头接住。让七章形成因果链而非各自独立。
- **观点先行**：每个小节开头放一个可直接引用的判断句，后续数据案例都是对它的支撑。速读只看首句就能获取全文骨架。
- **结尾收束**：最后一章不引入新话题，而是把前六章的线索汇聚到一个具象落脚点（如对某类从业者的具体参考），让读者感受到前面的分析在此落地。

## 报告结构（六章）

第一章「市场全景」：规模与增速（多口径对比）、子品类增速分化、消费者结构变化。如果研究材料中有供应链和产业地理信息就展开，没有不硬写。

第二章「近期热点」：过去3-6个月的具体案例。研究材料里有什么热点就写什么，不强制某个平台或渠道。如果材料中缺乏近期信息，诚实说明时间范围，不拿旧闻充数。

第三章「行业发展历程与关键节点」：从品类起源到当前格局的时间线，按阶段划分，标注关键事件。写出品类概念的演变：同一名字在不同阶段对应的工艺和形态变化，以及驱动演变的因素。材料里有义乌相关历史才提，没有不硬编。附关键节点表。

第四章「竞争格局」：参与者分层、价值链利润分布、商业模式分化、平台渠道分化。材料里有什么数据就写什么，不预设具体平台和渠道。

第五章「趋势展望」：基于研究材料，提炼本行业未来1-3年真正在发生的趋势，不要套用固定模板。有什么趋势写什么趋势，研究材料指向什么就分析什么。每个趋势落到实际影响，不写空话。

第六章「策略启示」：基于前五章的分析，提炼3-5条对从业者有实际参考价值的判断。每条包含三个要素：（1）基于什么数据和变化趋势、（2）意味着什么机会或风险、（3）不同类型的参与者分别面临什么选择。不做品牌/白牌/代工的优劣评判，但要说清楚不同路径对应的资源要求、时间窗口和风险特征。文末保留2-3个真正开放的问题，留给读者自己判断。

篇幅要求：正文 5000-6000 字。六章结构保持，但每章精炼，点到为止。不要展开不必要的细节。案例选 1-2 个最核心的即可。用 Markdown 格式输出。"""


class GenerateRequest(BaseModel):
    industry: str
    code: str = ""


# ── 授权码系统 ─────────────────────────────────────────────

AUTH_FILE = PROJECT_ROOT / "output" / "auth_codes.json"
ADMIN_CODE = "trendgrab2026"


_auth_lock = threading.Lock()


def _load_auth() -> dict:
    if AUTH_FILE.exists():
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    return {}


def _save_auth(data: dict):
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _auth_lock:
        AUTH_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _init_auth():
    data = _load_auth()
    if "admin" not in data:
        data["admin"] = {"quick_left": -1, "deep_left": -1, "reports": []}
    if len(data) <= 1:  # only admin, generate 10 codes
        for i in range(10):
            c = f"HM{uuid.uuid4().hex[:8].upper()}"
            data[c] = {"quick_left": 10, "deep_left": 3, "reports": []}
        _save_auth(data)
    return data


def _check_code(code: str, mode: str = "quick") -> dict | None:
    """验证授权码，返回码信息或 None"""
    data = _load_auth()
    if code == ADMIN_CODE:
        return data.get("admin", {"quick_left": -1, "deep_left": -1, "reports": []})
    c = data.get(code)
    if not c:
        return None
    key = f"{mode}_left"
    if c.get(key, 0) == 0:
        return None  # 次数用完
    return c


def _use_code(code: str, mode: str, industry: str, path: str):
    """扣减次数，记录报告"""
    data = _load_auth()
    if code == ADMIN_CODE:
        c = data.setdefault("admin", {"quick_left": -1, "deep_left": -1, "reports": []})
    else:
        c = data.get(code)
    if c and c.get(f"{mode}_left", 0) > 0:
        c[f"{mode}_left"] -= 1
    if c is not None:
        c["reports"].append({"industry": industry, "mode": mode, "time": datetime.now().isoformat()})
    _save_auth(data)


@app.get("/api/auth/status")
async def auth_status(code: str = ""):
    data = _init_auth()
    if code == ADMIN_CODE:
        c = data.get("admin", {})
        return {"valid": True, "admin": True, "quick_left": "无限", "deep_left": "无限"}
    c = data.get(code)
    if not c:
        return {"valid": False}
    return {"valid": True, "admin": False, "quick_left": c["quick_left"], "deep_left": c["deep_left"]}


@app.get("/api/auth/my-reports")
async def auth_my_reports(code: str = ""):
    data = _load_auth()
    if code == ADMIN_CODE:
        c = data.get("admin", {})
    else:
        c = data.get(code)
    if not c:
        raise HTTPException(403, "无效授权码")
    results = []
    out = PROJECT_ROOT / "output"
    # 扫描服务器上所有报告
    all_reports = {}
    for f in out.glob("report_*.md"):
        all_reports[f.stem.replace("report_", "")] = ("quick", f)
    deep_dir = out / "deep"
    if deep_dir.exists():
        for d in deep_dir.iterdir():
            if d.is_dir():
                for f in d.glob("*.md"):
                    all_reports[d.name] = ("deep", f)
    # 只返回该授权码记录中实际存在的报告
    for r in c.get("reports", []):
        industry = r.get("industry", "")
        safe = re.sub(r'[\\/:*?"<>|]', '_', industry)[:80]
        if safe in all_reports:
            mode, path = all_reports[safe]
            results.append({"industry": industry, "mode": mode, "time": r.get("time",""), "content": path.read_text(encoding="utf-8")})
    return results


@app.post("/api/auth/generate-code")
async def auth_generate_code(quick: int = 10, deep: int = 3, token: str = ""):
    if token != ADMIN_CODE:
        raise HTTPException(403, "仅管理员可操作")
    data = _load_auth()
    c = f"HM{uuid.uuid4().hex[:8].upper()}"
    data[c] = {"quick_left": quick, "deep_left": deep, "reports": []}
    _save_auth(data)
    return {"code": c, "quick_left": quick, "deep_left": deep}


@app.get("/api/auth/list-codes")
async def auth_list_codes(token: str = ""):
    if token != ADMIN_CODE:
        raise HTTPException(403, "仅管理员可操作")
    data = _load_auth()
    result = {}
    for c, info in data.items():
        result[c] = {"quick_left": info["quick_left"], "deep_left": info["deep_left"], "report_count": len(info.get("reports", []))}
    return result


class ReportStatus:
    def __init__(self):
        self._status = {}
    def set(self, rid: str, key: str, val):
        if rid not in self._status:
            self._status[rid] = {}
        self._status[rid][key] = val
    def get(self, rid: str):
        return self._status.get(rid, {})

statuses = ReportStatus()


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    industry = req.industry.strip()
    if not industry:
        raise HTTPException(400, "请输入行业名称")
    if not client:
        raise HTTPException(500, "LLM 未配置。请在 .env 中设置 LLM_API_KEY 和 LLM_BASE_URL。")

    # 检查快速研究缓存
    c = _check_code(req.code, "quick")
    if not c:
        raise HTTPException(403, "授权码无效或次数已用完")

    report = _gen_report(industry, "quick")
    safe = re.sub(r'[\\/:*?"<>|]', '_', industry)[:80]
    report_path = PROJECT_ROOT / "output" / f"report_{safe}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    _use_code(req.code, "quick", industry, str(report_path))
    ql = c.get("quick_left", 0)
    return {"report": report, "path": str(report_path), "industry": industry, "quick_left": ql if ql < 0 else ql - 1}


@app.post("/api/generate-readable")
async def generate_readable(req: GenerateRequest):
    """生成工厂接单研判页；不改变原有白皮书接口与文件。"""
    industry = req.industry.strip()
    if not industry:
        raise HTTPException(400, "请输入行业名称")
    if not client:
        raise HTTPException(500, "LLM 未配置。请在 .env 中设置 LLM_API_KEY 和 LLM_BASE_URL。")

    c = _check_code(req.code, "quick")
    if not c:
        raise HTTPException(403, "授权码无效或次数已用完")

    try:
        deep_path = _ensure_deep_report(industry)
        deep_text = deep_path.read_text(encoding="utf-8")
        content = generate_from_deep_report(client, LLM_MODEL, industry, deep_text, search_web)
        content = _attach_readable_section_images(content, search_images, _save_readable_images)
        deep_safe = re.sub(r"^report_", "", deep_path.stem)
        content["deep_report_url"] = f"/deep-report/{quote(deep_safe)}"
        readable_html = render_readable_html(content)
    except ValueError as e:
        raise HTTPException(502, f"工厂接单研判页内容格式异常，请重试：{e}")
    except Exception as e:
        raise HTTPException(500, f"工厂接单研判页生成失败：{e}")

    safe = re.sub(r'[\\/:*?"<>|]', '_', industry)[:80]
    readable_dir = PROJECT_ROOT / "output" / "readable"
    readable_dir.mkdir(parents=True, exist_ok=True)
    content_path = readable_dir / f"{safe}.json"
    html_path = readable_dir / f"{safe}.html"
    content_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(readable_html, encoding="utf-8")
    _use_code(req.code, "quick", industry, str(html_path))
    ql = c.get("quick_left", 0)
    return {
        "industry": industry,
        "path": str(html_path),
        "url": f"/readable/{safe}",
        "outline": {
            "headline": content["headline"],
            "decision": content["decision"],
            "signals": content["signals"],
            "sections": [
                {"eyebrow": section["eyebrow"], "title": section["title"]}
                for section in content["sections"]
            ],
        },
        "quick_left": ql if ql < 0 else ql - 1,
    }


@app.get("/api/reports")
async def list_reports():
    output_dir = PROJECT_ROOT / "output"
    if not output_dir.exists():
        return []
    reports = []
    for f in sorted(output_dir.glob("report_*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        reports.append({"name": f.stem.replace("report_", ""), "path": str(f), "size": f.stat().st_size})
    return reports


@app.get("/api/report/{name}")
async def get_report(name: str):
    path = PROJECT_ROOT / "output" / f"report_{name}.md"
    if not path.exists():
        raise HTTPException(404, "报告不存在")
    return {"name": name, "content": path.read_text(encoding="utf-8")}


@app.get("/api/download/{name}")
async def download_report(name: str):
    path = PROJECT_ROOT / "output" / f"report_{name}.md"
    if not path.exists():
        raise HTTPException(404, "报告不存在")
    return FileResponse(path, filename=f"{name}.md", media_type="text/markdown")


# ── 深度研究 ─────────────────────────────────────────────

DEEP_CACHE = {}  # 内存缓存

def _find_cached(industry: str, mode: str = "quick") -> str | None:
    """扫描缓存。mode='quick' 只查快速研究，mode='deep' 只查深度研究"""
    safe = re.sub(r'[\\/:*?"<>|]', '_', industry)[:80][:50]

    if mode == "quick":
        p = PROJECT_ROOT / "output" / f"report_{safe}.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
        return None

    # mode == "deep": 只查 output/deep/{行业}/{行业}.md
    deep_path = PROJECT_ROOT / "output" / "deep" / safe / f"{safe}.md"
    if deep_path.exists():
        return deep_path.read_text(encoding="utf-8")
    return None


def _fetch_trade_data(industry: str) -> str:
    """获取行业的WITS贸易数据（AI发散品类→搜HS编码→多个编码查WITS）"""
    _t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": f"「{industry}」属于什么产品类别？列出3-5个相关的、可用于海关查询的具体品类名称。每行一个品类名，不要写HS编码。"}],
            temperature=0.3, max_tokens=100,
        )
        categories = [c.strip() for c in resp.choices[0].message.content.strip().split("\n") if c.strip()]
        categories.insert(0, industry)
        print(f"[TRADE] {industry} | cats={categories[:3]} | {time.time()-_t0:.1f}s")

        all_codes = set()
        for cat in categories[:2]:
            results = search_web(f"{cat} HS编码 海关编码", max_results=2)
            for r in results:
                codes = re.findall(r'\b(\d{6,10})\b', r['title'] + r['snippet'])
                all_codes.update(c[:6] for c in codes)
        print(f"[TRADE] {industry} | codes={list(all_codes)[:3]} | {time.time()-_t0:.1f}s")

        if not all_codes:
            print(f"[TRADE] {industry} | NO CODES | {time.time()-_t0:.1f}s")
            return ""

        trade_text = ""
        for hs in list(all_codes)[:2]:
            got_total = False
            for year in [2024, 2023]:
                url = f"https://wits.worldbank.org/trade/comtrade/en/country/ALL/year/{year}/tradeflow/Exports/partner/WLD/product/{hs}"
                r = httpx.get(url, timeout=3, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    import re as re2
                    china_match = re2.search(r'China.{0,300}?([\d,.]+)', r.text)
                    kg_match = re2.search(r'China.*?(\d[\d,.]*)\s*Kg', r.text)
                    if china_match:
                        if not got_total:
                            wits_url = f"https://wits.worldbank.org/trade/comtrade/en/country/ALL/year/2024/tradeflow/Exports/partner/WLD/product/{hs}"
                            trade_text += f"\n【贸易数据 - HS{hs} [↗]({wits_url})】\n"
                            got_total = True
                        val = china_match.group(1).replace(',', '')
                        kg = kg_match.group(1).replace(',', '') if kg_match else '?'
                        trade_text += f"{year}年: 中国出口${float(val)*1000:,.0f} ({kg}kg)\n"  # WITS单位千美元
            if got_total and year == 2024:
                dest_url = f"https://wits.worldbank.org/trade/comtrade/en/country/CHN/year/2024/tradeflow/Exports/partner/ALL/product/{hs}"
                dr = httpx.get(dest_url, timeout=3, headers={"User-Agent": "Mozilla/5.0"})
                if dr.status_code == 200:
                    import re as re2
                    pairs = re2.findall(r'([\w\s]+)\s*\(\$([\d,.]+)K,\s*([\d,.]+)\s*Kg\)', dr.text)
                    if pairs and len(pairs) >= 2:
                        dests = [f"{n.strip()} ${float(v.replace(',','')):,.0f}K" for n,v,_ in pairs[:5]]
                        trade_text += f"出口目的地TOP5: {', '.join(dests)} [↗]({dest_url})\n"
            if got_total:
                break
        print(f"[TRADE] {industry} | done {len(trade_text)} chars | {time.time()-_t0:.1f}s")
        return trade_text if trade_text else ""
    except Exception as e:
        print(f"[TRADE] {industry} | ERROR {e} | {time.time()-_t0:.1f}s")
        return ""
    return ""


def _gen_report(industry: str, mode: str = "quick") -> str:
    """生成单个行业的报告文本"""
    _t0 = time.time()
    _log_file = PROJECT_ROOT / "output" / "timing.log"
    def _log(step, detail=""):
        elapsed = time.time() - _t0
        msg = f"{datetime.now().strftime('%H:%M:%S')} | {industry} | {step} | {elapsed:.1f}s | {detail}\n"
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(msg)

    cached = _find_cached(industry, mode)
    if cached:
        _log("cache_hit")
        return cached

    # 获取贸易数据
    _t1 = time.time()
    trade_info = _fetch_trade_data(industry)
    _log("trade_data", f"took {time.time()-_t1:.1f}s, got {len(trade_info)} chars")

    # 收集搜索结果
    all_snippets = []
    seen_urls = set()
    # 控制搜索总时间在180秒内
    search_deadline = time.time() + 180
    for dim_key, dim_queries in DIMENSIONS.items():
        if time.time() > search_deadline:
            break
        for q in dim_queries[:3]:  # 每个维度取前3个查询词
            if time.time() > search_deadline:
                break
            results = search_web(f"{industry} {q}", max_results=4)
            for r in results:
                url = r.get('url', '')
                if url and url not in seen_urls:
                    domain = re.search(r'https?://([^/]+)', url)
                    if domain and any(d in domain.group(1) for d in SKIP_DOMAINS):
                        continue
                    seen_urls.add(url)
                    all_snippets.append(f"[{dim_key}] {r['title']}\n{r['snippet']}\n{url}")

    for site in QUALITY_SITES[:3]:  # 只搜3个高质量站
        if time.time() > search_deadline:
            break
        results = search_web(f"site:{site} {industry}", max_results=3)
        for r in results:
            url = r.get('url', '')
            if url and url not in seen_urls:
                domain = re.search(r'https?://([^/]+)', url)
                if domain and any(d in domain.group(1) for d in SKIP_DOMAINS):
                    continue
                seen_urls.add(url)
                all_snippets.append(f"[{dim_key}] {r['title']}\n{r['snippet']}\n{url}")

    research_text = "\n\n".join(all_snippets[:200])
    _log("search_done", f"{len(all_snippets)} snippets, {len(seen_urls)} unique")
    if trade_info:
        research_text = trade_info + "\n\n---\n\n" + research_text
    _t2 = time.time()
    deep_text = ""
    for url in list(seen_urls)[:5]:
        c = fetch_content(url)
        if c:
            deep_text += f"\n{url}\n{c[:3000]}\n---\n"
    _log("deep_read", f"took {time.time()-_t2:.1f}s")

    _t3 = time.time()
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": WHITE_PAPER_SYSTEM},
            {"role": "user", "content": f"请为「{industry}」撰写行业白皮书。研究材料中每条数据都附带了真实 URL，引用时直接复制 URL 写成 [↗](URL) 格式，禁止编造链接。\n\n研究材料：\n{research_text[:10000]}\n\n深度页面：\n{deep_text[:8000]}"},
        ],
        temperature=0.7, max_tokens=8000,
    )
    report = resp.choices[0].message.content
    _log("llm_done", f"took {time.time()-_t3:.1f}s, output {len(report)} chars")
    return report


def _extract_keywords(report: str, industry: str) -> list[str]:
    """从报告中提取 3 个关联子关键词"""
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": f"从以下关于「{industry}」的行业报告中，提取3个报告中重点提及的具体子品类、产品或品牌名称。不要选太宽泛的词，要选有具体案例和数据支撑的。只输出关键词，每行一个。\n\n报告摘要:\n{report[:4000]}"}],
        temperature=0.3, max_tokens=200,
    )
    keywords = [k.strip() for k in resp.choices[0].message.content.strip().split("\n") if k.strip()]
    keywords = [k.lstrip("0123456789. -•·") for k in keywords]
    return keywords[:3]


def _get_parent(industry: str) -> str:
    """获取上级品类"""
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": f"「{industry}」的上级行业大类是什么？只输出1个词语，不要解释。"}],
        temperature=0.3, max_tokens=20,
    )
    return resp.choices[0].message.content.strip()


def _gen_deep(industry: str) -> str:
    """深度研究专用：使用更聚焦的搜索维度"""
    all_snippets = []
    seen_urls = set()
    # 合并深度维度 + 产业链维度
    all_dims = {**DEEP_DIMENSIONS, "supply_chain": DIMENSIONS.get("supply_chain", [])}
    for dim_key, dim_queries in all_dims.items():
        for q in dim_queries:
            results = search_web(f"{industry} {q}", max_results=5)
            for r in results:
                url = r.get('url', '')
                if url and url not in seen_urls:
                    domain = re.search(r'https?://([^/]+)', url)
                    if domain and any(d in domain.group(1) for d in SKIP_DOMAINS):
                        continue
                    seen_urls.add(url)
                    all_snippets.append(f"[{dim_key}] {r['title']}\n{r['snippet']}\n{url}")
    for site in QUALITY_SITES:
        results = search_web(f"site:{site} {industry}", max_results=3)
        for r in results:
            url = r.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_snippets.append(f"[{dim_key}] {r['title']}\n{r['snippet']}\n{url}")
    research_text = "\n\n".join(all_snippets[:150])
    deep_text = ""
    for url in list(seen_urls)[:8]:
        c = fetch_content(url)
        if c:
            deep_text += f"\n{url}\n{c[:3000]}\n---\n"

    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": WHITE_PAPER_SYSTEM},
            {"role": "user", "content": f"请为「{industry}」撰写一份深度分析报告，重点关注竞争格局、利润结构、供应链、关键变量和风险。引用时用 [↗](URL) 格式。\n\n研究材料：\n{research_text[:12000]}\n\n深度页面：\n{deep_text[:8000]}"},
        ],
        temperature=0.7, max_tokens=10000,
    )
    return resp.choices[0].message.content


def _run_deep_research(industry: str, safe: str, merged_path: Path, on_progress=None, on_keywords=None) -> str:
    """深度研究主流程：主报告 + 并行章节 + 概览 + 最终编辑。接口和工厂接单版共用。"""
    output_dir = merged_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    def progress(phase: str, value: int) -> None:
        if on_progress:
            on_progress(phase, value)

    progress("main", 0)
    main_report = _gen_report(industry, "quick")
    (PROJECT_ROOT / "output" / f"report_{safe}.md").write_text(main_report, encoding="utf-8")
    trade_extra = _fetch_trade_data(industry)
    sub_keywords = _extract_keywords(main_report, industry)
    parent_kw = _get_parent(industry)
    all_kws = [industry] + sub_keywords + [parent_kw]
    if on_keywords:
        on_keywords(all_kws)
    progress("main", 15)

    chapters = [None] * len(DEEP_CHAPTERS)
    all_dims = {
        **DEEP_DIMENSIONS,
        "supply_chain": DIMENSIONS.get("supply_chain", []),
        "trends": DIMENSIONS.get("trends", []),
        "yiwu": DIMENSIONS.get("yiwu", []),
    }

    def _gen_chapter(idx: int):
        ch = DEEP_CHAPTERS[idx]
        snippets = []
        seen = set()
        for dim_name in ch["dims"]:
            for q in all_dims.get(dim_name, []):
                for kw in all_kws[:3]:
                    results = search_web(f"{kw} {q}", max_results=4)
                    for r in results:
                        url = r.get('url', '')
                        if url and url not in seen:
                            domain = re.search(r'https?://([^/]+)', url)
                            if domain and any(d in domain.group(1) for d in SKIP_DOMAINS):
                                continue
                            seen.add(url)
                            snippets.append(f"{r['title']}\n{r['snippet']}\n{url}")
        deep = ""
        for url in list(seen)[:5]:
            c = fetch_content(url)
            if c:
                deep += f"\n{url}\n{c[:2500]}\n---\n"
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": ch["system"]},
                {"role": "user", "content": "为「" + industry + "」撰写「" + ch['title'] + "」章节。\n\n" + (trade_extra or "") + "\n\n研究材料：\n" + "\n\n".join(snippets)[:8000] + "\n\n深度页面：\n" + deep[:6000]},
            ],
            temperature=0.7,
            max_tokens=4000,
        )
        chapters[idx] = resp.choices[0].message.content

    with ThreadPoolExecutor(max_workers=len(DEEP_CHAPTERS)) as pool:
        futures = [pool.submit(_gen_chapter, i) for i in range(len(DEEP_CHAPTERS))]
        completed = 0
        for future in as_completed(futures):
            future.result()
            completed += 1
            progress("chapters", 15 + int(completed / len(DEEP_CHAPTERS) * 60))

    progress("stitch", 80)
    intro_resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": f"为以下行业报告写一篇800-1000字的行业概览。用4-5段文字，不使用###小标题，直接分段。内容覆盖：全球市场规模与增速（多口径对比）、中国在全球产业链的位置（制造份额、出口比例）、消费者结构变化、主要子品类或价格带分化、产业地理分布。每个数据点保留[↗](URL)引用。不用加粗。语气专业平实，不是聊天，是行业分析。\n\n{main_report[:8000]}"}],
        temperature=0.5,
        max_tokens=800,
    )
    overview = intro_resp.choices[0].message.content

    report = f"# {industry} 深度策略白皮书\n\n"
    report += f"> {datetime.now().strftime('%Y年%m月')} | 基于行业全景和{len(all_kws)}个关联品类的交叉分析\n\n"
    report += f"## 一、行业概览\n\n{overview.strip()}\n\n---\n\n"
    nums = ["二", "三", "四", "五", "六", "七"][:len(DEEP_CHAPTERS)]
    for i, ch in enumerate(chapters):
        if ch:
            ch_clean = re.sub(r'^#.*$', '', ch, flags=re.MULTILINE).strip()
            report += f"## {nums[i]}、{DEEP_CHAPTERS[i]['title']}\n\n{ch_clean}\n\n---\n\n"
    report += "\n\n*第一章为行业概览，第二至六章逐章独立搜索生成。数据来源以 [↗](URL) 格式标注于各章节内。*"

    progress("edit", 90)
    edit_prompt = f"""你是资深商业编辑。对以下报告进行最终润色。

## 最优先任务：加小标题
每章必须有3-5个###小标题。这是硬性要求，不是建议。每章开头先检查有几个###，不到3个就补到3个。小标题写具体判断句如"医用级材质正在从加分项变成入场券"，不写"概述""背景""市场分析"等空洞词。

## 其他任务：
1. 去重：同一公司背景、产品参数、数据，只在一处完整讲述，后文用"如前所述"。
2. 挂钩：每章开头接上章结尾。
3. 观点前置：小节开头放判断句，数据和案例在后。

## 去AI味：
- 禁止Markdown加粗（**）和破折号（——）
- 禁止"首先其次然后最后""这说明""因此""随着时代发展""这一趋势值得关注"
- 缩小叙事半径，段落长短交替

不要写前言或说明，直接输出报告正文。[↗](URL)链接全部保留。"""
    edit_resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": f"{edit_prompt}\n\n{report[:30000]}"}],
        temperature=0.5,
        max_tokens=16000,
    )
    report = edit_resp.choices[0].message.content

    merged_path.write_text(report, encoding="utf-8")
    progress("done", 100)
    return report


RESEARCH_REPORT_TEMPLATE = """# {industry} 行业深度分析 | {date}

> 研究覆盖：行业概述 | 发展路径 | 近期热点 | 竞争格局 | 趋势预测

---

## 一、行业概述

### 1.1 行业定义与边界
[行业是什么，包含哪些子类/细分，上下游关系]

### 1.2 市场规模
- **全球市场**：[数据 + 来源]
- **中国市场**：[数据 + 来源]
- **增长率**：[CAGR 数据]

### 1.3 产业链结构
[每段的具体说明]

---

## 二、发展路径与关键节点

### 2.1 时间线
[时间 | 阶段 | 关键事件]

### 2.2 关键驱动力
- **技术驱动**：[具体技术突破]
- **消费驱动**：[消费者行为变化]
- **政策/资本驱动**：[政策利好或资本推动]

---

## 三、近期热点（近 3-6 个月）

### 3.1 热点一：[标题]
[事件描述 + 影响分析 + 来源链接]

### 3.2 热点二：[标题]
[同上]

### 3.3 热点三：[标题]
[同上]

---

## 四、竞争格局

### 4.1 头部玩家
[梯队 | 代表品牌/公司 | 核心优势 | 市场份额（估算）]

### 4.2 商业模式对比
[直销 vs 平台 vs 订阅 vs ...]

### 4.3 供应链分析
[核心供应链在哪里？成本结构？进入壁垒？]

---

## 五、趋势预测

### 5.1 短期趋势（6-12 个月）
1. [趋势 + 依据]
2. [趋势 + 依据]

### 5.2 中期趋势（1-3 年）
1. [趋势 + 依据]
2. [趋势 + 依据]

### 5.3 风险与不确定性
- [风险点]

---

## 数据来源
[所有引用的 URL，按出现顺序编号]
"""


def _research_queries(industry: str) -> dict:
    """scripts/research.py 同款 5 维度搜索关键词。"""
    return {
        "overview": [
            f"{industry} 行业报告 市场规模 2025 2026",
            f"{industry} 行业概述 产业链 定义",
            f"{industry} market size industry report 2025 2026",
            f"{industry} 市场规模 增长趋势",
        ],
        "history": [
            f"{industry} 发展历程 起源 历史",
            f"{industry} 关键节点 里程碑",
            f"{industry} history development timeline",
            f"{industry} 风口 兴起 爆发",
        ],
        "hot_topics": [
            f"{industry} 2026 最新 趋势 热点",
            f"{industry} 最新 新闻 动态",
            f"{industry} latest trends 2026",
            f"{industry} 融资 投融资 资本",
            f"{industry} 出海 跨境电商 TikTok",
        ],
        "competition": [
            f"{industry} 头部品牌 头部玩家 排行",
            f"{industry} 竞争格局 市场份额",
            f"{industry} top brands companies ranking",
            f"{industry} 供应链 工厂 代工",
            f"{industry} 商业模式 盈利模式",
        ],
        "trends": [
            f"{industry} 未来趋势 预测 2025 2026",
            f"{industry} 消费趋势 消费者偏好",
            f"{industry} future trends forecast prediction",
            f"{industry} AI 数字化 技术升级",
            f"{industry} 政策 监管 合规",
        ],
    }


def _is_template_report(text: str) -> bool:
    markers = ("[数据 + 来源]", "[行业是什么", "[趋势 + 依据]", "[同上]", "[所有引用的 URL")
    return any(marker in text for marker in markers)


def _run_research_style_deep_report(industry: str, safe: str, path: Path, on_progress=None) -> str:
    """scripts/research.py 架构的自动深度研究：5 维度搜索 → 模板化撰写 → report_*.md。"""
    def progress(phase: str, value: int) -> None:
        if on_progress:
            on_progress(phase, value)

    progress("search", 0)
    queries = _research_queries(industry)
    all_snippets = []
    seen_urls = set()
    for dim_key, dim_queries in queries.items():
        for q in dim_queries[:3]:
            results = search_web(f"{industry} {q}", max_results=4)
            for r in results:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    domain = re.search(r"https?://([^/]+)", url)
                    if domain and any(d in domain.group(1) for d in SKIP_DOMAINS):
                        continue
                    seen_urls.add(url)
                    all_snippets.append(f"[{dim_key}] {r.get('title', '')}\n{r.get('snippet', '')}\n{url}")

    research_text = "\n\n".join(all_snippets[:120])
    deep_text = ""
    for url in list(seen_urls)[:6]:
        c = fetch_content(url)
        if c:
            deep_text += f"\n{url}\n{c[:3000]}\n---\n"

    progress("write", 60)
    now = datetime.now().strftime("%Y-%m-%d")
    template = RESEARCH_REPORT_TEMPLATE.replace("{industry}", industry).replace("{date}", now)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": f"你是行业研究员。按以下架构撰写深度分析报告，不编造数据和结论，所有数据必须来自研究材料并用 [↗](URL) 标注来源。\n\n{template}"},
            {"role": "user", "content": f"请为「{industry}」撰写行业深度分析报告，3000-5000字。\n\n研究材料：\n{research_text[:12000]}\n\n深度页面：\n{deep_text[:8000]}"},
        ],
        temperature=0.5,
        max_tokens=10000,
    )
    report = resp.choices[0].message.content
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    progress("done", 100)
    return report


def _ensure_deep_report(industry: str) -> Path:
    """工厂接单版优先用完整深度研究报告；没有就自动生成完整深度研究。"""
    safe = re.sub(r'[\\/:*?"<>|]', '_', industry)[:80][:50]
    deep_path = PROJECT_ROOT / "output" / "deep" / safe / f"{safe}.md"
    if deep_path.exists() and not _is_template_report(deep_path.read_text(encoding="utf-8")):
        return deep_path

    research_path = PROJECT_ROOT / "output" / f"report_{safe}.md"
    if research_path.exists() and research_path.stat().st_size > 20000 and not _is_template_report(research_path.read_text(encoding="utf-8")):
        return research_path

    _run_deep_research(industry, safe, deep_path)
    return deep_path


def _deep_merge(main_report: str, sub_reports: list[dict], industry: str) -> str:
    """深度融合：以策略型结构重组多份报告"""
    subs_text = "\n\n---\n\n".join([f"【{s['keyword']}】\n{s['report'][:8000]}" for s in sub_reports])
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": """你是产业策略分析师。将多份报告融合为一份深度策略白皮书，面向企业决策者。

## 融合原则
1. 所有 [↗](URL) 链接原样保留
2. 不少于 12000 字
3. 每个判断必须有具体数据或案例支撑
4. 不同路径的利弊说清楚，不推销单一方案

## 报告结构（五章）

第一章「格局判断」：谁在赢、谁在输。基于子报告中的品牌排名、融资、销量数据，判断当前竞争格局。用具体公司、具体数字说话。

第二章「利润池」：钱在哪里、怎么分、正在往哪流。基于产业链和成本数据，拆解每个环节的利润空间和变化趋势。子报告中关于产业带、集散地、1688价格、供应链的信息集中在这一章。

第三章「关键变量」：未来12-18个月可能改变游戏规则的因素。技术突破、政策变化、巨头入场、原材料波动。基于 trends 和 triggers 维度的搜索结果。

第四章「策略选择」：不同类型的参与者（工厂、品牌、跨境卖家、门店）分别面临什么选择。每种路径对应的资源要求、时间窗口和风险。基于 winners 和 profit 维度的数据。

第五章「风险与不确定性」：什么可能会出问题。基于 risk 维度的搜索结果，列出具体的风险场景和应对思路。"""},
            {"role": "user", "content": f"请为「{industry}」撰写深度策略白皮书。\n\n主报告（行业全景）：\n{main_report[:12000]}\n\n深度分析报告（竞争、利润、供应链、风险）：\n{subs_text[:20000]}\n\n要求：五章结构，不少于12000字，每条判断有数据支撑，[↗](URL)链接原样保留。"},
        ],
        temperature=0.7, max_tokens=16000,
    )
    return resp.choices[0].message.content


def _merge_reports(main_report: str, sub_reports: list[dict], industry: str) -> str:
    """融合主报告 + 子报告"""
    subs_text = "\n\n---\n\n".join([f"【{s['keyword']}】\n{s['report'][:8000]}" for s in sub_reports])
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": WHITE_PAPER_SYSTEM},
            {"role": "user", "content": f"""请将以下多份报告融合为一份关于「{industry}」的深度研究白皮书。

融合原则：
1. 以主报告为骨架，但每个章节必须从子报告中引入至少1-2个具体案例或数据
2. 子报告中的独特信息（案例、品牌、价格、销量）必须出现在最终报告中，不能只复述主报告
3. 遇到主报告和子报告数据冲突时，保留子报告的数据（子报告更聚焦更准确）
4. 六章结构不变，不少于10000字
5. 所有 `[来源](url)` 链接原样保留

主报告：
{main_report[:15000]}

子报告（每份都要从中提取至少3处内容融入主报告对应章节）：
{subs_text[:16000]}"""},
        ],
        temperature=0.7, max_tokens=16000,
    )
    return resp.choices[0].message.content


# 进度追踪
deep_tasks: dict = {}

@app.post("/api/deep-research")
async def deep_research(req: GenerateRequest):
    if not client:
        raise HTTPException(500, "LLM 未配置")
    industry = req.industry.strip()
    if not industry:
        raise HTTPException(400, "请输入行业名称")
    c = _check_code(req.code, "deep")
    if not c:
        raise HTTPException(403, "授权码无效或次数已用完")

    safe = re.sub(r'[\\/:*?"<>|]', '_', industry)[:80][:50]
    output_dir = PROJECT_ROOT / "output" / "deep" / safe

    # 检查缓存
    merged_path = output_dir / f"{safe}.md"
    if merged_path.exists():
        return {
            "cached": True,
            "report": merged_path.read_text(encoding="utf-8"),
            "path": str(merged_path),
            "industry": industry,
        }

    task_id = str(uuid.uuid4())[:8]
    deep_tasks[task_id] = {"phase": "main", "industry": industry, "progress": 0, "code": req.code}

    def run():
        try:
            def progress(phase: str, value: int) -> None:
                deep_tasks[task_id]["phase"] = phase
                deep_tasks[task_id]["progress"] = value

            report = _run_deep_research(
                industry,
                safe,
                merged_path,
                on_progress=progress,
                on_keywords=lambda keywords: deep_tasks[task_id].__setitem__("keywords", keywords),
            )
            deep_tasks[task_id]["progress"] = 100
            deep_tasks[task_id]["phase"] = "done"
            deep_tasks[task_id]["report"] = report
            deep_tasks[task_id]["path"] = str(merged_path)
            _use_code(deep_tasks[task_id].get("code", ""), "deep", industry, str(merged_path))
        except Exception as e:
            deep_tasks[task_id]["phase"] = "error"
            deep_tasks[task_id]["error"] = str(e)

    threading.Thread(target=run, daemon=True).start()
    return {"task_id": task_id, "cached": False, "industry": industry}


@app.get("/api/deep-research/{task_id}")
async def deep_status(task_id: str):
    if task_id not in deep_tasks:
        raise HTTPException(404, "任务不存在")
    t = deep_tasks[task_id]
    result = {"phase": t.get("phase"), "progress": t.get("progress", 0), "keywords": t.get("keywords", [])}
    if t.get("phase") == "done":
        result["report"] = t.get("report")
        result["path"] = t.get("path")
    if t.get("phase") == "error":
        result["error"] = t.get("error")
    return result


@app.get("/api/check-cache")
async def check_cache(industry: str = ""):
    if not industry:
        return {"exists": False}
    safe = re.sub(r"[^\w一-鿿\-]", "_", industry.strip())[:50]
    merged = PROJECT_ROOT / "output" / "deep" / safe / f"{safe}.md"
    if merged.exists():
        return {"exists": True, "industry": industry.strip(), "report": merged.read_text(encoding="utf-8")}
    return {"exists": False}


# ── HTML 导出 ─────────────────────────────────────────────

TEMPLATE_PATH = PROJECT_ROOT / "web" / "template.html"
TEMPLATE_CSS = ""
TEMPLATE_BODY_SAMPLE = ""
if TEMPLATE_PATH.exists():
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    m = re.search(r"<style>(.*?)</style>", raw, re.DOTALL)
    if m:
        TEMPLATE_CSS = m.group(1)
    bm = re.search(r"<body>(.*?)</body>", raw, re.DOTALL)
    if bm:
        TEMPLATE_BODY_SAMPLE = bm.group(1)[:4000]


class ExportRequest(BaseModel):
    markdown: str
    industry: str


HTML_CONVERT_PROMPT = """你是一个前端工程师。将以下 Markdown 行业白皮书转换为一个完整的 HTML 页面。Markdown 中的 [↗](URL) 格式必须原样保留为可点击链接，不要改成别的格式。

## 样式参考

**必须使用以下完整的 CSS。不要自己编样式，直接复制这段 CSS 到 <style> 标签中。**

在 CSS 末尾追加一条规则：`.sec-dark p, .sec-dark li, .sec-dark a, .sec-dark td, .sec-dark th { color: rgba(255,255,255,0.85); }` ——但优先不使用 sec-dark 类来展示正文内容。

```css
""" + TEMPLATE_CSS + """

.sec-dark p, .sec-dark li, .sec-dark a, .sec-dark td, .sec-dark th { color: rgba(255,255,255,0.85); }
```

## 页面结构参考

模板使用了以下组件模式（必须遵循）：

```html
<!-- nav: 固定导航栏 -->
<nav>
  <div class="nav-logo">行业白皮书</div>
  <div class="nav-links">
    <a class="nav-link" href="#ch1">市场全景</a>
    ...
  </div>
</nav>

<!-- hero: 顶部概览 -->
<div class="hero">
  <div class="hero-deco"></div><div class="hero-deco2"></div>
  <div class="hero-wrap">
    <div class="hero-badge"><div class="hero-badge-dot"></div><div class="hero-badge-text">行业白皮书 · 2026</div></div>
    <h1 class="hero-title">行业名称</h1>
    <p class="hero-sub">摘要描述</p>
    <div class="hero-author">数据来源与声明</div>
    <div class="hero-kpis">
      <div class="hero-kpi"><div class="kn">数字</div><div class="kl">标签</div></div>
    </div>
  </div>
</div>

<!-- section: 每个章节 -->
<section id="ch1">
  <div class="wrap">
    <div class="sec-head fi">
      <div>
        <div class="sec-ey">01 · 章节名</div>
        <h2 class="sec-h">章节标题</h2>
      </div>
    </div>
    <!-- 章节内容放在这里 -->
  </div>
</section>

<!-- section交替背景: class="sec-alt" 或 class="sec-accent" -->
<!-- 不要使用 sec-dark，它会导致正文在深色背景上不可读 -->

<!-- footer -->
<footer>
  <div class="ft-logo">trend_grab · 行业白皮书</div>
  <div class="ft-text">数据来源说明</div>
</footer>

<!-- 滚动动画脚本 -->
<script>
  const obs = new IntersectionObserver(entries => {
    entries.forEach(e => { if (e.isIntersecting) e.target.classList.add('on'); });
  }, { threshold: 0.07, rootMargin: '0px 0px -28px 0px' });
  document.querySelectorAll('.fi').forEach(el => obs.observe(el));
  const scrollObs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('on'));
        const a = document.querySelector('.nav-link[href="#' + e.target.id + '"]');
        if (a) a.classList.add('on');
      }
    });
  }, { threshold: 0.35 });
  document.querySelectorAll('section[id]').forEach(s => scrollObs.observe(s));
</script>
```

## 内容要求

HTML 正文内容不少于 6000 字。可以精简 Markdown 中重复的信息，但每个章节的核心分析、关键数据、案例细节必须保留。关键段落要展开写，不能全变成要点列表。用以下节奏：一段展开的长文字（200-300字），配一个卡片或对比块来可视化数据，再一段分析文字，再一个要点列表。长短交替。`[来源](URL)` 的 Markdown 链接在 HTML 中必须转成可点击的 `<a href="URL" target="_blank">来源</a>`，保留跳转功能。

## 输出

输出完整 HTML 文件（从 <!DOCTYPE html> 到 </html>）。Google Fonts 从 CDN 引入。"""


@app.post("/api/export-html")
async def export_html(req: ExportRequest):
    if not client:
        raise HTTPException(500, "LLM 未配置")
    if not req.markdown:
        raise HTTPException(400, "缺少 markdown 内容")

    prompt = HTML_CONVERT_PROMPT + f"\n\n## 行业名称\n{req.industry}\n\n## Markdown 内容\n\n{req.markdown[:20000]}"

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=16000,
            )
            html = resp.choices[0].message.content
            html = re.sub(r"^```html?\s*\n?", "", html)
            html = re.sub(r"\n?```\s*$", "", html)
            return {"html": html}
        except Exception as e:
            if attempt == 2:
                raise HTTPException(500, f"HTML 导出失败: {e}")
            time.sleep(3)


# ── 管理员 ─────────────────────────────────────────────

ADMIN_PW = os.getenv("ADMIN_PASSWORD", "trendgrab2026")


@app.post("/api/admin/login")
async def admin_login(password: str = ""):
    if password == ADMIN_PW:
        return {"ok": True, "token": ADMIN_PW}
    raise HTTPException(403, "密码错误")


@app.get("/api/admin/reports")
async def admin_reports(token: str = ""):
    if token != ADMIN_PW:
        raise HTTPException(403, "未授权")
    results = []
    output_dir = PROJECT_ROOT / "output"
    if output_dir.exists():
        # 快速研究
        for f in sorted(output_dir.glob("report_*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            results.append({"type": "快速", "name": f.stem.replace("report_", ""), "time": datetime.fromtimestamp(f.stat().st_mtime).isoformat(), "size": f.stat().st_size, "path": str(f)})
        # 深度研究：output/deep/{行业}/{行业}.md
        deep_dir = output_dir / "deep"
        if deep_dir.exists():
            for d in sorted(deep_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if d.is_dir():
                    for f in sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
                        name = d.name  # 目录名就是行业名
                        results.append({"type": "深度", "name": name, "time": datetime.fromtimestamp(f.stat().st_mtime).isoformat(), "size": f.stat().st_size, "path": str(f)})
    return results


@app.get("/api/admin/report-content")
async def admin_report_content(token: str = "", path: str = ""):
    if token != ADMIN_PW:
        raise HTTPException(403, "未授权")
    p = Path(path)
    if not p.exists() or not str(p.resolve()).startswith(str((PROJECT_ROOT / "output").resolve())):
        raise HTTPException(404, "报告不存在")
    return {"content": p.read_text(encoding="utf-8")}


# ── 分享页面 ─────────────────────────────────────────────

SHARE_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{title}</title>
<script src="/marked.min.js"></script>
<style>:root{{--bg:#FAFAF7;--paper:#fff;--ink:#1A1A24;--muted:#8A8898;--border:#E2DDD4;--gold:#B8871A;}}
*{{margin:0;padding:0;box-sizing:border-box}}body{{font:15px/1.8 system-ui,sans-serif;background:var(--bg);color:var(--ink)}}
.container{{max-width:860px;margin:0 auto;padding:40px 20px}}
header{{text-align:center;padding:40px 0;border-bottom:1px solid var(--border);margin-bottom:32px}}
h1{{font-size:24px;font-weight:700}}header p{{color:var(--muted);margin-top:8px;font-size:14px}}
.content h2{{font-size:20px;margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--border)}}
.content h3{{font-size:16px;margin:20px 0 8px}}
.content p{{margin:8px 0}}.content table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}}
.content td,.content th{{border:1px solid var(--border);padding:8px 12px;text-align:left}}.content th{{background:#f5f5f4}}
.content blockquote{{border-left:3px solid var(--gold);padding:4px 16px;margin:12px 0;color:var(--muted)}}
.content a{{color:var(--gold)}}footer{{text-align:center;padding:40px;color:var(--muted);font-size:12px}}
.content{{background:var(--paper);border:1px solid var(--border);border-radius:8px;padding:40px 48px}}
@media(max-width:640px){{.content{{padding:24px 20px}}}}
</style></head><body>
<div class="container"><header><h1>{title}</h1><p>行业白皮书 · trendgrab 生成 · {date}</p></header>
<div class="content" id="content"></div>
<footer>Powered by trendgrab · <a href="/">生成你自己的报告</a></footer></div>
<script>document.getElementById('content').innerHTML=marked.parse({content_json});</script>
</body></html>"""


@app.get("/deep-report/{name}", response_class=HTMLResponse)
async def view_deep_report(name: str):
    clean_name = re.sub(r"^report_", "", name)
    safe = re.sub(r'[\\/:*?"<>|]', '_', clean_name)[:80][:50]
    path = PROJECT_ROOT / "output" / "deep" / safe / f"{safe}.md"
    if not path.exists() or _is_template_report(path.read_text(encoding="utf-8")):
        fallback = PROJECT_ROOT / "output" / f"report_{safe}.md"
        path = fallback if fallback.exists() and fallback.stat().st_size > 20000 else path
    if not path.exists():
        raise HTTPException(404, "深度报告不存在")
    content = path.read_text(encoding="utf-8")
    return HTMLResponse(
        SHARE_HTML
        .replace("{title}", f"{safe} 深度报告")
        .replace("{date}", datetime.now().strftime("%Y-%m-%d"))
        .replace("{content_json}", json.dumps(content, ensure_ascii=False))
    )


SHARE_MAP_FILE = PROJECT_ROOT / "output" / "share_map.json"


def _load_share_map() -> dict:
    if SHARE_MAP_FILE.exists():
        return json.loads(SHARE_MAP_FILE.read_text(encoding="utf-8"))
    return {}


def _save_share_map(data: dict):
    SHARE_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    SHARE_MAP_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@app.get("/s/{sid}")
async def share_report(sid: str):
    smap = _load_share_map()
    entry = smap.get(sid)
    if not entry:
        raise HTTPException(404, "分享链接不存在或已过期")
    path = Path(entry["path"])
    if not path.exists():
        raise HTTPException(404, "报告文件已被删除")
    if entry.get("format") == "readable":
        return HTMLResponse(path.read_text(encoding="utf-8"))
    content = path.read_text(encoding="utf-8")
    title = entry["industry"]
    return HTMLResponse(SHARE_HTML.replace("{title}", title).replace("{date}", entry.get("date", "")).replace("{content_json}", json.dumps(content, ensure_ascii=False)))


@app.post("/api/share")
async def create_share(industry: str = "", code: str = ""):
    """创建分享链接，返回短ID"""
    if not industry:
        raise HTTPException(400, "缺少行业名称")
    safe = re.sub(r'[\\/:*?"<>|]', '_', industry)[:80]
    # 找报告文件
    path = PROJECT_ROOT / "output" / f"report_{safe}.md"
    mode = "quick"
    if not path.exists():
        deep_path = PROJECT_ROOT / "output" / "deep" / safe / f"{safe}.md"
        if deep_path.exists():
            path = deep_path
            mode = "deep"
    if not path.exists():
        raise HTTPException(404, "报告不存在，请先生成")
    # 生成短ID
    sid = uuid.uuid4().hex[:8]
    smap = _load_share_map()
    smap[sid] = {"industry": industry, "path": str(path), "date": datetime.now().strftime("%Y-%m-%d"), "mode": mode}
    _save_share_map(smap)
    return {"url": f"/s/{sid}"}


@app.post("/api/share-readable")
async def create_readable_share(industry: str = "", code: str = ""):
    """为工厂接单研判页创建独立分享链接，不影响既有白皮书分享。"""
    if not industry:
        raise HTTPException(400, "缺少行业名称")
    auth = _load_auth()
    if code != ADMIN_CODE and code not in auth:
        raise HTTPException(403, "授权码无效")

    safe = re.sub(r'[\\/:*?"<>|]', '_', industry)[:80]
    path = PROJECT_ROOT / "output" / "readable" / f"{safe}.html"
    if not path.exists():
        raise HTTPException(404, "工厂接单研判页不存在，请先生成")

    sid = uuid.uuid4().hex[:8]
    smap = _load_share_map()
    smap[sid] = {
        "industry": industry,
        "path": str(path),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "mode": "quick",
        "format": "readable",
    }
    _save_share_map(smap)
    return {"url": f"/s/{sid}"}


@app.get("/readable/{name}", response_class=HTMLResponse)
async def view_readable_report(name: str):
    """打开已生成的独立工厂接单研判页。"""
    safe = re.sub(r'[\\/:*?"<>|]', '_', name)[:80]
    if name != safe:
        raise HTTPException(404, "页面不存在")
    path = PROJECT_ROOT / "output" / "readable" / f"{safe}.html"
    if not path.exists():
        raise HTTPException(404, "工厂接单研判页不存在，请先生成")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/readable-assets/{name}/{filename}")
async def view_readable_asset(name: str, filename: str):
    safe = re.sub(r'[\\/:*?"<>|]', '_', name)[:80]
    if name != safe or not re.fullmatch(r"scene-[1-4]\.(?:jpg|png|webp|gif)", filename):
        raise HTTPException(404, "图片不存在")
    path = PROJECT_ROOT / "output" / "readable" / "assets" / safe / filename
    if not path.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path)


# ── 静态文件 ──
static_dir = PROJECT_ROOT / "web" / "static"


@app.get("/api/version")
async def get_version():
    return {"version": "2.34.0", "date": "2026-08-08"}


# ── 静态文件 ──
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
