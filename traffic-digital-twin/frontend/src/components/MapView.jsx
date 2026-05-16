import { useMemo } from "react";
import DeckGL from "@deck.gl/react";
import { ScatterplotLayer, TextLayer, PolygonLayer, IconLayer } from "@deck.gl/layers";
import Map from "react-map-gl/maplibre";
import { getVehicleColor } from "../utils/colorMap";

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

const MAP_STYLE =
  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

const VEHICLE_MIN_ZOOM = 15;

// CCTV 지면 커버리지: 사다리꼴 (가까운 쪽 좁고 먼 쪽 넓음 — 실제 원근 투영)
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
  onMapClick,
}) {
  const showVehicles = viewState.zoom >= VEHICLE_MIN_ZOOM;

  const sorted = useMemo(
    () => [...vehicles].sort((a, b) => (a.is_speeding ? 1 : 0) - (b.is_speeding ? 1 : 0)),
    [vehicles]
  );

  // ── 카메라 클릭 영역 (투명 ScatterplotLayer — 히트 테스트용) ──────────
  const cctvHitLayer = new ScatterplotLayer({
    id:              "cctvs-hit",
    data:            cctvList,
    getPosition:     (d) => [d.lon, d.lat],
    getRadius:       18,
    getFillColor:    [0, 0, 0, 0],   // 완전 투명
    getLineColor:    [0, 0, 0, 0],
    radiusUnits:     "pixels",
    pickable:        !calibrationMode,
    onClick:         ({ object }) => !calibrationMode && object && onCctvClick?.(object),
  });

  // ── 카메라 아이콘 (SVG IconLayer — 이모지 WebGL 렌더링 문제 우회) ───
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

  // ── 카메라 이름 라벨 ──────────────────────────────────────────────────
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

  // ── 선택된 카메라 시야 범위 (PolygonLayer) ────────────────────────────
  const fovLayer = useMemo(() => {
    if (!selectedCctv) return null;
    const ring = computeFovPolygon(
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

  // ── 차량 레이어 ────────────────────────────────────────────────────────
  const scatterLayer = new ScatterplotLayer({
    id:           "vehicles",
    data:         sorted,
    getPosition:  (d) => [d.lon, d.lat],
    getRadius:    3,
    getFillColor: (d) =>
      d.is_parked ? [80, 80, 80, 140] : getVehicleColor(d.direction, false),
    pickable:     true,
    radiusUnits:  "meters",
    radiusMinPixels: 5,
    updateTriggers: { getFillColor: vehicles },
  });

  const textLayer = new TextLayer({
    id:             "vehicle-labels",
    data:           sorted,
    getPosition:    (d) => [d.lon, d.lat],
    getText:        (d) => `#${d.track_id}`,
    getSize:        11,
    getColor:       [255, 255, 255, 200],
    getPixelOffset: [0, -14],
    updateTriggers: { getText: vehicles },
  });

  const layers = [
    ...(showVehicles ? extraLayers : []),
    fovLayer,           // FOV 시야 범위 (선택 시)
    cctvHitLayer,       // 투명 히트 영역
    cctvIconLayer,      // 📷 아이콘
    cctvLabelLayer,     // 카메라 이름
    ...(showVehicles ? [scatterLayer, textLayer] : []),
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
              ${d.is_parked ? ' &nbsp;<span style="color:#9ca3af">🅿 주차</span>' : ""}<br/>
              방향: <b>${d.direction}</b><br/>
              속도: <b>${d.speed_kph?.toFixed(1) ?? "—"} km/h</b>
              ${d.is_speeding ? ' &nbsp;<span style="color:#f87171">🚨 과속</span>' : ""}<br/>
              체류: ${d.dwell_frames}f
              ${d.is_bottleneck ? ' &nbsp;<span style="color:#fbbf24">⚠ 병목</span>' : ""}
            `,
            style: {
              background: "#111827", color: "#f9fafb",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        if (d.id) {
          return {
            html: `<b>📷 ${d.name || ""}</b><br/><span style="color:#9ca3af;font-size:11px">클릭 → 실시간 전환 · 시야 범위 표시</span>`,
            style: {
              background: "#111827", color: "#fbbf24",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        return null;
      }}
    >
      <Map mapStyle={MAP_STYLE} />
    </DeckGL>
  );
}
