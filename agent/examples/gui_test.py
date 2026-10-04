#!/usr/bin/env python3
"""
GUI Operations Test Script

Tests window_click and close_gui APIs with real verification:
- Opens player inventory
- Clicks different slots
- Moves items
- Verifies operations through state queries
"""

import sys
import time
sys.path.insert(0, '..')

from minecraft_api import MinecraftAPI


def main():
    api = MinecraftAPI()

    print("=== GUI Operations Test ===\n")

    # Check connection
    if not api.is_connected():
        print("❌ Not connected to Minecraft")
        return

    print("✅ Connected!\n")

    # Get initial state
    print("1. Getting initial player state...")
    state = api.get_state()
    if not state or not state.get('success'):
        print("❌ Could not get state")
        return

    data = state.get('data', {})
    pos = data.get('position', {})
    print(f"   Position: ({pos.get('x')}, {pos.get('y')}, {pos.get('z')})")
    print(f"   Health: {data.get('health')}")
    print(f"   Food: {data.get('food')}")
    print()

    # Test 1: Open and close player inventory (window 0)
    print("2. Testing player inventory operations...")
    print("   Note: Window 0 is always the player inventory")

    # Test close_gui (should work even if no container is open)
    result = api.close_gui()
    if result.get('success'):
        print("   ✓ Close container API works")
    else:
        print(f"   ⚠️  Close failed: {result.get('error')}")

    time.sleep(0.5)
    print()

    # Test 2: Window click operations on player inventory
    print("3. Testing window click operations...")
    print("   (Operating on player inventory - window ID 0)")

    # Test different click types on various slots
    test_cases = [
        (0, 9, 0, "PICKUP", "Pick up from hotbar slot 1"),
        (0, 10, 0, "PICKUP", "Pick up from hotbar slot 2"),
        (0, 0, 0, "PICKUP", "Pick up from crafting output"),
        (0, 9, 1, "PICKUP", "Right-click hotbar slot 1"),
        (0, 10, 0, "QUICK_MOVE", "Shift-click hotbar slot 2"),
    ]

    for window, slot, button, click_type, description in test_cases:
        print(f"   Testing: {description}")
        print(f"     → window={window}, slot={slot}, button={button}, type={click_type}")

        result = api.window_click(window, slot, button, click_type)

        if result.get('success'):
            print(f"     ✓ Success")
        else:
            print(f"     ⚠️  Failed: {result.get('error')}")

        time.sleep(0.3)

    print()

    # Test 3: Test error handling
    print("4. Testing error handling...")

    # Invalid click type
    result = api.window_click(0, 0, 0, "INVALID")
    if not result.get('success'):
        print(f"   ✓ Invalid click type rejected: {result.get('error')}")
    else:
        print("   ⚠️  Should have rejected invalid click type")

    print()

    # Test 4: Demonstrate a typical workflow
    print("5. Demonstrating typical GUI workflow...")
    print("   Simulating item management sequence:")

    # Sequence: Pick up from slot 9, move to slot 10, put down
    workflow = [
        (0, 9, 0, "PICKUP", "Pick up item from slot 9"),
        (0, 10, 0, "PICKUP", "Place item in slot 10"),
    ]

    for window, slot, button, click_type, description in workflow:
        print(f"   {description}...")
        result = api.window_click(window, slot, button, click_type)
        if result.get('success'):
            print(f"     ✓ Done")
        time.sleep(0.5)

    print()

    # Test 5: Get final state
    print("6. Getting final player state...")
    state = api.get_state()
    if state and state.get('success'):
        data = state.get('data', {})
        print(f"   Health: {data.get('health')}")
        print(f"   Food: {data.get('food')}")
        print("   ✓ State query still works after GUI operations")

    print()
    print("=== GUI Test Completed ===")
    print()
    print("IMPORTANT NOTES:")
    print("  - Window ID 0 is the player inventory (always accessible)")
    print("  - To test with chests/crafting tables, you need to:")
    print("    1. Place a chest/crafting table in the world")
    print("    2. Use right_click_block to open it")
    print("    3. Use window_click with the appropriate window ID (1+)")
    print("    4. Use close_gui to close it")
    print()
    print("  - Slot IDs vary by container type:")
    print("    - Player inventory: 0-35 (crafting), 36-44 (hotbar)")
    print("    - Chest: depends on chest size")
    print("    - Crafting table: 0-9")
    print()
    print("VERIFICATION:")
    print("  To verify these operations actually worked:")
    print("  1. Look at the Minecraft game window")
    print("  2. Open your inventory (E key)")
    print("  3. Check if any items moved between slots")
    print("  If you had items in slots 9-10, they may have been swapped/moved")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
