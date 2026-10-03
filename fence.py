"""The ``data_dirs`` fence — which local files the agent may connect and query.

The agent chooses what to connect, so every file it names is checked here, on the host, against
the disk as it is NOW (symlinks resolved first) — at connect time AND again before every query,
so a file that moved out of the fence (or a ``data_dirs`` entry the operator removed) stops being
readable without a reconnect. The rules, ported from campaign-plugin's ``upload_dirs`` fence:

* the operator's ``data_dirs`` allowlist; EMPTY (the default) refuses every connect;
* an allowlist entry that is the filesystem root, the home dir or any parent of it, or the agent's
  home (or a parent of it) is ignored — too broad;
* a file must resolve (symlinks followed) to a regular file INSIDE an allowlisted dir — so a
  symlink or a ``..`` can't point out of it;
* never the agent's secrets, even inside an allowlisted dir: credential dirs (.ssh, .aws, …),
  key/credential file names (secrets.yaml, .env, id_rsa, *.pem, …), and anything under the
  protoAgent home (~/.protoagent, $PROTOAGENT_HOME, $PROTOAGENT_BOX_ROOT);
* hardlinked files (link count > 1) are refused — a hardlink dodges every name check.

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


def roots(raw: Any) -> tuple[list[Path], list[str]]:
    """(the usable allowlisted dirs, resolved; a note for each configured entry that isn't)."""
    out: list[Path] = []
    notes: list[str] = []
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
        else:
            out.append(r)
    return out, notes


def configured_paths(raw: Any) -> list[Path]:
    """Every configured entry that resolves — usable or not. Exports must avoid ALL of them."""
    out = []
    for d in parse_dirs(raw):
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
            f"{raw!r} can't be read: no data folders are allowlisted. The operator sets them in {SETTINGS_HINT} "
            "(the agent can't change that setting)."
        )
    return (
        f"{raw!r} is outside the allowlisted data folders ({', '.join(str(r) for r in allowed)}) — "
        f"ask the operator to add its folder in {SETTINGS_HINT}"
    )
