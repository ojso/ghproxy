# ghproxy

一个**会落盘**的 GitHub 代理缓存。第一次请求回源下载并写入本地磁盘，之后同一 URL 直接从磁盘返回，不再访问外网。

本项目用 Python + Flask 实现，目标是**离线可用**：内网一台机器负责出网，其余机器（以及 CI）通过它拿工具链和 Release 文件。

## 特性

- **前缀式 URL**：`http://cache.corp:8080/https://github.com/<owner>/<repo>/releases/download/...`
- **服务端跟随 302**：GitHub Release 会跳转到 `release-assets.githubusercontent.com`，由代理自己完成，客户端无感
- **磁盘缓存 + LRU 淘汰**：按 URL 哈希存放，超过上限自动删最久未用的
- **原始字节缓存**：不解压上游响应，`Content-Encoding` / `Content-Length` 原样保留
- **Range / 断点续传**：命中缓存时完整支持 `206 Partial Content`
- **回源断点续传**：下载中断保留半成品，下次从断点向上游续传
- **并发去重**：同一 URL 的并发请求只回源一次
- **白名单**：只允许 GitHub 相关域名，防止被当成开放代理
- **不转发客户端凭据**：`Authorization` / `Cookie` 一律丢弃，私有仓库用服务端 token
- **依赖只有两个**：Flask、requests

## 快速开始

```bash
# 创建环境并安装依赖
uv sync

# 启动
uv run python -m ghproxy --addr 127.0.0.1:8080 --cache-dir ./cache
```

Linux/macOS 同样使用 `uv sync` 和 `uv run python -m ghproxy ...`。

验证：

```bash
curl http://127.0.0.1:8080/_health
curl -O "http://127.0.0.1:8080/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64"
# 响应头第一次是 X-Cache: MISS，第二次 X-Cache: HIT
curl -sI "http://127.0.0.1:8080/https://raw.githubusercontent.com/github/gitignore/main/Go.gitignore" | grep -i x-cache
```

## 接到自己的工具链上

### mise

```toml
# ~/.config/mise/config.toml
[settings.url_replacements]
"https://github.com/" = "http://cache.corp:8080/https://github.com/"
"https://release-assets.githubusercontent.com/" = "http://cache.corp:8080/https://release-assets.githubusercontent.com/"
```

因为本项目是标准前缀式，普通字符串替换就能接上。

> 如果反代挂在 `/ghproxy/` 前缀下并启用 HTTPS（见 `deploy/README.md`），把上面的 `http://cache.corp:8080/` 换成 `https://cache.corp/ghproxy/` 即可。

### 安装脚本

`curl ... | bash` 这类脚本里的 URL 是硬编码的，改环境变量无效，需要先把脚本落地再替换地址：

```bash
curl -fsSL https://opencode.ai/install -o install.sh
sed -i 's#https://github.com/anomalyco/opencode#http://cache.corp:8080/https://github.com/anomalyco/opencode#g' install.sh
bash install.sh
```

或者直接用官方提供的 `--binary <path>` 参数，跳过下载。

## 配置

| 环境变量 | 命令行 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ADDR` | `--addr` | `127.0.0.1:8080` | 监听地址，局域网共享改成 `0.0.0.0:8080` |
| `CACHE_DIR` | `--cache-dir` | `./cache` | 缓存目录 |
| `MAX_CACHE_BYTES` | `--max-cache` | `20GiB` | 总容量上限 |
| `MAX_OBJECT_BYTES` | `--max-object` | `4GiB` | 单个对象超过此值只转发不缓存 |
| `CACHE_TTL` | `--ttl` | `720h` | 条目有效期（支持 `s/m/h/d`） |
| `ALLOW_CIDRS` | `--allow-cidrs` | 空（不限制） | 允许访问的网段，如 `10.0.0.0/8` |
| `EXTRA_HOSTS` | `--extra-hosts` | 空 | 追加允许的上游域名 |
| `UPSTREAM_TOKEN` | `--upstream-token` | 空 | 服务端访问 GitHub 的 token（私有仓库）；仅通过 HTTPS 发给 `github.com` / `api.github.com` / `codeload.github.com` |

上游连接沿用系统的 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`。

> `ADDR` 只影响 `python -m ghproxy` 的监听地址。用 gunicorn 或 Docker 启动时，监听地址由 gunicorn 的 `-b` 参数决定，`ADDR` 不生效。

## 运维

```bash
curl http://127.0.0.1:8080/_stats
# {"entries":1,"bytes":2319424,"max_bytes":21474836480,"hits":1,"misses":1,...}

curl http://127.0.0.1:8080/_entries
# {"count":1,"bytes":2319424,"entries":[{"key":"8f14e45f...","url":"https://github.com/...","size":2319424,...}]}
```

删除缓存：

```bash
# 按前缀式 URL 删单条（不访问上游）
curl -X DELETE "http://127.0.0.1:8080/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64"

# 按 key 删单条，key 取自 /_entries
curl -X DELETE "http://127.0.0.1:8080/_entries/<key>"

# 清空全部，响应头 X-Deleted-Entries 是删除数量
curl -i -X DELETE http://127.0.0.1:8080/_entries
```

删除成功返回 `204`，条目不存在返回 `404`，`key` 不是 64 位小写十六进制返回 `400`。
删除只动本地缓存，不会触发回源；下一次请求同一 URL 会重新下载并缓存。

- 缓存按 URL 的 sha256 分两级目录存放，`.body` 是内容，`.meta` 是可读 JSON。
- 每次写入后检查容量，超过上限按最后访问时间淘汰到 90%。
- 也可以直接清磁盘：停止服务后删除 `cache/`，重启会自动重建索引。
- 升级版本后旧缓存仍然可用（键只与 URL 有关）。

## 安全边界

- 默认只监听 `127.0.0.1`。开放到局域网时**务必**同时配置 `ALLOW_CIDRS`。
- 代理路径接受 GET/HEAD/DELETE；拒绝路径穿越；只允许白名单内的域名，包括重定向目标。
- 不会把客户端的 `Authorization` / `Cookie` 转发给上游，也不回传上游的 `Set-Cookie`。
- `UPSTREAM_TOKEN` 只在 HTTPS 下发送给 `github.com`、`api.github.com`、`codeload.github.com`，不会发给 CDN 重定向目标或 `EXTRA_HOSTS`。
- 不做 HTTPS 监听。对外暴露时放在内网反代或只绑内网网卡。

## 已知限制

- 首次请求带 `Range` 时直接透传、不缓存（避免缓存半个文件），第二次完整请求才会落盘。
- 回源中断后保留半成品，但只有上游提供 `ETag` 或 `Last-Modified` 且与半成品一致时才续传；否则从头下载。半成品超过 24 小时会被清理。
- 半成品不计入缓存容量与 LRU 统计，数量由单 URL 一份 + 过期清理约束。
- 没有鉴权 UI，权限靠 CIDR / 网络隔离。
- 多进程部署（gunicorn `-w >1`）时每个进程各自维护缓存与统计，推荐 `-w 1 --threads N`。

## 项目结构

```text
pyproject.toml   项目元数据与依赖
src/
  ghproxy/       源代码包
    app.py       Flask 应用、路由、流式转发
    config.py    环境变量与命令行参数
    proxy.py     目标解析、上游请求、重定向、白名单
    cache.py     磁盘缓存、元数据、LRU 淘汰、按 URL 加锁
    version.py   版本号
    wsgi.py      gunicorn 入口
    __main__.py  python -m ghproxy 入口
tests/           pytest 用例（本地假上游，全部离线）
.specify/        项目宪法、模板、当前 feature 指针
specs/           每个 feature 的 spec / plan / tasks
contracts/       接口契约唯一事实源
docs/adr/        设计决策记录
deploy/          部署文档与样例（Docker、nginx、systemd）
```

## 规格文档（spec-kit 结构）

本项目按 Spec Kit 的产物约定组织文档，手工初始化（未运行 `specify` CLI）：

| 文件 | 作用 |
| --- | --- |
| `.specify/memory/constitution.md` | 不可协商的原则与红线 |
| `.specify/templates/` | 新 feature 的模板 |
| `.specify/feature.json` | 当前活跃 feature |
| `specs/001-github-cache-proxy/spec.md` | 行为期望与验收标准（`FR-xxx`） |
| `specs/001-github-cache-proxy/plan.md` | 技术方案 |
| `specs/001-github-cache-proxy/research.md` | 选型调研 |
| `specs/001-github-cache-proxy/data-model.md` | 缓存元数据与目录布局 |
| `contracts/openapi.yaml` | HTTP 接口契约（唯一事实源，仓库根） |
| `specs/001-github-cache-proxy/tasks.md` | 任务与完成状态 |
| `testing.md` | `FR-xxx` → pytest 用例的映射 |

新增功能时：复制模板到 `specs/NNN-name/`，先写 `spec.md`，再写 `plan.md`，最后拆 `tasks.md`。契约统一放仓库根 `contracts/`，feature 目录里只写引用路径。

一致性由只读脚本检查（不会修改文件）：

```bash
uv run python scripts/check_docs.py
# OK 契约 contracts/openapi.yaml；23 条需求全部有映射；24 个测试引用有效
```

它校验三件事：契约在仓库根且格式正确、`spec.md` 的每个 `FR-xxx` 在 `testing.md` 有映射、`testing.md` 引用的测试函数真实存在。

## 测试

```bash
uv sync
uv run pytest
```

## 部署

Docker 与 systemd 的完整步骤见 [`deploy/README.md`](deploy/README.md)，样例文件（`Dockerfile`、`docker-compose.yml`、`ghproxy.service`）都在 `deploy/`，nginx 配置在 `deploy/nginx/`。
