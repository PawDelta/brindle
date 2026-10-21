"""One-click provider setup: brindle downloads a provider's CLI itself, the
maker's unmodified official build from the maker's own host, verifies it and
unpacks it into brindle's own folder. Nothing goes on PATH, no shell profile
is edited, nothing is installed system-wide, and no installer script or
``<cli> install`` is ever run (they edit PATH and shell profiles).

Where (``root()``): ``~/Library/Application Support/brindle/providers`` on
macOS, ``$XDG_DATA_HOME/brindle/providers`` (``~/.local/share``) elsewhere;
``BRINDLE_PROVIDERS_DIR`` overrides it. Per provider ``<id>``:

- ``<id>/<version>/``: the unpacked CLI, one version kept;
- ``<id>/home/``: the config home brindle launches it with, where the CLI
  supports one (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``, ``OPENCODE_CONFIG_DIR``).
  On first use brindle copies the person's NON-secret settings into it once
  (settings, instructions, MCP config, agents, commands), dropping every key
  whose name looks like a credential and never copying a credential or token
  file; after that brindle's changes stay in brindle's copy. The CLI's own
  sign-in then lives in that home: the person signs in again there, and
  brindle never reads, copies or moves a credential;
- ``<id>/state.json``: which copy brindle runs (``active``: "brindle" for
  its own, "mine" for a copy the person pinned with use-mine), the version,
  and the last latest-version check.

Verification per provider (each as its maker publishes it):

- Claude Code: ``downloads.claude.ai`` binary, SHA-256 from the release's
  manifest.json, whose detached GPG signature is checked against Anthropic's
  release key (by fingerprint) with ``gpg`` in a throwaway keyring. Without
  gpg, on macOS the binary's Apple code signature must name Anthropic
  instead; elsewhere the install is refused.
- Codex: the GitHub release tarball, against the release API's ``digest`` or
  the release's SHA256SUMS.
- OpenCode 1.x: the GitHub release archive, against the API's sha256 digest.
- Antigravity (``agy``): Google's manifest and its SHA-512.

Self-update: brindle launches its own copies with the CLI's own updater off
(Claude Code ``DISABLE_UPDATES=1``; OpenCode ``OPENCODE_DISABLE_AUTOUPDATE``
plus ``autoupdate: false`` in its config), so updates come only through
brindle. Codex and Antigravity document no switch for it, so nothing is set.

Antigravity documents no way to move its config: brindle's copy shares the
person's ``~/.gemini`` (settings, sign-in, MCP) with any other agy."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from glob import glob
from pathlib import Path
from typing import Any

PROVIDERS = ("claude", "codex", "opencode", "antigravity")
NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "antigravity": "Antigravity"}

# The makers' official sources (tests point these at a local server).
CLAUDE_BASE = "https://downloads.claude.ai/claude-code-releases"
CLAUDE_KEY_URL = "https://downloads.claude.ai/keys/claude-code.asc"
CLAUDE_KEY_FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
GITHUB_API = "https://api.github.com"
GITHUB = "https://github.com"
CODEX_REPO = "openai/codex"
OPENCODE_REPO = "anomalyco/opencode"
AGY_BASE = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
# Where agy's manifest may point its download: Google's own hosts only.
AGY_DOWNLOADS = ("https://storage.googleapis.com/antigravity-public/", AGY_BASE + "/")

HTTP_TIMEOUT = 60
MAX_DOWNLOAD = 1 << 30          # no CLI is anywhere near 1 GiB
PROGRESS_EVERY = 1 << 20        # a download progress event per MiB
VERSION_PROBE_TIMEOUT = 10

Progress = Callable[..., None]


class InstallError(Exception):
    """An install or update refused or failed; nothing it started is left behind."""


# -- where -----------------------------------------------------------------------


def root() -> Path:
    override = os.environ.get("BRINDLE_PROVIDERS_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "brindle" / "providers"
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "brindle" / "providers"


def _dir(provider: str) -> Path:
    return root() / provider


def home(provider: str) -> Path | None:
    """The config home brindle gives ``provider``'s CLI, or None where the CLI
    has no documented way to move its config (Antigravity)."""
    return None if provider == "antigravity" else _dir(provider) / "home"


def _check(provider: str) -> None:
    if provider not in PROVIDERS:
        raise InstallError(f"unknown provider {provider!r}: one of {', '.join(PROVIDERS)}")


# -- state -----------------------------------------------------------------------


def state(provider: str) -> dict[str, Any]:
    try:
        data = json.loads((_dir(provider) / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(provider: str, data: dict[str, Any]) -> None:
    path = _dir(provider) / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".state-{secrets.token_hex(4)}.json")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _installed_binary(provider: str, st: dict[str, Any] | None = None) -> Path | None:
    st = state(provider) if st is None else st
    version, rel = st.get("version"), st.get("binary")
    if not (isinstance(version, str) and isinstance(rel, str)):
        return None
    path = _dir(provider) / version / rel
    return path if path.is_file() else None


def brindle_copy(provider: str) -> Path | None:
    """brindle's own copy of ``provider``'s CLI when that is the one it runs."""
    st = state(provider)
    return _installed_binary(provider, st) if st.get("active") == "brindle" else None


def binary(provider: str) -> str | None:
    """The CLI brindle runs for ``provider`` by this module's say: its own
    copy, or the copy the person pinned with use-mine. None leaves the usual
    lookup (PATH and the makers' default places) to the caller."""
    st = state(provider)
    if st.get("active") == "brindle":
        path = _installed_binary(provider, st)
        return str(path) if path else None
    mine = st.get("mine")
    if st.get("active") == "mine" and isinstance(mine, str) and os.path.isfile(mine):
        return mine
    return None


def launch_env(provider: str) -> dict[str, str]:
    """The environment brindle's own copy runs with: its config home and its
    self-update off. Empty unless brindle's own copy is the one it runs (a
    person's own copy keeps its own config and updater)."""
    if brindle_copy(provider) is None:
        return {}
    where = home(provider)
    if provider == "claude":
        return {"CLAUDE_CONFIG_DIR": str(where), "DISABLE_UPDATES": "1"}
    if provider == "codex":
        return {"CODEX_HOME": str(where)}
    if provider == "opencode":
        return {"OPENCODE_CONFIG_DIR": str(where), "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    return {}


def config_home(provider: str) -> str | None:
    """The config home brindle's own copy of ``provider`` runs with, if it is the one brindle runs."""
    env = launch_env(provider)
    return next((v for k, v in env.items() if k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_CONFIG_DIR")),
                None)


# -- the person's own copy -------------------------------------------------------

_OWN_ENV = {"claude": "BRINDLE_CLAUDE_BIN", "codex": "BRINDLE_CODEX_BIN", "opencode": "BRINDLE_OPENCODE_BIN",
            "antigravity": "BRINDLE_AGY_BIN"}
_CLI = {"claude": "claude", "codex": "codex", "opencode": "opencode", "antigravity": "agy"}


def own_copy(provider: str) -> str | None:
    """The person's own copy of the CLI brindle would find without this
    module: the BRINDLE_*_BIN override, PATH, or the makers' default places."""
    from brindle import antigravity, opencode, providers

    explicit = os.environ.get(_OWN_ENV[provider])
    if explicit:
        return explicit if (providers.which_cli(explicit) or os.path.isfile(explicit)) else None
    found = providers.which_cli(_CLI[provider])
    if found:
        return found
    fallback = {"codex": providers.CODEX_BUNDLED, "opencode": opencode.INSTALLER,
                "antigravity": antigravity.BUNDLED}.get(provider)
    return fallback if fallback and os.path.isfile(fallback) else None


_VERSIONS: dict[tuple[str, float], str | None] = {}


def probe_version(path: str) -> str | None:
    """``<cli> --version``'s version number, cached by the file's mtime."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key not in _VERSIONS:
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=VERSION_PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
            m = re.search(r"\d+\.\d+\.\d+[\w.+-]*", out.stdout + out.stderr)
            _VERSIONS[key] = m.group(0) if m else None
        except (OSError, subprocess.TimeoutExpired):
            _VERSIONS[key] = None
    return _VERSIONS[key]


# -- versions and platforms ------------------------------------------------------


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version.split("-")[0].split("+")[0]))


def _platform() -> tuple[str, str, bool]:
    """(os, arch, musl): os "darwin"/"linux", arch "arm64"/"x64"."""
    if sys.platform == "darwin":
        system = "darwin"
    elif sys.platform.startswith("linux"):
        system = "linux"
    else:
        raise InstallError(f"brindle can't install provider CLIs on {sys.platform}")
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine)
    if arch is None:
        raise InstallError(f"brindle can't install provider CLIs on {machine}")
    return system, arch, system == "linux" and bool(glob("/lib/ld-musl-*"))


_CODEX_TARGETS = {("darwin", "arm64"): "aarch64-apple-darwin", ("darwin", "x64"): "x86_64-apple-darwin",
                  ("linux", "x64"): "x86_64-unknown-linux-musl", ("linux", "arm64"): "aarch64-unknown-linux-musl"}


# -- HTTP ------------------------------------------------------------------------


def _open(url: str, accept: str | None = None):
    import brindle

    headers = {"User-Agent": f"brindle/{brindle.__version__}"}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT)


def _fetch(url: str, accept: str | None = None, limit: int = 4 << 20) -> bytes:
    try:
        with _open(url, accept) as r:
            data = r.read(limit + 1)
    except OSError as e:
        raise InstallError(f"couldn't fetch {url}: {e}") from e
    if len(data) > limit:
        raise InstallError(f"{url} is larger than expected")
    return data


def _json(url: str) -> Any:
    try:
        return json.loads(_fetch(url, "application/vnd.github+json" if url.startswith(GITHUB_API) else None))
    except ValueError as e:
        raise InstallError(f"{url} didn't answer JSON") from e


def _download(url: str, dest: Path, progress: Progress, algorithm: str = "sha256") -> str:
    """Stream ``url`` into ``dest`` and return its hex digest."""
    digest = hashlib.new(algorithm)
    done, last = 0, 0
    try:
        with _open(url) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0) or None
            progress("download", done=0, total=total)
            while chunk := r.read(64 * 1024):
                done += len(chunk)
                if done > MAX_DOWNLOAD:
                    raise InstallError(f"{url} is larger than any CLI should be")
                f.write(chunk)
                digest.update(chunk)
                if done - last >= PROGRESS_EVERY:
                    progress("download", done=done, total=total)
                    last = done
    except OSError as e:
        raise InstallError(f"couldn't download {url}: {e}") from e
    progress("download", done=done, total=total)
    return digest.hexdigest()


# -- what to fetch ---------------------------------------------------------------


@dataclass
class Release:
    version: str
    url: str
    algorithm: str                  # "sha256" or "sha512"
    digest: str | None              # the expected hex digest, when the source gives it up front
    archive: str                    # "raw", "tar.gz" or "zip"
    names: tuple[str, ...]          # the executable's file name inside it
    sums_url: str | None = None     # a SHA256SUMS file to check against instead
    asset: str = ""                 # the file name the sums list
    manifest: bytes | None = None   # Claude: the signed manifest, and its signature
    signature: bytes | None = None


def _claude_release(version: str | None) -> Release:
    system, arch, musl = _platform()
    plat = f"{system}-{arch}" + ("-musl" if musl else "")
    version = version or _fetch(f"{CLAUDE_BASE}/latest", limit=256).decode().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+[\w.-]*", version):
        raise InstallError(f"Claude Code's latest version isn't a version: {version[:40]!r}")
    manifest = _fetch(f"{CLAUDE_BASE}/{version}/manifest.json")
    signature = _fetch(f"{CLAUDE_BASE}/{version}/manifest.json.sig")
    try:
        checksum = json.loads(manifest)["platforms"][plat]["checksum"]
    except (ValueError, KeyError, TypeError) as e:
        raise InstallError(f"Claude Code {version}'s manifest has no checksum for {plat}") from e
    return Release(version, f"{CLAUDE_BASE}/{version}/{plat}/claude", "sha256", str(checksum).lower(), "raw",
                   ("claude",), manifest=manifest, signature=signature)


def _github_release(repo: str, version: str | None, tag: Callable[[str], str],
                    untag: Callable[[str], str | None]) -> tuple[str, dict]:
    if version:
        rel = _json(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag(version)}")
    else:
        rel = next((r for r in _json(f"{GITHUB_API}/repos/{repo}/releases?per_page=30")
                    if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease")
                    and untag(str(r.get("tag_name", "")))), None)
        if rel is None:
            raise InstallError(f"no release of {repo} brindle can install")
    found = untag(str(rel.get("tag_name", ""))) if isinstance(rel, dict) else None
    if not found:
        raise InstallError(f"{repo} has no installable release {version or ''}".strip())
    return found, rel


def _asset(repo: str, rel: dict, name: str) -> dict:
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == name), None)
    if asset is None:
        raise InstallError(f"{repo}'s release {rel.get('tag_name')} has no {name}")
    url = str(asset.get("browser_download_url", ""))
    if not url.startswith(f"{GITHUB}/{repo}/releases/download/"):
        raise InstallError(f"{name} isn't served from {repo}'s own GitHub releases")
    return asset


def _api_digest(asset: dict) -> str | None:
    algo, _, value = str(asset.get("digest") or "").partition(":")
    return value.lower() if algo == "sha256" and re.fullmatch(r"[0-9a-fA-F]{64}", value) else None


def _codex_release(version: str | None) -> Release:
    system, arch, _ = _platform()
    target = _CODEX_TARGETS[(system, arch)]
    version, rel = _github_release(CODEX_REPO, version, lambda v: f"rust-v{v}",
                                   lambda t: t.removeprefix("rust-v") if t.startswith("rust-v") else None)
    name = f"codex-{target}.tar.gz"
    asset = _asset(CODEX_REPO, rel, name)
    digest = _api_digest(asset)
    sums = None
    if digest is None:
        sums = next((a for a in rel.get("assets") or [] if str(a.get("name", "")).endswith("SHA256SUMS")), None)
        if sums is None:
            raise InstallError(f"Codex {version} publishes no checksum for {name}")
        sums = _asset(CODEX_REPO, rel, sums["name"])["browser_download_url"]
    return Release(version, asset["browser_download_url"], "sha256", digest, "tar.gz", ("codex", f"codex-{target}"),
                   sums_url=sums, asset=name)


def _opencode_release(version: str | None) -> Release:
    system, arch, musl = _platform()

    def untag(t: str) -> str | None:     # OpenCode 1.x only; 2 is a different CLI (opencode2)
        return t[1:] if re.fullmatch(r"v1\.\d+\.\d+", t) else None

    version, rel = _github_release(OPENCODE_REPO, version, lambda v: f"v{v}", untag)
    name = f"opencode-{system}-{arch}" + ("-musl" if musl else "") + (".zip" if system == "darwin" else ".tar.gz")
    asset = _asset(OPENCODE_REPO, rel, name)
    digest = _api_digest(asset)
    if digest is None:
        raise InstallError(f"OpenCode {version} publishes no checksum for {name}")
    return Release(version, asset["browser_download_url"], "sha256", digest,
                   "zip" if name.endswith(".zip") else "tar.gz", ("opencode",))


def _agy_release(version: str | None) -> Release:
    system, arch, musl = _platform()
    plat = f"{system}_{'arm64' if arch == 'arm64' else 'amd64'}" + ("_musl" if musl else "")
    manifest = _json(f"{AGY_BASE}/manifests/{plat}.json")
    if not isinstance(manifest, dict):
        raise InstallError("Antigravity's manifest isn't an object")
    latest, url, sha512 = (str(manifest.get(k) or "") for k in ("version", "url", "sha512"))
    if version and version != latest:
        raise InstallError(f"Antigravity publishes only its latest version ({latest}), not {version}")
    if not re.fullmatch(r"\d+\.\d+\.\d+[\w.-]*", latest) or not re.fullmatch(r"[0-9a-fA-F]{128}", sha512):
        raise InstallError("Antigravity's manifest has no version or SHA-512")
    if not url.startswith(AGY_DOWNLOADS):
        raise InstallError("Antigravity's manifest points outside Google's own download hosts")
    path = urllib.parse.urlparse(url).path
    archive = "zip" if path.endswith(".zip") else "tar.gz" if path.endswith((".tar.gz", ".tgz")) else "raw"
    return Release(latest, url, "sha512", sha512.lower(), archive, ("agy",))


_RELEASES = {"claude": _claude_release, "codex": _codex_release, "opencode": _opencode_release,
             "antigravity": _agy_release}


def latest_version(provider: str) -> str:
    """The version an install would fetch now. Antigravity's and Claude's
    take one small request; Codex's and OpenCode's one API call."""
    _check(provider)
    if provider == "claude":
        return _fetch(f"{CLAUDE_BASE}/latest", limit=256).decode().strip()
    if provider == "antigravity":
        return _agy_release(None).version
    repo, untag = ((CODEX_REPO, lambda t: t.removeprefix("rust-v") if t.startswith("rust-v") else None)
                   if provider == "codex" else
                   (OPENCODE_REPO, lambda t: t[1:] if re.fullmatch(r"v1\.\d+\.\d+", t) else None))
    return _github_release(repo, None, str, untag)[0]


# -- verify ----------------------------------------------------------------------


def claude_signature(manifest: Path, signature: Path, staging: Path) -> bool:
    """Whether Anthropic's release key signed ``manifest``: True when gpg
    says so, False when gpg isn't here to ask. A bad signature raises. The
    key goes into a throwaway keyring in ``staging``, never the person's."""
    gpg = shutil.which("gpg")
    if gpg is None:
        return False
    keyring = staging / "gnupg"
    keyring.mkdir(mode=0o700)
    key = staging / "claude-code.asc"
    key.write_bytes(_fetch(CLAUDE_KEY_URL, limit=1 << 20))
    base = [gpg, "--homedir", str(keyring), "--batch", "--no-tty"]
    try:
        subprocess.run([*base, "--import", str(key)], capture_output=True, timeout=60, check=True)
        out = subprocess.run([*base, "--status-fd", "1", "--verify", str(signature), str(manifest)],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise InstallError(f"gpg couldn't check Claude Code's manifest signature: {e}") from e
    finally:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["gpgconf", "--homedir", str(keyring), "--kill", "all"], capture_output=True, timeout=10)
    valid = [line.split() for line in out.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    if out.returncode != 0 or not any(CLAUDE_KEY_FINGERPRINT in (f[2], f[-1]) for f in valid):
        raise InstallError("Claude Code's manifest isn't signed by Anthropic's release key: refused")
    return True


def codesigned_by_anthropic(path: Path) -> bool:
    """macOS: the binary's Apple code signature is valid and names Anthropic."""
    try:
        ok = subprocess.run(["codesign", "--verify", "--strict", str(path)], capture_output=True, timeout=60)
        info = subprocess.run(["codesign", "-dv", "--verbose=2", str(path)], capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return ok.returncode == 0 and any(line.startswith("Authority=") and "Anthropic" in line
                                      for line in info.stderr.splitlines())


def _verify(provider: str, rel: Release, file: Path, got: str, staging: Path) -> None:
    expected = rel.digest
    if expected is None and rel.sums_url:
        sums = _fetch(rel.sums_url, limit=1 << 20).decode(errors="replace")
        expected = next((line.split()[0].lower() for line in sums.splitlines()
                         if len(line.split()) == 2 and line.split()[1].lstrip("*") == rel.asset), None)
    if expected is None:
        raise InstallError(f"no published checksum for {NAMES[provider]} {rel.version}: refused")
    if got != expected:
        raise InstallError(f"{NAMES[provider]} {rel.version}'s {rel.algorithm.upper()} doesn't match the "
                           "published one: refused")
    if provider == "claude":
        assert rel.manifest is not None and rel.signature is not None
        manifest, sig = staging / "manifest.json", staging / "manifest.json.sig"
        manifest.write_bytes(rel.manifest)
        sig.write_bytes(rel.signature)
        if not claude_signature(manifest, sig, staging):
            if sys.platform != "darwin":
                raise InstallError("gpg is needed to check Claude Code's signed manifest: install gpg, then try again")
            if not codesigned_by_anthropic(file):
                raise InstallError("Claude Code's binary isn't code-signed by Anthropic: refused")


# -- unpack ----------------------------------------------------------------------


def _inside(base: Path, name: str) -> Path:
    target = (base / name).resolve()
    if not target.is_relative_to(base.resolve()):
        raise InstallError(f"the archive tries to write outside its folder ({name})")
    return target


def _unpack(rel: Release, file: Path, dest: Path) -> Path:
    dest.mkdir()
    if rel.archive == "raw":
        out = dest / rel.names[0]
        shutil.move(file, out)
    elif rel.archive == "tar.gz":
        with tarfile.open(file, "r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                _inside(dest, m.name)
                if m.islnk() or m.issym():
                    _inside(dest, os.path.join(os.path.dirname(m.name), m.linkname))
                elif not (m.isfile() or m.isdir()):
                    raise InstallError(f"the archive holds a special file ({m.name})")
            kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(dest, members=members, **kw)
    else:
        with zipfile.ZipFile(file) as z:
            for info in z.infolist():
                target = _inside(dest, info.filename)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise InstallError(f"the archive holds a link ({info.filename})")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out_f:
                    shutil.copyfileobj(src, out_f)
                if mode & 0o111:
                    target.chmod(0o755)
    found = next((p for name in rel.names for p in sorted(dest.rglob(name)) if p.is_file() and not p.is_symlink()),
                 None)
    if found is None:
        raise InstallError(f"the download holds no {rel.names[0]} executable")
    found.chmod(found.stat().st_mode | 0o755)
    return found


# -- install / update / use-mine / remove ----------------------------------------


def _quiet(stage: str, **fields: Any) -> None:
    return None


def install(provider: str, version: str | None = None, progress: Progress = _quiet) -> dict[str, Any]:
    """Download, verify and unpack ``provider``'s CLI (``version``, else the
    latest) and make it the copy brindle runs. A refusal or failure leaves
    nothing behind and the previous state as it was."""
    _check(provider)
    if version is not None and not re.fullmatch(r"\d+\.\d+\.\d+[\w.-]*", version):
        raise InstallError(f"{version[:40]!r} isn't a version")
    progress("check_version")
    rel = _RELEASES[provider](version)
    progress("check_version", version=rel.version)
    base = _dir(provider)
    made_base = not base.exists()
    base.mkdir(parents=True, exist_ok=True)
    staging = base / f".staging-{secrets.token_hex(6)}"
    staging.mkdir()
    try:
        download = staging / "download"
        got = _download(rel.url, download, progress, rel.algorithm)
        progress("verify")
        _verify(provider, rel, download, got, staging)
        progress("unpack")
        found = _unpack(rel, download, staging / "unpacked")
        relpath = found.relative_to(staging / "unpacked")
        target = base / rel.version
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging / "unpacked", target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if made_base:
            shutil.rmtree(base, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    st = state(provider)
    st.update({"active": "brindle", "version": rel.version, "binary": str(relpath), "installed_at": time.time(),
               "latest": rel.version if not version else st.get("latest", rel.version),
               "checked_at": time.time() if not version else st.get("checked_at")})
    _save(provider, st)
    for old in base.iterdir():      # one version kept
        if old.is_dir() and old.name != rel.version and re.fullmatch(r"\d+\.\d+\.\d+[\w.-]*", old.name):
            shutil.rmtree(old, ignore_errors=True)
    copied = prepare_home(provider)
    return {"provider": provider, "version": rel.version, "path": str(base / rel.version / relpath),
            "config_home": config_home(provider), "settings_copied": copied}


def update(provider: str, progress: Progress = _quiet) -> dict[str, Any]:
    """Install the latest version over brindle's own copy, if it is newer."""
    _check(provider)
    st = state(provider)
    if _installed_binary(provider, st) is None:
        raise InstallError(f"brindle hasn't installed {NAMES[provider]}: install it first")
    progress("check_version")
    latest = latest_version(provider)
    _save(provider, {**st, "latest": latest, "checked_at": time.time()})
    if version_key(latest) <= version_key(st["version"]):
        progress("check_version", version=latest)
        return {"provider": provider, "version": st["version"], "updated": False}
    return {**install(provider, latest, progress), "updated": True, "previous": st["version"]}


def use_mine(provider: str, path: str | None = None) -> dict[str, Any]:
    """Run the person's own copy (``path``, else the one brindle finds) instead
    of brindle's. brindle's copy stays on disk until removed."""
    _check(provider)
    mine = os.path.abspath(os.path.expanduser(path)) if path else own_copy(provider)
    if not mine or not os.path.isfile(mine) or not os.access(mine, os.X_OK):
        raise InstallError(f"no executable {NAMES[provider]} CLI at {path}" if path
                           else f"{NAMES[provider]} isn't installed outside brindle")
    if Path(mine).resolve().is_relative_to(_dir(provider).resolve()):
        raise InstallError("that is brindle's own copy, not yours")
    _save(provider, {**state(provider), "active": "mine", "mine": mine})
    return {"provider": provider, "state": "mine", "path": mine, "version": probe_version(mine)}


def remove(provider: str) -> dict[str, Any]:
    """Delete brindle's copy of the CLI and forget the person's pinned one, so
    brindle looks the CLI up as it did before. The config home stays: it holds
    the CLI's own sign-in, which brindle doesn't touch."""
    _check(provider)
    base = _dir(provider)
    st = state(provider)
    if base.is_dir():
        for p in base.iterdir():
            if p.name != "home" and p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
    keep = {k: st[k] for k in ("settings_copied",) if k in st}
    if keep:
        _save(provider, keep)
    else:
        with contextlib.suppress(OSError):
            (base / "state.json").unlink()
    return {"provider": provider, "removed": bool(st.get("version") or st.get("mine")),
            "config_home_kept": str(home(provider)) if home(provider) and home(provider).exists() else None}


def status(provider: str, check: bool = False) -> dict[str, Any]:
    """One provider for GET /v1/providers/installed: ``state`` "brindle" (its
    own copy, with version and config home), "mine" (the person's own copy,
    path and version) or "missing", and ``update``: the latest version known
    and whether it is newer. ``check``: ask the source now (network)."""
    _check(provider)
    st = state(provider)
    row: dict[str, Any] = {"provider": provider, "name": NAMES[provider], "state": "missing", "version": None,
                           "path": None, "config_home": None, "self_update_disabled": False}
    own = _installed_binary(provider, st)
    if st.get("active") == "brindle" and own is not None:
        row.update(state="brindle", version=st.get("version"), path=str(own), config_home=config_home(provider),
                   self_update_disabled=provider in ("claude", "opencode"))
    else:
        mine = binary(provider) or own_copy(provider)
        if mine:
            row.update(state="mine", path=mine, version=probe_version(mine))
    row["installed_version"] = st.get("version") if own is not None else None
    error = None
    if check:
        try:
            st = {**state(provider), "latest": latest_version(provider), "checked_at": time.time()}
            if _dir(provider).exists():
                _save(provider, st)
        except InstallError as e:
            error = str(e)
    latest = st.get("latest")
    current = row["installed_version"]
    row["update"] = {"latest": latest, "checked_at": st.get("checked_at"), "error": error,
                     "available": bool(latest and current and version_key(latest) > version_key(current))}
    return row


def statuses(check: bool = False) -> list[dict[str, Any]]:
    return [status(p, check) for p in PROVIDERS]


# -- the config home -------------------------------------------------------------

# A key whose name says it holds a credential is never copied, wherever it is.
SECRET_NAME = re.compile(r"(?i)(api[_-]?key|token|secret|passw(or)?d|credential|bearer|authorization|"
                         r"private[_-]?key|cookie|session[_-]?key|^auth$)")
# Files that hold a sign-in, never copied.
CREDENTIAL_FILES = {".credentials.json", "credentials.json", "auth.json", ".claude.json.backup"}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not SECRET_NAME.search(str(k))}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _secret_file(name: str) -> bool:
    return name in CREDENTIAL_FILES or bool(SECRET_NAME.search(name)) or name.endswith((".pem", ".key"))


def _copy_json(src: Path, dest: Path, pick: Callable[[dict], dict] = lambda d: d) -> None:
    try:
        data = json.loads(src.read_text())
    except (OSError, ValueError):
        return      # unreadable, or JSON with comments: not vetted, not copied
    if isinstance(data, dict):
        dest.write_text(json.dumps(_scrub(pick(data)), indent=2) + "\n")


_TOML_KEY = re.compile(r"""(?:^|[{,])\s*["']?([A-Za-z0-9_.-]+)["']?\s*=""")


def _copy_toml(src: Path, dest: Path) -> None:
    """Codex's config.toml, every line that sets a secret-named key dropped."""
    try:
        text = src.read_text()
    except OSError:
        return
    keep = [line for line in text.splitlines()
            if not any(SECRET_NAME.search(k) for k in _TOML_KEY.findall(line.split("#", 1)[0]))]
    dest.write_text("\n".join(keep) + "\n")


def _copy_file(src: Path, dest: Path) -> None:
    if src.is_file() and not src.is_symlink():
        shutil.copyfile(src, dest)


def _copy_tree(src: Path, dest: Path) -> None:
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dest, symlinks=False, ignore_dangling_symlinks=True,
                        ignore=lambda d, names: [n for n in names if _secret_file(n)])


def _sources(provider: str) -> tuple[Path, list[tuple[str, Callable[[Path, Path], None]]]]:
    """Where the person's own settings for ``provider`` are, and which of them to copy how."""
    if provider == "claude":
        src = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return src, [("settings.json", _copy_json), ("CLAUDE.md", _copy_file),
                     *((d, _copy_tree) for d in ("agents", "commands", "skills", "output-styles"))]
    if provider == "codex":
        src = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return src, [("config.toml", _copy_toml), ("AGENTS.md", _copy_file), ("prompts", _copy_tree)]
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    src = Path(os.environ.get("OPENCODE_CONFIG_DIR") or Path(xdg) / "opencode")
    return src, [("opencode.json", _copy_json), ("AGENTS.md", _copy_file),
                 *((d, _copy_tree) for d in ("agent", "agents", "command", "commands", "mode", "modes",
                                             "plugin", "plugins", "themes"))]


def _claude_mcp(dest_home: Path) -> None:
    """Claude Code keeps MCP servers in its global state file, beside the
    sign-in: only ``mcpServers`` is taken from it."""
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    src = Path(config) / ".claude.json" if config else Path.home() / ".claude.json"
    dest = dest_home / ".claude.json"
    if not dest.exists():
        _copy_json(src, dest, lambda d: {"mcpServers": d["mcpServers"]} if isinstance(d.get("mcpServers"), dict)
                   else {})


def prepare_home(provider: str) -> bool:
    """Create ``provider``'s config home and, the first time only, copy the
    person's non-secret settings into it. Whether it copied now."""
    where = home(provider)
    if where is None:
        return False
    where.mkdir(parents=True, exist_ok=True)
    st = state(provider)
    if st.get("settings_copied"):
        return False
    src, items = _sources(provider)
    if src.resolve() != where.resolve():
        for name, copy in items:
            if not (where / name).exists() and (src / name).exists():
                copy(src / name, where / name)
        if provider == "claude":
            _claude_mcp(where)
    if provider == "opencode":     # OpenCode's updater off in the config it reads too
        cfg = where / "opencode.json"
        try:
            data = json.loads(cfg.read_text()) if cfg.exists() else {}
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            cfg.write_text(json.dumps({**data, "autoupdate": False}, indent=2) + "\n")
    _save(provider, {**st, "settings_copied": True, "settings_copied_at": time.time()})
    return True


# -- one job per provider --------------------------------------------------------

_running: dict[str, Any] = {}
_running_lock = threading.Lock()


def claim(provider: str, job: Any) -> Any:
    """Register ``job`` as ``provider``'s install or update, or return the one
    already running (a second press gets that job back, never a second download)."""
    with _running_lock:
        return _running.setdefault(provider, job)


def release(provider: str) -> None:
    with _running_lock:
        _running.pop(provider, None)


def running(provider: str) -> Any:
    with _running_lock:
        return _running.get(provider)
