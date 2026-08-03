#!/usr/bin/env python3
"""
trend_grab - 行业信息聚合器
从 Miniflux + changedetection.io 拉取数据，生成结构化 JSON 供 Claude 分析写文章。

用法:
    python scripts/aggregate.py                    # 采集数据 → output/daily_raw.json
    python scripts/aggregate.py --hours 48         # 回溯 48 小时
    python scripts/aggregate.py --dry-run          # 只检查 API 连通性
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

# ── 初始化 ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

console = Console()


def load_sources() -> dict:
    """加载 config/sources.yaml"""
    config_path = PROJECT_ROOT / "config" / "sources.yaml"
    if not config_path.exists():
        console.print(f"[red]配置文件不存在: {config_path}[/red]")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Miniflux Client ─────────────────────────────────────
class MinifluxClient:
    """Miniflux REST API 封装"""

    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(
            headers={"X-Auth-Token": api_key},
            timeout=30.0,
        )

    def ping(self) -> bool:
        """测试连通性（使用不需要认证的 healthcheck 端点）"""
        try:
            r = httpx.get(f"{self.base}/healthcheck", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    def get_feeds(self) -> list[dict]:
        """获取所有订阅源"""
        r = self.client.get(f"{self.base}/v1/feeds")
        r.raise_for_status()
        return r.json()

    def get_entries(self, after: datetime, limit: int = 100) -> list[dict]:
        """获取指定时间之后的条目"""
        after_ts = int(after.timestamp())
        params = {
            "after": after_ts,
            "limit": limit,
            "order": "published_at",
            "direction": "desc",
        }
        r = self.client.get(f"{self.base}/v1/entries", params=params)
        r.raise_for_status()
        return r.json().get("entries", [])

    def get_feed_entries(self, feed_id: int, after: datetime, limit: int = 50) -> list[dict]:
        """获取单个订阅源的条目"""
        after_ts = int(after.timestamp())
        params = {
            "after": after_ts,
            "limit": limit,
            "order": "published_at",
            "direction": "desc",
        }
        r = self.client.get(f"{self.base}/v1/feeds/{feed_id}/entries", params=params)
        r.raise_for_status()
        return r.json().get("entries", [])


# ── changedetection.io Client ───────────────────────────
class ChangedetectionClient:
    """changedetection.io REST API 封装"""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base = base_url.rstrip("/")
        headers = {}
        if api_key:
            headers["x-api-key"] = api_key
        self.client = httpx.Client(headers=headers, timeout=30.0)

    def ping(self) -> bool:
        """测试连通性"""
        try:
            r = httpx.get(f"{self.base}/", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    def get_watches(self) -> list[dict]:
        """获取所有监控任务（返回 list，uuid 注入每个 watch）"""
        r = self.client.get(f"{self.base}/api/v1/watch")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            # changedetection.io returns {uuid: watch_data, ...}
            result = []
            for uid, watch in data.items():
                if isinstance(watch, dict):
                    watch["uuid"] = uid
                    result.append(watch)
            return result
        return data if isinstance(data, list) else []

    def get_history(self, watch_uuid: str, after_ts: int) -> list[dict]:
        """获取单个监控任务的历史记录"""
        params = {"limit": 20}
        r = self.client.get(
            f"{self.base}/api/v1/watch/{watch_uuid}/history", params=params
        )
        r.raise_for_status()
        # 过滤出在时间窗口内的变化
        history = r.json()
        if isinstance(history, list):
            return [
                h for h in history
                if isinstance(h, dict) and h.get("timestamp", 0) >= after_ts
            ]
        return []


# ── 数据采集 ─────────────────────────────────────────────
def collect_rss_data(
    mf: MinifluxClient, sources: dict, since: datetime
) -> list[dict]:
    """从 Miniflux 采集 RSS 数据"""
    feeds_data = []
    console.print("[bold cyan]正在从 Miniflux 拉取数据...[/bold cyan]")

    try:
        feeds = mf.get_feeds()
        console.print(f"  找到 {len(feeds)} 个订阅源")
    except Exception as e:
        console.print(f"[red]无法获取订阅源列表: {e}[/red]")
        return feeds_data

    for feed in feeds:
        try:
            entries = mf.get_feed_entries(feed["id"], since)
        except Exception:
            continue

        if not entries:
            continue

        # 找对应的 category（从 sources.yaml 匹配）
        feed_url = feed.get("feed_url", "")
        category = feed.get("category", {}).get("title", "未分类")

        feed_block = {
            "feed_title": feed.get("title", "未知源"),
            "feed_url": feed_url,
            "category": category,
            "entries": [],
        }

        for entry in entries:
            feed_block["entries"].append({
                "id": entry["id"],
                "title": entry["title"],
                "url": entry["url"],
                "author": entry.get("author", ""),
                "summary": _strip_html(entry.get("content", "")),
                "published_at": entry.get("published_at", ""),
                "read": entry.get("status", "") == "read",
            })

        feed_block["entry_count"] = len(feed_block["entries"])
        feeds_data.append(feed_block)
        console.print(
            f"  [green]✓[/green] {feed_block['feed_title']}: "
            f"{feed_block['entry_count']} 条"
        )

    return feeds_data


def collect_change_data(
    cd: ChangedetectionClient, sources: dict, since: datetime
) -> list[dict]:
    """从 changedetection.io 采集变化数据"""
    watches_data = []
    console.print("[bold cyan]正在从 changedetection.io 拉取数据...[/bold cyan]")

    try:
        watches = cd.get_watches()
        console.print(f"  找到 {len(watches)} 个监控任务")
    except Exception as e:
        console.print(f"[red]无法获取监控列表: {e}[/red]")
        return watches_data

    after_ts = int(since.timestamp())

    for watch in watches:
        uuid = watch.get("uuid", "")
        watch_name = watch.get("title", "") or watch.get("url", "")
        last_checked = watch.get("last_checked", 0)
        last_changed = watch.get("last_changed", 0)

        change_block = {
            "uuid": uuid,
            "name": watch_name,
            "url": watch.get("url", ""),
            "last_checked_ts": last_checked,
            "last_changed_ts": last_changed,
            "change_detected": last_changed >= after_ts,
            "changes": [],
        }

        if change_block["change_detected"]:
            try:
                history = cd.get_history(uuid, after_ts)
                for h in history:
                    change_block["changes"].append({
                        "timestamp": h.get("timestamp", 0),
                        "timestamp_str": _ts_to_str(h.get("timestamp", 0)),
                        "diff": h.get("diff", ""),
                    })
            except Exception:
                pass

        watches_data.append(change_block)
        status = "有变化" if change_block["change_detected"] else "无变化"
        console.print(
            f"  [{'yellow' if change_block['change_detected'] else 'green'}]●[/] "
            f"{watch_name}: {status}"
        )

    return watches_data


# ── 输出 ────────────────────────────────────────────────
def build_report(
    feeds_data: list[dict],
    watches_data: list[dict],
    since: datetime,
    sources: dict,
) -> dict:
    """组装最终报告"""

    total_entries = sum(f["entry_count"] for f in feeds_data)
    total_changes = sum(1 for w in watches_data if w["change_detected"])

    # 统计分类
    categories = {}
    for f in feeds_data:
        cat = f["category"]
        categories[cat] = categories.get(cat, 0) + f["entry_count"]

    now = datetime.now(timezone.utc).astimezone()

    return {
        "generated_at": now.isoformat(),
        "period": {
            "from": since.isoformat(),
            "to": now.isoformat(),
        },
        "rss_feeds": feeds_data,
        "page_watches": watches_data,
        "stats": {
            "total_articles": total_entries,
            "feeds_with_content": sum(1 for f in feeds_data if f["entry_count"] > 0),
            "total_feeds": len(feeds_data),
            "total_changes": total_changes,
            "total_watches": len(watches_data),
            "categories": categories,
        },
    }


def save_report(report: dict):
    """保存报告到 output/"""
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "daily_raw.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    console.print(f"\n[bold green]报告已保存:[/bold green] {output_path}")
    console.print(f"  {report['stats']['total_articles']} 篇文章")
    console.print(f"  {report['stats']['total_changes']} 个页面变化")

    # 打印摘要表
    if report["stats"]["categories"]:
        table = Table(title="分类统计")
        table.add_column("分类", style="cyan")
        table.add_column("文章数", style="green")
        for cat, count in report["stats"]["categories"].items():
            table.add_row(cat, str(count))
        console.print(table)


# ── 工具函数 ─────────────────────────────────────────────
def _strip_html(html: str, max_len: int = 500) -> str:
    """简单去除 HTML 标签"""
    import re
    text = re.sub(r"<[^>]+>", "", html)
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text


def _ts_to_str(ts: int) -> str:
    """Unix 时间戳转 ISO 字符串"""
    if ts == 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()


# ── 入口 ────────────────────────────────────────────────
def main():
    import argparse

    parser = argparse.ArgumentParser(description="trend_grab 行业信息聚合器")
    parser.add_argument("--hours", type=int, default=24, help="回溯小时数 (默认 24)")
    parser.add_argument("--dry-run", action="store_true", help="仅测试 API 连通性")
    parser.add_argument("--limit", type=int, default=50, help="每个 feed 最多条目数")
    args = parser.parse_args()

    sources = load_sources()

    # API 连接信息
    mf_url = os.getenv("MINIFLUX_URL", "http://localhost:8088")
    mf_key = os.getenv("MINIFLUX_API_KEY", "")
    cd_url = os.getenv("CHANGEDETECTION_URL", "http://localhost:5001")
    cd_key = os.getenv("CHANGEDETECTION_API_KEY", "")

    # 连通性检查
    mf = MinifluxClient(mf_url, mf_key)
    cd = ChangedetectionClient(cd_url, cd_key)

    console.print(f"[bold]Miniflux:[/bold] {mf_url} ... ", end="")
    if mf.ping():
        console.print("[green]OK[/green]")
    else:
        console.print("[red]连接失败[/red]")
        if not args.dry_run:
            console.print(
                "[yellow]提示: 请确保 docker compose up -d 已运行，"
                "且 MINIFLUX_API_KEY 已配置[/yellow]"
            )

    console.print(f"[bold]changedetection.io:[/bold] {cd_url} ... ", end="")
    if cd.ping():
        console.print("[green]OK[/green]")
    else:
        console.print("[red]连接失败[/red]")
        if not args.dry_run:
            console.print(
                "[yellow]提示: 请确保 docker compose up -d 已运行[/yellow]"
            )

    if args.dry_run:
        console.print("[yellow]Dry-run 完成[/yellow]")
        return

    if not mf_key:
        console.print(
            "[red]MINIFLUX_API_KEY 未设置！[/red]\n"
            "1. 打开 http://localhost:8080\n"
            "2. Settings → API Keys → 创建 API Key\n"
            "3. 复制到 .env 的 MINIFLUX_API_KEY= 字段"
        )
        sys.exit(1)

    now = datetime.now(timezone.utc).astimezone()
    since = now - timedelta(hours=args.hours)
    console.print(f"\n[bold]采集时间范围:[/bold] {since.strftime('%Y-%m-%d %H:%M')} → {now.strftime('%Y-%m-%d %H:%M')}")

    # 采集
    feeds_data = collect_rss_data(mf, sources, since)
    watches_data = collect_change_data(cd, sources, since)

    # 组装 & 保存
    report = build_report(feeds_data, watches_data, since, sources)
    save_report(report)


if __name__ == "__main__":
    main()
