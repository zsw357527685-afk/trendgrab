#!/usr/bin/env python3
"""
trend_grab — 行业白皮书全自动生成器

用法:
    python scripts/generate_report.py 解压玩具
    python scripts/generate_report.py "3D打印饰品" --no-rss

流程:
    1. 生成研究简报（搜索关键词 + 报告模板）
    2. 输出执行指令文件
    3. 用户将指令发给 Claude → Claude 自动完成搜索、深读、写作

输出:
    output/brief_{行业}.json      → 研究简报（机器可读）
    output/instruction_{行业}.md   → Claude 执行指令（人机可读）
    output/report_{行业}.md        → 最终白皮书（Claude 写入）

可调整:
    白皮书生成后，可以继续对话调整：视角、语气、篇幅、增减章节
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
console = Console()

# ═══════════════════════════════════════════════════════════════
# 5 维度搜索关键词生成
# ═══════════════════════════════════════════════════════════════

def build_queries(industry: str) -> dict:
    return {
        "market": [
            f"{industry} 2026年 市场规模 最新数据",
            f"{industry} 子品类 增速 细分 2026",
            f"{industry} market size 2026 latest report",
        ],
        "hot": [
            f"{industry} 2026年7月 最新 热点 近日",
            f"{industry} TikTok 爆款 新品 2026年",
            f"{industry} 出海 跨境电商 热门 最新 2026",
        ],
        "yiwu": [
            f"{industry} 义乌 国际商贸城 工厂 2026",
            f"{industry} 义乌 1688 批发 出货 近期",
            f"{industry} 义乌 产业带 供应链 成本",
        ],
        "history": [
            f"{industry} 发展历程 起源 历史 关键节点",
            f"{industry} 行业演变 里程碑",
        ],
        "competition": [
            f"{industry} 头部品牌 竞争格局 市场份额 商业模式",
            f"{industry} 供应链 义乌 澄海 工厂 产业链",
            f"{industry} top brands companies market share",
        ],
        "trends": [
            f"{industry} 未来趋势 预测 2026 2027",
            f"{industry} AI 数字化 合规 新国标 3D打印",
            f"{industry} future trends forecast innovation",
        ],
    }


# ═══════════════════════════════════════════════════════════════
# Claude 执行指令生成
# ═══════════════════════════════════════════════════════════════

INSTRUCTION_TEMPLATE = """# 行业白皮书生成任务

## 任务
撰写一份关于「{industry}」的行业白皮书。

## 执行步骤

### 第一步：并行搜索
同时执行以下 5 组 WebSearch，每组选最相关的 2 个 query：

**市场与规模：**
{market_queries}

**近期热点：**
{hot_queries}

**行业沿革：**
{history_queries}

**竞争格局：**
{competition_queries}

**趋势展望：**
{trends_queries}

### 第二步：深读关键页面
从搜索结果中选出 5-8 篇最有价值的页面做 WebFetch。标准：
- 行业报告 > 一手数据 > 深度报道 > 观点评论
- 中英文交叉验证
- 宁可少读精读，不要泛泛掠过

### 第三步：撰写白皮书

## 白皮书规范

### 视角
站在义乌产业带的立场，为从业者提供信息参考。不预设结论，不引导决策。

### 六章结构

**第一章：市场全景** — 规模与增速（多口径对比）、子品类增速分化、消费者结构变化、产业地理分布。纯描述，不推导。

**第二章：近期热点** — 过去6-12个月的具体案例。TikTok爆款、国内现象级产品、品牌动态、制造端变化。每个案例讲清楚发生了什么、数字是多少、值得注意的地方在哪。

**第三章：行业发展历程与关键节点** — 从品类起源到当前格局的完整时间线。按阶段划分（萌芽期/兴起期/爆发期/加速期/分化期），标注关键事件和行业影响。最后附关键节点一览表。

**第四章：竞争格局** — 参与者分层、价值链利润分布、商业模式分化、平台渠道分化。品牌化是五类参与者之一、五种模式之一，不设为主线。

**第五章：趋势展望** — MESH概念、AI智能化、合规门槛、3D打印对设计制造关系的影响、情绪经济的长期支撑。五个趋势并列，不分主次。

**第六章：开放性问题** — 数据还回答不了的问题。消费者结构变化是结构性还是周期性、品牌溢价的上限、知识产权在快周期品类的运用、3D打印对义乌的净效应、平台红利窗口时长、AI制造链条的形成。只问不答。

### 写作铁律

1. 零破折号。需要解释的地方另起一句或用逗号。
2. 零加粗。让文字自己说话。
3. 不写"第一个/第二个/第三个"或"首先/其次/最后"。
4. 删除"正确但没信息量"的句子，如"随着时代发展""越来越多人意识到"。
5. 每个数据标注来源。
6. 品牌化是可选项之一，不是主线方向。不引导读者去做品牌。
7. 语气平实。像是长期跟踪这个行业的人在分享观察。
8. 句长控制在合理范围。长短交替，避免连续多段长句。
9. 每节末尾不要习惯性总结。直接进入下一节或自然收住。
10. 不确定的地方，说"目前还不清楚""还需要数据验证"，不要假装知道。

### 输出
最终白皮书保存到 `output/report_{safe_industry}.md`。

字数约 10,000-12,000 字。数据来源附在文末。

---

*此指令由 trend_grab 自动生成 | {timestamp}*
"""


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="trend_grab 行业白皮书生成器")
    parser.add_argument("industry", help="行业名称")
    parser.add_argument("--no-rss", action="store_true", help="跳过 RSS 匹配")
    args = parser.parse_args()

    industry = args.industry.strip()
    safe = re.sub(r"[^\w一-鿿\-]", "_", industry)[:50]
    now = datetime.now(timezone.utc).astimezone()

    # 1. 生成搜索关键词
    queries = build_queries(industry)
    m_qs = "\n".join(f"- {q}" for q in queries["market"])
    h_qs = "\n".join(f"- {q}" for q in queries["hot"])
    hi_qs = "\n".join(f"- {q}" for q in queries["history"])
    c_qs = "\n".join(f"- {q}" for q in queries["competition"])
    t_qs = "\n".join(f"- {q}" for q in queries["trends"])

    # 2. 生成研究简报 JSON
    brief = {
        "industry": industry,
        "generated_at": now.isoformat(),
        "search_queries": queries,
    }
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    brief_path = output_dir / f"brief_{safe}.json"
    with open(brief_path, "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)

    # 3. 生成 Claude 执行指令
    instruction = INSTRUCTION_TEMPLATE.format(
        industry=industry,
        safe_industry=safe,
        market_queries=m_qs,
        hot_queries=h_qs,
        history_queries=hi_qs,
        competition_queries=c_qs,
        trends_queries=t_qs,
        timestamp=now.strftime("%Y-%m-%d %H:%M"),
    )
    instruction_path = output_dir / f"instruction_{safe}.md"
    with open(instruction_path, "w", encoding="utf-8") as f:
        f.write(instruction)

    console.print()
    console.print(Panel.fit(
        f"[bold]研究简报:[/bold] {brief_path}\n"
        f"[bold]执行指令:[/bold] {instruction_path}\n\n"
        f"把 [bold]执行指令[/bold] 发给 Claude。\n"
        f"Claude 会自动完成：搜索 → 深读 → 写作。\n"
        f"白皮书保存到 [dim]output/report_{safe}.md[/dim]\n\n"
        f"[dim]生成后可继续对话调整：视角、语气、增减内容[/dim]",
        title=f"行业白皮书生成器: {industry}",
        border_style="green",
    ))


if __name__ == "__main__":
    main()
