# 部署

本目录提供 Docker 与 systemd 两种部署形态的样例，nginx 配置在 `nginx/` 子目录：

| 文件 | 用途 |
| --- | --- |
| `Dockerfile` | 镜像构建 |
| `docker-compose.yml` | compose 启动 |
| `ghproxy.service` | systemd unit |

nginx 相关文件：

| 文件 | 用途 |
| --- | --- |
| `nginx/nginx.conf` | 反向代理示例（HTTP，根路径） |
| `nginx/nginx_ghproxy_common.conf` | `upstream` + `map` 公共定义（多个站点共用） |
| `nginx/nginx_https_prefix.conf` | 反向代理示例（HTTPS + `/ghproxy/` 前缀） |

以下命令默认在**仓库根目录**执行。

## Docker 部署

### 1. 构建镜像

`deploy/Dockerfile` 需要以仓库根目录为构建上下文：

```bash
docker build -f deploy/Dockerfile -t ghproxy:0.1.0 .
```

`.dockerignore` 已排除 `.git/`、`.venv/`、`cache/`、测试与规格文档，构建上下文只保留运行所需内容。

PyPI 访问不了、构建卡在 `pip install` 时，把 Dockerfile 里的 `RUN pip install --no-cache-dir . gunicorn` 改成走镜像源（只在本地改，不需要提交）：

```dockerfile
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple . gunicorn
```

### 2. 准备外部缓存目录

镜像内用户是 `ghproxy`（UID/GID `10001`），宿主机目录必须让这个 UID 可写：

```bash
sudo mkdir -p /srv/ghproxy/cache
sudo chown -R 10001:10001 /srv/ghproxy/cache
```

不想改属主时，可以改用命名卷，由 Docker 按镜像内目录权限初始化：

```bash
docker volume create ghproxy-cache
# 运行时用 -v ghproxy-cache:/cache
```

### 3. 用 docker run 启动

```bash
docker run -d --name ghproxy \
  -p 127.0.0.1:18080:8080 \
  -v /srv/ghproxy/cache:/cache \
  -e CACHE_DIR=/cache \
  -e MAX_CACHE_BYTES=50GiB \
  -e MAX_OBJECT_BYTES=4GiB \
  -e CACHE_TTL=720h \
  --restart unless-stopped \
  ghproxy:0.1.0
```

| 参数 | 说明 |
| --- | --- |
| `-p 127.0.0.1:18080:8080` | 只绑定回环，把 `18080` 留给同机 nginx；容器内仍是 `8080` |
| `-v /srv/ghproxy/cache:/cache` | 外部缓存目录，数据在容器重建后保留 |
| `CACHE_DIR=/cache` | 必须指向容器内的挂载点 |
| `MAX_CACHE_BYTES` | 缓存总容量，超出后按 LRU 淘汰 |
| `MAX_OBJECT_BYTES` | 单对象上限，超过只转发不落盘 |
| `CACHE_TTL` | 条目有效期 |

验证：

```bash
curl http://127.0.0.1:18080/_health
curl http://127.0.0.1:18080/_entries
curl -O "http://127.0.0.1:18080/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64"
```

### 4. 用 docker compose 启动

`docker-compose.yml` 已经按“只绑回环 + 外挂缓存”配置好：

```bash
cd deploy
docker compose up -d --build
docker compose logs -f
```

可通过环境变量覆盖：

```bash
GH_PROXY_PORT=18080 \
GH_CACHE_DIR=/srv/ghproxy/cache \
MAX_CACHE_BYTES=50GiB \
docker compose up -d
```

> compose 文件在 `deploy/`，`build.context: ..` 指向仓库根。缓存路径建议写绝对路径，避免被解析成 `deploy/cache`。

### 5. nginx 反向代理

| 文件 | 形态 | 客户端地址 |
| --- | --- | --- |
| `nginx/nginx.conf` | HTTP，根路径挂载（自包含单文件） | `http://cache.corp/https://github.com/...` |
| `nginx/nginx_ghproxy_common.conf` | `upstream` + `map` 公共定义 | — |
| `nginx/nginx_https_prefix.conf` | HTTPS + `/ghproxy/` 前缀 | `https://cache.corp/ghproxy/https://github.com/...` |

带前缀的方案由两部分组成：`nginx/nginx_ghproxy_common.conf` 装到 `/etc/nginx/conf.d/00-ghproxy-common.conf`（`upstream` / `map` 是 `http` 级指令，同一台 nginx 只装一份），站点文件只保留 `server` 与 `location`。安装顺序：

```bash
sudo cp deploy/nginx/nginx_ghproxy_common.conf /etc/nginx/conf.d/00-ghproxy-common.conf
sudo cp deploy/nginx/nginx_https_prefix.conf  /etc/nginx/conf.d/ghproxy.conf
sudo nginx -t && sudo systemctl reload nginx
```

`nginx/nginx.conf` 是最小示例，自包含、不依赖公共文件；要点是 `merge_slashes off;` 必须保留（否则 `/https://` 会被合并成 `/https:/`），`proxy_pass http://ghproxy_backend;` 末尾不能加 `/`，以及保持 `proxy_buffering off` / `proxy_cache off` 以流式转发大文件。

#### HTTPS + `/ghproxy/` 前缀

完整示例见 `nginx/nginx_https_prefix.conf`。客户端地址形如：

```text
https://cache.corp/ghproxy/https://github.com/owner/repo/releases/download/v1/file
```

ghproxy 本身不处理 TLS，也不认识 `/ghproxy` 前缀，两者都由 nginx 完成：TLS 在 443 终止，前缀在转发前剥掉。

`upstream` 和 `map` 是 `http` 级指令，单独放在 `nginx/nginx_ghproxy_common.conf` 里，同一台 nginx 只装一份：

```nginx
# nginx/nginx_ghproxy_common.conf -> /etc/nginx/conf.d/00-ghproxy-common.conf
upstream ghproxy_backend {
    server 127.0.0.1:18080;
    keepalive 32;
}

# 剥前缀，其余原样转发。用 $request_uri 而不是 proxy_pass 尾斜杠，
# 后者会让 nginx 规范化 URI，把 /https:// 合并成 /https:/，并把 %2F 解码成 /
map $request_uri $ghproxy_uri {
    "~^/ghproxy(?<rest>/.*)$" $rest;
}
```

站点文件只保留 `server` 部分（`nginx/nginx_https_prefix.conf`）：

```nginx
server {
    listen 80;
    server_name cache.corp;

    location / {
        return 301 https://$host$request_uri;   # 保留 /ghproxy/ 前缀
    }
}

server {
    listen 443 ssl;
    server_name cache.corp;

    ssl_certificate     /etc/nginx/tls/cache.corp.crt;
    ssl_certificate_key /etc/nginx/tls/cache.corp.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:ghproxy_tls:10m;

    location /ghproxy/ {
        proxy_pass http://ghproxy_backend$ghproxy_uri;   # 末尾不要加 /
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
        proxy_request_buffering off;
        proxy_max_temp_file_size 0;
        proxy_cache off;

        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        send_timeout 3600s;
        client_max_body_size 0;
    }
}
```

为什么不用 `proxy_pass http://ghproxy_backend/;` 直接剥前缀：那种写法会让 nginx 先规范化请求 URI，实测 `/ghproxy/https://github.com/a/b` 会变成 `/https:/github.com/a/b`（除非加 `merge_slashes off`），并且 `%2F` 会被解码成 `/`。`map + $request_uri` 对双斜杠、百分号编码和查询串都是零改动透传。

另外两点：

- `merge_slashes` 只允许出现在 `http` / `server` 级，**不能写进 `location`**（`nginx: "merge_slashes" directive is not allowed here`）。用 `map + $request_uri` 时它根本不需要，所以 `nginx/nginx_https_prefix.conf` 里没有这一行。
- 如果改成尾斜杠形式，就必须在 `server` 级加回 `merge_slashes off;`。

#### 合并到已有的 `server` 块

不想为 ghproxy 单开域名时，把公共部分装好后直接改现有站点即可，域名和证书都沿用原来的。分两处：

1. `upstream` / `map` 只写在 `nginx/nginx_ghproxy_common.conf`（装成 `00-ghproxy-common.conf`），站点文件里不要重复定义；
2. 在站点 `server {}` 内加一个 `location ^~ /ghproxy/`。

```nginx
server {
    listen 443 ssl;
    server_name www.example.com;          # 原有域名
    # ... 原有 ssl_certificate / root / index / error_page 等保持不变 ...

    # ... 原有 location 保持不变 ...

    # 新增一个 location，用 ^~ 保证优先于 location /
    location ^~ /ghproxy/ {
        proxy_pass http://ghproxy_backend$ghproxy_uri;

        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
        proxy_request_buffering off;
        proxy_max_temp_file_size 0;
        proxy_cache off;

        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        send_timeout 3600s;
        client_max_body_size 0;
    }

    location / { ... }                   # 原有兜底 location 不用改
}
```

要点：

- `upstream` 和 `map` 是 `http` 级指令，统一放在 `nginx/nginx_ghproxy_common.conf`；写进 `server {}` 会报 `directive is not allowed here`。
- 同一台 nginx 只能有一份 `upstream` / `map`：多个站点共用同一个公共文件，在站点文件里再 `include` 或重复定义会导致启动失败。
- 裸写 `location /ghproxy/` 也能用，但加 `^~` 更稳妥：它会让匹配跳过正则 location，也不会落到 `location /` 的 `try_files` / 兜底后端。
- 如果还有 80 端口的跳转 server，用 `return 301 https://$host$request_uri;` 即可，`$request_uri` 会保留 `/ghproxy/` 前缀。

证书可以用内部 CA 或自签，客户端必须信任：

```bash
sudo mkdir -p /etc/nginx/tls && cd /etc/nginx/tls
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout cache.corp.key -out cache.corp.crt \
  -subj "/CN=cache.corp" -addext "subjectAltName=DNS:cache.corp"
sudo chmod 600 cache.corp.key
```

自签证书不要在配置里开 HSTS，否则证书出问题后浏览器无法回退。

验证：

```bash
nginx -t
curl -sI http://cache.corp/_health                       # 301 到 https
curl -s  https://cache.corp/ghproxy/_health              # 前缀同样能到 /_health
curl -sI https://cache.corp/ghproxy/https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64 \
  | grep -i x-cache
```

如果 nginx 与 ghproxy 在同一个 compose 网络里，可以去掉宿主机端口映射，把 upstream 改成 `server ghproxy:8080;`。

不想用前缀或不需要 HTTPS 时，直接用 `nginx/nginx.conf` 那份配置。

### 6. 端口说明

- 容器内固定监听 `8080`（Dockerfile 的 gunicorn `-b 0.0.0.0:8080`）
- 宿主机端口通过 `-p 127.0.0.1:18080:8080` 映射，nginx 连 `18080`
- **`ADDR` 环境变量对 Docker/gunicorn 无效**，不要用它改端口
- 确实要改容器内端口时，覆盖 CMD：

```bash
docker run -d --name ghproxy \
  -p 127.0.0.1:19000:9000 \
  -v /srv/ghproxy/cache:/cache \
  ghproxy:0.1.0 \
  gunicorn -w 1 --threads 16 --timeout 300 \
    -b 0.0.0.0:9000 ghproxy.wsgi:app
```

改内部端口后，镜像自带的 `HEALTHCHECK`（写死 `127.0.0.1:8080`）会失效，需要同时覆盖 `--health-cmd`。

### 7. 注意事项

- **`merge_slashes off;`**：`nginx/nginx.conf`（根路径方案）必须加，否则 `/https://` 会被合并成 `/https:/`；`nginx/nginx_https_prefix.conf` 用 `map + $request_uri` 转发原始 URI，不需要。该指令只能在 `http` / `server` 级设置，不能写进 `location`。
- **`proxy_pass` 末尾不要加 `/`**：否则 location 前缀被替换，路径丢失。
- **`proxy_buffering off`**：避免 nginx 把大文件先写临时文件，也保证流式下载不卡顿。
- **`proxy_cache off`**：缓存已由 ghproxy 负责，nginx 再缓存会双份占盘。
- **单 worker**：`-w 1` 不能改。缓存与统计在进程内维护，多 worker 会各存一份。
- **超时**：默认 gunicorn `--timeout 30s`，大文件慢链路建议加 `--timeout 300`。
- **健康检查间隔**：Dockerfile 默认 `HEALTHCHECK --interval=30s`。30s 探测偏频繁；长期运行、少变更的部署可以改成 `--interval=120s`，配合 `--retries=3` 最迟约 6 分钟判定 unhealthy。两种间隔都可用，按部署环境改 Dockerfile 即可。
- **`ALLOW_CIDRS`**：nginx 在前面时，应用看到的是 nginx 或容器网关地址，不是真实客户端 IP；建议留空，访问控制放到 nginx 或防火墙。若必须启用，记得把 Docker 网段（如 `172.16.0.0/12`）加进去。
- **缓存目录权限**：bind mount 必须属于 UID/GID `10001`，否则会因权限不足写缓存失败。
- **不要直接暴露端口**：`-p 0.0.0.0:8080:8080` 会把无鉴权代理暴露到网络；只绑 `127.0.0.1` 或内网网卡，前面加 nginx。

### 8. 推送到镜像仓库（可选）

```bash
docker tag ghproxy:0.1.0 registry.example.com/library/ghproxy:0.1.0
docker push registry.example.com/library/ghproxy:0.1.0
```

多架构构建：

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f deploy/Dockerfile \
  -t registry.example.com/library/ghproxy:0.1.0 \
  --push .
```

## systemd 部署

用 `deploy/ghproxy.service` 托管（内部用 gunicorn，`-w 1 --threads 16`）。

> **gunicorn 不是项目依赖**：`pyproject.toml` 只声明 Flask 与 requests，`uv sync` 不会装 gunicorn。
> `deploy/ghproxy.service` 的 `ExecStart` 指向 `/opt/ghproxy/.venv/bin/gunicorn`，必须按下面的步骤手工装进 venv。
> 重建 venv 或升级后需要重装一次。

### 1. 准备目录与用户

```bash
sudo useradd --system --home /opt/ghproxy --shell /usr/sbin/nologin ghproxy
sudo mkdir -p /opt/ghproxy /var/cache/ghproxy
sudo chown -R ghproxy:ghproxy /opt/ghproxy /var/cache/ghproxy
# 将代码放到 /opt/ghproxy（git clone / rsync 均可）
```

### 2. 安装依赖（含 gunicorn）

```bash
cd /opt/ghproxy
sudo -u ghproxy uv sync
sudo -u ghproxy uv pip install gunicorn   # 只装进 .venv，不写入 pyproject.toml
.venv/bin/gunicorn --version              # 确认存在
```

### 3. 写环境变量文件

```bash
sudo tee /etc/ghproxy.env >/dev/null <<'EOF'
CACHE_DIR=/var/cache/ghproxy
MAX_CACHE_BYTES=50GiB
CACHE_TTL=720h
EOF
sudo chmod 600 /etc/ghproxy.env
```

`CACHE_DIR` 必须指向 `/var/cache/ghproxy`：unit 启用了 `ProtectSystem=strict`，只有 `ReadWritePaths=/var/cache/ghproxy` 可写；
默认值 `./cache` 会落在只读的 `/opt/ghproxy/cache`，导致启动后写缓存失败。`/etc/ghproxy.env` 是**必需**的：
文件缺失时 systemd 会直接启动失败并提示找不到环境文件，不会静默使用默认值。

### 4. 安装并启动

```bash
sudo cp deploy/ghproxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ghproxy
```

`ExecStart` 已带 `--timeout 300`，适配大文件慢链路，不需要再手工追加。

### 5. 验证与运维

```bash
systemctl status ghproxy
journalctl -u ghproxy -f
curl -s http://127.0.0.1:8080/_health

sudo systemctl restart ghproxy   # 修改 /etc/ghproxy.env 后必须重启（启动时才读取）
```

常见失败：`status=203/EXEC` 表示 `.venv/bin/gunicorn` 不存在（漏了第 2 步）；`status=200/CHDIR` 表示
`WorkingDirectory=/opt/ghproxy` 不存在。若代码不放在 `/opt/ghproxy`，需同步修改 unit 中的
`WorkingDirectory`、`PYTHONPATH`、`ExecStart`、`ReadWritePaths` 四处。
