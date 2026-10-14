"""Deployed entry (plan r5 §2.1): ``<hermes>/scripts/clickup_dedicated_panel_intake.py`` next to ``clickup_intake/``
and ``templates/``. The configuration is read from this installation's own runtime directory
(``<hermes>/integrations/clickup-dedicated-panel/config.json``); no path is taken from the command line.
The Hermes ``--no-agent`` cron starts it with no arguments, which means ``run``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clickup_intake.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["run"], root=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
