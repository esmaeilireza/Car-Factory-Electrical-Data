#!/usr/bin/env python3
"""
debug.py — One-shot diagnostic runner for NEXUS SCADA.

Usage:
    py debug.py                 # full diagnostic (env + imports + pytest + services)
    py debug.py --pytest-only   # only run the pytest suite, verbose
    py debug.py --imports       # only check module imports
    py debug.py --no-pytest     # everything except pytest (fast)

Output:
    - Console summary (PASS/FAIL per check)
    - Full detailed report saved to: logs/debug/debug_report_<timestamp>.txt
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------- interpreter
def resolve_interpreter() -> tuple[str, str]:
    """Prefer the project venv interpreter over whatever launched debug.py."""
    candidates = [
        ROOT / ".venv" / "Scripts" / "python.exe",   # Windows
        ROOT / ".venv" / "bin" / "python",           # macOS / Linux
        ROOT / "venv" / "Scripts" / "python.exe",
        ROOT / "venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate), "venv"
    return sys.executable, "SYSTEM PYTHON (no venv found)"


PY, PY_SOURCE = resolve_interpreter()

LOG_DIR = ROOT / "logs" / "debug"

PORTS = {
    "PLC Modbus :5020": 5020,
    "Backend :8000": 8000,
    "Streamlit :8501": 8501,
}

SOURCE_DIRS = ["backend", "plc_simulator", "models", "hmi", "dashboard", "tests"]

# Modules that must be importable (adjust names to match your repo).
IMPORT_TARGETS = [
    "backend.api",
    "backend.database",
    "models.industrial_agent.agent",
    "models.industrial_agent.llm_reasoner",
    "obsidian_bridge",
]

CRITICAL_PACKAGES = [
    "fastapi", "uvicorn", "pymodbus", "llama_cpp",
    "streamlit", "pandas", "numpy", "plotly", "pytest",
]

# ---------------------------------------------------------------- helpers

_report_lines: list[str] = []
_counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}


def log(msg: str = "") -> None:
    print(msg)
    _report_lines.append(msg)


def report(name: str, status: str, detail: str = "") -> None:
    _counts[status] += 1
    suffix = f"  {detail}" if detail else ""
    log(f"[{status:<4}] {name}{suffix}")


def section(title: str) -> None:
    bar = "=" * 74
    log("")
    log(bar)
    log(f"  {title}")
    log(bar)


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str]:
    """Run a subprocess, capture combined stdout+stderr as text."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, f"TIMEOUT after {timeout}s: {' '.join(cmd)}"
    except Exception as exc:  # noqa: BLE001
        return 1, f"LAUNCH ERROR: {exc}\n{traceback.format_exc()}"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


# ---------------------------------------------------------------- checks

def check_environment() -> None:
    section("1. ENVIRONMENT")
    report("Interpreter source",
           "PASS" if PY_SOURCE == "venv" else "FAIL",
           f"{PY_SOURCE}: {PY}")
    report(f"Python version {sys.version.split()[0]}",
           "PASS" if sys.version_info >= (3, 11) else "FAIL")

    # Package check via stdlib metadata — immune to pip console noise.
    from importlib.metadata import PackageNotFoundError, version

    installed: dict[str, str] = {}
    for pkg in CRITICAL_PACKAGES:
        try:
            installed[pkg] = version(pkg)
        except PackageNotFoundError:
            pass

    missing = [p for p in CRITICAL_PACKAGES if p not in installed]
    report(
        "Critical packages installed",
        "FAIL" if missing else "PASS",
        f"missing: {missing}" if missing
        else ", ".join(f"{k}=={v}" for k, v in installed.items()),
    )
    for name, ver in installed.items():
        log(f"        {name:<20} {ver}")

    model = ROOT / "models" / "qwen2.5-coder-1.5b-instruct-q6_k.gguf"
    report(
        "GGUF model file present",
        "PASS" if model.exists() else "FAIL",
        f"{model.stat().st_size / 1e6:.0f} MB" if model.exists() else str(model),
    )

    db = ROOT / "data" / "scada.db"
    report("SQLite database present", "PASS" if db.exists() else "FAIL", str(db))



def check_syntax() -> None:
    section("2. SYNTAX / COMPILE CHECK (all source trees)")
    for directory in SOURCE_DIRS:
        path = ROOT / directory
        if not path.exists():
            report(f"compile {directory}/", "SKIP", "directory not found")
            continue
        code, out = run([PY, "-m", "compileall", "-q", str(path)], timeout=120)
        if code == 0:
            report(f"compile {directory}/", "PASS")
        else:
            report(f"compile {directory}/", "FAIL")
            log("    --- compileall output ---")
            for line in out.strip().splitlines()[-40:]:
                log(f"    {line}")


def check_imports() -> None:
    section("3. IMPORT CHECK (catches ModuleNotFoundError / circular imports)")
    for module_name in IMPORT_TARGETS:
        probe = (
            "import importlib, sys; "
            f"mod = importlib.import_module({module_name!r}); "
            "print('OK')"
        )
        code, out = run([PY, "-c", probe], cwd=ROOT, timeout=120)
        if code == 0 and "OK" in out:
            report(f"import {module_name}", "PASS")
        else:
            report(f"import {module_name}", "FAIL")
            log("    --- traceback tail ---")
            for line in out.strip().splitlines()[-25:]:
                log(f"    {line}")


def check_pytest() -> list[str]:
    """Run pytest verbosely. Returns list of FAILED test node ids."""
    section("4. PYTEST SUITE (verbose, full tracebacks)")
    cmd = [
        PY, "-m", "pytest", "tests/",
        "-vv", "--tb=long", "--showlocals", "--maxfail=0",
        "-p", "no:cacheprovider",
    ]
    log(f"    $ {' '.join(cmd)}\n")
    code, out = run(cmd, timeout=1800)

    # Echo the tail: summary + every FAILED/ERROR line
    lines = out.splitlines()
    interesting = [
        ln for ln in lines
        if re.search(r"(FAILED|ERROR|PASSED|assert|Exception|Error)", ln)
    ]
    for ln in lines[-60:]:
        log(f"    {ln}")

    failed = sorted({
        m.group(1)
        for ln in lines
        if (m := re.match(r"(?:FAILED|ERROR)\s+(\S+)", ln))
    })

    log("")
    if code == 0:
        report("pytest suite", "PASS", "all tests green")
    else:
        report("pytest suite", "FAIL", f"{len(failed)} failing test(s)")
        if failed:
            log("    Failing tests:")
            for node in failed:
                log(f"      - {node}")
    return failed


def rerun_failures_individually(failed: list[str]) -> None:
    """Run each failing test alone to surface order-dependent failures."""
    if not failed:
        return
    section("5. ISOLATED RERUN OF EACH FAILING TEST")
    for node in failed:
        code, out = run([PY, "-m", "pytest", node, "-vv", "--tb=long",
                         "--showlocals", "-p", "no:cacheprovider"], timeout=300)
        if code == 0:
            report(f"isolated: {node}", "PASS",
                   "passes alone -> ORDER-DEPENDENT or shared-state failure")
        else:
            report(f"isolated: {node}", "FAIL")
            log("    --- error block ---")
            for line in out.splitlines():
                if re.search(r"(Error|assert|raise|FAILED|Exception)", line):
                    log(f"    {line}")
            log("    Full traceback saved in report file below.")


def check_services() -> None:
    section("6. SERVICE PORTS")
    for label, port in PORTS.items():
        report(label, "PASS" if port_open(port) else "FAIL",
               "reachable" if port_open(port) else "not running (start services if tests need them)")


def check_backend_api() -> None:
    section("7. BACKEND API SMOKE (requires backend on :8000)")
    if not port_open(8000):
        report("GET /api/health", "SKIP", "backend not running")
        return
    code, out = run([PY, "-c",
                     "import urllib.request; "
                     "print(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5).read().decode())"])
    report("GET /api/health", "PASS" if code == 0 else "FAIL", out.strip()[:120])


def write_report() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"debug_report_{stamp}.txt"
    header = [
        f"NEXUS SCADA debug report  {datetime.now().isoformat(timespec='seconds')}",
        f"Python {sys.version}  |  {ROOT}",
        "=" * 74, "",
    ]
    path.write_text("\n".join(header + _report_lines), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="NEXUS SCADA diagnostics")
    parser.add_argument("--pytest-only", action="store_true")
    parser.add_argument("--imports", action="store_true")
    parser.add_argument("--no-pytest", action="store_true")
    args = parser.parse_args()

    log(f"NEXUS SCADA diagnostics — {datetime.now().isoformat(timespec='seconds')}")
    t0 = time.time()

    if args.pytest_only:
        failed = check_pytest()
        rerun_failures_individually(failed)
    elif args.imports:
        check_imports()
    else:
        check_environment()
        check_syntax()
        check_imports()
        failed = [] if args.no_pytest else check_pytest()
        if not args.no_pytest:
            rerun_failures_individually(failed)
        check_services()
        check_backend_api()

    path = write_report()
    section("SUMMARY")
    log(f"    PASS: {_counts['PASS']}   FAIL: {_counts['FAIL']}   SKIP: {_counts['SKIP']}")
    log(f"    Elapsed: {time.time() - t0:.1f}s")
    log(f"    Full report saved to: {path}")
    return 0 if _counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())