# astrbot_plugin_hltv

AstrBot 插件：在聊天里查询 [HLTV](https://www.hltv.org/)（CS2 赛事数据）。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/hltv today` | 今日赛程（直播中优先；过滤后为空时自动回退显示全部） |
| `/hltv matches [天数]` | 近期大赛（默认 1 天，上限 7 天；星级门槛见配置） |
| `/hltv live` | 正在进行的比赛（默认只看大赛，带地图/当前局比分，延迟场次单列） |
| `/hltv live <队名>` | 只看该队（模糊匹配，如 `100t`；未开打则显示今日几点开赛） |
| `/hltv results [天数]` | 近期赛果（不做星级过滤） |
| `/hltv ranking` | **Valve VRS 排名**（默认全球，V社积分） |
| `/hltv ranking asia\|europe\|americas` | 地区 VRS 排名（可用中文：亚洲/欧洲/美洲） |
| `/hltv ranking hltv` | HLTV 自家世界排名 |
| `/hltv events` | 近期赛事 |
| `/hltv team <名称>` | 战队信息：VRS/HLTV 双排名、阵容+国旗、教练、年龄、奖杯、近期战绩 |
| `/hltv player <昵称>` | 选手信息（任意选手） |
| `/hltv news` | 今日新闻（编号列表，自动中文翻译） |
| `/hltv news <序号>` | 新闻详情（正文摘要+翻译+原文链接） |
| `/hltv sub` / `unsub` | 在本会话订阅/退订每日赛程推送 |
| `/hltv help` | 帮助 |

所有子指令支持中文别名：`今日`、`比赛`、`直播`、`赛果`、`排名`、`赛事`、`战队`、
`选手`、`新闻`、`订阅`、`退订`、`帮助`。直接发 `/hltv` 会显示指令菜单。

## 配置

在 AstrBot WebUI 的插件管理页配置（对应 `_conf_schema.json`）：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `min_stars` | 1 | 比赛星级门槛（0-5）。过滤后为空会自动回退显示全部场次 |
| `event_keywords` | 空 | 大赛关键词白名单（如 Major、IEM、BLAST），留空不启用 |
| `translate_news` | 开 | 新闻标题/详情翻译成中文（微软翻译免费 Edge 通道，失败自动回退原文） |
| `enable_push` | 关 | 每日定时推送总开关（修改后需重载插件） |
| `push_time` | 09:00 | 每日推送时间（HH:MM，按 timezone 时区） |
| `push_sessions` | 空 | 订阅会话列表（用 `/hltv sub` 自动登记） |
| `default_days` | 1 | `matches` / `results` 缺省天数 |
| `max_items` | 10 | 列表类结果最多显示条数 |
| `send_waiting_tip` | 关 | 查询前发"正在查询"提示（缓存命中时不发） |
| `timezone` | Asia/Shanghai | 时间显示/今天判定/推送时间的时区 |
| `proxy_list` | 空 | 代理列表，如 `http://127.0.0.1:7890` |
| `timeout` | 15 | 单次请求超时（秒） |
| `max_retries` | 3 | 失败重试次数 |
| `cache_ttl` | 300 | 结果缓存秒数（比赛列表单独 ≤60 秒保证直播时效）；0 关闭 |

## 架构

```
main.py             指令层：/hltv 指令组、参数解析、翻译/推送编排
core/client.py      数据层：自建解析器 + hltv-async-api 混合；
                    TTL 缓存 + 全局锁串行化（30s 排队超时快速失败）
core/formatter.py   展示层：数据 → 文本；防御式取值 + 哨兵值归一化
core/translator.py  微软翻译（免费 Edge 通道，失败回退原文）
```

### 数据源分工（2026-07 实测各页面后确定）

| 页面 | 方式 | 原因 |
| --- | --- | --- |
| matches | **自建解析** | HLTV 已改版，库 0.8.3 选择器全部失效；新版 data-* 属性（match-id/stars/live/unix 时间戳）更稳 |
| team | **自建解析** | HLTV 新增 "Valve ranking" 行，库按位置取值整体错位（年龄显示成 Top30 周数） |
| VRS 排名 | **自建解析** | 新功能，HLTV 站内 `/valve-ranking/` 页面 |
| HLTV 排名 | **自建解析** | 与 VRS 共用 `.ranked-team` 结构 |
| 新闻 | **自建解析** | 保留完整文章链接供详情查看、不丢头条 |
| results / events / player | 库 | 实测选择器健在 |
| 传输层 | 库 `_fetch` | 统一复用其重试/代理轮换/Cloudflare 检测 |

## 已知限制

1. **数据来源是网页解析**，HLTV 改版可能导致失效；自建解析器优先用 data-*
   属性，比 class 选择器更耐改版。请求已做缓存 + 串行化 + 排队超时，
   仍建议配代理、避免高频调用。
2. **搜索接口风控较严**：任意战队/选手查询走 `/search?term=` JSON 接口，
   已做多次重试 + 代理轮换 + 库传输层兜底，选手查询另有选手榜回退。
3. 依赖 `hltv-async-api~=0.8.3` 的**私有方法 `_fetch`** 作传输层；
   该库 README 与源码严重不符（方法名/字段名/默认值），升级前必须核对源码。
4. 翻译走微软 Edge 免费通道，无需配 key；接口失效时自动显示英文原文。

## 开发

```bash
# 放到 AstrBot 的插件目录下
data/plugins/astrbot_plugin_hltv/
pip install -r requirements.txt
```

解析器离线测试样本与脚本见仓库外的开发环境（真实 DOM 片段验证）。
