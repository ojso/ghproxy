# 测试与需求的对应关系

每条需求都对应可执行的测试，全部离线运行（`pytest`，使用本地假上游）。

| 编号 | 需求 | 测试 |
| --- | --- | --- |
| FR-001 | 首次请求回源并落盘，二次请求命中缓存且不再访问上游 | `tests/test_proxy.py::test_miss_then_hit` |
| FR-002 | 非白名单域名一律拒绝 | `tests/test_proxy.py::test_disallowed_host_rejected` |
| FR-003 | 服务端跟随 302，并按原始 URL 建缓存 | `tests/test_proxy.py::test_redirect_followed_and_cached_under_original_url` |
| FR-004 | 重定向到白名单外域名时失败 | `tests/test_proxy.py::test_redirect_to_disallowed_host_fails` |
| FR-005 | 命中缓存时支持 Range，返回 206 | `tests/test_proxy.py::test_range_on_cached_entry` |
| FR-006 | 未命中且带 Range 时透传、不缓存 | `tests/test_proxy.py::test_range_on_miss_bypasses_cache` |
| FR-007 | 不把客户端 Authorization / Cookie 转发上游 | `tests/test_proxy.py::test_client_credentials_not_forwarded` |
| FR-008 | 服务端 token 仅通过 HTTPS 发给 GitHub 主机 | `tests/test_proxy.py::test_server_token_used`、`test_token_policy_is_github_https_only`、`test_token_not_forwarded_on_redirect`、`test_server_token_not_sent_to_extra_host` |
| FR-009 | 超过单对象上限时只转发不缓存 | `tests/test_proxy.py::test_oversized_object_bypasses_cache` |
| FR-010 | 代理路径接受 GET/HEAD/DELETE，其它方法 405 | `tests/test_proxy.py::test_method_not_allowed` |
| FR-011 | `/_health` 与 `/_stats` 可用 | `tests/test_proxy.py::test_health_and_stats` |
| FR-012 | 路径穿越与非法目标被拒绝 | `tests/test_proxy.py::test_bad_target_rejected`、`test_parse_target_rules` |
| FR-013 | 缓存条目按 TTL 过期并释放统计 | `tests/test_cache.py::test_ttl_expiry` |
| FR-014 | 超出容量时按 LRU 淘汰 | `tests/test_cache.py::test_evict_least_recently_used` |
| FR-015 | 容量未超时不淘汰 | `tests/test_cache.py::test_evict_noop_under_limit` |
| FR-016 | 重启后从磁盘重建容量统计 | `tests/test_cache.py::test_recount_after_restart` |
| FR-017 | 同一 URL 的并发下载被串行化 | `tests/test_cache.py::test_lock_serialises_same_url` |
| FR-018 | 容量字符串解析（KiB/GB/纯数字） | `tests/test_config.py::test_parse_bytes` |
| FR-019 | TTL 字符串解析（h/m/d/纯秒） | `tests/test_config.py::test_parse_ttl` |
| FR-020 | 配置默认值、环境变量与参数覆盖 | `tests/test_config.py::test_load_config_defaults`、`test_load_config_env_and_flags` |
| FR-021 | 透传并缓存原始字节，保留 `Content-Encoding` | `tests/test_proxy.py::test_encoded_body_forwarded_and_cached_verbatim` |
| FR-022 | 回源中断后从已下载偏移续传，校验不一致则重下 | `tests/test_proxy.py::test_partial_download_resumes_with_range`、`test_partial_restarts_when_etag_changes`、`test_interrupted_upstream_download_keeps_partial`、`tests/test_cache.py::test_abort_can_keep_partial_for_resume` |
| FR-023 | `/_entries` 返回缓存清单 | `tests/test_proxy.py::test_entries_lists_cached_items` |
| FR-024 | 非 2xx 透传时 `Content-Length` 与实际 body 一致 | `tests/test_proxy.py::test_upstream_error_body_length_matches`、`test_upstream_error_body_forwarded` |
| FR-025 | DELETE 删除单条/全部缓存，404 与 400 语义 | `tests/test_proxy.py::test_delete_entry_by_url`、`test_delete_entry_by_key`、`test_delete_missing_entry_returns_404`、`test_delete_rejects_bad_key`、`test_delete_disallowed_host`、`test_delete_all_entries`、`tests/test_cache.py::test_remove_key_updates_stats`、`test_remove_key_drops_partial_download`、`test_clear_removes_all_entries` |

## 手工冒烟（需要外网）

```bash
uv run python -m ghproxy --addr 127.0.0.1:18080 --cache-dir ./cache
curl -sD - -o /dev/null "http://127.0.0.1:18080/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64" | grep -i x-cache  # MISS
curl -sD - -o /dev/null "http://127.0.0.1:18080/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64" | grep -i x-cache  # HIT
curl -sD - -o /dev/null -H "Range: bytes=0-9" "http://127.0.0.1:18080/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64" | grep -iE "HTTP|x-cache"  # 206 + HIT
```

## 缺口

- 没有对超时与半途断开的自动化用例（依赖真实网络条件）。
- 没有大文件（>1GiB）与 Windows 长路径的压测。
- 多进程（gunicorn `-w >1`）下的缓存一致性未验证，部署约定为单 worker 多线程。
