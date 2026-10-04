# Ground Navigation

这是一个纯客户端 NeoForge Mod。它让 Baritone 只负责规划，并把路线显示成贴近地面的导航轨迹，供评测中的视觉 Agent 自己行走：

- 细青绿色点线：Agent 可参考的可行走路线
- 小型黄色箭头：前进方向
- 小型红色圆环：当前路径终点

目标环境：Minecraft 1.21.11、NeoForge 21.11.44、Baritone standalone 1.17.0。

## 评测模式

评测器通过 Minecraft 实例目录中的文件向 Mod 下发路线：

```text
<instance>/ground-navigation/request.json
```

Mod 收到路线后会依次执行以下流程：

1. 让 Baritone 规划当前必经点或终点。
2. 每个客户端 tick 强制 Baritone 保持暂停，不允许它发送移动输入。
3. 强制关闭 Baritone 的聊天、前缀命令和原生路径渲染入口。
4. 把当前路径绘制在游戏画面中的地面上，让视觉 Agent 根据画面自己移动。
5. Agent 到达必经点后，自动规划下一段；偏离路线或走到局部路径末端时自动重算。
6. 将确认、锁定状态和进度写到 `<instance>/ground-navigation/status.json`。

从仓库根目录下发一条正式任务，例如：

```bash
python3 scripts/eval/set_ground_navigation_route.py \
  --instance /path/to/minecraft-instance \
  --tasks eval/navigation/tasks.json \
  --waypoints eval/navigation/maps/shun-lee/waypoints.json \
  --task-id slr-n01 \
  --map-id shun-lee \
  --wait-seconds 10
```

评测启动顺序应为：重置世界并传送到任务起点 → 下发上述路线并等待 `status.json` 确认 → 开始向 Agent 提供画面和动作接口。

也可以直接下发一个或多个坐标：

```bash
python3 scripts/eval/set_ground_navigation_route.py \
  --instance /path/to/instance \
  --point=-267,10,-549 \
  --point=-147,6,-519
```

评测结束时清除路线：

```bash
python3 scripts/eval/set_ground_navigation_route.py \
  --instance /path/to/instance \
  --clear
```

没有活动评测请求时可按 `N` 开关线路显示。活动评测期间该按键被忽略，线路由 evaluator 独占控制。

## 请求格式

```json
{
  "id": "episode-0001",
  "enabled": true,
  "guide_only": true,
  "arrival_radius": 1.5,
  "points": [
    {"id": "waypoint-a", "x": -267, "y": 10, "z": -549},
    {"id": "target", "x": -147, "y": 6, "z": -519}
  ]
}
```

每个新的请求必须使用新的 `id`。`status.json` 会返回 `planning`、`active`、`completed`、`disabled` 或 `error`，以及当前 `point_index`。

本 Mod 不放置或破坏方块，也不修改世界存档。

## 构建

需要 JDK 21：

```bash
cd src/ground-navigation
./gradlew build
```

产物位于 `build/libs/ground_navigation-0.1.0.jar`。

它只在任务的 `eval_setting.guideline=true` 时加入该任务的客户端；默认评测不会加载 Ground Navigation 或 Baritone。

## 兼容策略

官方 Baritone API 通常提供 `IPathingBehavior` 和 `IPath`。当前使用的 standalone 1.17.0 NeoForge JAR 对大部分 API 名称做了混淆，因此本 Mod 在运行时按类型结构定位以下数据：

- 当前执行路径
- 下一段预计算路径
- 正在计算的最佳临时路径

Baritone 缺失或内部结构变化时，Mod 会显示状态提示并停止绘制，不会让客户端崩溃；详细原因记录在 `latest.log`。
