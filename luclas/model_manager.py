"""
model_manager.py — Interactive TUI for managing data/models.json

List screen:  curses  (stdscr.getch → curses.KEY_UP / KEY_DOWN)
Form + picker: normal terminal suspended from curses (endwin → raw getch)

Entry: run()
"""

from __future__ import annotations

import curses
import json
import os
import select
import shutil
import sys
import termios
import tty
from typing import Optional

from config import MODELS_CONFIG_PATH
from llm_router import TASK_TYPES
from local_llm_detect import fetch_openai_models, scan_local_llm_servers, DETECTED_PROVIDER_LABELS


# ── ANSI helpers (used outside curses in form / picker) ──────

_RST  = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM  = "\x1b[2m"
_GRN  = "\x1b[32m"
_YLW  = "\x1b[33m"
_CYN  = "\x1b[36m"
_RED  = "\x1b[31m"
_CLR  = "\x1b[H\x1b[2J"


def _b(s: str) -> str: return f"{_BOLD}{s}{_RST}"
def _d(s: str) -> str: return f"{_DIM}{s}{_RST}"
def _g(s: str) -> str: return f"{_GRN}{s}{_RST}"
def _y(s: str) -> str: return f"{_YLW}{s}{_RST}"
def _c(s: str) -> str: return f"{_CYN}{s}{_RST}"
def _r(s: str) -> str: return f"{_RED}{s}{_RST}"


# ── Raw getch (picker inside form, curses is suspended) ───────
#
# Uses os.read(fd, …) directly to bypass Python's buffered IO.
# sys.stdin.read(1) can consume all 3 bytes of an arrow sequence
# (\x1b[A) into Python's buffer, making select.select see an
# empty OS fd and causing us to return a bare '\x1b'.

def _getch() -> str:
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                more = os.read(fd, 8)
                return (ch + more).decode("utf-8", errors="replace")
            return "\x1b"
        return ch.decode("utf-8", errors="replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Persistence ───────────────────────────────────────────────

def _load() -> list[dict]:
    if not os.path.isfile(MODELS_CONFIG_PATH):
        return []
    try:
        with open(MODELS_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  {_r('!')} could not read {MODELS_CONFIG_PATH}: {e}", file=sys.stderr)
        return []


def _save(models: list[dict]) -> None:
    parent = os.path.dirname(MODELS_CONFIG_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # Back up the existing file before overwriting, and write via a temp
    # file + atomic rename. Without this, a crash mid-write (OOM-kill, disk
    # full, power loss) leaves a truncated/invalid models.json; load_models()
    # in llm_router.py silently returns [] on any parse error, which would
    # wipe the entire LLM routing config with no backup to recover from —
    # unlike .env, which setup.py already backs up on every rewrite.
    if os.path.isfile(MODELS_CONFIG_PATH):
        try:
            shutil.copy2(MODELS_CONFIG_PATH, MODELS_CONFIG_PATH + ".bak")
        except Exception:
            pass
    tmp_path = MODELS_CONFIG_PATH + f".tmp-{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(models, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, MODELS_CONFIG_PATH)


# ── Endpoint discovery ────────────────────────────────────────
# (probe logic lives in local_llm_detect.py, shared with setup.py)


# ── Curses list screen ────────────────────────────────────────

def _safe_add(stdscr, y: int, x: int, s: str, attr: int = curses.A_NORMAL) -> None:
    try:
        h, w = stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        stdscr.addstr(y, x, s[: w - x - 1], attr)
    except curses.error:
        pass


def _draw_list(stdscr, models: list[dict], sel: int, msg: str) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    _safe_add(stdscr, 0, 2, "LLM Model Manager", curses.A_BOLD)
    _safe_add(stdscr, 1, 2,
              "UP/DOWN navigate   a=add   e/Enter=edit   d=delete   q=quit",
              curses.A_DIM)
    _safe_add(stdscr, 2, 2, "-" * min(72, w - 4))

    row = 4
    if not models:
        _safe_add(stdscr, row, 4,
                  "No models configured. Press [a] to add the first model.",
                  curses.A_DIM)
    else:
        for i, m in enumerate(models):
            if row >= h - 4:
                break
            is_sel = i == sel
            id_    = m.get("id", "?")
            name   = m.get("name", "")
            cmp    = m.get("complexity", "?")
            pri    = m.get("priority", 1)
            clf    = "(*)" if m.get("classifier") else "   "
            cursor = ">" if is_sel else " "
            line   = f"  {cursor} {id_:<22} {name:<32} {cmp}  pri={pri} {clf}"

            attr = curses.A_REVERSE if is_sel else curses.A_NORMAL
            _safe_add(stdscr, row, 0, line, attr)
            row += 1

            if is_sel:
                url   = m.get("base_url", "")
                types = "types: " + ", ".join(m.get("task_types", ["general"]))
                _safe_add(stdscr, row, 6, url[:w - 8],   curses.A_DIM)
                row += 1
                _safe_add(stdscr, row, 6, types[:w - 8], curses.A_DIM)
                row += 1

    _safe_add(stdscr, h - 2, 2, "-" * min(72, w - 4))
    if msg:
        _safe_add(stdscr, h - 1, 2, msg[:w - 4], curses.A_BOLD)
    else:
        _safe_add(stdscr, h - 1, 2,
                  "a=add  e/Enter=edit  d=delete  q=quit", curses.A_DIM)

    stdscr.refresh()


def _list_loop(stdscr) -> None:
    sel = 0
    msg = ""
    while True:
        models = _load()
        n = len(models)
        if n:
            sel = max(0, min(sel, n - 1))
        _draw_list(stdscr, models, sel, msg)
        msg = ""

        key = stdscr.getch()

        if key in (ord("q"), ord("Q"), 27):              # q / Q / Esc
            break
        elif key == curses.KEY_UP and n:
            sel = (sel - 1) % n
        elif key == curses.KEY_DOWN and n:
            sel = (sel + 1) % n
        elif key in (ord("a"), ord("A")):
            curses.endwin()
            saved = _form_screen({}, is_new=True, idx=-1)
            stdscr.touchwin()
            stdscr.refresh()
            msg = "Saved." if saved else ""
        elif key in (ord("e"), ord("E"), ord("\n"), ord("\r"), curses.KEY_ENTER) and n:
            curses.endwin()
            saved = _form_screen(dict(models[sel]), is_new=False, idx=sel)
            stdscr.touchwin()
            stdscr.refresh()
            msg = "Saved." if saved else ""
        elif key in (ord("d"), ord("D")) and n:
            curses.endwin()
            deleted = _delete_screen(sel)
            stdscr.touchwin()
            stdscr.refresh()
            if deleted:
                msg = "Deleted."
                if sel >= len(_load()) and sel > 0:
                    sel -= 1


# ── Outside-curses picker (raw getch) ─────────────────────────

def _pick_list(options: list[str], title: str) -> Optional[str]:
    """Arrow-key picker rendered with ANSI, runs after curses.endwin()."""
    sel = 0
    n   = len(options)
    while True:
        out = [_CLR, f"  {_b(title)}\n", "  " + "-" * 60 + "\n\n"]
        # Show window of 20
        win_start = max(0, min(sel - 8, n - 20)) if n > 20 else 0
        win_end   = min(win_start + 20, n)
        if win_start > 0:
            out.append(f"  {_d(f'  ... {win_start} more above')}\n")
        for i in range(win_start, win_end):
            if i == sel:
                out.append(f"  {_c('>')} {_b(options[i])}\n")
            else:
                out.append(f"      {options[i]}\n")
        if win_end < n:
            out.append(f"  {_d(f'  ... {n - win_end} more below')}\n")
        out.append(f"\n  " + "-" * 60 + "\n")
        out.append(f"  {_d('UP/DOWN navigate  Enter=select  q=back')}\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

        key = _getch()
        if key == "\x1b[A":                # up
            sel = (sel - 1) % n
        elif key == "\x1b[B":              # down
            sel = (sel + 1) % n
        elif key in ("\r", "\n"):
            return options[sel]
        elif key in ("q", "Q", "\x1b"):
            return None


# ── Outside-curses form (print + input) ───────────────────────

def _prompt(label: str, default: str = "") -> str:
    hint = f"  [{default}]" if default else ""
    sys.stdout.write(f"  {_b(label)}{_d(hint)}: ")
    sys.stdout.flush()
    raw = input("")
    return raw.strip() if raw.strip() else default


def _form_screen(data: dict, is_new: bool, idx: int) -> bool:
    """Add / edit form. Returns True if saved."""
    title = "Add New Model" if is_new else f"Edit Model: {data.get('id', '')}"

    # ── Step 1: connection info ───────────────────────────────
    sys.stdout.write(_CLR)
    sys.stdout.write(f"  {_b(title)}\n")
    sys.stdout.write(f"  {_d('Press Enter to keep default shown in brackets.')}\n\n")
    sys.stdout.flush()

    id_val   = _prompt("ID  (unique slug, e.g. gpt4-fast)", data.get("id", ""))
    if not id_val:
        return False

    base_url = ""
    api_key  = ""
    if is_new:
        sys.stdout.write("  Scanning for locally running LLM servers…")
        sys.stdout.flush()
        detected = scan_local_llm_servers()
        sys.stdout.write(f" found {len(detected)}.\n" if detected else " none found.\n")
        if detected:
            labels = [
                f"✓ Detected: {DETECTED_PROVIDER_LABELS.get(d['provider'], 'Local server')} "
                f"at {d['base_url']}  ({len(d['models'])} model(s))"
                for d in detected
            ]
            by_label = dict(zip(labels, detected))
            picked = _pick_list(labels + ["-- enter manually --"], "Select a detected server, or enter manually")
            sys.stdout.write(_CLR)
            sys.stdout.write(f"  {_b(title)}\n\n")
            sys.stdout.flush()
            if picked and picked in by_label:
                d = by_label[picked]
                base_url = d["base_url"]
                api_key  = "none"

    base_url = _prompt("Base URL", base_url or data.get("base_url", "http://localhost:11434/v1"))
    if not base_url:
        return False

    api_key  = _prompt("API Key  (or 'none')", api_key or data.get("api_key", "none"))

    # ── Auto-connect ──────────────────────────────────────────
    sys.stdout.write(f"\n  Connecting to {_c(base_url)} ...\n")
    sys.stdout.flush()
    remote, effective_url = fetch_openai_models(base_url, api_key)

    model_name = data.get("name", "")
    if remote:
        if effective_url != base_url:
            sys.stdout.write(f"  {_d(f'(normalized: {effective_url})')}\n")
        base_url = effective_url   # save the correct /v1-normalized URL
        sys.stdout.write(f"  {_g('OK')}  {len(remote)} model(s) found\n")
        sys.stdout.flush()
        choices = remote + ["-- enter manually --"]
        picked  = _pick_list(choices, f"Select model  |  {base_url}")
        sys.stdout.write(_CLR)
        sys.stdout.write(f"  {_b(title)}\n\n")
        sys.stdout.flush()
        if picked and picked != "-- enter manually --":
            model_name = picked
            sys.stdout.write(f"  Model: {_c(model_name)}\n\n")
            sys.stdout.flush()
        else:
            model_name = _prompt("Model name", model_name)
    else:
        sys.stdout.write(
            f"  {_y('!')}  Could not reach endpoint — enter model name manually.\n\n"
        )
        sys.stdout.flush()
        model_name = _prompt("Model name", model_name)

    if not model_name:
        return False

    # ── Step 2: routing settings ──────────────────────────────
    sys.stdout.write(_CLR)
    sys.stdout.write(f"  {_b(title)}  -  routing settings\n")
    sys.stdout.write(f"  {_d(f'id={id_val}  model={model_name}  url={base_url}')}\n\n")
    sys.stdout.flush()

    cmp_default = {"low": "1", "mid": "2", "high": "3"}.get(
        data.get("complexity", "mid"), "2"
    )
    cmp_raw    = _prompt("Complexity  (1=low  2=mid  3=high)", cmp_default)
    complexity = {"1": "low", "2": "mid", "3": "high"}.get(cmp_raw, "mid")

    pri_raw = _prompt(
        "Priority  (1=normal, higher=preferred within same tier)",
        str(data.get("priority", 1)),
    )
    try:
        priority = int(pri_raw)
    except ValueError:
        priority = 1

    types_def  = ", ".join(data.get("task_types", ["general"]))
    types_raw  = _prompt(
        f"Task types  (comma-sep: {' · '.join(TASK_TYPES)})",
        types_def,
    )
    task_types = [t.strip() for t in types_raw.split(",") if t.strip()] or ["general"]

    clf_def = "y" if data.get("classifier") else "n"
    clf_raw = _prompt("Use as routing classifier?  (y/n)", clf_def)
    classifier = clf_raw.lower() in ("y", "yes", "true", "1")

    # ── Confirm ───────────────────────────────────────────────
    sys.stdout.write("\n  " + "-" * 60 + "\n")
    sys.stdout.write(f"  {_b('Summary')}\n")
    sys.stdout.write(f"  id:         {_c(id_val)}\n")
    sys.stdout.write(f"  model:      {model_name}\n")
    sys.stdout.write(f"  url:        {_d(base_url)}\n")
    sys.stdout.write(f"  complexity: {complexity}   priority: {priority}\n")
    sys.stdout.write(f"  types:      {', '.join(task_types)}\n")
    sys.stdout.write(f"  classifier: {'yes' if classifier else 'no'}\n")
    sys.stdout.write("  " + "-" * 60 + "\n")
    sys.stdout.write(f"  {_b('Save?')} [Y/n]: ")
    sys.stdout.flush()

    if input("").strip().lower() not in ("", "y", "yes"):
        sys.stdout.write("  Cancelled.\n")
        sys.stdout.flush()
        return False

    record: dict = {
        "id":         id_val,
        "name":       model_name,
        "base_url":   base_url,
        "api_key":    api_key,
        "priority":   priority,
        "complexity": complexity,
        "task_types": task_types,
    }
    if classifier:
        record["classifier"] = True

    models = _load()
    if is_new:
        existing = {m["id"] for m in models}
        base_id, sfx = id_val, 2
        while id_val in existing:
            id_val = f"{base_id}_{sfx}"
            sfx += 1
        record["id"] = id_val
        models.append(record)
    else:
        if 0 <= idx < len(models):
            # Only auto-add()'s new-model path checked for a duplicate id —
            # editing an existing model let its id collide with a *different*
            # model's id with no warning, silently leaving two entries
            # sharing one id in models.json.
            others = {m["id"] for i, m in enumerate(models) if i != idx}
            if id_val in others:
                base_id, sfx = id_val, 2
                while id_val in others:
                    id_val = f"{base_id}_{sfx}"
                    sfx += 1
                sys.stdout.write(
                    f"  {_y('!')} id '{record['id']}' is already used by another model — saved as '{id_val}' instead.\n"
                )
                sys.stdout.flush()
                record["id"] = id_val
            models[idx] = record
    _save(models)
    return True


# ── Outside-curses delete ─────────────────────────────────────

def _delete_screen(idx: int) -> bool:
    models = _load()
    if idx < 0 or idx >= len(models):
        return False
    m = models[idx]
    sys.stdout.write(_CLR)
    sys.stdout.write(f"\n  {_r(_b('Delete model'))}: {m.get('id', '?')}  ({m.get('name', '')})\n\n")
    sys.stdout.write(f"  Base URL: {_d(m.get('base_url', ''))}\n\n")
    sys.stdout.write(f"  {_d('This removes the entry from models.json.')}\n\n")
    sys.stdout.write("  Confirm delete? [y/N]: ")
    sys.stdout.flush()
    if input("").strip().lower() == "y":
        models.pop(idx)
        _save(models)
        sys.stdout.write(f"  {_g('Deleted.')}\n")
        sys.stdout.flush()
        return True
    sys.stdout.write("  Cancelled.\n")
    sys.stdout.flush()
    return False


# ── Entry point ───────────────────────────────────────────────

def _main(stdscr) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)    # makes arrow keys return curses.KEY_UP / KEY_DOWN
    _list_loop(stdscr)


def run() -> None:
    """Launch the interactive model manager. Requires an interactive terminal."""
    if not sys.stdin.isatty():
        print("Model manager requires an interactive terminal.")
        return
    curses.wrapper(_main)
