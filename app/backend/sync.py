"""Sync orchestration: reservation DB, entry building, simulation and apply."""
from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import config as C
from . import storage
from .models import Reservation, parse_date
from .planning import Planning, Entry

RES_DB_FILE = C.DATA_DIR / "reservations.json"
LEGACY_FILE = C.DATA_DIR / "legacy_entries.json"


# ---------------------------------------------------------------------------
# Reservation DB
# ---------------------------------------------------------------------------
def load_db() -> Dict[str, Reservation]:
    try:
        with open(RES_DB_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {ref: Reservation.from_dict(d) for ref, d in raw.items()}


def save_db(db: Dict[str, Reservation]):
    raw = {ref: r.to_dict() for ref, r in db.items()}
    tmp = str(RES_DB_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    import os
    os.replace(tmp, RES_DB_FILE)


def load_legacy() -> List[dict]:
    try:
        with open(LEGACY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_legacy(entries: List[dict]):
    tmp = str(LEGACY_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    import os
    os.replace(tmp, LEGACY_FILE)


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------
def passes_residence_filter(res: Reservation) -> bool:
    cfg = storage.get_config()
    residence = str(cfg.get("residence", "16"))
    code = (res.room_code or "").strip()
    return code.startswith(residence)


def passes_entity_filter(res: Reservation) -> bool:
    """Return False for reservations whose entity is in ``excluded_entities``."""
    cfg = storage.get_config()
    excluded = {C.normalize(e) for e in cfg.get("excluded_entities", [])}
    return C.normalize(res.entite or "") not in excluded


def occupancy_dates(res: Reservation, rule: dict) -> List[date]:
    """Return the list of nights (dates) this reservation occupies.

    A reservation may own several stays (periods). Nights are collected across
    all of them. Annulation facturée (one_per_person rule) yields 1 night only.
    """
    if rule.get("nuitees") == "one_per_person":
        # 1 nuitée facturée -> arrival night only
        return [res.date_arrivee] if res.date_arrivee else []
    # Use a set so each night is counted once even when the API returns the
    # same stay several times (one entry per guest) or when several stays
    # overlap the same night. The headcount comes from ``nb_personnes``.
    out: set = set()
    periods = res.periods or []
    if not periods and res.date_arrivee and res.date_depart:
        periods = [{"arrivee": res.date_arrivee.isoformat(),
                    "depart": res.date_depart.isoformat()}]
    for p in periods:
        a = parse_date(p.get("arrivee"))
        b = parse_date(p.get("depart"))
        if a and b:
            n = (b - a).days
            out.update(a + timedelta(days=k) for k in range(max(0, n)))
    return sorted(out)


def _period_bounds(res: Reservation):
    """Sorted list of (arrivee, depart) date tuples for a reservation."""
    periods = res.periods or []
    if not periods and res.date_arrivee and res.date_depart:
        periods = [{"arrivee": res.date_arrivee.isoformat(),
                    "depart": res.date_depart.isoformat()}]
    bounds = []
    for p in periods:
        a = parse_date(p.get("arrivee"))
        b = parse_date(p.get("depart"))
        if a and b:
            bounds.append((a, b))
    bounds.sort()
    return bounds


# ---------------------------------------------------------------------------
# Build entries (month -> section -> [Entry]) from DB + legacy rows
# ---------------------------------------------------------------------------
def build_entries(db: Dict[str, Reservation],
                  legacy: List[dict]) -> Dict[int, Dict[str, List[Entry]]]:
    # month -> section -> key -> Entry
    month_entries: Dict[int, Dict[str, Dict[tuple, Entry]]] = {}

    def get_entry(month: int, section: str, key: tuple,
                  nom, demandeur, lbudg, entite) -> Entry:
        secmap = month_entries.setdefault(month, {"STUDIOS": {}, "CHAMBRES": {}})
        emap = secmap[section]
        e = emap.get(key)
        if e is None:
            e = Entry(section)
            e.nom = nom or ""
            e.demandeur = demandeur or ""
            e.ligne_budgetaire = lbudg or ""
            e.entite = entite or ""
            emap[key] = e
        return e

    # 1) legacy rows first (verbatim), keyed so DB can merge by key
    legacy_refs = set()
    for row in legacy:
        section = row.get("section", "CHAMBRES")
        month = row.get("month")
        if not month:
            continue
        key = (C.normalize(row.get("nom")), C.normalize(row.get("entite")),
               C.normalize(row.get("ligne_budgetaire")), C.normalize(row.get("demandeur")))
        e = get_entry(month, section, key, row.get("nom"), row.get("demandeur"),
                      row.get("ligne_budgetaire"), row.get("entite"))
        for ref in row.get("refs", []):
            if ref not in e.refs:
                e.refs.append(ref)
            legacy_refs.add(ref)
        for day, persons in (row.get("occupancy") or {}).items():
            try:
                d = int(day)
                e.occupancy[d] = e.occupancy.get(d, 0) + int(persons)
            except (TypeError, ValueError):
                pass

    # 2) DB reservations grouped by (section, key)
    groups: Dict[Tuple[str, tuple], List[Tuple[Reservation, dict]]] = {}
    for ref, r in db.items():
        rule = storage.status_rule_for(r.status_raw)
        if rule.get("action") != "add":
            continue
        if not passes_residence_filter(r):
            continue
        if not passes_entity_filter(r):
            continue
        groups.setdefault((r.section, r.merge_key()), []).append((r, rule))

    for (section, key), items in groups.items():
        items.sort(key=lambda x: (x[0].date_arrivee or date.min))
        prev_depart = None
        for (r, rule) in items:
            # adjacent-stay note: if this reservation's first period starts
            # exactly when the previous one ended, annotate the arrival day.
            bounds = _period_bounds(r)
            first_arr = bounds[0][0] if bounds else r.date_arrivee
            note_text = None
            if prev_depart and first_arr == prev_depart and r.date_depart:
                note_text = f"Resa {first_arr.strftime('%d')} - {r.date_depart.strftime('%d')}"
            for d in occupancy_dates(r, rule):
                e = get_entry(d.month, section, key, r.nom, r.demandeur,
                              r.ligne_budgetaire, r.entite)
                e.occupancy[d.day] = e.occupancy.get(d.day, 0) + r.nb_personnes
                if r.reference not in e.refs:
                    e.refs.append(r.reference)
                if rule.get("note"):
                    e.status_note = rule["note"]
                if note_text and d == first_arr:
                    e.notes[d.day] = note_text
            last_depart = bounds[-1][1] if bounds else r.date_depart
            prev_depart = last_depart or prev_depart

    # 2b) CHU consolidé : une ligne 'CHU UM6P' (groupe 20 pers.) par mois,
    #      même si les chambres sont en R16 (les CHU sont exclus du groupage normal).
    _insert_chu_consolidated(month_entries, db)

    # 3) finalize -> lists, sort refs numerically-ish
    result: Dict[int, Dict[str, List[Entry]]] = {}
    for month, secmap in month_entries.items():
        result[month] = {}
        for section in ("STUDIOS", "CHAMBRES"):
            entries = list(secmap.get(section, {}).values())
            for e in entries:
                e.refs = _sort_refs(e.refs)
            # tri par entité (A→Z), puis par nom pour stabiliser
            entries.sort(key=lambda e: (C.normalize(e.entite), C.normalize(e.nom)))
            result[month][section] = entries
    return result


def _sort_refs(refs: List[str]) -> List[str]:
    def keyf(x):
        try:
            return (0, int(x))
        except ValueError:
            return (1, x)
    return sorted(dict.fromkeys(refs), key=keyf)


def _chu_pair(db: Dict[str, Reservation]):
    """Most frequent (ligne_budgetaire, demandeur) among CHU reservations."""
    cnt: Counter = Counter()
    for r in db.values():
        if C.normalize(r.entite or "") != "CHU":
            continue
        cnt[(r.ligne_budgetaire or "", r.demandeur or "")] += 1
    return cnt.most_common(1)[0][0] if cnt else ("", "")


def _insert_chu_consolidated(month_entries: dict, db: Dict[str, Reservation]):
    """Add one 'CHU UM6P' row per month (studios) filling every day with 20
    persons, using the shared CHU budget line + demandeur."""
    lb, dem = _chu_pair(db)
    if not (lb or dem):
        return
    for month, secmap in month_entries.items():
        e = Entry("STUDIOS")
        e.nom = "CHU UM6P"
        e.entite = "CHU"
        e.demandeur = dem
        e.ligne_budgetaire = lb
        # occupe tout le mois (1..31 ; les jours sans colonne sont ignorés au rendu)
        e.occupancy = {d: 20 for d in range(1, 32)}
        secmap.setdefault("STUDIOS", {})["__CHU__"] = e


# ---------------------------------------------------------------------------
# Simulation (diff without applying)
# ---------------------------------------------------------------------------
def _fields_snapshot(r: Reservation) -> dict:
    return {
        "nom": r.nom, "demandeur": r.demandeur, "ligne_budgetaire": r.ligne_budgetaire,
        "entite": r.entite, "room_code": r.room_code, "room_type": r.room_type,
        "status": r.status_raw, "nb_personnes": r.nb_personnes,
        "arrivee": r.date_arrivee.isoformat() if r.date_arrivee else None,
        "depart": r.date_depart.isoformat() if r.date_depart else None,
    }


def detect_anomalies(r: Reservation) -> List[str]:
    issues = []
    if r.date_arrivee and r.date_depart and r.date_depart < r.date_arrivee:
        issues.append("Date de départ < Date d'arrivée")
    if not r.room_type:
        issues.append("Chambre sans type (ni Studio ni Chambre)")
    return issues


def simulate(scraped: List[Reservation]) -> dict:
    db = load_db()
    existing_keys = {}
    for ref, r in db.items():
        rule = storage.status_rule_for(r.status_raw)
        if rule.get("action") == "add":
            existing_keys.setdefault((r.section, r.merge_key()), []).append(ref)

    changes = []
    anomalies = []
    seen_refs = set()

    for r in scraped:
        rule = storage.status_rule_for(r.status_raw)
        ref = r.reference
        if ref in seen_refs:
            anomalies.append({"ref": ref, "detail": "Référence dupliquée dans le scan"})
        seen_refs.add(ref)

        for iss in detect_anomalies(r):
            anomalies.append({"ref": ref, "detail": iss})

        if rule.get("action") == "delete":
            if ref in db:
                changes.append({"ref": ref, "kind": "suppression",
                                "detail": f"Status = {r.status_raw}",
                                "before": _fields_snapshot(db[ref]),
                                "after": _fields_snapshot(r)})
            continue

        # residence filter (only relevant for adds)
        if not passes_residence_filter(r):
            continue

        if ref not in db:
            key = (r.section, r.merge_key())
            kind = "fusion" if key in existing_keys else "ajout"
            detail = (f"Ajout de Réf {ref} au groupe existant"
                      if kind == "fusion" else "Nouvelle réservation")
            changes.append({"ref": ref, "kind": kind, "detail": detail,
                            "before": None, "after": _fields_snapshot(r)})
        else:
            before = _fields_snapshot(db[ref])
            after = _fields_snapshot(r)
            diffs = [k for k in after if before.get(k) != after.get(k)]
            if diffs:
                detail = ", ".join(
                    f"{k}: {before.get(k)} → {after.get(k)}" for k in diffs)
                changes.append({"ref": ref, "kind": "modification",
                                "detail": detail, "before": before, "after": after,
                                "fields": diffs})

    return {"changes": changes, "anomalies": anomalies,
            "counts": _counts(scraped)}


def _counts(scraped: List[Reservation]) -> dict:
    total = len(scraped)
    res16 = sum(1 for r in scraped if passes_residence_filter(r))
    en_attente = 0
    annul_fact = 0
    annul = 0
    for r in scraped:
        rule = storage.status_rule_for(r.status_raw)
        if rule.get("note"):
            en_attente += 1
        if rule.get("nuitees") == "one_per_person":
            annul_fact += 1
        if rule.get("action") == "delete":
            annul += 1
    return {"analysées": total, "residence": res16,
            "en_attente": en_attente, "annulation_facturee": annul_fact,
            "annulations": annul}


# ---------------------------------------------------------------------------
# Apply (commit to DB + render Excel)
# ---------------------------------------------------------------------------
def apply(scraped: List[Reservation], template_path: Optional[str] = None) -> dict:
    started = datetime.now()
    db = load_db()
    legacy = load_legacy()
    journal = []
    cfg = storage.get_config()

    added = updated = deleted = 0
    for r in scraped:
        rule = storage.status_rule_for(r.status_raw)
        ref = r.reference
        if rule.get("action") == "delete":
            if cfg.get("on_cancel") == "archive":
                # Keep in DB so the reservation stays visible in the reservations
                # list; it is automatically excluded from the planning by the
                # status rule (action != "add").
                if ref not in db:
                    db[ref] = r
                    added += 1
                else:
                    updated += 1
                journal.append(storage.journal_entry(ref, "annulation",
                               f"Status = {r.status_raw}"))
            else:
                if ref in db:
                    del db[ref]
                    deleted += 1
                    journal.append(storage.journal_entry(ref, "suppression",
                                   f"Status = {r.status_raw}"))
                _remove_ref_from_legacy(legacy, ref)
            continue
        if not passes_residence_filter(r):
            continue
        if ref in db:
            before = _fields_snapshot(db[ref])
            after = _fields_snapshot(r)
            if before != after:
                diffs = [k for k in after if before.get(k) != after.get(k)]
                journal.append(storage.journal_entry(
                    ref, "modification",
                    ", ".join(f"{k}: {before.get(k)} → {after.get(k)}" for k in diffs),
                    before, after))
                updated += 1
            db[ref] = r
        else:
            db[ref] = r
            added += 1
            journal.append(storage.journal_entry(ref, "ajout", "Nouvelle réservation"))
        _remove_ref_from_legacy(legacy, ref)   # DB is now authoritative for this ref

    save_db(db)
    save_legacy(legacy)

    # render
    tpl = template_path or (str(C.CURRENT_PLANNING)
                            if C.CURRENT_PLANNING.exists() else None)
    out_path = None
    if tpl:
        planning = Planning(tpl)
        entries = build_entries(db, legacy)
        planning.render(entries)
        planning.save(str(C.CURRENT_PLANNING))
        out_path = archive_current()

    # update markers
    last_cree = None
    last_ref = None
    for r in scraped:
        if r.cree_le and (last_cree is None or r.cree_le > last_cree):
            last_cree = r.cree_le
            last_ref = r.reference
    dur = (datetime.now() - started).total_seconds()
    patch = {"last_sync_at": started.isoformat(timespec="seconds"),
             "last_sync_duration": round(dur, 1)}
    if last_cree:
        patch["last_cree_le"] = last_cree.isoformat()
        patch["last_reference"] = last_ref
    storage.save_state(patch)
    storage.append_journal(journal)

    return {"added": added, "updated": updated, "deleted": deleted,
            "duration": round(dur, 1), "archive": out_path,
            "counts": _counts(scraped)}


def _remove_ref_from_legacy(legacy: List[dict], ref: str):
    for row in list(legacy):
        if ref in row.get("refs", []):
            row["refs"] = [x for x in row["refs"] if x != ref]
            if not row["refs"]:
                legacy.remove(row)


def archive_current() -> Optional[str]:
    if not C.CURRENT_PLANNING.exists():
        return None
    ts = datetime.now().strftime("%d-%m-%Y_%H-%M")
    name = f"Planning_{ts}.xlsx"
    dest = C.ARCHIVE_DIR / name
    shutil.copy2(C.CURRENT_PLANNING, dest)
    return str(dest)


# ---------------------------------------------------------------------------
# Upload handling: set template + import existing rows as legacy
# ---------------------------------------------------------------------------
def set_uploaded_planning(src_path: str) -> dict:
    shutil.copy2(src_path, C.CURRENT_PLANNING)
    planning = Planning(str(C.CURRENT_PLANNING))
    existing = planning.read_existing_entries()
    # import rows whose refs are not already in the DB
    db = load_db()
    legacy = []
    for row in existing:
        refs = [r for r in row.get("refs", []) if r not in db]
        if not row.get("refs"):
            # keep unref'd rows too (manual entries) using a synthetic id
            legacy.append(row)
            continue
        row2 = dict(row)
        row2["refs"] = refs
        if refs:
            legacy.append(row2)
    save_legacy(legacy)
    return {"sheets": [ws.title for ws in planning.wb.worksheets],
            "rows_imported": len(legacy)}
