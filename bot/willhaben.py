"""Willhaben fetch helpers (read-only: only reads public listing pages)."""
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://www.willhaben.at"
EBIKE_CAT = "/iad/kaufen-und-verkaufen/marktplatz/fahrraeder/e-bikes-4556"

# PLZ 4493 Wolfern (Oberoesterreich)
HOME = (48.0890, 14.3700)
RADIUS_KM = 170

_NEXT = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de-AT,de"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return e.code, ""
            last = e
            if e.code == 429:  # rate limited: back off a bit longer
                time.sleep(15 * (i + 1))
                continue
        except Exception as e:  # network hiccup
            last = e
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def _next_data(html):
    m = _NEXT.search(html)
    return json.loads(m.group(1)) if m else None


def _attrs(obj):
    out = {}
    for a in (obj.get("attributes") or {}).get("attribute", []):
        v = a.get("values") or []
        out[a["name"]] = v[0] if len(v) == 1 else v
    return out


def distance_km(coords):
    try:
        lat, lon = (float(x) for x in str(coords).split(","))
    except Exception:
        return None
    la1, lo1 = map(math.radians, HOME)
    la2, lo2 = math.radians(lat), math.radians(lon)
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(h)), 1)


def _price(v):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return None


def summary_to_ad(s):
    at = _attrs(s)
    return {
        "id": str(s.get("id")),
        "title": at.get("HEADING") or s.get("description", ""),
        "price": _price(at.get("PRICE")),
        "snippet": at.get("BODY_DYN", ""),
        "postcode": at.get("POSTCODE", ""),
        "location": at.get("LOCATION", ""),
        "coords": at.get("COORDINATES", ""),
        "km": distance_km(at.get("COORDINATES", "")),
        "published": at.get("PUBLISHED_String", ""),
        "dealer": at.get("ISPRIVATE") == "0",
        "status": (s.get("advertStatus") or {}).get("id", ""),
        "url": f"{BASE}/iad/{at.get('SEO_URL', '')}",
    }


def search(path=EBIKE_CAT, rows=200, page=1, **params):
    q = {"rows": rows, "page": page, "sort": 1, **params}  # sort=1 -> newest first
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    status, html = _get(url)
    d = _next_data(html) if status == 200 else None
    if not d:
        raise RuntimeError(f"no search data ({status}) for {url}")
    sr = d["props"]["pageProps"]["searchResult"]
    items = (sr.get("advertSummaryList") or {}).get("advertSummary", [])
    return sr.get("rowsFound", 0), [summary_to_ad(s) for s in items]


def _strip_html(t):
    t = re.sub(r"<br\s*/?>|</p>|</li>", "\n", t or "")
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def detail(ad_id_or_url):
    """Full ad. Returns dict with status 'geloescht' when the ad is gone."""
    s = str(ad_id_or_url)
    url = s if s.startswith("http") else f"{BASE}/iad/object?adId={s}"
    status, html = _get(url)
    if status in (404, 410):
        return {"status": "geloescht", "url": url}
    d = _next_data(html)
    ad = ((d or {}).get("props", {}).get("pageProps", {}) or {}).get("advertDetails")
    if not ad:
        return {"status": "geloescht", "url": url}
    at = _attrs(ad)
    st = (ad.get("advertStatus") or {}).get("id", "")
    desc_all = " ".join(str(x) for x in [at.get("DESCRIPTION", "")])
    status_txt = {"active": "verfuegbar"}.get(st, st)
    if ad.get("disposed") or at.get("DISPOSED") in ("true", "1"):
        status_txt = "verkauft"
    if st in ("sold",):
        status_txt = "verkauft"
    if st in ("reserved",) or at.get("RESERVED") in ("true", "1"):
        status_txt = "reserviert"
    seo = (ad.get("seoMetaData") or {}).get("canonicalUrl") or url
    zustand = ""
    for info in ad.get("attributeInformation") or []:
        if (info.get("treeAttributeElement") or {}).get("code") in ("CONDITION", "ZUSTAND") or \
                (info.get("treeAttributeElement") or {}).get("label") == "Zustand":
            vals = info.get("values") or [{}]
            zustand = vals[0].get("label", "")
    coords = at.get("COORDINATES", "")
    addr = ad.get("advertAddressDetails") or {}
    if not coords and addr.get("coordinates"):
        coords = addr.get("coordinates")
    return {
        "id": str(ad.get("id")),
        "title": ad.get("description", ""),
        "price": _price(at.get("PRICE")),
        "description": _strip_html(desc_all),
        "status": status_txt,
        "raw_status": st,
        "zustand": zustand,
        "coords": coords,
        "km": distance_km(coords),
        "location": at.get("LOCATION/ADDRESS_2", "") or addr.get("postCity", ""),
        "url": seo,
        "changed": ad.get("changedDate", ""),
        "online_seit": ad.get("firstPublishedDate") or ad.get("publishedDate") or "",
        "ablauf": ad.get("endDate") or "",
        "kategorie": " › ".join(b.get("displayName", "") for b in (ad.get("breadcrumbs") or [])[-2:]),
        "images": [i.get("mainImageUrl") or i.get("referenceImageUrl")
                   for i in ((ad.get("advertImageList") or {}).get("advertImage") or [])
                   if i.get("mainImageUrl") or i.get("referenceImageUrl")],
    }
