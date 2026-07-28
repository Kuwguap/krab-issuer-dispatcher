"use client";

import "leaflet/dist/leaflet.css";
import { useEffect, useRef, useState } from "react";

const CARTO_URL =
  "https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}@2x.png";
const CARTO_ATTR = "© OpenStreetMap © CARTO";
const ESRI_URL =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const ESRI_ATTR = "Esri";

const DEFAULT_CENTER = [40.7128, -74.006];
const DEFAULT_ZOOM = 11;

const MARKER_HTML =
  '<div class="pulse-marker"><span class="pulse-ring"></span><span class="pulse-ring"></span></div>';

export default function AdminMap({ drivers = [], trails = {}, selectedDriver = null, isMobile = false }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const leafletRef = useRef(null);
  const baseLayersRef = useRef({});
  const markersRef = useRef({}); // driver_id -> L.Marker
  const polylinesRef = useRef({}); // driver_id -> L.Polyline
  const didFitRef = useRef(false);

  const [ready, setReady] = useState(false);
  const [baseLayer, setBaseLayer] = useState("map");

  // Initialize the map once.
  useEffect(() => {
    let cancelled = false;

    async function init() {
      if (mapRef.current || !containerRef.current) return;
      const L = (await import("leaflet")).default;
      if (cancelled || mapRef.current || !containerRef.current) return;

      leafletRef.current = L;

      const map = L.map(containerRef.current, {
        center: DEFAULT_CENTER,
        zoom: DEFAULT_ZOOM,
        zoomControl: false,
      });

      const dark = L.tileLayer(CARTO_URL, { attribution: CARTO_ATTR, maxZoom: 20 });
      const sat = L.tileLayer(ESRI_URL, { attribution: ESRI_ATTR, maxZoom: 19 });
      dark.addTo(map);
      baseLayersRef.current = { map: dark, sat };

      mapRef.current = map;
      setReady(true);
    }

    init();

    return () => {
      cancelled = true;
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
      markersRef.current = {};
      polylinesRef.current = {};
      didFitRef.current = false;
      setReady(false);
    };
  }, []);

  // Base layer toggle.
  useEffect(() => {
    const map = mapRef.current;
    const layers = baseLayersRef.current;
    if (!ready || !map || !layers.map || !layers.sat) return;
    if (baseLayer === "sat") {
      map.removeLayer(layers.map);
      layers.sat.addTo(map);
    } else {
      map.removeLayer(layers.sat);
      layers.map.addTo(map);
    }
  }, [ready, baseLayer]);

  // Sync markers + trails with props.
  useEffect(() => {
    const map = mapRef.current;
    const L = leafletRef.current;
    if (!ready || !map || !L) return;

    // --- Markers ---
    const seen = new Set();
    for (const d of drivers) {
      const ping = d.last_ping;
      if (!ping || !Number.isFinite(Number(ping.lat)) || !Number.isFinite(Number(ping.lng))) {
        continue;
      }
      const key = String(d.driver_id);
      seen.add(key);
      const latLng = [Number(ping.lat), Number(ping.lng)];
      let marker = markersRef.current[key];
      if (marker) {
        marker.setLatLng(latLng);
      } else {
        const icon = L.divIcon({
          html: MARKER_HTML,
          className: "",
          iconSize: [14, 14],
          iconAnchor: [7, 7],
        });
        marker = L.marker(latLng, { icon }).addTo(map);
        markersRef.current[key] = marker;
      }
      const name = d.driver_name || `Driver ${key}`;
      marker.bindTooltip(name, { direction: "top", offset: [0, -10] });
    }
    for (const key of Object.keys(markersRef.current)) {
      if (!seen.has(key)) {
        map.removeLayer(markersRef.current[key]);
        delete markersRef.current[key];
      }
    }

    // --- Trails ---
    const seenTrails = new Set();
    for (const [key, points] of Object.entries(trails || {})) {
      const coords = (points || [])
        .map((p) => [Number(p[0]), Number(p[1])])
        .filter((c) => Number.isFinite(c[0]) && Number.isFinite(c[1]));
      if (coords.length < 2) continue;
      seenTrails.add(key);
      let line = polylinesRef.current[key];
      if (line) {
        line.setLatLngs(coords);
      } else {
        line = L.polyline(coords, {
          color: "#f59e0b",
          weight: 3,
          opacity: 0.85,
        }).addTo(map);
        polylinesRef.current[key] = line;
      }
    }
    for (const key of Object.keys(polylinesRef.current)) {
      if (!seenTrails.has(key)) {
        map.removeLayer(polylinesRef.current[key]);
        delete polylinesRef.current[key];
      }
    }

    // --- Initial fit ---
    if (!didFitRef.current) {
      const markerList = Object.values(markersRef.current);
      if (markerList.length > 0) {
        const group = L.featureGroup(markerList);
        map.fitBounds(group.getBounds().pad(0.25), { maxZoom: 15 });
        didFitRef.current = true;
      }
    }
  }, [ready, drivers, trails]);

  // Selected driver -> fit their trail (or center on last ping).
  useEffect(() => {
    const map = mapRef.current;
    const L = leafletRef.current;
    if (!ready || !map || !L || !selectedDriver) return;
    const key = String(selectedDriver);
    const line = polylinesRef.current[key];
    if (line) {
      map.fitBounds(line.getBounds(), { padding: [40, 40] });
      return;
    }
    const marker = markersRef.current[key];
    if (marker) {
      map.setView(marker.getLatLng(), 15);
    }
  }, [ready, selectedDriver]);

  function recenter() {
    const map = mapRef.current;
    const L = leafletRef.current;
    if (!map || !L) return;
    const markerList = Object.values(markersRef.current);
    if (markerList.length > 0) {
      const group = L.featureGroup(markerList);
      map.fitBounds(group.getBounds().pad(0.25), { maxZoom: 15 });
    } else {
      map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    }
  }

  const btnSize = isMobile ? 38 : 42;
  const roundBtnStyle = {
    width: btnSize,
    height: btnSize,
    borderRadius: "50%",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: isMobile ? 18 : 20,
    fontWeight: 700,
    color: "var(--text)",
  };

  // On phones the top edge is taken by the driver list + hours chips, so the
  // layer toggle moves to the bottom-left (opposite the zoom stack).
  const toggleStyle = isMobile
    ? { position: "absolute", left: 12, bottom: 90, zIndex: 1000, display: "flex", padding: 3, gap: 2 }
    : {
        position: "absolute",
        top: 16,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 1000,
        display: "flex",
        padding: 3,
        gap: 2,
      };

  return (
    <div style={{ position: "absolute", inset: 0 }}>
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />

      {/* Map | Satellite toggle */}
      <div className="glass-chip" style={toggleStyle}>
        {[
          { key: "map", label: "Map" },
          { key: "sat", label: isMobile ? "Sat" : "Satellite" },
        ].map((opt) => (
          <button
            key={opt.key}
            onClick={() => setBaseLayer(opt.key)}
            style={{
              padding: isMobile ? "6px 11px" : "6px 14px",
              borderRadius: 7,
              fontSize: 13,
              fontWeight: 700,
              background: baseLayer === opt.key ? "var(--accent)" : "transparent",
              color: baseLayer === opt.key ? "#170e02" : "var(--text-muted)",
            }}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* Zoom / recenter controls (bottom right) */}
      <div
        style={{
          position: "absolute",
          right: isMobile ? 12 : 16,
          bottom: 90,
          zIndex: 1000,
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}
      >
        <button
          className="glass-chip"
          style={roundBtnStyle}
          onClick={() => mapRef.current && mapRef.current.zoomIn()}
          aria-label="Zoom in"
        >
          +
        </button>
        <button
          className="glass-chip"
          style={roundBtnStyle}
          onClick={() => mapRef.current && mapRef.current.zoomOut()}
          aria-label="Zoom out"
        >
          −
        </button>
        <button
          className="glass-chip"
          style={{ ...roundBtnStyle, fontSize: 18 }}
          onClick={recenter}
          aria-label="Recenter"
        >
          ⌖
        </button>
      </div>
    </div>
  );
}
