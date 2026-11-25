"""Identify an e-bike from its photos + spec lookup (Claude Haiku via the user's own API key).

Runs only when ANTHROPIC_API_KEY is set (GitHub secret). Uses a pay-per-use API key,
never a Claude subscription. Daily cap keeps the cost bounded.
"""
import json
import os
import re

MODEL = "claude-haiku-5-5"
MAX_IMAGES = 4

PROMPT = """Du bist ein E-Bike-Experte. Unten ist ein willhaben-Inserat mit Fotos.
Aufgabe:
1. Identifiziere Marke, Modell und Modelljahr aus den FOTOS (Rahmenschriftzug, Motorgehäuse, Display, Akku,
   Rahmenform, Farbe) und dem Text. Verlass dich nicht nur auf die Verkäuferangaben.
2. Bestimme den Motor/Antrieb. Wenn das Modell erkennbar ist, suche mit der Websuche die Herstellerspezifikation
   (max. 1 Suche) und nimm den Antrieb von dort. Bosch-Motoren erkennt man auf Fotos am Bosch-Logo am Motor,
   Intuvia/Kiox/Purion/Nyon-Display, PowerPack/PowerTube-Akku.
3. Neupreis (UVP) des Modells, falls in der Spezifikation/Suche gefunden.
4. Sichtbare Schäden auf den Fotos.
5. Laufleistung (Gesamt-km): vom Display-Foto (ODO/Gesamt) oder aus dem Text. Nicht die Reichweite!

Antworte am Ende NUR mit einem JSON-Block:
```json
{"marke": "...", "modell": "...", "modelljahr": 2021 oder null, "motor_hersteller": "Bosch|Shimano|Yamaha|Brose|Bafang|Panasonic|andere|unbekannt",
 "bosch_linie": "Active Line|Active Line Plus|Performance Line|Performance Line CX|Performance Line SX|Cargo Line|null",
 "akku_wh": 500 oder null, "laufleistung_km": 5982 oder null, "uvp_eur": 3999 oder null, "schaeden": "kurz oder leer",
 "sicherheit": "hoch|mittel|niedrig", "quelle": "Foto|Spezifikation|Verkäufertext", "notiz": "1 Satz"}
```"""


def enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def analyze(ad):
    """ad: dict from willhaben.detail(). Returns parsed dict or None."""
    if not enabled():
        return None
    import anthropic  # installed in the workflow

    client = anthropic.Anthropic(max_retries=2, timeout=120)
    content = []
    for url in (ad.get("images") or [])[:MAX_IMAGES]:
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    content.append({"type": "text", "text": f"{PROMPT}\n\nTitel: {ad.get('title')}\nPreis: {ad.get('price')} €\n"
                                            f"Zustand: {ad.get('zustand')}\nBeschreibung:\n{(ad.get('description') or '')[:3000]}"})
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": "low"},
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.BadRequestError as e:
        # e.g. an image URL could not be fetched -> retry text-only once
        print("vision bad request, retry without images:", e)
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=4000, output_config={"effort": "low"},
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
                messages=[{"role": "user", "content": [content[-1]]}],
            )
        except anthropic.APIError as e2:
            print("vision failed:", e2)
            return None
    except anthropic.RateLimitError as e:
        print("vision rate limited:", e)
        return None
    except anthropic.APIStatusError as e:
        print("vision API error:", e.status_code, e)
        return None
    except anthropic.APIConnectionError as e:
        print("vision connection error:", e)
        return None

    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{[^{}]*\"marke\".*\})", text, re.S)
    if not m:
        print("vision: no JSON in answer")
        return None
    try:
        out = json.loads(m.group(1))
    except json.JSONDecodeError:
        print("vision: bad JSON")
        return None
    u = resp.usage
    out["_tokens"] = f"{u.input_tokens}/{u.output_tokens}"
    print(f"  vision: {out.get('marke')} {out.get('modell')} {out.get('modelljahr')} "
          f"{out.get('motor_hersteller')} {out.get('bosch_linie')} ({out.get('sicherheit')}, {out['_tokens']} tok)")
    return out
