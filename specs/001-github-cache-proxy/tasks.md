# 任务清单

**Feature**: 001-github-cache-proxy
**状态**: 全部完成（实现 + 测试 + 冒烟均已通过）

## Phase 1 基础设施

- [x] T-001 初始化 Python 环境与依赖清单（`pyproject.toml`、`uv.lock`）
- [x] T-002 [P] 配置模块：env + flag、容量/TTL 解析（`src/ghproxy/config.py`）
- [x] T-003 [P] 版本号与 gunicorn 入口（`src/ghproxy/version.py`、`src/ghproxy/wsgi.py`）

## Phase 2 缓存核心

- [x] T-004 磁盘布局与元数据读写（`src/ghproxy/cache.py`）
- [x] T-005 TTL 过期与统计更新（`src/ghproxy/cache.py`）
- [x] T-006 LRU 淘汰与容量重建（`src/ghproxy/cache.py`）
- [x] T-007 按 URL 加锁，保证并发只回源一次（`src/ghproxy/cache.py`）

## Phase 3 代理链路

- [x] T-008 [P] 目标解析与路径穿越防护（`src/ghproxy/proxy.py`）
- [x] T-009 白名单与重定向逐跳校验（`src/ghproxy/proxy.py`）
- [x] T-010 流式转发并同步落盘（`src/ghproxy/app.py`）
- [x] T-011 命中缓存时支持 Range/HEAD（`src/ghproxy/app.py` + `send_file`）
- [x] T-012 超大对象与首请求 Range 透传不缓存（`src/ghproxy/app.py`）

## Phase 4 可观测与安全

- [x] T-013 `/_health` 与 `/_stats`（`src/ghproxy/app.py`）
- [x] T-014 来源网段校验（`src/ghproxy/app.py`）
- [x] T-015 客户端凭据剥离（`src/ghproxy/proxy.py`）

## Phase 5 验证

- [x] T-016 缓存单元测试（`tests/test_cache.py`）
- [x] T-017 [P] 配置解析测试（`tests/test_config.py`）
- [x] T-018 [P] 代理行为测试（`tests/test_proxy.py`）
- [x] T-019 全部测试通过（45 passed，离线）
- [x] T-020 真实链路冒烟：2.3MB Release 下载 MISS → HIT，Range 206

## Phase 6 交付

- [x] T-021 文档：`README.md`、`AGENTS.md`、`docs/adr/`、`testing.md`
- [x] T-022 部署样例：Dockerfile、compose、systemd
- [x] T-023 spec-kit 结构：`.specify/` 与 `specs/001-github-cache-proxy/`
- [x] T-024 契约提升到仓库根 `contracts/`，feature 目录仅保留引用
- [x] T-025 只读一致性脚本 `scripts/check_docs.py`（契约位置 / FR 映射 / 测试存在性）

## Phase 7 传输可靠性

- [x] T-026 不解压转发上游原始字节，保留 `Content-Encoding`
- [x] T-027 回源中断保留半成品，按 `ETag` / `Last-Modified` 校验后 `Range` 续传
- [x] T-028 半成品唯一命名与过期清理（24 小时）
- [x] T-029 `/_entries` 缓存清单接口

## 后续（未排期）

- [ ] T-030 超时的自动化用例
- [ ] T-031 大文件（>1GiB）压测
- [ ] T-032 对象存储后端（多节点共享缓存时）
