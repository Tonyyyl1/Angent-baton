#!/usr/bin/env python3
"""Environment probe for relay-desk acceptance runs.

Records the source digest, the real OS and version, properties of the
filesystem holding the work directory (case sensitivity, symlinks, exec bit,
Unicode normalisation, long paths on Windows) and the Python / Git / rclone /
Node / PowerShell versions.  Output is JSON.

    python3 acceptance/collect_env.py [--workdir DIR] [--out FILE]

It never reads rclone, cloud or identity configuration; for rclone only the
binary version is recorded.
"""
from __future__ import annotations

import argparse
import json
import locale
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance.common import (  # noqa: E402
    REPO_ROOT,
    detect_level,
    iso_now,
    machine_fingerprint,
    platform_info,
    source_identity,
    which,
    write_json,
)

GIT_CONFIG_KEYS = [
    "core.autocrlf",
    "core.eol",
    "core.filemode",
    "core.ignorecase",
    "core.symlinks",
    "core.precomposeunicode",
    "core.longpaths",
    "core.quotepath",
    "init.defaultBranch",
]


def _run(argv: List[str], timeout: float = 30, cwd: Optional[str] = None) -> Dict[str, Any]:
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, cwd=cwd)
        return {
            "rc": p.returncode,
            "out": p.stdout.decode("utf-8", "replace").strip(),
            "err": p.stderr.decode("utf-8", "replace").strip()[:400],
        }
    except (OSError, subprocess.SubprocessError) as e:
        return {"rc": None, "out": "", "err": str(e)}


# ---------------------------------------------------------------- filesystem


def _fs_type(path: str) -> Dict[str, Any]:
    system = platform_info()["system"]
    if system == "Windows":
        try:
            import ctypes

            drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
            name_buf = ctypes.create_unicode_buffer(261)
            fs_buf = ctypes.create_unicode_buffer(261)
            serial = ctypes.c_ulong()
            max_len = ctypes.c_ulong()
            flags = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
                ctypes.c_wchar_p(drive), name_buf, 261, ctypes.byref(serial),
                ctypes.byref(max_len), ctypes.byref(flags), fs_buf, 261,
            )
            if ok:
                return {"fsType": fs_buf.value, "volume": drive, "maxComponentLength": max_len.value}
            return {"fsType": None, "error": "GetVolumeInformationW failed"}
        except Exception as e:  # pragma: no cover - Windows only
            return {"fsType": None, "error": str(e)}
    if system == "Linux":
        r = _run(["findmnt", "-n", "-o", "FSTYPE,TARGET,SOURCE", "-T", path])
        if r["rc"] == 0 and r["out"]:
            parts = r["out"].split()
            return {"fsType": parts[0], "mountPoint": parts[1] if len(parts) > 1 else "", "source": parts[2] if len(parts) > 2 else ""}
        r = _run(["stat", "-f", "-c", "%T", path])
        return {"fsType": r["out"] or None, "method": "stat -f"}
    if system == "Darwin":
        df = _run(["df", "-P", path])
        mount_point = ""
        lines = df["out"].splitlines()
        if len(lines) >= 2:
            mount_point = lines[-1].split()[-1]
        mt = _run(["mount"])
        for line in mt["out"].splitlines():
            # "/dev/disk3s1 on /System/Volumes/Data (apfs, local, journaled, nobrowse)"
            if " on %s (" % mount_point in line:
                fs = line.split(" (", 1)[1].split(",", 1)[0]
                return {"fsType": fs, "mountPoint": mount_point, "mountLine": line}
        return {"fsType": None, "mountPoint": mount_point}
    return {"fsType": None}


def probe_filesystem(workdir: Path) -> Dict[str, Any]:
    """Probe the filesystem the acceptance work directory lives on."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    res: Dict[str, Any] = {"path": str(workdir), "realpath": os.path.realpath(str(workdir))}
    res.update(_fs_type(str(workdir)))
    probe = Path(tempfile.mkdtemp(prefix="fsprobe-", dir=str(workdir)))
    try:
        # Case sensitivity
        try:
            (probe / "CaseProbe.txt").write_text("x", encoding="utf-8")
            res["caseSensitive"] = not (probe / "caseprobe.txt").exists()
        except OSError as e:
            res["caseSensitive"] = None
            res["caseError"] = str(e)
        # Symlinks
        try:
            (probe / "target.txt").write_text("t", encoding="utf-8")
            os.symlink("target.txt", str(probe / "link.txt"))
            res["symlinks"] = os.path.islink(str(probe / "link.txt")) and (probe / "link.txt").read_text() == "t"
        except (OSError, NotImplementedError) as e:
            res["symlinks"] = False
            res["symlinkError"] = str(e)
        # Exec bit
        if os.name == "nt":
            res["execBit"] = None
            res["execBitNote"] = "Windows has no POSIX exec bit"
        else:
            try:
                f = probe / "run.sh"
                f.write_text("#!/bin/sh\n", encoding="utf-8")
                os.chmod(str(f), 0o755)
                res["execBit"] = bool(os.stat(str(f)).st_mode & stat.S_IXUSR)
            except OSError as e:
                res["execBit"] = False
                res["execBitError"] = str(e)
        # Unicode normalisation: create an NFD name, look at what listdir returns
        try:
            nfd = unicodedata.normalize("NFD", "café.txt")
            nfc = unicodedata.normalize("NFC", nfd)
            (probe / nfd).write_text("u", encoding="utf-8")
            listed = [n for n in os.listdir(str(probe)) if n.startswith("caf")]
            res["unicode"] = {
                "createdForm": "NFD",
                "listedForm": "NFD" if listed and listed[0] == nfd else ("NFC" if listed and listed[0] == nfc else repr(listed)),
                "openableAsNFC": (probe / nfc).exists(),
            }
        except OSError as e:
            res["unicode"] = {"error": str(e)}
        # Windows long paths and reserved names
        if os.name == "nt":
            try:
                deep = probe
                while len(str(deep)) < 300:
                    deep = deep / ("d" * 40)
                deep.mkdir(parents=True)
                (deep / "f.txt").write_text("x", encoding="utf-8")
                res["longPaths"] = True
            except OSError as e:
                res["longPaths"] = False
                res["longPathsError"] = str(e)
    finally:
        shutil.rmtree(str(probe), ignore_errors=True)
    res["tempdir"] = tempfile.gettempdir()
    res["tempdirRealpath"] = os.path.realpath(tempfile.gettempdir())
    return res


# ---------------------------------------------------------------- tools


def _first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text else ""


def probe_tools() -> Dict[str, Any]:
    tools: Dict[str, Any] = {}
    tools["python"] = {
        "version": sys.version.split()[0],
        "full": sys.version,
        "implementation": sys.implementation.name,
        "executable": sys.executable,
        "filesystemEncoding": sys.getfilesystemencoding(),
        "preferredEncoding": locale.getpreferredencoding(False),
        "utf8Mode": bool(getattr(sys.flags, "utf8_mode", 0)),
    }
    try:
        import nacl  # type: ignore

        tools["pynacl"] = {"version": getattr(nacl, "__version__", "?")}
    except Exception as e:
        tools["pynacl"] = {"version": None, "error": str(e)}

    git = which("git")
    if git:
        neutral = tempfile.mkdtemp(prefix="gitcfg-")
        try:
            cfg = {}
            for key in GIT_CONFIG_KEYS:
                r = _run([git, "config", "--show-origin", "--get", key], cwd=neutral)
                cfg[key] = r["out"] if r["rc"] == 0 else None
        finally:
            shutil.rmtree(neutral, ignore_errors=True)
        tools["git"] = {"path": git, "version": _first_line(_run([git, "--version"])["out"]), "config": cfg}
    else:
        tools["git"] = {"path": None}

    rclone = os.environ.get("RELAY_DESK_RCLONE") or which("rclone")
    tools["rclone"] = {
        "path": rclone,
        "version": _first_line(_run([rclone, "version"])["out"]) if rclone else None,
        "note": "version only; remotes and config are never read",
    }
    node = which("node")
    tools["node"] = {"path": node, "version": _first_line(_run([node, "--version"])["out"]) if node else None}
    for ps in ("powershell", "pwsh"):
        exe = which(ps)
        if exe:
            r = _run([exe, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"])
            tools[ps] = {"path": exe, "version": r["out"] or None}
        else:
            tools[ps] = {"path": None}
    return tools


def collect(workdir: Optional[Path] = None, kind: str = "native") -> Dict[str, Any]:
    info = platform_info()
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="relay-acc-env-"))
    return {
        "schema": "relay-desk-acceptance-env/1",
        "createdAt": iso_now(),
        "level": detect_level(kind, info),
        "levelKind": kind,
        "platform": info,
        "machineId": machine_fingerprint(),
        "source": source_identity(REPO_ROOT),
        "repoPath": str(REPO_ROOT),
        "filesystem": probe_filesystem(work),
        "tools": probe_tools(),
        "env": {
            "TMPDIR": os.environ.get("TMPDIR"),
            "relayDeskVarsSet": sorted(k for k in os.environ if k.startswith("RELAY_DESK_")),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="relay-desk acceptance environment probe")
    ap.add_argument("--workdir", help="directory whose filesystem is probed (default: a new temp dir)")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)
    data = collect(Path(args.workdir) if args.workdir else None)
    if args.out:
        write_json(Path(args.out), data)
        print(json.dumps({"out": args.out, "level": data["level"], "sourceDigest": data["source"]["sourceDigest"]}))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
