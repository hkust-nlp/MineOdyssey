#!/usr/bin/env python3
"""
Workflow Test for AgentBridge API

Tests a complete crafting workflow to demonstrate how multiple APIs
work together in a realistic scenario.
"""

import sys
import time
sys.path.insert(0, '..')

from minecraft_api import MinecraftAPI


def test_crafting_workflow():
    api = MinecraftAPI()

    print("=" * 70)
    print("AgentBridge API Workflow Test")
    print("Complete Crafting Workflow Demonstration")
    print("=" * 70)
    print()

    # Check connection
    if not api.is_connected():
        print("❌ Not connected to Minecraft")
        return

    print("✅ Connected to Minecraft\n")

    # Step 1: Get initial state
    print("Step 1: Getting player state...")
    state = api.get_state()
    if not state or not state.get('success'):
        print("❌ Could not get state")
        return

    pos = state['data']['position']
    px, py, pz = int(pos['x']), int(pos['y']), int(pos['z'])
    rotation = state['data']['rotation']

    print(f"  Position: ({px}, {py}, {pz})")
    print(f"  Rotation: yaw={rotation['yaw']:.1f}°, pitch={rotation['pitch']:.1f}°")
    print(f"  Health: {state['data']['health']}/{state['data']['max_health']}")
    print(f"  Food: {state['data']['food']}/20")
    print("  ✅ State retrieved")
    print()

    # Step 2: Look at a crafting table location
    print("Step 2: Looking at crafting table location...")
    # Assume crafting table is at player's feet level, 2 blocks in front
    table_x, table_y, table_z = px, py, pz + 2

    api.look_at(table_x, table_y, table_z)
    time.sleep(0.3)
    print(f"  Target: ({table_x}, {table_y}, {table_z})")
    print("  ✅ Looking at crafting table")
    print()

    # Step 3: Try to open crafting table
    print("Step 3: Opening crafting table...")
    result = api.right_click_block(table_x, table_y, table_z)

    if result.get('success'):
        print("  ✅ Right-clicked block (would open GUI if crafting table exists)")
    else:
        print(f"  ⚠️  {result.get('error', 'Failed')}")
        print("  Note: This is expected if no crafting table exists at that position")

    time.sleep(0.5)
    print()

    # Step 4: Demonstrate crafting operations
    print("Step 4: Demonstrating crafting operations...")
    print("  (Simulating crafting 4 planks from 1 log)")
    print()

    # In a real scenario with a crafting table open:
    # Window IDs:
    # - 0: Player inventory
    # - 1+: Opened containers (crafting table, chest, etc.)

    # Crafting table slots:
    # - 0: Output slot
    # - 1-9: Crafting grid (3x3)
    # - 10-46: Player inventory slots

    crafting_operations = [
        {
            "desc": "4a. Pick up log from player inventory (slot 36 = hotbar slot 1)",
            "window": 0,
            "slot": 36,
            "button": 0,
            "type": "PICKUP"
        },
        {
            "desc": "4b. Place log in crafting grid (slot 1)",
            "window": 1,  # Crafting table window
            "slot": 1,
            "button": 0,
            "type": "PICKUP"
        },
        {
            "desc": "4c. Pick up planks from output slot (slot 0)",
            "window": 1,
            "slot": 0,
            "button": 0,
            "type": "PICKUP"
        },
        {
            "desc": "4d. Place planks in player inventory (slot 37)",
            "window": 0,
            "slot": 37,
            "button": 0,
            "type": "PICKUP"
        },
    ]

    for i, op in enumerate(crafting_operations, 1):
        print(f"  {op['desc']}")
        result = api.window_click(
            op['window'],
            op['slot'],
            op['button'],
            op['type']
        )

        if result.get('success'):
            print(f"    ✅ Click successful")
        else:
            print(f"    ⚠️  {result.get('error', 'Failed')}")
            print("    Note: This is expected without an actual crafting table GUI open")

        time.sleep(0.2)

    print()

    # Step 5: Close container
    print("Step 5: Closing crafting table GUI...")
    result = api.close_gui()

    if result.get('success'):
        print("  ✅ Container closed")
    else:
        print(f"  ⚠️  {result.get('error', 'Failed')}")

    print()

    # Step 6: Verify final state
    print("Step 6: Verifying final state...")
    final_state = api.get_state()

    if final_state and final_state.get('success'):
        final_pos = final_state['data']['position']
        print(f"  Final position: ({int(final_pos['x'])}, {int(final_pos['y'])}, {int(final_pos['z'])})")
        print(f"  Health: {final_state['data']['health']}/{final_state['data']['max_health']}")
        print("  ✅ Final state retrieved")
    else:
        print("  ⚠️  Could not get final state")

    print()

    # Summary
    print("=" * 70)
    print("WORKFLOW TEST SUMMARY")
    print("=" * 70)
    print()
    print("APIs Used in This Workflow:")
    print("  1. get_state()           - Get player information")
    print("  2. look_at()             - Aim at the crafting table")
    print("  3. right_click_block()   - Open the crafting table GUI")
    print("  4. window_click()        - Perform crafting operations")
    print("  5. close_gui()     - Close the GUI")
    print()
    print("This demonstrates how multiple APIs work together to perform")
    print("complex tasks in Minecraft. In a real scenario with actual items")
    print("and a crafting table, this sequence would successfully craft items.")
    print()
    print("✅ Workflow test completed!")
    print()

    # Bonus: Demonstrate a simple movement workflow
    print("=" * 70)
    print("BONUS: Movement Workflow")
    print("=" * 70)
    print()
    print("Demonstrating coordinated movement + look:")
    print()

    # Walk in a small circle while looking at center
    circle_steps = [
        (0, "Walking forward..."),
        (90, "Turning right..."),
        (0, "Walking forward..."),
        (90, "Turning right..."),
        (0, "Walking forward..."),
        (90, "Turning right..."),
        (0, "Walking forward..."),
        (90, "Turning right..."),
    ]

    for turn_angle, desc in circle_steps:
        print(f"  {desc}")

        if "Turning" in desc:
            current_state = api.get_state()
            current_yaw = current_state['data']['rotation']['yaw']
            new_yaw = (current_yaw + turn_angle) % 360
            api.set_look(new_yaw, 0)
            time.sleep(0.3)
        else:
            api.move_forward(True)
            time.sleep(0.5)
            api.move_forward(False)
            time.sleep(0.2)

    print()
    print("  ✅ Movement workflow completed")
    print()
    print("=" * 70)


if __name__ == "__main__":
    try:
        test_crafting_workflow()
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        # Stop all movement
        try:
            api = MinecraftAPI()
            api.stop_all_movement()
        except:
            pass
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
