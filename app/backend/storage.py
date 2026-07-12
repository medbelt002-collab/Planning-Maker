"""Persistent storage: config, sync state, journal, verification status."""
from __future__ import annotations
import json
import threading
from datetime import datetime
from typing import Any, Dict, List

from . import config as C

_lock = threading.Lock()


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    import os
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def get_config() -> Dict[str, Any]:
    with _lock:
        cfg = _read(C.CONFIG_FILE, None)
        if cfg is None:
            cfg = dict(C.DEFAULT_CONFIG)
            _write(C.CONFIG_FILE, cfg)
        # fill missing keys with defaults
        changed = False
        for k, v in C.DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
                changed = True
        if changed:
            _write(C.CONFIG_FILE, cfg)
        return cfg


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        current = _read(C.CONFIG_FILE, dict(C.DEFAULT_CONFIG))
        current.update(cfg)
        _write(C.CONFIG_FILE, current)
        return current


def status_rule_for(status_raw: str) -> Dict[str, Any]:
    """Find the first matching status rule for a raw status string."""
    from .config import normalize
    norm = normalize(status_raw)
    cfg = get_config()
    for rule in cfg.get("status_rules", []):
        if normalize(rule.get("match", "")) in norm:
            return rule
    # unknown status -> safest default: add, keep dates, no note
    return {"match": "", "label": status_raw or "Inconnu", "action": "add",
            "note": "", "nuitees": "dates", "unknown": True}


# ---------------------------------------------------------------------------
# State (last sync markers)
# ---------------------------------------------------------------------------
def get_state() -> Dict[str, Any]:
    with _lock:
        return _read(C.STATE_FILE, {
            "last_cree_le": None,
            "last_reference": None,
            "last_sync_at": None,
            "last_sync_duration": None,
            "logged_in": False,
        })


def save_state(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        st = _read(C.STATE_FILE, {})
        st.update(patch)
        _write(C.STATE_FILE, st)
        return st


# ---------------------------------------------------------------------------
# Journal (change log, persistent history)
# ---------------------------------------------------------------------------
def append_journal(entries: List[Dict[str, Any]]):
    if not entries:
        return
    with _lock:
        data = _read(C.JOURNAL_FILE, [])
        data.extend(entries)
        # keep last 5000 entries
        if len(data) > 5000:
            data = data[-5000:]
        _write(C.JOURNAL_FILE, data)


def get_journal(limit: int = 500) -> List[Dict[str, Any]]:
    with _lock:
        data = _read(C.JOURNAL_FILE, [])
        return list(reversed(data[-limit:]))


def journal_entry(ref: str, kind: str, detail: str, before=None, after=None) -> Dict[str, Any]:
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "ref": ref,
        "kind": kind,          # ajout | modification | fusion | suppression | attente | anomalie
        "detail": detail,
        "before": before,
        "after": after,
    }


# ---------------------------------------------------------------------------
# Verification status
# ---------------------------------------------------------------------------
def get_verify() -> Dict[str, str]:
    with _lock:
        return _read(C.VERIFY_FILE, {})


def set_verified(ref: str, verified: bool = True):
    with _lock:
        data = _read(C.VERIFY_FILE, {})
        if verified:
            data[ref] = datetime.now().isoformat(timespec="seconds")
        else:
            data.pop(ref, None)
        _write(C.VERIFY_FILE, data)
