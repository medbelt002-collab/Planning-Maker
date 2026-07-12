"""Configuration, constants and paths for UM6P Booking -> Planning sync."""
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # app/
BACKEND_DIR = BASE_DIR / "backend"
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR = BASE_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
UPLOAD_DIR = DATA_DIR / "uploads"

for _d in (DATA_DIR, ARCHIVE_DIR, UPLOAD_DIR):
    _d.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
JOURNAL_FILE = DATA_DIR / "journal.json"
VERIFY_FILE = DATA_DIR / "verify.json"
SESSION_FILE = DATA_DIR / "session_state.json"      # Playwright storage_state
CURRENT_PLANNING = DATA_DIR / "current_planning.xlsx"

# ---------------------------------------------------------------------------
# Booking site
# ---------------------------------------------------------------------------
BOOKING_BASE = "https://booking.um6p.ma"
BOOKING_SIGNIN = f"{BOOKING_BASE}/requester/auth/sign-in"

# ---------------------------------------------------------------------------
# Planning layout
# ---------------------------------------------------------------------------
# Columns (1-indexed) for the information part of each row.
COL_NOM = 1          # A - Nom / Groupe
COL_REF = 2          # B - Réf
COL_CAMPUS = 3       # C - Campus  (constant "Campus 1")
COL_STATUT = 4       # D - Statut  (constant "invité")
COL_DEMANDEUR = 5    # E - Demandeur
COL_LBUDG = 6        # F - L.Budgétaire
COL_ENTITE = 7       # G - Entité
FIRST_DAY_COL = 8    # H - day 1 (discovered dynamically from header row)

CONST_CAMPUS = "Campus 1"
CONST_STATUT = "invité"

SECTION_STUDIOS = "STUDIOS"
SECTION_CHAMBRES = "CHAMBRES"

# Text markers used to locate section header rows.
STUDIOS_MARKER = "STUDIOS"
CHAMBRES_MARKER = "CHAMBRES"

# ---------------------------------------------------------------------------
# Colors (per number of persons) - exact RGB requested by the user.
# ---------------------------------------------------------------------------
COLOR_WHITE = None                 # 1-3 persons -> no fill change (white)
COLOR_LIGHT = "FCE4D6"             # 4-9 persons  (light orange)
COLOR_DARK = "F4B084"              # 10+ persons  (orange)
COLOR_GROUP_NAME = "D9E1F2"        # group row -> tint the name cell
COLOR_OVERSTAY = "FF0000"          # overstay (no check-out) -> red

# Uniform height (points) applied to every data row so all rows stay the same
# size in the generated planning.
ROW_HEIGHT = 18.0


def color_for_persons(n: int):
    """Return hex fill (without leading FF) for a given person count, or None for white."""
    if n is None:
        return None
    if n <= 3:
        return COLOR_WHITE
    if n <= 9:
        return COLOR_LIGHT
    return COLOR_DARK


# ---------------------------------------------------------------------------
# French month names (index 1..12) as used in sheet titles.
# ---------------------------------------------------------------------------
MONTHS_FR = {
    1: "JANVIER", 2: "FEVRIER", 3: "MARS", 4: "AVRIL",
    5: "MAI", 6: "JUIN", 7: "JUILLET", 8: "AOUT",
    9: "SEPTEMBRE", 10: "OCTOBRE", 11: "NOVEMBRE", 12: "DECEMBRE",
}

# accents that may appear in real sheet titles
MONTHS_FR_ACCENT = {
    2: "FÉVRIER", 8: "AOÛT", 12: "DÉCEMBRE",
}


def normalize(text: str) -> str:
    """Uppercase + strip accents for lenient matching."""
    if text is None:
        return ""
    import unicodedata
    t = unicodedata.normalize("NFD", str(text))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.upper().strip()


def month_matches(sheet_title: str, month: int) -> bool:
    return normalize(sheet_title) == normalize(MONTHS_FR[month])


# ---------------------------------------------------------------------------
# Default status handling (configurable from the website).
# action: add | ignore | delete
# note:   optional note written above the name
# nuitees: "dates" (départ-arrivée) | "one_per_person" (1 x personnes)
# ---------------------------------------------------------------------------
DEFAULT_STATUS_RULES = [
    # API status values are UPPERCASE (APPROVED, VALIDATED, COMPLETED,
    # CANCELED, CANCELLED, PENDING, REJECTED, ...). Matching is a substring
    # test (normalized), evaluated in order; first match wins.
    {"match": "APPROV",   "label": "Approuvé",            "action": "add",    "note": "",             "nuitees": "dates"},
    {"match": "VALID",    "label": "Validé",              "action": "add",    "note": "",             "nuitees": "dates"},
    {"match": "COMPLET",  "label": "Confirmé / Complété", "action": "add",    "note": "",             "nuitees": "dates"},
    {"match": "CONFIRM",  "label": "Confirmé",            "action": "add",    "note": "",             "nuitees": "dates"},
    {"match": "PEND",     "label": "En attente",          "action": "add",    "note": "En attente",   "nuitees": "dates"},
    {"match": "FACTUR",   "label": "Annulation facturée", "action": "add",    "note": "",             "nuitees": "one_per_person"},
    {"match": "CANCEL",   "label": "Annulé",              "action": "delete", "note": "",             "nuitees": "dates"},
    {"match": "ANNUL",    "label": "Annulé",              "action": "delete", "note": "",             "nuitees": "dates"},
    {"match": "REJECT",   "label": "Rejeté",              "action": "delete", "note": "",             "nuitees": "dates"},
    {"match": "REFUS",    "label": "Refusé",              "action": "delete", "note": "",             "nuitees": "dates"},
]

DEFAULT_CONFIG = {
    "residence": "16",
    "group_threshold": 4,          # > 4 persons -> use Sujet as group name
    "auto_sync_enabled": False,
    "auto_sync_minutes": 10,
    "auto_apply": False,           # default: simulation before apply
    "on_cancel": "archive",          # delete | archive (archive keeps them in the reservations list)
    "status_rules": DEFAULT_STATUS_RULES,
    "max_parallel": 3,
    # Entities excluded from the planning even when the room belongs to the
    # configured residence (case/accents insensitive, normalized match).
    "excluded_entities": ["CHU"],
}
