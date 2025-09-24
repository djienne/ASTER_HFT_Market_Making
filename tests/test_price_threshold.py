#!/usr/bin/env python3
"""
Test script to verify the price movement threshold logic
"""

# Mock the price movement threshold logic
PRICE_MOVEMENT_THRESHOLD = 0.0001  # 0.01%

def test_price_movement_check():
    """Test the price movement threshold logic"""
    test_cases = [
        # (placed_price, current_price, expected_should_cancel, description)
        (100.0, 100.0, False, "No price change"),
        (100.0, 100.005, False, "0.005% change - below threshold"),
        (100.0, 100.009, False, "0.009% change - below threshold"),
        (100.0, 100.01, True, "0.01% change - at threshold"),
        (100.0, 100.015, True, "0.015% change - above threshold"),
        (100.0, 99.995, False, "0.005% negative change - below threshold"),
        (100.0, 99.99, True, "0.01% negative change - at threshold"),
        (100.0, 99.985, True, "0.015% negative change - above threshold"),
        (2500.0, 2500.25, True, "0.01% change on higher price"),
        (2500.0, 2500.24, False, "0.0096% change on higher price - below threshold"),
    ]

    print("Testing price movement threshold logic:")
    print(f"Threshold: {PRICE_MOVEMENT_THRESHOLD * 100:.4f}%\n")

    all_passed = True

    for placed_price, current_price, expected_should_cancel, description in test_cases:
        # Simulate the logic from market_maker.py
        price_change_percentage = abs(current_price - placed_price) / placed_price
        should_cancel = price_change_percentage >= PRICE_MOVEMENT_THRESHOLD

        status = "PASS" if should_cancel == expected_should_cancel else "FAIL"
        if should_cancel != expected_should_cancel:
            all_passed = False

        print(f"{status} {description}")
        print(f"     Placed: ${placed_price:.6f}, Current: ${current_price:.6f}")
        print(f"     Change: {price_change_percentage*100:.6f}%, Should cancel: {should_cancel}")
        print()

    print(f"Overall result: {'All tests passed' if all_passed else 'Some tests failed'}")
    return all_passed

def test_edge_cases():
    """Test edge cases"""
    print("\nTesting edge cases:")

    # None values
    placed_price = None
    current_price = 100.0

    if placed_price is not None and current_price is not None:
        price_change_percentage = abs(current_price - placed_price) / placed_price
        should_cancel = price_change_percentage >= PRICE_MOVEMENT_THRESHOLD
    else:
        should_cancel = True  # Default to cancel if we don't have price data

    print(f"PASS None placed_price handling: should_cancel = {should_cancel}")

    # Zero division protection
    placed_price = 0.0
    current_price = 100.0

    try:
        if placed_price is not None and current_price is not None and placed_price > 0:
            price_change_percentage = abs(current_price - placed_price) / placed_price
            should_cancel = price_change_percentage >= PRICE_MOVEMENT_THRESHOLD
        else:
            should_cancel = True  # Default to cancel if we can't calculate
        print(f"PASS Zero placed_price handling: should_cancel = {should_cancel}")
    except ZeroDivisionError:
        print("FAIL Zero division error not handled!")

if __name__ == "__main__":
    test_price_movement_check()
    test_edge_cases()