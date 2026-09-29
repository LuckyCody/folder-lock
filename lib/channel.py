"""channel.py — foreground vs. background routing (PROTOCOL §18, v5.4).

Every board item carries an `origin_channel`:

  window       minted from an INTERACTIVE session — a bound ICM_WINDOW that is not a `fired-*` identity.
               Foreground work: it runs NOW. Same folder as the window → the session does it itself (no item
               round-trip: `handoff.py` refuses the mint). Another folder → `autorun.py --fire-direct <key>` is
               spawned at mint time; no queue entry, no sweep interval, no coalescing, the tier of the originating
               window, no fail-up chain. If the target is held by another interactive session the item is flagged
               `waiting_on_cody` with one plain sentence — never queued.
  background   the autorun loop, tripwires, procedures, board answers (`board.py answer`, buttons and --batch).
               Unchanged: the queue, the cheap tier, the chain.

The owner can push a window item to the queue verbally — the agent then mints it with `--channel background`
(or `ICM_ORIGIN_CHANNEL=background` in its env). No flag is needed for the default: the channel is DERIVED.

The signoff guard (`window_item_guard`): an interactive session may not end while a window item it minted is still
open in a folder it holds (finish it here) or still unclaimed in another folder (its direct fire has not started).
Allowed exits: the item is done, it is flagged `waiting_on_cody`, or the direct fire has claimed it (`fired_at`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import lockpath as lp  # noqa: E402

CHANNELS = ("window", "background")
WINDOW, BACKGROUND = CHANNELS
RC_SAME_FOLDER = 12          # handoff.py: a window item aimed at a folder the window holds
OPEN_STATUSES = ("ready", "in_progress")
# model → tier name, for the tier an interactive window runs on (the CLI's settings.json `model`); the loop's
# tier table is the runner's business, these names are the ones `autorun.py --tier` accepts.
MODEL_TIER = (("fable", "critical"), ("opus", "default"), ("sonnet", "routine"), ("haiku", "routine"))


def normalize(value) -> str:
    v = str(value or "").strip().strip('"').lower()
    return v if v in CHANNELS else ""


def channel_of(item: Optional[dict]) -> str:
    """`window` | `background` — an item without the field predates v5.4 and is background."""
    return normalize((item or {}).get("origin_channel")) or BACKGROUND


def origin_channel(env: Optional[dict] = None, session_id: str = "") -> str:
    """The channel of the CURRENT process: `ICM_ORIGIN_CHANNEL` when set (the verbal override), else `window` for an
    interactive identity (bound window, not `fired-*`, no ICM_FOLDER export), else `background`."""
    env = os.environ if env is None else env
    forced = normalize(env.get("ICM_ORIGIN_CHANNEL"))
    if forced:
        return forced
    if (env.get("ICM_FOLDER") or "").strip():
        return BACKGROUND                                   # only the runner exports ICM_FOLDER
    try:
        me = lp.identity(session_id=session_id, env=env)
    except Exception:  # noqa: BLE001 — an identity conflict is not a channel
        return BACKGROUND
    if me is None:
        return BACKGROUND
    w = str(me.window or "").strip().lower()
    return BACKGROUND if (not w or w.startswith("fired-")) else WINDOW


def window_tier(env: Optional[dict] = None) -> str:
    """The tier the originating window runs on: `AUTORUN_TIER` when the process carries one, else the CLI's
    configured model mapped through MODEL_TIER, else `critical` (the owner's own window is never cheap)."""
    env = os.environ if env is None else env
    t = (env.get("AUTORUN_TIER") or "").strip().lower()
    if t:
        return t
    model = (env.get("ANTHROPIC_MODEL") or env.get("CLAUDE_MODEL") or "").lower()
    if not model:
        for p in (Path.home() / ".claude" / "settings.json",):
            try:
                model = str(json.loads(p.read_text(encoding="utf-8")).get("model") or "").lower()
            except (OSError, ValueError):
                model = ""
            if model:
                break
    for needle, tier in MODEL_TIER:
        if needle in model:
            return tier
    return "critical"


def dispatch_direct(key: str, tier: str, autorun_script: Path, cwd: Path) -> str:
    """Spawn `autorun.py --fire-direct <key> --tier <tier>` detached; returns the one-line receipt.
    `ICM_NO_DIRECT_FIRE=1` (tests, a host without a runner) records the intent without spawning."""
    cmd = [sys.executable, str(autorun_script), "--fire-direct", key, "--tier", tier or window_tier()]
    if (os.environ.get("ICM_NO_DIRECT_FIRE") or "").strip() == "1":
        return f"DIRECT FIRE SKIPPED (ICM_NO_DIRECT_FIRE=1) {key} (tier {cmd[-1]})"
    kw: dict = {"cwd": str(cwd), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    return f"FIRED NOW {key} (tier {cmd[-1]}, foreground — no queue entry)"


def window_item_guard(window: str, held: list, data: dict) -> list:
    """The signoff refusals for an INTERACTIVE window: one sentence per open window item it minted."""
    out = []
    held_set = {str(h).replace("\\", "/").strip("/") for h in (held or [])}
    for k, e in (data.get("items") or {}).items():
        if not isinstance(e, dict) or channel_of(e) != WINDOW:
            continue
        if not lp.same_window(str(e.get("minted_by") or ""), window):
            continue
        st = str(e.get("status") or "")
        if st not in OPEN_STATUSES or e.get("waiting_on_cody"):
            continue
        owner = str(e.get("owner") or k.split("|", 1)[0]).replace("\\", "/").strip("/")
        if owner in held_set:
            out.append(f"open foreground item {k} targets {owner}, which this window holds — finish it here, or "
                       f"flag it: python lib/items.py waiting-on-cody \"{k}\" \"<one plain sentence>\"")
        elif st == "ready" and not e.get("fired_at"):
            out.append(f"foreground item {k} for {owner} has not been claimed by its direct fire yet — wait for "
                       f"`autorun.py --fire-direct` to start it, or flag it waiting-on-cody")
    return out
