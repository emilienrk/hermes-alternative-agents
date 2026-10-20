"""Former name of task_run.py, kept for the cron scripts of tasks created before Claude Code joined."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().with_name("task_run.py")), run_name="__main__")
