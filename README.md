# 品牌舆情监控系统

项目目标是一套本地运行的品牌舆情巡检桌面应用。应用管理品牌、关键词和平台
账号，在后台定时搜索抖音与小红书，对公开内容进行规则或大模型辅助研判，并在
应用内形成舆情台账和播报。

当前仓库由已经验收的浏览器采集 POC 向桌面应用演进，现阶段能力包括：

- 使用平台账号对应的独立 Chrome/Playwright 用户档案手动登录抖音和小红书；
- 后续运行复用登录态，不保存或导出明文 Cookie；
- 初始搜索“速探长、优速卖、配达人”，并支持为品牌配置更多关键词；
- 抽取公开内容链接和标题，并写入 SQLite；
- 使用平台内容 ID 去重；
- 对采集结果进行保守的规则判定、风险分级并生成待复核队列；
- 登录失效、验证码和访问频繁时暂停并提示人工处理。

产品需求见 [`docs/PRODUCT.md`](docs/PRODUCT.md)，技术架构与分阶段边界见
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 当前状态

- 已完成：Git 仓库、Python 项目骨架、品牌增删改查、持久化浏览器档案、
  抖音/小红书真实登录与搜索、详情快照、SQLite 去重入库和基础测试。
- 已验证：速探长、优速卖、配达人在抖音和小红书均可完成搜索、标题抽取、
  详情快照和去重入库；两个平台的一级评论均有真实入库样本。
- 已验证：登录失效和平台滑块验证可以识别并交给人工处理；浅层复扫不会覆盖
  已保存的详情与评论快照。
- 已完成：扫描运行记录、关键词级尝试记录、通用错误有限重试、运行异常告警和
  前台定时循环；桌面端支持每日一次、每周一次或按分钟间隔配置。
- 已完成：规则判定、P0-P3 风险分级、人工复核队列和人工结论保护；每轮扫描
  成功后自动判定新增及更新内容。
- 已完成：搜索卡片先写入巡检候选台账，再进行规则/大模型筛选；普通内容保留过滤
  原因，只有入选内容进入详情采集和正式内容库。单轮大模型调用共享预算，图片接口
  不兼容时自动回退为纯文本请求。
- 当前“速探长、优速卖、配达人”同时作为品牌名和主体公司名称，以精确关键词
  搜索；后续品牌仍通过数据库增删改，不写死在采集器中。
- 待完善：相似事件聚类、定期简报、系统托盘和安装包。
- 当前采集依赖平台页面结构；选择器失效、验证码或访问频率限制均需要人工介入。
- 已完成桌面基础版：6个桌面页面、品牌多关键词管理、多平台账号记录、账号独立
  自动巡检登录档案、应用内舆情中心、人工复核、未读播报和应用内定时巡检。
- 已完成企微智能机器人日报基础能力：定时巡检当天首次成功后生成一条日报，
  通过 Bot ID、Secret 和目标群聊 ID 使用 WebSocket 长连接发送 Markdown 消息；
  Secret 保存在 Windows 凭据管理器中，不写入 SQLite、源码或日志；日报按日期原子
  认领，避免并发重复发送。
- 已完成：SQLite 版本化迁移、巡检计数字段修复、跨进程巡检租约和中断任务恢复；
  初始化不会重新启用用户停用的品牌/账号或补回已删除的品牌同名关键词。
- 自动巡检现在按“平台账号”选择已登录的独立 Playwright 档案；旧版 Qt 登录档案
  不会被复制或导出，首次切换到新流程的账号需要重新登录一次。
- 明确不做：自动填写或提交平台举报；大模型只提供辅助复判，不替代人工结论。

## 环境

- Python 3.12
- uv
- 已安装的 Google Chrome

## 桌面应用

安装桌面依赖并启动：

```powershell
uv sync --dev --extra desktop
uv run opinion-watch init
uv run opinion-watch-desktop
```

桌面应用以“自动巡检”为首页，包含自动巡检、品牌与关键词、平台账号、舆情中心、
应用内播报和设置六个页面。首页使用巡检时间线呈现运行历史；没有业务数据时显示真实
空状态，不填充演示内容。最小启动检查：

```powershell
uv run opinion-watch-desktop --smoke-test
```

在“平台账号”中新建账号后点击“打开自动巡检浏览器”。应用会打开与自动巡检一致的
独立 Chrome 档案，完成登录后点击“登录完成并检查”。每个账号的数据存储在
`runtime/browser-profiles/<platform>/<account-id>/`，账号备注重命名不会改变档案路径。

桌面应用最小化后仍保持定时器运行；关闭应用后停止。“立即巡检”和定时巡检会先检查
账号登录态，再调用已验收的 Playwright 采集器。完整验收步骤见 [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md)。

正式启用前可检查业务数据数量，并在确认后清空舆情、巡检、告警和播报数据。品牌、
关键词、账号记录和浏览器登录档案不会被删除；重置前会自动备份数据库：

```powershell
uv run opinion-watch data status
uv run opinion-watch data reset --confirm RESET
```

## 安装

```powershell
uv sync --dev
uv run opinion-watch init
uv run opinion-watch doctor
```

初始化会创建 `runtime/`，并写入三个初始品牌。浏览器档案、数据库和诊断截图均已加入 `.gitignore`。

## 首次登录

关闭使用同一专用档案的其他 Chrome 进程，然后执行：

```powershell
uv run opinion-watch login --platform douyin
uv run opinion-watch login --platform xiaohongshu
```

命令会打开真实 Chrome。完成扫码或账号登录后，回到终端按回车保存登录状态并关闭浏览器。

## 检查登录态

```powershell
uv run opinion-watch check --platform douyin
uv run opinion-watch check --platform xiaohongshu
```

## 运行采集

采集单个平台、单个关键词：

```powershell
uv run opinion-watch search --platform douyin --keyword 速探长 --limit 20
```

顺序扫描全部启用品牌和两个平台：

```powershell
uv run opinion-watch scan --mode quick
```

深度扫描每个平台、每个关键词至少检索50条：

```powershell
uv run opinion-watch scan --mode deep
```

快速巡检每个平台、每个关键词至少检索20条，深度巡检至少检索50条。系统先保存
搜索结果轻量候选并进行规则/大模型初筛，只对入选的疑似舆情打开详情页；普通内容会
保留过滤原因但不会进入正式内容库。扫描默认对通用页面错误重试1次，品牌之间等待3秒。登录失效、验证码和限流不会
立即重试，而是生成告警并等待人工处理。可按需调整：

```powershell
uv run opinion-watch scan --mode quick --retries 2 `
  --retry-delay-seconds 10 --brand-delay-seconds 5
```

`--detail-limit` 现在表示疑似内容的详情调查上限；不指定时快速巡检为20、深度巡检为50，
`--comments-limit` 控制每条详情读取的一级评论数量。详情页会记录图片和视频关键帧证据，
并在配置的大模型支持多模态消息时一并提交。默认使用有界面浏览器，POC阶段不建议使用
`--headless`。

扫描成功后会自动运行规则判定。规则只生成“疑似”结论，不作为事实或法律定性，
也不会自动提交平台举报。

## 舆情判定与人工复核

对已有内容执行或重新执行规则判定：

```powershell
uv run opinion-watch classify run --limit 100
uv run opinion-watch classify run --limit 100 --force
```

查看全部结果、待复核队列或单条详情：

```powershell
uv run opinion-watch classify list --limit 100
uv run opinion-watch classify list --needs-review
uv run opinion-watch classify list --severity P1
uv run opinion-watch classify show 7
```

人工确认后可修订分类和等级：

```powershell
uv run opinion-watch classify review 7 `
  --category reasonable_consumer_complaint --severity P2 `
  --note "消费者描述了具体订单和售后问题" --reviewer 张三
```

当前分类包括：疑似虚假信息、疑似恶意诽谤、集中投诉、疑似水军攻击、合理消费者
投诉、普通吐槽、无关内容和其他。风险等级含义如下：

- `P0`：重大危机，当前只允许人工确认；
- `P1`：高风险疑似内容，必须人工复核；
- `P2`：明确投诉或需要跟进的业务问题，必须人工复核；
- `P3`：普通吐槽、低风险或信息不足。

若可提取文本中没有出现品牌精确名称，系统不会直接判为无关，而是按 `P3/其他`
进入人工复核，以覆盖文字位于图片或视频中的情况。人工结论保存为 `manual` 来源，
后续自动扫描和 `--force` 均不会覆盖。

## 大模型辅助研判

大模型是可选的第二阶段能力。系统先用本地规则筛选高风险或需要复核的内容，再调用
兼容 OpenAI `chat/completions` 协议的接口进行类型、风险、摘要和证据复判；人工结论
不会被覆盖。未配置或关闭时，巡检仍完整使用规则分类。

在桌面应用“设置”的“大模型辅助研判”中填写提供商、Base URL、模型名和 API Key，
并选择每轮最多复判的条数。API Key 只保存到 Windows 凭据管理器，不写入 SQLite、
日志、源码或 Git。可用以下命令测试当前配置：

```powershell
uv run opinion-watch llm test
```

## 定时扫描

以前台进程每30分钟运行一次：

```powershell
uv run opinion-watch watch --interval-minutes 30
```

验证单轮定时任务后退出：

```powershell
uv run opinion-watch watch --interval-minutes 30 --max-runs 1 --mode quick
```

`watch` 不会并发启动下一轮，当前轮次结束后才开始计算等待时间。它是前台进程；
终端关闭后任务会停止。生产环境后续应由 Windows 服务、任务计划程序或容器托管。

桌面端“自动巡检”页面支持设置每日执行时间、每周执行日，或切换为按分钟间隔。
“巡检动态”和“舆情中心”的巡检批次支持编辑标题/备注和删除批次。删除批次只清理
该批次的执行明细与关联关系，已采集内容仍保留在“全部历史”中。

## 运行历史与告警

```powershell
uv run opinion-watch run list --limit 20
uv run opinion-watch run show 1
uv run opinion-watch alert list
uv run opinion-watch alert list --all
uv run opinion-watch alert ack 1
```

每次 `scan` 或 `watch` 轮次都会保存运行汇总；每个平台和品牌的每次尝试会单独
记录采集量、入库量、错误状态及诊断截图。默认告警列表只显示尚未确认的告警。
巡检成功采集的内容会关联到对应运行批次；“舆情中心”默认优先显示最近批次，
也可以切换“全部历史”，并查看首次发现时间、最近发现时间和判定来源。

## 品牌管理

```powershell
uv run opinion-watch brand list
uv run opinion-watch brand add 新品牌
uv run opinion-watch brand rename 新品牌 新品牌名
uv run opinion-watch brand disable 新品牌名
uv run opinion-watch brand enable 新品牌名
uv run opinion-watch brand delete 新品牌名
```

## 关键词、账号与应用内通知

```powershell
uv run opinion-watch keyword list
uv run opinion-watch keyword add 速探长 速探长物流
uv run opinion-watch keyword rename <keyword-id> 新关键词
uv run opinion-watch keyword disable <keyword-id>

uv run opinion-watch account list
uv run opinion-watch account add --platform douyin 运营一号
uv run opinion-watch notification list
uv run opinion-watch notification read <notification-id>
```

新建品牌时自动添加同名关键词；品牌名与检索关键词分开保存。巡检遍历所有启用品牌
的启用关键词，采集内容仍归属到对应品牌。

## 企微智能机器人日报

在桌面应用“设置”中填写 Bot ID、Secret 和目标群聊 ID，保存后可发送测试消息。
Secret 仅写入本机 Windows 凭据管理器；群聊 ID 是企微智能机器人回调消息中的
`chatid`，Bot 必须已经加入目标群聊。若暂时不知道群聊 ID，点击“监听群聊 ID”，
再在目标群聊中 `@机器人` 发送任意消息，应用会自动回填。也可以使用命令行监听：

```powershell
uv run opinion-watch wecom discover --timeout-seconds 120
uv run opinion-watch wecom test
```

日报仅在 `watch` 或桌面端定时巡检触发的当天首次成功/部分成功巡检后发送一次；
同一天后续巡检不会重复发送。手动“立即巡检”不会触发日报发送。

## 安全与运行约束

- 每个平台使用独立的 `runtime/browser-profiles/<platform>/` 档案。
- 不要把日常使用的 Chrome 默认用户目录复制进项目。
- 同一平台档案不得被两个进程同时打开。
- 采集器只读取当前浏览器页面公开呈现的内容，不绕过验证码。
- 出现验证码、二次验证或访问频繁时，采集器会停止当前任务。
- 页面结构随时可能变化，平台选择器集中在对应适配器中维护。

## 开发检查

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
