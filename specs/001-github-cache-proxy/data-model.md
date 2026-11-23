# 数据模型

## 缓存条目

每个上游 URL 对应一份缓存：一个内容文件（`.body`）与一份元数据（`.meta`）。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `url` | string | 原始请求 URL（缓存键的来源） |
| `status` | int | 上游返回的状态码 |
| `content_type` | string | 上游 `Content-Type` |
| `size` | int | 内容字节数 |
| `sha256` | string | 内容的 SHA-256，用于完整性与 ETag |
| `etag` | string | 上游 `ETag`（若提供） |
| `content_encoding` | string | 上游 `Content-Encoding`，命中时原样返回 |
| `created_at` | float | 写入时间戳，用于 TTL 判断 |
| `last_access` | float | 最近访问时间，用于 LRU 淘汰 |

缓存键 = `sha256(url)` 的十六进制；键只与原始 URL 有关，与重定向后的地址无关。

## 目录布局

```text
<cache_dir>/
└── <key[0:2]>/
    ├── <key>.body        内容
    ├── <key>.meta        JSON 元数据
    ├── <key>.body.part   下载中的半成品（完成后 rename）
    └── <key>.body.part.json  半成品的校验信息（ETag / Last-Modified / 总长度）
```

## 统计

`/_stats` 返回：

| 字段 | 说明 |
| --- | --- |
| `entries` | 条目数 |
| `bytes` | 当前占用字节 |
| `max_bytes` | 容量上限 |
| `hits` / `misses` | 命中/未命中次数 |
| `bypasses` | 透传次数（Range 首请求、超大对象、HEAD） |
| `errors` | 上游或写入失败次数 |
| `evictions` | 被淘汰条目数 |
| `hit_rate` | hits / (hits + misses) |

## 生命周期

1. `begin` 创建或打开 `.body.part`
2. 若已有半成品且校验信息一致，向上游发 `Range: bytes=<offset>-` 续传
3. 流式写入并计算 hash
4. 完整结束 → `commit`：rename、写 meta、更新统计
5. 失败或客户端断开 → `abort(keep=True)`：保留半成品，等待下次续传
6. 过期（TTL）或超容量（LRU）→ 删除 body 与 meta；超过 24 小时的半成品在启动时清理
