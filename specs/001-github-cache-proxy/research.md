# 调研记录（Phase 0）

## 目标

把 GitHub 的下载流量收敛到一台内网机器，其余机器从它读取，且**断网后仍可重装**。

## 候选方案

| 方案 | 是否落盘 | 额外组件 | 结论 |
| --- | --- | --- | --- |
| nginx `proxy_cache` | 是 | 需要 nginx 与额外配置 | 可行但运维面更大，且本机无 nginx |
| 自研（Flask + requests） | 是 | 无 | 采纳 |

## 关键事实（实测）

- GitHub Release 下载返回 `302`，目标主机为 `release-assets.githubusercontent.com`，不再是旧文档里的 `objects.githubusercontent.com`。白名单必须包含它，否则真实下载会 502。
- mise 的 `url_replacements` 做的是普通字符串替换，因此代理必须采用**前缀式 URL**（`/https://host/path`）才能直接接入。
- 缓存命中的 Range 请求可以交给 Werkzeug 的 `send_file(conditional=True)`，无需自行切片。

## 未采纳的原因

自研的唯一代价是维护 600 行代码；换来的是可落盘、零额外组件、依赖最少。考虑到“断网可用”是核心目标，这个交换成立。
