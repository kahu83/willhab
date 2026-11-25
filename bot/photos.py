"""Download listing photos (resized) + details for the presentation page."""
import io
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import willhaben as wh  # noqa: E402
from PIL import Image  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
OUT = os.path.join(ROOT, "praesentation")


def main():
    os.makedirs(OUT, exist_ok=True)
    meta = {}
    for ln in open(os.path.join(ROOT, "praesentation_ids.txt")):
        if not ln.strip():
            continue
        grp, ad_id = ln.split()
        d = wh.detail(ad_id)
        time.sleep(0.7)
        if d.get("status") == "geloescht":
            meta[ad_id] = {"group": grp, "status": "geloescht"}
            continue
        files = []
        for n, url in enumerate((d.get("images") or [])[: 4 if grp == "I" else 3]):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": wh.UA})
                raw = urllib.request.urlopen(req, timeout=30).read()
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                im.thumbnail((720, 720))
                name = f"{ad_id}_{n}.jpg"
                im.save(os.path.join(OUT, name), "JPEG", quality=70, optimize=True)
                files.append(name)
            except Exception as e:
                print("img fail", ad_id, n, e)
        meta[ad_id] = {"group": grp, "title": d["title"], "price": d["price"], "status": d["status"],
                       "zustand": d.get("zustand"), "location": d.get("location"), "url": d["url"],
                       "description": __import__("re").sub(r"(\+?\d[\d /-]{7,}\d)|([\w.+-]+@[\w-]+\.[\w.]+)", "[entfernt]", (d.get("description") or ""))[:700], "images": files,
                       "changed": d.get("changed")}
        print(ad_id, len(files))
    json.dump(meta, open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
