#!/usr/bin/env python3
"""One-time sign-in for the 'signed-in user' mode. Prints / stores a refresh token.

    python login_device.py --client-id <app id> [--tenant consumers|common|<tenant id>] [--out sync_run/token.json]

Open the shown address, enter the code, sign in with the account that owns the OneDrive. Then put the printed
token into the MS_REFRESH_TOKEN secret (or keep the token file on the machine that runs the job).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

from bsync.graph import delegated_scope, login_base


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--tenant", default="common")
    ap.add_argument("--out", help="also write {'refresh_token': ...} to this file")
    a = ap.parse_args(argv)
    base = f"{login_base()}/{a.tenant}/oauth2/v2.0"
    r = requests.post(f"{base}/devicecode", data={"client_id": a.client_id, "scope": delegated_scope()}, timeout=30)
    js = r.json()
    if "device_code" not in js:
        print("could not start the sign-in:", js.get("error_description", js), file=sys.stderr)
        return 2
    print("\n" + js.get("message", f"Open {js['verification_uri']} and enter {js['user_code']}") + "\n")
    deadline = time.time() + int(js.get("expires_in", 900))
    interval = max(0, int(js.get("interval", 5)))
    while time.time() < deadline:
        time.sleep(interval)
        t = requests.post(f"{base}/token", timeout=30, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": a.client_id,
            "device_code": js["device_code"]}).json()
        if "refresh_token" in t:
            print("Signed in. Refresh token (keep it secret):\n")
            print(t["refresh_token"])
            if a.out:
                Path(a.out).parent.mkdir(parents=True, exist_ok=True)
                Path(a.out).write_text(json.dumps({"refresh_token": t["refresh_token"]}), encoding="utf-8")
                print(f"\n(also saved to {a.out})")
            return 0
        if t.get("error") not in ("authorization_pending", "slow_down"):
            print("sign-in failed:", t.get("error_description", t), file=sys.stderr)
            return 1
        if t.get("error") == "slow_down":
            interval += 5
    print("timed out", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
