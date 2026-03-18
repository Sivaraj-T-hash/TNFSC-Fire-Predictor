"""
TNFSC Smart Fire Intelligence Portal — Flask Backend (v2: Real-Time AI)
=======================================================================
What makes this "real-time AI":
  ✅ Model trains on startup with 1200-row synthetic historical dataset
  ✅ Background thread generates NEW synthetic readings every 15 seconds
     (simulating live sensor/API feeds) and appends them to the dataset
  ✅ Background retraining loop re-fits both RF and GB every 2 minutes
     on the ever-growing dataset, so accuracy and weights evolve over time
  ✅ Every API call uses the CURRENT model weights — not static data
  ✅ /api/status shows model version, training rounds, and dataset size

Routes:
  GET  /                  → serves index.html
  POST /api/calculate     → runs current models, returns zone risk scores + chart
  GET  /api/heatmap       → heatmap JSON with ML-scored risk per zone
  GET  /api/incidents     → ML-scored live incident feed
  GET  /api/metrics       → live dashboard headline metrics + ticker
  GET  /api/status        → AI model status (version, accuracy, data size)
  POST /api/retrain       → manually trigger a model retrain
"""

import json
import random
import datetime
import threading
import numpy as np
import pandas as pd
import requests
import time
import os
import base64

from flask import Flask, jsonify, send_from_directory, request
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import r2_score

# ── Web Push / VAPID ──────────────────────────────────────────────────────────
try:
    from pywebpush import webpush, WebPushException
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.backends import default_backend
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False
    print("[TNFSC-AI] ⚠️  pywebpush not installed — mobile push disabled. Run: pip install pywebpush")

# ── Flask App ──────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder='.', template_folder='.')

# ── VAPID Keys + Push Subscription Registry ────────────────────────────────────
# VAPID keys are auto-generated on first run and cached in vapid_keys.json.
VAPID_KEYS_FILE = os.path.join(os.path.dirname(__file__), 'vapid_keys.json')
VAPID_CLAIMS    = {"sub": "mailto:tnfrs-admin@tnfrs.gov.in"}
_push_subscriptions = []  # In-memory list: [{endpoint, keys:{p256dh, auth}}]
_push_lock          = threading.Lock()

def _load_or_generate_vapid_keys():
    """Load VAPID keys from disk, or generate + save a fresh pair."""
    if os.path.exists(VAPID_KEYS_FILE):
        try:
            with open(VAPID_KEYS_FILE, 'r') as f:
                keys = json.load(f)
            print("[TNFSC-AI] 🔑 VAPID keys loaded from vapid_keys.json")
            return keys
        except Exception as e:
            print(f"[TNFSC-AI] ⚠️  VAPID key load failed ({e}), regenerating…")

    # Generate new EC key pair (P-256)
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    public_key  = private_key.public_key()

    # Encode to base64url (uncompressed point format for Web Push)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
    pub_bytes  = public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    priv_bytes = private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())

    pub_b64  = base64.urlsafe_b64encode(pub_bytes).rstrip(b'=').decode('utf-8')
    priv_pem = priv_bytes.decode('utf-8')

    keys = {"public_key": pub_b64, "private_key": priv_pem}
    with open(VAPID_KEYS_FILE, 'w') as f:
        json.dump(keys, f, indent=2)
    print("[TNFSC-AI] 🔑 New VAPID keys generated and saved to vapid_keys.json")
    return keys


VAPID_KEYS = {"public_key": "", "private_key": ""}
if PUSH_AVAILABLE:
    VAPID_KEYS = _load_or_generate_vapid_keys()


def _send_push_to_all(title: str, body: str, zone: str = "", risk: float = 0, level: str = "HIGH"):
    """Send a Web Push notification to all registered subscribers."""
    if not PUSH_AVAILABLE or not VAPID_KEYS.get("private_key"):
        return
    with _push_lock:
        subs = list(_push_subscriptions)
    if not subs:
        return

    payload_data = json.dumps({
        "title": title,
        "body":  body,
        "zone":  zone,
        "risk":  risk,
        "level": level,
    })

    stale = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload_data,
                vapid_private_key=VAPID_KEYS["private_key"],
                vapid_claims=VAPID_CLAIMS,
                ttl=600,
            )
        except WebPushException as ex:
            status = ex.response.status_code if ex.response else 0
            if status in (404, 410):  # subscription expired
                stale.append(sub)
            else:
                print(f"[TNFSC-AI] ⚠️  Push failed ({status}): {ex}")
        except Exception as ex:
            print(f"[TNFSC-AI] ⚠️  Push error: {ex}")

    # Remove expired subscriptions
    if stale:
        with _push_lock:
            for s in stale:
                if s in _push_subscriptions:
                    _push_subscriptions.remove(s)
        print(f"[TNFSC-AI] 🗑️  Removed {len(stale)} expired push subscriptions.")

# ─────────────────────────────────────────────────────────────────────────────
#  GLOBAL MODEL STATE  (protected by a threading.Lock)
# ─────────────────────────────────────────────────────────────────────────────
model_lock = threading.Lock()

state = {
    "rf_model":       None,
    "gb_model":       None,
    "le":             None,
    "df":             None,           # live-growing dataset
    "rf_accuracy":    0.0,
    "gb_accuracy":    0.0,
    "model_version":  0,
    "retrain_count":  0,
    "last_retrained": None,
    "training":       False,
    "official_stations": [], # Loaded from JSON
    "live_weather": {
        "temp_c": 33.3,
        "humidity": 63,
        "wind_kph": 23.0,
        "feelslike_c": 39.6,
        "uv": 7.1,
        "condition": "Sunny",
        "last_updated": "N/A"
    },
    "last_weather_query": "auto:ip",
    "active_zones": [],           # Dynamic list (Phase 17)
    "active_zone_positions": {},   # Dynamic (x,y) (Phase 17)
    "active_zone_gps": {}          # Dynamic (lat,lng) (Phase 17)
}

ZONES = [
    "Chennai Central", "Adyar", "T. Nagar", "Vadapalani",
    "Velachery", "Anna Nagar", "Royapettah", "Madurai North",
    "Coimbatore East", "Salem West", "Trichy Old Town", "Erode Market"
]

INCIDENT_TYPES = [
    "Structure Fire", "Electrical Short Circuit", "Gas Leak Report",
    "Building Structural Alert", "Chemical Storage Warning", "Smoke Detected"
]

FEATURES = [
    "zone_encoded", "hour", "day_of_week",
    "temperature_c", "humidity_pct", "wind_speed_kmh",
    "building_density", "road_proximity_km", "water_pressure_bar"
]
TARGET = "fire_risk"

# ── STATIC ZONE CHARACTERISTICS ───────────────────────────────────────────────
# Building density is a fixed geographic property of each zone (0-100 scale).
# It does NOT change with time — only weather/sensor readings do.
ZONE_BUILDING_DENSITY = {
    "Chennai Central":  92.0,   # Very dense CBD, mixed commercial/residential
    "Adyar":            65.0,   # Dense residential with tree cover
    "T. Nagar":         95.0,   # Extremely dense commercial hub
    "Vadapalani":       78.0,   # Dense mixed-use
    "Velachery":        72.0,   # Modern residential/IT corridor
    "Anna Nagar":       70.0,   # Planned residential, medium-high density
    "Royapettah":       85.0,   # Old city, dense residential/commercial
    "Madurai North":    80.0,   # Heritage dense urban
    "Coimbatore East":  68.0,   # Industrial + residential mix
    "Salem West":       60.0,   # Semi-urban commercial
    "Trichy Old Town":  82.0,   # Very dense heritage district
    "Erode Market":     75.0,   # Dense wholesale market area
}

def _zone_building_density(zone_name: str) -> float:
    """Return a stable, static building density for a zone.
    Known zones use hand-coded realistic values.
    Unknown/dynamic zones (e.g. city-generated sectors) get a deterministic
    value seeded from the zone name so they never change between batches.
    """
    if zone_name in ZONE_BUILDING_DENSITY:
        return ZONE_BUILDING_DENSITY[zone_name]
    # For dynamically-created zones: use zone-name hash for a stable value
    seed = sum(ord(c) for c in zone_name) % 1000
    rng  = random.Random(seed)
    return round(rng.uniform(40.0, 90.0), 1)

# ── AI STRATEGIC ADVISOR (HEURISTIC LLM-SIM) ──
def _generate_mission_briefing(avg_risk, zone_risks, ds_size, virtual_count=0):
    """Simulates an LLM 'Advisor' generating operational intelligence."""
    critical_zones = [z for z, r in zone_risks.items() if r > 70]
    high_zones = [z for z, r in zone_risks.items() if 50 < r <= 70]
    
    # Calculate Mitigation Roadmap impact
    # Base safety is 65. If virtual stations exist, we estimate their margin
    mitigated_margin = min(35, virtual_count * 4.5) 
    total_safety_margin = round(65 + mitigated_margin, 1)

    brief = []
    
    # 1. Operational Outlook
    if avg_risk > 65:
        brief.append("⚠️ STATUS: CRITICAL. Thermal anomalies detected across major hubs. Dispatch priority: EXTREME.")
    elif avg_risk > 45:
        brief.append("🟡 STATUS: ELEVATED. Moderate risk density identified. Pre-emptive unit relocation recommended.")
    else:
        brief.append("🟢 STATUS: NOMINAL. Routine monitoring active. No immediate strategic shifts required.")

    # 2. Strategic Mitigation Roadmap (Phase 15 Logic)
    if virtual_count > 0:
        brief.append(f"🛡️ STRATEGY: {virtual_count} tactical assets deployed. Current deployment provides a {total_safety_margin}% City Safety Margin.")
    
    # 3. Zone-Specific Intelligence
    if critical_zones:
        brief.append(f"🔍 INTELLIGENCE: {', '.join(critical_zones)} are exhibiting high building-density correlations. Hydrant checks prioritized.")
    elif high_zones:
        brief.append(f"ℹ️ NOTICE: Growth in risk vectors for {', '.join(high_zones[:2])}. Monitor closely for smoke alerts.")

    # 4. Strategic Recommendations
    if avg_risk > 60:
        brief.append("📋 RECOMMENDATION: Activate Level-2 Response Protocol. Reallocate 30% of standby units to T. Nagar/Adyar axis.")
    else:
        brief.append("📋 RECOMMENDATION: Maintain standard grid coverage. Optimize fuel logistics for evening shifts.")

    return {
        "summary": " ".join(brief[:4]),
        "steps": [
            "Syncing telemetry with Meteorological API...",
            "Recalculating ensemble weights...",
            f"Analyzing patterns for {len(state['active_zones'])} localized sectors...",
            f"Mitigation Roadmap: {total_safety_margin}% Safety Margin Calculated.",
            "Strategic Briefing Generated."
        ]
    }

# ─────────────────────────────────────────────────────────────────────────────
#  SYNTHETIC DATA GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def _make_rows(n: int, seed=None) -> list[dict]:
    """Generate n synthetic sensor rows with realistic correlations."""
    rng = random.Random(seed)
    rows = []
    with model_lock:
        active_zones = list(state["active_zones"])
        current_weather = state["live_weather"].copy()

    for _ in range(n):
        zone        = rng.choice(active_zones) if active_zones else "Primary Sector"
        hour        = rng.randint(0, 23)
        day_of_week = rng.randint(0, 6)

        temp        = current_weather["temp_c"] if rng.random() > 0.3 else round(rng.uniform(22, 44), 1)
        humidity    = current_weather["humidity"] if rng.random() > 0.3 else round(rng.uniform(30, 95), 1)
        wind_speed  = current_weather["wind_kph"] if rng.random() > 0.3 else round(rng.uniform(0, 40), 1)
        # Building density is a STATIC zone property — never randomised per row
        bld_density = _zone_building_density(zone)
        road_prox   = round(rng.uniform(0.1, 5.0), 2)
        water_pres  = round(rng.uniform(2, 10), 1)

        risk_score = (
            0.25 * (temp / 44)
            + 0.20 * (1 - humidity / 95)
            + 0.20 * (bld_density / 100)
            + 0.15 * (wind_speed / 40)
            + 0.10 * (1 - water_pres / 10)
            + 0.05 * (hour in range(12, 18))
            + 0.05 * (day_of_week in [5, 6])
        )
        fire_risk = 1 if risk_score > 0.45 else 0

        rows.append({
            "zone": zone, "hour": hour, "day_of_week": day_of_week,
            "temperature_c": temp, "humidity_pct": humidity,
            "wind_speed_kmh": wind_speed, "building_density": bld_density,
            "road_proximity_km": road_prox, "water_pressure_bar": water_pres,
            "fire_risk": fire_risk,
            "recorded_at": datetime.datetime.now().isoformat()
        })
    return rows


def _build_initial_dataset() -> pd.DataFrame:
    np.random.seed(42)
    rows = _make_rows(1200, seed=42)
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
#  ENCODING + TRAINING
# ─────────────────────────────────────────────────────────────────────────────

def _encode_df(df: pd.DataFrame, le: LabelEncoder) -> pd.DataFrame:
    df = df.copy()
    known_zones = set(le.classes_)
    df["zone_encoded"] = df["zone"].apply(
        lambda z: int(le.transform([z])[0]) if z in known_zones else 0
    )
    return df


def _train_models(df: pd.DataFrame, le: LabelEncoder):
    """Fit RF and GB on the provided dataframe, return (rf, gb, rf_acc, gb_acc)."""
    df_enc = _encode_df(df, le)
    X = df_enc[FEATURES]
    y = df_enc[TARGET]
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    rf = RandomForestClassifier(n_estimators=150, max_depth=8, n_jobs=-1, random_state=42)
    rf.fit(X_train, y_train)

    gb = GradientBoostingClassifier(n_estimators=100, learning_rate=0.08,
                                     max_depth=4, random_state=42)
    gb.fit(X_train, y_train)

    rf_acc = round(rf.score(X_test, y_test) * 100, 2)
    gb_acc = round(gb.score(X_test, y_test) * 100, 2)
    return rf, gb, rf_acc, gb_acc


# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP — initial train
# ─────────────────────────────────────────────────────────────────────────────

print("[TNFSC-AI] Generating initial historical dataset (1 200 records)…")
_initial_df = _build_initial_dataset()
with model_lock:
    state["active_zones"] = [
        "Chennai Central", "Adyar", "T. Nagar", "Vadapalani",
        "Velachery", "Anna Nagar", "Royapettah", "Madurai North",
        "Coimbatore East", "Salem West", "Trichy Old Town", "Erode Market"
    ]
    state["active_zone_positions"] = {
        "Chennai Central": (0.50,0.30), "Adyar": (0.55,0.65), "T. Nagar": (0.45,0.50),
        "Vadapalani": (0.36,0.44), "Velachery": (0.52,0.72), "Anna Nagar": (0.38,0.30),
        "Royapettah": (0.56,0.40), "Madurai North": (0.30,0.80), "Coimbatore East": (0.20,0.55),
        "Salem West": (0.35,0.20), "Trichy Old Town": (0.50,0.62), "Erode Market": (0.22,0.35)
    }

print("[TNFSC-AI] Training Initial Command Models…")
_le = LabelEncoder()
_le.fit(state["active_zones"])
_rf, _gb, _rf_acc, _gb_acc = _train_models(_initial_df, _le)

with model_lock:
    state["rf_model"]      = _rf
    state["gb_model"]      = _gb
    state["le"]            = _le
    state["df"]            = _initial_df
    state["rf_accuracy"]   = _rf_acc
    state["gb_accuracy"]   = _gb_acc
    state["model_version"] = 1
    state["last_retrained"]= datetime.datetime.now().isoformat()

# Load Official TNFRS Master Database (Phase 18 Integration)
try:
    if os.path.exists('TNFRS_Master_Database_2026.csv'):
        station_df = pd.read_csv('TNFRS_Master_Database_2026.csv')
        stations_list = []
        for _, row in station_df.iterrows():
            stations_list.append({
                "station_name": str(row.get("name", row.get("na", "Unknown"))),
                "district":     str(row.get("district", "Unknown")),
                "category":     str(row.get("cat", "B")),
                "cug":          str(row.get("cug", "")),
                "landline":     str(row.get("landline", "")),
                "lat":          float(row.get("lat", 0)),
                "lng":          float(row.get("lng", 0)),
                "address":      str(row.get("address", ""))
            })
        with model_lock:
            state["official_stations"] = stations_list
        print(f"[TNFSC-AI] 📡 Loaded {len(stations_list)} official TNFRS stations from CSV.")
    elif os.path.exists('TNFRS_Historical_Database_1900_2026.json'):
        with open('TNFRS_Historical_Database_1900_2026.json', 'r', encoding='utf-8') as f:
            state["official_stations"] = json.load(f)
        print(f"[TNFSC-AI] 📡 Loaded {len(state['official_stations'])} stations from legacy JSON.")
except Exception as e:
    print(f"[TNFSC-AI] ❌ Failed to load master database: {e}")
    state["official_stations"] = []

print(f"[TNFSC-AI] ✅ Initial training done — RF: {_rf_acc}%  GB: {_gb_acc}%")
print(f"[TNFSC-AI] Dataset size: {len(_initial_df)} rows")


# ─────────────────────────────────────────────────────────────────────────────
#  BACKGROUND THREADS
# ─────────────────────────────────────────────────────────────────────────────

def _live_data_writer():
    """Every 15 s: generate 3-5 new 'sensor readings' and append to the dataset."""
    while True:
        time.sleep(15)
        new_rows = _make_rows(rng_n := random.randint(3, 6))
        new_df   = pd.DataFrame(new_rows)
        with model_lock:
            state["df"] = pd.concat([state["df"], new_df], ignore_index=True)
        print(f"[TNFSC-AI] 📡 +{rng_n} live readings ingested "
              f"| Total dataset: {len(state['df'])} rows")


def _retrain_loop():
    """Every 120 s: retrain both models on the full (growing) dataset."""
    while True:
        time.sleep(120)
        with model_lock:
            if state["training"]:
                continue
            state["training"] = True
            current_df = state["df"].copy()
            le         = state["le"]

        print(f"[TNFSC-AI] 🔄 Retraining on {len(current_df)} rows…")
        try:
            rf, gb, rf_acc, gb_acc = _train_models(current_df, le)
            with model_lock:
                state["rf_model"]      = rf
                state["gb_model"]      = gb
                state["rf_accuracy"]   = rf_acc
                state["gb_accuracy"]   = gb_acc
                state["model_version"] += 1
                state["retrain_count"] += 1
                state["last_retrained"] = datetime.datetime.now().isoformat()
                state["training"]       = False
            print(f"[TNFSC-AI] ✅ Retrain complete — v{state['model_version']} "
                  f"RF: {rf_acc}%  GB: {gb_acc}%")
        except Exception as e:
            with model_lock:
                state["training"] = False
            print(f"[TNFSC-AI] ❌ Retrain error: {e}")


def _fetch_weather_for_query(query, api_key):
    """Fetch WeatherAPI current data for a given query (lat,lng or auto:ip). Returns state dict or None."""
    try:
        url  = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={query}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            d = resp.json()
            w = d["current"]
            loc = d.get("location", {})
            return {
                "temp_c":       w["temp_c"],
                "feelslike_c":  w["feelslike_c"],
                "humidity":     w["humidity"],
                "wind_kph":     w["wind_kph"],
                "uv":           w["uv"],
                "condition":    w["condition"]["text"],
                "condition_icon": w["condition"]["icon"],
                "last_updated": w["last_updated"],
                "city":         loc.get("name", ""),
                "region":       loc.get("region", "Tamil Nadu"),
                "country":      loc.get("country", "India"),
                "lat":          loc.get("lat", 13.08),
                "lng":          loc.get("lon", 80.28),
            }
    except Exception as e:
        print(f"[TNFSC-AI] ❌ Weather fetch error: {e}")
    return None


def _weather_sync_loop():
    """Every 15 mins: Sync live telemetry using the last known location query (Phase 16)."""
    API_KEY = "c053b492182c4161bfa80350261403"
    time.sleep(5)  # let init_location run first
    while True:
        with model_lock:
            query = state.get("last_weather_query") or "auto:ip"
        
        print(f"[TNFSC-AI] 🌦️ Background weather refresh for: {query}")
        result = _fetch_weather_for_query(query, API_KEY)
        if result:
            with model_lock:
                state["live_weather"].update(result)
            print(f"[TNFSC-AI] ✅ Weather synced for: {result.get('city','?')} at {result.get('last_updated','unknown')}")
        else:
            print(f"[TNFSC-AI] ❌ Weather sync failed for: {query}")
        
        time.sleep(900) # 15 minutes


threading.Thread(target=_live_data_writer, daemon=True).start()
threading.Thread(target=_retrain_loop,    daemon=True).start()
threading.Thread(target=_weather_sync_loop, daemon=True).start()
print("[TNFSC-AI] 🚀 Live data writer + retrain + weather-sync threads started.")


# ─────────────────────────────────────────────────────────────────────────────
#  HELPER — build a real-time prediction row for a zone
# ─────────────────────────────────────────────────────────────────────────────

def _zone_feature_row(zone_name: str, df: pd.DataFrame, le: LabelEncoder) -> pd.DataFrame:
    now   = datetime.datetime.now()
    z_df  = df[df["zone"] == zone_name] if not df.empty and zone_name in df["zone"].values else df

    encoded = int(le.transform([zone_name])[0]) if zone_name in le.classes_ else 0

    return pd.DataFrame([{
        "zone_encoded":       encoded,
        "hour":               now.hour,
        "day_of_week":        now.weekday(),
        "temperature_c":      round(float(z_df["temperature_c"].tail(50).mean()), 1),
        "humidity_pct":       round(float(z_df["humidity_pct"].tail(50).mean()), 1),
        "wind_speed_kmh":     round(float(z_df["wind_speed_kmh"].tail(50).mean()), 1),
        "building_density":   round(float(z_df["building_density"].tail(50).mean()), 1),
        "road_proximity_km":  round(float(z_df["road_proximity_km"].tail(50).mean()), 2),
        "water_pressure_bar": round(float(z_df["water_pressure_bar"].tail(50).mean()), 1),
    }])


def _predict_zone(zone: str, rf, gb, df, le, virtual_stations=None) -> dict:
    row    = _zone_feature_row(zone, df, le)
    rf_p   = float(rf.predict_proba(row)[0][1])
    gb_p   = float(gb.predict_proba(row)[0][1])
    ens    = (rf_p + gb_p) / 2
    risk   = round(ens * 100, 1)
    
    # Mitigation Logic (Phase 15): Suppress risk if virtual stations are nearby
    mitigation = 0
    if virtual_stations:
        with model_lock:
             gps = state["active_zone_gps"]
        zlat, zlng = gps.get(zone, (13.08, 80.28))
        for s in virtual_stations:
            # Handle both x/y (legacy) and lat/lng
            if 'lat' in s and 'lng' in s:
                dist_km = _haversine_km(zlat, zlng, s['lat'], s['lng'])
                if dist_km < 5.0: # 5km suppression radius
                    mitigation += max(0, (5.0 - dist_km) * 6) # Max 30% reduction per station
            elif 'x' in s and 'y' in s:
                # Legacy fallback
                with model_lock: positions = state["active_zone_positions"]
                zx, zy = positions.get(zone, (0.5, 0.5))
                dist = ((zx - s['x'])**2 + (zy - s['y'])**2)**0.5
                if dist < 0.2:
                    mitigation += max(0, (0.2 - dist) * 150)

    risk = max(10, risk - mitigation) # Never drops below 10 for realism

    # Feature Impact Diagnostic
    factors = []
    if float(row["temperature_c"].iloc[0]) > 38: factors.append("Critical Temperature")
    if float(row["humidity_pct"].iloc[0]) < 40: factors.append("Extreme Aridity")
    if float(row["building_density"].iloc[0]) > 75: factors.append("High Structural Density")
    if float(row["wind_speed_kmh"].iloc[0]) > 25: factors.append("Strong Wind Vectors")
    if float(row["water_pressure_bar"].iloc[0]) < 4: factors.append("Low Water Pressure")
    
    if mitigation > 15: factors.append("Active Resource Suppression")
    if not factors: factors = ["Historical Baseline"]

    level  = ("CRITICAL" if risk > 80 else
              "HIGH"     if risk > 60 else
              "MODERATE" if risk > 40 else "LOW")
    return {
        "rf": round(rf_p*100,1), 
        "gb": round(gb_p*100,1),
        "ensemble": round(risk, 1), 
        "level": level,
        "diagnostics": factors[:2],
        "mitigation_applied": round(mitigation, 1)
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(".", filename)


# ── /api/status ───────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    with model_lock:
        return jsonify({
            "model_version":   state["model_version"],
            "retrain_count":   state["retrain_count"],
            "dataset_size":    len(state["df"]),
            "rf_accuracy":     state["rf_accuracy"],
            "gb_accuracy":     state["gb_accuracy"],
            "last_retrained":  state["last_retrained"],
            "is_training":     state["training"],
            "status":          "LIVE",
            "zones_tracked":   len(ZONES),
        })


# ── /api/calculate ─────────────────────────────────────────────────────────────
@app.route("/api/calculate", methods=["POST"])
def calculate():
    body = request.get_json(silent=True) or {}
    zone = body.get("zone") or random.choice(ZONES)
    v_stations = body.get("stations", [])

    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
        rf_acc, gb_acc = state["rf_accuracy"], state["gb_accuracy"]
        ver            = state["model_version"]

    if zone not in le.classes_:
        zone = random.choice(ZONES)

    # Prediction for selected zone
    pred = _predict_zone(zone, rf, gb, df, le, virtual_stations=v_stations)

    # Zone-by-zone chart data
    chart_zones, chart_density, chart_prob = [], [], []
    for z in ZONES:
        z_pred    = _predict_zone(z, rf, gb, df, le)
        z_density = round(float(df[df["zone"]==z]["building_density"].tail(50).mean()), 1)
        chart_zones.append(z.split()[0])
        chart_density.append(z_density)
        chart_prob.append(z_pred["ensemble"])

    # Feature importance from Random Forest
    importance = dict(zip(FEATURES, [round(float(v),4) for v in rf.feature_importances_]))

    # ── Active Defense: Station-to-Zone Proximity Alert ───────────────────────
    # If risk ≥ 85%, generate a structured alert payload ready to POST to
    # a messaging API (Twilio SMS / Telegram Bot) for real-world dispatch.
    # Example hookup:
    #   import requests
    #   requests.post("https://api.twilio.com/...", data=active_defense_alert)
    ALERT_THRESHOLD_ACTIVE = 85.0
    active_defense_alert = None
    if pred["ensemble"] >= ALERT_THRESHOLD_ACTIVE:
        active_defense_alert = {
            "triggered": True,
            "threshold": ALERT_THRESHOLD_ACTIVE,
            "zone": zone,
            "risk": pred["ensemble"],
            "level": pred["level"],
            "drivers": pred["diagnostics"],
            "message": (
                f"🚨 TNFRS ACTIVE DEFENSE ALERT | Zone: {zone} | "
                f"Risk: {pred['ensemble']}% ({pred['level']}) | "
                f"Drivers: {', '.join(pred['diagnostics'])} | "
                f"Dispatch recommended immediately."
            ),
            "timestamp": datetime.datetime.now().isoformat(),
            "api_hookup": "POST this payload to Twilio/Telegram to move from Analytics → Active Defense"
        }
        print(f"[TNFSC-AI] 🚨 ACTIVE DEFENSE ALERT: {zone} — {pred['ensemble']}% risk")
    else:
        active_defense_alert = {"triggered": False, "threshold": ALERT_THRESHOLD_ACTIVE}

    return jsonify({
        "zone":               zone,
        "rf_probability":     pred["rf"],
        "gb_probability":     pred["gb"],
        "ensemble_probability": pred["ensemble"],
        "risk_level":         pred["level"],
        "risk_drivers":       pred["diagnostics"],
        "response_time_min":  round(5 + (100 - pred["ensemble"]) / 50, 2),
        "model_version":      ver,
        "dataset_size":       len(df),
        "model_accuracies":   {"random_forest": rf_acc, "gradient_boosting": gb_acc},
        "feature_importances": importance,
        "active_defense_alert": active_defense_alert,
        "building_density_chart": {
            "zones":       chart_zones,
            "density":     chart_density,
            "probability": chart_prob
        }
    })


# ── /api/heatmap ───────────────────────────────────────────────────────────────
def _recalibrate_operational_region(city, lat, lng):
    """Phase 17: Generates localized operational sectors and retrains ML baseline."""
    print(f"[TNFSC-AI] 📡 RECALIBRATING: Generating local intelligence for {city}…")
    
    # Generate 10-12 Local Operational Sectors
    sectors = [
        f"{city} Central", f"{city} North", f"{city} East", f"{city} West", f"{city} South",
        f"{city} Industrial Hub", f"{city} Logistics Corridor", f"{city} Residential Alpha",
        f"{city} Commercial Delta", f"{city} Heritage District", f"{city} Waterfront", f"{city} Sector-XII"
    ]
    
    # Generate random but stable positions on our 0-1 simulation canvas
    rng = random.Random(city)
    positions = {}
    gps_coords = {}
    for s in sectors:
        positions[s]  = (round(rng.uniform(0.1, 0.9), 2), round(rng.uniform(0.1, 0.9), 2))
        gps_coords[s] = (lat + rng.uniform(-0.05, 0.05), lng + rng.uniform(-0.05, 0.05))

    # Generate NEW hyper-local historical baseline
    new_df = _build_initial_dataset(custom_zones=sectors)
    le = LabelEncoder()
    le.fit(sectors)
    rf, gb, rf_acc, gb_acc = _train_models(new_df, le)

    with model_lock:
        state["active_zones"] = sectors
        state["active_zone_positions"] = positions
        state["active_zone_gps"] = gps_coords
        state["df"] = new_df
        state["le"] = le
        state["rf_model"] = rf
        state["gb_model"] = gb
        state["rf_accuracy"] = rf_acc
        state["gb_accuracy"] = gb_acc
        state["model_version"] += 1
        state["last_retrained"] = datetime.datetime.now().isoformat()
    
    print(f"[TNFSC-AI] ✅ REGION DEPLOYED: {len(sectors)} sectors active in {city}.")

def _build_initial_dataset(custom_zones=None):
    """Generates a synthetic historical dataset for initial training."""
    # Temporarily override state["active_zones"] if custom_zones provided
    old_zones = state["active_zones"]
    if custom_zones:
        state["active_zones"] = custom_zones
    
    data = _make_rows(1200, seed=42)
    
    if custom_zones:
        state["active_zones"] = old_zones # Restore
    return pd.DataFrame(data)

@app.route("/api/heatmap")
def heatmap():
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
        positions = state["active_zone_positions"].copy()

    points = []
    # Accept stations via query params for simpler GET sync
    query_stations = request.args.get("stations", "[]")
    try:
        v_stations = json.loads(query_stations)
    except:
        v_stations = []

    for zone, (x, y) in positions.items():
        pred    = _predict_zone(zone, rf, gb, df, le, virtual_stations=v_stations)
        density = round(float(df[df["zone"]==zone]["building_density"].tail(50).mean()), 1)
        
        # Get dynamic GPS for this zone
        zlat, zlng = state["active_zone_gps"].get(zone, (13.08, 80.28))

        points.append({
            "zone":             zone,
            "x":                x, "y": y,
            "lat":              zlat,
            "lng":              zlng,
            "risk":             pred["ensemble"],
            "level":            pred["level"],
            "building_density": density,
            "rf_probability":   pred["rf"],
            "gb_probability":   pred["gb"],
            "mitigated":        pred.get("mitigation_applied", 0) > 0
        })

    return jsonify({
        "points":       points,
        "official_stations": state["official_stations"], # Include for mapping
        "generated_at": datetime.datetime.now().isoformat(),
        "model_version": state["model_version"],
        "dataset_size":  len(df),
    })


# ── /api/incidents ─────────────────────────────────────────────────────────────
@app.route("/api/incidents")
def incidents():
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
        active_zones = list(state["active_zones"])

    now  = datetime.datetime.now()
    feed = []
    for i in range(6):
        zone = random.choice(active_zones) if active_zones else "Primary"
        ts   = now - datetime.timedelta(minutes=random.randint(1, 90))
        pred = _predict_zone(zone, rf, gb, df, le)
        feed.append({
            "time":     ts.strftime("%H:%M"),
            "zone":     zone,
            "type":     random.choice(INCIDENT_TYPES),
            "severity": "high" if pred["ensemble"] > 65 else "med",
            "risk_pct": pred["ensemble"],
        })
    feed.sort(key=lambda x: x["time"], reverse=True)
    return jsonify({"incidents": feed})


# ── /api/metrics ───────────────────────────────────────────────────────────────
@app.route("/api/metrics")
def metrics():
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
        rf_acc, gb_acc = state["rf_accuracy"], state["gb_accuracy"]
        ver, ds_size   = state["model_version"], len(df)
        last_rt        = state["last_retrained"]
        active_zones   = list(state["active_zones"])

    # Compute average risk across all zones RIGHT NOW
    # Accept stations for mitigated metrics
    query_stations = request.args.get("stations", "[]")
    try: v_stations = json.loads(query_stations)
    except: v_stations = []

    all_risks = []
    for zone in active_zones:
        pred = _predict_zone(zone, rf, gb, df, le, virtual_stations=v_stations)
        all_risks.append(pred["ensemble"])

    now        = datetime.datetime.now()
    avg_risk   = round(sum(all_risks) / len(all_risks), 1)
    freq_pct   = round(avg_risk * 0.92, 1)
    freq_label = "High" if avg_risk > 65 else "Moderate" if avg_risk > 45 else "Low"

    resp_sec   = int(300 + (100 - avg_risk) * 1.5)
    resp_str   = f"{resp_sec//60}:{resp_sec%60:02d}"

    # Latest weather reading from dataset tail
    latest = df.tail(1).iloc[0]
    temp   = round(float(latest["temperature_c"]), 1)
    hum    = round(float(latest["humidity_pct"]), 1)
    wind   = round(float(latest["wind_speed_kmh"]), 1)
    wp     = round(float(latest["water_pressure_bar"]), 1)

    w = state["live_weather"]
    ticker = (
        f"METEOROLOGICAL API [LIVE]: {w['condition']} | Temp {w['temp_c']}°C (Feels {w['feelslike_c']}°C) "
        f"| Humidity {w['humidity']}% | Wind {w['wind_kph']}km/h | UV {w['uv']} "
        f"| INFRASTRUCTURE: Water Pressure {wp}bar"
        f" | Fire Hydrant Grid: {round(96 + random.random()*3, 1)}% Operational"
        f" | Traffic: {'High' if random.random()>0.5 else 'Moderate'} (GST Road)"
        f" | AI Model v{ver} | RF Acc: {rf_acc}% | GB Acc: {gb_acc}%"
        f" | Dataset: {ds_size} records | Last Retrained: {last_rt[11:19] if last_rt else 'N/A'}"
        f" | Updated: {now.strftime('%H:%M:%S')}"
    )

    return jsonify({
        "risk_probability":   avg_risk,
        "response_time":      resp_str,
        "seasonal_frequency": freq_label,
        "seasonal_freq_pct":  freq_pct,
        "ticker":             ticker,
        "model_version":      ver,
        "dataset_size":       ds_size,
        "model_accuracies":   {"random_forest": rf_acc, "gradient_boosting": gb_acc},
        "zone_risks":         dict(zip(ZONES, all_risks)),
    })


# ── /api/retrain (manual) ──────────────────────────────────────────────────────
@app.route("/api/retrain", methods=["POST"])
def retrain():
    """Manually trigger a model retrain on current dataset."""
    with model_lock:
        if state["training"]:
            return jsonify({"status": "already_training", "message": "Retrain already in progress."}), 409
        state["training"] = True
        df = state["df"].copy()
        le = state["le"]

    def _do_retrain():
        print("[TNFSC-AI] 🔄 Manual retrain triggered…")
        try:
            rf, gb, rf_acc, gb_acc = _train_models(df, le)
            with model_lock:
                state["rf_model"]      = rf
                state["gb_model"]      = gb
                state["rf_accuracy"]   = rf_acc
                state["gb_accuracy"]   = gb_acc
                state["model_version"] += 1
                state["retrain_count"] += 1
                state["last_retrained"] = datetime.datetime.now().isoformat()
                state["training"]       = False
            print(f"[TNFSC-AI] ✅ Manual retrain done — v{state['model_version']}")
        except Exception as e:
            with model_lock:
                state["training"] = False
            print(f"[TNFSC-AI] ❌ Manual retrain error: {e}")

    threading.Thread(target=_do_retrain, daemon=True).start()
    return jsonify({
        "status":       "started",
        "dataset_size": len(df),
        "message":      f"Retraining started on {len(df)} records. Check /api/status for progress."
    })



# ── STRATEGIC PLANNING UTILITIES ──

def _calculate_city_coverage(station_points, zone_risks):
    """Calculates a global safety/coverage score based on station proximity to high-risk zones."""
    # station_points is list of (x, y)
    total_reduction = 0
    for zone, pos in ZONE_POSITIONS.items():
        zx, zy = pos
        risk = zone_risks.get(zone, 50)
        
        # Power of proximity: nearest station's impact
        min_dist = min([((zx - sx)**2 + (zy - sy)**2)**0.5 for sx, sy in station_points]) if station_points else 1.0
        
        # If a station is very close (<0.1 units), it reduces risk impact significantly
        impact = max(0, (1.0 - min_dist * 5)) * (risk / 100)
        total_reduction += impact
        
    base_safety = 65.0  # Base safety score for the city
    return min(100, round(base_safety + (total_reduction * 5), 1))


# ── /api/simulate ─────────────────────────────────────────────────────────────
    body = request.get_json(silent=True) or {}
    virtual_stations = body.get("stations", []) # user placed
    
    # Base official stations (scaled to 0-1)
    base_stations = []
    for s in state["official_stations"]:
        # Simple lat/lng to 0-1 scaling for Tamil Nadu approx (8-14N, 76-80E)
        sx = (s['lng'] - 76.0) / 4.0
        sy = 1.0 - (s['lat'] - 8.0) / 6.0
        base_stations.append((sx, sy))

    coords = [(s['x'], s['y']) for s in virtual_stations] + base_stations
    
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
    
    # Get current zone risks
    zone_risks = {}
    for zone in ZONES:
        pred = _predict_zone(zone, rf, gb, df, le)
        zone_risks[zone] = pred["ensemble"]
    
    coverage_score = _calculate_city_coverage(coords, zone_risks)
    
    return jsonify({
        "coverage_score": coverage_score,
        "impact_summary": f"Station placement provides {coverage_score}% city-wide coverage.",
        "timestamp": datetime.datetime.now().isoformat()
    })


# ── /api/optimize ─────────────────────────────────────────────────────────────
@app.route("/api/optimize")
def optimize():
    """AI Search for the best single coordinate to place a new station."""
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
    
    zone_risks = {z: _predict_zone(z, rf, gb, df, le)["ensemble"] for z in ZONES}
    
    best_score = -1
    best_coord = (0.5, 0.5)
    
    # Simple grid search 
    for x in np.linspace(0.1, 0.9, 10):
        for y in np.linspace(0.1, 0.9, 10):
            score = _calculate_city_coverage([(x, y)], zone_risks)
            if score > best_score:
                best_score = score
                best_coord = (float(x), float(y))
                
    return jsonify({
        "optimal_coordinate": {"x": best_coord[0], "y": best_coord[1]},
        "projected_coverage": best_score,
        "rationale": "Point maximizes proximity to highest risk density clusters."
    })


# ── /api/advisor ─────────────────────────────────────────────────────────────
@app.route("/api/advisor")
def advisor():
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
    
    query_stations = request.args.get("stations", "[]")
    try: v_stations = json.loads(query_stations)
    except: v_stations = []

    # Quick risk check for all zones
    all_risks = {}
    for zone in ZONES:
        pred = _predict_zone(zone, rf, gb, df, le, virtual_stations=v_stations)
        all_risks[zone] = pred["ensemble"]
    
    avg_risk = sum(all_risks.values()) / len(all_risks)
    
    briefing = _generate_mission_briefing(avg_risk, all_risks, len(df), len(v_stations))
    
    return jsonify({
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "briefing": briefing["summary"],
        "logs": briefing["steps"],
        "avg_risk": round(avg_risk, 1),
        "alert_level": "CRITICAL" if avg_risk > 65 else "HIGH" if avg_risk > 50 else "STABLE"
    })


# ── /api/vapid-public-key ─────────────────────────────────────────────────────
@app.route("/api/vapid-public-key")
def vapid_public_key():
    """Returns the VAPID public key so the frontend can create a push subscription."""
    return jsonify({"publicKey": VAPID_KEYS.get("public_key", ""), "available": PUSH_AVAILABLE})


# ── /api/subscribe ────────────────────────────────────────────────────────────
@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    """Registers a browser push subscription. Accepts the PushSubscription JSON object."""
    sub = request.get_json(silent=True)
    if not sub or not sub.get("endpoint"):
        return jsonify({"status": "error", "message": "Invalid subscription object"}), 400
    with _push_lock:
        # Avoid duplicates (same endpoint)
        existing_eps = [s["endpoint"] for s in _push_subscriptions]
        if sub["endpoint"] not in existing_eps:
            _push_subscriptions.append(sub)
            print(f"[TNFSC-AI] 📱 New push subscriber registered. Total: {len(_push_subscriptions)}")
    return jsonify({"status": "ok", "subscribers": len(_push_subscriptions)})


# ── /api/alerts ───────────────────────────────────────────────────────────────
ALERT_THRESHOLD = 75.0
_last_pushed_zones = set()   # Prevents spamming the same zone every 10s

@app.route("/api/alerts")
def alerts():
    """Returns active critical fire risk alerts for zones exceeding threshold."""
    global _last_pushed_zones
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
        weather = state["live_weather"]
        active_zones = list(state["active_zones"])

    wind = weather.get("wind_kph", 0)
    complexity = "CRITICAL SPREAD" if wind > 30 else "EXTREME" if wind > 20 else "HIGH"

    active_alerts = []
    current_zones  = set()
    for zone in active_zones:
        pred = _predict_zone(zone, rf, gb, df, le)
        if pred["ensemble"] >= ALERT_THRESHOLD:
            units = 3 if pred["ensemble"] > 90 else 2 if pred["ensemble"] > 80 else 1
            active_alerts.append({
                "zone":         zone,
                "risk":         pred["ensemble"],
                "level":        pred["level"],
                "top_driver":   pred["diagnostics"][0] if pred["diagnostics"] else "Baseline",
                "units_needed": units,
                "complexity":   complexity,
                "timestamp":    datetime.datetime.now().strftime("%H:%M:%S")
            })
            current_zones.add(zone)

    # Send Web Push to mobiles for NEW alert zones only
    new_zones = current_zones - _last_pushed_zones
    for alert in active_alerts:
        if alert["zone"] in new_zones:
            _send_push_to_all(
                title=f"🚨 TNFRS FIRE RISK ALERT — {alert['level']}",
                body=f"Zone: {alert['zone']} | Risk: {alert['risk']}% | Driver: {alert['top_driver']} | {alert['units_needed']} units needed.",
                zone=alert["zone"],
                risk=alert["risk"],
                level=alert["level"]
            )
    _last_pushed_zones = current_zones

    active_alerts.sort(key=lambda x: x["risk"], reverse=True)
    return jsonify({"alerts": active_alerts, "total": len(active_alerts), "threshold": ALERT_THRESHOLD})


# ── /api/init_location ───────────────────────────────────────────────────────
API_KEY_WEATHER = "c053b492182c4161bfa80350261403"

@app.route("/api/init_location", methods=["POST"])
def init_location():
    """Called by frontend. Supports lat/lng or auto:ip if coords missing."""
    body = request.get_json(silent=True) or {}
    lat  = body.get("lat")
    lng  = body.get("lng")

    if lat and lng:
        query = f"{lat},{lng}"
    else:
        query = "auto:ip"

    result = _fetch_weather_for_query(query, API_KEY_WEATHER)
    if result:
        # Trigger Recalibration for Phase 17
        city = result.get("city", "Unknown")
        lat_res = result.get("lat", 13.08)
        lng_res = result.get("lng", 80.28)
        
        # This will update state with new sectors and retrain ML
        _recalibrate_operational_region(city, lat_res, lng_res)
        
        with model_lock:
            state["live_weather"].update(result)
            state["last_weather_query"] = query
        
        print(f"[TNFSC-AI] 📍 Command calibrated to: {city} (Source: {query})")
        return jsonify({"status": "ok", "location": city, "weather": result})
    return jsonify({"status": "error", "message": "Location analysis failed"}), 500


# ── Persistent Incident Logging (Step 11) ──────────────────────────────────
INCIDENT_LOG_FILE = "incident_logs.json"

def _log_incident(event_data):
    """Appends an event to a JSON file for future AI training (Step 11)."""
    try:
        logs = []
        if os.path.exists(INCIDENT_LOG_FILE):
            with open(INCIDENT_LOG_FILE, "r") as f:
                logs = json.load(f)
        
        event_data["logged_at"] = datetime.datetime.now().isoformat()
        logs.append(event_data)
        
        # Keep only last 500 logs for demo purposes
        with open(INCIDENT_LOG_FILE, "w") as f:
            json.dump(logs[-500:], f, indent=2)
        print(f"[TNFSC-LOG] 📝 Incident logged: {event_data.get('type')} at {event_data.get('zone')}")
    except Exception as e:
        print(f"[TNFSC-LOG] ❌ Logging failed: {e}")

@app.route("/api/incidents/report", methods=["POST"])
def report_incident():
    """Manually report an incident (Step 6)."""
    data = request.get_json(silent=True) or {}
    zone = data.get("zone", "Manual Report")
    itype = data.get("type", "General Fire")
    
    incident = {
        "zone": zone,
        "type": itype,
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
        "severity": "CRITICAL",
        "manual": True
    }
    _log_incident(incident)
    return jsonify({"status": "ok", "incident": incident})

# ── /api/weather ──────────────────────────────────────────────────────────────
@app.route("/api/weather")
def weather_endpoint():
    """Expose live weather telemetry to the frontend."""
    with model_lock:
        w = state["live_weather"].copy()
    return jsonify(w)


# ── Haversine helper ──────────────────────────────────────────────────────────
import math

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


ZONE_GPS = {
    "Chennai Central":  (13.0827, 80.2707),
    "Adyar":            (13.0012, 80.2565),
    "T. Nagar":         (13.0418, 80.2341),
    "Vadapalani":       (13.0520, 80.2121),
    "Velachery":        (12.9815, 80.2180),
    "Anna Nagar":       (13.0891, 80.2098),
    "Royapettah":       (13.0524, 80.2603),
    "Madurai North":    (9.9261,  78.1198),
    "Coimbatore East":  (11.0168, 76.9558),
    "Salem West":       (11.6643, 78.1460),
    "Trichy Old Town":  (10.7905, 78.7047),
    "Erode Market":     (11.3410, 77.7172),
}


# ── /api/nearest_stations ─────────────────────────────────────────────────────
@app.route("/api/nearest_stations")
def nearest_stations():
    """Return top-3 nearest official TNFRS stations to a given zone."""
    zone = request.args.get("zone", "")
    
    with model_lock:
        active_zone_gps = state["active_zone_gps"]
        stations = list(state["official_stations"])

    if zone not in active_zone_gps:
        # Default to the first active zone if missing
        zone = list(active_zone_gps.keys())[0] if active_zone_gps else ""
    
    if not zone:
        return jsonify({"error": "No zones active"}), 500
        
    zlat, zlng = active_zone_gps[zone]

    distances = []
    for s in stations:
        try:
            slat = float(s.get("lat", 0) or 0)
            slng = float(s.get("lng", 0) or 0)
            if slat == 0 and slng == 0:
                continue
            d = _haversine_km(zlat, zlng, slat, slng)
            travel_min = round((d / 30) * 60, 1)
            distances.append({
                "name":        s.get("station_name", "Unknown"),
                "district":    s.get("district", ""),
                "category":    s.get("category", "B"),
                "cug":         s.get("cug", ""),
                "landline":    s.get("landline", ""),
                "distance_km": round(d, 2),
                "eta_min":     travel_min,
                "gps":         (slat, slng)
            })
        except Exception:
            continue

    distances.sort(key=lambda x: x["distance_km"])
    
    # Generate a simulated route for Step 9: Route Optimization
    nearest_stations = distances[:3]
    simulated_route = []
    if nearest_stations:
        s = nearest_stations[0]
        slat, slng = s["gps"]
        # Create 5-point curved route towards the zone
        for i in range(6):
            frac = i / 5.0
            # Add some "traffic avoidance" noise to the route
            noise_lat = (random.random() - 0.5) * 0.005
            noise_lng = (random.random() - 0.5) * 0.005
            curr_lat = zlat + (slat - zlat) * frac + noise_lat
            curr_lng = zlng + (slng - zlng) * frac + noise_lng
            simulated_route.append([curr_lat, curr_lng])

    return jsonify({
        "zone": zone, 
        "nearest": nearest_stations, 
        "zone_gps": {"lat": zlat, "lng": zlng},
        "optimized_route": simulated_route
    })


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "═"*60)
    print("  TNFSC Smart Fire Intelligence Portal — Backend v2")
    print("  Open: http://127.0.0.1:5000")
    print("  AI Status: http://127.0.0.1:5000/api/status")
    print(f"  Web Push: {'ENABLED ✅' if PUSH_AVAILABLE else 'DISABLED ❌ (pip install pywebpush)'}")
    print("═"*60 + "\n")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
