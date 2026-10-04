#!/usr/bin/env python3
"""
Boundary Test for AgentBridge API

Tests edge cases and invalid parameters to ensure proper error handling.
"""

import sys
sys.path.insert(0, '..')

from minecraft_api import MinecraftAPI


def test_boundary_cases():
    api = MinecraftAPI()

    print("=" * 70)
    print("AgentBridge API Boundary Test")
    print("=" * 70)
    print()

    # Check connection
    if not api.is_connected():
        print("❌ Not connected to Minecraft")
        return

    print("✅ Connected to Minecraft\n")

    test_results = []

    # Test 1: Invalid Input Type
    print("Test 1: Invalid Input Type")
    print("  Testing: set_input with invalid type")
    try:
        result = api.set_input("INVALID_INPUT", True)
        success = not result.get('success')
        error_msg = result.get('error', '')
        print(f"  Result: {'✅ PASS' if success else '❌ FAIL'}")
        if error_msg:
            print(f"  Error: {error_msg}")
        test_results.append(("Invalid Input Type", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Invalid Input Type", False))
    print()

    # Test 2: Extreme Coordinates (very far)
    print("Test 2: Extreme Coordinates (very far)")
    print("  Testing: break_block at (999999999, 255, 999999999)")
    try:
        result = api.break_block(999999999, 255, 999999999)
        # Should either fail or succeed, but not crash
        success = True
        print(f"  Result: ✅ PASS (no crash)")
        print(f"  API Response: {result.get('message', result.get('error', 'Unknown'))}")
        test_results.append(("Extreme Coordinates", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Extreme Coordinates", False))
    print()

    # Test 3: Negative Coordinates
    print("Test 3: Negative Coordinates")
    print("  Testing: break_block at (-1000, -100, -1000)")
    try:
        result = api.break_block(-1000, -100, -1000)
        success = True
        print(f"  Result: ✅ PASS (no crash)")
        print(f"  API Response: {result.get('message', result.get('error', 'Unknown'))}")
        test_results.append(("Negative Coordinates", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Negative Coordinates", False))
    print()

    # Test 4: Invalid Yaw/Pitch (out of range)
    print("Test 4: Invalid Yaw/Pitch Values")
    print("  Testing: set_look with yaw=999, pitch=999")
    try:
        result = api.set_look(999.0, 999.0)
        success = True  # Should handle gracefully
        print(f"  Result: ✅ PASS (no crash)")
        print(f"  API Response: {result.get('message', 'Success')}")
        test_results.append(("Invalid Yaw/Pitch", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Invalid Yaw/Pitch", False))
    print()

    # Test 5: Invalid Window ID
    print("Test 5: Invalid Window ID")
    print("  Testing: window_click with window=-1")
    try:
        result = api.window_click(-1, 0, 0, "PICKUP")
        # Should either fail gracefully or succeed
        success = True
        print(f"  Result: ✅ PASS (no crash)")
        print(f"  API Response: {result.get('message', result.get('error', 'Unknown'))}")
        test_results.append(("Invalid Window ID", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Invalid Window ID", False))
    print()

    # Test 6: Invalid Slot ID (extremely large)
    print("Test 6: Invalid Slot ID")
    print("  Testing: window_click with slot=99999")
    try:
        result = api.window_click(0, 99999, 0, "PICKUP")
        success = True
        print(f"  Result: ✅ PASS (no crash)")
        print(f"  API Response: {result.get('message', result.get('error', 'Unknown'))}")
        test_results.append(("Invalid Slot ID", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Invalid Slot ID", False))
    print()

    # Test 7: Invalid Click Type
    print("Test 7: Invalid Click Type")
    print("  Testing: window_click with type='INVALID_TYPE'")
    try:
        result = api.window_click(0, 9, 0, "INVALID_TYPE")
        success = not result.get('success')  # Should fail
        error_msg = result.get('error', '')
        print(f"  Result: {'✅ PASS' if success else '❌ FAIL'}")
        if error_msg:
            print(f"  Error: {error_msg}")
        test_results.append(("Invalid Click Type", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Invalid Click Type", False))
    print()

    # Test 8: Rapid Sequential Calls (stress test)
    print("Test 8: Rapid Sequential Calls")
    print("  Testing: 50 rapid API calls")
    try:
        success_count = 0
        for i in range(50):
            result = api.health_check()
            if result.get('success'):
                success_count += 1

        success = success_count == 50
        print(f"  Result: {'✅ PASS' if success else '❌ FAIL'}")
        print(f"  Successful calls: {success_count}/50")
        test_results.append(("Rapid Sequential Calls", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Rapid Sequential Calls", False))
    print()

    # Test 9: Float Coordinates (should be converted to int)
    print("Test 9: Float Coordinates")
    print("  Testing: break_block with float coordinates (100.5, 64.7, 200.3)")
    try:
        result = api.break_block(100.5, 64.7, 200.3)
        success = True
        print(f"  Result: ✅ PASS (no crash)")
        print(f"  API Response: {result.get('message', result.get('error', 'Unknown'))}")
        test_results.append(("Float Coordinates", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Float Coordinates", False))
    print()

    # Test 10: Close container when none is open
    print("Test 10: Close Container When None Open")
    print("  Testing: close_gui when no container is open")
    try:
        result = api.close_gui()
        success = result.get('success')  # Should succeed (no-op)
        print(f"  Result: {'✅ PASS' if success else '❌ FAIL'}")
        print(f"  API Response: {result.get('message', 'Success')}")
        test_results.append(("Close Non-Open Container", success))
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected exception: {e}")
        test_results.append(("Close Non-Open Container", False))
    print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()

    passed = sum(1 for _, success in test_results if success)
    total = len(test_results)

    print(f"Tests Passed: {passed}/{total}\n")

    for name, success in test_results:
        status = "✅" if success else "❌"
        print(f"  {status} {name}")

    print()

    if passed == total:
        print("✅ All boundary tests passed! The API handles edge cases gracefully.")
    else:
        print(f"⚠️  {total - passed} test(s) failed. Review error handling.")

    print()
    print("=" * 70)


if __name__ == "__main__":
    try:
        test_boundary_cases()
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
