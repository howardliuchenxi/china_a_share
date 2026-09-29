"""One-shot runner: self-tests -> universe -> fetch -> evaluate -> report."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def sh(cmd: str) -> int:
    print(f"\n$ {cmd}", flush=True)
    return subprocess.call(cmd, shell=True, cwd=HERE)


def main() -> int:
    if sh(f"{sys.executable} -m pytest tests/ -q") != 0:
        return 1
    if sh(f"{sys.executable} s0_universe.py") != 0:
        return 1
    if sh(f"{sys.executable} s0_fetch.py") != 0:
        return 1
    if sh(f"{sys.executable} -m rfr.pipeline") != 0:
        return 1
    print("\nALL STAGES DONE — see REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
