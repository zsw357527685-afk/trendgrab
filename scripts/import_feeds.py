#!/usr/bin/env python3
"""
trend_grab - 批量导入订阅源
读取 config/sources.yaml，自动：
  1. 将 RSSHub 路由 + 直接 RSS 源导入 Miniflux
  2. 将页面监控目标导入 changedetection.io

用法:
    python scripts/import_feeds.py                  # 导入所有源（跳过已存在）
    python scripts/import_feeds.py --dry-run        # 预览但不执行
    python scripts/import_feeds.py --force          # 强制重新导入
"""

import os
import sys
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

RSSHUB_BASE = os.getenv("RSSHUB_URL", "http://localhost:1201")
RSSHUB_INTERNAL = os.getenv("RSSHUB_INTERNAL_URL", "http://rsshub:1200")
MINIFLUX_BASE = os.getenv("MINIFLUX_URL", "http://localhost:8088")
CHANGEDETECTION_BASE = os.getenv("CHANGEDETECTION_URL", "http://localhost:5001")


def load_sources() -> dict:
    config_path = PROJECT_ROOT / "config" / "sources.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Miniflux 导入 ───────────────────────────────────────
def get_miniflux_client() -> httpx.Client:
    api_key = os.getenv("MINIFLUX_API_KEY", "")
    if not api_key:
        console.print("[red]MINIFLUX_API_KEY 未设置[/red]")
        sys.exit(1)
    return httpx.Client(
        base_url=MINIFLUX_BASE,
        headers={"X-Auth-Token": api_key},
        timeout=30.0,
    )


def get_existing_feeds(client: httpx.Client) -> list[dict]:
    """获取 Miniflux 中已有的订阅源"""
    try:
        r = client.get("/v1/feeds")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        console.print(f"[red]无法获取已有 feeds: {e}[/red]")
        return []


def get_or_create_category(client: httpx.Client, title: str) -> int:
    """获取或创建分类，返回 category_id"""
    try:
        r = client.get("/v1/categories")
        r.raise_for_status()
        categories = r.json()
        for cat in categories:
            if cat["title"] == title:
                return cat["id"]
    except Exception:
        pass

    # 创建新分类
    try:
        r = client.post("/v1/categories", json={"title": title})
        r.raise_for_status()
        return r.json()["id"]
    except Exception as e:
        console.print(f"[yellow]无法创建分类 '{title}': {e}[/yellow]")
        return 0


def add_feed_to_miniflux(
    client: httpx.Client,
    url: str,
    title: str,
    category_id: int,
    existing_urls: set,
    dry_run: bool,
) -> bool:
    """添加一个 feed 到 Miniflux"""
    if url in existing_urls:
        console.print(f"  [dim]跳过 (已存在): {title}[/dim]")
        return False

    if dry_run:
        console.print(f"  [blue]将导入: {title}[/blue]")
        return True

    try:
        payload = {
            "feed_url": url,
            "title": title,
            "category_id": category_id,
        }
        r = client.post("/v1/feeds", json=payload)
        if r.status_code == 201:
            console.print(f"  [green]✓ 已导入: {title}[/green]")
            return True
        else:
            console.print(f"  [red]✗ 导入失败 {title}: {r.text}[/red]")
            return False
    except Exception as e:
        console.print(f"  [red]✗ 导入失败 {title}: {e}[/red]")
        return False


def import_to_miniflux(sources: dict, dry_run: bool = False, force: bool = False):
    """导入所有 RSS 源到 Miniflux"""
    console.print("\n[bold cyan]━━━ 导入 RSS 源到 Miniflux ━━━[/bold cyan]")

    client = get_miniflux_client()
    existing_feeds = get_existing_feeds(client)
    existing_urls = set(f["feed_url"] for f in existing_feeds) if not force else set()

    console.print(f"  已有 {len(existing_feeds)} 个订阅源\n")

    imported = 0
    skipped = 0

    # 1. RSSHub 路由
    rsshub_routes = sources.get("rsshub_routes", [])
    for group in rsshub_routes:
        category = group.get("category", "未分类")
        routes = group.get("routes", [])
        cat_id = get_or_create_category(client, category) if not dry_run else 0

        console.print(f"[bold]{category}[/bold] (RSSHub)")

        for route in routes:
            url = f"{RSSHUB_INTERNAL}{route}"
            # 从路由生成一个友好的名字
            title = route.strip("/").replace("/", " · ")
            if add_feed_to_miniflux(client, url, title, cat_id, existing_urls, dry_run):
                imported += 1
            else:
                skipped += 1

    # 2. 直接 RSS 源
    direct_feeds = sources.get("direct_feeds", [])
    for feed_cfg in direct_feeds:
        name = feed_cfg.get("name", feed_cfg["url"])
        url = feed_cfg["url"]
        category = feed_cfg.get("category", "未分类")
        cat_id = get_or_create_category(client, category) if not dry_run else 0

        if add_feed_to_miniflux(client, url, name, cat_id, existing_urls, dry_run):
            imported += 1
        else:
            skipped += 1

    console.print(f"\n  导入 {imported} 个，跳过 {skipped} 个")


# ── changedetection.io 导入 ─────────────────────────────
def get_changedetection_client() -> httpx.Client:
    api_key = os.getenv("CHANGEDETECTION_API_KEY", "")
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    return httpx.Client(
        base_url=CHANGEDETECTION_BASE,
        headers=headers,
        timeout=30.0,
    )


def get_existing_watches(client: httpx.Client) -> set:
    """获取已有的监控目标 URL"""
    try:
        r = client.get("/api/v1/watch")
        r.raise_for_status()
        watches = r.json()
        return set(w.get("url", "") for w in watches)
    except Exception as e:
        console.print(f"[red]无法获取已有 watches: {e}[/red]")
        return set()


def import_to_changedetection(sources: dict, dry_run: bool = False, force: bool = False):
    """导入页面监控目标到 changedetection.io"""
    console.print("\n[bold cyan]━━━ 导入监控目标到 changedetection.io ━━━[/bold cyan]")

    client = get_changedetection_client()
    existing_urls = get_existing_watches(client) if not force else set()

    console.print(f"  已有 {len(existing_urls)} 个监控目标\n")

    watch_targets = sources.get("watch_targets", [])
    imported = 0

    for watch in watch_targets:
        name = watch["name"]
        url = watch["url"]
        css_selector = watch.get("css_selector", "")

        if url in existing_urls:
            console.print(f"  [dim]跳过 (已存在): {name}[/dim]")
            continue

        if dry_run:
            console.print(f"  [blue]将添加: {name} ({url})[/blue]")
            imported += 1
            continue

        try:
            payload = {
                "url": url,
                "title": name,
                "tags": watch.get("tags", []),
            }
            if css_selector:
                payload["css_filter"] = css_selector

            r = client.post("/api/v1/watch", json=payload)
            if r.status_code in (200, 201):
                console.print(f"  [green]✓ 已添加: {name}[/green]")
                imported += 1
            else:
                console.print(f"  [red]✗ 添加失败 {name}: {r.text}[/red]")
        except Exception as e:
            console.print(f"  [red]✗ 添加失败 {name}: {e}[/red]")

    console.print(f"\n  导入 {imported} 个")


# ── 入口 ────────────────────────────────────────────────
def main():
    import argparse

    parser = argparse.ArgumentParser(description="trend_grab 批量导入订阅源")
    parser.add_argument("--dry-run", action="store_true", help="预览，不实际执行")
    parser.add_argument("--force", action="store_true", help="强制重新导入（忽略已存在检查）")
    parser.add_argument("--miniflux-only", action="store_true", help="仅导入 Miniflux")
    parser.add_argument("--changedetection-only", action="store_true", help="仅导入 changedetection.io")
    args = parser.parse_args()

    sources = load_sources()

    console.print(f"[bold]RSSHub:[/bold] {RSSHUB_BASE}")
    console.print(f"[bold]Miniflux:[/bold] {MINIFLUX_BASE}")
    console.print(f"[bold]changedetection.io:[/bold] {CHANGEDETECTION_BASE}")

    if args.dry_run:
        console.print("\n[yellow]═══ DRY RUN 模式（不会实际修改）═══[/yellow]")

    if not args.changedetection_only:
        import_to_miniflux(sources, dry_run=args.dry_run, force=args.force)

    if not args.miniflux_only:
        import_to_changedetection(sources, dry_run=args.dry_run, force=args.force)

    console.print("\n[bold green]完成！[/bold green]")
    if not args.dry_run:
        console.print("  Miniflux: http://localhost:8080")
        console.print("  changedetection.io: http://localhost:5000")


if __name__ == "__main__":
    main()
