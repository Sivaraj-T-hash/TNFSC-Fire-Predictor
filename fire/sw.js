/* ═══════════════════════════════════════════════════════════════════
   TNFSC Smart Fire Intelligence Portal — Service Worker
   Handles background Web Push notifications for mobile/desktop alerts.
═══════════════════════════════════════════════════════════════════ */

const CACHE_NAME = 'tnfsc-sw-v1';

// ── Install ──────────────────────────────────────────────────────
self.addEventListener('install', (event) => {
    console.log('[TNFSC-SW] Service Worker installed.');
    self.skipWaiting();
});

// ── Activate ─────────────────────────────────────────────────────
self.addEventListener('activate', (event) => {
    console.log('[TNFSC-SW] Service Worker activated.');
    event.waitUntil(clients.claim());
});

// ── Push Event ─────────────────────────────────────────────────────
// Fired whenever the Flask backend sends a Web Push message
self.addEventListener('push', (event) => {
    console.log('[TNFSC-SW] Push received:', event);

    let payload = {
        title: '🚨 TNFRS FIRE RISK ALERT',
        body: 'Critical fire risk detected. Tap to view Command Center.',
        zone: 'Unknown Zone',
        risk: '—',
        level: 'CRITICAL',
        icon: '/icon-192.png',
        badge: '/icon-192.png',
        tag: 'tnfsc-alert',
        requireInteraction: true,
    };

    // Parse JSON payload from backend if available
    if (event.data) {
        try {
            const data = event.data.json();
            payload.title = data.title || payload.title;
            payload.body  = data.body  || payload.body;
            payload.zone  = data.zone  || payload.zone;
            payload.risk  = data.risk  || payload.risk;
            payload.level = data.level || payload.level;
            // Override tag per zone so multiple alerts appear simultaneously
            payload.tag   = `tnfsc-alert-${data.zone || 'global'}`;
        } catch (e) {
            // Fallback: treat as plain text
            payload.body = event.data.text() || payload.body;
        }
    }

    const notifOptions = {
        body:               payload.body,
        icon:               payload.icon,
        badge:              payload.badge,
        tag:                payload.tag,
        requireInteraction: payload.requireInteraction,
        vibrate:            [300, 100, 400, 100, 300],
        data: {
            url:   self.registration.scope,
            zone:  payload.zone,
            risk:  payload.risk,
            level: payload.level
        },
        actions: [
            { action: 'view',    title: '📡 View Command Center' },
            { action: 'dismiss', title: '✕ Dismiss'              }
        ]
    };

    event.waitUntil(
        self.registration.showNotification(payload.title, notifOptions)
    );
});

// ── Notification Click ─────────────────────────────────────────────
self.addEventListener('notificationclick', (event) => {
    event.notification.close();

    if (event.action === 'dismiss') return;

    // Focus existing portal tab or open a new one
    const targetUrl = event.notification.data?.url || self.registration.scope;
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then((windowClients) => {
                for (const client of windowClients) {
                    if (client.url.startsWith(targetUrl) && 'focus' in client) {
                        return client.focus();
                    }
                }
                return clients.openWindow(targetUrl);
            })
    );
});
