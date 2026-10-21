"""
import_profile.py — Seed a hands profile from an existing Chrome profile.

Chrome 136+ refuses `--remote-debugging-port` against the default data
directory, so the hands browser cannot attach to the Chrome a person is already
using. Copying the whole profile works but is slow: a real profile is often
several gigabytes of cache, GPU shaders and service-worker storage that no
automation needs.

This script copies only what carries identity:

    Cookies, Login Data, Web Data, Preferences, Local State,
    Local Storage/, Session Storage/, IndexedDB/ (optional)

Everything else — Cache, Code Cache, GPUCache, Service Worker/CacheStorage,
Crashpad — is skipped. A 4 GB profile usually seeds in a few hundred megabytes.

    python import_profile.py                     # copy Default
    python import_profile.py --profile "Profile 1"
    python import_profile.py --with-indexeddb    # include IndexedDB
    python import_profile.py --dest D:\\hands-profile

The destination is safe to use only while the source Chrome is closed, because
Chrome holds locks on Cookies and Login Data. The script copies to a temporary
directory first and moves it into place, so a half-copied profile never looks
valid.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

#: Files that carry logins, cookies and per-profile settings.
CORE_FILES: tuple[str, ...] = (
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data For Account",
    "Login Data-journal",
    "Web Data",
    "Web Data-journal",
    "Preferences",
    "Secure Preferences",
    "History",
    "Bookmarks",
)

#: Directories that hold site storage.
CORE_DIRS: tuple[str, ...] = (
    "Local Storage",
    "Session Storage",
    "Network",
)

#: Only copied when asked: large and rarely needed for a click-and-type agent.
OPTIONAL_DIRS: tuple[str, ...] = ("IndexedDB",)

#: Never copied.
SKIP_DIRS: frozenset[str] = frozenset({
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "ShaderCache", "GrShaderCache", "Crashpad", "Safe Browsing",
    "Optimization Hints", "segmentation_platform", "Service Worker",
    "component_crx_cache", "extensions_crx_cache", "File System",
    "Platform Notifications", "BudgetDatabase", "Site Characteristics Database",
})


def chrome_user_data() -> Path:
    """Default Chrome user-data root for the current platform."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def copy_tree(src: Path, dst: Path, *, label: str) -> tuple[int, int, list[str]]:
    """Copy a directory, returning (files, bytes, locked filenames)."""
    files = size = 0
    locked: list[str] = []
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = Path(root).relative_to(src)
        target = dst / rel
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        for name in names:
            s = Path(root) / name
            try:
                if s.stat().st_size > 512 * 1024 * 1024:
                    continue
            except OSError:
                continue
            try:
                shutil.copy2(s, target / name)
                files += 1
                size += s.stat().st_size
            except PermissionError:
                locked.append(str(Path(rel) / name))
            except OSError:
                locked.append(str(Path(rel) / name))
    note = f" ({len(locked)} locked)" if locked else ""
    print(f"  {label}: {files} files, {human(size)}{note}")
    return files, size, locked


def copy_file(src: Path, dst: Path, *, label: str) -> tuple[bool, int]:
    """Copy one file, reporting lock failures instead of hiding them.

    Chrome keeps an exclusive lock on Cookies and Login Data while it runs.
    A silent skip here produces a profile that looks seeded but has no logins,
    which is worse than a loud failure.
    """
    try:
        shutil.copy2(src, dst)
        size = dst.stat().st_size
        print(f"  {label}: {human(size)}")
        return True, size
    except PermissionError:
        print(f"  {label}: LOCKED (close Chrome and re-run)")
        return False, 0
    except OSError as exc:
        print(f"  {label}: skipped ({exc.__class__.__name__}: {exc})")
        return False, 0


def chrome_running() -> int:
    """Count running Chrome processes. Used to warn before seeding."""
    import subprocess
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return out.lower().count("chrome.exe")
        out = subprocess.run(
            ["pgrep", "-c", "-f", "Google Chrome"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return int(out or 0)
    except Exception:
        return 0


def close_chrome() -> bool:
    """Terminate Chrome so its profile files unlock. Returns True on success."""
    import subprocess
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"],
                           capture_output=True, timeout=30)
        else:
            subprocess.run(["pkill", "-f", "Google Chrome"],
                           capture_output=True, timeout=30)
        import time
        for _ in range(20):
            if chrome_running() == 0:
                return True
            time.sleep(0.25)
        return chrome_running() == 0
    except Exception:
        return False


def seed(source: Path, dest: Path, *, with_indexeddb: bool) -> None:
    if not source.exists():
        raise SystemExit(f"Source profile not found: {source}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="hands-seed-", dir=str(dest.parent)))
    try:
        print(f"Seeding from {source}")
        print(f"        to {staging}")

        total_files = total_bytes = 0
        locked: list[str] = []

        for name in CORE_FILES:
            src = source / name
            if not src.exists():
                continue
            ok, size = copy_file(src, staging / name, label=name)
            if ok:
                total_files += 1
                total_bytes += size
            else:
                locked.append(name)

        for name in CORE_DIRS + (OPTIONAL_DIRS if with_indexeddb else ()):
            src = source / name
            if not src.is_dir():
                continue
            f, b, sub_locked = copy_tree(src, staging / name, label=name)
            total_files += f
            total_bytes += b
            locked.extend(sub_locked)

        # Chrome needs a Preferences file to accept the directory as a profile.
        if not (staging / "Preferences").exists():
            (staging / "Preferences").write_text("{}", encoding="utf-8")
            print("  Preferences: wrote minimal stub")

        if dest.exists():
            backup = dest.with_suffix(dest.suffix + ".old")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            try:
                dest.rename(backup)
                print(f"  existing profile moved to {backup.name}")
            except OSError as exc:
                # A locked destination must not abort the whole seed; the
                # staging directory is complete and can be swapped manually.
                print(f"  could not move existing profile ({exc.__class__.__name__}); "
                      f"seeded copy left at {staging}")
                raise SystemExit(1)

        try:
            staging.rename(dest)
        except OSError as exc:
            print(f"  could not move seeded profile into place "
                  f"({exc.__class__.__name__}: {exc})")
            print(f"  the seeded copy is complete at {staging}")
            raise SystemExit(1)
        print(f"\nDone: {total_files} files, {human(total_bytes)}")
        print(f"Profile ready at {dest}")

        if locked:
            print("\nWARNING: these files could not be copied because Chrome "
                  "is running:")
            for name in locked:
                print(f"  - {name}")
            print("Close Chrome completely, then re-run this script. Without "
                  "Cookies, no site will treat the profile as signed in.")

        print("\nStart the hands browser with:")
        print(f'  start(profile_dir=r"{dest}")')

    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed a hands profile from an existing Chrome profile."
    )
    parser.add_argument(
        "--source", type=Path, default=None,
        help="Chrome user-data root (default: the platform default)",
    )
    parser.add_argument(
        "--profile", default="Default",
        help='Which profile directory to copy (default: "Default")',
    )
    parser.add_argument(
        "--dest", type=Path, default=Path.home() / ".mcp-hands" / "profile",
        help="Destination profile directory",
    )
    parser.add_argument(
        "--with-indexeddb", action="store_true",
        help="Also copy IndexedDB (larger, needed by some web apps)",
    )
    parser.add_argument(
        "--close-chrome", action="store_true",
        help="Terminate Chrome first so profile files unlock. "
             "Cookies cannot be copied while Chrome is running.",
    )
    args = parser.parse_args()

    root = args.source or chrome_user_data()
    source = root / args.profile

    print(f"Chrome user-data root : {root}")
    print(f"Source profile        : {source}")
    print(f"Destination           : {args.dest}")

    if not source.is_dir():
        available = [p.name for p in root.iterdir()
                     if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile"))]
        raise SystemExit(
            f"Profile {args.profile!r} not found. Available: {available}"
        )

    running = chrome_running()
    if running:
        print(f"\nChrome is running ({running} processes).")
        print("Cookies and Login Data are locked and cannot be copied.")
        if args.close_chrome:
            print("Closing Chrome...")
            if not close_chrome():
                raise SystemExit("Could not close Chrome. Close it manually and re-run.")
            print("Chrome closed.")
        else:
            print("Re-run with --close-chrome, or close Chrome manually first.")
            print("Continuing anyway; locked files will be listed at the end.")

    seed(source, args.dest, with_indexeddb=args.with_indexeddb)


if __name__ == "__main__":
    main()
