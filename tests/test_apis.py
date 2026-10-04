#!/usr/bin/env python3
"""Test script for AgentBridge and Remote Bash APIs"""

import sys
sys.path.insert(0, 'agent')

from minecraft_api import MinecraftAPI, RemoteBashAPI

def test_agentbridge():
    """Test AgentBridge API"""
    print("=== Testing AgentBridge API ===")
    api = MinecraftAPI()

    # Test connection
    print(f"✓ Connected: {api.is_connected()}")

    # Test get_state
    state = api.get_state()
    print(f"✓ Position: {state['data']['position']}")
    print(f"✓ Health: {state['data']['health']}")

    print()

def test_remote_bash():
    """Test Remote Bash API"""
    print("=== Testing Remote Bash API ===")
    bash = RemoteBashAPI()

    # Test connection
    print(f"✓ Connected: {bash.is_connected()}")

    # Test simple command
    result = bash.exec('echo "Hello from bash"')
    print(f"✓ Echo test: {result['stdout'].strip()}")

    # Test ls command
    result = bash.exec('ls /app/game | wc -l')
    print(f"✓ Files in /app/game: {result['stdout'].strip()}")

    # Test xdotool
    result = bash.exec('export DISPLAY=:0 && xdotool getwindowfocus getwindowname')
    print(f"✓ Current window: {result['stdout'].strip()}")

    # Test finding Minecraft window
    result = bash.exec('export DISPLAY=:0 && xdotool search --name Minecraft')
    if result['exit_code'] == 0:
        print(f"✓ Minecraft window ID: {result['stdout'].strip()}")
    else:
        print(f"⚠ Minecraft window not found")
    print()

if __name__ == '__main__':
    try:
        test_agentbridge()
        test_remote_bash()
        print("🎉 All tests passed!")
    except Exception as e:
        print(f"❌ Test failed: {e}")
        sys.exit(1)
