# AGENTS.md

本文件面向在该仓库工作的 agent，只写“怎么干”和“不能干什么”。

## 环境与依赖

```bash
uv sync
```

运行时依赖只有 `Flask` 和 `requests`，测试依赖 `pytest`。新增依赖需要先说明理由，并通过 `uv add` 更新 `pyproject.toml` 与 `uv.lock`。

## 构建与验证

```bash
uv run pytest                                     # 全部离线，使用本地假上游
uv run python scripts/check_docs.py               # 契约位置、FR 编号与测试映射
uv run python -m ghproxy --addr 127.0.0.1:18080 --cache-dir ./cache
```

测试禁止访问真实网络。需要“上游”时用 `tests/conftest.py` 的 `upstream` fixture（本地 ThreadingHTTPServer），并通过 `EXTRA_HOSTS` 注入其地址。

## 代码边界

- **不要引入新的第三方依赖**。当前只用 Flask + requests，保持这一点。
- `cache/`、`.venv/`、`__pycache__/`、`.pytest_cache/` 不入库。
- `config.DEFAULT_HOSTS` 是安全边界，新增域名必须有明确理由，并在 `testing.md` 补一条用例。
- 不得转发客户端的 `Authorization` / `Cookie` 到上游；这是防止凭据泄露的核心约束。
- 修改 URL 解析或缓存键逻辑时，必须同时更新 `testing.md` 的映射表。

## 文件地图

| 文件 | 职责 |
| --- | --- |
| `src/ghproxy/app.py` | Flask 应用、路由、流式转发、缓存决策 |
| `src/ghproxy/config.py` | 配置解析（env + flag）、容量与 TTL 解析 |
| `src/ghproxy/proxy.py` | 目标解析、上游请求、重定向、白名单 |
| `src/ghproxy/cache.py` | 磁盘缓存、元数据、LRU 淘汰、按 URL 加锁 |
| `src/ghproxy/wsgi.py` | gunicorn 入口 |
| `.specify/memory/constitution.md` | 项目原则，冲突时以此为准 |
| `specs/001-github-cache-proxy/spec.md` | 行为期望（`FR-xxx`），不含实现 |
| `contracts/openapi.yaml` | HTTP 契约唯一事实源（仓库根） |
| `testing.md` | `FR-xxx` → pytest 用例映射 |

## 文档职责边界

- `spec.md`：只写可观察行为。出现类名、库名、函数名即为越界。
- `plan.md`：技术方案与取舍，可以讨论实现。
- `contracts/`：接口形状的唯一事实源，位于仓库根；**禁止在 `specs/*/contracts/` 下新建副本**，feature 目录里只写引用路径。
- `tasks.md`：可执行任务与状态，完成后打勾。

修改行为时：先改 `spec.md` → 再改 `contracts/` → 更新测试 → 最后改代码。

## 契约（改动需同步 README 与 testing.md）

```text
GET|HEAD /https://<host>/<path>   代理下载
DELETE   /https://<host>/<path>   删除该 URL 的缓存
GET      /_health                 {"status":"ok",...}
GET      /_stats                  缓存统计 JSON
GET      /_entries                缓存清单 JSON（含 key）
DELETE   /_entries                清空全部缓存
DELETE   /_entries/<key>          删除单条缓存

响应头 X-Cache: MISS | HIT | BYPASS
```

配置项以 `README.md` 的表格为唯一事实源，不要在两处各写一份。
