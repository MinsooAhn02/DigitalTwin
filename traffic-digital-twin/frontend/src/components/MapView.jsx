import { useMemo } from "react";
import DeckGL from "@deck.gl/react";
import { ScatterplotLayer, TextLayer, PolygonLayer } from "@deck.gl/layers";
import Map from "react-map-gl/maplibre";
import { getVehicleColor } from "../utils/colorMap";

const MAP_STYLE =
  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

const VEHICLE_MIN_ZOOM = 15;

/**
 * 카메라 시야 범위 삼각형 계산.
 * ITS API가 heading을 제공하지 않으므로 기본값 0°(북쪽) 사용.
 * Calibration 완료 후 per-camera heading으로 교체 가능.
 */
function computeFovPolygon(lat, lon, headingDeg = 0, fovDeg = 70, distM = 90) {
  const R = 6371000;
  const dLatPerM = (1 / R) * (180 / Math.PI);
  const dLonPerM = (1 / (R * Math.cos((lat * Math.PI) / 180))) * (180 / Math.PI);

  const half = ((fovDeg / 2) * Math.PI) / 180;
  const hdg  = (headingDeg * Math.PI) / 180;

  const leftLon  = lon + dLonPerM * distM * Math.sin(hdg - half);
  const leftLat  = lat + dLatPerM * distM * Math.cos(hdg - half);
  const rightLon = lon + dLonPerM * distM * Math.sin(hdg + half);
  const rightLat = lat + dLatPerM * distM * Math.cos(hdg + half);

  // 닫힌 ring: [카메라, 왼쪽 끝, 오른쪽 끝]
  return [[lon, lat], [leftLon, leftLat], [rightLon, rightLat], [lon, lat]];
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

  // ── 카메라 아이콘 (📷 이모지 TextLayer) ───────────────────────────────
  const cctvIconLayer = new TextLayer({
    id:             "cctv-icons",
    data:           cctvList,
    getPosition:    (d) => [d.lon, d.lat],
    getText:        () => "📷",
    getSize:        (d) => selectedCctv?.id === d.id ? 26 : 20,
    getColor:       (d) => selectedCctv?.id === d.id ? [34, 211, 238, 255] : [251, 191, 36, 230],
    getPixelOffset: [0, 0],
    fontFamily:     '"Segoe UI Emoji", "Apple Color Emoji", sans-serif',
    fontSettings:   { sdf: false },
    updateTriggers: { getSize: [selectedCctv], getColor: [selectedCctv] },
  });

  // ── 카메라 이름 라벨 ──────────────────────────────────────────────────
  const cctvLabelLayer = new TextLayer({
    id:             "cctv-labels",
    data:           cctvList,
    getPosition:    (d) => [d.lon, d.lat],
    getText:        (d) => d.name || d.id,
    getSize:        11,
    getColor:       [253, 230, 138, 220],
    getPixelOffset: [0, -28],
    fontFamily:     '"Segoe UI", system-ui, sans-serif',
    fontWeight:     600,
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
    getRadius:    (d) => (d.is_speeding ? 5 : 3),
    getFillColor: (d) =>
      d.is_parked ? [80, 80, 80, 140] : getVehicleColor(d.direction, d.is_speeding),
    pickable:     true,
    radiusUnits:  "meters",
    radiusMinPixels: 5,
    updateTriggers: { getFillColor: vehicles, getRadius: vehicles },
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
