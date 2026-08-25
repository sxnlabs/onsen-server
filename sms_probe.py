#!/usr/bin/env python3
"""Send one SMS through the real OVH account, to check the alert path is armed.

Sibling of `probe.py`: that one asks the spa whether it answers, this one asks
the phone network whether an alert would reach you. It proves the transport —
credentials, signature, clock, recipient, credits — and nothing else. The rules
that decide *when* to text live in `intex_spa/alerts.py` and are covered by
`tests/test_alerts.py`; to rehearse those end to end, see the "prod smoke test"
section of DEPLOY.md.

It never opens a socket to the spa, so it is safe to run against a live
container while the supervisor holds its single TCP connection.

    docker compose exec onsen /app/.venv/bin/python sms_probe.py            # remote
    uv run python sms_probe.py                                              # LaunchAgent
    uv run python sms_probe.py --dry-run                                    # resolve config only

Exit codes: 0 sent, 1 not configured, 2 refused by OVH.
"""

from __future__ import annotations

import argparse
import sys

from intex_spa.sms import OvhCredentials, SmsSender, alerting_env

DEFAULT_MESSAGE = "Onsen: test d'alerte. La surveillance du spa est armee."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="body to send")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and print the configuration without sending (and without spending a credit)",
    )
    parser.add_argument(
        "--state",
        default="state/.sms",
        help="key=value fallback file, used when the environment carries nothing (default: state/.sms)",
    )
    args = parser.parse_args(argv)

    env = alerting_env(args.state)
    recipient = (env.get("ONSEN_SMS_TO") or "").strip()
    credentials = OvhCredentials.from_env(env)

    # Half-configured is the failure that hides: the app starts, the UI works,
    # and nothing ever texts. Name which half is missing.
    if not recipient or credentials is None:
        missing = []
        if not recipient:
            missing.append("ONSEN_SMS_TO")
        if credentials is None:
            missing.append("OVH_APPLICATION_KEY/SECRET, OVH_CONSUMER_KEY, OVH_SMS_SERVICE")
        print(f"not configured — missing {', '.join(missing)}", file=sys.stderr)
        print(f"(looked at the environment, then {args.state})", file=sys.stderr)
        return 1

    print(f"recipient: ...{recipient[-4:]}")
    print(f"service:   {credentials.service_name}")
    print(f"sender:    {credentials.sender}")
    if args.dry_run:
        print("dry run — nothing sent")
        return 0

    if not SmsSender(credentials, recipient).send_blocking(args.message):
        print("send failed — see the log line above for what OVH said", file=sys.stderr)
        return 2
    print("sent")
    return 0


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
