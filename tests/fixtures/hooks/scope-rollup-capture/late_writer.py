import os
import subprocess
import sys
import time


capture_dir = os.environ["SCOPE_ROLLUP_CAPTURE_DIR"]
late_write_path = os.path.join(capture_dir, "scope_rollup_capture_late_write.txt")
child_script = (
    "import pathlib, time; "
    "time.sleep(10); "
    f"pathlib.Path({late_write_path!r}).write_text('late', encoding='utf-8')"
)

subprocess.Popen([sys.executable, "-c", child_script], env={"PYTHONUNBUFFERED": "1"})
time.sleep(20)
