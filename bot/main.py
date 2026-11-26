"""Willhaben E-Bike robot.

  python bot/main.py search   # every ~10 min: new defekt/unfall bikes + Bosch deals
  python bot/main.py watch    # 3x daily: check watchlist for price/text/status changes

Only reads public willhaben pages. Never buys or contacts anyone.
"""
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
import value as val  # noqa: E402
import vision  # noqa: E402
import willhaben as wh  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
BIKES = os.path.join(ROOT, "fahrraeder")
STATE = os.path.join(ROOT, ".state.json")
TOPIC = os.environ.get("NTFY_TOPIC", "wh-ebike-zzee721a6z")
NTFY = "https://ntfy.sh"
VIE = ZoneInfo("Europe/Vienna")

MAX_PRICE = 1200  # gilt für alle Suchen
ONLY_PRIVATE = True
MIN_YEAR = 2019
DEAL = 0.30
ALERT = 0.45
DRY = os.environ.get("DRY_RUN") == "1"


def now():
    return datetime.now(VIE).strftime("%Y-%m-%d %H:%M")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def append_csv(name, header, row):
    path = os.path.join(ROOT, name)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(header)
        w.writerow(row)


def push(title, msg, url=None, prio="high", tags="bike,moneybag"):
    print(f"[push] {title} | {msg}")
    if DRY:
        return
    headers = {"Title": title.encode("utf-8"), "Priority": prio, "Tags": tags}
    if url:
        headers["Click"] = url
    req = urllib.request.Request(f"{NTFY}/{TOPIC}", data=msg.encode("utf-8"), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print("ntfy failed:", e)


def eur(x):
    return "?" if x is None else f"{x:,.0f} €".replace(",", ".")


DEFECT_RE = re.compile(r"defekt|unfall|sturzschaden|rahmenbruch|kaputt|beschädigt|beschaedigt|bastler|"
                       r"motorschaden|akku\s*(ist\s*)?(tot|kaputt|defekt)|fehlermeldung|error\s*\d|geht nicht|"
                       r"funktioniert nicht|ersatzteilspender", re.I)


OTHER_MOTOR_RE = re.compile(r"shimano\s*steps|\bsteps\s*e\d|yamaha|bafang|panasonic|ananda|mivice|"
                            r"fazua|mahle|giant\s*syncdrive|heckmotor|hinterradmotor|"
                            r"frontmotor|vorderradmotor|nabenmotor", re.I)
VISION_PER_DAY = int(os.environ.get("VISION_PER_DAY", "50"))  # cost cap: ~1.5 Cent per bike -> max ~22 €/Monat


def vision_budget(state):
    v = state.setdefault("vision", {})
    today = datetime.now(VIE).strftime("%Y-%m-%d")
    if v.get("date") != today:
        v["date"], v["n"] = today, 0
    if v["n"] >= VISION_PER_DAY:
        return False
    v["n"] += 1
    return True


def is_defect(ad, zustand=""):
    txt = f"{ad['title']} {ad.get('snippet', '')} {ad.get('description', '')}"
    if re.search(r"nicht\s+defekt|kein(e|en)?\s+(unfall|defekt)|unfallfrei", txt, re.I) and "defekt" not in zustand.lower():
        txt = re.sub(r"nicht\s+defekt|kein(e|en)?\s+(unfall|defekt)|unfallfrei", "", txt, flags=re.I)
    return "defekt" in zustand.lower() or bool(DEFECT_RE.search(txt))


PII_RE = re.compile(r"(\+?\d[\d /-]{7,}\d)|([\w.+-]+@[\w-]+\.[\w.]+)")


def scrub(text):
    """Remove phone numbers and e-mail addresses of sellers before storing text."""
    return PII_RE.sub("[entfernt]", text or "")


DEAL_HEADER = ["erfasst", "id", "modell", "titel", "preis", "schaetzwert", "rabatt_%", "jahr", "laufleistung_km",
               "uvp", "uvp_quelle", "kaufrabatt_%", "altersabschlag_%", "privatabschlag_%", "km_abschlag_%",
               "system2_%", "servicekosten", "entfernung_km", "link"]


def deal_row(d, e, disc, y, odo, model_txt):
    return [now(), d["id"], model_txt, d["title"], d["price"], e["value"], round(disc * 100), y, odo or "",
            e["uvp"], e["uvp_quelle"], e["kaufrabatt_pct"], e["altersabschlag_pct"], e["privatabschlag_pct"],
            e["km_abschlag_pct"], e["system_abschlag_pct"], e["servicekosten"], d["km"], d["url"]]


def bike_report(d, est=None, note=""):
    os.makedirs(BIKES, exist_ok=True)
    path = os.path.join(BIKES, f"{d['id']}.md")
    lines = [f"# {d['title']}", "", f"- Link: {d['url']}", f"- Preis: {eur(d.get('price'))}",
             f"- Ort: {d.get('location', '')} ({d.get('km', '?')} km von 4493)",
             f"- Zustand: {d.get('zustand') or '?'}", f"- Status: {d.get('status', '?')}",
             f"- Erfasst: {now()}"]
    if note:
        lines.append(f"- Hinweis: {note}")
    if est:
        e = est
        lines += ["", "## Preisbildung (Dossier-Methode)",
                  "| Schritt | Wert |", "|---|---|",
                  f"| Antrieb | {e['drive']} |",
                  f"| UVP ({e['uvp_quelle']}) | {eur(e['uvp'])} |",
                  f"| − vermuteter Rabatt beim Kauf ({e['year']}) | −{e['kaufrabatt_pct']} % → {eur(e['strassenpreis'])} |",
                  f"| − Altersabschlag ({e['age']} J.) | −{e['altersabschlag_pct']} % → Händlerwert {eur(e['haendlerwert'])} |",
                  f"| − Privat statt Händler | −{e['privatabschlag_pct']} % → {eur(e['intact_value'])} |",
                  f"| km-Korrektur ({str(e['mileage']) + ' km' if e.get('mileage') else 'km unbekannt'}) | {e['km_abschlag_pct']:+} % |",
                  f"| Bosch System 2 | {e['system_abschlag_pct']} % |",
                  f"| Zustand | ×{e['condition_factor']} {', '.join(e['condition_notes'])} |",
                  f"| − Servicekosten | −{eur(e['servicekosten'])} |",
                  f"| Marktcheck | {e['comparables']} Vergleichsinserate" + (f", Median {eur(e['comparable_median'])}" if e.get('comparable_median') else "")
                  + (" → gedeckelt" if e.get('gedeckelt') else "") + " |",
                  f"| **Geschätzter Privatwert** | **{eur(e['value'])}** |",
                  f"| **Rabatt zum Angebot** | **{e.get('discount', 0) * 100:.0f} %** |"]
    lines += ["", "## Beschreibung", "", scrub(d.get("description", ""))[:4000], ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------- watchlist add via ntfy
def poll_add_topic(state):
    """Hannes can share a willhaben link to topic <TOPIC>-add in the ntfy app -> goes on watchlist."""
    since = state.get("add_since", "12h")
    url = f"{NTFY}/{TOPIC}-add/json?poll=1&since={since}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            lines = r.read().decode().splitlines()
    except Exception as e:
        print("ntfy poll failed:", e)
        return []
    added = []
    for ln in lines:
        try:
            m = json.loads(ln)
        except Exception:
            continue
        if m.get("event") != "message":
            continue
        state["add_since"] = m["id"]
        for ad_id in re.findall(r"(?:-|adId=|\b)(\d{8,11})(?:/|\b)", m.get("message", "") + " " + m.get("click", "")):
            added.append(ad_id)
    return added


def watchlist_ids():
    path = os.path.join(ROOT, "watchlist.txt")
    if not os.path.exists(path):
        return []
    ids = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.split("#")[0].strip()
        m = re.search(r"(\d{8,11})/?$", ln) or re.search(r"(\d{8,11})", ln)
        if m:
            ids.append(m.group(1))
    return list(dict.fromkeys(ids))


def add_to_watchlist(ad_id, label=""):
    path = os.path.join(ROOT, "watchlist.txt")
    if ad_id in watchlist_ids():
        return False
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Eine Anzeige pro Zeile: Link oder ID. Alles nach # ist Kommentar.\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{ad_id}  # {label} (hinzugefügt {now()})\n")
    return True


# ---------------------------------------------------------------- search
def run_search():
    state = load(STATE, {"seen": {}})
    seen = state.setdefault("seen", {})
    first_run = not seen and os.environ.get("FORCE_EVAL") != "1"

    for ad_id in poll_add_topic(state):
        if add_to_watchlist(ad_id, "per ntfy"):
            push("Watchlist ✅", f"Anzeige {ad_id} wird jetzt 3x täglich überwacht.", prio="low", tags="eyes")

    new_ads = []
    for page in (1, 2):
        _, ads = wh.search(rows=200, page=page)
        fresh = [a for a in ads if a["id"] not in seen]
        new_ads += fresh
        if len(fresh) < len(ads):  # reached already-known ads
            break
        time.sleep(2)
    # defect listings also by Zustand filter (catches bikes without the word in title)
    _, dz = wh.search(path=wh.EBIKE_CAT + "/a/zustand-defekt-24", rows=100)
    defect_zustand = {a["id"] for a in dz}
    new_ads += [a for a in dz if a["id"] not in seen and a["id"] not in {x["id"] for x in new_ads}]

    print(f"{now()}: {len(new_ads)} neue Inserate")
    stats = {"defekt": 0, "deal": 0, "alert": 0}
    for a in new_ads:
        seen[a["id"]] = a["published"] or now()
        if first_run:
            continue  # first run only learns the current listings
        if ONLY_PRIVATE and a["dealer"]:
            continue
        if a["price"] is None or a["price"] > MAX_PRICE:
            continue
        near = a["km"] is not None and a["km"] <= wh.RADIUS_KM
        if a["km"] is None:
            near = None  # unknown -> decide after detail fetch
        txt = f"{a['title']} {a['snippet']}"
        defect_hint = a["id"] in defect_zustand or is_defect(a)
        bosch_hint = a["price"] is not None and a["price"] <= MAX_PRICE
        if near is False or not (defect_hint or bosch_hint):
            continue
        d = wh.detail(a["url"])
        time.sleep(1)
        if d.get("status") == "geloescht":
            continue
        d["km"] = d.get("km") or a["km"]
        d["location"] = d.get("location") or a["location"]
        if d["km"] is not None and d["km"] > wh.RADIUS_KM:
            continue
        full = f"{d['title']}\n{d['description']}"

        text_ok = val.is_bosch(full) or val.is_brose(full)
        other_motor = bool(OTHER_MOTOR_RE.search(full))
        tyear, thint = val.find_year(full)
        too_old = tyear is not None and tyear < MIN_YEAR and not thint
        vis = None
        if vision.enabled() and (text_ok or not other_motor) and not too_old and vision_budget(state):
            vis = vision.analyze(d)
        y = (vis or {}).get("modelljahr") or tyear or thint
        try:
            y = int(y) if y else None
        except (TypeError, ValueError):
            y = tyear or thint

        if a["id"] in defect_zustand or is_defect(d, d.get("zustand", "")):
            stats["defekt"] += 1
            if y and y >= MIN_YEAR:
                model_d = f"{vis.get('marke')} {vis.get('modell')}" if vis and vis.get("marke") else d["title"]
                push(f"🔧 Defekt-Bike ab {MIN_YEAR}: Bj {y}",
                     f"{model_d}\n{eur(d['price'])} · {d['km']} km entfernt · {d.get('location') or ''}\n"
                     f"{(vis or {}).get('schaeden') or ''}\n{d['url']}".replace("\n\n", "\n"), url=d["url"], tags="wrench")
            append_csv("defekt_unfall.csv",
                       ["erfasst", "id", "titel", "preis", "entfernung_km", "laufleistung_km", "ort", "zustand", "link"],
                       [now(), d["id"], d["title"], d["price"], d["km"], val.find_mileage(full) or "",
                        d["location"], d.get("zustand"), d["url"]])

        if not (d["price"] and d["price"] <= MAX_PRICE):
            continue
        sure = vis and vis.get("sicherheit") in ("hoch", "mittel")
        vm = (vis or {}).get("motor_hersteller") or "unbekannt"
        if sure and vm not in ("Bosch", "Brose", "unbekannt"):
            continue  # photos/specs show another drive
        if not (text_ok or vm in ("Bosch", "Brose")):
            continue
        if y is None or y < MIN_YEAR:
            continue
        extra = ""
        if vis:
            extra = (f"\n{vis.get('marke') or ''} {vis.get('modell') or ''} {vm} {vis.get('bosch_linie') or ''} "
                     f"{(str(vis.get('akku_wh')) + ' Wh') if vis.get('akku_wh') else ''}\n{vis.get('schaeden') or ''}")
        try:
            odo = int((vis or {}).get("laufleistung_km") or 0) or None
        except (TypeError, ValueError):
            odo = None
        odo = odo or val.find_mileage(full)
        est = val.estimate(d["title"], d["description"] + extra, d.get("zustand", ""), y,
                           new_price=(vis or {}).get("uvp_eur"), mileage=odo)
        if not est["value"]:
            continue
        disc = 1 - d["price"] / est["value"]
        est["discount"] = disc
        model_txt = f"{vis.get('marke')} {vis.get('modell')}" if vis else ""
        print(f"  {vm if vm != 'unbekannt' else 'Bosch/Brose'} {y}: {d['title'][:50]} {eur(d['price'])} vs {eur(est['value'])} -> {disc:.0%} {model_txt}")
        note = ""
        if vis:
            note = (f"Foto-/Spezifikationsprüfung: {model_txt} ({vis.get('modelljahr') or '?'}), "
                    f"Motor {vis.get('motor_hersteller')} {vis.get('bosch_linie') or ''}, UVP {eur(vis.get('uvp_eur'))}, "
                    f"Sicherheit {vis.get('sicherheit')}, Quelle {vis.get('quelle')}. {vis.get('notiz') or ''} "
                    f"Schäden: {vis.get('schaeden') or '-'}")
        if disc >= DEAL:
            stats["deal"] += 1
            append_csv("deals.csv", DEAL_HEADER, deal_row(d, est, disc, y, odo, model_txt))
            bike_report(d, est, note=("Deal ≥30%. " + note).strip())
            add_to_watchlist(d["id"], f"Deal {round(disc * 100)}%: {d['title'][:40]}")
        if disc >= ALERT:
            stats["alert"] += 1
            push(f"🔥 {round(disc * 100)}% unter Wert!",
                 f"{model_txt or d['title']}\n{eur(d['price'])} statt ~{eur(est['value'])} · {d['km']} km entfernt · "
                 f"Bj {y} · Laufleistung {f'{odo:,} km'.replace(',', '.') if odo else '?'}",
                 url=d["url"])

    # keep state small: forget ids older than ~60 days
    if len(seen) > 30000:
        for k in sorted(seen, key=lambda k: seen[k])[:-20000]:
            seen.pop(k)
    state["last_search"] = now()
    save(STATE, state)
    print("Ergebnis:", stats, "(Lernlauf)" if first_run else "")


# ---------------------------------------------------------------- watchlist check
def run_watch():
    wstate = load(os.path.join(ROOT, ".watch_state.json"), {})
    ids = watchlist_ids()
    # least recently checked first, so a run cut short by rate limits resumes next time
    ids.sort(key=lambda i: (wstate.get(i) or {}).get("checked", ""))
    deadline = time.time() + 60 * int(os.environ.get("WATCH_MAX_MIN", "20"))
    fails = 0
    print(f"{now()}: prüfe {len(ids)} Watchlist-Anzeigen")
    try:
        _watch_loop(ids, wstate, deadline, fails)
    finally:
        save(os.path.join(ROOT, ".watch_state.json"), wstate)
        write_overview(wstate)


def _watch_loop(ids, wstate, deadline, fails):
    done = 0
    for ad_id in ids:
        if time.time() > deadline or fails >= 3:
            print(f"  Abbruch nach {done} Anzeigen (Zeit/Ratenlimit), Rest beim nächsten Lauf")
            break
        try:
            d = wh.detail(ad_id)
            fails = 0
        except Exception as e:  # rate limit / network: skip this ad, next run retries
            print(f"  {ad_id} übersprungen: {e}")
            fails += 1
            time.sleep(20)
            continue
        done += 1
        time.sleep(2.5)
        cur = {"price": d.get("price"), "status": d.get("status"),
               "desc_hash": hashlib.sha1((d.get("description") or "").encode()).hexdigest()[:10],
               "title": d.get("title") or (wstate.get(ad_id) or {}).get("title", ""),
               "mileage": val.find_mileage(d.get("description") or "") or (wstate.get(ad_id) or {}).get("mileage"),
               "online_seit": d.get("online_seit") or (wstate.get(ad_id) or {}).get("online_seit", ""),
               "ablauf": d.get("ablauf") or (wstate.get(ad_id) or {}).get("ablauf", ""),
               "kategorie": d.get("kategorie") or (wstate.get(ad_id) or {}).get("kategorie", ""),
               "url": d.get("url") or (wstate.get(ad_id) or {}).get("url", ""),
               "expiry_pushed": (wstate.get(ad_id) or {}).get("expiry_pushed", ""),
               "checked": now(),
               "image": ((d.get("images") or [None])[0]) or (wstate.get(ad_id) or {}).get("image", ""),
               "since": (wstate.get(ad_id) or {}).get("since") or now()}
        old = wstate.get(ad_id)
        changes = []
        if old is None:
            changes.append(("neu überwacht", "", f"{eur(cur['price'])}, {cur['status']}"))
        else:
            if cur["status"] == "geloescht":
                cur["price"], cur["desc_hash"] = old.get("price"), old.get("desc_hash")
            if cur["price"] != old.get("price"):
                changes.append(("Preis", eur(old.get("price")), eur(cur["price"])))
            if cur["status"] != old.get("status"):
                changes.append(("Status", old.get("status"), cur["status"]))
            if cur["desc_hash"] != old.get("desc_hash"):
                changes.append(("Beschreibung", "geändert", "geändert"))
        for what, a, b in changes:
            append_csv("watchlist_log.csv", ["zeit", "id", "titel", "änderung", "alt", "neu", "link"],
                       [now(), ad_id, cur["title"], what, a, b, d.get("url", "")])
            print(f"  {ad_id} {what}: {a} -> {b}")
        if changes and d.get("status") != "geloescht":
            note = "; ".join(f"{w}: {a} → {b}" for w, a, b in changes)
            bike_report({**d, "id": ad_id}, note=f"Letzte Änderung {now()}: {note}")
        # push once when the ad runs out tomorrow (Vienna date)
        end = parse_dt(cur["ablauf"])
        tomorrow = (datetime.now(VIE) + timedelta(days=1)).date()
        if end and cur["status"] not in ("geloescht", "verkauft") and end.astimezone(VIE).date() == tomorrow \
                and cur["expiry_pushed"] != cur["ablauf"]:
            push("⏳ Anzeige läuft morgen ab", f"{cur['title'][:60]}\n{eur(cur['price'])} · endet {end.astimezone(VIE):%d.%m. %H:%M}\n{cur['url']}",
                 url=cur["url"], prio="default", tags="hourglass")
            cur["expiry_pushed"] = cur["ablauf"]
        wstate[ad_id] = cur


def parse_dt(v):
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None


def fmt_date(v):
    dt = parse_dt(v)
    if not dt or dt.year > datetime.now().year + 2:
        return "–"
    return dt.astimezone(VIE).strftime("%d.%m.%Y")


def write_overview(wstate):
    lines = ["# Watchlist – aktueller Stand", "", f"Stand: {now()}", "",
             "| ID | Titel | Preis | Laufleistung | Status | Online seit | Läuft ab | Kategorie |",
             "|---|---|---|---|---|---|---|---|"]
    ids = watchlist_ids()
    for k, v in wstate.items():
        if k in ids:
            lines.append(f"| [{k}](fahrraeder/{k}.md) | {v.get('title', '')[:50]} | {eur(v.get('price'))} | "
                         f"{str(v.get('mileage')) + ' km' if v.get('mileage') else '?'} | {v.get('status')} | "
                         f"{fmt_date(v.get('online_seit'))} | {fmt_date(v.get('ablauf'))} | {v.get('kategorie') or ''} |")
    with open(os.path.join(ROOT, "WATCHLIST.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    write_watchlist_html(wstate, ids)


STATUS_STYLE = {"verfuegbar": ("verfügbar", "#1f6f54", "#e3f2ec"), "reserviert": ("reserviert", "#8a6100", "#fdf5dd"),
                "verkauft": ("verkauft", "#9b2c2c", "#fde8e8"), "geloescht": ("offline / abgelaufen", "#5f6a63", "#eceeed")}


def _watch_log():
    path = os.path.join(ROOT, "watchlist_log.csv")
    out = {}
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8"), delimiter=";"):
            out.setdefault(r["id"], []).append(r)
    return out


def write_watchlist_html(wstate, ids=None):
    """Document-style watchlist (one card per ad: photo, facts, change history) -> data/WATCHLIST.html.
    Uploaded to Google Drive as a Google Doc by the evening task."""
    from html import escape as h
    ids = ids or watchlist_ids()
    log = _watch_log()
    rank = {"verfuegbar": 0, "reserviert": 1, "verkauft": 2, "geloescht": 3}
    items = [(k, wstate[k]) for k in ids if k in wstate]
    cutoff = (datetime.now(VIE) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    fresh = {k for k, rows in log.items() if any(r.get("änderung") == "Status" and r.get("zeit", "") >= cutoff for r in rows)}
    # freshly changed status first (24 h), then available ... offline at the bottom
    items.sort(key=lambda kv: (0 if kv[0] in fresh else 1, rank.get(kv[1].get("status"), 4), kv[1].get("ablauf") or "9999"))
    n = {s: sum(1 for _, v in items if v.get("status") == s) for s in rank}
    soon = [v for _, v in items if v.get("status") == "verfuegbar" and parse_dt(v.get("ablauf"))
            and (parse_dt(v.get("ablauf")).astimezone(VIE).date() - datetime.now(VIE).date()).days in (0, 1, 2)]
    out = [f"<html><head><meta charset='utf-8'><title>Watchlist</title></head>"
           f"<body style='font-family:Arial,sans-serif;color:#1d2420'>",
           "<h1 style='color:#1f6f54'>🚲 Meine willhaben-Watchlist</h1>",
           f"<p style='color:#5f6a63'>Stand {now()} · {len(items)} Anzeigen · {n['verfuegbar']} verfügbar · "
           f"{n['reserviert']} reserviert · {n['verkauft']} verkauft · {n['geloescht']} offline</p>"]
    if soon:
        out.append("<p style='background:#fff1e8;padding:8px'><b>⏳ Läuft bald ab:</b> " +
                   ", ".join(f"{h(v.get('title', '')[:40])} ({fmt_date(v.get('ablauf'))})" for v in soon) + "</p>")
    for k, v in items:
        lab, fg, bg = STATUS_STYLE.get(v.get("status"), (v.get("status") or "?", "#333", "#eee"))
        url = v.get("url") or f"https://www.willhaben.at/iad/object?adId={k}"
        img = (f"<a href='{h(url)}'><img src='{h(v['image'])}' width='160' height='120' "
               f"style='object-fit:cover;border-radius:8px'></a>") if v.get("image") else "<i>kein Foto</i>"
        changes = [r for r in log.get(k, []) if r.get("änderung") != "neu überwacht"]
        hist = "".join(f"<tr><td></td><td style='color:#c2410c;font-size:13px'>↳ {h(r['zeit'])}: "
                       f"<b>{h(r['änderung'])}</b> {h(r['alt'])} → {h(r['neu'])}</td></tr>" for r in changes)
        out.append(
            "<table style='width:100%;border-collapse:collapse;margin:14px 0;border-bottom:1px solid #dde1dc"
            + (";background:#fff4c2" if k in fresh else "") + "'>"
            + (f"<tr><td colspan='2' style='background:#f5c518;color:#1d2420;font-weight:bold;padding:4px 8px'>"
               f"🆕 Status geändert: {h(STATUS_STYLE.get(v.get('status'), (v.get('status') or '?',))[0])}</td></tr>" if k in fresh else "")
            + f"<tr><td style='width:170px;vertical-align:top;padding:6px'>{img}</td>"
            "<td style='vertical-align:top;padding:6px'>"
            f"<a href='{h(url)}' style='font-size:16px;font-weight:bold;color:#1d2420'>{h(v.get('title') or '(nicht mehr abrufbar)')}</a><br>"
            f"<span style='font-size:18px;font-weight:bold'>{eur(v.get('price'))}</span> &nbsp; "
            f"<span style='background:{bg};color:{fg};padding:2px 8px;font-size:12px'>{lab}</span><br>"
            f"<span style='color:#5f6a63;font-size:13px'>{h(v.get('kategorie') or '')}</span><br>"
            f"<span style='font-size:13px'>Online seit <b>{fmt_date(v.get('online_seit'))}</b> · "
            f"läuft ab <b>{fmt_date(v.get('ablauf'))}</b> · überwacht seit <b>{h((v.get('since') or '')[:10]) or '–'}</b>"
            + (f" · {v['mileage']:,} km".replace(",", ".") if v.get("mileage") else "") + "</span>"
            "</td></tr>" + hist + "</table>")
    out.append("</body></html>")
    with open(os.path.join(ROOT, "WATCHLIST.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(out).encode("ascii", "xmlcharrefreplace").decode())  # entities: Google Docs import breaks raw emoji


def run_visiontest():
    """Live check of the photo recognition on a few ads (ids in VISION_TEST_IDS)."""
    ids = os.environ.get("VISION_TEST_IDS", "1912186949,926233291,940653622,892824741,905904608").split(",")
    print("Schlüssel vorhanden:", vision.enabled())
    for ad_id in ids:
        d = wh.detail(ad_id.strip())
        if d.get("status") == "geloescht":
            print(ad_id, "gelöscht"); continue
        v = vision.analyze(d)
        y = (v or {}).get("modelljahr")
        est = val.estimate(d["title"], d["description"], d.get("zustand", ""), y or val.find_year(d["title"] + "\n" + d["description"])[0],
                           new_price=(v or {}).get("uvp_eur")) if v else None
        print(f"RESULT|{ad_id}|{d['price']}|{json.dumps(v, ensure_ascii=False)}|{est and est['value']}")


def run_batch():
    """One-off: photo recognition on up to BATCH_N current listings (ignores the daily cap)."""
    from concurrent.futures import ThreadPoolExecutor
    n = int(os.environ.get("BATCH_N", "100"))
    cands = []
    for page in range(1, 8):
        _, ads = wh.search(rows=200, page=page, PRICE_TO=MAX_PRICE)
        for a in ads:
            if a["dealer"] or not a["price"] or a["price"] > MAX_PRICE or a["km"] is None or a["km"] > wh.RADIUS_KM:
                continue
            cands.append(a)
        if not ads:
            break
        time.sleep(1)
    print(f"{len(cands)} Kandidaten (privat, <= {MAX_PRICE} €, <= {wh.RADIUS_KM} km)")
    details = []
    for a in cands:
        if len(details) >= n:
            break
        d = wh.detail(a["url"])
        time.sleep(0.7)
        if d.get("status") == "geloescht":
            continue
        full = f"{d['title']}\n{d['description']}"
        if OTHER_MOTOR_RE.search(full) and not val.is_bosch(full):
            continue  # clearly another motor brand -> skip, saves money
        d["km"] = a["km"]
        details.append(d)
    print(f"{len(details)} Bikes zur Fotoprüfung")
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(vision.analyze, details))
    rows = []
    for d, v in zip(details, results):
        full = f"{d['title']}\n{d['description']}"
        v = v or {}
        bosch = v.get("motor_hersteller") == "Bosch" or (val.is_bosch(full) and v.get("motor_hersteller") in (None, "unbekannt"))
        y = v.get("modelljahr") or val.find_year(full)[0] or val.find_year(full)[1]
        try:
            y = int(y) if y else None
        except (TypeError, ValueError):
            y = None
        value = disc = None
        if bosch and y and y >= MIN_YEAR:
            extra = f"\n{v.get('marke') or ''} {v.get('modell') or ''} Bosch {v.get('bosch_linie') or ''}"
            est = val.estimate(d["title"], d["description"] + extra, d.get("zustand", ""), y, new_price=v.get("uvp_eur"),
                               mileage=v.get("laufleistung_km") or val.find_mileage(full))
            value = est["value"]
            disc = round((1 - d["price"] / value) * 100) if value else None
        rows.append([d["id"], d["price"], d["km"], v.get("marke"), v.get("modell"), y, v.get("motor_hersteller"),
                     v.get("bosch_linie"), v.get("uvp_eur"), value, disc, v.get("sicherheit"), v.get("schaeden"),
                     d["title"][:60], d["url"]])
    path = os.path.join(ROOT, "fototest_100.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "preis", "km", "marke", "modell", "jahr", "motor", "bosch_linie", "uvp", "schaetzwert",
                    "rabatt_%", "sicherheit", "schaeden", "titel", "link"])
        w.writerows(rows)
    ok = sum(1 for r in rows if r[3])
    bosch20 = [r for r in rows if r[9]]
    print(f"BATCH erkannt {ok}/{len(rows)}, Bosch ab 2020: {len(bosch20)}")
    for r in sorted(bosch20, key=lambda r: -(r[10] or -999)):
        print("BATCH|" + "|".join(str(x) for x in r))


def run_odo():
    """Re-read odometer (Tacho) photos for the ids in data/odo_ids.txt -> data/laufleistung_fotos.csv."""
    from concurrent.futures import ThreadPoolExecutor
    ids = [l.strip() for l in open(os.path.join(ROOT, "odo_ids.txt")) if l.strip()]
    details = []
    for i in ids:
        d = wh.detail(i)
        time.sleep(0.7)
        if d.get("status") != "geloescht":
            details.append(d)
    with ThreadPoolExecutor(max_workers=5) as ex:
        res = list(ex.map(vision.read_odometer, details))
    path = os.path.join(ROOT, "laufleistung_fotos.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "laufleistung_km", "quelle", "foto_nr", "titel", "link"])
        for d, v in zip(details, res):
            v = v or {}
            w.writerow([d["id"], v.get("laufleistung_km") or "", v.get("quelle") or "", v.get("foto_nr") or "",
                        d["title"][:60], d["url"]])
    found = sum(1 for v in res if v and v.get("laufleistung_km"))
    print(f"ODO {found}/{len(details)} mit Laufleistung ({len(ids) - len(details)} gelöscht)")


if __name__ == "__main__":
    os.makedirs(ROOT, exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "search"
    {"search": run_search, "watch": run_watch, "visiontest": run_visiontest, "batch": run_batch, "odo": run_odo,
     "ping": lambda: push("Willhaben-Roboter 🚲", "Test: Hier kommen deine Schnäppchen-Alarme (≥45% unter Wert) an!",
                          tags="tada")}[cmd]()
