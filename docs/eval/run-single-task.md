# 运行单个 Eval 任务

## 推荐方式：`run-openha-task-eval-inside.sh`

这是运行单个任务的推荐入口。它会自动：
- 根据 `TASK_NAME` 推断 `RUNNER_PATH` 和 `TASK_CONFIG`（使用 imported 的完整配置）
- 通过 snapshot plan 解析出正确的预生成快照世界作为 `TEMPLATE_WORLD_DIR`
- 加载 imported 的 instructions

只需指定 `TASK_NAME` 即可：

```bash
TASK_NAME=mine_block:dirt \
API_MODEL_ALIAS=gemini3flash \
SKIP_AGENT=false \
bash scripts/eval/run-openha-task-eval-inside.sh
```

### 常用环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TASK_NAME` | `kill_entity:sheep` | 任务名（必填） |
| `API_MODEL_ALIAS` | `gemini3flash` | 模型别名，对应 `config/api_models.json` 中的 key |
| `SKIP_AGENT` | `true` | 设为 `false` 才会启动 agent 真正执行任务并产生 records |
| `TASK_TIMEOUT_SEC` | `120` | 单轮任务超时（秒） |
| `OPENHA_SNAPSHOT_TEMPLATE_MODE` | `required` | snapshot 模式，`required` 要求必须有快照，`off` 跳过 |
| `DRY_RUN` | `false` | 仅打印命令不实际执行 |
| `MEMORY_MIN` / `MEMORY_MAX` | `2G` / `8G` | MC 服务端 JVM 内存 |

### 支持的任务族

wrapper 根据 `TASK_NAME` 前缀自动推断 runner 和 config：

| 任务族 | TASK_NAME 前缀 | 自动推断的 Runner | 自动推断的 Config |
|--------|----------------|-------------------|-------------------|
| 挖方块 | `mine_block:` | `mine_block_family_eval_runner.py` | `imported/task_configs/mine_block.json` |
| 杀实体 | `kill_entity:` | `kill_entity_eval_runner.py` | `imported/task_configs/kill_entity.json` |
| 合成物品 | `craft_item:` | `craft_item_eval_runner.py` | `imported/task_configs/craft_item.json` |
| 熔炼物品 | `smelt_item:` | `smelt_item_eval_runner.py` | `imported/task_configs/smelt_item.json` |
| 交互方块 | `interact_block:` | `interact_block_eval_runner.py` | `imported/task_configs/interact_block.json` |

### 结果输出

默认 layout 为 `single_task_dir`，结果保存在：

```
eval/results/single_tasks/<timestamp>_<task_name>/
├── summary.json              # 任务结果
├── run_config.json            # 运行配置
└── records/                   # SKIP_AGENT=false 时才有
    ├── session.mp4            # 录像
    ├── messages.json          # agent 对话记录
    ├── frame_filter_summary.json
    ├── frame_filter_telemetry.jsonl
    └── screenshots/           # 截图序列
```

> **注意**：`SKIP_AGENT=true`（默认）时不会产生 records 目录，也不会有 agent 执行任务。

### 轮数

轮数由 task config JSON 中对应任务的 `seeds` 数组长度决定。

---

## 底层方式：`run-openha-eval-core-inside.sh`

如果需要完全手动控制所有参数，可以直接调用底层脚本。但需要自行指定 `TASK_CONFIG`、`RUNNER_PATH`、`TEMPLATE_WORLD_DIR` 等，不会自动加载 snapshot。

```bash
TASK_CONFIG=eval/openha_assets/mine_block_min.json \
TASK_NAME=mine_block:dirt \
RUNNER_PATH=eval/mine_block_eval_runner.py \
API_MODEL_ALIAS=gemini3flash \
SKIP_AGENT=false \
bash scripts/eval/run-openha-eval-core-inside.sh
```

一般不推荐直接使用，除非有特殊调试需求。

---

## 批量运行：`run-openha-dev-subset-eval-inside.py`

用于批量运行一组任务（dev subset eval），支持并发：

```bash
python3 scripts/eval/run-openha-dev-subset-eval-inside.py \
    --task mine_block:dirt \
    --api-model-alias gemini3flash \
    --parallelism 1
```

也可以跑预定义的 subset：

```bash
python3 scripts/eval/run-openha-dev-subset-eval-inside.py \
    --subset quick10 \
    --api-model-alias gemini3flash
```
