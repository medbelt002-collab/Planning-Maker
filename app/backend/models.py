"""Data models for reservations and planning rows."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Optional, List
import re


def parse_date(value) -> Optional[date]:
    """Parse many date formats coming from Booking (dd/mm/yyyy, yyyy-mm-dd, etc.)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    # keep only the date part if there is a time
    s = s.split("T")[0].split(" ")[0]
    fmts = ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%y", "%d.%m.%Y"]
    for f in fmts:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def parse_datetime(value) -> Optional[datetime]:
    """Parse 'Créé le' timestamps like '08-07-2026 20:26:55' or '08/07/2026 20:26'."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip()
    fmts = [
        "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
        "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d",
    ]
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


# Regex to read "16C22-A (Studio [Campus 1])" -> number, type
ROOM_RE = re.compile(r"^\s*([A-Za-z0-9\-]+)\s*\((studio|chambre)", re.IGNORECASE)


def parse_room(room_name: str):
    """Return (room_code, room_type) from a 'Nom de la chambre' string.

    room_type is 'STUDIO' or 'CHAMBRE' (normalized) or None if unknown.
    """
    if not room_name:
        return None, None
    m = ROOM_RE.match(room_name)
    if m:
        code = m.group(1).strip()
        rtype = m.group(2).strip().upper()
        return code, rtype
    # fallback: find the word studio/chambre anywhere
    low = room_name.lower()
    code = room_name.split("(")[0].strip()
    if "studio" in low:
        return code, "STUDIO"
    if "chambre" in low:
        return code, "CHAMBRE"
    return code, None


@dataclass
class Reservation:
    """A single reservation (one Réf, one stay) scraped from Booking."""
    reference: str = ""
    cree_le: Optional[datetime] = None
    status_raw: str = ""            # raw status text from Booking
    nom: str = ""                   # names joined by " / " (<=4) or Sujet (group)
    is_group: bool = False
    sujet: str = ""
    demandeur: str = ""             # E
    ligne_budgetaire: str = ""      # F
    entite: str = ""                # G
    room_code: str = ""             # 16C22-A
    room_type: str = ""             # STUDIO | CHAMBRE
    residence: str = ""             # "16"
    date_arrivee: Optional[date] = None   # overall span (min arrivee)
    date_depart: Optional[date] = None    # overall span (max depart)
    nb_personnes: int = 1
    guests: List[str] = field(default_factory=list)
    # One booking can have several stays (guests / non-adjacent periods).
    # Each period: {"arrivee": "YYYY-MM-DD", "depart": "YYYY-MM-DD"}
    periods: List[dict] = field(default_factory=list)

    # ---- computed ----
    @property
    def section(self) -> str:
        return "STUDIOS" if self.room_type == "STUDIO" else "CHAMBRES"

    def nights(self, status_rule: dict) -> int:
        """Total nuitées according to the status rule."""
        if status_rule and status_rule.get("nuitees") == "one_per_person":
            return 1  # annulation facturée -> 1 nuitée facturée
        total = 0
        for p in self.periods:
            a = parse_date(p.get("arrivee"))
            b = parse_date(p.get("depart"))
            if a and b:
                total += max(0, (b - a).days)
        if not self.periods and self.date_arrivee and self.date_depart:
            total = max(0, (self.date_depart - self.date_arrivee).days)
        return total

    def merge_key(self) -> tuple:
        """Key used to group reservations into one planning row."""
        from .config import normalize
        return (
            normalize(self.nom),
            normalize(self.entite),
            normalize(self.ligne_budgetaire),
            normalize(self.demandeur),
        )

    def to_dict(self):
        d = asdict(self)
        d["cree_le"] = self.cree_le.isoformat() if self.cree_le else None
        d["date_arrivee"] = self.date_arrivee.isoformat() if self.date_arrivee else None
        d["date_depart"] = self.date_depart.isoformat() if self.date_depart else None
        d["section"] = self.section
        d["periods"] = [
            {"arrivee": (parse_date(p.get("arrivee")).isoformat()
                         if parse_date(p.get("arrivee")) else p.get("arrivee")),
             "depart": (parse_date(p.get("depart")).isoformat()
                        if parse_date(p.get("depart")) else p.get("depart"))}
            for p in self.periods
        ]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Reservation":
        r = Reservation(
            reference=str(d.get("reference", "")),
            cree_le=parse_datetime(d.get("cree_le")),
            status_raw=d.get("status_raw", ""),
            nom=d.get("nom", ""),
            is_group=bool(d.get("is_group", False)),
            sujet=d.get("sujet", ""),
            demandeur=d.get("demandeur", ""),
            ligne_budgetaire=d.get("ligne_budgetaire", ""),
            entite=d.get("entite", ""),
            room_code=d.get("room_code", ""),
            room_type=d.get("room_type", ""),
            residence=d.get("residence", ""),
            date_arrivee=parse_date(d.get("date_arrivee")),
            date_depart=parse_date(d.get("date_depart")),
            nb_personnes=int(d.get("nb_personnes", 1) or 1),
            guests=list(d.get("guests", []) or []),
            periods=list(d.get("periods", []) or []),
        )
        return r
