"""Send ONE test alert through your real email settings.

    $env:HEALOPS_SMTP_PASSWORD = "<app password>"
    python examples\\live_email_check.py --config selfhealing.yaml

The config must contain an `email:` section (see selfhealing.example.yaml). The password is read
from the environment variable named in `password_env`; it is never printed.
"""

import argparse
import sys

from healops.healing.notify import Alert, AlertKind, SafeNotifier
from healops.healing.smtp import SmtpNotifier
from healops.loader import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    if config.email is None:
        print("error: the config has no email section")
        return 2

    alert = Alert(
        kind=AlertKind.APPROVAL_NEEDED,
        incident_id="0123456789abcdef0123456789abcdef",
        app_name="email-check",
        incident_type="health_failure",
        title="restart_component needs your approval (this is only a test)",
        details=("incident: test alert from live_email_check.py", "proposal_id: test"),
    )
    [result] = SafeNotifier([SmtpNotifier(config.email)]).send(alert)
    if result.delivered:
        print(f"sent to {', '.join(config.email.recipients)}; check the inbox (and spam)")
        return 0
    print(f"NOT sent: {result.error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())