# Minecraft Agent API Examples

这个目录包含了使用 Minecraft Agent Bridge API 的 Python 示例代码。

## 前置条件

1. Minecraft 客户端正在运行，并且加载了 Agent Bridge mod
2. 当前 workspace 已存在 `/workspace/.mcbots_runtime.json`（包含 `agentbridge`/`remote_bash` 端口）
3. Python 3.7+ 和 `requests` 库已安装

## 安装依赖

```bash
pip install requests
```

或者使用项目的 uv：

```bash
cd /export/path/to/projects/mcbots
uv pip install requests
```

## 运行示例

### 1. 基础移动 (basic_movement.py)

展示基本的玩家控制：走动、跳跃、视角控制。

```bash
cd /export/path/to/projects/mcbots/agent/examples
python3 basic_movement.py
```

**演示内容：**
- 连接检查和状态查询
- 跳跃
- 向前走2秒
- 转身180度
- 走正方形
- 疾跑跳跃

### 2. 挖矿 (mining.py)

展示如何破坏方块。

```bash
python3 mining.py
```

**演示内容：**
- 破坏前方单个方块
- 连续挖掘3个方块
- 挖掘2x2区域
- 边走边挖

### 3. 方块交互 (interaction.py)

展示如何右键方块和使用物品。

```bash
python3 interaction.py
```

**演示内容：**
- 右键前方方块（可以打开箱子、按按钮等）
- 使用手中物品（右键空气）
- 与四周方块交互
- 完整的交互流程演示

## 在容器内运行

如果你的 Minecraft 在容器内运行（ai-agent-Bot1），需要从容器内执行：

```bash
# 方法1：进入容器
podman exec -it ai-agent-Bot1 bash
cd /app/agent/examples
python3 basic_movement.py

# 方法2：直接执行
podman exec ai-agent-Bot1 python3 /app/agent/examples/basic_movement.py
```

## API 文档

详细的 API 文档请查看 `../minecraft_api.py` 中的 docstrings。

### 主要类和方法

```python
from minecraft_api import MinecraftAPI

# 创建客户端
api = MinecraftAPI()

# 健康检查
api.health_check()
api.is_connected()

# 状态查询
api.get_state()
api.get_position()  # 返回 (x, y, z)
api.get_rotation()  # 返回 (yaw, pitch)

# 移动控制
api.move_forward(True)   # 开始前进
api.move_forward(False)  # 停止前进
api.jump(True)
api.sprint(True)
api.sneak(True)

# 视角控制
api.set_look(yaw=90, pitch=0)  # 向西水平看
api.look_at(x, y, z)           # 看向指定方块

# 方块交互
api.break_block(x, y, z)
api.right_click_block(x, y, z)
api.right_click()  # 使用物品

# 辅助方法
api.stop_all_movement()
api.walk_forward(duration=2.0)
```

## 注意事项

1. **坐标系统：**
   - X: 东(+) / 西(-)
   - Y: 上(+) / 下(-)
   - Z: 南(+) / 北(-)

2. **视角（Yaw）：**
   - 0° = 南 (+Z)
   - 90° = 西 (-X)
   - 180° = 北 (-Z)
   - 270° = 东 (+X)

3. **视角（Pitch）：**
   - -90° = 正上方
   - 0° = 水平
   - 90° = 正下方

4. **错误处理：**
   所有示例都包含基本的错误处理。如果遇到连接问题，请检查：
   - Minecraft 是否运行
   - Agent Bridge mod 是否加载
   - `/workspace/.mcbots_runtime.json` 是否存在且端口正确

5. **中断示例：**
   按 `Ctrl+C` 可以随时中断示例脚本。脚本会尝试停止所有移动。

## 故障排查

### 连接失败

```bash
# 检查 HTTP 服务器是否运行
podman exec ai-agent-Bot1 curl -s http://localhost:8080/api/health

# 检查日志
podman logs ai-agent-Bot1 | grep -i "agent bridge"
```

### API 返回错误

检查返回的 JSON 中的 `error` 字段以了解具体错误原因。

## 扩展开发

可以基于这些示例创建更复杂的 Agent 行为：

1. **自动化任务：** 自动挖矿、建筑、农业
2. **探索：** 自动探索地图
3. **战斗：** 自动战斗和生存
4. **交易：** 自动与村民交易
5. **LLM 集成：** 让大语言模型控制 Minecraft

示例代码都是基础构建块，可以自由组合和扩展！
