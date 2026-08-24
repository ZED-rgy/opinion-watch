# AGENTS.md

## 项目

品牌舆情自动巡检桌面应用。桌面端管理品牌关键词、平台账号、应用内播报和定时任务；
采集层搜索抖音、小红书公开内容并写入本地 SQLite。

## 技术栈

- Python 3.12、uv、PySide6/Qt WebEngine、Playwright、SQLite
- pytest、Ruff

## 常用命令

```powershell
uv sync --dev --extra desktop
uv run opinion-watch init
uv run opinion-watch doctor
uv run opinion-watch login --platform douyin
uv run opinion-watch login --platform xiaohongshu
uv run opinion-watch scan --limit 20
uv run opinion-watch ingest .\agent-results.jsonl
uv run opinion-watch watch --interval-minutes 30 --max-runs 1
uv run opinion-watch run list
uv run opinion-watch alert list
uv run opinion-watch classify run --limit 100
uv run opinion-watch classify list --needs-review
uv run opinion-watch keyword list
uv run opinion-watch account list
uv run opinion-watch notification list
uv run opinion-watch-desktop --smoke-test
uv run opinion-watch-desktop
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## 目录与约束

- `src/opinion_watch/`：应用代码；平台差异集中在 `collectors/`。
- `tests/`：离线单元测试。
- `runtime/`：数据库和浏览器档案；本地敏感运行数据，不得提交。
- `output/playwright/`：诊断截图；不得提交。
- 不导出 Cookie，不记录账号密码，不绕过验证码或平台访问限制。
- 每个平台使用独立持久化档案；同一档案不得被多个进程同时打开。
- 桌面账号档案按“平台 + 稳定账号ID”隔离；删除账号记录不得自动删除档案。
- Qt WebEngine 与 Playwright Profile 不通过复制或导出 Cookie 同步。
- 品牌必须通过数据库管理，不能把后续品牌硬编码进采集器。
- 页面选择器可能变化；修改适配器时同步补充离线解析测试。

## 当前边界

采集 POC 已通过三个品牌、两个平台的真实搜索验收。桌面基础版、品牌多关键词、
多平台账号记录、账号级独立 Playwright 登录档案、舆情中心、人工复核、应用内播报和
定时巡检已实现。账号登录和自动巡检复用同一账号级 Playwright Profile，不复制 Cookie。
大模型辅助筛选、媒体证据、候选留痕、企微机器人日报、巡检租约、事件聚类和系统托盘
已实现；安装包尚未实现。自动举报、申诉及下架跟踪不在当前产品范围。

巡检写入前会保留轻量搜索候选，随后进行规则/大模型筛选，仅对入选内容做详情采集和
入库；模型调用按单轮巡检共享预算限制。SQLite 使用版本化迁移，初始化不应重置用户的
品牌、关键词或账号状态；跨进程巡检通过数据库租约互斥，企微日报按日期原子认领以防重复发送。
外部 Agent 通过 UTF-8 JSONL 导入候选，不能直接写 SQLite；没有详情证据的搜索候选最多
按 P3 待调查线索处理，不得直接生成 P1/P2 舆情播报。
