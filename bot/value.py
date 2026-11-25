"""Rough market-value estimate for a used Bosch e-bike (EUR).

Two sources, combined:
 1) a rule-based model (Bosch drive line, battery, brand tier, age, condition)
 2) median asking price of comparable working bikes currently on willhaben
The result is a guide, not an appraisal.
"""
import re
import statistics
from datetime import date

import willhaben as wh

THIS_YEAR = date.today().year

# typical new price (EUR) of a complete bike by Bosch drive line
DRIVE_NEW = [
    (r"turbo\s*levo|\blevo\b", "Specialized Turbo Levo (Brose)", 5500),
    (r"turbo\s*(vado|como|tero)|\bvado\b|\bcomo\b|\btero\b", "Specialized Turbo Vado/Como/Tero (Brose)", 3800),
    (r"\bbrose\b|specialized\s*turbo", "Brose", 3500),
    (r"cargo\s*line", "Cargo Line", 5500),
    (r"performance\s*(line\s*)?sx|\bsx\b", "Performance Line SX", 4800),
    (r"\bcx\b|performance\s*(line\s*)?cx|\b(85|100|120)\s*nm", "Performance Line CX", 4300),
    (r"performance\s*(line)?\s*(speed)", "Performance Speed", 4500),
    (r"performance", "Performance Line", 3300),
    (r"active\s*(line)?\s*plus", "Active Line Plus", 2800),
    (r"active", "Active Line", 2500),
]
DEFAULT_DRIVE = ("Bosch (Linie unbekannt)", 3000)

BRAND_TIER = {
    1.35: ["riese", "müller", "mueller", "r&m", "stromer", "moustache", "gazelle", "santa cruz", "orbea"],
    1.15: ["cube", "ktm", "haibike", "scott", "trek", "focus", "giant", "simplon", "kalkhoff",
           "rotwild", "lapierre", "ghost", "flyer", "corratec", "bergamont",
           "centurion", "merida", "winora", "husqvarna", "canyon", "rose", "kona", "felt"],
    0.85: ["prophete", "fischer", "zündapp", "zundapp", "telefunken", "llobe", "rex", "blaupunkt"],
}

GEN_HINT = [  # Bosch drives/components that only exist from ~2020 onwards
    (r"smart\s*system", 2022), (r"\bcx\s*gen\s*4|bosch.{0,30}gen\s*4|4\.\s*gen", 2020), (r"(bosch|performance)[^\n]{0,20}\bsx\b|\bsx\b[^\n]{0,10}(motor|antrieb)", 2023),
    (r"powertube\s*750|750\s*wh", 2021), (r"powertube\s*625|\b625\s*wh|\b625\b", 2020), (r"kiox\s*300|kiox\s*500|purion\s*200|led\s*remote", 2022),
    (r"powertube\s*800|800\s*wh", 2024), (r"\bkiox\b", 2020), (r"nyon\s*\(?20(2[1-9])", 2021),
]

DEFECT_WORDS = ["defekt", "unfall", "sturz", "rahmenbruch", "rahmen gebrochen", "riss", "kaputt",
                "beschädigt", "beschaedigt", "bastler", "fehler", "error", "geht nicht", "funktioniert nicht",
                "ohne akku", "akku fehlt", "ersatzteil", "unfallschaden", "verbogen"]


def is_bosch(text):
    return bool(re.search(r"\bbosch\b", text, re.I))


BROSE_RE = re.compile(r"\bbrose\b|specialized\s*turbo|turbo\s*(levo|vado|como|tero)|specialized\s*(levo|vado|como|tero)", re.I)


def is_brose(text):
    """Brose motors, incl. Specialized Turbo bikes (Specialized 'Turbo' motors are built by Brose)."""
    return bool(BROSE_RE.search(text))


def find_year(text):
    """Model / purchase year from explicit wording only (ignores test/service dates). None if unknown."""
    t = text.lower()
    hits = []
    for m in re.finditer(r"(baujahr|bj\.?|modelljahr|modell|my|jahrgang|gekauft|kaufdatum|erstkauf|kauf)"
                         r"[\s:\-/]*(?:(?:im|in|am|ende|anfang|mitte)\s+)?(?:\d{1,2}[./]\s*)?(?:[a-zä]+\s+)?(20[12]\d)\b", t):
        hits.append(int(m.group(2)))
    for m in re.finditer(r"\bmy\s?'?(\d{2})\b", t):
        hits.append(2000 + int(m.group(1)))
    # a bare year in the ad title (e.g. "KTM Macina 2021") is usually the model year
    first_line = t.split("\n", 1)[0]
    hits += [int(y) for y in re.findall(r"\b(20[12]\d)\b", first_line)]
    hits = [y for y in hits if 2010 <= y <= THIS_YEAR + 1]
    model = [int(y) for y in re.findall(r"(?:modelljahr|modell|\bmy)[\s:\-/]*(20[12]\d)\b", t)]
    if model:
        year = max(model)  # model year is what counts for value
    else:
        year = min(hits) if hits else None   # Baujahr beats later purchase mentions
    hint = None
    for pat, y in GEN_HINT:
        if re.search(pat, t):
            hint = max(hint or 0, y)
    return year, hint


MILEAGE_RE = re.compile(
    r"(?:laufleistung|km[- ]?stand|kilometerstand|kilometer|gefahren|fahrleistung|tacho|gesamt[- ]?km|odo)"
    r"[^0-9\n]{0,25}?(?:ca\.?|zirka|circa|etwa|rund|knapp|nur|ungefähr|~)?\s*(\d{1,3}(?:[.\s']\d{3})+|\d{2,6})\s*(?:km|kilometer)?"
    r"|(?:nur|ca\.?|zirka|circa|etwa|rund|knapp|~)\s*(\d{1,3}(?:[.\s']\d{3})+|\d{2,6})\s*(?:km|kilometer)\b[^.\n]{0,30}?(?:gefahren|gelaufen|laufleistung|am tacho|runter|drauf)"
    r"|(\d{1,3}(?:[.\s']\d{3})+|\d{2,6})\s*(?:km|kilometer)\s*(?:gefahren|gelaufen|laufleistung|am tacho|runter|drauf|km-stand)",
    re.I)


def find_mileage(text):
    """Odometer reading in km from listing text, None if not stated. Ignores range ('Reichweite 100 km')."""
    best = None
    for m in MILEAGE_RE.finditer(text or ""):
        raw = next(g for g in m.groups() if g)
        ctx = text[max(0, m.start() - 25):m.start()].lower()
        if "reichweite" in ctx or "akku" in m.group(0).lower():
            continue
        try:
            km = int(re.sub(r"[.\s']", "", raw))
        except ValueError:
            continue
        if 20 <= km <= 80000:
            best = max(best or 0, km)
    return best


def mileage_factor(km, age=None):
    """Wear deduction: up to 2000 km free, then -2.5 % per 1000 km, floor 0.55."""
    if not km or km <= 2000:
        return 1.0
    return max(0.55, 1 - 0.025 * (km - 2000) / 1000)


def drive_line(text):
    for pat, name, price in DRIVE_NEW:
        if re.search(pat, text, re.I):
            return name, price
    return DEFAULT_DRIVE


def brand_factor(text):
    t = text.lower()
    for f, names in BRAND_TIER.items():
        if any(n in t for n in names):
            return f, next(n for n in names if n in t)
    return 1.0, None


def battery_wh(text):
    m = re.findall(r"(\d{3})\s*wh", text.lower())
    return max(int(x) for x in m) if m else None


def condition(text, zustand=""):
    t = text.lower()
    z = (zustand or "").lower()
    factor, notes = 1.0, []
    if "neuwertig" in z or "neu" == z:
        factor = 1.05
    t = re.sub(r"(von|aus) einem anderen\s+(e-?)?(rad|bike)[^.]*", "", t)
    if "akku" in t and any(w in t for w in ["akku defekt", "ohne akku", "akku fehlt", "akku kaputt", "akku tot"]):
        factor -= 0.25
        notes.append("Akku defekt/fehlt (-25%)")
    if re.search(r"motor\s*(ist\s*)?(defekt|kaputt)|motorschaden", t):
        factor -= 0.30
        notes.append("Motor defekt (-30%)")
    if re.search(r"rahmen(bruch|\s*gebrochen|\s*riss)|riss\s*im\s*rahmen", re.sub(r"anderen\s+(e-?)?(rad|bike)[^.]*", "", t)):
        factor -= 0.45
        notes.append("Rahmenschaden (-45%)")
    if not notes and ("defekt" in z or any(w in t for w in DEFECT_WORDS)):
        factor -= 0.20
        notes.append("Defekt/Unfall allgemein (-20%)")
    return max(factor, 0.2), notes


def rule_value(text, year, zustand="", new_price=None, mileage=None):
    drive, line_price = drive_line(text)
    bf, brand = brand_factor(text)
    wh_ = battery_wh(text)
    base = line_price * bf
    try:
        np_ = float(new_price) if new_price else None
    except (TypeError, ValueError):
        np_ = None
    if np_ and 800 <= np_ <= 15000:
        base = np_  # real UVP from spec lookup beats the rule table
    if wh_ and wh_ >= 625:
        base += 250
    elif wh_ and wh_ <= 400:
        base -= 250
    age = max(0, THIS_YEAR - (year or THIS_YEAR - 4))
    keep = 0.70 * (0.88 ** max(0, age - 1)) if age >= 1 else 0.80   # used value when intact
    keep = max(keep, 0.22)
    z = (zustand or "").lower()
    if z == "neu":
        keep = min(keep * 1.15, 0.85)  # unused (often old-stock) bike: a bit above used value
    elif "neuwertig" in z:
        keep = min(keep * 1.08, 0.80)
    intact = base * keep
    cf, notes = condition(text, zustand)
    km = mileage if mileage else find_mileage(text)
    mf = mileage_factor(km)
    if mf < 1:
        cf = max(0.2, cf * mf)
        notes = notes + [f"Laufleistung {km:,} km (-{round((1 - mf) * 100)}%)".replace(",", ".")]
    return {
        "mileage": km,
        "drive": drive, "brand": brand, "battery_wh": wh_, "age": age,
        "intact_value": round(intact), "condition_factor": round(cf, 2), "condition_notes": notes,
        "value": round(intact * cf),
    }


def comparable_median(ad_title, year, motor="bosch"):
    """Median asking price of working (gebraucht/neuwertig) Bosch bikes with similar title words."""
    words = [w for w in re.findall(r"[a-zA-ZäöüÄÖÜ0-9]{3,}", ad_title.lower())
             if w not in {"bike", "ebike", "e-bike", "fahrrad", "defekt", "unfall", "bosch", "verkaufe",
                          "elektro", "elektrofahrrad", "mit", "und", "brose", "specialized", "für", "zoll", "herren", "damen", "motor",
                          "akku", "neu", "gebraucht", "top", "zustand", "sehr", "gut"}][:3]
    if not words:
        return None, 0
    prices = []
    for zpath in ("/a/zustand-gebraucht-23", "/a/zustand-neuwertig-2546"):
        try:
            _, ads = wh.search(path=wh.EBIKE_CAT + zpath, rows=60, keyword=" ".join(words + [motor]))
        except Exception:
            continue
        for a in ads:
            p = a["price"]
            txt = (a["title"] + " " + a["snippet"]).lower()
            if not p or p < 400 or p > 12000 or any(w in txt for w in ("defekt", "unfall", "bastler")):
                continue
            y, _ = find_year(txt)
            if year and y and abs(y - year) > 2:
                continue
            prices.append(p)
    if len(prices) < 4:
        return None, len(prices)
    # asking prices are ~10% above real selling prices
    return round(statistics.median(prices) * 0.9), len(prices)


def estimate(title, text, zustand="", year=None, new_price=None, mileage=None):
    full = f"{title}\n{text}"
    r = rule_value(full, year, zustand, new_price, mileage)
    med, n = comparable_median(title, year, "specialized" if is_brose(full) and not is_bosch(full) else "bosch")
    if med:
        intact = round(0.5 * r["intact_value"] + 0.5 * med)
        r["comparables"] = n
        r["comparable_median"] = med
        r["intact_value"] = intact
        r["value"] = round(intact * r["condition_factor"])
    else:
        r["comparables"] = n
        r["comparable_median"] = None
    return r
