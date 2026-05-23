import { useMemo } from "react";
import DeckGL from "@deck.gl/react";
import { ScatterplotLayer, TextLayer, PolygonLayer, IconLayer } from "@deck.gl/layers";
import Map from "react-map-gl/maplibre";
import { getVehicleColor } from "../utils/colorMap";
import { useLang } from "../i18n/index.jsx";

function makeCameraIconUrl(selected) {
  const stroke  = selected ? "#22d3ee" : "#fbbf24";
  const bg      = selected ? "#0e3a44" : "#1a1200";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" width="40" height="40">
    <rect x="2" y="2" width="36" height="36" rx="8" fill="${bg}" stroke="${stroke}" stroke-width="2"/>
    <rect x="4" y="13" width="21" height="12" rx="3" fill="${stroke}" opacity="0.9"/>
    <circle cx="29" cy="19" r="6.5" fill="${bg}" stroke="${stroke}" stroke-width="2"/>
    <circle cx="29" cy="19" r="3.5" fill="${stroke}" opacity="0.85"/>
    <circle cx="29" cy="19" r="1.5" fill="${bg}"/>
    <rect x="11" y="24" width="3.5" height="5" rx="1" fill="${stroke}" opacity="0.85"/>
    <rect x="7" y="28" width="12" height="3" rx="1.5" fill="${stroke}" opacity="0.85"/>
  </svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
const ICON_NORMAL   = makeCameraIconUrl(false);
const ICON_SELECTED = makeCameraIconUrl(true);

const MAP_STYLES = {
  dark:      "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  light:     "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  satellite: {
    version: 8,
    sources: {
      satellite: {
        type: "raster",
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
        tileSize: 256,
        attribution: "Esri World Imagery",
      },
    },
    layers: [{ id: "satellite", type: "raster", source: "satellite" }],
  },
};

const MAP_MODE_ICONS = { dark: "🌙", light: "☀️", satellite: "🛰️" };
const MAP_MODE_NEXT  = { dark: "light", light: "satellite", satellite: "dark" };

const VEHICLE_MIN_ZOOM = 15;

function computeFovPolygon(lat, lon, headingDeg = 0, fovDeg = 70, distM = 90, nearM = 15) {
  const R = 6371000;
  const dLatPerM = (1 / R) * (180 / Math.PI);
  const dLonPerM = (1 / (R * Math.cos((lat * Math.PI) / 180))) * (180 / Math.PI);

  const half = ((fovDeg / 2) * Math.PI) / 180;
  const hdg  = (headingDeg * Math.PI) / 180;

  const nearLeftLon  = lon + dLonPerM * nearM * Math.sin(hdg - half);
  const nearLeftLat  = lat + dLatPerM * nearM * Math.cos(hdg - half);
  const nearRightLon = lon + dLonPerM * nearM * Math.sin(hdg + half);
  const nearRightLat = lat + dLatPerM * nearM * Math.cos(hdg + half);

  const farLeftLon  = lon + dLonPerM * distM * Math.sin(hdg - half);
  const farLeftLat  = lat + dLatPerM * distM * Math.cos(hdg - half);
  const farRightLon = lon + dLonPerM * distM * Math.sin(hdg + half);
  const farRightLat = lat + dLatPerM * distM * Math.cos(hdg + half);

  return [
    [nearLeftLon, nearLeftLat],
    [nearRightLon, nearRightLat],
    [farRightLon, farRightLat],
    [farLeftLon, farLeftLat],
    [nearLeftLon, nearLeftLat],
  ];
}

export default function MapView({
  vehicles = [],
  extraLayers = [],
  cctvList = [],
  selectedCctv = null,
  viewState,
  onViewStateChange,
  onCctvClick,
  calibrationMode = false,
  snapNodes = [],
  onMapClick,
  mapMode = "dark",
  onMapModeChange,
}) {
  const { t } = useLang();
  const showVehicles = viewState.zoom >= VEHICLE_MIN_ZOOM;

  const sorted = useMemo(
    () => [...vehicles].sort((a, b) => (a.is_speeding ? 1 : 0) - (b.is_speeding ? 1 : 0)),
    [vehicles]
  );

  const cctvHitLayer = new ScatterplotLayer({
    id:           "cctvs-hit",
    data:         cctvList,
    getPosition:  (d) => [d.lon, d.lat],
    getRadius:    18,
    getFillColor: [0, 0, 0, 0],
    getLineColor: [0, 0, 0, 0],
    radiusUnits:  "pixels",
    pickable:     !calibrationMode,
    onClick:      ({ object }) => !calibrationMode && object && onCctvClick?.(object),
  });

  const cctvIconLayer = new IconLayer({
    id:          "cctv-icons",
    data:        cctvList,
    getPosition: (d) => [d.lon, d.lat],
    getIcon:     (d) => ({
      url:    selectedCctv?.id === d.id ? ICON_SELECTED : ICON_NORMAL,
      width:  40,
      height: 40,
    }),
    getSize:        (d) => selectedCctv?.id === d.id ? 48 : 36,
    sizeUnits:      "pixels",
    getPixelOffset: [0, 0],
    pickable:       false,
    updateTriggers: { getIcon: [selectedCctv?.id], getSize: [selectedCctv?.id] },
  });

  const cctvLabelLayer = new TextLayer({
    id:             "cctv-labels",
    data:           cctvList,
    getPosition:    (d) => [d.lon, d.lat],
    getText:        (d) => d.name || String(d.id),
    getSize:        (d) => selectedCctv?.id === d.id ? 13 : 11,
    getColor:       (d) => selectedCctv?.id === d.id ? [34, 211, 238, 255] : [253, 230, 138, 230],
    getPixelOffset: [0, -30],
    fontFamily:     '"Segoe UI", system-ui, sans-serif',
    fontWeight:     700,
    fontSettings:   { sdf: true, smoothing: 0.3 },
    outlineWidth:   3,
    outlineColor:   [0, 0, 0, 200],
    updateTriggers: { getSize: [selectedCctv?.id], getColor: [selectedCctv?.id] },
  });

  const fovLayer = useMemo(() => {
    if (!selectedCctv) return null;
    const ring = selectedCctv.calibGpsRing ?? computeFovPolygon(
      selectedCctv.lat,
      selectedCctv.lon,
      selectedCctv.heading ?? 0,
    );
    return new PolygonLayer({
      id:             "cctv-fov",
      data:           [{ ring }],
      getPolygon:     (d) => d.ring,
      getFillColor:   [34, 211, 238, 30],
      getLineColor:   [34, 211, 238, 160],
      lineWidthMinPixels: 1.5,
      stroked:        true,
      filled:         true,
    });
  }, [selectedCctv]);

  const nodeStroked = mapMode !== "dark";
  const nodeOutline = mapMode === "satellite" ? [0, 0, 0, 230] : [80, 80, 80, 180];
  const parkedColor = mapMode === "light" ? [120, 120, 120, 160] : [80, 80, 80, 140];

  const scatterLayer = new ScatterplotLayer({
    id:           "vehicles",
    data:         sorted,
    getPosition:  (d) => [d.lon, d.lat],
    getRadius:    3,
    getFillColor: (d) => d.is_parked ? parkedColor : getVehicleColor(d.direction, mapMode !== "dark"),
    getLineColor:    nodeOutline,
    lineWidthMinPixels: nodeStroked ? 1.5 : 0,
    stroked:      nodeStroked,
    pickable:     true,
    radiusUnits:  "meters",
    radiusMinPixels: 5,
    updateTriggers: { getFillColor: [vehicles, mapMode], getLineColor: mapMode },
  });

  const labelColor = mapMode === "light" ? [30, 30, 30, 220] : [255, 255, 255, 200];
  const textLayer = new TextLayer({
    id:             "vehicle-labels",
    data:           sorted,
    getPosition:    (d) => [d.lon, d.lat],
    getText:        (d) => `#${d.track_id}`,
    getSize:        11,
    getColor:       labelColor,
    getPixelOffset: [0, -14],
    outlineWidth:   mapMode !== "dark" ? 2 : 0,
    outlineColor:   mapMode === "light" ? [255, 255, 255, 200] : [0, 0, 0, 180],
    updateTriggers: { getText: vehicles, getColor: mapMode },
  });

  const snapNodeLayer = calibrationMode && snapNodes.length > 0
    ? new ScatterplotLayer({
        id:           "snap-nodes",
        data:         snapNodes,
        getPosition:  (d) => [d.lon, d.lat],
        getRadius:    6,
        radiusUnits:  "pixels",
        getFillColor: [251, 191, 36, 220],
        getLineColor: [255, 255, 255, 255],
        lineWidthMinPixels: 2,
        stroked:      true,
        pickable:     true,
      })
    : null;

  const snapNodeLabelLayer = calibrationMode && snapNodes.length > 0
    ? new TextLayer({
        id:             "snap-node-labels",
        data:           snapNodes,
        getPosition:    (d) => [d.lon, d.lat],
        getText:        (d) => d.node_name || d.node_id,
        getSize:        10,
        getColor:       [251, 191, 36, 220],
        getPixelOffset: [0, 14],
        outlineWidth:   2,
        outlineColor:   [0, 0, 0, 200],
        pickable:       false,
      })
    : null;

  const layers = [
    ...(showVehicles ? extraLayers : []),
    fovLayer,
    cctvHitLayer,
    cctvIconLayer,
    cctvLabelLayer,
    ...(showVehicles ? [scatterLayer, textLayer] : []),
    snapNodeLayer,
    snapNodeLabelLayer,
  ].filter(Boolean);

  return (
    <DeckGL
      viewState={viewState}
      onViewStateChange={({ viewState: vs }) => onViewStateChange(vs)}
      controller
      layers={layers}
      style={{ position: "relative", width: "100%", height: "100%", cursor: calibrationMode ? "crosshair" : "grab" }}
      onClick={calibrationMode ? onMapClick : undefined}
      getTooltip={({ object: d }) => {
        if (!d) return null;

        if (d.track_id !== undefined) {
          return {
            html: `
              <b>#${d.track_id}</b> &nbsp; <span style="color:#aaa">${d.class_name}</span>
              ${d.is_parked ? ` &nbsp;<span style="color:#9ca3af">${t("map.parked")}</span>` : ""}<br/>
              ${t("map.dir")}: <b>${d.direction}</b><br/>
              ${t("map.speed")}: <b>${d.speed_kph?.toFixed(1) ?? "—"} km/h</b>
              ${d.is_speeding ? ` &nbsp;<span style="color:#f87171">${t("map.speeding")}</span>` : ""}<br/>
              ${t("map.dwell")}: ${d.dwell_frames}f
              ${d.is_bottleneck ? ` &nbsp;<span style="color:#fbbf24">${t("map.bottleneck")}</span>` : ""}
            `,
            style: {
              background: "#111827", color: "#f9fafb",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        if (d.node_id) {
          return {
            html: `<b>📍 ${d.node_name || d.node_id}</b><br/><span style="color:#9ca3af;font-size:11px">클릭하면 이 노드 GPS 사용</span>`,
            style: {
              background: "#111827", color: "#fbbf24",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        if (d.id) {
          return {
            html: `<b>📷 ${d.name || ""}</b><br/><span style="color:#9ca3af;font-size:11px">${t("map.clickHint")}</span>`,
            style: {
              background: "#111827", color: "#fbbf24",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        return null;
      }}
    >
      <Map mapStyle={MAP_STYLES[mapMode] ?? MAP_STYLES.dark} />

      <button
        onClick={() => onMapModeChange?.(MAP_MODE_NEXT[mapMode])}
        title={t("map.modeToggle", { mode: mapMode, next: MAP_MODE_NEXT[mapMode] })}
        style={{
          position: "absolute", top: 12, right: 12,
          background: "rgba(17,24,39,0.88)", border: "1px solid #374151",
          borderRadius: 8, padding: "6px 10px", cursor: "pointer",
          fontSize: 18, lineHeight: 1,
          backdropFilter: "blur(4px)", zIndex: 10,
        }}
      >
        {MAP_MODE_ICONS[mapMode]}
      </button>
    </DeckGL>
  );
}
