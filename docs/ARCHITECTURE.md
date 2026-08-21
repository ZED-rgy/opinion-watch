# 技术架构

## 总体结构

```text
PySide6 桌面应用
├─ 首页与应用内播报
├─ 品牌 / 关键词管理
├─ 平台账号与内置登录浏览器
├─ 舆情中心与人工复核
├─ 定时任务控制
├─ 每日巡检日报生成器
└─ 通知渠道接口（应用内 + 企微智能机器人）
          │
          ▼
Python 应用服务
├─ 巡检调度器
├─ 抖音 / 小红书采集器
├─ 规则分类器
├─ 可选大模型研判接口
├─ 企微智能机器人 WebSocket 通知适配器
└─ SQLite 存储
```

## 技术选择

- 保持 Python 3.12、uv、SQLite、pytest、Ruff。
- 桌面端使用 PySide6 Widgets；账号登录通过可见 Chrome/Playwright 会话完成。
- 每个账号创建独立 Playwright 持久化档案，目录使用稳定账号 ID，不使用账号名。
- 已验收的 Playwright 采集器和账号登录现在复用同一档案，巡检前会主动检查登录态。
- 桌面应用通过后台进程运行巡检，避免阻塞界面。

## 登录态边界

账号登录和自动采集使用同一套 Playwright/Chrome Profile，不复制或导出 Cookie。
账号档案迁移分两步：

1. 桌面端建立平台账号模型和稳定的账号级 Playwright Profile。
2. 登录窗口打开该 Profile，登录完成后由巡检前置检查确认健康状态。

旧版本 Qt 登录档案不会被复制；升级后的账号需要重新登录一次。验证码和二次验证由
用户在对应账号浏览器中处理。

## 数据模型

- `brands`：监控主体。
- `brand_keywords`：品牌的可配置检索关键词。
- `platform_accounts`：平台账号、启用状态和登录健康状态。
- `content_items`：平台内容去重主表。
- `content_matches`：内容与品牌、命中关键词的关系。
- `scan_candidates`：巡检中保存的轻量搜索候选，记录入选/过滤状态和过滤原因，避免
  搜索卡片在详情采集前无迹可查。
- `scan_run_contents`：巡检批次、关键词尝试与内容的关联，用于区分本次与历史数据。
- `opinion_assessments`：规则/模型判断及人工复核结论。
- `scan_runs`、`scan_attempts`、`alerts`：巡检运行和异常记录。
- `app_notifications`：应用内播报、未读和确认状态。
- `wecom_config`：企微 Bot ID、目标群聊 ID、启用状态和 WebSocket 地址；Secret 不存入数据库。
- `daily_reports`：按本地日期保存日报内容、发送状态和失败原因；发送前使用原子认领，
  保证并发任务不会重复发送。
- `llm_config`：大模型提供商、Base URL、模型名、启用状态、单轮候选上限和最近一次能力探测；
  API Key 不存入数据库。
- `schedule_config`：启用状态、每日/每周/间隔频次、执行时间、上次计划时间、下次执行时间
  和错过任务策略。
- `schema_migrations`、`task_leases`：数据库迁移版本和跨进程任务租约。

## 大模型接口

大模型不是巡检前置依赖。未配置时使用关键词和规则；配置后先对轻量搜索候选进行入库前
筛选，再对最终候选进行第二阶段复判：

- 舆情类型与风险复判；
- 简短摘要和模型证据；
- 模型失败时保留规则结论并生成告警；一轮巡检共享调用预算，避免按关键词无限叠加调用。
- 保存配置或测试连接时探测文本和多模态能力，巡检按探测结果选择消息格式；服务商不接受
  `image_url` 消息时自动回退为文本请求；非本机 Base URL 要求 HTTPS。

当前客户端使用兼容 OpenAI `chat/completions` 的 HTTP 接口。API Key 存入 Windows
凭据管理器，不进入 SQLite、日志、配置文件或 Git。相似内容聚类和定期简报仍是后续能力。

## Worker 事件协议

桌面端通过 QProcess 启动巡检 Worker。Worker 的结构化输出使用 JSON Lines，每条事件包含
`version` 和 `type`，当前版本为 `1`；普通日志仍可显示在诊断区域，但不会被当作业务事件解析。
协议实现位于 `opinion_watch.events`，便于未来将桌面启动方式替换为独立 Worker 或本地 IPC。

## 调度边界

调度配置已进入 SQLite，不再只依赖 QSettings。桌面端负责展示和配置，纯计算规则位于
`opinion_watch.scheduling`，启动时会恢复未来的 `next_run_at`；如果发现错过计划，则按
`run_once` 策略补跑一次，再恢复正常频次。CLI `watch` 仍可用于显式前台循环，后续可复用同一
调度服务接入无界面 Worker。

## 通知扩展

领域层只产生标准通知事件。当前企微智能机器人使用 Bot ID + Secret 建立
`wss://openws.work.weixin.qq.com` 长连接，向配置的群聊 `chatid` 主动发送 Markdown
日报；Secret 通过 Windows 凭据管理器保存，不进入 SQLite、日志或 Git。
