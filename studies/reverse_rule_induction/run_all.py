"""One-shot runner: full pipeline then report. Idempotent stages via CHECKPOINT."""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def sh(cmd: str) -> int:
    print(f"\n$ {cmd}", flush=True)
    t0 = time.time()
    code = subprocess.call(cmd, shell=True, cwd=HERE)
    print(f"  -> exit {code} ({time.time()-t0:.0f}s)", flush=True)
    return code


if __name__ == "__main__":
    if sh(f"{sys.executable} -m pytest tests/ -q") != 0:
        sys.exit("self-certification tests failed; aborting")
    if sh(f"{sys.executable} -m rri.pipeline") != 0:
        sys.exit("pipeline failed")
    if sh(f"{sys.executable} -m rri.report") != 0:
        sys.exit("report rendering failed")
    print("\nALL STAGES DONE — see REPORT.md")
