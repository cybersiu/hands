"""
reload.py — Stop the running hands MCP server so the next start picks up edits.

Python imports are cached per process. Editing ``browser.py`` or ``attach.py``
has no effect on a server that is already running, and restarting the client
does not always help: OpenCode keeps talking to the old process if it is still
alive.

Run this after changing any module:

    python reload.py

Then restart the client, or call any hands tool — the client respawns the
server on demand.
"""

from __future__ import annotations

import subprocess
import sys
import time


def find_servers() -> list[tuple[int, str]]:
    """Return (pid, command line) for hands MCP server processes."""
    if sys.platform == "win32":
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*mcp-hands*server.py*' } | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
        ).stdout
    else:
        out = subprocess.run(
            ["pgrep", "-af", "mcp-hands/server.py"],
            capture_output=True, text=True, timeout=30,
        ).stdout

    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        pid_text, cmd = line.split("|", 1)
        if pid_text.isdigit():
            found.append((int(pid_text), cmd))
    return found


def kill(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    result = subprocess.run(["kill", "-9", str(pid)], timeout=30)
    return result.returncode == 0


def main() -> int:
    servers = find_servers()
    if not servers:
        print("No hands MCP server is running. Nothing to stop.")
        print("Edits will take effect the next time a client starts it.")
        return 0

    print(f"Found {len(servers)} hands server process(es):")
    for pid, cmd in servers:
        print(f"  {pid}: {cmd[:100]}")

    failed = []
    for pid, _ in servers:
        if kill(pid):
            print(f"  stopped {pid}")
        else:
            print(f"  FAILED to stop {pid}")
            failed.append(pid)

    time.sleep(1.0)

    remaining = find_servers()
    if remaining:
        print(f"\n{len(remaining)} process(es) still running:")
        for pid, _ in remaining:
            print(f"  {pid}")
        return 1

    print("\nAll stopped. Restart the client, or call a hands tool to respawn.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
