# astrbot_plugin_hltv

用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 HLTV 数据查询插件，支持
CS2 赛程、实时比分、赛果、排名、战队、选手、新闻及比赛提醒。

## 功能

- 查询今日赛程、近期比赛、实时比分和历史赛果
- 查询 Valve VRS、HLTV 世界排名和年度 TOP20
- 将 HLTV 数据注册为 AstrBot 大模型工具，普通对话中的 CS 赛事问题会优先查询实时资料后回答
- 查看战队阵容、排名、Major 冠军、奖杯、近期战绩及选手荣誉
- 订阅直播或指定战队的今日待开赛比赛，在开图、逐图结束和整场结束时接收提醒
- 定向查看直播战队时，在一张图片中展示当前地图比分、双方十人实时战绩和本场选图
- 每张地图及整场比赛结束后，以 HLTV 风格表格图片展示双方选手 Rating
- 新闻标题中英双语显示，并提供中文摘要
- 查询结果优先使用图片卡片，渲染失败时自动返回文字
- 支持每日赛程定时推送和群聊免 @ 调用

## 效果预览

### 选手详情

![选手详情卡](docs/images/player-niko.webp)

### 近期赛事

![近期赛事卡](docs/images/recent-matches.webp)

### 实时比分

![实时比分卡](docs/images/live-score.webp)

## 安装

可在 AstrBot WebUI 的插件市场中安装，也可以下载 Release 压缩包后在插件管理页上传。

手动安装：

```bash
cd data/plugins
git clone https://github.com/tianyingtl/astrbot_plugin_hltv.git
cd astrbot_plugin_hltv
pip install -r requirements.txt
```

安装完成后，在 AstrBot WebUI 中重载插件。

## AI 资料查询

插件会向 AstrBot 注册 `query_hltv` 工具。使用支持工具调用的聊天模型时，用户可以直接询问
CS 职业赛事、实时比分、赛程赛果、战队、选手、排名、赛事、新闻或年度 TOP20，无需输入
`/hltv` 指令。机器人会优先查询插件数据，再根据返回资料组织回答。

该接口只向模型返回文字资料，不生成或发送图片卡片。传统 `/hltv` 指令仍按原方式提供图片查询、
直播订阅和比赛提醒。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/hltv today` | 查看今日赛程 |
| `/hltv matches [天数]` | 查看近期比赛，默认 1 天，最多 7 天 |
| `/live` 或 `/hltv live` | 只查看正在进行的比赛，不自动订阅 |
| `/hltv live 1 2 3` | 按直播卡片序号批量订阅比赛 |
| `/hltv live <队名>` | 查看指定战队的当前图实时战绩和本场选图，并订阅比赛 |
| `/hltv live 取消` | 取消当前账号在本会话中的直播订阅 |
| `/hltv results [天数]` | 查看近期赛果 |
| `/hltv ranking` | 查看全球 Valve VRS 排名 |
| `/hltv ranking asia\|europe\|americas` | 查看指定地区的 Valve VRS 排名 |
| `/hltv ranking hltv` | 查看 HLTV 世界排名 |
| `/hltv top20 [年份]` | 查看年度 TOP20；不填年份时查询上一年 |
| `/hltv top20 <年份> <名次>` | 查看指定名次的 HLTV 新闻和个人 TOP 图片 |
| `/hltv events` | 查看近期赛事 |
| `/hltv team <名称>` | 查看战队资料、Major、阵容和近期战绩；支持常用缩写及中文称呼 |
| `/hltv player <昵称>` | 查看选手资料、排名与荣誉 |
| `/hltv news` | 查看今日新闻列表 |
| `/hltv news <序号>` | 查看新闻摘要、翻译和原文链接 |
| `/hltv sub` | 订阅当前会话的每日赛程推送 |
| `/hltv unsub` | 取消当前会话的每日赛程推送 |
| `/hltv help` | 查看指令帮助 |

子指令同时支持中文别名。默认可直接发送 `/hltv ...`，无需 @ 机器人；该行为可通过
`free_wake` 配置关闭。

### 直播与订阅

- 发送 `/hltv live` 只查看当前直播，不会自动订阅。
- 查看直播卡片后，发送 `/hltv live 1 2 3` 可按序号订阅一场或多场比赛。
- 发送 `/hltv live <战队>` 可直接查看该队。比赛已经开始时，一张图片会同时显示当前地图比分、双方十人的实时 `K-D`、正负值、助攻、ADR、KAST，以及本场 BO3/BO5 的地图和状态，并自动订阅比赛。
- 指定战队今天尚未开赛时，会订阅开赛提醒；首图正式开始后自动 @ 你。
- 订阅后会收到新地图开始提醒。每张地图结束时推送该图正式 Rating 表格，整场结束后再推送完赛 Rating。
- 战队名称支持常见缩写和中文称呼，例如 `NAVI`、`FNC`、`NIP`、`蜜蜂`、`猎鹰`、`绿龙`。

## 配置

在 AstrBot WebUI 的插件配置页中修改：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `min_stars` | `1` | 赛程最低星级，范围 0-5 |
| `event_keywords` | 空 | 赛事名称关键词白名单，留空表示不启用 |
| `translate_news` | 开启 | 翻译新闻标题和详情，并保留英文原标题 |
| `live_poll_interval` | `45` | 直播订阅检查间隔，范围 20-300 秒 |
| `enable_push` | 关闭 | 开启每日赛程推送，修改后需重载插件 |
| `push_time` | `09:00` | 每日推送时间 |
| `push_sessions` | 空 | 推送目标会话，通常由订阅指令自动维护 |
| `default_days` | `1` | 赛程和赛果的默认查询天数 |
| `max_items` | `10` | 列表类结果的最大显示数量 |
| `free_wake` | 开启 | 允许无需 @ 直接调用 `/hltv` |
| `send_waiting_tip` | 关闭 | 查询前发送等待提示 |
| `timezone` | `Asia/Shanghai` | 比赛时间、日期和定时推送所用时区 |
| `proxy_list` | 空 | 请求代理列表 |
| `timeout` | `15` | 单次请求超时时间，单位为秒 |
| `max_retries` | `3` | 请求失败后的最大重试次数 |
| `cache_ttl` | `300` | 查询缓存时间，设为 0 可关闭缓存 |

## 使用说明

- 数据来自 HLTV 网站，页面结构调整或访问限制可能影响查询结果。
- 已收录的历史年度 TOP20 优先使用 5E 成品总榜，首次查询下载并校验，后续从本地缓存读取。
- HLTV 对请求频率有限制；连接不稳定时可配置代理，并适当延长缓存时间。
- 新闻翻译依赖第三方翻译服务，服务不可用时会显示英文原文。
- 当前地图实时战绩来自 HLTV scorebot；逐图 Rating 和完赛 Rating 以 HLTV 实际更新时间为准。
- 战队缩写和中文称呼由 `team`、`live` 等战队查询统一识别。
- 图片卡片会从插件内置背景中随机选取，渲染失败时自动回退为文字消息。

本项目与 HLTV 无官方关联，赛事数据及相关素材版权归其原权利人所有。
