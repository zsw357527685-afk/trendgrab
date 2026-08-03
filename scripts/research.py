#!/usr/bin/env python3
"""
trend_grab - 行业研究启动器
输入行业名 → 生成研究简报（搜索关键词 + RSS匹配 + 报告模板）

用法:
    python scripts/research.py 解压玩具                # 生成研究简报
    python scripts/research.py 解压玩具 --no-rss        # 跳过RSS匹配（更快）
    python scripts/research.py 解压玩具 --output report  # 同时输出研究报告模板

输出:
    output/industry_brief_{行业名}.json  → Claude 读取后进行深度研究
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ── 初始化 ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
console = Console()


# ── 5 维度搜索关键词生成 ────────────────────────────────
def generate_search_queries(industry: str) -> dict:
    """为一个行业生成 5 个维度的搜索关键词（中英文）"""

    queries = {
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
    return queries


# ── RSS 数据匹配 ─────────────────────────────────────────
def search_rss_matches(industry: str, keywords: list[str]) -> list[dict]:
    """在已有的 Miniflux RSS 数据中搜索匹配内容"""
    api_key = os.getenv("MINIFLUX_API_KEY", "")
    mf_url = os.getenv("MINIFLUX_URL", "http://localhost:8088")

    if not api_key:
        console.print("[yellow]⚠ MINIFLUX_API_KEY 未配置，跳过 RSS 匹配[/yellow]")
        return []

    matches = []
    try:
        client = httpx.Client(
            base_url=mf_url,
            headers={"X-Auth-Token": api_key},
            timeout=15.0,
        )

        # 搜索最近 90 天的条目
        r = client.get("/v1/entries", params={
            "search": industry,
            "limit": 30,
            "order": "published_at",
            "direction": "desc",
        })
        r.raise_for_status()
        entries = r.json().get("entries", [])

        for entry in entries:
            matches.append({
                "title": entry["title"],
                "url": entry["url"],
                "feed": entry.get("feed", {}).get("title", ""),
                "published_at": entry.get("published_at", ""),
                "summary": _truncate(entry.get("content", ""), 200),
            })

        return matches
    except Exception as e:
        console.print(f"[dim]RSS 匹配跳过: {e}[/dim]")
        return []


# ── 研究简报生成 ─────────────────────────────────────────
def build_brief(
    industry: str,
    queries: dict,
    rss_matches: list[dict],
) -> dict:
    """生成研究简报 JSON"""
    now = datetime.now(timezone.utc).astimezone()

    return {
        "industry": industry,
        "generated_at": now.isoformat(),
        "research_dimensions": [
            {"key": "overview", "label": "行业概述", "focus": "行业定义、市场规模、产业链结构"},
            {"key": "history", "label": "发展路径", "focus": "起源→发展→成熟的时间线，关键里程碑事件"},
            {"key": "hot_topics", "label": "近期热点", "focus": "近3-6个月的最新动态、热门事件、投融资"},
            {"key": "competition", "label": "竞争格局", "focus": "头部玩家、品牌梯队、商业模式对比"},
            {"key": "trends", "label": "趋势预测", "focus": "短期/中期趋势，技术演进，消费变化，风险"},
        ],
        "search_queries": queries,
        "rss_matches": rss_matches,
        "rss_match_count": len(rss_matches),
        "report_template": REPORT_TEMPLATE_EN,
        "instructions": (
            "Claude 研究指令：读取此 JSON 后，按以下步骤工作：\n"
            "1. 并行搜索 5 个维度的关键词（每个维度选 2-3 个 query）\n"
            "2. 收集有价值的 URL 链接（至少 10-15 篇）\n"
            "3. WebFetch 深读最关键的 5-8 篇（行业报告、数据文章优先）\n"
            "4. 将 rss_matches 中的数据作为补充（如果有的话）\n"
            "5. 按 report_template 结构写出 3000-5000 字报告\n"
            "6. 所有数据和引用都要标注来源 URL\n"
            "7. 报告保存到 output/industry_report_{行业名}.md"
        ),
    }


# ── 报告模板 ─────────────────────────────────────────────
REPORT_TEMPLATE_EN = """# {industry} 行业深度分析 | {date}

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
```
上游：[原材料/技术/设计] → 中游：[生产/制造/品牌] → 下游：[渠道/零售/消费者]
```
[每段的具体说明]

---

## 二、发展路径与关键节点

### 2.1 时间线

| 时间 | 阶段 | 关键事件 |
|------|------|---------|
| 20XX-20XX | 萌芽期 | [事件] |
| 20XX-20XX | 成长期 | [事件] |
| 20XX-至今 | 爆发/成熟期 | [事件] |

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

| 梯队 | 代表品牌/公司 | 核心优势 | 市场份额（估算） |
|------|-------------|---------|----------------|
| 第一梯队 | [品牌名] | [优势] | [%] |
| 第二梯队 | [品牌名] | [优势] | [%] |

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
- [风险点]

---

## 数据来源

[所有引用的 URL，按出现顺序编号]
"""


# ── 输出 ────────────────────────────────────────────────
def save_brief(brief: dict, industry: str) -> str:
    """保存研究简报"""
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitize_filename(industry)
    path = output_dir / f"industry_brief_{safe_name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)
    return str(path)


def save_template(industry: str) -> str:
    """保存报告模板（可直接填充）"""
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitize_filename(industry)
    path = output_dir / f"industry_report_{safe_name}.md"
    now = datetime.now().strftime("%Y-%m-%d")
    content = REPORT_TEMPLATE_EN.replace("{industry}", industry).replace("{date}", now)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return str(path)


# ── 工具函数 ─────────────────────────────────────────────
def _truncate(text: str, max_len: int) -> str:
    """去除 HTML 标签并截断"""
    text = re.sub(r"<[^>]+>", "", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text


def _sanitize_filename(name: str) -> str:
    """清理文件名"""
    return re.sub(r"[^\w一-鿿\-]", "_", name)[:50]


# ── 入口 ────────────────────────────────────────────────
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="trend_grab 行业研究启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/research.py 解压玩具
  python scripts/research.py "3D打印饰品" --no-rss
  python scripts/research.py 平价饰品 --output all
        """,
    )
    parser.add_argument("industry", help="要研究的行业名称")
    parser.add_argument("--no-rss", action="store_true", help="跳过 RSS 数据匹配")
    parser.add_argument(
        "--output", choices=["brief", "report", "all"], default="all",
        help="输出类型 (默认: all)"
    )
    args = parser.parse_args()

    industry = args.industry.strip()

    # 1. 生成搜索关键词
    queries = generate_search_queries(industry)

    # 2. RSS 匹配
    rss_matches = []
    if not args.no_rss:
        keywords = [industry] + [q.split()[0] for qs in queries.values() for q in qs]
        rss_matches = search_rss_matches(industry, keywords)

    # 3. 构建简报
    brief = build_brief(industry, queries, rss_matches)

    # 4. 输出
    if args.output in ("brief", "all"):
        brief_path = save_brief(brief, industry)
        console.print(f"[green]✓ 研究简报:[/green] {brief_path}")

    if args.output in ("report", "all"):
        tmpl_path = save_template(industry)
        console.print(f"[green]✓ 报告模板:[/green] {tmpl_path}")

    # 5. 打印摘要
    console.print()
    table = Table(title=f"行业研究计划: {industry}")
    table.add_column("维度", style="cyan")
    table.add_column("搜索关键词数", style="green")
    table.add_column("核心问题", style="yellow")
    for dim in brief["research_dimensions"]:
        table.add_row(
            dim["label"],
            str(len(queries.get(dim["key"], []))),
            dim["focus"],
        )
    console.print(table)

    if rss_matches:
        console.print(f"\n[dim]现有 RSS 数据中找到 {len(rss_matches)} 条相关内容[/dim]")

    console.print(f"\n[bold]下一步:[/bold] 把 {brief_path} 发给 Claude，让 AI 完成深度研究")


if __name__ == "__main__":
    main()
