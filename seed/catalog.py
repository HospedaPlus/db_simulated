"""Fixed reference data: hotels, room types, channels and prices.

Nothing here is random. Codes match the other simulators in the team
repo (F2 events, F4 agency files, F5 reviews all use hotel-001..hotel-028
and SINGLE/DOUBLE/TWIN/SUITE), so a hotel or room type means the same
thing in every source. Change a code here and you break their joins.

The chain: 26 hotels in Colombia (sold in COP) plus the 2 Portuguese
hotels behind the Kaggle history F1 (sold in EUR), as the statement says.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RoomType:
    code: str
    name: str
    max_guests: int


ROOM_TYPES = [
    RoomType("SINGLE", "Single room", 1),
    RoomType("DOUBLE", "Double room", 2),
    RoomType("TWIN", "Twin room", 2),
    RoomType("SUITE", "Suite", 4),
]


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


_CO = ("CO", "America/Bogota", "COP")
_PT = ("PT", "Europe/Lisbon", "EUR")

# (name, category, city, country info, rooms, price factor)
_HOTELS = [
    ("HospedaPlus Chapinero",           "urban",  "Bogota",          _CO, 210, 1.10),
    ("HospedaPlus Usaquen",             "urban",  "Bogota",          _CO, 180, 1.10),
    ("HospedaPlus Centro Internacional", "urban", "Bogota",          _CO, 190, 1.05),
    ("HospedaPlus Aeropuerto El Dorado", "urban", "Bogota",          _CO, 160, 1.00),
    ("HospedaPlus Zona T",              "urban",  "Bogota",          _CO, 160, 1.15),
    ("HospedaPlus El Poblado",          "urban",  "Medellin",        _CO, 160, 1.10),
    ("HospedaPlus Laureles",            "urban",  "Medellin",        _CO, 140, 1.00),
    ("HospedaPlus Ciudad del Rio",      "urban",  "Medellin",        _CO, 150, 1.05),
    ("HospedaPlus Guatape",             "resort", "Guatape",         _CO,  90, 1.00),
    ("HospedaPlus Granada",             "urban",  "Cali",            _CO, 150, 0.95),
    ("HospedaPlus Ciudad Jardin",       "urban",  "Cali",            _CO, 130, 0.95),
    ("HospedaPlus Barranquilla Norte",  "urban",  "Barranquilla",    _CO, 160, 0.95),
    ("HospedaPlus Puerto Colombia",     "resort", "Puerto Colombia", _CO, 110, 0.95),
    ("HospedaPlus Bocagrande",          "resort", "Cartagena",       _CO, 190, 1.25),
    ("HospedaPlus Centro Historico",    "urban",  "Cartagena",       _CO, 120, 1.30),
    ("HospedaPlus Manzanillo",          "resort", "Cartagena",       _CO, 180, 1.20),
    ("HospedaPlus El Rodadero",         "resort", "Santa Marta",     _CO, 170, 1.05),
    ("HospedaPlus Santa Marta Centro",  "urban",  "Santa Marta",     _CO, 100, 1.00),
    ("HospedaPlus San Andres",          "resort", "San Andres",      _CO, 200, 1.25),
    ("HospedaPlus Sound Bay",           "resort", "San Andres",      _CO, 120, 1.20),
    ("HospedaPlus Cabecera",            "urban",  "Bucaramanga",     _CO, 130, 0.90),
    ("HospedaPlus Pereira",             "urban",  "Pereira",         _CO, 110, 0.90),
    ("HospedaPlus Salento",             "resort", "Salento",         _CO,  80, 0.95),
    ("HospedaPlus Villa de Leyva",      "resort", "Villa de Leyva",  _CO,  70, 1.00),
    ("HospedaPlus Manizales",           "urban",  "Manizales",       _CO, 100, 0.90),
    ("HospedaPlus Cucuta",              "urban",  "Cucuta",          _CO, 110, 0.85),
    # The two F1 (Kaggle) hotels: one city hotel and one resort in Portugal.
    ("HospedaPlus Lisboa",              "urban",  "Lisbon",          _PT, 180, 1.00),
    ("HospedaPlus Algarve",             "resort", "Algarve",         _PT, 150, 1.00),
]

HOTELS = [
    Hotel(f"hotel-{i:03d}", name, cat, city, c[0], c[1], c[2], rooms, factor)
    for i, (name, cat, city, c, rooms, factor) in enumerate(_HOTELS, start=1)
]

TOTAL_ROOMS = sum(h.rooms for h in HOTELS)  # 4,000

# Share of each room type in a hotel. Resorts lean to doubles and suites.
ROOM_MIX = {
    "urban":  {"SINGLE": 0.25, "DOUBLE": 0.40, "TWIN": 0.25, "SUITE": 0.10},
    "resort": {"SINGLE": 0.05, "DOUBLE": 0.50, "TWIN": 0.30, "SUITE": 0.15},
}

# Nightly base rate per room before season and destination factors.
BASE_RATES = {
    "COP": {"SINGLE": 240_000, "DOUBLE": 320_000, "TWIN": 330_000, "SUITE": 680_000},
    "EUR": {"SINGLE": 90, "DOUBLE": 120, "TWIN": 125, "SUITE": 260},
}
RESORT_FACTOR = 1.15

# RN-05 season multipliers.
SEASON_MULTIPLIER = {"LOW": 0.85, "MID": 1.00, "HIGH": 1.25}


def season_for(day: date, country: str) -> str:
    """Rough tourist calendar per country. Good enough for simulated
    prices; holidays and events are added later by F6, not here."""
    md = (day.month, day.day)
    if country == "PT":
        if (6, 15) <= md <= (9, 15) or md >= (12, 20) or md <= (1, 5):
            return "HIGH"
        if md >= (11, 1) or md <= (3, 31):
            return "LOW"
        return "MID"
    # Colombia: year-end and mid-year school holidays are the peaks.
    if md >= (12, 15) or md <= (1, 15) or (6, 15) <= md <= (7, 31):
        return "HIGH"
    if (2, 1) <= md <= (3, 15) or (9, 1) <= md <= (11, 30):
        return "LOW"
    return "MID"


@dataclass(frozen=True)
class Channel:
    code: str
    name: str
    channel_type: str
    commission_pct: float


# RN-02 commissions: OTA 18%, traditional agency 10%, corporate 0%, direct 0%.
# OTA codes are the ones the F2 simulator emits; AG01..AG15 are the
# agencies of the F4 simulator (their booking ids start with these codes).
_AGENCIES = [
    "ViajaFacil", "SolyMar Tours", "EuroTrip Agency", "Andes Travel",
    "GlobalStay Agency", "CaribeViajes", "MontanaTours", "CostaBella Travel",
    "NorteSur Agencia", "Horizonte Viajes", "Rutas del Sur", "BlueSky Agency",
    "Destino Feliz", "Puerta del Sol Travel", "Viajes Amanecer",
]

CHANNELS = [
    Channel("direct_web", "HospedaPlus website", "direct", 0),
    Channel("corporate", "Corporate agreements", "corporate", 0),
    Channel("booking_channel_a", "OTA channel A", "ota", 18),
    Channel("booking_channel_b", "OTA channel B", "ota", 18),
    Channel("expedia", "Expedia", "ota", 18),
    Channel("direct_ota", "OTA direct connect", "ota", 18),
] + [Channel(f"AG{i:02d}", name, "agency", 10) for i, name in enumerate(_AGENCIES, start=1)]

# Channels that book through our own engine and therefore land in F3.
# OTA and agency bookings arrive through F2 and F4 instead.
F3_CHANNEL_WEIGHTS = {"direct_web": 0.8, "corporate": 0.2}
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
    ("CO", "es", 0.62), ("US", "en", 0.10), ("MX", "es", 0.05), ("ES", "es", 0.05),
    ("PT", "pt", 0.06), ("BR", "pt", 0.05), ("GB", "en", 0.04), ("CA", "en", 0.03),
]

LOYALTY_WEIGHTS = {"none": 0.80, "silver": 0.15, "gold": 0.05}
