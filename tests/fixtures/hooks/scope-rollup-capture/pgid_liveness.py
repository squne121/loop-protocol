import json
import os
import sys
import time

capture_dir = os.environ["SCOPE_ROLLUP_CAPTURE_DIR"]
pid_file = os.path.join(capture_dir, "pgid_liveness_pids.json")
with open(pid_file, "w", encoding="utf-8") as handle:
    json.dump({"pid": os.getpid(), "pgid": os.getpgrp()}, handle)
sys.stdout.flush()
time.sleep(30)
