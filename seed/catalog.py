"""Reference data: hotels, room types, channels, seasons and prices.

The shared part (hotels, room types, channels, seasons) is read from
catalog/catalog.json, a pinned copy of the HospedaPlus shared catalog. F2,
F4 and F5 read the same file, so a hotel or room type means the same thing
in every source. Never edit the copy here: change the shared catalog,
publish a new version and copy it in.

What only F3 needs to generate data (base prices, price level per hotel,
guest names) stays in this module.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import ROOT

CATALOG_PATH = Path(os.getenv("HOSPEDAPLUS_CATALOG", ROOT / "catalog" / "catalog.json"))
_CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
CATALOG_VERSION: str = _CATALOG["catalog_version"]


@dataclass(frozen=True)
class RoomType:
    code: str
    name: str
    max_guests: int


ROOM_TYPES = [RoomType(r["code"], r["name"], r["max_guests"]) for r in _CATALOG["room_types"]]


@dataclass(frozen=True)
class Hotel:
    code: str
    name: str
    category: str   # urban | resort
    city: str
    country: str
    timezone: str
    currency: str
    rooms: int
    price_factor: float  # how pricey the destination is vs. the chain average


# F3-only: price level of each destination (1.00 = chain average).
PRICE_FACTORS = {
    "HP-CO-002": 1.25, "HP-CO-003": 1.30, "HP-CO-009": 1.20, "HP-CO-005": 1.10,
    "HP-CO-010": 0.90, "HP-CO-008": 0.95,
    "HP-MX-001": 1.25, "HP-MX-006": 1.30, "HP-MX-002": 1.15, "HP-MX-003": 1.10,
    "HP-BR-001": 1.20, "HP-BR-002": 1.10, "HP-BR-006": 0.90,
    "HP-ES-002": 1.15, "HP-ES-001": 1.10, "HP-ES-003": 1.05,
    "HP-PT-001": 1.00, "HP-PT-002": 1.00, "HP-PT-003": 0.95,
}

HOTELS = [
    Hotel(h["code"], h["name"], h["category"], h["city"], h["country"], h["timezone"],
          h["currency"], h["rooms"], PRICE_FACTORS.get(h["code"], 1.00))
    for h in _CATALOG["hotels"]
]

TOTAL_ROOMS = sum(h.rooms for h in HOTELS)  # 4,000

# Share of each room type in a hotel. Resorts lean to doubles and suites.
ROOM_MIX = {
    "urban":  {"SINGLE": 0.25, "DOUBLE": 0.40, "TWIN": 0.25, "SUITE": 0.10},
    "resort": {"SINGLE": 0.05, "DOUBLE": 0.50, "TWIN": 0.30, "SUITE": 0.15},
}

# F3-only: nightly base rate per room before season and destination factors.
BASE_RATES = {
    "COP": {"SINGLE": 240_000, "DOUBLE": 320_000, "TWIN": 330_000, "SUITE": 680_000},
    "MXN": {"SINGLE": 1_300, "DOUBLE": 1_700, "TWIN": 1_750, "SUITE": 3_600},
    "BRL": {"SINGLE": 380, "DOUBLE": 500, "TWIN": 520, "SUITE": 1_050},
    "EUR": {"SINGLE": 90, "DOUBLE": 120, "TWIN": 125, "SUITE": 260},
}
RESORT_FACTOR = 1.15

# RN-05 season multipliers, shared with every price calculation.
SEASON_MULTIPLIER: dict[str, float] = _CATALOG["pricing_rules"]["season_multipliers"]


def _season_table() -> dict[str, dict[tuple[int, int], str]]:
    """(month, day) -> season for every country, from the catalog ranges."""
    table: dict[str, dict[tuple[int, int], str]] = {}
    for country, seasons in _CATALOG["seasons"].items():
        if country.startswith("_"):
            continue
        days: dict[tuple[int, int], str] = {}
        for name, ranges in seasons.items():
            for start, end in ranges:
                s = (int(start[:2]), int(start[3:]))
                e = (int(end[:2]), int(end[3:]))
                for m in range(1, 13):
                    for d in range(1, 32):
                        md = (m, d)
                        inside = s <= md <= e if s <= e else (md >= s or md <= e)
                        if inside:
                            days[md] = name
        table[country] = days
    return table


_SEASONS = _season_table()


def season_for(day: date, country: str) -> str:
    """RN-05 season of a day in a country; days not listed are MID.
    Holidays and events are added later by F6, not here."""
    return _SEASONS[country].get((day.month, day.day), "MID")


@dataclass(frozen=True)
class Channel:
    code: str
    name: str
    channel_type: str
    commission_pct: float


# RN-02 commissions come with each channel in the shared catalog:
# OTA 18% (F2), traditional agency 10% (F4: AG01..AG15), direct and corporate 0%.
CHANNELS = [Channel(c["code"], c["name"], c["type"], c["commission_pct"]) for c in _CATALOG["channels"]]

# Channels that book through our own engine and therefore land in F3.
# OTA and agency bookings arrive through F2 and F4 instead.
F3_CHANNEL_WEIGHTS = {
    "urban":  {"direct_web": 0.75, "corporate": 0.25},
    "resort": {"direct_web": 0.92, "corporate": 0.08},
}

# Corporate agreements are mostly for city hotels.
CORPORATE_DISCOUNT = 0.90  # negotiated corporate rate (RN-02)


# Small name pools for fake guests. Kept in the repo instead of using a
# faker library so the output never changes when a library updates.
NAME_POOLS = {
    "es": (
        ["Andres", "Camila", "Santiago", "Valentina", "Juan", "Laura", "Mateo", "Daniela",
         "Sebastian", "Mariana", "Carlos", "Paula", "Felipe", "Natalia", "Diego", "Sofia",
         "Alejandro", "Isabella", "David", "Gabriela", "Jorge", "Valeria", "Esteban", "Lina",
         "Nicolas", "Ricardo", "Ana", "Luis", "Catalina", "Miguel"],
        ["Rodriguez", "Gomez", "Martinez", "Garcia", "Lopez", "Hernandez", "Gonzalez", "Perez",
         "Sanchez", "Ramirez", "Torres", "Diaz", "Vargas", "Moreno", "Rojas", "Castro",
         "Jimenez", "Ruiz", "Mendoza", "Herrera", "Ortiz", "Silva", "Cardenas", "Rios"],
    ),
    "pt": (
        ["Joao", "Maria", "Pedro", "Ana", "Tiago", "Beatriz", "Rui", "Ines", "Miguel",
         "Leonor", "Duarte", "Carolina", "Goncalo", "Mariana", "Lucas", "Rafaela"],
        ["Silva", "Santos", "Ferreira", "Pereira", "Oliveira", "Costa", "Rodrigues",
         "Martins", "Sousa", "Fernandes", "Goncalves", "Almeida", "Ribeiro", "Carvalho"],
    ),
    "en": (
        ["James", "Emma", "Oliver", "Olivia", "William", "Ava", "Henry", "Mia", "Jack",
         "Charlotte", "Noah", "Amelia", "Ethan", "Harper", "Liam", "Grace"],
        ["Smith", "Johnson", "Brown", "Taylor", "Wilson", "Davies", "Miller", "Clark",
         "Walker", "Wright", "Hall", "Young", "King", "Scott", "Green", "Baker"],
    ),
}

# Where guests come from: (country, name pool, weight).
GUEST_ORIGINS = [
    ("CO", "es", 0.28), ("MX", "es", 0.18), ("BR", "pt", 0.18), ("ES", "es", 0.08),
    ("PT", "pt", 0.06), ("US", "en", 0.12), ("GB", "en", 0.05), ("CA", "en", 0.05),
]

LOYALTY_WEIGHTS = {"none": 0.80, "silver": 0.15, "gold": 0.05}
