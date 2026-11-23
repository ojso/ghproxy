# 技术方案：GitHub 下载缓存代理

**Feature**: 001-github-cache-proxy
**输入**: `spec.md`、`.specify/memory/constitution.md`
**契约**: `../../contracts/openapi.yaml`（仓库根，唯一事实源）

## 技术上下文

| 项 | 取值 |
| --- | --- |
| 语言 | Python 3.13 |
| Web 框架 | Flask 3.x |
| HTTP 客户端 | requests 2.32+（流式） |
| 测试 | pytest，离线假上游 |
| 部署 | Linux + gunicorn（`-w 1 --threads 16`）或 Docker |
| 存储 | 本地磁盘，无需数据库 |

## Phase 0：调研结论

见 `research.md`。结论：nginx 需要额外组件且运维面更大，因此自研；选 Flask + requests 是因为与团队技术栈一致且依赖最少。

## Phase 1：设计

### 架构

```text
客户端
  │  GET /https://github.com/...
  ▼
Flask 路由 (src/ghproxy/app.py)
  ├─ before_request：来源网段校验
  ├─ 目标解析 + 白名单（src/ghproxy/proxy.py）
  ├─ 命中 → send_file(conditional=True)  ← 处理 Range/HEAD/ETag
  └─ 未命中 → 按 URL 加锁（src/ghproxy/cache.py）
                └─ 上游请求（src/ghproxy/proxy.py，手动跟随 302）
                     └─ 边写临时文件边回传（hash 同时计算）
                          └─ 完成后原子 rename + 写 meta（src/ghproxy/cache.py）
```

### 模块职责

| 模块 | 职责 |
| --- | --- |
| `src/ghproxy/app.py` | 路由、缓存决策、流式响应、`/_health`、`/_stats` |
| `src/ghproxy/config.py` | 配置解析（env + flag）、容量与 TTL 解析、白名单集合 |
| `src/ghproxy/proxy.py` | 目标解析、白名单、重定向跟随、上游请求构造 |
| `src/ghproxy/cache.py` | 磁盘布局、元数据、LRU 淘汰、按 URL 加锁、统计 |
| `src/ghproxy/wsgi.py` | gunicorn 入口 |

### 关键决策

1. **前缀式 URL**：mise 的 `url_replacements` 只做字符串替换，前缀式无需客户端支持自定义 Host。
2. **缓存键 = sha256(原始 URL)**：与重定向后的地址无关，保证同一 Release 链接稳定命中。
3. **手动跟随重定向**：`requests` 默认跟随，但无法校验每跳主机；改为 `allow_redirects=False` 循环，逐跳校验白名单，防止 SSRF。
4. **流式落盘**：`iter_content` 边写客户端边写临时文件，避免大文件占内存；完成后 `os.replace` 原子替换。
5. **命中用 `send_file(conditional=True)`**：Range、If-Range、ETag、HEAD 交给 Werkzeug，避免手写字节切片。
6. **单进程加锁**：按 URL 的锁保证并发只回源一次；因此部署约定 `-w 1`。

### 数据模型

见 `data-model.md`。

### 接口

见 `../../contracts/openapi.yaml`。本文与 `quickstart.md` 只引用，不复制字段表。

## Phase 2：任务

见 `tasks.md`。

## 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 被当成开放代理滥用 | 默认只听 127.0.0.1；局域网需 CIDR；域名白名单 |
| 客户端断开导致脏缓存 | 未完成时不提交 meta；保留半成品供下次按 `Range` 续传，校验信息不一致则重下 |
| 大文件撑爆磁盘 | 单对象上限 4GiB + LRU 淘汰到 90% |
| GitHub 更换 Release 域名 | 白名单已含 `release-assets.githubusercontent.com`，缺失时可通过 `EXTRA_HOSTS` 追加 |
| 多 worker 各自缓存 | 部署固定单 worker 多线程 |

## 部署

`deploy/` 提供 Docker 与 systemd 两种形态；生产建议放在 Linux 拉取机，绑定内网地址，前置反代若需 HTTPS 由反代承担。
