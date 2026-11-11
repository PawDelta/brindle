"""T0 spike: which pycrdt builds exist for the platforms the app ships on.

Reads the wheel list PyPI publishes for the pinned version, and the result of
`uv pip compile --python-platform ... --only-binary :all:` that run.sh wrote
to out/uv_platforms.txt (a second, independent resolver opinion).
"""

import json
import re
import urllib.request
from pathlib import Path

OUT = Path(__file__).with_name("out")
OUT.mkdir(exist_ok=True)
VERSION = "0.14.13"
PY_VERSIONS = [(3, 11), (3, 12), (3, 13)]  # pyproject requires-python >=3.11

# From .github/workflows (desktop release) and pyproject.toml: macOS arm64 and
# x86_64 (Intel) apps, Linux x86_64 AppImage/deb built on ubuntu-22.04 (glibc
# 2.35), and the Windows app, a WSL thin client whose engine runs inside WSL
# (so the engine's platform there is Linux); a native win_amd64 build is listed
# too in case the engine ever runs on Windows itself.
TARGETS = {
    "macOS Apple Silicon": lambda p: p.startswith("macosx") and (p.endswith("arm64") or p.endswith("universal2")),
    "macOS Intel": lambda p: p.startswith("macosx") and (p.endswith("x86_64") or p.endswith("universal2")),
    "Linux x86_64 (glibc 2.35 or older)": lambda p: _manylinux_ok(p, "x86_64"),
    "Windows via WSL (engine runs on Linux x86_64)": lambda p: _manylinux_ok(p, "x86_64"),
    "Windows native x64": lambda p: p == "win_amd64",
    "Linux aarch64 (not shipped, for reference)": lambda p: _manylinux_ok(p, "aarch64"),
}


def _manylinux_ok(plat: str, arch: str) -> bool:
    if not plat.endswith(arch):
        return False
    m = re.match(r"manylinux_(\d+)_(\d+)_", plat)
    if m:
        return (int(m.group(1)), int(m.group(2))) <= (2, 35)
    return plat.startswith(("manylinux2014", "manylinux2010", "manylinux1"))


def py_ok(pytag: str, abi: str, ver: tuple[int, int]) -> bool:
    m = re.match(r"cp(\d)(\d+)$", pytag)
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    if abi == "abi3":
        return (major, minor) <= ver
    return (major, minor) == ver


def parse(fn: str):
    parts = fn[:-4].split("-")
    pytag, abi, plats = parts[-3], parts[-2], parts[-1]
    return pytag, abi, plats.split(".")


def main() -> None:
    url = f"https://pypi.org/pypi/pycrdt/{VERSION}/json"
    with urllib.request.urlopen(url) as r:  # noqa: S310
        meta = json.load(r)
    files = [f["filename"] for f in meta["urls"]]
    wheels = [f for f in files if f.endswith(".whl")]
    sdist = [f for f in files if f.endswith(".tar.gz")]
    print(f"pycrdt {VERSION}: {len(wheels)} wheels, sdist: {bool(sdist)}, requires_python {meta['info'].get('requires_python')}")
    all_ok = True
    results = []
    for name, match in TARGETS.items():
        cells = []
        shipped = not name.endswith("reference)")
        for ver in PY_VERSIONS:
            hits = []
            for w in wheels:
                pytag, abi, plats = parse(w)
                if py_ok(pytag, abi, ver) and any(match(p) for p in plats):
                    hits.append(w)
            cells.append((ver, hits))
            if shipped and not hits:
                all_ok = False
        desc = "; ".join(
            f"py{v[0]}.{v[1]}: " + (", ".join(sorted({h.split('-', 2)[2][:-4] for h in hs})) if hs else "NO WHEEL") for v, hs in cells
        )
        line = f"{name}: {desc}"
        print("  " + line)
        results.append(line)
    uv = OUT / "uv_platforms.txt"
    uv_text = uv.read_text().strip() if uv.exists() else "(uv resolver run not found)"
    print("uv resolver, binary wheels only:\n" + uv_text)
    uv_ok = "FAILED" not in uv_text
    detail = f"pycrdt {VERSION}, wheels per platform (python 3.11/3.12/3.13): " + " | ".join(results) + f" || uv --only-binary resolver: {uv_text.replace(chr(10), '; ')}"
    ok = all_ok and uv_ok
    print(("PASS" if ok else "FAIL"), "[python] platforms: every shipped platform has a wheel for 3.11-3.13" if ok else "[python] platforms: at least one shipped platform lacks a wheel")
    (OUT / "results_platforms.json").write_text(
        json.dumps([{"id": "platforms", "side": "python", "ok": ok, "detail": detail}])
    )


main()
