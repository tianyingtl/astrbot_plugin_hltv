# astrbot_plugin_hltv

AstrBot 插件：在聊天里查询 [HLTV](https://www.hltv.org/)（CS2 赛事数据）。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/hltv today` | 今日赛程（直播中优先，按开赛时间排序） |
| `/hltv matches [天数]` | 近期大赛（默认 1 天，上限 7 天；星级门槛见配置） |
| `/hltv live` | 正在进行的比赛 |
| `/hltv results [天数]` | 近期赛果（默认 1 天） |
| `/hltv ranking` | 战队世界排名 Top50 |
| `/hltv events` | 近期赛事 |
| `/hltv team <名称>` | 战队信息（任意战队：先匹配排名榜，再走站内搜索） |
| `/hltv player <昵称>` | 选手信息（任意选手：走 HLTV 站内搜索） |
| `/hltv news` | 今日新闻 |
| `/hltv help` | 帮助 |

所有子指令支持中文别名：`/hltv 今日`、`/hltv 比赛 3`、`/hltv 直播`、`/hltv 赛果`、
`/hltv 排名`、`/hltv 赛事`、`/hltv 战队 spirit`、`/hltv 选手 donk`、`/hltv 新闻`、`/hltv 帮助`。
直接发 `/hltv` 会显示指令菜单。

## 配置

在 AstrBot WebUI 的插件管理页配置（对应 `_conf_schema.json`）：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `min_stars` | 1 | 比赛星级门槛（0-5）。HLTV 用星级标注比赛重要程度，默认过滤 0 星小比赛；0 = 全部显示，2-3 = 只看顶级对局 |
| `event_keywords` | 空 | 大赛关键词白名单（如 Major、IEM、BLAST、ESL、PGL），只显示赛事名含关键词的比赛；留空不启用。对 matches / today 生效 |
| `default_days` | 1 | `matches` / `results` 不带参数时默认查询的天数（1-7） |
| `max_items` | 10 | 列表类结果最多显示条数 |
| `send_waiting_tip` | 关 | 查询前先发一条"正在查询"提示（HLTV 爬取慢，开启体验更好但消息多一条） |
| `timezone` | Asia/Shanghai | 比赛时间显示时区（today 的"今天"也按此时区判定） |
| `proxy_list` | 空 | 代理列表，如 `http://127.0.0.1:7890`。HLTV 有 Cloudflare 风控，直连不稳时建议配置 |
| `timeout` | 15 | 单次请求超时（秒） |
| `max_retries` | 3 | 失败重试次数 |
| `cache_ttl` | 300 | 结果缓存秒数，降低请求频率；0 关闭 |

## 架构

```
main.py            指令层：注册 /hltv 指令组、参数解析、错误话术
core/client.py     数据层：封装 hltv-async-api + 自实现站内搜索；
                   TTL 缓存 + 全局锁串行化请求（30s 排队超时快速失败）
core/formatter.py  展示层：原始数据 → 文本；全部 .get() 防御式取值
```

三层单向依赖（指令层 → 数据层 / 展示层），互不越级：

- **换数据源**（自建爬虫、REST 镜像）→ 只改 `client.py`
- **换输出形式**（图片卡片、合并转发）→ 只改 `formatter.py`
- **加指令** → 在 `main.py` 加一个 handler，照现有模式写

## 实现要点 / 已知限制

1. **数据来源是非官方爬虫**。HLTV 无官方 API，`hltv-async-api` 靠解析网页，
   HLTV 改版或 Cloudflare 风控收紧时可能失效。请求已做缓存 + 串行化 +
   排队超时，仍建议不要高频调用，必要时配代理。
2. **任意战队/选手查询**依赖 HLTV 站内搜索接口
   （`/search?term=`，返回 JSON，插件用 aiohttp 自行请求），同样受风控影响；
   选手搜索被拦时自动退回选手榜 Top100 模糊匹配。
3. **直播判定是确定的**：`hltv-async-api` 源码中直播条目的 `date` 字段
   固定为 `'LIVE'`；未开赛条目日期格式为 `DD-MM-YYYY`（已按配置时区本地化），
   `today` 据此过滤。
4. **必须 `safe_mode=False`**：库在 safe_mode 下 `get_matches` / `get_results` /
   `get_top_players` 直接返回 None，client.py 已显式关闭。
5. 各接口返回字段以 [hltv-async-api](https://github.com/akimerslys/hltv-async-api)
   **源码**为准，展示层已做缺字段容错。注意该库 README 与实际代码多处不一致
   （README 写 `get_best_players`/`name`/safe_mode 默认 True，0.8.3 源码实为
   `get_top_players`/`nickname`/默认 False），依赖已锁定 `~=0.8.3`，升级时请核对源码。

## 开发

```bash
# 放到 AstrBot 的插件目录下
data/plugins/astrbot_plugin_hltv/

# 安装依赖（或在 WebUI 插件管理中安装）
pip install -r requirements.txt
```

发布前记得改 `metadata.yaml` 里的 `author` / `repo`。
