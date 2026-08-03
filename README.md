# trend_grab — 轻量行业信息监控 + AI 综述

三种模式，覆盖从日常监控到深度研究：

| 模式 | 命令 | 输出 |
|------|------|------|
| **日常监控** | `python scripts/aggregate.py` | 通用新闻综述 |
| **行业研究** | `python scripts/research.py 行业名` | 行业深度分析报告 |

## 架构

```
RSSHub (抓取无RSS源) ──→ Miniflux (RSS聚合器) ──→ aggregate.py ──→ Claude → 日报
changedetection.io (页面监控) ──────────────────────→ aggregate.py ──┘

research.py + WebSearch + WebFetch ──→ Claude ──→ 行业深度报告
```

| 工具 | 端口 | 作用 |
|------|------|------|
| **RSSHub** | 1200 | 给没有 RSS 的网站生成 RSS（微博、知乎、GitHub…） |
| **Miniflux** | 8080 | RSS 阅读器，聚合所有订阅源 + 全文抓取 |
| **changedetection.io** | 5000 | 监控网页变化（政策页面、竞品官网） |

## 快速开始

### 1. 启动服务

```powershell
cd e:\trend_grab
docker compose up -d
```

等待约 30 秒，确认所有容器健康：

```powershell
docker compose ps
```

### 2. 获取 Miniflux API Key

1. 打开 http://localhost:8080
2. 用 `.env` 中配置的用户名密码登录
3. 进入 **Settings → API Keys** → 创建一个 API Key
4. 把 Key 填入 `.env` 的 `MINIFLUX_API_KEY=` 字段

### 3. 导入监控源

```powershell
pip install -r scripts/requirements.txt

# 预览要导入的源（不实际执行）
python scripts/import_feeds.py --dry-run

# 确认无误后执行导入
python scripts/import_feeds.py
```

### 4. 采集数据 & 生成报告

```powershell
# 采集最近 24 小时的数据
python scripts/aggregate.py

# 输出: output/daily_raw.json
```

### 5. 让 Claude 写文章

把 `output/daily_raw.json` 的内容发给 Claude：

> 读取 output/daily_raw.json，写一篇行业综述。结构：
> - 本期摘要（3 个要点）
> - 热点事件详析
> - 分类动态
> - 趋势观察
> - 下周关注

Claude 会分析数据、爬取关键原文、写出 `output/daily_report.md`。

## 配置

编辑 `config/sources.yaml` 来定制监控内容：

```yaml
# RSSHub 路由（自动生成 RSS）
rsshub_routes:
  - category: "科技资讯"
    routes:
      - "/36kr/newsflashes"
      - "/zhihu/daily"

# 直接 RSS 源（已有 RSS 的网站）
direct_feeds:
  - name: "36氪"
    url: "https://36kr.com/feed"
    category: "科技资讯"

# 页面变化监控（没有 RSS 的重要页面）
watch_targets:
  - name: "工信部政策"
    url: "https://www.miit.gov.cn/zwgk/zcwj/"
    css_selector: ".pageContent"
```

## 常用命令

```powershell
# ── 日常监控 ──
python scripts/aggregate.py              # 采集最近 24h 数据
python scripts/aggregate.py --hours 48   # 采集 48h
python scripts/aggregate.py --dry-run    # 测试 API 连通性

# ── 行业研究 ──
python scripts/research.py 解压玩具                  # 生成研究简报 + 模板
python scripts/research.py "3D打印饰品" --no-rss     # 跳过 RSS 匹配（更快）
python scripts/research.py 平价饰品 --output brief    # 只生成简报

# ── 导入源 ──
python scripts/import_feeds.py --dry-run   # 预览
python scripts/import_feeds.py             # 执行

# ── Docker ──
docker compose restart miniflux
docker compose logs -f rsshub
```

### 行业研究工作流

```
1. python scripts/research.py 解压玩具     → 生成研究简报
2. 把 output/industry_brief_解压玩具.json 发给 Claude
3. Claude 自动：5维度 WebSearch → WebFetch 深读 → 写报告
4. 报告保存到 output/industry_report_解压玩具.md
```

报告涵盖 5 个板块：
- 行业概述（定义、市场规模、产业链）
- 发展路径（时间线 + 关键节点）
- 近期热点（3-6个月动态）
- 竞争格局（玩家分层 + 商业模式）
- 趋势预测（短期/中期 + 风险提示）

## 目录结构

```
e:\trend_grab\
├── docker-compose.yml        # 4 容器编排
├── .env                      # 密钥（不提交）
├── config/
│   └── sources.yaml          # 监控源配置
├── scripts/
│   ├── aggregate.py          # 数据聚合
│   ├── import_feeds.py       # 批量导入源
│   └── requirements.txt
├── output/
│   └── daily_raw.json        # 聚合结果
└── data/                     # Docker volumes
```
