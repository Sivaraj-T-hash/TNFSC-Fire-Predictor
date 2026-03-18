/* ═══════════════════════════════════════════════════════════════════
   TNFSC Smart Fire Intelligence Portal — Frontend Logic
   Connects to Flask backend at /api/* when served via Flask.
   Falls back to local simulation when opened as a plain file.
═══════════════════════════════════════════════════════════════════ */

const IS_FLASK = (window.location.protocol !== 'file:');

// ── Web Push — Service Worker Registration ────────────────────────────────────
// This must be at top level (outside DOMContentLoaded) so the SW registers
// as early as possible, before any interactive events run.
let _swRegistration = null;

if (IS_FLASK && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js')
        .then(reg => {
            _swRegistration = reg;
            console.log('[TNFSC] Service Worker registered:', reg.scope);
        })
        .catch(err => console.warn('[TNFSC] SW registration failed:', err));
}

// Helper: convert a base64url string to Uint8Array (VAPID public key format)
function _urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64  = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw     = window.atob(base64);
    return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
}

// Main push subscription flow
async function _subscribeToPush() {
    if (!IS_FLASK || !_swRegistration) return;
    try {
        // 1. Fetch VAPID public key from Flask
        const keyRes = await fetch('/api/vapid-public-key');
        const keyData = await keyRes.json();
        if (!keyData.available || !keyData.publicKey) {
            console.warn('[TNFSC] Push not available (pywebpush missing on server).');
            return;
        }

        // 2. Subscribe via PushManager
        const applicationServerKey = _urlBase64ToUint8Array(keyData.publicKey);
        const subscription = await _swRegistration.pushManager.subscribe({
            userVisibleOnly:      true,
            applicationServerKey: applicationServerKey
        });

        // 3. POST subscription to Flask so backend can push to this device
        await fetch('/api/subscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(subscription.toJSON())
        });

        // 4. Show the push badge in the header
        const pushBadge = document.getElementById('push-badge');
        if (pushBadge) pushBadge.style.display = 'flex';

        console.log('[TNFSC] 📱 Web Push subscription registered successfully!');
    } catch (err) {
        console.warn('[TNFSC] Push subscription failed:', err);
    }
}

console.log('[TNFSC] Script execution started');
document.addEventListener('DOMContentLoaded', () => {
    console.log('[TNFSC] DOMContentLoaded fired');
    // ── Init Lucide Icons ──────────────────────────────────────
    try {
        lucide.createIcons();
        console.log('[TNFSC] Lucide icons initialized');
    } catch (e) {
        console.error('[TNFSC] Lucide failed:', e);
    }

    // ── Global Scope State ────────────────────────────────────
    let map             = null;
    let markerLayer     = null;
    let stationLayer    = null;
    let virtualStationLayer = null; // Dedicated layer for commander-placed virtual stations
    let pathLayer       = null; // For routing
    let droneLayer      = null; // For drone surveillance
    let spreadLayer     = null; // For fire spread polygons
    let virtualStations = [];
    let heatPoints      = [];
    let officialStations = [];
    let userCoords      = null; // { lat, lng }
    let isRegionalFilterActive = false;

    // ── Initialize Live Map ───────────────────────────────────
    function initMap() {
        const container = document.getElementById('live-map-container');
        if (!container) return;
        
        // Initialize map centered on India/Tamil Nadu as default
        map = L.map('live-map-container', {
            zoomControl: false,
            attributionControl: false
        }).setView([13.0827, 80.2707], 11);

        // Add Dark Theme Tiles (Free/No API Key)
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            maxZoom: 19
        }).addTo(map);

        markerLayer = L.layerGroup().addTo(map);
        stationLayer = L.layerGroup().addTo(map);
        pathLayer = L.layerGroup().addTo(map);
        virtualStationLayer = L.layerGroup().addTo(map); // Dedicated layer for user-placed virtual stations
        droneLayer = L.layerGroup().addTo(map);
        spreadLayer = L.layerGroup().addTo(map);

        L.control.zoom({ position: 'bottomright' }).addTo(map);

        // Global Map Click Handler
        map.on('click', (e) => {
            const { lat, lng } = e.latlng;
            if (document.getElementById('planning-toggle').checked) {
                addVirtualStation(lat, lng);
            } else {
                handleMapClick(lat, lng);
            }
        });

        // Global View Button
        const globalBtn = document.getElementById('global-view-btn');
        if (globalBtn) {
            globalBtn.addEventListener('click', () => {
                const markers = [];
                stationLayer.eachLayer(l => markers.push(l));
                markerLayer.eachLayer(l => markers.push(l));
                
                if (markers.length > 0) {
                    const group = L.featureGroup(markers);
                    map.fitBounds(group.getBounds(), { padding: [50, 50] });
                    
                    const log = document.createElement('span');
                    log.textContent = `> [SYSTEM] Global View synchronization: ${markers.length} tactical points mapped.`;
                    if (advisorLogs) advisorLogs.prepend(log);
                }
            });
        }
    }
    // ── Workflow Tracker Controller ──────────────────────────
    function setWorkflowStep(step, status = 'active') {
        const el = document.querySelector(`.wf-step[data-step="${step}"]`);
        if (!el) return;
        
        // Remove existing states
        el.classList.remove('active', 'pulse');
        
        if (status === 'active') {
            el.classList.add('active');
        } else if (status === 'pulse') {
            el.classList.add('active', 'pulse');
        }
    }

    function resetPostInitializationWorkflow() {
        // Steps 6-11 are situational/post-init
        for(let i=6; i<=11; i++) {
            const el = document.querySelector(`.wf-step[data-step="${i}"]`);
            if (el) el.classList.remove('active', 'pulse');
        }
    }

    initMap();

    // ── Live Clock ────────────────────────────────────────────
    const timeDisplay = document.getElementById('current-time');
    function updateClock() {
        const now = new Date();
        timeDisplay.textContent = now.toLocaleTimeString('en-US', { hour12: false });
    }
    updateClock();
    setInterval(updateClock, 1000);

    // ── Phase: Command Initialization (Professional GPS Boot) ──
    const bootScreen     = document.getElementById('boot-screen');
    const bootLog        = document.getElementById('boot-log');
    const locationPrompt = document.getElementById('location-prompt');
    const detectBtn     = document.getElementById('detect-location-btn');
    const skipBtn       = document.getElementById('skip-location-btn');
    const locStatus      = document.getElementById('loc-status');

    async function initCommandSequence() {
        if (!bootScreen) return;

        // STEP 1: Data Collection (Live Environment Scan)
        setWorkflowStep(1, 'pulse');

        // Simulated Boot Sequence
        const lines = [
            '[SYSTEM] RE-CALIBRATING GLOBAL SENSORS...',
            '[NET] PINGING METEOROLOGICAL NODES...',
            '[DB] OFFICIAL TNFRS DIRECTORY LOADED (v2026.03)',
            '[AI] ENSEMBLE CLIMATE MODELS READY.',
            '[GEO] AUTO-DETECTING COMMAND CENTER COORDINATES...'
        ];

        for (const line of lines) {
            await new Promise(r => setTimeout(r, 400));
            const p = document.createElement('p');
            p.className = 'boot-line';
            p.textContent = line;
            bootLog.appendChild(p);
            bootLog.scrollTop = bootLog.scrollHeight;
        }

        // AUTO-TRIGGER GEOLOCATION (Always use current location)
        if ("geolocation" in navigator) {
            navigator.geolocation.getCurrentPosition(
                (pos) => {
                    calibrateSystem(pos.coords.latitude, pos.coords.longitude);
                    startPermanentTracking();
                },
                (err) => {
                    console.warn('[TNFSC] Geolocation denied. Falling back to IP analysis.');
                    const p = document.createElement('p');
                    p.className = 'boot-line';
                    p.style.color = 'var(--emergency-amber)';
                    p.textContent = '[WARN] GPS REFUSED. FALLING BACK TO IP-BASED CALIBRATION.';
                    bootLog.appendChild(p);
                    calibrateSystem(null, null);
                }
            );
        } else {
            calibrateSystem(null, null);
        }

        locationPrompt.style.display = 'block'; // Keep as fallback/status indicator
        lucide.createIcons();
    }

    function startPermanentTracking() {
        if ("geolocation" in navigator) {
            navigator.geolocation.watchPosition(
                (pos) => {
                    const { latitude: lat, longitude: lng } = pos.coords;
                    console.log('[TNFSC] 📍 Live Location Update:', lat, lng);
                    userCoords = { lat, lng };
                    updateMeteorologicalMarker(lat, lng);
                    if (isRegionalFilterActive) {
                        drawHeatmap(heatPoints);
                    }
                },
                (err) => console.warn('[TNFSC] Live tracking interrupted:', err),
                { enableHighAccuracy: true }
            );
        }
    }

    let metMarker = null;
    function updateMeteorologicalMarker(lat, lng) {
        if (!map) return;
        if (!metMarker) {
            const icon = L.divIcon({
                className: 'met-station-marker',
                html: '<div class="met-station-dot" title="METEOROLOGICAL COMMAND CENTER (LIVE)"></div>',
                iconSize: [20, 20]
            });
            metMarker = L.marker([lat, lng], { icon, zIndexOffset: 1000 }).addTo(map);
            metMarker.bindPopup('<strong style="color:#00e676;">🛰️ METEOROLOGICAL COMMAND</strong><br/><span style="font-size:0.8rem;">Persistent Satellite Sync: Active</span>');
        } else {
            metMarker.setLatLng([lat, lng]);
        }
    }

    async function calibrateSystem(lat, lng) {
        locStatus.style.display = 'flex';
        detectBtn.disabled = true;
        skipBtn.disabled   = true;

        // STEP 2: Ingestion & Preprocessing
        setWorkflowStep(2, 'pulse');

        try {
            const res = await fetch('/api/init_location', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lat, lng })
            });
            const data = await res.json();
            
            if (data.status === 'ok') {
                const locationName = data.location.toUpperCase();
                
                // Update header badge
                const regionBadge = document.getElementById('command-region-badge');
                const citySpan    = document.getElementById('command-city');
                if (regionBadge && citySpan) {
                    citySpan.textContent = locationName;
                    regionBadge.style.display = 'flex';
                }

                // Fly to calibrated location
                if (map && data.weather) {
                    userCoords = { lat: data.weather.lat, lng: data.weather.lng };
                    updateMeteorologicalMarker(userCoords.lat, userCoords.lng);
                    map.flyTo([userCoords.lat, userCoords.lng], 12, {
                        animate: true,
                        duration: 2
                    });
                }
                
                setWorkflowStep(1, 'active');
                setWorkflowStep(2, 'active');

                const p = document.createElement('p');
                p.className = 'boot-line';
                p.style.color = '#fff';
                p.textContent = `[SUCCESS] COMMAND CENTER CALIBRATED TO: ${locationName}`;
                bootLog.appendChild(p);
                
                await new Promise(r => setTimeout(r, 1200));
                bootScreen.style.opacity = '0';

                setTimeout(() => {
                    bootScreen.style.display = 'none';
                    // Kick off main data fetches once calibrated
                    
                    // PROXIMITY ANALYSIS: Find 150km Heat Center (Expanded for Sample Dataset)
                    if (lat && lng && window._heatmapZones) {
                        const radius = 150; // km
                        const nearby = window._heatmapZones.filter(z => {
                            const d = Math.hypot(lat - z.lat, lng - z.lng) * 111.32;
                            return d <= radius;
                        });
                        if (nearby.length > 0) {
                            nearby.sort((a, b) => b.risk - a.risk);
                            const top = nearby[0];
                            const log = document.createElement('span');
                            log.className = 'log-warning';
                            log.innerHTML = `> [PROXIMITY] 150km Heat Center: <strong>${top.zone}</strong> (${top.risk}% Risk)`;
                            if (advisorLogs) advisorLogs.prepend(log);
                        } else {
                            const log = document.createElement('span');
                            log.className = 'log-info';
                            log.textContent = `> [PROXIMITY] No tactical zones within 150km of command center. Resetting to Global View.`;
                            if (advisorLogs) advisorLogs.prepend(log);
                        }
                    }

                    // STEP 3: AI Prediction Engine
                    setWorkflowStep(3, 'pulse');
                    fetchHeatmap();
                    fetchMetrics();
                    fetchAdvisor();
                    setTimeout(() => setWorkflowStep(3, 'active'), 1500);

                    // ── Periodic Telemetry Sync (Step 3: Update every 15 mins) ──
                    setInterval(() => {
                        console.log('[TNFSC] 🌦️ Performing 15-minute telemetry refresh...');
                        fetchHeatmap();
                        fetchMetrics();
                        fetchAdvisor();
                        
                        const log = document.createElement('span');
                        log.textContent = `> [SYSTEM] Meteorological telemetry synced (15m interval)`;
                        if (advisorLogs) advisorLogs.prepend(log);
                    }, 15 * 60 * 1000); 
                }, 1200);
            }
        } catch (e) {
            console.error('Calibration failed', e);
            bootScreen.style.display = 'none'; // Fallback to avoid getting stuck
        }
    }

    if (detectBtn) {
        detectBtn.addEventListener('click', () => {
            if ("geolocation" in navigator) {
                navigator.geolocation.getCurrentPosition(
                    (pos) => {
                        calibrateSystem(pos.coords.latitude, pos.coords.longitude);
                    },
                    (err) => {
                        console.warn('Geolocation denied/failed. Switching to IP analysis.');
                        calibrateSystem(null, null); // Backend will use auto:ip
                    }
                );
            } else {
                calibrateSystem(null, null);
            }
        });
    }

    if (skipBtn) {
        skipBtn.textContent = 'IP-BASED REGIONAL SCAN';
        skipBtn.addEventListener('click', () => {
            calibrateSystem(null, null); // Trigger IP-based detection
        });
    }

    // Start boot sequence
    if (IS_FLASK) {
        initCommandSequence();
    } else {
        bootScreen.style.display = 'none';
    }

    // ── Chart.js — ML Insights (Building Density vs Probability) ──
    let mlChart = null;
    const mlCtx = document.getElementById('ml-chart').getContext('2d');
    const modelMetaP = document.getElementById('model-meta-text');

    function buildChartConfig(labels, densityData, probData) {
        return {
            type: 'line',
            data: {
                labels,
                datasets: [
                    {
                        label: 'Building Density',
                        data: densityData,
                        borderColor: '#00f2ff',
                        backgroundColor: 'rgba(0, 242, 255, 0.1)',
                        borderWidth: 2,
                        tension: 0.4,
                        fill: true,
                        pointBackgroundColor: '#00f2ff'
                    },
                    {
                        label: 'Incident Probability (%)',
                        data: probData,
                        borderColor: '#ff3e3e',
                        backgroundColor: 'rgba(255, 62, 62, 0.1)',
                        borderWidth: 2,
                        tension: 0.4,
                        fill: true,
                        pointBackgroundColor: '#ff3e3e'
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 600, easing: 'easeInOutQuart' },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(255,255,255,0.05)' },
                        ticks: { color: '#94a3b8', font: { size: 10 } }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { color: '#94a3b8', font: { size: 10 } }
                    }
                },
                plugins: {
                    legend: {
                        display: true,
                        labels: { color: '#e2e8f0', boxWidth: 12, font: { size: 10 } },
                        position: 'top'
                    }
                }
            }
        };
    }

    function renderChart(labels, densityData, probData) {
        if (mlChart) { mlChart.destroy(); }
        mlChart = new Chart(mlCtx, buildChartConfig(labels, densityData, probData));
    }

    // Default chart (static seed until API responds)
    renderChart(
        ['Zone A', 'Zone B', 'Zone C', 'Zone D', 'Zone E', 'Zone F', 'Zone G'],
        [45, 52, 38, 65, 48, 82, 70],
        [35, 48, 42, 70, 55, 88, 75]
    );

    // ═══════════════════════════════════════════════════════════
    //   LEAFLET MAP RENDERING
    // ═══════════════════════════════════════════════════════════
    function drawHeatmap(points) {
        if (!map || !markerLayer) return;
        markerLayer.clearLayers();
        window._heatmapZones = []; // Reset for new data

        const filteredPoints = isRegionalFilterActive && userCoords 
            ? points.filter(p => {
                const lat = p.lat || (13.08 + (p.y - 0.3) * 0.2);
                const lng = p.lng || (80.27 + (p.x - 0.5) * 0.2);
                const d = Math.hypot(userCoords.lat - lat, userCoords.lng - lng) * 111.32;
                return d <= 150; // Increased radius for regional stability
              })
            : points;

        filteredPoints.forEach(p => {
            const lat = p.lat || (13.08 + (p.y - 0.3) * 0.2); 
            const lng = p.lng || (80.27 + (p.x - 0.5) * 0.2);
            
            // Populate global zones for click handling (Step 7)
            window._heatmapZones.push({ zone: p.zone, lat, lng, risk: p.risk });

            const color = p.risk > 70 ? '#ff3e3e' : p.risk > 40 ? '#ff8f00' : '#22d3ee';
            
            const icon = L.divIcon({
                className: 'risk-bubble-marker',
                html: `<div class="risk-bubble-core ${p.mitigated ? 'mitigated' : ''}" 
                            style="background:${color}; box-shadow: 0 0 15px ${color}; width:${15 + p.risk/10}px; height:${15 + p.risk/10}px;">
                       </div>`,
                iconSize: [30, 30],
                iconAnchor: [15, 15] // Centered anchor
            });

            const m = L.marker([lat, lng], { icon }).addTo(markerLayer);
            
            m.bindPopup(`
                <div style="padding:5px;">
                    <strong style="color:${color}; font-size:1rem;">${p.zone}</strong><br/>
                    <span style="font-size:0.8rem; opacity:0.8;">Predicted Risk: ${p.risk}%</span><br/>
                    <div style="margin-top:5px; height:4px; width:100%; background:#334155; border-radius:2px;">
                        <div style="height:100%; width:${p.risk}%; background:${color}; border-radius:2px;"></div>
                    </div>
                </div>
            `);

            m.on('mouseover', () => {
                document.getElementById('risk-score').textContent = p.risk + '%';
                document.getElementById('risk-diagnostics').textContent = `Zone Focus: ${p.zone}`;
            });
        });
    }

    function addVirtualStation(lat, lng) {
        if (!map || !virtualStationLayer) return;
        const id = Date.now();
        const station = { id, lat, lng };
        virtualStations.push(station);
        
        // Add visual marker to the DEDICATED virtual station layer
        const sMarker = L.circle([lat, lng], {
            radius: 3000, // 3km suppression radius
            color: '#00f2ff',
            weight: 2,
            fillColor: '#00f2ff',
            fillOpacity: 0.12,
            dashArray: '5, 10'
        }).addTo(virtualStationLayer);

        // Small center pin
        const pinIcon = L.divIcon({
            className: '',
            html: `<div style="width:12px;height:12px;background:#00f2ff;border:2px solid white;border-radius:50%;box-shadow:0 0 12px #00f2ff;transform:translate(-50%,-50%);"></div>`,
            iconSize: [0, 0]
        });
        const pinMarker = L.marker([lat, lng], { icon: pinIcon }).addTo(virtualStationLayer);
        pinMarker.bindPopup(`<div style="padding:4px;"><strong style="color:#00f2ff;">⬡ Virtual Station #${virtualStations.length}</strong><br/><span style="font-size:0.75rem;">Suppression radius: 3km</span></div>`);
        
        station.circleMarker = sMarker;
        station.pinMarker    = pinMarker;
        
        fetchHeatmap(); // Recalculate suppression
        fetchAdvisor();
        fetchSimulate();
    }

    function renderOfficialStations(stations) {
        if (!map || !stationLayer) return;
        stationLayer.clearLayers();

        stations.forEach(s => {
            const lat = parseFloat(s.lat);
            const lng = parseFloat(s.lng);
            if (isNaN(lat) || isNaN(lng)) return;

            const icon = L.divIcon({
                className: 'station-marker',
                html: `<div class="station-dot" style="width:10px; height:10px; background:#00f2ff; border-radius:50%; box-shadow:0 0 10px #00f2ff; border:2px solid white; margin: 2px;"></div>`,
                iconSize: [14, 14],
                iconAnchor: [7, 7] // Pixel-perfect center anchor
            });

            const m = L.marker([lat, lng], { icon }).addTo(stationLayer);
            m.bindPopup(`
                <div style="padding:5px;">
                    <strong style="color:#00f2ff;">${s.station_name}</strong><br/>
                    <span style="font-size:0.8rem; opacity:0.8;">DISTRICT: ${s.district}</span><br/>
                    <span style="font-size:0.8rem; opacity:0.8;">Tel: ${s.landline || 'N/A'}</span>
                </div>
            `);
        });
    }

    window.addEventListener('resize', () => { if(map) map.invalidateSize(); });
    // drawHeatmap([]); // No longer needed as placeholder
    window._heatmapZones = [];  // exposed for dispatch advisor click

    // ═══════════════════════════════════════════════════════════
    //   API CALLS
    // ═══════════════════════════════════════════════════════════

    // ── /api/heatmap ──────────────────────────────────────────
    async function fetchHeatmap() {
        if (!IS_FLASK) return;
        try {
            const stationsQuery = JSON.stringify(virtualStations.map(s => ({ lat: s.lat, lng: s.lng })));
            const res  = await fetch(`/api/heatmap?stations=${encodeURIComponent(stationsQuery)}`);
            const data = await res.json();
            heatPoints = data.points || [];
            officialStations = data.official_stations || []; // New: official data
            window._heatmapZones = heatPoints; // expose for dispatch advisor
            
            // heatmap already sets step 3, but let's confirm prediction is active
            setWorkflowStep(3, 'active');
            
            drawHeatmap(heatPoints);
            renderOfficialStations(officialStations);
            
            // 🛸 Drone Sync
            fetchDrones();
            
            // STEP 9: Drone Surveillance HUD
            setWorkflowStep(9, 'active');

            if (officialStations.length > 0) {
                renderStationDirectory(officialStations);
            }
        } catch (e) {
            console.warn('[TNFSC] Heatmap fetch failed – using placeholder.', e);
        }
    }

    // ── /api/metrics ──────────────────────────────────────────
    const riskScoreEl        = document.getElementById('risk-score');
    const tickerEl           = document.getElementById('data-ticker');
    const freqBar            = document.querySelector('.freq-bar .fill');

    async function fetchMetrics() {
        if (!IS_FLASK) {
            // Local simulation fallback
            const cur = parseFloat(riskScoreEl.textContent);
            const next = (cur + (Math.random() - 0.5) * 0.4).toFixed(1);
            riskScoreEl.textContent = next + '%';
            return;
        }
        try {
            const stationsQuery = JSON.stringify(virtualStations.map(s => ({ lat: s.lat, lng: s.lng })));
            const res  = await fetch(`/api/metrics?stations=${encodeURIComponent(stationsQuery)}`);
            const data = await res.json();

            riskScoreEl.textContent = data.risk_probability + '%';
            if (freqBar) freqBar.style.width = data.seasonal_freq_pct + '%';
            if (tickerEl) {
                tickerEl.innerHTML = data.ticker.replace('[LIVE]', '<span style="color:#ff8f00; font-weight:bold;">[LIVE]</span>');
            }

            // Update response time card
            const respCard = document.querySelector('.metric-card:nth-child(2) .value');
            if (respCard) respCard.innerHTML = `${data.response_time} <small>min</small>`;

            if (modelMetaP) {
                const rf  = data.model_accuracies?.random_forest?.toFixed(1) ?? '–';
                const gb  = data.model_accuracies?.gradient_boosting?.toFixed(1) ?? '–';
                modelMetaP.innerHTML =
                    `Ensemble: <strong style="color:#00f2ff">RF ${rf}%</strong> (Feature Importance) | <strong style="color:#ff3e3e">GB ${gb}%</strong> (False-Negative Guard)`;
            }
        } catch (e) {
            console.warn('[TNFSC] Metrics fetch failed.', e);
        }
    }

    // ── /api/calculate (Auto-Calculate button) ────────────────

    async function runCalculate(zone) {
        if (!IS_FLASK) return;
        try {
            const res  = await fetch('/api/calculate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    zone: zone || null,
                    stations: virtualStations.map(s => ({ x: s.x, y: s.y }))
                })
            });
            const data = await res.json();

            // Update risk score card
            riskScoreEl.textContent = data.ensemble_probability + '%';
            
            // Update diagnostics
            const diagEl = document.getElementById('risk-diagnostics');
            if (diagEl) {
                diagEl.textContent = `PRIMARY DRIVERS: ${data.risk_drivers.join(', ')}`;
                diagEl.style.display = data.risk_drivers.length > 0 ? 'inline-block' : 'none';
            }

            // Update chart with real ML data
            const chartData = data.building_density_chart;
            if (chartData) {
                renderChart(chartData.zones, chartData.density, chartData.probability);
            }

            // Update model meta
            if (modelMetaP) {
                const rf = data.model_accuracies?.random_forest?.toFixed(1) ?? '–';
                const gb = data.model_accuracies?.gradient_boosting?.toFixed(1) ?? '–';
                modelMetaP.innerHTML =
                    `Zone: <strong style="color:#00f2ff">${data.zone}</strong> |
                     Risk: <strong style="color:#ff3e3e">${data.risk_level}</strong> |
                     RF <strong style="color:#00f2ff">${rf}%</strong> (Feature Imp) | GB <strong style="color:#ff3e3e">${gb}%</strong> (False-Neg Guard)`;
            }

            // Refresh heatmap after new calculation
            // Calculation results trigger Prediction and Detection sequence
            setWorkflowStep(3, 'active'); // Prediction
            setWorkflowStep(4, 'active'); // Detection

            fetchHeatmap();

            // ── Active Defense: Station-to-Zone Proximity Alert ──────────────
            // If risk ≥ 85%, the backend returns a dispatch-ready payload.
            const ada = data.active_defense_alert;
            if (ada && ada.triggered) {
                // STEP 10: Alert & Notification
                setWorkflowStep(10, 'pulse');
                // STEP 11: Logging
                setWorkflowStep(11, 'pulse');

                const alertBannerEl = document.getElementById('alert-banner');
                const alertCardsRowEl = document.getElementById('alert-cards-row');
                if (alertBannerEl && alertCardsRowEl) {
                    alertBannerEl.style.display = 'block';
                    const existingAD = document.getElementById('active-defense-card');
                    if (existingAD) existingAD.remove();
                    const adCard = document.createElement('div');
                    adCard.id = 'active-defense-card';
                    adCard.className = 'alert-card alert-critical';
                    adCard.style.cssText = 'border:1.5px solid #ff3e3e; background:rgba(255,62,62,0.15);';
                    adCard.innerHTML = `
                        <div class="alert-card-zone">📡 STATION ALERT: ${ada.zone}</div>
                        <div class="alert-card-risk">${ada.risk}%</div>
                        <div class="alert-card-driver">ACTIVE DEFENSE TRIGGERED</div>
                        <div class="alert-card-units">🚨 DISPATCH NOW — Messaging API Ready</div>
                    `;
                    alertCardsRowEl.prepend(adCard);
                }
                console.warn('[TNFSC] 🚨 ACTIVE DEFENSE:', ada.message);
                
                setTimeout(() => {
                    setWorkflowStep(10, 'active'); // Advisor Briefing
                    setWorkflowStep(11, 'active'); // Logging
                }, 4000);
            }

            console.log('[TNFSC] Calculate result:', data);
        } catch (e) {
            console.warn('[TNFSC] Calculate failed.', e);
        }
    }

    // ── Resource Planning Controls ──────────────────────────
    const autoCalcBtn   = document.getElementById('auto-calculate-btn');
    const manualPlotBtn = document.getElementById('manual-plot-btn');
    let isManualPlotActive = false;

    if (autoCalcBtn) {
        autoCalcBtn.addEventListener('click', () => {
            isManualPlotActive = false;
            document.body.classList.remove('manual-plot-active');
            autoCalcBtn.classList.add('active');
            manualPlotBtn?.classList.remove('active');
            runCalculate(null);
            showRadarPing(0.5, 0.5); // Ping center as global scan
        });
    }

    if (manualPlotBtn) {
        // Tooltip: commanders can override AI with local knowledge (festivals, constructions etc.)
        manualPlotBtn.title = 'Commander Override: Click any map zone to force-analyze it. Use when a local event (festival, construction) hasn\'t been captured by sensors yet.';
        manualPlotBtn.addEventListener('click', () => {
            isManualPlotActive = !isManualPlotActive;
            manualPlotBtn.classList.toggle('active', isManualPlotActive);
            autoCalcBtn?.classList.remove('active');
            document.body.classList.toggle('manual-plot-active', isManualPlotActive);
            if (isManualPlotActive) {
                console.log('[TNFSC] Manual Plot Mode Enabled — Commander Override Active');
                alertBanner.style.display = 'block';
                alertBanner.innerHTML = '<div class="alert-banner-title"><i data-lucide="crosshair"></i> COMMANDER OVERRIDE ACTIVE — Click any zone to force-analyze (use for festivals, construction, or local events the sensors haven\'t captured)</div>';
                lucide.createIcons();
            } else {
                fetchAlerts(); // Restore normal alerts
            }
        });
    }

    function showRadarPing(lat, lng) {
        if (!map) return;
        const icon = L.divIcon({
            className: 'radar-ping-marker',
            html: '<div class="radar-ping" style="width:40px; height:40px; border:2px solid var(--fire-red); border-radius:50%; animation: ripple 1s ease-out forwards; position:absolute; transform:translate(-50%,-50%);"></div>',
            iconSize: [40, 40]
        });
        const ping = L.marker([lat, lng], { icon }).addTo(map);
        setTimeout(() => ping.remove(), 1000);
    }

    // ── Command Directory Logic ──────────────────────────────
    const stationDirectory = document.getElementById('station-directory');
    const stationSearch    = document.getElementById('station-search');

    function renderStationDirectory(stations) {
        if (!stationDirectory) return;
        stationDirectory.innerHTML = '';

        // Integration: Meteorological Node Entry (Always Visible)
        const metNode = document.createElement('div');
        metNode.className = 'station-contact-item recommended';
        metNode.style.borderLeftColor = '#00e676';
        metNode.innerHTML = `
            <span class="s-name" style="color:#00e676;">🛰️ METEOROLOGICAL COMMAND</span>
            <span class="s-district">${userCoords ? 'LIVE NETWORK NODE | PERSISTENT GPS' : 'SIGNAL PENDING | AUTO-CALIBRATING...'}</span>
            <div class="s-contact">
                <i data-lucide="radio"></i> ${userCoords ? 'SATELLITE SYNC: ACTIVE' : 'SEARCHING FOR NODE...'}
            </div>
        `;
        metNode.addEventListener('click', () => {
            if (map && userCoords) {
                map.flyTo([userCoords.lat, userCoords.lng], 15, { animate: true });
            }
        });
        stationDirectory.appendChild(metNode);

        const limit = stationSearch.value ? stations.length : 20; 
        
        stations.slice(0, limit).forEach(s => {
            const name     = s.station_name || s.name || 'Unknown Station';
            const district = s.district || '—';
            const grade    = s.category || s.cat || '—';
            const contact  = s.cug || s.landline || 'N/A';
            const lat      = parseFloat(s.lat);
            const lng      = parseFloat(s.lng);

            const el = document.createElement('div');
            el.className = 'station-contact-item';
            el.innerHTML = `
                <span class="s-name">${name}</span>
                <span class="s-district">${district} | ${grade} GRADE</span>
                <div class="s-contact">
                    <i data-lucide="phone"></i> ${contact}
                </div>
            `;
            el.addEventListener('click', () => {
                if (map && !isNaN(lat) && !isNaN(lng)) {
                    map.flyTo([lat, lng], 14, { animate: true, duration: 1 });
                    showLayer('stations');
                    setActiveMapBtn('Stations');
                }
            });
            stationDirectory.appendChild(el);
        });
        if (window.lucide) lucide.createIcons();
    }

    stationSearch.addEventListener('input', (e) => {
        const query = e.target.value.toLowerCase();
        const filtered = officialStations.filter(s => {
            const name     = (s.station_name || s.name || '').toLowerCase();
            const district = (s.district || '').toLowerCase();
            return name.includes(query) || district.includes(query);
        });
        renderStationDirectory(filtered);
    });

    // ── /api/incidents ─────────────────────────────────────────
    const incidentFeed = document.getElementById('incident-feed');

    async function fetchIncidents() {
        if (!IS_FLASK) {
            addLocalIncident();
            return;
        }
        try {
            const res   = await fetch('/api/incidents');
            const data  = await res.json();
            const items = (data.incidents || []).slice(0, 5);
            
            if (items.length > 0) {
                // STEP 4: Incident Detection
                setWorkflowStep(4, 'active');
            }
            
            incidentFeed.innerHTML = '';
            items.forEach(inc => {
                const el = document.createElement('div');
                el.className = `incident-item ${inc.severity}`;
                el.style.cssText = 'opacity:0;transform:translateX(-20px);transition:all 0.5s ease;';
                el.innerHTML = `
                    <span class="time">${inc.time}</span>
                    <span class="loc">${inc.zone}</span>
                    <span class="type">${inc.type} &nbsp;<em style="color:#94a3b8">${inc.risk_pct}%</em></span>`;
                incidentFeed.prepend(el);
                setTimeout(() => { el.style.opacity = '1'; el.style.transform = 'translateX(0)'; }, 80);
            });
        } catch (e) {
            console.warn('[TNFSC] Incidents fetch failed.', e);
            addLocalIncident();
        }
    }

    // Local fallback incident simulation
    const _locs  = ['Sector Alpha', 'Sector Beta', 'Alpha Corridor', 'Grid Sector Delta', 'Commercial Hub'];
    const _types = ['Electrical Short Circuit', 'Smoke Detected', 'Building Structural Alert'];
    function addLocalIncident() {
        const now  = new Date();
        const time = `${now.getHours()}:${String(now.getMinutes()).padStart(2,'0')}`;
        const el   = document.createElement('div');
        el.className = `incident-item ${Math.random() > 0.7 ? 'high' : 'med'}`;
        el.style.cssText = 'opacity:0;transform:translateX(-20px);transition:all 0.5s ease;';
        el.innerHTML = `<span class="time">${time}</span>
            <span class="loc">${_locs[Math.floor(Math.random()*_locs.length)]}</span>
            <span class="type">${_types[Math.floor(Math.random()*_types.length)]}</span>`;
        incidentFeed.prepend(el);
        if (incidentFeed.children.length > 5) incidentFeed.lastElementChild.remove();
        setTimeout(() => { el.style.opacity = '1'; el.style.transform = 'translateX(0)'; }, 100);
    }

    // ── Map layer buttons ──────────────────────────────────────
    // Track current active layer
    let activeMapLayer = 'heatmap'; // 'heatmap' | 'stations' | 'hydrants'

    function showLayer(layerName) {
        activeMapLayer = layerName;
        if (!map) return;
        if (layerName === 'heatmap') {
            // Show risk zone markers, hide stations
            if (markerLayer) markerLayer.addTo(map);
            if (stationLayer) map.removeLayer(stationLayer);
            fetchHeatmap();
        } else if (layerName === 'stations') {
            // Hide zone risk bubbles, show official fire stations
            if (markerLayer) map.removeLayer(markerLayer);
            if (stationLayer) stationLayer.addTo(map);
            if (officialStations.length > 0) {
                renderOfficialStations(officialStations);
            }
        } else if (layerName === 'hydrants') {
            // STEP 8: Hydrant & Resource ID
            setWorkflowStep(8, 'active');
            
            // Hydrant layer: show station positions as hydrant icons
            if (markerLayer) map.removeLayer(markerLayer);
            if (stationLayer) stationLayer.clearLayers();
            if (stationLayer) stationLayer.addTo(map);
            officialStations.forEach(s => {
                const lat = parseFloat(s.lat);
                const lng = parseFloat(s.lng);
                if (isNaN(lat) || isNaN(lng)) return;
                const icon = L.divIcon({
                    className: 'hydrant-marker',
                    html: `<div title="Hydrant: ${s.station_name||s.name||''}" style="width:10px;height:10px;background:#22d3ee;border-radius:2px;border:2px solid white;box-shadow:0 0 8px #22d3ee;"></div>`,
                    iconSize: [14, 14]
                });
                const m = L.marker([lat, lng], { icon }).addTo(stationLayer);
                const name     = s.station_name || s.name || 'Hydrant Point';
                const landline = s.landline || s.cug || 'N/A';
                m.bindPopup(`<div style="padding:5px;"><strong style="color:#22d3ee;">💧 ${name}</strong><br/><span style="font-size:0.8rem;opacity:0.8;">DISTRICT: ${s.district||'—'}</span><br/><span style="font-size:0.8rem;">Tel: ${landline}</span></div>`);
            });
        }
    }

    function setActiveMapBtn(label) {
        document.querySelectorAll('.btn-map').forEach(b => {
            b.classList.toggle('active', b.textContent.trim() === label);
        });
    }

    document.querySelectorAll('.btn-map').forEach(btn => {
        btn.addEventListener('click', function () {
            const label = this.textContent.trim();
            if (label !== 'REGIONAL FOCUS') {
                setActiveMapBtn(label);
                if (label === 'Heatmap')  showLayer('heatmap');
                else if (label === 'Stations') showLayer('stations');
                else if (label === 'Hydrants') showLayer('hydrants');
            }
        });
    });

    const regionalBtn = document.getElementById('regional-focus-btn');
    if (regionalBtn) {
        regionalBtn.addEventListener('click', function() {
            isRegionalFilterActive = !isRegionalFilterActive;
            this.classList.toggle('active', isRegionalFilterActive);
            
            const log = document.createElement('span');
            log.className = isRegionalFilterActive ? 'log-warning' : 'log-success';
            log.textContent = `> [SYSTEM] Regional Focus ${isRegionalFilterActive ? 'ENABLED (30km Radius)' : 'DISABLED (Global View)'}`;
            if (advisorLogs) advisorLogs.prepend(log);
            
            drawHeatmap(heatPoints); // Re-render with new filter
        });
    }

    // ── /api/advisor ──────────────────────────────────────────
    const advisorText  = document.getElementById('advisor-text');
    const advisorAlert = document.getElementById('advisor-alert');
    const advisorLogs  = document.getElementById('advisor-logs');
    let typingTimeout  = null;

    async function fetchAdvisor() {
        if (!IS_FLASK) return;
        try {
            const stationsQuery = JSON.stringify(virtualStations.map(s => ({ lat: s.lat, lng: s.lng })));
            const res  = await fetch(`/api/advisor?stations=${encodeURIComponent(stationsQuery)}`);
            const data = await res.json();

            // Typing effect for briefing
            if (advisorText) {
                if (typingTimeout) clearTimeout(typingTimeout);
                const text = data.briefing;
                let i = 0;
                advisorText.innerHTML = '';
                const type = () => {
                    if (i < text.length) {
                        advisorText.innerHTML += text.charAt(i);
                        i++;
                        typingTimeout = setTimeout(type, 15);
                    } else {
                        // Add cursor when finished
                        const cursor = document.createElement('span');
                        cursor.className = 'typing-cursor';
                        advisorText.appendChild(cursor);
                    }
                };
                type();
            }

            // Update alert status
            if (advisorAlert) {
                advisorAlert.textContent = data.alert_level;
                advisorAlert.className = `status-badge ${data.alert_level.toLowerCase()}`;
            }

            // Update Reasoning & Prediction
            const reasoningEl = document.getElementById('advisor-reasoning');
            const predEl      = document.getElementById('advisor-prediction');
            if (reasoningEl && data.reasoning) {
                reasoningEl.style.display = 'flex';
                document.getElementById('reasoning-text').textContent = data.reasoning;
            }
            if (predEl && data.incident_prediction) {
                predEl.style.display = 'block';
                document.getElementById('pred-val').textContent = data.incident_prediction;
                document.getElementById('danger-fill').style.width = data.danger_score + '%';
            }

            // Update log stream
            if (advisorLogs && data.logs) {
                // STEP 10: AI Strategic Advisor Briefing
                setWorkflowStep(10, 'pulse');
                
                advisorLogs.innerHTML = '';
                data.logs.forEach(log => {
                    const span = document.createElement('span');
                    span.textContent = `> ${log}`;
                    advisorLogs.appendChild(span);
                });

                setTimeout(() => setWorkflowStep(10, 'active'), 2000);
            }
        } catch (e) {
            console.warn('[TNFSC] Advisor fetch failed.', e);
        }
    }

    // ── /api/status polling ────────────────────────────────────
    const modelBadge   = document.getElementById('model-badge');
    const aiVersion    = document.getElementById('ai-version');
    const aiDataset    = document.getElementById('ai-dataset');
    const aiRfAcc      = document.getElementById('ai-rf-acc');
    const aiGbAcc      = document.getElementById('ai-gb-acc');
    const aiLast       = document.getElementById('ai-last');
    const aiRounds     = document.getElementById('ai-rounds');
    const retrainBtn   = document.getElementById('retrain-btn');

    async function fetchStatus() {
        if (!IS_FLASK) return;
        try {
            const res  = await fetch('/api/status');
            const data = await res.json();

            const ver = data.model_version;
            const ds  = data.dataset_size;
            if (modelBadge) modelBadge.textContent = `AI v${ver} | ${ds} recs`;
            if (aiVersion)  aiVersion.textContent  = `v${ver}`;
            if (aiDataset)  aiDataset.textContent  = ds.toLocaleString();
            if (aiRfAcc)    aiRfAcc.textContent    = data.rf_accuracy + '%';
            if (aiGbAcc)    aiGbAcc.textContent    = data.gb_accuracy + '%';
            if (aiLast && data.last_retrained)
                aiLast.textContent = data.last_retrained.slice(11, 19);
            if (aiRounds)   aiRounds.textContent   = data.retrain_count;

            // Flash badge colour when retraining
            if (modelBadge) {
                modelBadge.style.color           = data.is_training ? '#ff3e3e' : '#ffcc00';
                modelBadge.style.borderColor     = data.is_training ? 'rgba(255,62,62,0.4)' : 'rgba(255,204,0,0.4)';
                modelBadge.textContent = data.is_training
                    ? `⚙ Retraining… v${ver}`
                    : `AI v${ver} | ${ds} recs`;
            }
        } catch (e) {
            console.warn('[TNFSC] Status fetch failed.', e);
        }
    }

    // Force Retrain button
    if (retrainBtn) {
        retrainBtn.addEventListener('click', async () => {
            retrainBtn.disabled = true;
            retrainBtn.textContent = '⚙ Retraining…';
            try {
                const res  = await fetch('/api/retrain', { method: 'POST' });
                const data = await res.json();
                retrainBtn.textContent = '✅ Retrain Started';
                console.log('[TNFSC] Retrain:', data);
                // Poll status quickly to show progress
                setTimeout(fetchStatus, 2000);
                setTimeout(fetchStatus, 8000);
                setTimeout(() => {
                    retrainBtn.disabled = false;
                    retrainBtn.textContent = '🔄 Force Retrain Now';
                }, 15000);
            } catch (e) {
                retrainBtn.textContent = '❌ Retrain Failed';
                setTimeout(() => {
                    retrainBtn.disabled = false;
                    retrainBtn.textContent = '🔄 Force Retrain Now';
                }, 3000);
            }
        });
    }

    // ═══════════════════════════════════════════════════════════
    //   POLLING SCHEDULE
    // ═══════════════════════════════════════════════════════════
    // Initial load
    fetchHeatmap();
    fetchMetrics();
    fetchIncidents();
    fetchStatus();
    fetchAdvisor();
    if (IS_FLASK) runCalculate(null);   // prime chart with real data

    // Refresh intervals
    setInterval(fetchMetrics,   5000);   // metrics every 5 s
    setInterval(fetchIncidents, 8000);   // incidents every 8 s
    setInterval(fetchHeatmap,   15000);  // heatmap every 15 s
    setInterval(fetchStatus,    4000);   // model status every 4 s
    setInterval(fetchAdvisor,   10000);  // advisor every 10 s

    // ── STRATEGIC PLANNING MODE (Phase 4) ────────────────────
    const planningToggle = document.getElementById('planning-toggle');
    const planningHud    = document.getElementById('planning-hud');
    const safetyScoreEl  = document.getElementById('city-safety-score');
    // NOTE: mapPlaceholder intentionally removed — the page uses Leaflet, not .map-placeholder
    const clearBtn       = document.getElementById('clear-stations');
    const optimizeBtn    = document.getElementById('optimize-btn');


    planningToggle.addEventListener('change', () => {
        const isActive = planningToggle.checked;
        planningHud.style.display = isActive ? 'flex' : 'none';
        document.body.classList.toggle('planning-mode-active', isActive);
        if (isActive) {
            // Show the virtual station layer when planning starts
            if (virtualStationLayer && map) virtualStationLayer.addTo(map);
        } else {
            // Silently clear virtual stations when planning ends (no confirm needed)
            virtualStations = [];
            if (virtualStationLayer) virtualStationLayer.clearLayers();
            if (safetyScoreEl) safetyScoreEl.textContent = '65.0%';
            fetchHeatmap();
            fetchMetrics();
            fetchAdvisor();
        }
    });



    async function fetchSimulate() {
        if (!IS_FLASK || virtualStations.length === 0) return;
        try {
            const res = await fetch('/api/simulate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    stations: virtualStations.map(s => ({ lat: s.lat, lng: s.lng }))
                })
            });
            const data = await res.json();
            if (safetyScoreEl) {
                safetyScoreEl.textContent = data.coverage_score + '%';
                // Sync everything for true simulation feel
                fetchHeatmap();
                fetchMetrics();
                fetchAdvisor();
            }
        } catch (e) {
            console.warn('[TNFSC] Simulation failed.', e);
        }
    }

    async function fetchOptimize() {
        if (!IS_FLASK) return;
        optimizeBtn.disabled = true;
        optimizeBtn.innerHTML = '<i data-lucide="loader-2" class="spin"></i> SELECTING SITE...';
        lucide.createIcons();

        try {
            const res  = await fetch('/api/optimize');
            const data = await res.json();
            
            // Backend optimize gives us lat/lng now
            if (data.optimal_gps && data.optimal_gps.lat) {
                addVirtualStation(data.optimal_gps.lat, data.optimal_gps.lng);
            } else if (data.optimal_coordinate) {
                // Fallback: convert 0-1 canvas coords to approximate Chennai lat/lng
                const lat = 13.08 + (0.5 - data.optimal_coordinate.y) * 0.5;
                const lng = 80.27 + (data.optimal_coordinate.x - 0.5) * 0.5;
                addVirtualStation(lat, lng);
            }
            fetchSimulate();
            optimizeBtn.innerHTML = '<i data-lucide="zap"></i> AI OPTIMIZE';
            try { if (window.lucide) lucide.createIcons(); } catch (e) {}
        } catch (e) {
            console.warn('[TNFSC] Optimization failed.', e);
            optimizeBtn.innerHTML = '<i data-lucide="alert-circle"></i> FAILED';
        } finally {
            optimizeBtn.disabled = false;
            try { if (window.lucide) lucide.createIcons(); } catch (e) {}
        }
    }

    function clearAllStations() {
        if (!confirm('Abort Current Tactical Deployment?')) return;
        virtualStations = [];
        // Clear virtual station circles from the dedicated layer (NOT mapPlaceholder which doesn't exist)
        if (virtualStationLayer) virtualStationLayer.clearLayers();
        fetchSimulate();
        
        // Full Sync: Dashboard should revert to actual risk
        fetchHeatmap();
        fetchMetrics();
        fetchAdvisor();
        if (safetyScoreEl) safetyScoreEl.textContent = '65.0%';
    }

    if (clearBtn) clearBtn.addEventListener('click', clearAllStations);
    if (optimizeBtn) optimizeBtn.addEventListener('click', fetchOptimize);

    // ─────────────────────────────────────────────────────────────────────
    // PHASE 9: Real-Time Alert Engine
    // ─────────────────────────────────────────────────────────────────────
    const alertBanner  = document.getElementById('alert-banner');
    const alertCardsRow = document.getElementById('alert-cards-row');

    // ── Request Notification Permission + Subscribe to Web Push ─────────
    if ('Notification' in window && 'serviceWorker' in navigator) {
        const requestAndSubscribe = async () => {
            if (Notification.permission === 'granted') {
                // Already granted — subscribe to push immediately
                await _subscribeToPush();
            } else if (Notification.permission === 'default') {
                // Ask user then subscribe
                const perm = await Notification.requestPermission();
                if (perm === 'granted') {
                    await _subscribeToPush();
                }
            }
        };
        // Wait a short moment for SW to fully activate, then subscribe
        setTimeout(requestAndSubscribe, 1500);
    }

    let lastAlertZones = new Set();

    async function fetchAlerts() {
        if (!IS_FLASK) return;
        try {
            const res    = await fetch('/api/alerts');
            const data   = await res.json();
            const alerts = data.alerts || [];

            if (alerts.length > 0) {
                alertBanner.style.display = 'block';
                alertCardsRow.innerHTML = alerts.map(a => `
                    <div class="alert-card alert-${a.level.toLowerCase()}">
                        <div class="alert-card-zone">⚠ ${a.zone}</div>
                        <div class="alert-card-risk">${a.risk}%</div>
                        <div class="alert-card-driver">DRV: ${a.top_driver}</div>
                        <div class="alert-card-units">CPLX: <strong>${a.complexity || 'HIGH'}</strong> | 🚒 ${a.units_needed} units</div>
                    </div>
                `).join('');

                // Browser notification for new critical zones
                alerts.forEach(a => {
                    if (!lastAlertZones.has(a.zone) && a.risk >= 90) {
                        if (Notification.permission === 'granted') {
                            new Notification('🚨 TNFRS CRITICAL ALERT', {
                                body: `${a.zone}: ${a.risk}% Fire Risk — ${a.units_needed} units required`,
                                icon: ''
                            });
                        }
                    }
                });
                lastAlertZones = new Set(alerts.map(a => a.zone));
            } else {
                alertBanner.style.display = 'none';
                lastAlertZones.clear();
            }
        } catch(e) {
            console.warn('[TNFSC] Alert fetch failed:', e);
        }
    }
    fetchAlerts();
    setInterval(fetchAlerts, 10000); // Poll every 10s

    // ─────────────────────────────────────────────────────────────────────
    // PHASE 11: Live Weather Widget
    // ─────────────────────────────────────────────────────────────────────
    const conditionIcons = {
        'Sunny': '☀️', 'Clear': '🌙', 'Partly cloudy': '⛅', 'Cloudy': '☁️',
        'Overcast': '☁️', 'Mist': '🌫️', 'Rain': '🌧️', 'Drizzle': '🌦️',
        'Thundery': '⛈️', 'Blizzard': '❄️', 'Fog': '🌫️'
    };

    async function fetchWeatherWidget() {
        if (!IS_FLASK) return;
        try {
            const res  = await fetch('/api/weather');
            const data = await res.json();
            const icon = Object.entries(conditionIcons).find(([k]) =>
                data.condition && data.condition.includes(k)
            )?.[1] || '🌡️';
            document.getElementById('wx-condition').textContent = icon;
            document.getElementById('wx-temp').textContent     = `${data.temp_c}°C`;
            document.getElementById('wx-humidity').textContent = `💧 ${data.humidity}%`;
            document.getElementById('wx-uv').textContent       = `UV ${data.uv}`;
        } catch(e) {}
    }
    fetchWeatherWidget();
    setInterval(fetchWeatherWidget, 60000); // Refresh every 1 min

    // ─────────────────────────────────────────────────────────────────────
    // PHASE 10: Dispatch Advisor — Nearest Station Lookup
    // ─────────────────────────────────────────────────────────────────────
    const dispatchResults   = document.getElementById('dispatch-results');
    const dispatchZoneLabel = document.getElementById('dispatch-zone-label');

    async function fetchNearestStations(zoneName) {
        if (!dispatchResults || !IS_FLASK) return;
        
        // Intelligence Handshake Sequence
        resetPostInitializationWorkflow();
        setWorkflowStep(7, 'pulse');
        
        dispatchZoneLabel.textContent = zoneName;
        dispatchResults.innerHTML = '<p style="color:var(--text-secondary);font-size:0.75rem;padding:0.5rem;">[STEP 7] SCANNING PROXIMITY...</p>';
        
        // Artificial delay for cinematic feel
        await new Promise(r => setTimeout(r, 600));

        try {
            const res  = await fetch(`/api/nearest_stations?zone=${encodeURIComponent(zoneName)}`);
            const data = await res.json();
            
            setWorkflowStep(7, 'active');
            
            // STEP 5: Fire Spread Simulation (Triggered upon proximity analysis)
            setWorkflowStep(5, 'pulse');
            setTimeout(() => setWorkflowStep(5, 'active'), 1500);

            setWorkflowStep(8, 'pulse');
            dispatchResults.innerHTML = '<p style="color:var(--text-secondary);font-size:0.75rem;padding:0.5rem;">[STEP 8] ANALYZING CAPACITIES...</p>';
            
            await new Promise(r => setTimeout(r, 400));
            setWorkflowStep(8, 'active');
            
            if (!data.nearest || data.nearest.length === 0) {
                dispatchResults.innerHTML = '<p style="color:var(--text-secondary);font-size:0.75rem;padding:0.5rem;">No station data available.</p>';
                return;
            }
            if (dispatchResults) {
                dispatchResults.innerHTML = data.nearest.map((s, i) => `
                    <div class="dispatch-station-card ${i === 0 ? 'recommended' : ''}">
                        <div class="ds-rank">#${i + 1}</div>
                        <div class="ds-info">
                            <div class="ds-name">${s.name} ${i === 0 ? '⭐' : ''}</div>
                            <div class="ds-detail">${s.district} | Grade ${s.category}</div>
                            <div class="ds-cug"><i>📞</i> ${s.cug || s.landline || 'N/A'}</div>
                        </div>
                        <div class="ds-eta">
                            <div class="ds-dist">${s.distance_km} km</div>
                            <div class="ds-time">ETA ~${s.eta_min} min</div>
                        </div>
                    </div>
                `).join('');
            }

            // Draw Step 9: Route Optimization
            if (pathLayer && data.optimized_route) {
                setWorkflowStep(9, 'pulse');
                pathLayer.clearLayers();
                const path = L.polyline(data.optimized_route, {
                    color: '#ffdd00',
                    weight: 4,
                    opacity: 0.8,
                    dashArray: '10, 10',
                    lineJoin: 'round'
                }).addTo(pathLayer);
                
                // Add "Traffic Aware" animation to the route
                path.setStyle({ dashArray: '10, 10', dashOffset: '0' });
                let offset = 0;
                const anim = setInterval(() => {
                    offset -= 2;
                    path.setStyle({ dashOffset: offset.toString() });
                    if (!pathLayer.hasLayer(path)) {
                        clearInterval(anim);
                        setWorkflowStep(9, 'active');
                    }
                }, 100);
            }

            // Show manual report button (Step 6) - now globalized
            const reportBtn = document.getElementById('report-fire-btn');
            if (reportBtn) {
                reportBtn.style.display = 'inline-block';
                reportBtn.dataset.zone = zoneName;
            }
        } catch(e) {
            dispatchResults.innerHTML = `<p style="color:var(--neon-red);font-size:0.75rem;padding:0.5rem;">Error: ${e.message}</p>`;
        }
    }

    function handleMapClick(lat, lng) {
        // Shared map click handler for different modes
        
        // 1. Radar Feedback
        showRadarPing(lat, lng);

        // 2. Manual Plot Mode
        if (isManualPlotActive) {
            let hit = null, minD = 5.0; // 5km radius for 'hitting' a zone
            for (const z of (window._heatmapZones || [])) {
                // Approximate distance check (roughly, very rough but okay for UI)
                const d = Math.hypot(lat - z.lat, lng - z.lng) * 111.32; // Degree to KM
                if (d < minD) { minD = d; hit = z; }
            }
            if (hit) {
                runCalculate(hit.zone);
                fetchAdvisor(); // React immediately
            }
            return;
        }

        // 3. Dispatch Mode (only if not planning)
        if (!planningToggle.checked) {
            let hit = null, minD = 8.0; // 8km radius
            for (const z of (window._heatmapZones || [])) {
                const d = Math.hypot(lat - z.lat, lng - z.lng) * 111.32;
                if (d < minD) { minD = d; hit = z; }
            }
            if (hit) {
                fetchNearestStations(hit.zone);
                // Also trigger a minor update to advisor to note the dispatch focus
                const log = document.createElement('span');
                log.textContent = `> Dispatching inquiry for ${hit.zone}...`;
                advisorLogs.prepend(log);
            }
        }
    }

    // ── Manual Incident Reporting (Step 6) ─────────────
    async function reportManualFire(zoneName) {
        if (!IS_FLASK) return;
        
        // STEP 6: Smart Dispatch & Coordination
        setWorkflowStep(6, 'pulse');
        
        // STEP 5: Emergency Spread Simulation
        setWorkflowStep(5, 'pulse');
        setTimeout(() => setWorkflowStep(5, 'active'), 2000);

        const btn = document.getElementById('report-fire-btn');
        btn.disabled = true;
        btn.textContent = '⏱ DISPATCHING...';

        try {
            const res = await fetch('/api/incidents/report', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ zone: zoneName, type: 'Building Fire' })
            });
            const data = await res.json();
            
            // UI Feedback (Step 11: Logged)
            setWorkflowStep(11, 'pulse');
            const log = document.createElement('span');
            log.className = 'log-success';
            log.textContent = `> [STEP 11] Incident logged & archived for ${zoneName}`;
            advisorLogs.prepend(log);

            fetchIncidents(); // Refresh feed
            showRadarPing(data.incident.lat || 13.08, data.incident.lng || 80.27);
            
            // STEP 10: Alert & Notification
            setWorkflowStep(10, 'pulse');
            
            alert('🚨 EMERGENCY DISPATCH: Unit assigned to ' + zoneName + '. Fastest route mapped.');
            btn.textContent = '✅ DISPATCHED';
            
            setTimeout(() => {
                setWorkflowStep(6, 'active');
                setWorkflowStep(10, 'active');
                setWorkflowStep(11, 'active');
            }, 3000);

        } catch (e) {
            console.error('Report failed:', e);
            btn.textContent = '❌ FAILED';
        } finally {
            setTimeout(() => {
                btn.disabled = false;
                btn.textContent = '🔥 REPORT FIRE';
            }, 5000);
        }
    }

    const reportBtn = document.getElementById('report-fire-btn');
    if (reportBtn) {
        reportBtn.addEventListener('click', () => {
            const zn = reportBtn.dataset.zone;
            if (zn) reportManualFire(zn);
        });
    }

    // Leaflet Click Event listener is added in initMap()

    // Remove the old redundant risk-map listener
    // document.getElementById('risk-map')?.addEventListener('click', ...);

    // 🛸 DRONE SURVEILLANCE LOGIC
    async function fetchDrones() {
        if (!IS_FLASK) return;
        try {
            const res = await fetch('/api/drones');
            const data = await res.json();
            const droneList = document.getElementById('drone-list');
            if (!droneList) return;
            
            droneList.innerHTML = '';
            droneLayer.clearLayers();
            
            data.drones.forEach(d => {
                // Update HUD
                const el = document.createElement('div');
                el.className = 'drone-item';
                el.innerHTML = `
                    <div class="drone-info">
                        <span class="d-id">${d.id}</span>
                        <span class="d-status">${d.status}</span>
                    </div>
                    <div class="d-stats-row">
                        <span>BATT: ${Math.round(d.battery)}%</span>
                        <span style="color:${d.hotspots > 0 ? '#ff3e3e' : '#94a3b8'}">
                            ${d.hotspots > 0 ? '🔥 HOTSPOT' : 'NOMINAL'}
                        </span>
                    </div>
                    <div class="battery-bar">
                        <div class="battery-fill" style="width:${d.battery}%; background:${d.battery < 20 ? '#ff3e3e' : '#00e676'}"></div>
                    </div>
                `;
                droneList.appendChild(el);
                
                // Update Map
                const icon = L.divIcon({
                    className: 'drone-marker',
                    html: `<div class="drone-pulse" style="width:14px; height:14px; background:#00f2ff; border:2px solid #fff; border-radius:3px; box-shadow:0 0 10px #00f2ff; position:relative;">
                            <div style="position:absolute; top:-4px; left:6px; width:2px; height:4px; background:#fff;"></div>
                           </div>`,
                    iconSize: [20, 20]
                });
                L.marker([d.lat, d.lng], { icon }).addTo(droneLayer)
                    .bindPopup(`<strong style="color:#00f2ff;">${d.id}</strong><br/>Status: ${d.status}<br/>Battery: ${Math.round(d.battery)}%`);
            });
        } catch (e) {
            console.warn('[TNFSC] Drone fetch failed.');
        }
    }
    setInterval(fetchDrones, 5000);

    // 🏢 SMART BUILDING SCORES
    const buildingSearch = document.getElementById('building-search');
    const checkBuildingBtn = document.getElementById('check-building-btn');
    const bResult = document.getElementById('building-score-result');

    async function checkBuildingScore() {
        if (!buildingSearch) return;
        const name = buildingSearch.value.trim();
        if (!name) return;
        
        checkBuildingBtn.disabled = true;
        try {
            const res = await fetch(`/api/building/score?name=${encodeURIComponent(name)}`);
            const data = await res.json();
            
            bResult.style.display = 'block';
            document.getElementById('b-res-name').textContent = data.building;
            const gradeBadge = document.getElementById('b-res-grade');
            gradeBadge.textContent = data.grade;
            gradeBadge.className = `grade-badge grade-${data.grade}`;
            document.getElementById('b-res-score').textContent = data.score;
            
            const factorsEl = document.getElementById('b-res-factors');
            factorsEl.innerHTML = Object.entries(data.factors).map(([k, v]) => `
                <span>${k.replace(/_/g, ' ')}: <strong>${v}</strong></span>
            `).join('');
            
        } catch(e) {
            console.warn('[TNFSC] Building score fetch failed.');
        } finally {
            checkBuildingBtn.disabled = false;
        }
    }

    if (checkBuildingBtn) checkBuildingBtn.addEventListener('click', checkBuildingScore);

    // 🔥 FIRE SPREAD SIMULATION
    async function fetchFireSpread(zoneName) {
        if (!IS_FLASK || !spreadLayer) return;
        try {
            const res = await fetch(`/api/spread?zone=${encodeURIComponent(zoneName)}`);
            const data = await res.json();
            
            // STEP 5: Fire Spread Simulation
            setWorkflowStep(5, 'active');
            
            spreadLayer.clearLayers();
            data.spread.forEach((s, idx) => {
                const color = idx === 0 ? '#ff3e3e' : idx === 1 ? '#ff8f00' : '#ffdd00';
                L.polygon(s.polygon, {
                    className: 'fire-spread-poly',
                    color: color,
                    fillColor: color,
                    fillOpacity: 0.3 - (idx * 0.1)
                }).addTo(spreadLayer)
                  .bindTooltip(`T+${s.time} min`, { permanent: false, direction: 'top' });
            });
            
            const log = document.createElement('span');
            log.innerHTML = `> [AI] Spread simulation active for <strong>${zoneName}</strong> (T+30m)`;
            if (advisorLogs) advisorLogs.prepend(log);
            
        } catch(e) {
            console.warn('[TNFSC] Spread simulation failed.');
        }
    }

    // ── VISUAL INTELLIGENCE (CCTV) ──────────────────────────
    let activeCCTVZone = null;
    let cctvInterval = null;

    async function fetchCCTV(zoneName) {
        if (!IS_FLASK) return;
        const hud = document.getElementById('cctv-hud');
        if (hud) hud.style.display = 'block';
        
        try {
            const res = await fetch(`/api/cctv?zone=${encodeURIComponent(zoneName)}`);
            const data = await res.json();
            
            activeCCTVZone = zoneName;
            document.getElementById('cctv-feed-id').textContent = data.feed_id;
            document.getElementById('cctv-zone').textContent = data.zone.toUpperCase();
            document.getElementById('cctv-smoke').textContent = data.vision_ai_data.smoke_density_pct + '%';
            document.getElementById('cctv-thermal').textContent = data.vision_ai_data.thermal_hotspot ? 'ANOMALY' : 'NOMINAL';
            document.getElementById('cctv-thermal').style.color = data.vision_ai_data.thermal_hotspot ? '#ff3e3e' : '#00e676';
            document.getElementById('cctv-timestamp').textContent = new Date().toLocaleTimeString();
            
            const feedStatus = hud.querySelector('.feed-status');
            if (data.anomaly_detected) {
                feedStatus.textContent = 'ALERT';
                feedStatus.style.background = 'rgba(255, 62, 62, 0.3)';
            } else {
                feedStatus.textContent = 'LIVE';
                feedStatus.style.background = 'rgba(255, 62, 62, 0.1)';
            }
        } catch (e) {
            console.warn('[TNFSC] CCTV fetch failed.');
        }
    }

    function startCCTVPulling(zoneName) {
        if (cctvInterval) clearInterval(cctvInterval);
        fetchCCTV(zoneName);
        cctvInterval = setInterval(() => fetchCCTV(zoneName), 5000);
    }

    // Update handleMapClick to include CCTV & spread
    const originalHandleMapClick = handleMapClick;
    handleMapClick = function(lat, lng) {
        originalHandleMapClick(lat, lng);
        
        let hit = null, minD = 10.0;
        for (const z of (window._heatmapZones || [])) {
            const d = Math.hypot(lat - z.lat, lng - z.lng) * 111.32;
            if (d < minD) { minD = d; hit = z; }
        }
        if (hit) {
            fetchFireSpread(hit.zone);
            startCCTVPulling(hit.zone);
        } else {
            if (spreadLayer) spreadLayer.clearLayers();
            const hud = document.getElementById('cctv-hud');
            if (hud) hud.style.display = 'none';
            if (cctvInterval) clearInterval(cctvInterval);
        }
    };
});
