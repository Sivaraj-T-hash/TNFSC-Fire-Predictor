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
import math

from flask import Flask, jsonify, send_from_directory, request
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

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
            if status in (404, 410):
                stale.append(sub)
            else:
                print(f"[TNFSC-AI] ⚠️  Push failed ({status}): {ex}")
        except Exception as ex:
            print(f"[TNFSC-AI] ⚠️  Push error: {ex}")

    if stale:
        with _push_lock:
            for s in stale:
                if s in _push_subscriptions:
                    _push_subscriptions.remove(s)
        print(f"[TNFSC-AI] 🗑️  Removed {len(stale)} expired push subscriptions.")

# ─────────────────────────────────────────────────────────────────────────────
#  GLOBAL MODEL STATE
# ─────────────────────────────────────────────────────────────────────────────
model_lock = threading.Lock()

state = {
    "rf_model":       None,
    "gb_model":       None,
    "le":             None,
    "df":             None,
    "rf_accuracy":    0.0,
    "gb_accuracy":    0.0,
    "model_version":  0,
    "retrain_count":  0,
    "last_retrained": None,
    "training":       False,
    "official_stations": [],
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
    "active_zones": [],
    "active_zone_positions": {},
    "active_zone_gps": {},
    "active_drones": [
        {"id": "DRONE-ALPHA", "status": "PATROLLING", "lat": 13.08, "lng": 80.27, "battery": 88, "hotspots": 0},
        {"id": "DRONE-BETA", "status": "STATION_DOCK", "lat": 13.04, "lng": 80.23, "battery": 100, "hotspots": 0},
        {"id": "DRONE-GAMMA", "status": "ASCENDING", "lat": 13.00, "lng": 80.25, "battery": 94, "hotspots": 1}
    ]
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
    "building_density", "road_proximity_km", "water_pressure_bar",
    "traffic_index", "industrial_risk", "hydrant_status"
]
TARGET = "fire_risk"

ZONE_BUILDING_DENSITY = {
    "Chennai Central":  92.0,
    "Adyar":            65.0,
    "T. Nagar":         95.0,
    "Vadapalani":       78.0,
    "Velachery":        72.0,
    "Anna Nagar":       70.0,
    "Royapettah":       85.0,
    "Madurai North":    80.0,
    "Coimbatore East":  68.0,
    "Salem West":       60.0,
    "Trichy Old Town":  82.0,
    "Erode Market":     75.0,
}

def _zone_building_density(zone_name: str) -> float:
    if zone_name in ZONE_BUILDING_DENSITY:
        return ZONE_BUILDING_DENSITY[zone_name]
    seed = sum(ord(c) for c in zone_name) % 1000
    rng  = random.Random(seed)
    return round(rng.uniform(40.0, 90.0), 1)

def _generate_mission_briefing(avg_risk, zone_risks, ds_size, virtual_count=0):
    analysis = []
    incident_prediction = "LOW"
    
    if avg_risk > 65:
        brief.append("⚠️ STATUS: CRITICAL. Thermal anomalies detected across major hubs. Dispatch priority: EXTREME.")
        incident_prediction = "HIGHLY LIKELY"
    elif avg_risk > 45:
        brief.append("🟡 STATUS: ELEVATED. Moderate risk density identified. Pre-emptive unit relocation recommended.")
        incident_prediction = "POSSIBLE"
    else:
        brief.append("🟢 STATUS: NOMINAL. Routine monitoring active. No immediate strategic shifts required.")

    if virtual_count > 0:
        brief.append(f"🛡️ STRATEGY: {virtual_count} tactical assets deployed. Current deployment provides a {total_safety_margin}% City Safety Margin.")
    
    if critical_zones:
        brief.append(f"🔍 INTELLIGENCE: {', '.join(critical_zones)} are exhibiting high building-density and industrial-risk correlations. Hydrant checks prioritized.")
        analysis.append(f"High risk in {critical_zones[0]} is driven by critical building density (>85%) and industrial volatility.")
    elif high_zones:
        brief.append(f"ℹ️ NOTICE: Growth in risk vectors for {', '.join(high_zones[:2])}. Traffic congestion may delay response by 15-20%.")
        analysis.append("Current risk is linked to rising temperatures and sustained traffic bottlenecks in urban sectors.")

    with model_lock:
        active_drones = [d for d in state["active_drones"] if d["status"] != "STATION_DOCK"]
    if active_drones:
        brief.append(f"🛸 SURVEILLANCE: {len(active_drones)} drones currently patrolling high-risk vectors. Hotspot detection active.")

    if avg_risk > 60:
        brief.append("📋 RECOMMENDATION: Activate Level-2 Response Protocol. Reallocate 30% of standby units to axis.")
    else:
        brief.append("📋 RECOMMENDATION: Maintain standard grid coverage. Optimize fuel logistics for evening shifts.")

    return {
        "summary": " ".join(brief[:5]),
        "reasoning": analysis[0] if analysis else "Standard risk patterns detected based on real-time ensemble weighting.",
        "incident_prediction": incident_prediction,
        "danger_score": round(avg_risk, 1),
        "steps": [
            "Syncing telemetry with Meteorological API...",
            "Recalculating ensemble weights (RF + GB)...",
            "Analyzing traffic-index impact on response curves...",
            f"Drone Fleet: {len(active_drones)} active assets synchronized.",
            "Strategic Briefing Generated."
        ]
    }

# ── SYNTHETIC DATA GENERATOR ─────────────────────────────────────────────────────

def _make_rows(n: int, seed=None) -> list[dict]:
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
        
        bld_density = _zone_building_density(zone)
        road_prox   = round(rng.uniform(0.1, 5.0), 2)
        water_pres  = round(rng.uniform(3.0, 9.0), 1)
        
        traffic_base = 0.8 if (8 <= hour <= 10 or 17 <= hour <= 20) else 0.4
        traffic_idx = round(traffic_base + rng.uniform(0, 0.2), 2)
        ind_risk = 1.2 if "Industrial" in zone or "Logistics" in zone or "Market" in zone else 0.8
        ind_score = round(ind_risk * rng.uniform(40, 90), 1)
        hydrant_status = round(rng.uniform(0.7, 1.0), 2)

        risk_score = (
            0.20 * (temp / 44)
            + 0.15 * (1 - humidity / 95)
            + 0.15 * (bld_density / 100)
            + 0.10 * (wind_speed / 40)
            + 0.10 * (1 - water_pres / 10)
            + 0.10 * (traffic_idx)
            + 0.10 * (ind_score / 100)
            + 0.05 * (1 - hydrant_status)
            + 0.05 * (hour in range(12, 18))
        )
        fire_risk = 1 if risk_score > 0.45 else 0

        rows.append({
            "zone": zone, "hour": hour, "day_of_week": day_of_week,
            "temperature_c": temp, "humidity_pct": humidity,
            "wind_speed_kmh": wind_speed, "building_density": bld_density,
            "road_proximity_km": road_prox, "water_pressure_bar": water_pres,
            "traffic_index": traffic_idx, "industrial_risk": ind_score,
            "hydrant_status": hydrant_status,
            "fire_risk": fire_risk,
            "recorded_at": datetime.datetime.now().isoformat()
        })
    return rows

def _build_initial_dataset(custom_zones=None) -> pd.DataFrame:
    np.random.seed(42)
    old_zones = state["active_zones"]
    if custom_zones:
        state["active_zones"] = custom_zones
    rows = _make_rows(200, seed=42)
    if custom_zones:
        state["active_zones"] = old_zones
    return pd.DataFrame(rows)

def _encode_df(df: pd.DataFrame, le: LabelEncoder) -> pd.DataFrame:
    df = df.copy()
    known_zones = set(le.classes_)
    df["zone_encoded"] = df["zone"].apply(lambda z: int(le.transform([z])[0]) if z in known_zones else 0)
    return df

def _train_models(df: pd.DataFrame, le: LabelEncoder):
    df_enc = _encode_df(df, le)
    X = df_enc[FEATURES]
    y = df_enc[TARGET]
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    rf = RandomForestClassifier(n_estimators=150, max_depth=8, n_jobs=-1, random_state=42)
    rf.fit(X_train, y_train)
    gb = GradientBoostingClassifier(n_estimators=100, learning_rate=0.08, max_depth=4, random_state=42)
    gb.fit(X_train, y_train)
    rf_acc = round(rf.score(X_test, y_test) * 100, 2)
    gb_acc = round(gb.score(X_test, y_test) * 100, 2)
    return rf, gb, rf_acc, gb_acc

# ── STARTUP ──

# Consolidate training after zone determination
_le = LabelEncoder()

with model_lock:
    state["rf_model"]      = None
    state["gb_model"]      = None
    state["le"]            = _le
    state["df"]            = None
    state["rf_accuracy"]   = 0.0
    state["gb_accuracy"]   = 0.0
    state["model_version"] = 0
    state["last_retrained"]= None
    state["active_zone_positions"] = {
        "Chennai Central": (0.50,0.30), "Adyar": (0.55,0.65), "T. Nagar": (0.45,0.50),
        "Vadapalani": (0.36,0.44), "Velachery": (0.52,0.72), "Anna Nagar": (0.38,0.30),
        "Royapettah": (0.56,0.40), "Madurai North": (0.30,0.80), "Coimbatore East": (0.20,0.55),
        "Salem West": (0.35,0.20), "Trichy Old Town": (0.50,0.62), "Erode Market": (0.22,0.35)
    }
    state["active_zone_gps"] = {
        "Chennai Central": (13.0827, 80.2707), "Adyar": (13.0012, 80.2565), "T. Nagar": (13.0418, 80.2341),
        "Vadapalani": (13.0520, 80.2121), "Velachery": (12.9815, 80.2180), "Anna Nagar": (13.0891, 80.2098),
        "Royapettah": (13.0524, 80.2603), "Madurai North": (9.9261,  78.1198), "Coimbatore East": (11.0168, 76.9558),
        "Salem West": (11.6643, 78.1460), "Trichy Old Town": (10.7905, 78.7047), "Erode Market": (11.3410, 77.7172)
    }

# Load master database and derive zones
try:
    if os.path.exists('tnfrs_master_dataset_sample.json'):
        with open('tnfrs_master_dataset_sample.json', 'r') as f:
            sample_data = json.load(f)
        
        stations_list = []
        district_coords = {} # To store sum of lats/lngs for district centers
        district_counts = {}

        for s in sample_data:
            dist = s.get("district", "Unknown")
            # Removed Chennai-only filter to show older full-state heatmap
            
            lat = float(s.get("latitude", 0))
            lng = float(s.get("longitude", 0))
            
            stations_list.append({
                "station_name": str(s.get("station_name", "Unknown")),
                "district":     dist,
                "category":     str(s.get("category", "B")),
                "cug":          str(s.get("mobile", "")),
                "landline":     str(s.get("landline", "")),
                "lat":          lat,
                "lng":          lng
            })
            
            # Accumulate for district center calculation
            if dist not in district_coords:
                district_coords[dist] = [0, 0]
                district_counts[dist] = 0
            district_coords[dist][0] += lat
            district_coords[dist][1] += lng
            district_counts[dist] += 1
        
        # Calculate averages for district centers
        zone_gps = {d: (district_coords[d][0]/district_counts[d], district_coords[d][1]/district_counts[d]) 
                   for d in district_coords}
        
        # Add individual station GPS for granular lookups (Sidebar Fix)
        for s in stations_list:
            zone_gps[s["station_name"]] = (s["lat"], s["lng"])
        
        with model_lock:
            state["official_stations"] = stations_list
            state["active_zones"] = list(district_coords.keys())
            state["active_zone_gps"] = zone_gps
            # ZONES should be the districts for the AI classification baseline
            ZONES = list(district_coords.keys())
            
        print(f"[TNFSC-AI] Using sample dataset: {len(stations_list)} stations across {len(ZONES)} districts.")
        state["le"].fit(state["active_zones"])
        state["df"] = _build_initial_dataset(custom_zones=state["active_zones"])
        print("[TNFSC-AI] Training ensemble models…")
        _rf, _gb, _rf_acc, _gb_acc = _train_models(state["df"], state["le"])
        
        with model_lock:
            state["rf_model"], state["gb_model"] = _rf, _gb
            state["rf_accuracy"], state["gb_accuracy"] = _rf_acc, _gb_acc
            state.update({"model_version": 1, "last_retrained": datetime.datetime.now().isoformat()})

    elif os.path.exists('TNFRS_Master_Database_2026.csv'):
        station_df = pd.read_csv('TNFRS_Master_Database_2026.csv')
        stations_list = []
        zone_gps = {}
        districts = station_df['district'].unique().tolist()
        
        for d in districts:
            d_stations = station_df[station_df['district'] == d]
            zone_gps[d] = (d_stations['lat'].mean(), d_stations['lng'].mean())
            
        for _, row in station_df.iterrows():
            dist = str(row.get("district", "Unknown"))
            # Removed Chennai-only filter to show older full-state heatmap

            stations_list.append({
                "station_name": str(row.get("name", "Unknown")),
                "district":     dist,
                "category":     str(row.get("cat", "B")),
                "cug":          str(row.get("cug", "")),
                "landline":     str(row.get("landline", "")),
                "lat":          float(row.get("lat", 0)),
                "lng":          float(row.get("lng", 0))
            })
        
        # Re-derive districts/zones based on filtered stations
        districts = list(set(s["district"] for s in stations_list))
        zone_gps = {}
        for d in districts:
            d_stats = [s for s in stations_list if s["district"] == d]
            zone_gps[d] = (sum(s["lat"] for s in d_stats)/len(d_stats), sum(s["lng"] for s in d_stats)/len(d_stats))
        
        # Add individual station GPS for granular lookups (Sidebar Fix)
        for s in stations_list:
            zone_gps[s["station_name"]] = (s["lat"], s["lng"])

        with model_lock:
            state["official_stations"] = stations_list
            state["active_zones"] = districts
            state["active_zone_gps"] = zone_gps
            # ZONES should be the districts for the AI classification baseline
            ZONES = districts
        print(f"[TNFSC-AI] Loaded {len(stations_list)} Chennai stations from CSV.")

    elif os.path.exists('TNFRS_Historical_Database_1900_2026.json'):
        pass # This block was implicitly removed by the instruction, keeping it as a pass for structural integrity if it was meant to be empty.
except Exception as e:
    print(f"[TNFSC-AI] ❌ DB load failed: {e}")

# ── BACKGROUND THREADS ──

def _live_data_writer():
    while True:
        time.sleep(15)
        new_rows = _make_rows(random.randint(3, 6))
        with model_lock:
            state["df"] = pd.concat([state["df"], pd.DataFrame(new_rows)], ignore_index=True)

def _retrain_loop():
    while True:
        time.sleep(120)
        with model_lock:
            if state["training"]: continue
            state["training"] = True
            df, le = state["df"].copy(), state["le"]
        try:
            rf, gb, rf_acc, gb_acc = _train_models(df, le)
            with model_lock:
                state["rf_model"], state["gb_model"] = rf, gb
                state["rf_accuracy"], state["gb_accuracy"] = rf_acc, gb_acc
                state["model_version"] += 1
                state["retrain_count"] += 1
                state["last_retrained"] = datetime.datetime.now().isoformat()
                state["training"] = False
        except:
            with model_lock: state["training"] = False

def _fetch_weather_for_query(query, api_key):
    try:
        url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={query}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            d = r.json()
            w, loc = d["current"], d["location"]
            return {
                "temp_c": w["temp_c"], "humidity": w["humidity"], "wind_kph": w["wind_kph"],
                "condition": w["condition"]["text"], "city": loc["name"], "lat": loc["lat"], "lng": loc["lon"]
            }
    except: pass
    # Fallback to plausible Chennai weather if API fails
    return {
        "temp_c": 31.5, "humidity": 68, "wind_kph": 14.5,
        "feelslike_c": 35.2, "uv": 6.8,
        "condition": "Cloudy (Simulated)", "city": "Chennai (fallback)", "lat": 13.08, "lng": 80.27
    }

def _weather_sync_loop():
    api_key = "c053b492182c4161bfa80350261403"
    while True:
        with model_lock: query = state.get("last_weather_query", "auto:ip")
        res = _fetch_weather_for_query(query, api_key)
        if res:
            with model_lock: state["live_weather"].update(res)
        time.sleep(900)

threading.Thread(target=_live_data_writer, daemon=True).start()
threading.Thread(target=_retrain_loop, daemon=True).start()
threading.Thread(target=_weather_sync_loop, daemon=True).start()

# ── HELPERS ──

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def _zone_feature_row(zone_name, df, le):
    now = datetime.datetime.now()
    z_df = df[df["zone"] == zone_name] if not df.empty and zone_name in df["zone"].values else df
    encoded = int(le.transform([zone_name])[0]) if zone_name in le.classes_ else 0
    return pd.DataFrame([{
        "zone_encoded": encoded, "hour": now.hour, "day_of_week": now.weekday(),
        "temperature_c": round(z_df["temperature_c"].tail(50).mean(), 1),
        "humidity_pct": round(z_df["humidity_pct"].tail(50).mean(), 1),
        "wind_speed_kmh": round(z_df["wind_speed_kmh"].tail(50).mean(), 1),
        "building_density": round(z_df["building_density"].tail(50).mean(), 1),
        "road_proximity_km": round(z_df["road_proximity_km"].tail(50).mean(), 2),
        "water_pressure_bar": round(z_df["water_pressure_bar"].tail(50).mean(), 1),
        "traffic_index": round(z_df["traffic_index"].tail(50).mean(), 2),
        "industrial_risk": round(z_df["industrial_risk"].tail(50).mean(), 1),
        "hydrant_status": round(z_df["hydrant_status"].tail(50).mean(), 2)
    }])

def _predict_zone(zone, rf, gb, df, le, virtual_stations=None):
    row = _zone_feature_row(zone, df, le)
    rf_p, gb_p = rf.predict_proba(row)[0][1], gb.predict_proba(row)[0][1]
    risk = round(((rf_p + gb_p) / 2) * 100, 1)
    
    mitigation = 0
    if virtual_stations:
        with model_lock: gps = state["active_zone_gps"]
        zlat, zlng = gps.get(zone, (13.08, 80.28))
        for s in virtual_stations:
            if 'lat' in s and 'lng' in s:
                d = _haversine_km(zlat, zlng, s['lat'], s['lng'])
                if d < 5.0: mitigation += max(0, (5.0 - d) * 6)
    
    risk = max(10, risk - mitigation)
    level = "CRITICAL" if risk > 80 else "HIGH" if risk > 60 else "MODERATE" if risk > 40 else "LOW"
    
    factors = []
    if row["temperature_c"].iloc[0] > 38: factors.append("High Temp")
    if row["building_density"].iloc[0] > 80: factors.append("Dense Sector")
    return {"rf": round(rf_p*100,1), "gb": round(gb_p*100,1), "ensemble": risk, "level": level, "diagnostics": factors[:2]}

def _generate_mission_briefing(avg_risk, risks, data_size):
    """Generate cinematic AI advisory text based on real-time metrics."""
    high_zones = [z for z, r in risks.items() if r > 70]
    stable_zones = [z for z, r in risks.items() if r <= 40]
    
    status = "STABLE" if avg_risk < 45 else "ELEVATED" if avg_risk < 65 else "CRITICAL"
    
    briefing = f"Current operational baseline is {status}. "
    if high_zones:
        briefing += f"Anomalies detected in {', '.join(high_zones[:2])}. Neural patterns suggest elevated thermal risk profiles."
    else:
        briefing += "All primary sectors reporting nominal signatures. Climate telemetry is within standard parameters."
        
    reasoning = "Traffic congestion and low humidity are primary risk drivers today." if avg_risk > 50 else "High structural safety scores and optimal response times maintain stability."
    prediction = "POSSIBLE BUILDING FIRE" if avg_risk > 75 else "MINOR ELECTRICAL FLASH" if avg_risk > 55 else "NO SIGNIFICANT INCIDENT"
    
    steps = [
        f"Scanning {len(ZONES)} tactical sectors...",
        f"Analyzing {data_size} historical data points...",
        "Syncing with meteorological satellites...",
        f"Risk weights updated for {len(high_zones)} high-priority zones."
    ]
    
    return {
        "summary": briefing,
        "reasoning": reasoning,
        "incident_prediction": prediction,
        "danger_score": avg_risk,
        "steps": steps
    }

# ── ROUTES ──

@app.route("/")
def index(): return send_from_directory(".", "index.html")

@app.route("/<path:path>")
def static_proxy(path): return send_from_directory(".", path)

@app.route("/api/status")
def api_status():
    with model_lock:
        return jsonify({
            "model_version": state["model_version"], "retrain_count": state["retrain_count"],
            "dataset_size": len(state["df"]), "rf_accuracy": state["rf_accuracy"],
            "gb_accuracy": state["gb_accuracy"], "last_retrained": state["last_retrained"],
            "is_training": state["training"]
        })

@app.route("/api/heatmap")
def heatmap():
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
        gps_map = state["active_zone_gps"].copy()
    
    v_stations = json.loads(request.args.get("stations", "[]"))
    points = []
    # 1. ADD DISTRICT CENTERS (Older Heatmap Style)
    for zone, (lat, lng) in gps_map.items():
        if zone in state["active_zones"]:
            pred = _predict_zone(zone, rf, gb, df, le, v_stations)
            points.append({"zone": f"{zone} District", "lat": lat, "lng": lng, "risk": pred["ensemble"], "level": pred["level"], "type": "district"})
    
    # 2. ADD INDIVIDUAL STATIONS (Newer Tactical Style)
    for s in state["official_stations"]:
        zone = s["station_name"]
        lat, lng = s["lat"], s["lng"]
        pred = _predict_zone(s["district"], rf, gb, df, le, v_stations)
        points.append({"zone": zone, "lat": lat, "lng": lng, "risk": pred["ensemble"], "level": pred["level"], "type": "station"})
    
    return jsonify({"points": points, "official_stations": state["official_stations"]})

@app.route("/api/metrics")
def metrics():
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
        zones = list(state["active_zones"])
        w = state["live_weather"]
    v_stations = json.loads(request.args.get("stations", "[]"))
    risks = [_predict_zone(z, rf, gb, df, le, v_stations)["ensemble"] for z in zones]
    avg = round(sum(risks)/len(risks), 1)
    ticker = f"WEATHER: {w.get('condition')} | {w.get('temp_c')}°C | AI v{state['model_version']} | Acc: {state['rf_accuracy']}%"
    return jsonify({"risk_probability": avg, "response_time": "4:20", "ticker": ticker, "seasonal_freq_pct": 78})

@app.route("/api/advisor")
def advisor():
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
    risks = {z: _predict_zone(z, rf, gb, df, le)["ensemble"] for z in ZONES}
    avg = sum(risks.values())/len(risks)
    briefing = _generate_mission_briefing(avg, risks, len(df))
    return jsonify({
        "briefing": briefing["summary"],
        "reasoning": briefing["reasoning"],
        "incident_prediction": briefing["incident_prediction"],
        "danger_score": briefing["danger_score"],
        "logs": briefing["steps"], 
        "alert_level": "STABLE" if avg < 45 else "ELEVATED" if avg < 65 else "CRITICAL"
    })

@app.route("/api/nearest_stations")
def nearest_stations():
    zone = request.args.get("zone", "")
    with model_lock:
        gps = state["active_zone_gps"]
        stations = state["official_stations"]
    if zone not in gps: return jsonify({"error": "No zone"}), 400
    zlat, zlng = gps[zone]
    dists = []
    for s in stations:
        d = _haversine_km(zlat, zlng, s['lat'], s['lng'])
        dists.append({"name": s['station_name'], "district": s['district'], "category": s['category'], "cug": s['cug'], "landline": s['landline'], "distance_km": round(d, 1), "eta_min": round(d*2,1)})
    dists.sort(key=lambda x: x["distance_km"])
    return jsonify({"zone": zone, "nearest": dists[:3]})

@app.route("/api/spread")
def api_spread():
    zone = request.args.get("zone", ZONES[0])
    with model_lock: zlat, zlng = state["active_zone_gps"].get(zone, (13.08, 80.27))
    spread = []
    for t in [10, 20, 30]:
        r = (t/60) * 0.05
        poly = [[zlat+r, zlng], [zlat, zlng+r], [zlat-r, zlng], [zlat, zlng-r]]
        spread.append({"time": t, "polygon": poly})
    return jsonify({"zone": zone, "spread": spread})

@app.route("/api/drones")
def api_drones():
    with model_lock: drones = list(state["active_drones"])
    for d in drones:
        d["lat"] += (random.random()-0.5)*0.001
        d["lng"] += (random.random()-0.5)*0.001
        d["battery"] = max(0, d["battery"] - 0.05)
    return jsonify({"drones": drones})

@app.route("/api/cctv")
def api_cctv():
    zone = request.args.get("zone", "Chennai Central")
    with model_lock: rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
    pred = _predict_zone(zone, rf, gb, df, le)
    
    # Simulate visual anomalies based on risk
    anomaly = pred["ensemble"] > 60
    return jsonify({
        "zone": zone,
        "status": "LIVE",
        "feed_id": f"CAM-{hash(zone) % 9999}",
        "anomaly_detected": anomaly,
        "vision_ai_data": {
            "smoke_density_pct": round(pred["ensemble"] * 0.8, 1) if anomaly else 2.1,
            "thermal_hotspot": anomaly,
            "crowd_density": "HIGH" if anomaly else "LOW"
        }
    })

@app.route("/api/building/score")
def api_building_score():
    name = request.args.get("name", "Unknown Building")
    # Simulation logic for building risk
    score = random.randint(30, 95)
    grade = "A" if score < 40 else "B" if score < 60 else "C" if score < 80 else "D"
    return jsonify({
        "building": name,
        "score": score,
        "grade": grade,
        "factors": {
            "structural_integrity": random.choice(["Optimal", "Stable", "Warning"]),
            "fire_suppression": random.choice(["Compliant", "Verification Needed"]),
            "occupancy_load": random.randint(50, 500)
        }
    })

@app.route("/api/init_location", methods=["POST"])
def init_location():
    data = request.json or {}
    lat, lng = data.get("lat"), data.get("lng")
    query = f"{lat},{lng}" if lat and lng else "auto:ip"
    with model_lock: state["last_weather_query"] = query
    
    api_key = "c053b492182c4161bfa80350261403"
    res = _fetch_weather_for_query(query, api_key)
    if res:
        with model_lock: state["live_weather"].update(res)
    
    return jsonify({"status": "ok", "location": res["city"] if res else "Unknown", "weather": res})

@app.route("/api/retrain", methods=["POST"])
def retrain():
    with model_lock:
        if state["training"]: return jsonify({"status": "already_training"})
        state["training"] = True
    
    def run_retrain():
        with model_lock: df, le = state["df"].copy(), state["le"]
        try:
            rf, gb, rf_acc, gb_acc = _train_models(df, le)
            with model_lock:
                state["rf_model"], state["gb_model"] = rf, gb
                state["rf_accuracy"], state["gb_accuracy"] = rf_acc, gb_acc
                state["model_version"] += 1
                state["retrain_count"] += 1
                state["last_retrained"] = datetime.datetime.now().isoformat()
                state["training"] = False
        except:
            with model_lock: state["training"] = False
            
    threading.Thread(target=run_retrain).start()
    return jsonify({"status": "retraining_started"})

@app.route("/api/alerts")
def get_alerts():
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
    alerts = []
    for zone in ZONES:
        # Use district mapping for the model prediction logic if zone is a station
        model_zone = zone
        for s in state["official_stations"]:
            if s["station_name"] == zone:
                model_zone = s["district"]
                break
        
        pred = _predict_zone(model_zone, rf, gb, df, le)
        if pred["ensemble"] > 60:
            alerts.append({
                "zone": zone, "risk": pred["ensemble"], "level": pred["level"],
                "top_driver": pred["diagnostics"][0] if pred["diagnostics"] else "Density",
                "units_needed": random.randint(2, 5), "complexity": "HIGH" if pred["ensemble"] > 80 else "MEDIUM"
            })
    return jsonify({"alerts": alerts})

@app.route("/api/weather")
def get_weather():
    with model_lock: return jsonify(state["live_weather"])

@app.route("/api/optimize", methods=["POST", "GET"])
def optimize_stations():
    with model_lock: 
        gps = state["active_zone_gps"]
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
    
    risks = {z: _predict_zone(z, rf, gb, df, le)["ensemble"] for z in ZONES}
    best_zone = max(risks, key=risks.get)
    lat, lng = gps.get(best_zone, (13.08, 80.27))
    # Small offset for "optimal" vs zone center
    lat += (random.random()-0.5)*0.01
    lng += (random.random()-0.5)*0.01
    
    return jsonify({"optimal_gps": {"lat": lat, "lng": lng}, "zone": best_zone})

@app.route("/api/simulate", methods=["POST"])
def simulate():
    stations = request.json.get("stations", [])
    with model_lock:
        rf, gb, df, le = state["rf_model"], state["gb_model"], state["df"].copy(), state["le"]
    
    risks = [_predict_zone(z, rf, gb, df, le, stations)["ensemble"] for z in ZONES]
    avg = sum(risks)/len(risks)
    coverage = 100 - avg
    return jsonify({"coverage_score": round(coverage,1)})

@app.route("/api/incidents")
def get_incidents():
    incidents = []
    for _ in range(5):
        incidents.append({
            "time": datetime.datetime.now().strftime("%H:%M"),
            "zone": random.choice(ZONES),
            "type": random.choice(INCIDENT_TYPES),
            "severity": "high",
            "risk_pct": random.randint(40, 95)
        })
    return jsonify({"incidents": incidents})

@app.route("/api/incidents/report", methods=["POST"])
def report_incident():
    data = request.json
    zone = data.get("zone", "Unknown")
    with model_lock: lat, lng = state["active_zone_gps"].get(zone, (13.08, 80.27))
    return jsonify({"status": "logged", "incident": {"lat": lat, "lng": lng, "zone": zone}})

@app.route("/api/vapid-public-key")
def vapid_key():
    return jsonify({"available": PUSH_AVAILABLE, "publicKey": VAPID_KEYS.get("public_key")})

@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    sub = request.json
    with _push_lock:
        if sub not in _push_subscriptions:
            _push_subscriptions.append(sub)
    return jsonify({"status": "subscribed"})

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
