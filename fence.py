"""The ``data_dirs`` fence — which local files the agent may connect and query.

The agent chooses what to connect, so every file it names is checked here, on the host, against
the disk as it is NOW (symlinks resolved first) — at connect time AND again before every query,
so a file that moved out of the fence (or a ``data_dirs`` entry the operator removed) stops being
readable without a reconnect. The rules, ported from campaign-plugin's ``upload_dirs`` fence:

* the operator's ``data_dirs`` allowlist, plus the agent's own DEFAULT data folder
  (``<agent workspace>/data`` — :func:`default_root`) unless ``use_default_folder`` is off; with
  neither, every connect is refused;
* an allowlist entry that is the filesystem root, the home dir or any parent of it, or the agent's
  home (or a parent of it) is ignored — too broad;
* a file must resolve (symlinks followed) to a regular file INSIDE an allowlisted dir — so a
  symlink or a ``..`` can't point out of it;
* never the agent's secrets, even inside an allowlisted dir: credential dirs (.ssh, .aws, …),
  key/credential file names (secrets.yaml, .env, id_rsa, *.pem, …), and anything under the
  protoAgent home (~/.protoagent, $PROTOAGENT_HOME, $PROTOAGENT_BOX_ROOT);
* hardlinked files (link count > 1) are refused — a hardlink dodges every name check.

The ONE carve-out from the agent-home refusal is the default data folder itself: on a host it
lives in the agent's workspace, inside the agent home, and files that resolve (symlinks followed)
into exactly that folder are readable. Nothing else under the home is — a symlink in the default
folder pointing at the agent's config, secrets or databases resolves OUT of it and is refused. The
folder is only trusted if it is a real directory (not a symlink — else it could alias the whole
home) and isn't the filesystem root, a home dir or a parent of one.

Deny checks are case-insensitive and also match by inode (macOS/Windows filesystems are
case-insensitive); the allowlist compares exactly, so a case variant fails CLOSED there.

The name lists catch common key/credential files, not every secret on a disk: the real fence is
the allowlist — point ``data_dirs`` at folders of data, never at one the agent can write into.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from . import paths, settings

SECRET_DIR_NAMES = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".password-store",
        ".gcloud",
        "gcloud",
        "keychains",
        ".mozilla",
        "google-chrome",
        "chromium",
    }
)
# Adjacent directory pairs that hold credentials (lowercased): gh's token, browser profiles.
SECRET_DIR_PAIRS = frozenset({(".config", "gh"), ("application support", "google"), ("application support", "firefox")})
SECRET_FILE_RE = re.compile(
    r"^(?:secrets?\.(?:ya?ml|json|toml)|\.env(?:\..*)?|\.netrc|_netrc|\.git-credentials|\.pgpass|\.npmrc|\.pypirc"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)(?:[._-].*)?|\.?credentials(?:\..*)?|auth\.json|\.htpasswd|\.s3cfg|\.boto"
    r"|cookies|login data|hosts\.ya?ml|.*\.(?:pem|key|p12|pfx|kdbx|keychain-db|keystore|jks))$",
    re.IGNORECASE,
)

SETTINGS_HINT = "Settings ▸ Plugins ▸ Data Analyst ▸ Data folders"


def parse_dirs(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple, set)):
        items = [str(d) for d in raw]
    else:
        items = re.split(r"[,\n]+", str(raw or ""))
    return [d.strip() for d in items if d.strip()]


def agent_homes() -> list[Path]:
    homes = [Path.home() / ".protoagent"]
    for var in ("PROTOAGENT_HOME", "PROTOAGENT_BOX_ROOT"):
        env = os.environ.get(var, "").strip()
        if env:
            homes.append(Path(env).expanduser())
    out = []
    for h in homes:
        try:
            out.append(h.resolve())
        except (OSError, RuntimeError):
            pass
    return out


def within(path: Path, root: Path) -> bool:
    """Exact containment — for the allowlist (a case variant fails closed)."""
    return path == root or root in path.parents


def _ident(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def within_any_case(path: Path, root: Path) -> bool:
    """Containment for DENY checks: case-insensitive, and by inode (same dir, any spelling)."""
    a, b = [x.casefold() for x in path.parts], [x.casefold() for x in root.parts]
    if a[: len(b)] == b:
        return True
    rid = _ident(root)
    return rid is not None and any(_ident(q) == rid for q in (path, *path.parents))


def default_root(conf: dict | None = None) -> tuple[Path | None, str | None]:
    """(the agent's default data folder, resolved — or None; why it isn't usable, if it should be).

    Created if missing. Trusted only as a plain directory: if ``<workspace>/data`` were a symlink
    to the agent home (or ``~``), the carve-out below would hand out the whole of it."""
    if not settings.flag("use_default_folder", conf):
        return None, None
    p = paths.default_data_dir(create=True)
    if p is None:
        return None, None
    if not p.is_absolute():
        return None, f"default data folder {str(p)!r} is not an absolute path — ignored"
    try:
        st = p.lstat()
        real = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"default data folder {str(p)!r} doesn't exist and couldn't be created — ignored"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return None, f"default data folder {str(p)!r} is not a plain directory (a symlink?) — ignored"
    home = Path.home().resolve()
    if real == Path(real.anchor) or any(within_any_case(h, real) for h in (home, *agent_homes())):
        return None, f"default data folder {str(p)!r} is the filesystem root or contains a home dir — ignored"
    return real, None


def roots(raw: Any, conf: dict | None = None) -> tuple[list[Path], list[str]]:
    """(the usable allowlisted dirs, resolved — the default data folder first, then ``data_dirs``;
    a note for each configured entry that isn't usable). ``conf`` judges ``use_default_folder``
    from that config instead of the live one (register() during a reload)."""
    out: list[Path] = []
    notes: list[str] = []
    default, note = default_root(conf)
    if default is not None:
        out.append(default)
    if note:
        notes.append(note)
    home = Path.home().resolve()
    homes = agent_homes()
    for d in parse_dirs(raw):
        p = Path(d).expanduser()
        if not p.is_absolute():
            notes.append(f"data_dirs entry {d!r} is not an absolute path — ignored")
            continue
        try:
            r = p.resolve(strict=True)
        except (OSError, RuntimeError):
            notes.append(f"data_dirs entry {d!r} doesn't exist — ignored")
            continue
        if not r.is_dir():
            notes.append(f"data_dirs entry {d!r} is not a directory — ignored")
        elif r == Path(r.anchor) or any(within_any_case(h, r) for h in (home, *homes)):
            notes.append(
                f"data_dirs entry {d!r} is the filesystem root, your home dir (or a parent of it), or the "
                "agent's home — too broad, ignored"
            )
        elif r not in out:
            out.append(r)
    return out, notes


def configured_paths(raw: Any) -> list[Path]:
    """Every configured entry that resolves — usable or not — plus the default data folder (even
    when ``use_default_folder`` is off: turning it back on mustn't make old exports sources).
    Exports must avoid ALL of them."""
    out = []
    default = paths.default_data_dir()
    for d in [*parse_dirs(raw), *([str(default)] if default is not None else [])]:
        try:
            out.append(Path(d).expanduser().resolve())
        except (OSError, RuntimeError):
            pass
    return out


def _denied(original: Path, real: Path) -> str | None:
    parts = [x.casefold() for x in (*original.parts, *real.parts)]
    pairs = set(zip(parts, parts[1:]))
    if pairs & SECRET_DIR_PAIRS or any(x in SECRET_DIR_NAMES for x in parts):
        return "is inside a credentials directory — never read"
    if SECRET_FILE_RE.match(real.name) or SECRET_FILE_RE.match(original.name):
        return "looks like a key or credentials file — never read"
    default, _ = default_root()
    if default is not None and within(real, default):
        return None  # the one carve-out: the agent's own data folder (``real`` is symlink-resolved)
    for h in agent_homes():
        if within_any_case(real, h):
            return f"is inside the agent's home ({h}) — never read"
    return None


def dir_problem(raw: str, allowed: list[Path]) -> tuple[str | None, Path | None]:
    """(why folder ``raw`` may not be walked or None, its resolved path)."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        return f"{raw!r} is not an absolute path", None
    try:
        real = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return f"{raw!r} doesn't exist", None
    if not any(within(real, r) for r in allowed):
        return _outside(raw, allowed), None
    why = _denied(p, real)
    if why:
        return f"{raw!r} {why}", None
    return None, real


def identity(p: Path | str) -> list[int] | None:
    """``[st_dev, st_ino]`` of ``p`` itself (``lstat`` — a symlink is NOT followed), or None if it
    is missing, a symlink, or not a regular file. Taken when a source passes the fence and again
    after the engine read it: a source swapped (for a symlink, or another file) in between is
    caught, because the path the engine opens is the already-resolved real path, which must
    still be the very same plain file."""
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):  # S_ISREG on an lstat is False for a symlink
        return None
    return [int(st.st_dev), int(st.st_ino)]


def file_problem(raw: str | Path, allowed: list[Path]) -> tuple[str | None, Path | None]:
    """(why file ``raw`` may not be read or None, its resolved path)."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        return f"{str(raw)!r} is not an absolute path", None
    try:
        real = p.resolve(strict=True)
        st = real.stat()
    except (OSError, RuntimeError):
        return f"{str(raw)!r} doesn't exist", None
    if not stat.S_ISREG(st.st_mode):
        return f"{str(raw)!r} is not a regular file", None
    if st.st_nlink > 1:
        return f"{str(raw)!r} is hardlinked elsewhere — copy it instead (a hardlink dodges the name checks)", None
    # Symlinks are resolved FIRST, so a link inside an allowlisted dir can't point out of it.
    if not any(within(real, r) for r in allowed):
        return _outside(str(raw), allowed), None
    why = _denied(p, real)
    if why:
        return f"{str(raw)!r} {why}", None
    return None, real


def _outside(raw: str, allowed: list[Path]) -> str:
    if not allowed:
        return (
            f"{raw!r} can't be read: no data folders are allowlisted (and the default data folder is off). "
            f"The operator sets them in {SETTINGS_HINT} (the agent can't change that setting)."
        )
    return (
        f"{raw!r} is outside the allowlisted data folders ({', '.join(str(r) for r in allowed)}) — "
        f"ask the operator to add its folder in {SETTINGS_HINT}"
    )
