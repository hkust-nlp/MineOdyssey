# Fast Q&A

常见操作的速查手册。复杂话题拆分到 `docs/eval/` 等子目录，这里只放简明答案。

---

## Bootstrap

### Q: bootstrap 报 `java.net.SocketException: Network is unreachable` 怎么办？

**原因**：机器只有 IPv6 出口，NeoForge installer 的 Java 进程强制使用了 IPv4（`preferIPv4Stack=true`）。

**快速诊断**：
```bash
curl -4 -I --connect-timeout 5 https://maven.neoforged.net   # 失败 → 没有 IPv4 出口
curl -6 -I --connect-timeout 5 https://maven.neoforged.net   # 成功 → IPv6 正常
```

**修复**：`bootstrap-runtime-assets.sh` 和 `run-openha-eval-core-inside.sh` 开头已加入 IPv4 自动检测，不通则自动设置 `JAVA_TOOL_OPTIONS` 走 IPv6。直接重跑即可。

---

## 运行单个 Eval 任务

详见 [docs/eval/run-single-task.md](eval/run-single-task.md)。

**推荐方式**（自动推断 runner、task config、snapshot）：
```bash
TASK_NAME=mine_block:dirt \
API_MODEL_ALIAS=gemini3flash \
SKIP_AGENT=false \
bash scripts/eval/run-openha-task-eval-inside.sh
```

---

## 可用模型别名

配置文件：`config/api_models.json`

| 别名 | 模型 |
|------|------|
| `gemini3flash` | google/gemini-3-flash-preview |
| `gemini3flashlite` | google/gemini-3.1-flash-lite-preview |
| `gemini31pro` | google/gemini-3.1-pro-preview |
| `minimax2.5` | minimax/minimax-m2.5 |
| `kimi-k2.5` | kimi-k2.5 |

---

## 启动主链路

### Q: 怎么同时启动 server + client？

```bash
CLIENT_COUNT=1 bash scripts/launch/start-server-clients-inside.sh
```

### Q: 怎么单独启动 server / client？

```bash
# 单独启动 server（端口自动分配）
bash scripts/launch/run-server-only-inside.sh

# 单独启动 client 连接已有 server
SERVER_PORT=20008 bash scripts/launch/start-single-bot-inside.sh Bot2
```

> 三个脚本都是 inside 模式（宿主机直接运行，不依赖 podman）。
> 容器版脚本是 `run-server-only.sh` / `start-single-bot.sh`，不要搞混。

### Q: 前台运行 server 怎么做？

```bash
SERVER_FOREGROUND=true bash scripts/launch/run-server-only-inside.sh
```

### Q: 日志和 PID 文件在哪？

- 日志：`logs/<run_id>/`
- PID 文件：`runtime/<run_id>.pids`
