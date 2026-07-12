"""Scraper for UM6P Booking (backoffice).

The data is exposed through a REST API (React Router 7 / Remix SSR app):

  * GET /api/v2/housing-bookings                 -> list of bookings (paginated)
  * GET /api/v2/housing-bookings/{id}            -> one booking (status + user)
  * GET /api/v2/guests-to-rooms                  -> stays (guest <-> room <-> booking)
        with filter[expectedCheckIn][lt]=MONTH_END
         and filter[expectedCheckOut][gt]=MONTH_START  (overlap query)

A "stay" carries everything needed for the planning:
  expectedCheckIn / expectedCheckOut (local dates),
  booking (increment=Réf, entity, budgetLine, subject, createdAt, status),
  guest (name, gender), room (name like "16E11-a"), roomType (Studio/Chambre).

We fetch all stays overlapping the target month, keep those whose room starts
with the configured residence, group them by booking into one Reservation
(with possibly several `periods`), and resolve the authoritative status +
Demandeur from the booking detail endpoint (cached).
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from . import config as C
from . import storage
from .models import Reservation, parse_date, parse_datetime

API_BOOKINGS = f"{C.BOOKING_BASE}/api/v2/housing-bookings"
API_G2R = f"{C.BOOKING_BASE}/api/v2/guests-to-rooms"
API_BOOKING_DETAIL = f"{C.BOOKING_BASE}/api/v2/housing-bookings/{{id}}"
BACKOFFICE_DEMANDES = f"{C.BOOKING_BASE}/backoffice/housing-bookings"

BOOKING_CACHE = C.DATA_DIR / "booking_cache.json"


def _log(cb: Optional[Callable], msg: str):
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Session / login
# ---------------------------------------------------------------------------
def has_session() -> bool:
    return C.SESSION_FILE.exists()


class LoginManager:
    """Keeps a headed browser open so the user completes login (incl. OTP) at
    their own pace, then saves the session when they click 'J'ai terminé'."""

    def __init__(self):
        self._thread: Optional[object] = None
        self._finish = None
        self._active = False
        self._result: dict = {}

    @property
    def active(self) -> bool:
        return self._active

    def start(self, cb: Optional[Callable] = None) -> dict:
        import threading
        if self._active:
            return {"active": True, "message": "Un navigateur de connexion est déjà ouvert."}
        self._finish = threading.Event()
        self._result = {}
        self._active = True
        self._thread = threading.Thread(target=self._run, args=(cb,), daemon=True)
        self._thread.start()
        return {"started": True, "message": "Navigateur ouvert. Connectez-vous puis cliquez sur « J'ai terminé »."}

    def _run(self, cb):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context()
                page = context.new_page()
                _log(cb, "Ouverture de la page de connexion…")
                try:
                    page.goto(C.BOOKING_SIGNIN, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                deadline = time.time() + 900
                while time.time() < deadline and not self._finish.is_set():
                    time.sleep(0.5)
                try:
                    context.storage_state(path=str(C.SESSION_FILE))
                    self._result = {"ok": True, "message": "Session enregistrée."}
                    storage.save_state({"logged_in": True})
                    _log(cb, "Session enregistrée.")
                except Exception as e:
                    self._result = {"ok": False, "message": f"Erreur d'enregistrement: {e}"}
                browser.close()
        except Exception as e:
            self._result = {"ok": False, "message": f"Erreur navigateur: {e}"}
        finally:
            self._active = False

    def finish(self) -> dict:
        if not self._active and not self._result:
            return {"ok": False, "message": "Aucune connexion en cours."}
        if self._finish:
            self._finish.set()
        for _ in range(100):
            if not self._active:
                break
            time.sleep(0.2)
        return self._result or {"ok": True, "message": "Session enregistrée."}


login_manager = LoginManager()


def manual_login(timeout_seconds: int = 300, cb: Optional[Callable] = None) -> dict:
    """Open a visible browser so the user can log in (incl. OTP) once."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        _log(cb, "Ouverture de la page de connexion…")
        page.goto(C.BOOKING_SIGNIN, wait_until="domcontentloaded")

        deadline = time.time() + timeout_seconds
        logged = False
        while time.time() < deadline:
            url = page.url
            if "sign-in" not in url and "auth" not in url:
                time.sleep(2)
                if "sign-in" not in page.url and "auth" not in page.url:
                    logged = True
                    break
            time.sleep(1)

        if logged:
            context.storage_state(path=str(C.SESSION_FILE))
            storage.save_state({"logged_in": True})
            _log(cb, "Connexion réussie, session enregistrée.")
            browser.close()
            return {"ok": True, "message": "Session enregistrée."}
        browser.close()
        return {"ok": False, "message": "Délai dépassé sans connexion."}


def check_login(cb: Optional[Callable] = None) -> bool:
    if not has_session():
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(C.SESSION_FILE))
            api = context.request
            r = api.get(f"{C.BOOKING_BASE}/api/v2/auth/get-session", timeout=20000)
            ok = r.status == 200 and bool((r.json() or {}).get("user"))
            browser.close()
            storage.save_state({"logged_in": ok})
            return ok
    except Exception:
        return False


def booking_url(ref: str) -> str:
    """Best-effort public URL for a booking (ref = increment)."""
    # Try to resolve the internal id from the cache (backoffice-style URL).
    try:
        cache = _load_booking_cache()
        for key, val in cache.items():
            if str(val.get("increment")) == str(ref):
                return f"{C.BOOKING_BASE}/backoffice/housing-bookings/{key}"
    except Exception:
        pass
    return f"{C.BOOKING_BASE}/requester/housing-bookings/{ref}"


def debug_dump(cb: Optional[Callable] = None) -> dict:
    if not has_session():
        return {"ok": False, "message": "Pas de session. Connectez-vous d'abord."}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(C.SESSION_FILE))
        page = context.new_page()
        try:
            page.goto(BACKOFFICE_DEMANDES, wait_until="networkidle", timeout=25000)
        except PWTimeout:
            pass
        if "sign-in" in page.url:
            browser.close()
            return {"ok": False, "message": "Redirigé vers la connexion."}
        html = page.content()
        dump = C.DATA_DIR / "debug_demandes.html"
        dump.write_text(html, encoding="utf-8")
        try:
            page.screenshot(path=str(C.DATA_DIR / "debug_demandes.png"), full_page=True)
        except Exception:
            pass
        browser.close()
        return {"ok": True, "url": page.url, "html_file": str(dump)}
    return {"ok": False, "message": "Échec."}


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------
def _api_get(api, url: str, params: dict):
    for i in range(4):
        try:
            r = api.get(url, params=params, timeout=30000)
            if r.status == 200:
                return r
        except Exception:
            pass
        time.sleep(1.2 * (i + 1))
    return None


def _load_booking_cache() -> Dict[str, dict]:
    try:
        with open(BOOKING_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_booking_cache(cache: Dict[str, dict]):
    tmp = str(BOOKING_CACHE) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        import os
        os.replace(tmp, BOOKING_CACHE)
    except Exception:
        pass


def _fetch_booking_detail(api, bid: str) -> Optional[dict]:
    r = _api_get(api, API_BOOKING_DETAIL.format(id=bid), {})
    if not r:
        return None
    try:
        d = r.json()
    except Exception:
        return None
    return {
        "status": d.get("status"),
        "increment": d.get("increment"),
        "user": d.get("user"),
        "user_name": (d.get("user") or {}).get("name"),
    }


# ---------------------------------------------------------------------------
# Main scrape
# ---------------------------------------------------------------------------
def scrape(year: Optional[int] = None, month: Optional[int] = None,
           residence: Optional[str] = None, cb: Optional[Callable] = None) -> List[Reservation]:
    """Scrape reservations overlapping the target month.

    Returns one Reservation per booking (residence-filtered), each carrying the
    list of `periods` (guest stays) that fall inside the residence.
    """
    if not has_session():
        raise RuntimeError("Aucune session. Connectez-vous d'abord.")

    cfg = storage.get_config()
    residence = (residence or str(cfg.get("residence", "16"))).strip()
    threshold = int(cfg.get("group_threshold", 4))

    now = datetime.now()
    y = int(year) if year else now.year
    m = int(month) if month else now.month
    month_start = date(y, m, 1)
    month_end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    _log(cb, f"Scan {C.MONTHS_FR[m]} {y} — résidence {residence}…")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=str(C.SESSION_FILE))
        api = ctx.request

        stays = _fetch_g2r(api, month_start, month_end, cb)
        _log(cb, f"  {len(stays)} séjour(s) ce mois.")

        by_booking: Dict[str, list] = {}
        for s in stays:
            bk = s.get("booking") or {}
            bid = bk.get("id")
            if not bid:
                continue
            by_booking.setdefault(bid, []).append(s)

        cache = _load_booking_cache()
        details: Dict[str, dict] = {}
        for bid in by_booking:
            if bid in cache:
                details[bid] = cache[bid]
                continue
            det = _fetch_booking_detail(api, bid)
            if det:
                details[bid] = det
                cache[bid] = det
            time.sleep(0.15)
        _save_booking_cache(cache)
        browser.close()

    reservations: List[Reservation] = []
    for bid, sts in by_booking.items():
        det = details.get(bid) or {}
        bk0 = sts[0].get("booking") or {}

        r16 = [s for s in sts
               if (s.get("room") or {}).get("name", "").startswith(residence)]
        if not r16:
            continue

        guests: List[str] = []
        for s in r16:
            g = (s.get("guest") or {}).get("name")
            if g and g not in guests:
                guests.append(g)
        nb = len(guests) if guests else 1
        is_group = nb > threshold
        sujet = bk0.get("subject") or ""
        if is_group:
            nom = f"Groupe {sujet}".strip() if sujet else "Groupe"
        else:
            nom = " / ".join(guests[:threshold]) if guests else (sujet or "")

        rtype = ((r16[0].get("roomType") or {}).get("name") or "").upper()
        room_type = "STUDIO" if rtype == "STUDIO" else "CHAMBRE"
        room_code = (r16[0].get("room") or {}).get("name", "")

        periods = []
        seen_periods = set()
        for s in r16:
            ai = parse_date((s.get("expectedCheckIn") or "")[:10])
            ao = parse_date((s.get("expectedCheckOut") or "")[:10])
            if ai and ao:
                pkey = (ai.isoformat(), ao.isoformat())
                if pkey not in seen_periods:
                    seen_periods.add(pkey)
                    periods.append({"arrivee": ai.isoformat(), "depart": ao.isoformat()})
        if not periods:
            continue
        periods.sort(key=lambda x: x["arrivee"])
        all_a = [parse_date(p["arrivee"]) for p in periods]
        all_b = [parse_date(p["depart"]) for p in periods]
        date_arr = min(all_a)
        date_dep = max(all_b)

        ref = str(bk0.get("increment") if bk0.get("increment") is not None
                  else det.get("increment") or "")
        status_raw = det.get("status") or bk0.get("status") or ""
        demandeur = det.get("user_name") or (det.get("user") or {}).get("name") or ""
        entite = bk0.get("entity") or ""
        lbudg = bk0.get("budgetLine") or ""
        created = bk0.get("createdAt") or sts[0].get("createdAt")
        cree = None
        if created:
            try:
                cree = datetime.fromisoformat(created)
            except Exception:
                cree = parse_datetime(created)

        reservations.append(Reservation(
            reference=ref, cree_le=cree, status_raw=status_raw,
            nom=nom, is_group=is_group, sujet=sujet,
            demandeur=demandeur, ligne_budgetaire=lbudg, entite=entite,
            room_code=room_code, room_type=room_type,
            residence=(room_code or "")[:len(residence)],
            date_arrivee=date_arr, date_depart=date_dep,
            nb_personnes=nb, guests=guests, periods=periods,
        ))

    _log(cb, f"Scan terminé: {len(reservations)} réservation(s) résidence {residence}.")
    return reservations


def fetch_stays_for_month(year: Optional[int] = None, month: Optional[int] = None,
                           cb: Optional[Callable] = None) -> List[dict]:
    """Fetch all guests-to-rooms stays overlapping the given month (live).

    Returns the raw stay items (each carrying booking.increment, status,
    expectedCheckIn/Out, bookingCheckIn/Out, roomType, entity, budgetLine…).
    An empty list is returned when there is no session or on error, so the
    caller can fall back to the local DB.
    """
    if not has_session():
        _log(cb, "Pas de session — données live Booking indisponibles.")
        return []
    now = datetime.now()
    y = int(year) if year else now.year
    m = int(month) if month else now.month
    month_start = date(y, m, 1)
    month_end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    _log(cb, f"Récupération live Booking {C.MONTHS_FR[m]} {y}…")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=str(C.SESSION_FILE))
            api = ctx.request
            stays = _fetch_g2r(api, month_start, month_end, cb)
            browser.close()
        _log(cb, f"  {len(stays)} séjour(s) live.")
        return stays
    except Exception as e:
        _log(cb, f"Erreur live Booking: {e}")
        return []


def _fetch_g2r(api, month_start: date, month_end: date, cb) -> List[dict]:
    base = {
        "type": "all",
        "limit": 300,
        "filter[expectedCheckIn][lt]": month_end.isoformat(),
        "filter[expectedCheckOut][gt]": month_start.isoformat(),
    }
    stays: List[dict] = []
    cursor = 1
    while True:
        r = _api_get(api, API_G2R, dict(base, cursor=cursor))
        if not r:
            _log(cb, "Erreur de requête guests-to-rooms.")
            break
        d = r.json()
        items = d.get("items", [])
        stays.extend(items)
        tot = d.get("pagination", {}).get("total")
        if not items or (tot is not None and len(stays) >= tot):
            break
        cursor += 1
        time.sleep(0.3)
    return stays
