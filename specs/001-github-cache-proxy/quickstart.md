# 快速验证

## 启动

```bash
uv sync
uv run python -m ghproxy --addr 127.0.0.1:8080 --cache-dir ./cache
```

Windows/Linux/macOS 都使用同一条 `uv sync` + `uv run` 命令。

## 验证缓存

```bash
curl http://127.0.0.1:8080/_health

# 第一次：X-Cache: MISS
curl -sD - -o /dev/null \
  http://127.0.0.1:8080/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64

# 第二次：X-Cache: HIT，且不再访问上游
curl -sD - -o /dev/null \
  http://127.0.0.1:8080/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64

# Range：206 + HIT
curl -sD - -o /dev/null -H "Range: bytes=0-9" \
  http://127.0.0.1:8080/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64

curl http://127.0.0.1:8080/_stats
```

## 接到 mise

```toml
[settings.url_replacements]
"https://github.com/" = "http://127.0.0.1:8080/https://github.com/"
"https://release-assets.githubusercontent.com/" = "http://127.0.0.1:8080/https://release-assets.githubusercontent.com/"
```

## 断网验证

停掉外网后，重复上面的第二次请求，仍应返回 `HIT` 与完整内容。
