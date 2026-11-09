#!/usr/bin/env python3
"""
stacja_pogodowa.py — archiwum stacji Ecowitt/GoGEN + porównanie z IMGW + dashboard HTML.

Co robi jedno uruchomienie:
  1. Dociąga z chmury Ecowitt (API v3) wszystkie nowe pomiary i zapisuje je
     w lokalnej bazie dane/archiwum.sqlite (5 min, gdy są dostępne; starsze 30 min / 4 h).
     Pierwsze uruchomienie pobiera całą historię stacji (kilka–kilkanaście minut).
  2. Raz na dobę odświeża dane dobowe IMGW (stacja synoptyczna najbliższa Twojej)
     i liczy normy 1991–2020 (temperatura, opad, usłonecznienie).
  3. Generuje dashboard.html — otwierasz go w przeglądarce, działa bez internetu.

Wymagania:  Python 3.10+,  pip install requests pandas tzdata
Konfiguracja: konfiguracja.ini (w tym samym folderze).

Użycie:
  python stacja_pogodowa.py                  # normalna synchronizacja + dashboard
  python stacja_pogodowa.py --tylko-dashboard
  python stacja_pogodowa.py --pelne          # ponowne pobranie całej historii Ecowitt
  python stacja_pogodowa.py --od 2024-01-01  # historia od podanej daty (z --pelne)
  python stacja_pogodowa.py --imgw           # wymuś odświeżenie IMGW
"""

from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
import traceback
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

KATALOG = Path(__file__).resolve().parent
PLIK_CFG = KATALOG / "konfiguracja.ini"
DANE = KATALOG / "dane"
BAZA = DANE / "archiwum.sqlite"
CACHE_IMGW = DANE / "imgw_cache"
SZABLON = KATALOG / "szablon_dashboard.html"
WYJSCIE = KATALOG / "dashboard.html"
LOG = DANE / "log.txt"
KATALOG_WC = KATALOG / "weathercloud"

API = "https://api.ecowitt.net/api/v3"
CALL_BACK = "outdoor,indoor,solar_and_uvi,rainfall,rainfall_piezo,wind,pressure"
JEDNOSTKI = dict(temp_unitid=1, pressure_unitid=3, wind_speed_unitid=6,
                 rainfall_unitid=12, solar_irradiance_unitid=16)   # ℃, hPa, m/s, mm, W/m²
IMGW_SYNOP = ("https://danepubliczne.imgw.pl/data/dane_pomiarowo_obserwacyjne/"
              "dane_meteorologiczne/dobowe/synop/")
UA = {"User-Agent": "stacja-pogodowa-archiwum/1.0"}

# Stacje synoptyczne IMGW (przybliżone współrzędne) — do wyboru najbliższej.
SYNOP = {
    "BIAŁA PODLASKA": (52.04, 23.13), "BIAŁYSTOK": (53.11, 23.16), "BIELSKO-BIAŁA": (49.81, 19.00),
    "CHOJNICE": (53.72, 17.53), "CZĘSTOCHOWA": (50.81, 19.09), "ELBLĄG": (54.22, 19.43),
    "GDAŃSK": (54.33, 18.93), "GORZÓW WIELKOPOLSKI": (52.74, 15.28), "HEL": (54.60, 18.81),
    "JELENIA GÓRA": (50.90, 15.79), "KALISZ": (51.78, 18.08), "KATOWICE": (50.24, 19.03),
    "KĘTRZYN": (54.07, 21.37), "KIELCE": (50.81, 20.69), "KŁODZKO": (50.44, 16.61),
    "KOŁO": (52.20, 18.66), "KOŁOBRZEG": (54.16, 15.39), "KOSZALIN": (54.20, 16.16),
    "KRAKÓW": (50.08, 19.80), "KROSNO": (49.71, 21.77), "LEGNICA": (51.19, 16.21),
    "LESKO": (49.47, 22.34), "LESZNO": (51.84, 16.53), "LĘBORK": (54.55, 17.75),
    "LUBLIN": (51.22, 22.39), "ŁEBA": (54.75, 17.53), "ŁÓDŹ": (51.72, 19.40),
    "MIKOŁAJKI": (53.79, 21.59), "MŁAWA": (53.10, 20.36), "NOWY SĄCZ": (49.63, 20.69),
    "OLSZTYN": (53.77, 20.42), "OPOLE": (50.63, 17.97), "OSTROŁĘKA": (53.09, 21.56),
    "PIŁA": (53.13, 16.75), "PŁOCK": (52.59, 19.73), "POZNAŃ": (52.42, 16.83),
    "PRZEMYŚL": (49.81, 22.77), "RACIBÓRZ": (50.06, 18.19), "RESKO": (53.77, 15.42),
    "RZESZÓW": (50.11, 22.04), "SANDOMIERZ": (50.70, 21.72), "SIEDLCE": (52.18, 22.25),
    "SŁUBICE": (52.35, 14.60), "SULEJÓW": (51.35, 19.87), "SUWAŁKI": (54.13, 22.95),
    "SZCZECIN": (53.40, 14.62), "SZCZECINEK": (53.71, 16.69), "ŚWINOUJŚCIE": (53.92, 14.24),
    "TARNÓW": (50.03, 20.98), "TERESPOL": (52.08, 23.62), "TORUŃ": (53.04, 18.60),
    "USTKA": (54.59, 16.85), "WARSZAWA": (52.16, 20.96), "WIELUŃ": (51.21, 18.56),
    "WŁODAWA": (51.55, 23.53), "WROCŁAW": (51.10, 16.90), "ZAKOPANE": (49.29, 19.96),
    "ZAMOŚĆ": (50.70, 23.20), "ZIELONA GÓRA": (51.93, 15.53),
}

# ───────────────────────── narzędzia ─────────────────────────

def log(msg: str) -> None:
    linia = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(linia, flush=True)
    try:
        DANE.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(linia + "\n")
    except OSError:
        pass


def norm_nazwa(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("Ł", "L").replace("ł", "l"))
    return "".join(c for c in s if not unicodedata.combining(c)).upper().strip()


def odleglosc_km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


class Konfig:
    def __init__(self) -> None:
        c = configparser.ConfigParser()
        if PLIK_CFG.exists():
            c.read(PLIK_CFG, encoding="utf-8")
        elif not os.getenv("ECOWITT_API_KEY"):
            sys.exit(f"Brak pliku {PLIK_CFG}")
        g = lambda s, k, d="": c.get(s, k, fallback=d).strip()
        # zmienne środowiskowe (np. sekrety GitHub Actions) mają pierwszeństwo przed plikiem
        self.app_key = os.getenv("ECOWITT_APP_KEY") or g("ecowitt", "application_key")
        self.api_key = os.getenv("ECOWITT_API_KEY") or g("ecowitt", "api_key")
        self.mac = os.getenv("ECOWITT_MAC") or g("ecowitt", "mac")
        self.nazwa = os.getenv("STACJA_NAZWA") or g("stacja", "nazwa", "Moja stacja")
        self.tz = g("stacja", "strefa", "Europe/Warsaw")
        self.lat = float(g("stacja", "szerokosc") or "nan")
        self.lon = float(g("stacja", "dlugosc") or "nan")
        self.imgw = g("imgw", "stacja_synop", "auto")
        self.prog_slonca = float(g("opcje", "prog_slonca", "0.5"))
        self.pauza = float(g("opcje", "pauza_s", "2"))
        self.wc_wilgotnosc = g("weathercloud", "uwzglednij_wilgotnosc", "nie").lower() in ("tak", "1", "yes")
        self.wc_nazwa = g("weathercloud", "nazwa", "VEVOR")
        self.plik_opadow = g("opad_reczny", "plik", "")
        if not (self.app_key and self.api_key and self.mac):
            sys.exit("Uzupełnij application_key, api_key i mac w konfiguracja.ini")


# ───────────────────────── baza ─────────────────────────

def baza() -> sqlite3.Connection:
    DANE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(BAZA)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS obs(metric TEXT, ts INTEGER, value REAL, step INTEGER,
                                       PRIMARY KEY(metric, ts)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS opad_reczny(rok INTEGER, mies INTEGER, mm REAL, PRIMARY KEY(rok, mies));
        CREATE TABLE IF NOT EXISTS imgw(d TEXT PRIMARY KEY, tmax REAL, tmin REAL, tavg REAL,
                                        rain REAL, sun REAL);
    """)
    return con


def meta_get(con, k, d=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else d


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
    con.commit()


# ───────────────────────── Ecowitt ─────────────────────────

def ecowitt(cfg: Konfig, endpoint: str, **p) -> dict:
    p.update(application_key=cfg.app_key, api_key=cfg.api_key)
    for proba in range(7):
        try:
            r = requests.get(f"{API}/{endpoint}", params=p, timeout=90, headers=UA)
            js = r.json()
        except (requests.RequestException, ValueError) as e:
            log(f"  ! sieć: {e} — ponawiam")
            time.sleep(10 * (proba + 1))
            continue
        kod, msg = js.get("code"), str(js.get("msg", ""))
        if kod == 0:
            d = js.get("data")
            return d if isinstance(d, dict) else {}
        if kod == -1 or "limit" in msg.lower():
            czekaj = 60 + 30 * proba
            log(f"  ! limit zapytań API ({msg}) — czekam {czekaj} s")
            time.sleep(czekaj)
            continue
        raise RuntimeError(f"Ecowitt API {endpoint}: code={kod} msg={msg}")
    raise RuntimeError(f"Ecowitt API {endpoint}: wyczerpane próby")


def splaszcz(d: dict, pre: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        if isinstance(v.get("list"), dict):
            out[pre + k] = (v.get("unit", ""), v["list"])
        else:
            out.update(splaszcz(v, pre + k + "."))
    return out


KROK = {"5min": 300, "30min": 1800, "4hour": 14400}


def pobierz(cfg: Konfig, con, start: datetime, koniec: datetime, cykl: str, porcja: timedelta) -> int:
    tz = ZoneInfo(cfg.tz)
    lok = lambda t: t.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
    t, suma, puste = start, 0, 0
    while t < koniec:
        t2 = min(t + porcja, koniec)
        data = ecowitt(cfg, "device/history", mac=cfg.mac, start_date=lok(t), end_date=lok(t2),
                       cycle_type=cykl, call_back=CALL_BACK, **JEDNOSTKI)
        plaskie = splaszcz(data)
        znaczniki = sorted({int(ts) for _, (_, l) in plaskie.items() for ts in l})
        krok = KROK[cykl]
        if len(znaczniki) > 3:
            med = float(np.median(np.diff(znaczniki)))
            krok = min(KROK.values(), key=lambda k: abs(k - med))
        wiersze = []
        for m, (jedn, lista) in plaskie.items():
            if jedn:
                con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"unit:{m}", json.dumps(jedn)))
            for ts, v in lista.items():
                try:
                    wiersze.append((m, int(ts), float(v), krok))
                except (TypeError, ValueError):
                    pass
        con.executemany("""INSERT INTO obs(metric,ts,value,step) VALUES(?,?,?,?)
                           ON CONFLICT(metric,ts) DO UPDATE SET value=excluded.value, step=excluded.step
                           WHERE excluded.step <= obs.step""", wiersze)
        con.commit()
        suma += len(znaczniki)
        puste = puste + 1 if not znaczniki else 0
        log(f"  {cykl:>5} {t.astimezone(tz):%Y-%m-%d %H:%M} → {t2.astimezone(tz):%Y-%m-%d %H:%M}: "
            f"{len(znaczniki)} pomiarów (co {krok // 60} min)")
        t = t2
        time.sleep(cfg.pauza)
    return suma


def synchronizuj_ecowitt(cfg: Konfig, con, pelne: bool, od: str | None) -> None:
    teraz = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    try:
        info = ecowitt(cfg, "device/info", mac=cfg.mac)
        if info:
            meta_set(con, "device_info", {k: v for k, v in info.items() if not isinstance(v, (dict, list))})
    except Exception as e:  # informacje o urządzeniu nie są niezbędne
        log(f"  ! device/info: {e}")
    info = meta_get(con, "device_info", {}) or {}

    ost = con.execute("SELECT MAX(ts) FROM obs WHERE step<=300").fetchone()[0]
    pusto = con.execute("SELECT COUNT(*) FROM obs").fetchone()[0] == 0
    start5 = teraz - timedelta(days=89)
    if ost and not pelne:
        start5 = max(start5, datetime.fromtimestamp(ost, timezone.utc) - timedelta(minutes=30))
    log(f"Ecowitt: dane 5-min od {start5:%Y-%m-%d %H:%M} UTC")
    pobierz(cfg, con, start5, teraz, "5min", timedelta(days=1))

    if pusto or pelne:
        s30 = teraz - timedelta(days=364)
        log("Ecowitt: historia 30-min (ostatni rok)")
        pobierz(cfg, con, s30, teraz - timedelta(days=89), "30min", timedelta(days=7))
        if od:
            poczatek = datetime.fromisoformat(od).replace(tzinfo=timezone.utc)
        elif str(info.get("createtime", "")).isdigit():
            poczatek = datetime.fromtimestamp(int(info["createtime"]), timezone.utc)
        else:
            poczatek = teraz - timedelta(days=3 * 365)
        if poczatek < s30:
            log(f"Ecowitt: historia 4-godzinna od {poczatek:%Y-%m-%d}")
            pobierz(cfg, con, poczatek, s30, "4hour", timedelta(days=30))
    meta_set(con, "sync_ecowitt", int(time.time()))


# ───────────────────────── IMGW ─────────────────────────

def http(url: str, binarnie=False):
    for proba in range(4):
        try:
            r = requests.get(url, timeout=120, headers=UA)
            r.raise_for_status()
            return r.content if binarnie else r.text
        except requests.RequestException as e:
            log(f"  ! IMGW {url}: {e}")
            time.sleep(5 * (proba + 1))
    raise RuntimeError(f"Nie udało się pobrać {url}")


def linki(url: str) -> list[str]:
    html = http(url)
    out = []
    for h in re.findall(r'href="([^"]+)"', html):
        if h.startswith(("?", "/", "http", "..")):
            continue
        out.append(h)
    return out


def wybierz_stacje_imgw(cfg: Konfig, con) -> str:
    if cfg.imgw and cfg.imgw.lower() != "auto":
        return cfg.imgw
    lat, lon = cfg.lat, cfg.lon
    info = meta_get(con, "device_info", {}) or {}
    if math.isnan(lat) and info.get("latitude"):
        lat, lon = float(info["latitude"]), float(info["longitude"])
    if math.isnan(lat):
        log("  ! brak współrzędnych stacji — używam SZCZECIN (ustaw [stacja] szerokosc/dlugosc)")
        return "SZCZECIN"
    nazwa = min(SYNOP, key=lambda n: odleglosc_km((lat, lon), SYNOP[n]))
    log(f"IMGW: najbliższa stacja synoptyczna {nazwa} "
        f"({odleglosc_km((lat, lon), SYNOP[nazwa]):.0f} km)")
    return nazwa


def _f(v: str, status: str, zero_gdy_9=False):
    status = (status or "").strip()
    if status == "8":
        return None
    if status == "9" and zero_gdy_9:
        return 0.0
    try:
        return float(v.replace(",", "."))
    except ValueError:
        return None


def synchronizuj_imgw(cfg: Konfig, con, wymus=False) -> None:
    ost = meta_get(con, "sync_imgw", 0)
    stacja = wybierz_stacje_imgw(cfg, con)
    if not wymus and time.time() - ost < 20 * 3600 and meta_get(con, "imgw_stacja") == stacja:
        return
    klucz = norm_nazwa(stacja)
    log(f"IMGW: pobieram dane dobowe synop dla {stacja}")
    CACHE_IMGW.mkdir(parents=True, exist_ok=True)
    foldery = [f for f in linki(IMGW_SYNOP) if f.endswith("/")]
    foldery.sort(key=lambda f: [int(x) for x in re.findall(r"\d{4}", f)] or [0], reverse=True)
    rok_teraz = date.today().year
    kod3 = None
    wiersze: dict[str, tuple] = {}
    for f in foldery:
        lata = [int(x) for x in re.findall(r"\d{4}", f)]
        if not lata or max(lata) < 1991:
            continue
        zipy = [z for z in linki(IMGW_SYNOP + f) if z.lower().endswith(".zip")]
        stacyjne = [z for z in zipy if re.search(r"_(\d{3})_s\.zip$", z)]
        if kod3 and stacyjne:
            pasujace = [z for z in stacyjne if z.endswith(f"_{kod3}_s.zip")]
            if pasujace:
                zipy = pasujace
        swiezy = max(lata) >= rok_teraz - 1
        for z in zipy:
            lok = CACHE_IMGW / f.strip("/") / z
            if not lok.exists() or (swiezy and time.time() - lok.stat().st_mtime > 6 * 86400):
                lok.parent.mkdir(parents=True, exist_ok=True)
                lok.write_bytes(http(IMGW_SYNOP + f + z, binarnie=True))
                time.sleep(0.3)
            try:
                zf = zipfile.ZipFile(lok)
            except zipfile.BadZipFile:
                lok.unlink(missing_ok=True)
                continue
            for nazwa_pliku in zf.namelist():
                n = Path(nazwa_pliku).name.lower()
                if not n.startswith("s_d_") or n.startswith("s_d_t"):
                    continue
                tekst = zf.read(nazwa_pliku).decode("cp1250", errors="replace")
                for c in csv.reader(io.StringIO(tekst)):
                    if len(c) < 22 or norm_nazwa(c[1]) != klucz and not norm_nazwa(c[1]).startswith(klucz + "-"):
                        continue
                    kod3 = kod3 or c[0].strip()[-3:]
                    try:
                        d = date(int(c[2]), int(c[3]), int(c[4])).isoformat()
                    except ValueError:
                        continue
                    wiersze[d] = (d, _f(c[5], c[6]), _f(c[7], c[8]), _f(c[9], c[10]),
                                  _f(c[13], c[14], True), _f(c[20], c[21], True))
    if not wiersze:
        raise RuntimeError(f"IMGW: nie znaleziono danych dla stacji '{stacja}'")
    # kontrola formatu: średnia powinna leżeć między min a max
    ok = [w for w in wiersze.values() if None not in (w[1], w[2], w[3])]
    zle = sum(1 for w in ok if not (w[2] - 0.5 <= w[3] <= w[1] + 0.5))
    if ok and zle / len(ok) > 0.05:
        log(f"  ! IMGW: podejrzany układ kolumn ({zle}/{len(ok)} dób z Tśr poza Tmin–Tmax)")
    con.execute("DELETE FROM imgw")
    con.executemany("INSERT INTO imgw VALUES(?,?,?,?,?,?)", sorted(wiersze.values()))
    con.commit()
    meta_set(con, "imgw_stacja", stacja)
    meta_set(con, "sync_imgw", int(time.time()))
    log(f"IMGW: {len(wiersze)} dób ({min(wiersze)} – {max(wiersze)})")


# ───────────────────────── Weathercloud (poprzednia stacja) ─────────────────────────

WC_KOLUMNY = [  # (początek nagłówka, metryka w archiwum, mnożnik)
    ("Temperature (", "outdoor.temperature", 1), ("Inside temperature (", "indoor.temperature", 1),
    ("Inside humidity (", "indoor.humidity", 1), ("Humidity (", "outdoor.humidity", 1),
    ("Dew point (", "outdoor.dew_point", 1), ("Average wind speed (km/h)", "wind.wind_speed", 1 / 3.6),
    ("Average wind speed (m/s)", "wind.wind_speed", 1), ("Wind gust (km/h)", "wind.wind_gust", 1 / 3.6),
    ("Wind gust (m/s)", "wind.wind_gust", 1), ("Average wind direction", "wind.wind_direction", 1),
    ("Atmospheric pressure (hPa)", "pressure.relative", 1), ("Rain (mm)", "rainfall.daily", 1),
    ("Rain rate (mm/h)", "rainfall.rain_rate", 1), ("Solar radiation (W/m", "solar_and_uvi.solar", 1),
    ("UV index", "solar_and_uvi.uvi", 1),
]
KROK_WC = 600


def czytaj_tekst(p: Path) -> str:
    raw = p.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:200]:
        enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-16-le" if raw[1:2] == b"\x00" else "utf-16-be")
        return raw.decode(enc).lstrip("﻿")
    return raw.decode("utf-8-sig", errors="replace")


def _serie_stale(pary: list[tuple[int, float]], min_n: int) -> set[int]:
    """Znaczniki czasu w ciągach >= min_n identycznych, niezerowych wartości."""
    out, run = set(), []
    for ts, v in sorted(pary):
        if run and v == run[-1][1]:
            run.append((ts, v))
        else:
            if len(run) >= min_n and run[0][1] != 0:
                out.update(t for t, _ in run)
            run = [(ts, v)]
    if len(run) >= min_n and run and run[0][1] != 0:
        out.update(t for t, _ in run)
    return out


def odfiltruj_zamrozone(dane: list[tuple]) -> tuple[list[tuple], int]:
    """Usuwa okresy, w których stacja wysyłała stale te same wartości (zawieszenie konsoli,
    zamarznięty wiatrowskaz). Temperatura stała >= 6 h -> odrzucane wszystkie pomiary z tego czasu;
    kierunek wiatru stały >= 12 h -> odrzucany tylko kierunek."""
    temp = [(t, v) for m, t, v, _ in dane if m == "outdoor.temperature"]
    zle = _serie_stale(temp, 36)
    kier = _serie_stale([(t, v) for m, t, v, _ in dane if m == "wind.wind_direction"], 72)
    wynik = [w for w in dane if w[1] not in zle and not (w[0] == "wind.wind_direction" and w[1] in kier)]
    return wynik, len(dane) - len(wynik)


def importuj_weathercloud(cfg: Konfig, con) -> None:
    pliki = sorted(KATALOG_WC.glob("*.csv")) if KATALOG_WC.exists() else []
    if not pliki:
        return
    zrobione = meta_get(con, "wc_pliki", {}) or {}
    granica = con.execute("SELECT MIN(ts) FROM obs WHERE step<=300").fetchone()[0] or 10 ** 12
    tz = ZoneInfo(cfg.tz)
    pomin = set() if cfg.wc_wilgotnosc else {"outdoor.humidity", "outdoor.dew_point"}
    for p in pliki:
        podpis = hashlib.sha1(p.read_bytes()).hexdigest()
        if zrobione.get(p.name) == podpis:
            continue
        wiersze = list(csv.reader(io.StringIO(czytaj_tekst(p)), delimiter=";"))
        if len(wiersze) < 2:
            continue
        nagl = [h.strip() for h in wiersze[0]]
        mapa = []
        for i, h in enumerate(nagl):
            for pref, metr, mn in WC_KOLUMNY:
                if h.startswith(pref) and metr not in pomin and metr not in [m for _, m, _ in mapa]:
                    mapa.append((i, metr, mn))
                    break
        dane, ts_min, ts_max = [], None, None
        for w in wiersze[1:]:
            if not w or not w[0].strip():
                continue
            try:
                t = int(datetime.strptime(w[0].strip(), "%d/%m/%Y %H:%M:%S").replace(tzinfo=tz).timestamp())
            except ValueError:
                try:
                    t = int(datetime.strptime(w[0].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp())
                except ValueError:
                    continue
            if t >= granica:
                continue
            for i, metr, mn in mapa:
                if i < len(w) and w[i].strip():
                    try:
                        dane.append((metr, t, round(float(w[i].replace(",", "")) * mn, 2), KROK_WC))
                    except ValueError:
                        pass
            ts_min = t if ts_min is None else min(ts_min, t)
            ts_max = t if ts_max is None else max(ts_max, t)
        dane, odrzucone = odfiltruj_zamrozone(dane)
        con.executemany("""INSERT INTO obs(metric,ts,value,step) VALUES(?,?,?,?)
                           ON CONFLICT(metric,ts) DO UPDATE SET value=excluded.value, step=excluded.step
                           WHERE excluded.step <= obs.step""", dane)
        con.commit()
        if odrzucone:
            log(f"Weathercloud: {p.name} — pominięto {odrzucone} wartości z okresów „zamrożonych” odczytów")
        zrobione[p.name] = podpis
        zakres = meta_get(con, "wc_zakres", [None, None]) or [None, None]
        if ts_min:
            zakres = [min(x for x in (zakres[0], ts_min) if x), max(x for x in (zakres[1], ts_max) if x)]
        meta_set(con, "wc_zakres", zakres)
        log(f"Weathercloud: {p.name} — {len(dane)} wartości")
    meta_set(con, "wc_pliki", zrobione)


# ───────────────────────── opad z deszczomierza ręcznego (xlsx) ─────────────────────────

MIESIACE_PL = ["styczen", "luty", "marzec", "kwiecien", "maj", "czerwiec", "lipiec", "sierpien",
               "wrzesien", "pazdziernik", "listopad", "grudzien"]


def czytaj_xlsx(p: Path) -> list[list]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    z = zipfile.ZipFile(p)
    wspolne = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            wspolne.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
    arkusz = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))[0]
    wiersze: dict[int, dict[int, object]] = {}
    for c in ET.fromstring(z.read(arkusz)).iter(f"{{{ns['m']}}}c"):
        ref = c.get("r", "")
        m = re.match(r"([A-Z]+)(\d+)", ref)
        if not m:
            continue
        kol = 0
        for ch in m.group(1):
            kol = kol * 26 + ord(ch) - 64
        v = c.find("m:v", ns)
        t = c.get("t")
        if t == "inlineStr":
            val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
        elif v is None or v.text is None:
            continue
        elif t == "s":
            val = wspolne[int(v.text)]
        elif t in ("str", "e", "b"):
            val = v.text
        else:
            try:
                val = float(v.text)
            except ValueError:
                val = v.text
        wiersze.setdefault(int(m.group(2)), {})[kol - 1] = val
    return [[r.get(i) for i in range(max(r) + 1)] for _, r in sorted(wiersze.items())]


def importuj_opad_reczny(cfg: Konfig, con) -> None:
    p = KATALOG / cfg.plik_opadow if cfg.plik_opadow else None
    if not p or not p.exists():
        kand = [x for x in KATALOG.glob("*.xlsx") if "opad" in norm_nazwa(x.name).lower()]
        p = kand[0] if kand else None
    if not p:
        return
    podpis = f"{p.name}-{hashlib.sha1(p.read_bytes()).hexdigest()}"
    if meta_get(con, "opad_reczny_plik") == podpis:
        return
    wiersze = czytaj_xlsx(p)
    kol_lat = {}
    for w in wiersze:
        lata = {i: int(v) for i, v in enumerate(w) if isinstance(v, (int, float)) and 1990 <= v <= 2100 and float(v).is_integer()}
        if len(lata) >= 2:
            kol_lat = lata
            break
    dane = []
    for w in wiersze:
        if not w or not isinstance(w[0], str):
            continue
        nazwa = norm_nazwa(w[0]).lower()
        if nazwa not in MIESIACE_PL:
            continue
        mies = MIESIACE_PL.index(nazwa) + 1
        for i, rok in kol_lat.items():
            v = w[i] if i < len(w) else None
            if isinstance(v, (int, float)):
                dane.append((rok, mies, float(v)))
    con.execute("DELETE FROM opad_reczny")
    con.executemany("INSERT INTO opad_reczny VALUES(?,?,?)", dane)
    con.commit()
    meta_set(con, "opad_reczny_plik", podpis)
    log(f"Opad ręczny: {p.name} — {len(dane)} miesięcy")


# ───────────────────────── obliczenia ─────────────────────────

def sin_wysokosci(ts: np.ndarray, lat: float, lon: float) -> np.ndarray:
    d = (ts - 946728000) / 86400.0
    g = np.radians(357.529 + 0.98560028 * d)
    q = 280.459 + 0.98564736 * d
    L = np.radians(q + 1.915 * np.sin(g) + 0.020 * np.sin(2 * g))
    e = np.radians(23.439 - 0.00000036 * d)
    dek = np.arcsin(np.sin(e) * np.sin(L))
    ra = np.arctan2(np.cos(e) * np.sin(L), np.cos(L))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = np.radians(gmst * 15 + lon) - ra
    la = math.radians(lat)
    return np.sin(la) * np.sin(dek) + np.cos(la) * np.cos(dek) * np.cos(ha)


def idx366(d: date) -> int:
    return (date(2000, d.month, d.day) - date(2000, 1, 1)).days


def wygladz(a: np.ndarray, okno: int) -> np.ndarray:
    h = okno // 2
    ext = np.concatenate([a[-h:], a, a[:h]])
    out = np.full(len(a), np.nan)
    for i in range(len(a)):
        w = ext[i:i + okno]
        if np.isfinite(w).any():
            out[i] = np.nanmean(w)
    return out


def normy(con) -> dict | None:
    df = pd.read_sql("SELECT * FROM imgw", con)
    if df.empty:
        return None
    df["d"] = pd.to_datetime(df["d"])
    base = df[(df.d.dt.year >= 1991) & (df.d.dt.year <= 2020)].copy()
    if base.empty:
        return None
    base["i"] = [idx366(x.date()) for x in base.d]
    wynik = {"lata": [int(base.d.dt.year.min()), int(base.d.dt.year.max())]}
    for kol, nazwa in (("tavg", "t"), ("rain", "rain"), ("sun", "sun")):
        sr = base.groupby("i")[kol].mean().reindex(range(366)).to_numpy(dtype=float, copy=True)
        if nazwa == "rain" or nazwa == "sun":
            sr[59] = np.nanmean([sr[58], sr[60]])  # 29 lutego jak sąsiednie doby
        wynik[nazwa] = wygladz(sr, 31)
    # zakres typowy (10–90 percentyl) z okna ±7 dób
    grupy = {i: g.dropna().to_numpy() for i, g in base.groupby("i")["tavg"]}
    p10, p90 = np.full(366, np.nan), np.full(366, np.nan)
    for i in range(366):
        w = np.concatenate([grupy.get((i + k) % 366, np.array([])) for k in range(-7, 8)])
        if len(w) > 20:
            p10[i], p90[i] = np.percentile(w, [10, 90])
    wynik["p10"], wynik["p90"] = wygladz(p10, 15), wygladz(p90, 15)
    return {k: (np.round(v, 2).tolist() if isinstance(v, np.ndarray) else v) for k, v in wynik.items()}


MAPA = {  # nazwa w dashboardzie: (kandydaci w archiwum Ecowitt)
    "temp": ["outdoor.temperature"], "feels": ["outdoor.feels_like", "outdoor.app_temp"],
    "hum": ["outdoor.humidity"], "dew": ["outdoor.dew_point"],
    "press": ["pressure.relative", "pressure.absolute"],
    "wind": ["wind.wind_speed"], "gust": ["wind.wind_gust"],
    "dir": ["wind.wind_direction", "wind.10_minute_average_wind_direction"],
    "rr": ["{r}.rain_rate"], "rd": ["{r}.daily"], "rm": ["{r}.monthly"], "ry": ["{r}.yearly"],
    "solar": ["solar_and_uvi.solar"], "uvi": ["solar_and_uvi.uvi"],
    "tin": ["indoor.temperature"], "hin": ["indoor.humidity"],
}
AGREG_30 = {"gust": "max", "rr": "max", "rd": "max", "rm": "max", "ry": "max", "uvi": "max"}


def wczytaj(con) -> pd.DataFrame:
    df = pd.read_sql("SELECT metric, ts, value, step FROM obs", con)
    if df.empty:
        return df
    deszcz = "rainfall" if (df.metric.str.startswith("rainfall.")).any() else "rainfall_piezo"
    obecne = set(df.metric.unique())
    odwz = {}
    for k, kand in MAPA.items():
        for m in kand:
            m = m.format(r=deszcz)
            if m in obecne:
                odwz[m] = k
                break
    df = df[df.metric.isin(odwz)].copy()
    df["k"] = df.metric.map(odwz)
    return df


def kolumnowo(wide: pd.DataFrame, nd=1) -> dict:
    out = {"t": [int(x) for x in wide.index]}
    precyzja = {"press": 1, "solar": 0, "hum": 0, "hin": 0, "dir": 0, "uvi": 1, "rd": 1, "rr": 1}
    for k in MAPA:
        if k in wide:
            v = wide[k].round(precyzja.get(k, nd))
            out[k] = [None if pd.isna(x) else (int(x) if precyzja.get(k) == 0 else float(x)) for x in v]
    return out


def kierunek_sredni(kat: pd.Series) -> float:
    r = np.radians(kat.dropna())
    if r.empty:
        return np.nan
    return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())) % 360)


def zbuduj_dane(cfg: Konfig, con) -> dict:
    tz = ZoneInfo(cfg.tz)
    df = wczytaj(con)
    info = meta_get(con, "device_info", {}) or {}
    lat = cfg.lat if not math.isnan(cfg.lat) else float(info.get("latitude") or 53.4)
    lon = cfg.lon if not math.isnan(cfg.lon) else float(info.get("longitude") or 14.6)
    teraz = int(time.time())
    wynik = {"meta": {"nazwa": cfg.nazwa, "tz": cfg.tz, "lat": lat, "lon": lon,
                      "wygenerowano": teraz, "sync_ecowitt": meta_get(con, "sync_ecowitt"),
                      "sync_imgw": meta_get(con, "sync_imgw"), "imgw_stacja": meta_get(con, "imgw_stacja"),
                      "bledy": meta_get(con, "bledy", []), "prog_slonca": cfg.prog_slonca}}
    if df.empty:
        wynik.update(fine={"t": []}, mid={"t": []}, daily={"d": []}, now={})
        return wynik

    ost_ts = int(df.ts.max())
    wynik["meta"]["ostatni_ts"] = ost_ts

    # — warstwa 5-min (ostatnie 14 dni)
    f = df[(df.step <= 300) & (df.ts >= ost_ts - 14 * 86400)]
    fine = f.pivot_table(index="ts", columns="k", values="value", aggfunc="mean")
    wynik["fine"] = kolumnowo(fine)

    # — warstwa 30-min (ostatnie 400 dni)
    m = df[df.ts >= ost_ts - 400 * 86400].copy()
    m["b"] = (m.ts // 1800) * 1800
    czesci = []
    for k, g in m.groupby("k"):
        if k == "dir":
            s = g.groupby("b")["value"].apply(kierunek_sredni)
        else:
            s = g.groupby("b")["value"].agg(AGREG_30.get(k, "mean"))
        czesci.append(s.rename(k))
    mid = pd.concat(czesci, axis=1).sort_index() if czesci else pd.DataFrame()
    wynik["mid"] = kolumnowo(mid)

    # — warstwa dobowa (całe archiwum), z najdokładniejszej rozdzielczości danej doby
    df["dzien"] = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert(tz).dt.date
    df = df[df.step == df.groupby(["k", "dzien"]).step.transform("min")]
    g = df.groupby(["dzien", "k"])["value"]
    sr, mn, mx, n = (g.mean().unstack(), g.min().unstack(), g.max().unstack(), g.count().unstack())
    krok = df.groupby(["dzien", "k"]).step.min().unstack()
    dni = sr.index
    D = pd.DataFrame(index=dni)
    pokrycie = (n.get("temp") * krok.get("temp") / 86400) if "temp" in n else pd.Series(0, index=dni)
    ok = pokrycie >= 0.6
    for k in ("temp", "feels", "hum", "dew", "press", "wind", "tin", "hin"):
        if k in sr:
            D[{"temp": "tavg"}.get(k, k)] = sr[k].where(ok) if k == "temp" else sr[k]
    if "temp" in mn:
        D["tmin"], D["tmax"] = mn["temp"].where(ok), mx["temp"].where(ok)
    if "gust" in mx:
        D["gust"] = mx["gust"]
    if "uvi" in mx:
        D["uvi"] = mx["uvi"]
    if "dir" in sr:
        D["dir"] = df[df.k == "dir"].groupby("dzien")["value"].apply(kierunek_sredni)
    if "solar" in sr:
        pok_s = n["solar"] * krok["solar"] / 86400
        D["energy"] = (sr["solar"] * 24 / 1000).where(pok_s >= 0.8)
        D["solarmax"] = mx["solar"]
        s = df[(df.k == "solar") & (df.step <= 1800)].copy()
        if not s.empty:
            sh = sin_wysokosci(s.ts.to_numpy(dtype=float), lat, lon)
            czyste = np.where(sh > 0.05, 1098 * sh * np.exp(-0.057 / np.maximum(sh, 0.05)), np.nan)
            s["slonce"] = (s.value.to_numpy() >= cfg.prog_slonca * czyste) & (sh > math.sin(math.radians(3)))
            s["h"] = s.slonce * s.step / 3600
            godz = s.groupby("dzien")["h"].sum()
            D["sun"] = godz.where(pok_s.reindex(godz.index) >= 0.8)
    # opad dobowy: licznik dzienny (dokładne dane) albo przyrost licznika rocznego (dane 4-h)
    deszcz = pd.Series(np.nan, index=dni)
    if "rd" in mx:
        deszcz = mx["rd"].where(krok["rd"] <= 1800)
    if "ry" in mx:
        ost_r = df[df.k == "ry"].sort_values("ts").groupby("dzien")["value"].last()
        poprz = ost_r.shift(1)
        nowy_rok = pd.Series([d.month == 1 and d.day == 1 for d in ost_r.index], index=ost_r.index)
        przyrost = (ost_r - poprz).where(~nowy_rok, ost_r)
        przyrost = przyrost.where(przyrost >= 0, ost_r)
        deszcz = deszcz.fillna(przyrost.reindex(dni))
    D["rain"] = deszcz
    D["step"] = krok.min(axis=1)
    D = D.sort_index()
    wynik["daily"] = {"d": [d.isoformat() for d in D.index]}
    precyzja = {"press": 1, "hum": 0, "hin": 0, "dir": 0, "energy": 2, "solarmax": 0, "step": 0}
    for k in D.columns:
        v = D[k].astype(float).round(precyzja.get(k, 1))
        wynik["daily"][k] = [None if pd.isna(x) else (int(x) if precyzja.get(k) == 0 else float(x)) for x in v]

    # — bieżące wartości
    teraz_w = {}
    ostatnie = df.sort_values("ts").groupby("k").tail(1).set_index("k")
    for k, w in ostatnie.iterrows():
        teraz_w[k] = {"v": float(w.value), "ts": int(w.ts)}
    if "press" in teraz_w:
        p = df[(df.k == "press") & (df.ts <= teraz_w["press"]["ts"] - 3 * 3600 + 900)]
        if not p.empty and teraz_w["press"]["ts"] - p.ts.max() < 4 * 3600:
            teraz_w["press3h"] = float(teraz_w["press"]["v"] - p.sort_values("ts").value.iloc[-1])
    wynik["now"] = teraz_w

    # — IMGW + normy
    im = pd.read_sql("SELECT * FROM imgw", con)
    if not im.empty:
        start = (D.index.min() - timedelta(days=400)).isoformat()
        im = im[im.d >= start]
        wynik["imgw"] = {k: [None if pd.isna(x) else round(float(x), 1) for x in im[k]] for k in
                         ("tavg", "rain", "sun")}
        wynik["imgw"]["d"] = im.d.tolist()
        wynik["meta"]["imgw_do"] = im.d.max() if not im.empty else None
    wynik["normy"] = normy(con)
    wynik["reczny"] = [list(r) for r in con.execute("SELECT rok, mies, mm FROM opad_reczny ORDER BY rok, mies")]
    wc = meta_get(con, "wc_zakres")
    if wc and wc[0]:
        wynik["meta"]["zrodla"] = [{"nazwa": cfg.wc_nazwa + " (Weathercloud, co 10 min)", "od": wc[0], "do": wc[1]},
                                   {"nazwa": cfg.nazwa + " (Ecowitt)", "od": int(df.ts[df.step <= 300].min()) if (df.step <= 300).any() else None, "do": ost_ts}]
    return wynik


def zapisz_dashboard(dane: dict) -> None:
    szablon = SZABLON.read_text(encoding="utf-8")
    js = json.dumps(dane, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    html = szablon.replace("/*__DANE__*/null", js.replace("</", "<\\/"))
    tmp = WYJSCIE.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(WYJSCIE)
    log(f"Dashboard: {WYJSCIE} ({len(html) / 1e6:.1f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tylko-dashboard", action="store_true")
    ap.add_argument("--pelne", action="store_true", help="pobierz ponownie całą historię")
    ap.add_argument("--od", help="początek historii RRRR-MM-DD (z --pelne lub przy pierwszym uruchomieniu)")
    ap.add_argument("--imgw", action="store_true", help="wymuś odświeżenie danych IMGW")
    ap.add_argument("--publiczny", action="store_true", help="bez współrzędnych stacji w dashboardzie")
    ap.add_argument("--wyjscie", help="ścieżka pliku dashboardu (domyślnie dashboard.html)")
    a = ap.parse_args()

    cfg = Konfig()
    con = baza()
    bledy = []
    if not a.tylko_dashboard:
        try:
            synchronizuj_ecowitt(cfg, con, a.pelne, a.od)
        except Exception as e:
            bledy.append(f"Ecowitt: {e}")
            log("BŁĄD Ecowitt:\n" + traceback.format_exc())
        try:
            synchronizuj_imgw(cfg, con, a.imgw)
        except Exception as e:
            bledy.append(f"IMGW: {e}")
            log("BŁĄD IMGW:\n" + traceback.format_exc())
        meta_set(con, "bledy", bledy)
    for nazwa, fn in (("Weathercloud", importuj_weathercloud), ("Opad ręczny", importuj_opad_reczny)):
        try:
            fn(cfg, con)
        except Exception as e:
            bledy.append(f"{nazwa}: {e}")
            log(f"BŁĄD {nazwa}:\n" + traceback.format_exc())
    meta_set(con, "bledy", bledy)
    try:
        if not SZABLON.exists():
            raise FileNotFoundError(f"brak pliku {SZABLON.name} w folderze {KATALOG}")
        dane = zbuduj_dane(cfg, con)
        if a.publiczny:
            dane["meta"]["lat"] = dane["meta"]["lon"] = None
        if a.wyjscie:
            global WYJSCIE
            WYJSCIE = Path(a.wyjscie).resolve()
            WYJSCIE.parent.mkdir(parents=True, exist_ok=True)
        zapisz_dashboard(dane)
    except Exception as e:
        bledy.append(f"Dashboard: {e}")
        log("BŁĄD dashboardu:\n" + traceback.format_exc())
    if bledy:
        sys.exit(1)


if __name__ == "__main__":
    main()
