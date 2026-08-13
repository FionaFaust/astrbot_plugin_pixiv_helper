# 艾珀莉亚的Pixiv图片小助手

> 作者：艾珀莉亚（FionaFaust）

通过 pixiv.re 镜像图床与 pixivision 官方杂志获取 Pixiv 插画，支持定时推送、按 ID 取图、特辑获取。

## 功能

- **定时推送**：按配置间隔向白名单群聊发送随机抽选的 Pixiv 热门图片
- **按 ID 取图**：`/pixiv 作品ID [页码]` 获取指定插画（支持多页）
- **特辑获取**：`/pixiv 特辑` 获取 pixivision 最新主题特辑插画

## 指令

```
/pixiv 帮助
/pixiv 作品ID        （如 /pixiv 143138124）
/pixiv 作品ID 2      （获取第 2 页）
/pixiv 特辑
```

## 配置说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enable_push` | `true` | 是否开启定时推图 |
| `push_interval` | `3600` | 推送间隔（秒） |
| `push_count` | `15` | 每次推送数量（上限 25） |
| `whitelist_groups` | 空 | 推送目标群聊白名单 |
| `enable_r18` | `false` | 是否允许 R-18 图片 |
| `enable_r18g` | `false` | 是否允许 R-18G 图片 |
| `enable_aigc` | `false` | 是否允许 AIGC/AI 生成图片 |
| `show_info` | `true` | 发送时附带简介（ID/标题/作者） |
| `use_forward` | `true` | 查询功能是否以转发聊天记录（合并转发）方式发送 |
| `push_use_forward` | `true` | 定时推送图片是否整合为一条聊天记录发送 |
| `enable_cache_cleanup` | `true` | 是否定时清理图片缓存 |
| `cache_cleanup_interval` | `3600` | 缓存清理间隔（秒） |

## 抽选池说明

- 抽选池来自 pixivision 特辑（热门/较热门作品），目标约 **300 张**；
- 每天 **0 点与 12 点（UTC+8）** 自动清理抽选池缓存并重新爬取；
- 定时推送按配置数量从抽选池随机抽取。

## 说明

- 图片经 pixiv.re 镜像获取，特辑数据来自 pixivision（均为 Pixiv 热门/较热门作品）；
- R-18/R-18G 受 pixivision 数据源限制（特辑以全年龄为主），开启后从可获取池中尽量包含；
- 所有推送/查询均支持合并转发（QQ OneBot v11）；
- 下载的临时图片按配置定时清理。

---

© 艾珀莉亚 (FionaFaust) · AstrBot 插件
