"""Operator-only binding of an actual OpenCode session to a preconfigured contract."""
import argparse
import json
import os
import urllib.request
from intercept.policy.auditors import NoRedirect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080", choices=["http://127.0.0.1:8080"])
    args = parser.parse_args()
    token = os.environ.get("INTERCEPT_ADMIN_TOKEN")
    if not token:
        parser.error("INTERCEPT_ADMIN_TOKEN must be supplied only to the operator process")
    request = urllib.request.Request(args.endpoint + "/v1/runs/bind",
        json.dumps({"session_id": args.session, "contract_id": args.contract}).encode(),
        {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=3) as response:
            print(response.read(4096).decode())
    except Exception:
        parser.exit(1, "Session binding failed; no tool permission should be assumed.\n")


if __name__ == "__main__":
    main()
