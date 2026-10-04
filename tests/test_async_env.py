#!/usr/bin/env python3
"""测试env.py的异步命令执行和停止功能"""
import time
from agent.env import Environment, Action

def main():
    print("=" * 50)
    print("测试 Environment 异步命令执行")
    print("=" * 50)

    # 创建环境
    env = Environment(
        container_name="ai-agent-Bot1",
        screenshot_interval=5.0,  # 降低截图频率以便观察
        exec_timeout=30.0,
        remote_bash_host="localhost",
        remote_bash_port=9090
    )

    print("\n启动环境...")
    env.start()
    time.sleep(2)

    # 测试1: 短命令执行
    print("\n[测试1] 执行短命令...")
    env.send_action(Action(type="exec", content="echo 'Test 1: Quick command' && date"))
    time.sleep(3)

    # 测试2: 长命令 + 停止
    print("\n[测试2] 启动长命令...")
    env.send_action(Action(type="exec", content="sleep 15 && echo 'This should not appear'"))
    time.sleep(2)

    print("[测试2] 停止命令...")
    env.send_action(Action(type="stop_execute"))
    time.sleep(2)

    # 测试3: workspace文件操作
    print("\n[测试3] 在workspace创建文件...")
    env.send_action(Action(type="exec", content="echo 'Created by env.py test' > env_test.txt && cat env_test.txt"))
    time.sleep(3)

    # 测试4: 连续命令（测试任务队列）
    print("\n[测试4] 连续发送多个命令...")
    env.send_action(Action(type="exec", content="echo 'Command 1' && sleep 1"))
    time.sleep(0.5)
    env.send_action(Action(type="exec", content="echo 'Command 2' && sleep 1"))
    time.sleep(0.5)
    env.send_action(Action(type="exec", content="echo 'Command 3' && sleep 1"))
    time.sleep(5)

    # 检查状态
    print("\n[结果] 检查state队列...")
    states = env.get_new_states(0)
    command_results = [s for s in states if s.type == "command_result"]
    print(f"共收集到 {len(command_results)} 个命令结果:")
    for i, state in enumerate(command_results[-5:], 1):  # 只显示最后5个
        print(f"\n  结果 {i}:")
        print(f"    命令: {state.command[:50]}...")
        print(f"    退出码: {state.exit_code}")
        if state.stdout:
            print(f"    输出: {state.stdout[:100]}")

    # 停止环境
    print("\n停止环境...")
    env.stop()
    time.sleep(1)

    print("\n" + "=" * 50)
    print("测试完成！")
    print("=" * 50)

if __name__ == "__main__":
    main()
