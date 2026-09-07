"""Identifier mint — the ONLY place an ICM identifier is formatted (PROTOCOL.md §1b).

Agents never hand-format a window ID, a lock timestamp, a handoff name, an
_inbox drop name or a fired-agent ID. Everything here is collision-safe:
a timestamp plus a short random base32 suffix. One format family:

  window   <hint>-<yymmdd>-<4 chars>          icm-enforce-260907-k7q2
  agent    fired-<specialist>-<yymmdd>-<4>    fired-forge-260907-m3xa
  handoff  <YYYYMMDD-HHMM>-<slug>-<4>         20260907-1412-reexport-extf-9p2q   (.staged.md / .fired.md)
  drop     <YYYYMMDD-HHMM>-<slug>-<4>         20260907-1412-vermietung-steuer-h7n1
  timestamp  YYYY-MM-DDTHH:MM                 lock `started:` / handoff `written:` (protocol §1 shape)

Importable and CLI-callable:
  python lib/mint.py window <hint>
  python lib/mint.py agent <specialist>
  python lib/mint.py handoff "<task>"
  python lib/mint.py drop "<label>"
  python lib/mint.py timestamp
"""
from __future__ import annotations

import datetime as _dt
import re
import secrets
import sys

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/1/i/l/o — readable in a taskbar
TS_FMT = "%Y-%m-%dT%H:%M"


def _suffix(n: int = 4) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].rstrip("-") or "item"


def now() -> _dt.datetime:
    return _dt.datetime.now()


def timestamp(when: _dt.datetime | None = None) -> str:
    """Lock `started:` / handoff `written:` value — minute precision, protocol shape."""
    return (when or now()).strftime(TS_FMT)


def compact(when: _dt.datetime | None = None) -> str:
    return (when or now()).strftime("%Y%m%d-%H%M")


def window(hint: str) -> str:
    """Window ID = LOCK.yaml window: = ICM_WINDOW. hint is the task/codename in a word or two."""
    return f"{slug(hint, 24)}-{now().strftime('%y%m%d')}-{_suffix()}"


def agent(specialist: str) -> str:
    """Fired-agent ID for .firing.lock window: (GoalResumer)."""
    return f"fired-{slug(specialist, 16)}-{now().strftime('%y%m%d')}-{_suffix()}"


def handoff(task: str, when: _dt.datetime | None = None) -> str:
    """Handoff file stem (caller appends .staged.md / .fired.md)."""
    return f"{compact(when)}-{slug(task, 36)}-{_suffix()}"


def drop(label: str, when: _dt.datetime | None = None) -> str:
    """_inbox/ drop name (folder or file stem)."""
    return f"{compact(when)}-{slug(label, 36)}-{_suffix()}"


def main(argv: list[str]) -> int:
    if len(argv) < 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    kind, rest = argv[0], " ".join(argv[1:])
    if kind == "timestamp":
        print(timestamp())
    elif kind == "window":
        print(window(rest or "session"))
    elif kind == "agent":
        print(agent(rest or "forge"))
    elif kind == "handoff":
        print(handoff(rest or "handoff"))
    elif kind == "drop":
        print(drop(rest or "drop"))
    else:
        print(f"unknown kind {kind!r}; see --help", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
