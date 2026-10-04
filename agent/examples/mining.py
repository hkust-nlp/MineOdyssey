#!/usr/bin/env python3
"""
Mining Example

Demonstrates block breaking:
- Finding and looking at blocks
- Breaking blocks
- Mining in a pattern
"""

import sys
import time
import math
sys.path.insert(0, '..')

from minecraft_api import MinecraftAPI


def main():
    # Connect to Minecraft
    api = MinecraftAPI()

    print("=== Mining Example ===\n")

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

    # Test 1: Break block in front
    print("1. Breaking block in front...")
    # Look straight ahead and slightly down
    api.set_look(0, 15)
    time.sleep(0.5)

    # Calculate block position in front (assuming facing south initially)
    target_x = px
    target_y = py
    target_z = pz + 1

    print(f"   Target block: ({target_x}, {target_y}, {target_z})")

    # Break the block
    result = api.break_block(target_x, target_y, target_z)
    if result.get('success'):
        print("   ✓ Breaking block...")
        time.sleep(1)  # Give time for block to break
    else:
        print(f"   ⚠️  Could not break block: {result.get('error', 'Unknown error')}")

    print()

    # Test 2: Mine 3 blocks in a row
    print("2. Mining 3 blocks in a row...")
    for i in range(3):
        # Look at the block
        api.look_at(target_x, target_y, target_z + i)
        time.sleep(0.3)

        # Break it
        result = api.break_block(target_x, target_y, target_z + i)
        if result.get('success'):
            print(f"   Block {i+1}/3: Breaking ({target_x}, {target_y}, {target_z + i})")
            time.sleep(1)
        else:
            print(f"   Block {i+1}/3: Failed")

    print("   ✓ Row completed\n")

    # Test 3: Mine a 2x2 area
    print("3. Mining a 2x2 area...")
    base_x = px
    base_y = py
    base_z = pz + 4

    blocks_to_mine = [
        (base_x, base_y, base_z),
        (base_x + 1, base_y, base_z),
        (base_x, base_y, base_z + 1),
        (base_x + 1, base_y, base_z + 1),
    ]

    for i, (bx, by, bz) in enumerate(blocks_to_mine, 1):
        # Look at the block
        api.look_at(bx, by, bz)
        time.sleep(0.3)

        # Break it
        result = api.break_block(bx, by, bz)
        if result.get('success'):
            print(f"   Block {i}/4: Breaking ({bx}, {by}, {bz})")
            time.sleep(1)
        else:
            print(f"   Block {i}/4: Failed")

    print("   ✓ 2x2 area completed\n")

    # Test 4: Mine while moving
    print("4. Mining while walking...")
    api.move_forward(True)

    for i in range(3):
        current_pos = api.get_position()
        if current_pos:
            cx, cy, cz = current_pos
            # Mine block ahead
            api.break_block(cx, cy, cz + 2)
            print(f"   Breaking block {i+1}/3 at ({cx}, {cy}, {cz + 2})")
        time.sleep(1)

    api.move_forward(False)
    print("   ✓ Mobile mining completed\n")

    print("=== Mining example completed! ===")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
