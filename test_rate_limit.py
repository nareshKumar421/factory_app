"""
Rate Limit Test Script
Tests rate limiting for both anonymous and authenticated users.

Config (from settings.py):
  - anon:  50/hour
  - user: 500/hour

Usage: python test_rate_limit.py
Requires the Django server running at http://127.0.0.1:8000
"""

import requests
import sys

BASE_URL = "http://127.0.0.1:8000"
LOGIN_URL = f"{BASE_URL}/api/v1/accounts/login/"
ME_URL = f"{BASE_URL}/api/v1/accounts/me/"

# --- Test user credentials ---
USERNAME = "ratetest@test.com"
PASSWORD = "testpass123"


def test_anon_rate_limit():
    """Test anonymous user rate limit (50/hour)."""
    print("=" * 60)
    print("TEST: Anonymous User Rate Limit (50/hour)")
    print("=" * 60)

    hit_limit = False
    for i in range(1, 55):
        resp = requests.get(LOGIN_URL)
        print(f"  Request {i:3d}: {resp.status_code}", end="")
        if resp.status_code == 429:
            print("  <-- RATE LIMITED")
            hit_limit = True
            break
        print()

    if hit_limit:
        print(f"\n  PASS: Anon rate limit triggered at request {i}")
    else:
        print(f"\n  FAIL: Sent 54 requests without hitting rate limit")

    return hit_limit


def get_auth_token():
    """Login and return an access token."""
    resp = requests.post(LOGIN_URL, json={
        "email": USERNAME,
        "password": PASSWORD,
    })
    if resp.status_code != 200:
        print(f"  Login failed ({resp.status_code}): {resp.text}")
        return None
    data = resp.json()
    return data.get("access")


def test_user_rate_limit(token):
    """Test authenticated user rate limit (500/hour)."""
    print("\n" + "=" * 60)
    print("TEST: Authenticated User Rate Limit (500/hour)")
    print("=" * 60)

    print(f"  Using pre-fetched token for '{USERNAME}'.\n")
    headers = {"Authorization": f"Bearer {token}"}

    hit_limit = False
    for i in range(1, 510):
        resp = requests.get(ME_URL, headers=headers)
        # Print every 50th request or the last few near the limit
        if i % 50 == 0 or i >= 498 or resp.status_code == 429:
            print(f"  Request {i:3d}: {resp.status_code}", end="")
            if resp.status_code == 429:
                print("  <-- RATE LIMITED")
                hit_limit = True
                break
            print()

    if hit_limit:
        print(f"\n  PASS: User rate limit triggered at request {i}")
    else:
        print(f"\n  FAIL: Sent 509 requests without hitting rate limit")

    return hit_limit


if __name__ == "__main__":
    print("Rate Limit Test")
    print(f"Server: {BASE_URL}\n")

    # Get auth token FIRST (before anon test exhausts the anon quota)
    print("Obtaining auth token before tests...")
    token = get_auth_token()
    if not token:
        print("WARNING: Could not get auth token. Authenticated test will be skipped.\n")

    anon_ok = test_anon_rate_limit()

    # Ask before running the longer authenticated test
    if "--all" not in sys.argv:
        print("\nRun authenticated user test too? (sends ~500 requests)")
        choice = input("y/n [n]: ").strip().lower()
        if choice != "y":
            print("Skipping authenticated test.")
            sys.exit(0 if anon_ok else 1)

    if not token:
        print("\n  SKIP: No auth token available.")
        user_ok = False
    else:
        user_ok = test_user_rate_limit(token)

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"  Anon rate limit:  {'PASS' if anon_ok else 'FAIL'}")
    print(f"  User rate limit:  {'PASS' if user_ok else 'FAIL'}")
    sys.exit(0 if (anon_ok and user_ok) else 1)
