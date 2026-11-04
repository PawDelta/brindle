"""Time the macOS --after-exit-of startup gate with a live launchd daemon.

Run before and after a change with the corresponding cargo-built binary.
All fixture files stay in the specified worktree output directory. The probe
launches the executable in a throwaway bundle directly, measures when it claims
app.lock, and removes only its own app process, launchd job, and fixture files.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import plistlib
import select
import shutil
import subprocess
import tempfile
import time


def probe(binary: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="relaunch-", dir=output) as fixture:
        root = Path(fixture).resolve()
        home = root / "home"
        home.mkdir()
        bundle = root / "RelaunchProbe.app"
        executable = bundle / "Contents/MacOS/brindle-desktop"
        executable.parent.mkdir(parents=True)
        shutil.copy2(binary, executable)
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": executable.name,
            "CFBundleIdentifier": "com.pawdelta.brindle.relaunch-probe",
            "CFBundlePackageType": "APPL",
        }))
        # The old app is already gone, but its daemon will remain alive.
        predecessor = subprocess.Popen(["/usr/bin/true"])
        predecessor.wait()
        home_hash = 0xCBF29CE484222325
        for byte in os.fsencode(home):
            home_hash = ((home_hash ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        label = f"com.pawdelta.brindle.daemon.{home_hash:016x}.{predecessor.pid}"
        domain = f"gui/{os.getuid()}"
        service = f"{domain}/{label}"
        plist = root / "daemon.plist"
        app = None
        fifo = root / "input"
        os.mkfifo(fifo)
        with closing(select.kqueue()) as events:
            plist.write_bytes(plistlib.dumps({
                "Label": label,
                "ProgramArguments": ["/bin/cat", str(fifo)],
                "RunAtLoad": False,
                "KeepAlive": False,
            }))
            lock = home / "app.lock"
            lock.touch()
            with lock.open("rb") as claim, (root / "app.log").open("wb") as log:
                change = select.kevent(claim.fileno(), filter=select.KQ_FILTER_VNODE,
                                       flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                       fflags=select.KQ_NOTE_WRITE)
                events.control([change], 0)
                try:
                    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)], check=True)
                    subprocess.run(["/bin/launchctl", "kickstart", service], check=True)
                    with fifo.open("wb"):
                        before = subprocess.check_output(["/bin/launchctl", "print", service], text=True)
                        daemon_pid = next(int(line.split(" = ")[1]) for line in before.splitlines()
                                          if line.strip().startswith("pid = "))
                        began = time.monotonic()
                        app = subprocess.Popen([str(executable), "--after-exit-of", str(predecessor.pid)],
                                               env={**os.environ, "BRINDLE_HOME": str(home)},
                                               stdout=log, stderr=log)
                        if not events.control(None, 1, 40):
                            raise RuntimeError("app did not reach its startup claim")
                        elapsed = time.monotonic() - began
                        os.kill(daemon_pid, 0)
                        return {"seconds_to_app_claim": round(elapsed, 3),
                                "daemon_pid": daemon_pid, "daemon_still_alive": True,
                                "app_pid": app.pid, "predecessor_pid": predecessor.pid,
                                "binary": str(binary.resolve())}
                finally:
                    if app is not None:
                        app.terminate()
                        app.wait()
                    subprocess.run(["/bin/launchctl", "bootout", service], capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--stage-only", action="store_true", help="Keep a binary for a later host-side probe")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    archived = args.output / f"{args.name}-brindle-desktop"
    if args.binary.resolve() != archived.resolve():
        shutil.copy2(args.binary, archived)
    if args.stage_only:
        print(archived)
        raise SystemExit(0)
    result = probe(archived, args.output)
    report = json.dumps(result, indent=2) + "\n"
    (args.output / f"{args.name}.json").write_text(report)
    print(report, end="")
