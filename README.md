# Willhaben E-Bike Roboter 🚲

Läuft komplett in der Cloud (GitHub Actions), der Laptop kann aus sein.

**Was er macht**
- Alle 30 Min: neue E-Bikes auf willhaben, nur Privatverkäufer, bis 1200 €, max. 80 km um 4493 Wolfern.
  - Defekt / Unfall / Bastler → `data/defekt_unfall.csv`
  - Bosch-Motor, ab Baujahr 2020, bis 1200 € → Wert schätzen
    - ≥30 % unter Wert → `data/deals.csv` + Bericht in `data/fahrraeder/` + automatisch auf die Watchlist
    - ≥45 % unter Wert → Push aufs Handy (ntfy)
- 3x täglich (6/12/18 Uhr): Watchlist prüfen (Preis, Beschreibung, Status: verfügbar/reserviert/verkauft/gelöscht) → `data/watchlist_log.csv`, `data/WATCHLIST.md`

**Anzeige auf die Watchlist setzen**
- In der ntfy-App den willhaben-Link an das Thema `wh-ebike-zzee721a6z-add` senden, oder
- `data/watchlist.txt` auf GitHub bearbeiten (eine Zeile pro Link).

**Wertschätzung**: Regelmodell (Bosch-Antriebslinie, Akku, Marke, Alter, Schaden) gemischt mit dem Median ähnlicher intakter Inserate auf willhaben. Nur ein Richtwert!

Der Roboter liest nur öffentliche Seiten. Er kauft nichts und kontaktiert niemanden.
