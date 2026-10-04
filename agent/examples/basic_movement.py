#!/usr/bin/env python3
"""
Basic Movement Example

Demonstrates basic player control:
- Walking in different directions
- Jumping
- Looking around
- Checking player state
"""

import sys
import time
sys.path.insert(0, '..')

from minecraft_api import MinecraftAPI, InputType


def main():
    # Connect to Minecraft
    api = MinecraftAPI()

    print("=== Basic Movement Example ===\n")

    # Check connection
    print("1. Checking connection...")
    if not api.is_connected():
        print("❌ Not connected to Minecraft. Make sure the game is running.")
        return

    print("✅ Connected!\n")

    # Get initial state
    print("2. Getting initial state...")
    state = api.get_state()
    data = state['data']
    print(f"   Position: ({data['position']['x']}, {data['position']['y']}, {data['position']['z']})")
    print(f"   Rotation: yaw={data['rotation']['yaw']:.1f}°, pitch={data['rotation']['pitch']:.1f}°")
    print(f"   Health: {data['health']}/{data['max_health']}")
    print()

    # Test 1: Jump
    print("3. Testing jump...")
    api.jump(True)
    time.sleep(0.5)
    api.jump(False)
    print("   ✓ Jumped\n")
    time.sleep(1)

    # Test 2: Walk forward
    print("4. Walking forward for 2 seconds...")
    api.move_forward(True)
    time.sleep(2)
    api.move_forward(False)
    print("   ✓ Stopped\n")
    time.sleep(0.5)

    # Check new position
    new_pos = api.get_position()
    if new_pos:
        print(f"   New position: ({new_pos[0]}, {new_pos[1]}, {new_pos[2]})")
        print()

    # Test 3: Turn around
    print("5. Turning around...")
    current_yaw, current_pitch = api.get_rotation() or (0, 0)
    new_yaw = (current_yaw + 180) % 360
    api.set_look(new_yaw, current_pitch)
    print(f"   ✓ Rotated from {current_yaw:.1f}° to {new_yaw:.1f}°\n")
    time.sleep(1)

    # Test 4: Walk in a square
    print("6. Walking in a square (1 second per side)...")
    for i, direction in enumerate(["forward", "right", "back", "left"], 1):
        print(f"   Side {i}: {direction}")

        # Move
        if direction == "forward":
            api.move_forward(True)
        elif direction == "right":
            api.move_right(True)
        elif direction == "back":
            api.move_back(True)
        elif direction == "left":
            api.move_left(True)

        time.sleep(1)

        # Stop
        api.stop_all_movement()
        time.sleep(0.2)

    print("   ✓ Square completed\n")

    # Test 5: Sprint and jump
    print("7. Sprint jumping...")
    api.sprint(True)
    api.move_forward(True)
    time.sleep(0.3)
    api.jump(True)
    time.sleep(0.2)
    api.jump(False)
    time.sleep(0.8)
    api.move_forward(False)
    api.sprint(False)
    print("   ✓ Sprint jump completed\n")

    # Final state
    print("8. Final state:")
    final_pos = api.get_position()
    if final_pos:
        print(f"   Final position: ({final_pos[0]}, {final_pos[1]}, {final_pos[2]})")

    print("\n=== Example completed! ===")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        # Try to stop all movement
        try:
            api = MinecraftAPI()
            api.stop_all_movement()
        except:
            pass
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
