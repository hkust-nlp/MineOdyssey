# Containers Build Notes

## 1. 构建链路（当前）

- `containers/Containerfile`:
  - 构建基础客户端镜像（`mc-agent-gpu-ubuntu2204`）
- `containers/Containerfile.unified`:
  - 基于 `mc-agent-gpu-ubuntu2204`
  - 补齐 VNC/stream/server 依赖
  - 补齐 `mc-unified` 容器基础命令行工具集（含 `jq`）

`scripts/containers/run-unified-container-once.sh` / `scripts/containers/start-openha-eval-container.sh` 默认使用镜像：

- `mc-agent-unified-ubuntu2204`

## 2. 工具清单维护方式

- 基础工具集：`containers/tooling/unified-baseline-apt.txt`
- 调试增强工具集：`containers/tooling/unified-debug-apt.txt`

原则：

- 基础工具集默认安装，覆盖 agent 常见 shell 命令（优先保证 `jq`、进程/网络排障、压缩与文本查看）。
- 调试增强工具集通过构建参数启用，避免默认镜像过重。

## 3. 构建命令（统一镜像）

先构建基础 GPU 镜像（若本地尚未构建）：

```bash
podman build -t mc-agent-gpu-ubuntu2204 -f containers/Containerfile .
```

构建默认运行镜像（基础工具集）：

```bash
podman build -t mc-agent-unified-ubuntu2204 -f containers/Containerfile.unified .
```

构建调试镜像（包含 debug 工具集）：

```bash
podman build \
  -t mc-agent-unified-ubuntu2204-debug \
  --build-arg INSTALL_DEBUG_TOOLS=1 \
  -f containers/Containerfile.unified .
```

## 4. 版本变更与验证流程（TODO-002）

每次调整工具集或安装逻辑，按以下流程执行：

1. 修改工具清单文件（`containers/tooling/*.txt`）或 `containers/Containerfile.unified`
2. 更新 `TOOLING_BASELINE_VERSION`（`containers/Containerfile.unified`）
3. 重建统一镜像（默认或 debug）
4. 启动容器（`scripts/containers/run-unified-container-once.sh` 或 `scripts/containers/start-openha-eval-container.sh`）
5. 运行工具自检：
   - 宿主机执行：`./scripts/runtime/check-container-tools.sh --container mc-unified`
   - 若使用 debug 镜像：`./scripts/runtime/check-container-tools.sh --container mc-unified --profile debug`
6. 至少验证一个管道命令场景（`jq`）通过
7. 在 `docs/TODOS/TODO-002-container-tooling-baseline.md` 记录结果

镜像标签（用于追踪工具集版本）：

- `mcbots.tooling.baseline.version`
- `mcbots.tooling.debug.enabled`
