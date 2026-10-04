#!/usr/bin/env python3
"""
Performance Test for AgentBridge API

Tests response time for all API endpoints to ensure they meet
the < 10ms target.
"""

import sys
import time
import statistics
sys.path.insert(0, '..')

from minecraft_api import MinecraftAPI


def measure_api_call(func, *args, **kwargs):
    """Measure the response time of an API call."""
    start = time.perf_counter()
    result = func(*args, **kwargs)
    end = time.perf_counter()
    elapsed_ms = (end - start) * 1000
    return elapsed_ms, result


def run_performance_test():
    api = MinecraftAPI()

    print("=" * 70)
    print("AgentBridge API Performance Test")
    print("=" * 70)
    print()

    # Check connection
    if not api.is_connected():
        print("❌ Not connected to Minecraft")
        return

    print("✅ Connected to Minecraft\n")

    # Get player position for tests
    state = api.get_state()
    if not state or not state.get('success'):
        print("❌ Could not get state")
        return

    pos = state['data']['position']
    px, py, pz = int(pos['x']), int(pos['y']), int(pos['z'])

    print(f"Player position: ({px}, {py}, {pz})\n")
    print("=" * 70)
    print()

    # Define test cases
    test_cases = [
        {
            "name": "Health Check",
            "func": api.health_check,
            "args": [],
            "iterations": 100
        },
        {
            "name": "Get State",
            "func": api.get_state,
            "args": [],
            "iterations": 100
        },
        {
            "name": "Set Input (Move Forward)",
            "func": api.move_forward,
            "args": [True],
            "iterations": 100
        },
        {
            "name": "Set Look Direction",
            "func": api.set_look,
            "args": [90.0, 0.0],
            "iterations": 100
        },
        {
            "name": "Break Block",
            "func": api.break_block,
            "args": [px, py - 1, pz],
            "iterations": 50
        },
        {
            "name": "Right Click Block",
            "func": api.right_click_block,
            "args": [px, py - 1, pz],
            "iterations": 50
        },
        {
            "name": "Right Click (Air)",
            "func": api.right_click,
            "args": [],
            "iterations": 50
        },
        {
            "name": "Window Click",
            "func": api.window_click,
            "args": [0, 9, 0, "PICKUP"],
            "iterations": 50
        },
        {
            "name": "Close Container",
            "func": api.close_gui,
            "args": [],
            "iterations": 50
        }
    ]

    results = []

    # Run tests
    for test in test_cases:
        name = test["name"]
        func = test["func"]
        args = test["args"]
        iterations = test["iterations"]

        print(f"Testing: {name}")
        print(f"  Iterations: {iterations}")

        times = []
        success_count = 0

        for i in range(iterations):
            elapsed_ms, result = measure_api_call(func, *args)
            times.append(elapsed_ms)

            if isinstance(result, dict) and result.get('success'):
                success_count += 1

        # Calculate statistics
        avg = statistics.mean(times)
        median = statistics.median(times)
        min_time = min(times)
        max_time = max(times)
        stdev = statistics.stdev(times) if len(times) > 1 else 0

        # Determine if passed (< 10ms average)
        passed = avg < 10.0
        status = "✅ PASS" if passed else "⚠️  SLOW"

        print(f"  Average:   {avg:.2f} ms  {status}")
        print(f"  Median:    {median:.2f} ms")
        print(f"  Min:       {min_time:.2f} ms")
        print(f"  Max:       {max_time:.2f} ms")
        print(f"  Std Dev:   {stdev:.2f} ms")
        print(f"  Success:   {success_count}/{iterations}")
        print()

        results.append({
            "name": name,
            "avg": avg,
            "median": median,
            "min": min_time,
            "max": max_time,
            "passed": passed
        })

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()

    passed_count = sum(1 for r in results if r["passed"])
    total_count = len(results)

    print(f"Tests Passed: {passed_count}/{total_count}\n")

    print("API Endpoint Rankings (by average response time):")
    sorted_results = sorted(results, key=lambda x: x["avg"])

    for i, result in enumerate(sorted_results, 1):
        status = "✅" if result["passed"] else "⚠️"
        print(f"  {i}. {status} {result['name']:<25} {result['avg']:.2f} ms")

    print()

    # Performance analysis
    slowest = max(results, key=lambda x: x["avg"])
    fastest = min(results, key=lambda x: x["avg"])

    print("Performance Analysis:")
    print(f"  Fastest API: {fastest['name']} ({fastest['avg']:.2f} ms)")
    print(f"  Slowest API: {slowest['name']} ({slowest['avg']:.2f} ms)")

    overall_avg = statistics.mean([r["avg"] for r in results])
    print(f"  Overall Average: {overall_avg:.2f} ms")

    if overall_avg < 10.0:
        print(f"\n✅ All APIs meet the < 10ms performance target!")
    else:
        print(f"\n⚠️  Some APIs exceed the 10ms target")

    print()
    print("=" * 70)

    # Cleanup: stop all movement
    api.stop_all_movement()


if __name__ == "__main__":
    try:
        run_performance_test()
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
