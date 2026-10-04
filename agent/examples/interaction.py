#!/usr/bin/env python3
"""
Block Interaction Example

Demonstrates right-clicking blocks and using items:
- Right-clicking blocks (open chests, use buttons, etc.)
- Using items in hand
- Combined interactions
"""

import sys
import time
sys.path.insert(0, '..')

from minecraft_api import MinecraftAPI


def main():
    # Connect to Minecraft
    api = MinecraftAPI()

    print("=== Block Interaction Example ===\n")

    # Check connection
    if not api.is_connected():
        print("❌ Not connected to Minecraft")
        return

    print("✅ Connected!\n")

    # Get current position
    pos = api.get_position()
    if not pos:
        print("❌ Could not get player position")
        return

    px, py, pz = pos
    print(f"Current position: ({px}, {py}, {pz})\n")

    # Test 1: Right-click block in front
    print("1. Right-clicking block in front...")
    print("   (This could open a chest, use a button, etc.)")

    # Look straight ahead
    api.set_look(0, 0)
    time.sleep(0.3)

    # Right-click block in front
    target_x = px
    target_y = py
    target_z = pz + 1

    result = api.right_click_block(target_x, target_y, target_z)
    if result.get('success'):
        print(f"   ✓ Right-clicked block at ({target_x}, {target_y}, {target_z})")
        time.sleep(1)
    else:
        print(f"   ⚠️  Failed: {result.get('error', 'Unknown error')}")

    print()

    # Test 2: Use item in hand
    print("2. Using item in hand (right-click air)...")
    result = api.right_click()
    if result.get('success'):
        print("   ✓ Used item")
    else:
        print(f"   ⚠️  Failed: {result.get('error', 'Unknown error')}")

    print()

    # Test 3: Look at and interact with nearby blocks
    print("3. Interacting with blocks around you...")

    # Define positions relative to player (cardinal directions)
    directions = {
        "front": (px, py, pz + 1),
        "back": (px, py, pz - 1),
        "left": (px - 1, py, pz),
        "right": (px + 1, py, pz),
    }

    for direction, (bx, by, bz) in directions.items():
        print(f"   {direction.capitalize()}: ({bx}, {by}, {bz})")

        # Look at the block
        api.look_at(bx, by, bz)
        time.sleep(0.3)

        # Right-click it
        result = api.right_click_block(bx, by, bz)
        if result.get('success'):
            print(f"      ✓ Interacted")
        else:
            print(f"      ⚠️  No interaction")

        time.sleep(0.5)

    print()

    # Test 4: Demonstration - Place and break workflow
    print("4. Demonstration: Look, place, wait, break")
    print("   (Simulating a typical interaction pattern)")

    demo_x, demo_y, demo_z = px, py - 1, pz + 2

    # Step 1: Look at position
    print(f"   Looking at ({demo_x}, {demo_y}, {demo_z})...")
    api.look_at(demo_x, demo_y, demo_z)
    time.sleep(0.5)

    # Step 2: Right-click (place/interact)
    print("   Right-clicking...")
    api.right_click_block(demo_x, demo_y, demo_z)
    time.sleep(1)

    # Step 3: Wait
    print("   Waiting...")
    time.sleep(1)

    # Step 4: Break
    print("   Breaking...")
    api.break_block(demo_x, demo_y, demo_z)
    time.sleep(1)

    print("   ✓ Workflow completed\n")

    print("=== Interaction example completed! ===")
    print("\nNote: The actual effects depend on:")
    print("  - What blocks are at the target positions")
    print("  - What item is held in hand")
    print("  - Game mode and permissions")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
