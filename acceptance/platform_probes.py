"""Native platform probes for relay-desk (used by native_runner.py).

Each probe exercises real relay-desk code on the running OS and filesystem
and returns ``{"name", "applicable", "ok", "details"}``.  ``ok`` is ``None``
for informational probes; a probe that could not run reports ``error`` and
``ok: False`` -- it is never counted as passed.

Covered (plan section 8):
* all:     case sensitivity vs relay-desk's own probe, Unicode normalisation,
           OS lock released when the holder process is killed, ledger lock
           left behind by a killed process, replacing a file another process
           holds open (plain os.replace and relay-desk's retry helper),
           symlink and exec-bit preservation through build/extract/apply
* Windows: reserved device names, invalid characters, trailing dot/space vs
           relay-desk's Windows path rules, long paths, PowerShell invocation
           of relay.cmd with Unicode and spaces, msvcrt lock (via the lock probe)
* POSIX:   shell launcher ``relay``; fcntl lock (via the lock probe)
* macOS:   /var -> /private/var alias canonicalisation
"""
from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from acceptance.common import REPO_ROOT, platform_info, which

IS_WIN = os.name == "nt"


def _probe(name: str, fn: Callable[[Path], Dict[str, Any]], work: Path, applicable: bool = True,
           reason: str = "") -> Dict[str, Any]:
    if not applicable:
        return {"name": name, "applicable": False, "ok": None, "details": {"reason": reason}}
    d = work / name
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        res = fn(d)
        ok = res.pop("ok", None)
        return {"name": name, "applicable": True, "ok": ok, "durationSec": round(time.time() - t0, 2), "details": res}
    except Exception as e:
        return {"name": name, "applicable": True, "ok": False, "durationSec": round(time.time() - t0, 2),
                "error": "%s: %s" % (type(e).__name__, e), "traceback": traceback.format_exc()[-2000:]}


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _wait_file(path: Path, proc: subprocess.Popen, timeout: float = 30) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if path.exists():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def _kill(proc: subprocess.Popen) -> int:
    try:
        if IS_WIN:
            proc.kill()  # TerminateProcess
        else:
            os.kill(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    return proc.wait(timeout=30)


# ---------------------------------------------------------------- filesystem


def probe_case(d: Path) -> Dict[str, Any]:
    from relaydesk import packager

    (d / "CaseProbe.txt").write_text("x", encoding="utf-8")
    actual_insensitive = (d / "caseprobe.txt").exists()
    relay_view = packager._is_fs_case_insensitive(str(d / "target"))
    return {"ok": actual_insensitive == relay_view, "fsCaseInsensitive": actual_insensitive,
            "relayDeskThinksInsensitive": relay_view}


def probe_unicode(d: Path) -> Dict[str, Any]:
    nfd = unicodedata.normalize("NFD", "café-接力.txt")
    nfc = unicodedata.normalize("NFC", nfd)
    (d / nfd).write_text("u", encoding="utf-8")
    listed = os.listdir(str(d))
    return {"ok": None, "listedAsNFD": nfd in listed, "listedAsNFC": nfc in listed and nfc != nfd,
            "nfcOpenable": (d / nfc).exists()}


def _payload_roundtrip(src: Path, d: Path) -> Dict[str, Any]:
    from relaydesk import packager

    tar = d / "payload.tar.gz"
    stage = d / "stage"
    dest = d / "dest"
    packager.build_payload(str(src), str(tar), {"lastTask": "probe", "nextAction": "probe"}, {"sessions": []})
    packager.extract_and_verify_payload(str(tar), str(stage))
    packager.apply_verified_payload(str(stage), str(dest))
    return {"dest": dest}


def probe_symlinks(d: Path) -> Dict[str, Any]:
    """Symlinks: Git-tracked links must survive, links escaping the project must be refused,
    and the plain-folder behaviour is recorded as observed."""
    from relaydesk import packager

    src = d / "src"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    try:
        os.symlink("a.txt", str(src / "link-inside"))
    except (OSError, NotImplementedError) as e:
        return {"ok": None, "symlinkCreation": "unavailable: %s" % e}
    dest = _payload_roundtrip(src, d / "folder")["dest"]
    if os.path.islink(str(dest / "link-inside")):
        folder_mode = "preserved-as-symlink"
    elif (dest / "link-inside").is_file():
        folder_mode = "copied-as-regular-file"
    else:
        folder_mode = "missing"
    # Git mode: committed symlink keeps mode 120000 and stays a link in the worktree
    repo = d / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=probe", "-c", "user.email=probe@relay.invalid", "-C", str(repo)]
    subprocess.run(git + ["init", "-q"], check=True)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    os.symlink("a.txt", str(repo / "link"))
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "link"], check=True)
    gdest = _payload_roundtrip(repo, d / "git")["dest"]
    ls = subprocess.run(["git", "-C", str(gdest), "ls-files", "-s", "link"], stdout=subprocess.PIPE).stdout.decode()
    git_mode_kept = ls.startswith("120000")
    git_link_kept = os.path.islink(str(gdest / "link")) and os.readlink(str(gdest / "link")) == "a.txt"
    out_src = d / "src-out"
    out_src.mkdir()
    (d / "outside.txt").write_text("outside", encoding="utf-8")
    (out_src / "a.txt").write_text("a", encoding="utf-8")
    os.symlink(os.path.join("..", "outside.txt"), str(out_src / "link-out"))
    escaped_rejected, err = False, ""
    try:
        packager.build_payload(str(out_src), str(d / "out.tar.gz"), {}, {"sessions": []})
    except Exception as e:  # relay-desk must refuse a link escaping the project
        escaped_rejected, err = True, "%s: %s" % (type(e).__name__, str(e)[:160])
    return {"ok": escaped_rejected and git_mode_kept and git_link_kept,
            "gitTrackedLinkMode120000": git_mode_kept, "gitWorktreeLinkKept": git_link_kept,
            "plainFolderInsideLink": folder_mode, "escapingLinkRejected": escaped_rejected, "rejection": err}


def probe_exec_bit(d: Path) -> Dict[str, Any]:
    src = d / "src"
    src.mkdir()
    f = src / "run.sh"
    f.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    os.chmod(str(f), 0o755)
    (src / "plain.txt").write_text("p", encoding="utf-8")
    dest = _payload_roundtrip(src, d)["dest"]
    exec_kept = bool(os.stat(str(dest / "run.sh")).st_mode & stat.S_IXUSR)
    plain_not_exec = not bool(os.stat(str(dest / "plain.txt")).st_mode & stat.S_IXUSR)
    return {"ok": exec_kept and plain_not_exec, "execPreserved": exec_kept, "plainStaysNonExec": plain_not_exec}


# ---------------------------------------------------------------- locks and process exit

WORKSPACE_LOCK_HOLDER = r"""
import sys, time
from pathlib import Path
from relaydesk import relay_engine as E
with E._workspace_operation(Path(sys.argv[1])):
    Path(sys.argv[2]).write_text("locked")
    time.sleep(300)
"""

LEDGER_LOCK_HOLDER = r"""
import sys, time
from pathlib import Path
from relaydesk import ledger as L
with L._Lock(sys.argv[1]):
    Path(sys.argv[2]).write_text("locked")
    time.sleep(300)
"""

FILE_HOLDER = r"""
import os, sys, time
held, ready, release = sys.argv[1:4]
fh = open(held, "rb")
with open(ready, "w") as r:
    r.write(str(os.getpid()))
t0 = time.time()
while not os.path.exists(release) and time.time() - t0 < 300:
    time.sleep(0.02)
fh.close()
"""


def probe_workspace_lock(d: Path) -> Dict[str, Any]:
    """msvcrt (Windows) / flock (POSIX) lock held by a process that gets killed."""
    from relaydesk import relay_engine as E

    home = d / "home"
    home.mkdir()
    ready = d / "ready"
    proc = subprocess.Popen([sys.executable, "-c", WORKSPACE_LOCK_HOLDER, str(home), str(ready)], env=_child_env())
    if not _wait_file(ready, proc):
        _kill(proc)
        return {"ok": False, "error": "lock holder did not start"}
    busy_code = None
    try:
        with E._workspace_operation(home):
            busy_code = "acquired-while-held"
    except E.RelayError as e:
        busy_code = getattr(e, "code", "relay-error")
    rc = _kill(proc)
    t0 = time.time()
    acquired = False
    while time.time() - t0 < 10 and not acquired:
        try:
            with E._workspace_operation(home):
                acquired = True
        except E.RelayError:
            time.sleep(0.1)
    return {"ok": busy_code == "busy" and acquired, "whileHeld": busy_code, "holderExit": rc,
            "acquiredAfterKill": acquired, "secondsToAcquire": round(time.time() - t0, 2),
            "mechanism": "msvcrt.locking" if IS_WIN else "fcntl.flock"}


def probe_ledger_lock_after_kill(d: Path) -> Dict[str, Any]:
    """The ledger lock file a killed process leaves behind (O_EXCL lock file)."""
    from relaydesk import ledger as L

    path = d / "ledger.json"
    L.update(path, lambda led: None)
    ready = d / "ready"
    proc = subprocess.Popen([sys.executable, "-c", LEDGER_LOCK_HOLDER, str(path), str(ready)], env=_child_env())
    if not _wait_file(ready, proc):
        _kill(proc)
        return {"ok": False, "error": "ledger lock holder did not start"}
    _kill(proc)
    lock_left = os.path.exists(str(path) + ".lock")
    t0 = time.time()
    first = "updated"
    try:
        L.update(path, lambda led: None)
    except L.LedgerError as e:
        first = "LedgerError: %s" % str(e)[:120]
    first_s = round(time.time() - t0, 2)
    deadline = time.time() + L.LOCK_STALE + 10
    recovered = False
    while time.time() < deadline and not recovered:
        try:
            L.update(path, lambda led: None)
            recovered = True
        except L.LedgerError:
            time.sleep(1)
    return {"ok": recovered, "lockFileLeftByKilledProcess": lock_left, "firstAttempt": first,
            "firstAttemptSeconds": first_s, "recovered": recovered,
            "secondsUntilRecovered": round(time.time() - t0, 1), "lockWait": L.LOCK_WAIT, "lockStale": L.LOCK_STALE}


def probe_replace_open_file(d: Path) -> Dict[str, Any]:
    """Replace a file that another process keeps open (Windows: file in use)."""
    from relaydesk import relay_engine as E

    target = d / "target.txt"
    target.write_text("old", encoding="utf-8")
    ready, release = d / "ready", d / "release"
    proc = subprocess.Popen([sys.executable, "-c", FILE_HOLDER, str(target), str(ready), str(release)])
    if not _wait_file(ready, proc):
        _kill(proc)
        return {"ok": False, "error": "file holder did not start"}
    tmp = d / "new.tmp"
    tmp.write_text("new", encoding="utf-8")
    try:
        os.replace(str(tmp), str(target))
        plain = "replaced"
    except PermissionError as e:
        plain = "PermissionError: %s" % e
    tmp2 = d / "new2.tmp"
    tmp2.write_text("new2", encoding="utf-8")
    timer = threading.Timer(0.3, lambda: release.write_text("x", encoding="utf-8"))
    timer.start()
    try:
        E._replace_with_retry(str(tmp2), str(target))
        retry = "replaced"
    except PermissionError as e:
        retry = "PermissionError: %s" % e
    timer.join()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _kill(proc)
    final = target.read_text(encoding="utf-8")
    return {"ok": retry == "replaced" and final == "new2", "plainReplaceWhileOpen": plain,
            "relayRetryHelperWithReleaseAfter300ms": retry, "finalContent": final,
            "note": "workspace_tx._replace_workspace_file uses plain os.replace (no retry); see loopback L18"}


# ---------------------------------------------------------------- Windows specifics

WIN_NAMES = ["CON", "con.txt", "NUL", "aux.md", "PRN", "COM1", "LPT1.log", "trailing.", "trailing ",
             "a<b.txt", "a>b.txt", "a:b.txt", 'a"b.txt', "a|b.txt", "a?b.txt", "a*b.txt", "normal-name.txt"]


def probe_windows_names(d: Path) -> Dict[str, Any]:
    from relaydesk import packager

    rows = []
    for name in WIN_NAMES:
        try:
            flagged = [m for _, m in packager._find_windows_path_issues([name])]
        except Exception as e:  # rejected even earlier, e.g. "a:b" looks like a drive path
            flagged = ["rejected: %s" % str(e)[:80]]
        row: Dict[str, Any] = {"name": name, "relayDeskFlags": flagged}
        if IS_WIN:
            full = str(d / name)
            mapped = os.path.abspath(full)
            if mapped.startswith("\\\\.\\"):
                row["os"] = "device-mapped:%s" % mapped
            elif any(c in name for c in '<>:"|?*'):
                row["os"] = "invalid-character"
            else:
                try:
                    fd = os.open(full, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(fd)
                    listed = [n for n in os.listdir(str(d)) if n.lower().startswith(name.strip(" .").lower())]
                    row["os"] = "created-exact" if name in listed else "created-altered:%r" % listed
                    for n in listed:
                        try:
                            os.unlink(str(d / n))
                        except OSError:
                            pass
                except OSError as e:
                    row["os"] = "refused:%s" % e.__class__.__name__
            row["consistent"] = (row["os"] == "created-exact") == (not flagged)
        rows.append(row)
    ok = all(r.get("consistent", True) for r in rows) if IS_WIN else None
    return {"ok": ok, "names": rows,
            "note": "OS column only on Windows; elsewhere this lists relay-desk's Windows-target rules"}


def probe_windows_long_path(d: Path) -> Dict[str, Any]:
    deep = d
    while len(str(deep)) < 300:
        deep = deep / ("d" * 40)
    try:
        deep.mkdir(parents=True)
        (deep / "f.txt").write_text("x", encoding="utf-8")
        created = True
        err = ""
    except OSError as e:
        created, err = False, str(e)
    return {"ok": None, "pathLength": len(str(deep)), "created": created, "error": err}


def _isolated_cli_env(d: Path) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RELAY_DESK_")}
    env.update({
        "RELAY_DESK_HOME": str(d / "relay-home"),
        "RELAY_DESK_LEDGER": str(d / "relay-home" / "ledger.json"),
        "RELAY_DESK_USER_HOME": str(d / "user-home"),
        "PYTHONIOENCODING": "utf-8",
    })
    (d / "user-home").mkdir(parents=True, exist_ok=True)
    return env


def _run(argv: List[str], env: Dict[str, str], timeout: float = 120) -> Dict[str, Any]:
    p = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return {"rc": p.returncode, "out": p.stdout.decode("utf-8", "replace"), "err": p.stderr.decode("utf-8", "replace")[-600:]}


def probe_powershell(d: Path) -> Dict[str, Any]:
    import json

    shells = [s for s in ("powershell", "pwsh") if which(s)]
    if not shells:
        return {"ok": False, "error": "no PowerShell found"}
    relay_cmd = str(REPO_ROOT / "relay.cmd")
    results = {}
    all_ok = True
    for sh in shells:
        sd = d / sh
        sd.mkdir()
        env = _isolated_cli_env(sd)
        proj = sd / "带 空格 的 验收目录"
        proj.mkdir()

        def ps(cmd: str) -> Dict[str, Any]:
            return _run([which(sh), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                         "& '%s' %s; exit $LASTEXITCODE" % (relay_cmd.replace("'", "''"), cmd)], env)

        r_init = ps("init --device ps-probe")
        r_dev = ps("device --json")
        r_add = ps("add 'PS 验收 项目' --path '%s' --id ps-probe" % str(proj).replace("'", "''"))
        r_st = ps("status ps-probe --json")
        try:
            dev_ok = r_dev["rc"] == 0 and isinstance(json.loads(r_dev["out"]), dict)
        except ValueError:
            dev_ok = False
        try:
            st = json.loads(r_st["out"])
            path_ok = os.path.normcase(str((st.get("paths") or {}).get("ps-probe") or "")) == os.path.normcase(str(proj))
            name_ok = st.get("name") == "PS 验收 项目"
        except ValueError:
            path_ok = name_ok = False
        ok = r_init["rc"] == 0 and dev_ok and r_add["rc"] == 0 and path_ok and name_ok
        all_ok = all_ok and ok
        results[sh] = {"ok": ok, "init": r_init["rc"], "deviceJson": dev_ok, "add": r_add["rc"],
                       "unicodePathRoundTrip": path_ok, "unicodeNameRoundTrip": name_ok,
                       "stderr": (r_init["err"] + r_add["err"] + r_st["err"])[-600:]}
    return {"ok": all_ok, "shells": results}


def probe_posix_launcher(d: Path) -> Dict[str, Any]:
    import json

    env = _isolated_cli_env(d)
    launcher = str(REPO_ROOT / "relay")
    r_init = _run(["sh", launcher, "init", "--device", "sh-probe"], env)
    r_dev = _run(["sh", launcher, "device", "--json"], env)
    try:
        dev_ok = r_dev["rc"] == 0 and isinstance(json.loads(r_dev["out"]), dict)
    except ValueError:
        dev_ok = False
    return {"ok": r_init["rc"] == 0 and dev_ok, "init": r_init["rc"], "deviceJson": dev_ok,
            "stderr": (r_init["err"] + r_dev["err"])[-400:]}


def probe_macos_var_alias(d: Path) -> Dict[str, Any]:
    from relaydesk import ledger as L

    tmp = tempfile.gettempdir()
    real = os.path.realpath(tmp)
    same = L.canonical_local_path(tmp) == L.canonical_local_path(real)
    return {"ok": same, "tempdir": tmp, "realpath": real, "canonicalEqual": same,
            "varIsSymlink": os.path.islink("/var")}


def run_all(work: Path, quick: bool = False) -> List[Dict[str, Any]]:
    info = platform_info()
    system = info["system"]
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    out = [
        _probe("fs-case-sensitivity", probe_case, work),
        _probe("fs-unicode-normalisation", probe_unicode, work),
        _probe("symlink-preserve-and-escape", probe_symlinks, work),
        _probe("exec-bit-preserved", probe_exec_bit, work, applicable=not IS_WIN, reason="no POSIX exec bit on Windows"),
        _probe("workspace-lock-released-on-kill", probe_workspace_lock, work),
        _probe("ledger-lock-left-by-killed-process", probe_ledger_lock_after_kill, work, applicable=not quick,
               reason="skipped with --quick (takes about 35 s)"),
        _probe("replace-file-held-open", probe_replace_open_file, work),
        _probe("windows-reserved-and-invalid-names", probe_windows_names, work),
        _probe("windows-long-path", probe_windows_long_path, work, applicable=IS_WIN, reason="Windows only"),
        _probe("powershell-invokes-relay-cmd", probe_powershell, work, applicable=IS_WIN, reason="Windows only"),
        _probe("posix-shell-launcher", probe_posix_launcher, work, applicable=not IS_WIN, reason="POSIX only"),
        _probe("macos-var-private-alias", probe_macos_var_alias, work, applicable=system == "Darwin", reason="macOS only"),
    ]
    return out
