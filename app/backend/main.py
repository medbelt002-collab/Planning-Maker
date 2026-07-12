"""FastAPI backend wiring scraper + Excel engine + storage into a website."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, date, timedelta
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import config as C
from . import storage, sync, scraper
from .models import Reservation, parse_date
from .planning import Planning

app = FastAPI(title="UM6P Booking → Planning Sync")

# ---------------------------------------------------------------------------
# In-memory runtime state
# ---------------------------------------------------------------------------
_scrape_lock = threading.Lock()
_progress: List[str] = []
_pending_file = C.DATA_DIR / "pending.json"
_auto_thread: Optional[threading.Thread] = None
_auto_stop = threading.Event()


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    _progress.append(f"{ts}  {msg}")
    if len(_progress) > 300:
        del _progress[:-300]


def _save_pending(reservations: List[Reservation]):
    with open(_pending_file, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in reservations], f, ensure_ascii=False, indent=2)


def _load_pending() -> List[Reservation]:
    try:
        with open(_pending_file, "r", encoding="utf-8") as f:
            return [Reservation.from_dict(d) for d in json.load(f)]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# Status / dashboard
# ---------------------------------------------------------------------------
@app.get("/api/status")
def api_status():
    st = storage.get_state()
    cfg = storage.get_config()
    db = sync.load_db()
    return {
        "logged_in": st.get("logged_in", False),
        "has_session": scraper.has_session(),
        "last_sync_at": st.get("last_sync_at"),
        "last_sync_duration": st.get("last_sync_duration"),
        "last_cree_le": st.get("last_cree_le"),
        "reservations_count": len(db),
        "has_planning": C.CURRENT_PLANNING.exists(),
        "auto_sync_enabled": cfg.get("auto_sync_enabled", False),
        "auto_sync_minutes": cfg.get("auto_sync_minutes", 10),
        "auto_apply": cfg.get("auto_apply", False),
    }


@app.get("/api/dashboard")
def api_dashboard():
    db = sync.load_db()
    studios = chambres = personnes = en_attente = 0
    for r in db.values():
        rule = storage.status_rule_for(r.status_raw)
        if rule.get("action") != "add":
            continue
        if not sync.passes_residence_filter(r):
            continue
        if r.section == "STUDIOS":
            studios += 1
        else:
            chambres += 1
        personnes += r.nb_personnes
        if rule.get("note"):
            en_attente += 1
    verify = storage.get_verify()
    return {
        "reservations": len(db),
        "personnes": personnes,
        "studios": studios,
        "chambres": chambres,
        "en_attente": en_attente,
        "verified": len(verify),
        "state": storage.get_state(),
    }


@app.get("/api/log")
def api_log():
    return {"lines": _progress[-100:]}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@app.get("/api/config")
def api_get_config():
    return storage.get_config()


@app.post("/api/config")
def api_set_config(patch: dict = Body(...)):
    cfg = storage.save_config(patch)
    _restart_auto_sync()
    return cfg


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
@app.post("/api/login/start")
def api_login_start():
    _log("Ouverture du navigateur de connexion…")
    return scraper.login_manager.start(cb=_log)


@app.post("/api/login/finish")
def api_login_finish():
    _log("Enregistrement de la session…")
    res = scraper.login_manager.finish()
    _log(res.get("message", ""))
    return res


@app.post("/api/login")
def api_login():
    # legacy blocking login kept as fallback
    if not _scrape_lock.acquire(blocking=False):
        raise HTTPException(409, "Une opération est déjà en cours.")
    try:
        _log("Connexion manuelle: un navigateur va s'ouvrir. Terminez le login + OTP.")
        res = scraper.manual_login(cb=_log)
        _log(res.get("message", ""))
        return res
    finally:
        _scrape_lock.release()


@app.get("/api/login/check")
def api_login_check():
    ok = scraper.check_login(cb=_log)
    return {"logged_in": ok}


@app.post("/api/session/upload")
async def api_session_upload(file: UploadFile = File(...)):
    dest = C.SESSION_FILE
    data = await file.read()
    with open(dest, "wb") as f:
        f.write(data)
    _log(f"Session Booking importée ({len(data)} bytes).")
    ok = scraper.has_session()
    return {"ok": True, "message": "Session enregistrée.", "logged_in": ok}


@app.post("/api/debug/dump")
def api_debug_dump():
    return scraper.debug_dump(cb=_log)


# ---------------------------------------------------------------------------
# Upload / download planning
# ---------------------------------------------------------------------------
@app.post("/api/planning/upload")
async def api_upload(file: UploadFile = File(...)):
    dest = C.UPLOAD_DIR / file.filename
    data = await file.read()
    with open(dest, "wb") as f:
        f.write(data)
    info = sync.set_uploaded_planning(str(dest))
    _log(f"Planning importé: {file.filename} ({info.get('rows_imported')} lignes).")
    return info


@app.get("/api/planning/download")
def api_download():
    if not C.CURRENT_PLANNING.exists():
        raise HTTPException(404, "Aucun planning disponible.")
    name = f"Planning_{datetime.now().strftime('%d-%m-%Y_%H-%M')}.xlsx"
    return FileResponse(str(C.CURRENT_PLANNING), filename=name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/archives")
def api_archives():
    files = sorted(C.ARCHIVE_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": f.name, "size": f.stat().st_size,
             "date": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")}
            for f in files]


@app.get("/api/archives/{name}")
def api_archive_download(name: str):
    f = C.ARCHIVE_DIR / name
    if not f.exists() or f.parent != C.ARCHIVE_DIR:
        raise HTTPException(404, "Introuvable.")
    return FileResponse(str(f), filename=name)


# ---------------------------------------------------------------------------
# Booking URL (open a specific reservation) + Planning check
# ---------------------------------------------------------------------------
@app.get("/api/booking-url/{ref}")
def api_booking_url(ref: str):
    return {"url": scraper.booking_url(ref)}


def _ref_locations():
    """Map ref -> (nom, sheet, section, row) from the current planning file."""
    if not C.CURRENT_PLANNING.exists():
        return {}, {}
    try:
        pl = Planning(str(C.CURRENT_PLANNING))
        loc, noms = {}, {}
        for row in pl.read_existing_entries():
            for ref in row.get("refs", []):
                loc.setdefault(ref, {"sheet": row.get("sheet"),
                                     "section": row.get("section"),
                                     "row": row.get("row")})
                noms.setdefault(ref, row.get("nom"))
        return loc, noms
    except Exception:
        return {}, {}


def _aggregate_stays(stays: List[dict]) -> dict:
    """Group raw guests-to-rooms stays by booking increment (Réf).

    Returns increment -> {
        status, real_in, real_out, exp_in, exp_out, room_type,
        entity, budgetLine, subject, guests(set), nb_personnes
    } where real_* / exp_* are ``date`` objects (or None) for the
    earliest/latest relevant stay.
    """
    agg = {}
    for s in stays:
        bk = s.get("booking") or {}
        inc = bk.get("increment")
        if inc is None:
            continue
        inc = str(inc)
        a = agg.setdefault(inc, {
            "status": None, "real_in": None, "real_out": None,
            "exp_in": None, "exp_out": None, "room_type": None,
            "entity": None, "budgetLine": None, "subject": None,
            "room": None, "guests": set(),
        })
        st = bk.get("status") or s.get("status")
        if st:
            a["status"] = st
        a["entity"] = a["entity"] or bk.get("entity")
        a["budgetLine"] = a["budgetLine"] or bk.get("budgetLine")
        a["subject"] = a["subject"] or bk.get("subject")
        a["room"] = a["room"] or (s.get("room") or {}).get("name")
        rt = ((s.get("roomType") or {}).get("name") or "").upper()
        if rt:
            a["room_type"] = rt
        g = (s.get("guest") or {}).get("name")
        if g:
            a["guests"].add(g)
        gi = parse_date((s.get("bookingCheckIn") or "")[:10])
        go = parse_date((s.get("bookingCheckOut") or "")[:10])
        ei = parse_date((s.get("expectedCheckIn") or "")[:10])
        eo = parse_date((s.get("expectedCheckOut") or "")[:10])
        if gi and (a["real_in"] is None or gi < a["real_in"]):
            a["real_in"] = gi
        if go and (a["real_out"] is None or go > a["real_out"]):
            a["real_out"] = go
        if ei and (a["exp_in"] is None or ei < a["exp_in"]):
            a["exp_in"] = ei
        if eo and (a["exp_out"] is None or eo > a["exp_out"]):
            a["exp_out"] = eo
    for a in agg.values():
        a["nb_personnes"] = max(1, len(a["guests"]))
    return agg


@app.post("/api/planning/check")
async def api_planning_check(file: UploadFile = File(...)):
    """Upload a planning file and verify every reservation against Booking.

    Reports, per Réf present in the planning:
      * not_in_booking : absent de Booking (et de la DB locale).
      * canceled_still : trouvée dans Booking mais annulée / rejetée.
      * mismatched     : présente mais avec des infos incorrectes.
      * overstay       : « Départ réel » après la « Date de départ » (écrit
                         dans le planning, fichier corrigé téléchargeable).
    """
    dest = C.UPLOAD_DIR / file.filename
    data = await file.read()
    with open(dest, "wb") as f:
        f.write(data)
    pl = Planning(str(dest))
    rows = pl.read_existing_entries()

    plan_refs: dict = {}
    for row in rows:
        for ref in row.get("refs", []):
            plan_refs.setdefault(ref, {
                "nom": row.get("nom") or "",
                "sheet": row.get("sheet") or "",
                "section": row.get("section") or "",
                "demandeur": row.get("demandeur") or "",
                "ligne_budgetaire": row.get("ligne_budgetaire") or "",
                "entite": row.get("entite") or "",
                "month": row.get("month"),
                "row": row.get("row"),
            })

    # ---- données live Booking (par mois présent dans le fichier) ----
    inc_map: dict = {}
    live_available = False
    months = sorted({info["month"] for info in plan_refs.values()
                     if info.get("month")})
    if scraper.has_session():
        y = date.today().year
        for m in months:
            try:
                stays = scraper.fetch_stays_for_month(y, m, cb=_log)
            except Exception as e:
                _log(f"Check live échoué (mois {m}): {e}")
                stays = []
            if stays:
                live_available = True
            for inc, a in _aggregate_stays(stays).items():
                inc_map.setdefault(inc, a)

    db = sync.load_db()
    not_in_booking, canceled_still, mismatched, overstay = [], [], [], []
    overstay_marks = []   # (sheet, row, start_day, end_day, persons, note)

    today = date.today()
    for ref, info in plan_refs.items():
        booking = inc_map.get(ref)
        db_res = db.get(ref)
        status_raw = (booking or {}).get("status") or (db_res.status_raw if db_res else "")
        rule = storage.status_rule_for(status_raw)

        if booking is None and db_res is None:
            not_in_booking.append({"ref": ref, **info})
            continue

        # A) annulée / rejetée -> "réservation rejetée" (pas "introuvable")
        if rule.get("action") == "delete":
            canceled_still.append({
                "ref": ref, "status": status_raw,
                "nom": info["nom"], "sheet": info["sheet"],
                "section": info["section"],
            })
            continue

        b = booking or {}
        exp_in = b.get("exp_in")
        real_in = b.get("real_in")
        exp_out = b.get("exp_out")
        real_out = b.get("real_out")
        room_type = b.get("room_type")
        entity = b.get("entity")
        budget = b.get("budgetLine")
        persons = b.get("nb_personnes") or (db_res.nb_personnes if db_res else 1)

        # E) Overstay : départ réel après la date de départ prévue
        if real_out and exp_out and real_out > exp_out and info.get("month"):
            extra = (real_out - exp_out).days
            if extra > 0:
                start_day = exp_out.day + 1
                end_day = exp_out.day + extra
                note = f"{persons} PAX NO CHECK OUT"
                overstay.append({
                    "ref": ref, "nom": info["nom"], "sheet": info["sheet"],
                    "section": info["section"],
                    "depart_prevu": exp_out.isoformat(),
                    "depart_reel": real_out.isoformat(),
                    "extra_days": extra, "personnes": persons,
                })
                overstay_marks.append((info["sheet"], info["row"],
                                       start_day, end_day, persons, note))

        # B) infos incorrectes (studio/chambre, ligne budgetaire, entité…)
        # On ne signale un champ que si les DEUX côtés sont renseignés et
        # différents (une cellule vide du planning n'est pas "incorrecte").
        issues = []
        expected_rt = "STUDIO" if info["section"] == "STUDIOS" else "CHAMBRE"
        if room_type and room_type != expected_rt:
            issues.append({"field": "section/room_type",
                           "plan": info["section"], "booking": room_type})
        plan_lb = (info["ligne_budgetaire"] or "").strip()
        if budget and plan_lb and C.normalize(budget) != C.normalize(plan_lb):
            issues.append({"field": "ligne_budgetaire",
                           "plan": plan_lb, "booking": budget})
        plan_ent = (info["entite"] or "").strip()
        if entity and plan_ent and C.normalize(entity) != C.normalize(plan_ent):
            issues.append({"field": "entite",
                           "plan": plan_ent, "booking": entity})
        if issues:
            mismatched.append({
                "ref": ref, "sheet": info["sheet"], "section": info["section"],
                "plan": {"nom": info["nom"], "demandeur": info["demandeur"],
                         "ligne_budgetaire": info["ligne_budgetaire"],
                         "entite": info["entite"], "section": info["section"]},
                "booking": {"room_type": room_type, "entity": entity,
                            "budgetLine": budget, "status": status_raw},
                "issues": issues,
            })

    # ---- écrire l'overstay dans le planning (fichier corrigé) ----
    corrected = None
    if overstay_marks:
        for (sheet, row, sd, ed, pers, note) in overstay_marks:
            pl.mark_overstay(sheet, row, sd, ed, pers, note)
        ts = datetime.now().strftime("%d-%m-%Y_%H-%M")
        corrected = f"Planning_check_{ts}.xlsx"
        pl.save(str(C.UPLOAD_DIR / corrected))

    _log(f"Vérification: {len(plan_refs)} réf — absentes {len(not_in_booking)}, "
         f"rejetées {len(canceled_still)}, incorrectes {len(mismatched)}, "
         f"overstay {len(overstay)}.")
    return {
        "total": len(plan_refs),
        "live_available": live_available,
        "not_in_booking": not_in_booking,
        "canceled_still": canceled_still,
        "mismatched": mismatched,
        "overstay": overstay,
        "corrected_file": corrected,
    }


@app.get("/api/no-show")
def api_no_show():
    """Live No Show list: reservations whose expected arrival is today (or in the
    past) and that have NO real check-in (« Arrivée réelle » vide). They stay in
    the list until a real arrival date is recorded.

    Requires an active Booking session (live data)."""
    if not scraper.has_session():
        raise HTTPException(400, "Connectez-vous d'abord (session Booking requise).")
    today = date.today()
    y, m = today.year, today.month
    months = [(y, m), (y, m - 1) if m > 1 else (y - 1, 12)]
    inc_map: dict = {}
    for (yy, mm) in months:
        try:
            stays = scraper.fetch_stays_for_month(yy, mm, cb=_log)
        except Exception as e:
            _log(f"No-show live échoué ({yy}-{mm}): {e}")
            stays = []
        for inc, a in _aggregate_stays(stays).items():
            inc_map.setdefault(inc, a)

    out = []
    for inc, a in inc_map.items():
        exp_in = a.get("exp_in")
        real_in = a.get("real_in")
        if exp_in and exp_in <= today and real_in is None:
            guests = sorted(a.get("guests", []))
            nom = a.get("subject") or (", ".join(guests) if guests else "")
            out.append({
                "ref": inc,
                "nom": nom,
                "arrivee": exp_in.isoformat(),
                "personnes": a.get("nb_personnes", 1),
                "room": a.get("room") or "",
                "status": a.get("status") or "",
            })
    out.sort(key=lambda x: x["arrivee"])
    _log(f"No Show: {len(out)} réservation(s) sans arrivée réelle.")
    return {"today": today.isoformat(), "no_show": out}


# ---------------------------------------------------------------------------
# Sync (scrape + simulate) and Apply
# ---------------------------------------------------------------------------
def _run_scrape(mode: str, date_from, date_to) -> List[Reservation]:
    """Target the month of `date_from` (or the current month by default)."""
    from datetime import datetime as _dt
    y = m = None
    if date_from:
        y, m = date_from.year, date_from.month
    else:
        now = _dt.now()
        y, m = now.year, now.month
    return scraper.scrape(year=y, month=m, cb=_log)


@app.post("/api/sync")
def api_sync(payload: dict = Body(default={})):
    """Scrape then simulate. If auto_apply is on, also apply. Returns simulation."""
    mode = payload.get("mode", "auto")
    date_from = parse_date(payload.get("date_from"))
    date_to = parse_date(payload.get("date_to"))
    if not _scrape_lock.acquire(blocking=False):
        raise HTTPException(409, "Une synchronisation est déjà en cours.")
    try:
        _progress.clear()
        _log(f"Synchronisation ({mode})…")
        scraped = _run_scrape(mode, date_from, date_to)
        _save_pending(scraped)
        sim = sync.simulate(scraped)
        result = {"simulation": sim, "applied": False}
        cfg = storage.get_config()
        if cfg.get("auto_apply"):
            _log("Auto-apply activé: application des changements…")
            ap = sync.apply(scraped)
            result["applied"] = True
            result["apply"] = ap
        _log("Terminé.")
        return result
    except Exception as e:
        _log(f"Erreur: {e}")
        raise HTTPException(500, str(e))
    finally:
        _scrape_lock.release()


@app.post("/api/apply")
def api_apply():
    """Apply the last simulated (pending) scrape."""
    scraped = _load_pending()
    if not scraped:
        raise HTTPException(400, "Aucune simulation en attente. Lancez d'abord une synchronisation.")
    if not _scrape_lock.acquire(blocking=False):
        raise HTTPException(409, "Une opération est déjà en cours.")
    try:
        _log("Application des changements…")
        ap = sync.apply(scraped)
        _log(f"Appliqué: +{ap['added']} ~{ap['updated']} -{ap['deleted']}.")
        return ap
    finally:
        _scrape_lock.release()


# ---------------------------------------------------------------------------
# Journal / search / verify
# ---------------------------------------------------------------------------
@app.get("/api/journal")
def api_journal(limit: int = 500):
    entries = storage.get_journal(limit)
    loc, noms = _ref_locations()
    db = sync.load_db()
    for e in entries:
        ref = e.get("ref")
        e["nom"] = noms.get(ref) or (db[ref].nom if ref in db else "")
        l = loc.get(ref)
        if l:
            sect = "Studios" if l["section"] == "STUDIOS" else "Chambres"
            e["blassa"] = f"{l['sheet']} · {sect} · ligne {l['row']}"
        else:
            e["blassa"] = ""
    return entries


@app.get("/api/reservations")
def api_reservations(q: str = ""):
    db = sync.load_db()
    verify = storage.get_verify()
    qn = C.normalize(q)
    out = []
    for ref, r in db.items():
        blob = C.normalize(" ".join([
            ref, r.nom, r.sujet, r.room_code, r.demandeur, r.entite,
            r.ligne_budgetaire, r.status_raw]))
        if qn and qn not in blob:
            continue
        d = r.to_dict()
        d["verified"] = ref in verify
        d["canceled"] = storage.status_rule_for(r.status_raw).get("action") == "delete"
        out.append(d)
    # tri par date (Créé le si présent, sinon date d'arrivée), le plus récent en premier
    out.sort(key=lambda x: (x.get("cree_le") or x.get("date_arrivee") or ""),
             reverse=True)
    return out


@app.get("/api/verify")
def api_get_verify():
    return storage.get_verify()


@app.post("/api/verify/{ref}")
def api_set_verify(ref: str, payload: dict = Body(default={})):
    storage.set_verified(ref, payload.get("verified", True))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Auto-sync scheduler
# ---------------------------------------------------------------------------
def _auto_loop():
    while not _auto_stop.is_set():
        cfg = storage.get_config()
        minutes = max(1, int(cfg.get("auto_sync_minutes", 10)))
        # wait interval (checking stop flag)
        for _ in range(minutes * 60):
            if _auto_stop.is_set():
                return
            time.sleep(1)
        if not storage.get_config().get("auto_sync_enabled"):
            continue
        if not scraper.has_session():
            _log("Auto-sync: pas de session, ignoré.")
            continue
        if not _scrape_lock.acquire(blocking=False):
            continue
        try:
            _log("Auto-sync: scan en cours…")
            scraped = _run_scrape("auto", None, None)
            _save_pending(scraped)
            sim = sync.simulate(scraped)
            n = len(sim["changes"])
            if storage.get_config().get("auto_apply") and n:
                sync.apply(scraped)
                _log(f"Auto-sync: {n} changement(s) appliqué(s).")
            else:
                _log(f"Auto-sync: {n} changement(s) détecté(s).")
        except Exception as e:
            _log(f"Auto-sync erreur: {e}")
        finally:
            _scrape_lock.release()


def _restart_auto_sync():
    global _auto_thread
    _auto_stop.set()
    if _auto_thread and _auto_thread.is_alive():
        _auto_thread.join(timeout=2)
    _auto_stop.clear()
    if storage.get_config().get("auto_sync_enabled"):
        _auto_thread = threading.Thread(target=_auto_loop, daemon=True)
        _auto_thread.start()
        _log("Auto-sync activé.")


@app.on_event("startup")
def _startup():
    _restart_auto_sync()


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    idx = C.FRONTEND_DIR / "index.html"
    return HTMLResponse(idx.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=str(C.FRONTEND_DIR)), name="static")
