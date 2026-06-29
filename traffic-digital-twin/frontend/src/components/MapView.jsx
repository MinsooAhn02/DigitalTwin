import { useCallback, useMemo, useRef, useEffect, useState } from "react";
import DeckGL from "@deck.gl/react";
import { ScatterplotLayer, TextLayer, PolygonLayer, IconLayer, PathLayer } from "@deck.gl/layers";
import Map from "react-map-gl/maplibre";
import { getVehicleColor, getSeverityColor } from "../utils/colorMap";
import { useLang } from "../i18n/index.jsx";

const BG_STATUS_COLORS = {
  dark: {
    selected:  { stroke: "#22d3ee", bg: "#0e3a44" },
    normal:    { stroke: "#22c55e", bg: "#0a2210" },
    busy:      { stroke: "#f97316", bg: "#2a1200" },
    congested: { stroke: "#ef4444", bg: "#2a0000" },
    loading:   { stroke: "#94a3b8", bg: "#334155" },
    error:     { stroke: "#9ca3af", bg: "#27272a" },
    default:   { stroke: "#cbd5e1", bg: "#475569" },
  },
  light: {
    selected:  { stroke: "#0891b2", bg: "#e0f2fe" },
    normal:    { stroke: "#15803d", bg: "#dcfce7" },
    busy:      { stroke: "#c2410c", bg: "#fff7ed" },
    congested: { stroke: "#b91c1c", bg: "#fee2e2" },
    loading:   { stroke: "#475569", bg: "#f8fafc" },
    error:     { stroke: "#4b5563", bg: "#f9fafb" },
    default:   { stroke: "#64748b", bg: "#f1f5f9" },
  },
};

function makeCameraIconUrl(status, theme = "dark") {
  const palette = BG_STATUS_COLORS[theme] ?? BG_STATUS_COLORS.dark;
  const { stroke, bg } = palette[status] ?? palette.default;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 46" width="36" height="46">
    <path d="M18 1 C8.6 1 1 8.6 1 18 C1 29 18 45 18 45 C18 45 35 29 35 18 C35 8.6 27.4 1 18 1Z" fill="${bg}" stroke="${stroke}" stroke-width="1.8"/>
    <rect x="8" y="10" width="15" height="11" rx="2.5" fill="${stroke}" opacity="0.92"/>
    <circle cx="15.5" cy="15.5" r="4" fill="${bg}"/>
    <circle cx="15.5" cy="15.5" r="2.2" fill="${stroke}" opacity="0.88"/>
    <circle cx="15.5" cy="15.5" r="0.9" fill="${bg}"/>
    <rect x="23" y="11.5" width="3.5" height="4" rx="1" fill="${stroke}" opacity="0.82"/>
  </svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

const ICON_KEYS = ["selected", "normal", "busy", "congested", "loading", "error", "default"];
const CAMERA_ICONS_DARK  = Object.fromEntries(ICON_KEYS.map((k) => [k, makeCameraIconUrl(k, "dark")]));
const CAMERA_ICONS_LIGHT = Object.fromEntries(ICON_KEYS.map((k) => [k, makeCameraIconUrl(k, "light")]));

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

// 기본 FOV 사다리꼴 (캘리브 없을 때)
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

// 도로 중심선(road_pts) 따라 곡선 polygon 생성
// road_pts: [[lat, lon], ...] F→T 순서
// snapAlongM: road_pts[0]에서 snap까지의 거리(m)
// headingDeg: 카메라 방향 (도로 중심선과 비교해 F→T / T→F 판별)
// nearM, farM: snap 기준 앞/뒤 거리
// halfWidthM: 도로 폭의 절반
function computeRoadCorridorPolygon(roadPts, snapAlongM, headingDeg, nearM, farM, halfWidthM) {
  const R_lat = 110574;
  if (!roadPts || roadPts.length < 2) return null;

  // 누적 거리 계산
  const cumDist = [0];
  for (let i = 1; i < roadPts.length; i++) {
    const R_lon = 111320 * Math.cos(roadPts[i - 1][0] * Math.PI / 180);
    cumDist.push(
      cumDist[i - 1] +
      Math.hypot(
        (roadPts[i][0] - roadPts[i - 1][0]) * R_lat,
        (roadPts[i][1] - roadPts[i - 1][1]) * R_lon,
      ),
    );
  }
  const totalLen = cumDist[cumDist.length - 1];

  // snap에 가장 가까운 인덱스 찾기
  const snapIdx = cumDist.reduce(
    (best, d, i) => Math.abs(d - snapAlongM) < Math.abs(cumDist[best] - snapAlongM) ? i : best, 0
  );
  const i0 = Math.min(snapIdx, roadPts.length - 2);
  const p0 = roadPts[i0], p1 = roadPts[i0 + 1];
  const R_lon0 = 111320 * Math.cos(p0[0] * Math.PI / 180);
  const roadBearFT = (Math.atan2(
    (p1[1] - p0[1]) * R_lon0,
    (p1[0] - p0[0]) * R_lat,
  ) * 180 / Math.PI + 360) % 360;

  // 카메라가 F→T 방향을 보는지 T→F 방향을 보는지 판단
  const bearDiff = ((headingDeg - roadBearFT + 180) % 360) - 180;
  const fwdIsFT = Math.abs(bearDiff) < 90;

  // snap 기준 near/far 거리를 F→T 누적 거리계로 변환
  // fwdIsFT=true: 카메라가 F→T 방향을 봄 → near/far 모두 snap보다 앞(cumDist 증가 방향)
  // fwdIsFT=false: 카메라가 T→F 방향을 봄 → near/far 모두 snap보다 뒤(cumDist 감소 방향)
  const nearDist = fwdIsFT ? snapAlongM + nearM : snapAlongM - nearM;
  const farDist  = fwdIsFT ? snapAlongM + farM  : snapAlongM - farM;
  const startD   = Math.max(0, Math.min(nearDist, farDist));
  const endD     = Math.min(totalLen, Math.max(nearDist, farDist));
  if (endD <= startD) return null;

  // 누적 거리 d에 해당하는 보간 점 반환
  function interp(d) {
    for (let i = 0; i < roadPts.length - 1; i++) {
      if (cumDist[i] <= d && d <= cumDist[i + 1]) {
        const frac = (d - cumDist[i]) / Math.max(1e-9, cumDist[i + 1] - cumDist[i]);
        return [
          roadPts[i][0] + frac * (roadPts[i + 1][0] - roadPts[i][0]),
          roadPts[i][1] + frac * (roadPts[i + 1][1] - roadPts[i][1]),
        ];
      }
    }
    return roadPts[roadPts.length - 1];
  }

  // startD ~ endD 구간의 중심선 점 수집
  const center = [interp(startD)];
  for (let i = 0; i < roadPts.length; i++) {
    if (cumDist[i] > startD && cumDist[i] < endD) center.push(roadPts[i]);
  }
  center.push(interp(endD));

  // 각 점에서 도로 방향에 수직으로 ±halfWidthM 오프셋
  function offsetPts(pts, side) {
    return pts.map((pt, i) => {
      const prev = pts[i > 0 ? i - 1 : 0];
      const next = pts[i < pts.length - 1 ? i + 1 : pts.length - 1];
      const R_lon = 111320 * Math.cos(pt[0] * Math.PI / 180);
      const dx = (next[1] - prev[1]) * R_lon; // 동(East) 성분 (미터)
      const dy = (next[0] - prev[0]) * R_lat; // 북(North) 성분 (미터)
      const len = Math.hypot(dx, dy) || 1e-9;
      // 오른쪽 수직: (dy, -dx) / len × halfWidthM
      const perpE =  (dy / len) * halfWidthM * side;
      const perpN = -(dx / len) * halfWidthM * side;
      return [pt[0] + perpN / R_lat, pt[1] + perpE / R_lon];
    });
  }

  const right = offsetPts(center,  1);
  const left  = offsetPts(center, -1);

  // polygon: 오른쪽 엣지 → 왼쪽 엣지(역순) → 닫기
  return [
    ...right,
    ...left.slice().reverse(),
    right[0],
  ].map(([lat, lon]) => [lon, lat]); // deck.gl: [lon, lat]
}

// 자동 캘리브레이션 후 — transform.py의 GPS 코너와 동일한 방식으로 계산
// near/far 모두 road_width_m 고정 폭의 직사각형 (호모그래피 실제 사용 영역)
function computeCalibPolygon(lat, lon, headingDeg, nearM, farM, halfWidthM) {
  const R_lat = 110574;
  const R_lon = 111320 * Math.cos((lat * Math.PI) / 180);
  const b = (headingDeg * Math.PI) / 180;
  const sinB = Math.sin(b), cosB = Math.cos(b);

  return [
    [-halfWidthM, nearM],
    [ halfWidthM, nearM],
    [ halfWidthM, farM ],
    [-halfWidthM, farM ],
    [-halfWidthM, nearM],
  ].map(([lateral, along]) => {
    const dlat = (along * cosB - lateral * sinB) / R_lat;
    const dlon = (along * sinB + lateral * cosB) / R_lon;
    return [lon + dlon, lat + dlat];
  });
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
  fovNearM = null,
  fovFarM = null,
  fovRoadWidthM = null,
  fovHeadingDeg = null,
  fovSnapLat = null,
  fovSnapLon = null,
  fovRoadPts = null,
  fovSnapAlongM = null,
  fovRoiGpsRing = null,
  backgroundStatus = {},
  congestionClusters = [],
}) {
  const { t, lang } = useLang();
  const mapRef = useRef(null);
  const [fontReady, setFontReady] = useState(false);

  // 같은 위치(≈16m) 카메라는 대표 1개만 표시 — 팝업 없이 아이콘/레이블 중복 방지.
  // 선택된 카메라가 그룹 내에 있으면 그 카메라를 대표로 우선 사용.
  const singles = useMemo(() => {
    const THRESH = 0.00015;
    const assigned = new Set();
    const result = [];
    for (let i = 0; i < cctvList.length; i++) {
      if (assigned.has(i)) continue;
      const a = cctvList[i];
      const group = [i];
      assigned.add(i);
      for (let j = i + 1; j < cctvList.length; j++) {
        if (assigned.has(j)) continue;
        const b = cctvList[j];
        if (Math.abs(a.lat - b.lat) < THRESH && Math.abs(a.lon - b.lon) < THRESH) {
          group.push(j);
          assigned.add(j);
        }
      }
      // 선택된 카메라가 그룹 내에 있으면 그걸 대표로
      const selectedIdx = group.find((idx) => cctvList[idx].id === selectedCctv?.id);
      result.push(cctvList[selectedIdx ?? group[0]]);
    }
    return result;
  }, [cctvList, selectedCctv?.id]);

  // severe 클러스터가 해제될 때 초록색으로 잠깐 표시 후 사라짐
  const [resolvingClusters, setResolvingClusters] = useState([]);
  const prevClustersRef = useRef([]);
  const resolvingTimerRef = useRef(null);
  useEffect(() => {
    const prev = prevClustersRef.current;
    const resolved = prev.filter(
      (p) => p.severity === "severe" && !congestionClusters.find((c) => c.id === p.id)
    );
    prevClustersRef.current = congestionClusters;
    if (resolved.length === 0) return;
    clearTimeout(resolvingTimerRef.current);
    setResolvingClusters(resolved);
    resolvingTimerRef.current = setTimeout(() => setResolvingClusters([]), 1500);
  }, [congestionClusters]);
  useEffect(() => {
    document.fonts.load('bold 12px "Malgun Gothic"').then(() => setFontReady(true));
  }, []);

  useEffect(() => {
    const map = mapRef.current?.getMap?.();
    if (!map) return;
    const apply = () => {
      const textField = lang === "ko" ? ["coalesce", ["get", "name:ko"], ["get", "name"]] : ["coalesce", ["get", "name:en"], ["get", "name"]];
      map.getStyle()?.layers?.forEach((layer) => {
        if (layer.type === "symbol" && layer.layout?.["text-field"] !== undefined) {
          map.setLayoutProperty(layer.id, "text-field", textField);
        }
      });
    };
    if (map.isStyleLoaded()) {
      apply();
    } else {
      map.once("styledata", apply);
    }
  }, [lang, mapMode]);

  const cctvLabel = useCallback((d) => {
    if (lang === "en") return d.name_en || d.name_ko || d.name || String(d.id);
    return d.name_ko || d.name || String(d.id);
  }, [lang]);

  // 긴 이름을 2줄로 분할 (공백 기준 우선, 없으면 중간 분할)
  const wrapLabel = useCallback((text) => {
    if (!text) return "";
    if (text.length <= 10) return text;
    const spaceIdx = text.lastIndexOf(" ", Math.ceil(text.length / 2) + 2);
    const breakAt = spaceIdx > 0 ? spaceIdx : Math.ceil(text.length / 2);
    return text.slice(0, breakAt) + "\n" + text.slice(breakAt).trim();
  }, []);
  const showVehicles = viewState.zoom >= VEHICLE_MIN_ZOOM;

  const sorted = useMemo(
    () => [...vehicles].sort((a, b) => (a.is_speeding ? 1 : 0) - (b.is_speeding ? 1 : 0)),
    [vehicles]
  );

  const cctvHitLayer = useMemo(() => new ScatterplotLayer({
    id:           "cctvs-hit",
    data:         singles,
    getPosition:  (d) => [d.lon, d.lat],
    getRadius:    18,
    getFillColor: [0, 0, 0, 0],
    getLineColor: [0, 0, 0, 0],
    radiusUnits:  "pixels",
    pickable:     !calibrationMode,
    onClick:      ({ object }) => !calibrationMode && object && onCctvClick?.(object),
  }), [singles, calibrationMode, onCctvClick]);

  const cameraIcons = useMemo(() => {
    return mapMode === "light" ? CAMERA_ICONS_LIGHT : CAMERA_ICONS_DARK;
  }, [mapMode]);

  const cctvIconLayer = useMemo(() => new IconLayer({
    id:          "cctv-icons",
    data:        singles,
    getPosition: (d) => [d.lon, d.lat],
    getIcon:     (d) => {
      let iconKey;
      if (selectedCctv?.id === d.id) {  // singles only
        iconKey = "selected";
      } else {
        const bgInfo = d.cam_key ? backgroundStatus[d.cam_key] : null;
        iconKey = bgInfo ? (bgInfo.status || "loading") : "default";
      }
      return { url: cameraIcons[iconKey] ?? cameraIcons.default, width: 36, height: 46, anchorX: 18, anchorY: 45 };
    },
    getSize:        (d) => selectedCctv?.id === d.id ? 52 : 38,
    sizeUnits:      "pixels",
    getPixelOffset: [0, 0],
    pickable:       false,
    updateTriggers: { getIcon: [selectedCctv?.id, backgroundStatus, cameraIcons], getSize: [selectedCctv?.id] },
  }), [singles, selectedCctv?.id, backgroundStatus, cameraIcons]);

  const cctvLabelLayer = useMemo(() => new TextLayer({
    id:               "cctv-labels",
    data:             singles,
    getPosition:      (d) => [d.lon, d.lat],
    getText:          (d) => wrapLabel(cctvLabel(d)),
    getSize:          (d) => selectedCctv?.id === d.id ? 13 : 11,
    getColor:         (d) => selectedCctv?.id === d.id ? [34, 211, 238, 255] : [255, 255, 255, 210],
    getPixelOffset:   (d) => [0, selectedCctv?.id === d.id ? -72 : -58],
    fontFamily:       lang === "ko"
      ? '"Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif'
      : '"Segoe UI", system-ui, sans-serif',
    fontWeight:       700,
    characterSet:     "auto",
    fontSettings:     { sdf: false },
    background:       true,
    getBackgroundColor: [15, 23, 42, 175],
    backgroundPadding:  [4, 2, 4, 2],
    lineHeight:       1.35,
    textAnchor:       "middle",
    alignmentBaseline: "bottom",
    updateTriggers: {
      getText:        [lang],
      getSize:        [selectedCctv?.id],
      getColor:       [selectedCctv?.id],
      getPixelOffset: [selectedCctv?.id],
    },
  }), [singles, selectedCctv?.id, lang, cctvLabel, wrapLabel, fontReady]);

  const fovLayer = useMemo(() => {
    if (!selectedCctv) return null;
    // 폴리곤 heading 우선순위: fovHeadingDeg (노드링크/이름 방위) > selectedCctv.heading > 0
    const heading = fovHeadingDeg ?? selectedCctv.heading ?? 0;
    const originLat = fovSnapLat ?? selectedCctv.lat;
    const originLon = fovSnapLon ?? selectedCctv.lon;
    let ring;
    // Phase 3: 우선순위 — ROI GPS ring > calibGpsRing > corridor > 기본 trapezoid
    if (fovRoiGpsRing && fovRoiGpsRing.length >= 3) {
      // ROI를 GPS로 투영한 실제 detect 범위 (수평선 clamp 적용)
      ring = fovRoiGpsRing.map(([lat, lon]) => [lon, lat]); // deck.gl: [lon, lat]
    } else if (selectedCctv.calibGpsRing) {
      ring = selectedCctv.calibGpsRing;
    } else if (fovRoadPts && fovRoadPts.length >= 2 && fovNearM != null && fovFarM != null && fovRoadWidthM != null) {
      // 도로 중심선을 따라가는 곡선 polygon — 커브 구간에서 실제 도로 형태 반영
      ring = computeRoadCorridorPolygon(fovRoadPts, fovSnapAlongM, heading, fovNearM, fovFarM, fovRoadWidthM / 2);
    } else if (fovNearM != null && fovFarM != null && fovRoadWidthM != null) {
      // H_gps와 동일한 직선 사각형 — 차량 GPS 위치와 일치함
      ring = computeCalibPolygon(originLat, originLon, heading, fovNearM, fovFarM, fovRoadWidthM / 2);
    } else {
      ring = computeFovPolygon(originLat, originLon, heading);
    }
    return new PolygonLayer({
      id:             "cctv-fov",
      data:           [{ ring }],
      getPolygon:     (d) => d.ring,
      getFillColor:   [34, 211, 238, 25],
      getLineColor:   [34, 211, 238, 140],
      lineWidthMinPixels: 1,
      stroked:        true,
      filled:         true,
    });
  }, [selectedCctv, fovNearM, fovFarM, fovRoadWidthM, fovHeadingDeg, fovSnapLat, fovSnapLon, fovRoiGpsRing, fovRoadPts, fovSnapAlongM]);

  // 도로 중심선 — 실제 도로 곡선을 별도 선으로 표시 (FOV polygon과 분리)
  const roadCenterlineLayer = useMemo(() => {
    if (!selectedCctv || !fovRoadPts || fovRoadPts.length < 2) return null;
    const path = fovRoadPts.map(([lat, lon]) => [lon, lat]);
    return new PathLayer({
      id:           "road-centerline",
      data:         [{ path }],
      getPath:      (d) => d.path,
      getColor:     [251, 191, 36, 160],
      getWidth:     2,
      widthUnits:   "meters",
      widthMinPixels: 2,
    });
  }, [selectedCctv, fovRoadPts]);

  const nodeStroked = mapMode !== "dark";
  const nodeOutline = mapMode === "satellite" ? [0, 0, 0, 230] : [80, 80, 80, 180];
  const parkedColor = mapMode === "light" ? [120, 120, 120, 160] : [80, 80, 80, 140];

  const scatterLayer = useMemo(() => new ScatterplotLayer({
    id:           "vehicles",
    data:         sorted,
    getPosition:  (d) => [d.lon, d.lat],
    getRadius:    2,
    getFillColor: (d) => d.is_parked ? parkedColor : getVehicleColor(d.direction, mapMode !== "dark"),
    getLineColor:    nodeOutline,
    lineWidthMinPixels: nodeStroked ? 1.5 : 0,
    stroked:      nodeStroked,
    pickable:     true,
    radiusUnits:  "meters",
    radiusMinPixels: 3,
    updateTriggers: { getFillColor: [sorted, mapMode], getLineColor: mapMode },
  }), [sorted, mapMode, nodeStroked, nodeOutline, parkedColor]);

  const labelColor = mapMode === "light" ? [30, 30, 30, 220] : [255, 255, 255, 200];
  const textLayer = useMemo(() => new TextLayer({
    id:             "vehicle-labels",
    data:           sorted,
    getPosition:    (d) => [d.lon, d.lat],
    getText:        (d) => `#${d.track_id}`,
    getSize:        11,
    getColor:       labelColor,
    getPixelOffset: [0, -14],
    outlineWidth:   mapMode !== "dark" ? 2 : 0,
    outlineColor:   mapMode === "light" ? [255, 255, 255, 200] : [0, 0, 0, 180],
    updateTriggers: { getText: sorted, getColor: mapMode },
  }), [sorted, mapMode, labelColor]);

  const snapNodeLayer = useMemo(() => calibrationMode && snapNodes.length > 0
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
    : null, [calibrationMode, snapNodes]);

  const snapNodeLabelLayer = useMemo(() => calibrationMode && snapNodes.length > 0
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
    : null, [calibrationMode, snapNodes]);

  // 정체 구간 클러스터 오버레이 ([B]) — 차량/카메라보다 아래(배경)에 깔림
  const congestionLayer = useMemo(() => {
    if (!congestionClusters || congestionClusters.length === 0) return null;
    return new PolygonLayer({
      id:           "congestion-clusters",
      data:         congestionClusters,
      getPolygon:   (d) => d.polygon,
      getFillColor: (d) => getSeverityColor(d.severity, 70),
      getLineColor: (d) => getSeverityColor(d.severity, 200),
      lineWidthMinPixels: 2,
      stroked:      true,
      filled:       true,
      pickable:     true,
    });
  }, [congestionClusters]);

  // severe 해제 시 초록색 플래시 레이어 (1.5초 후 사라짐)
  const resolvingLayer = useMemo(() => {
    if (resolvingClusters.length === 0) return null;
    return new PolygonLayer({
      id:                 "congestion-resolving",
      data:               resolvingClusters,
      getPolygon:         (d) => d.polygon,
      getFillColor:       [34, 197, 94, 60],
      getLineColor:       [22, 163, 74, 200],
      lineWidthMinPixels: 2,
      stroked:            true,
      filled:             true,
      pickable:           false,
    });
  }, [resolvingClusters]);

  const layers = useMemo(() => [
    congestionLayer,
    resolvingLayer,
    ...(showVehicles ? extraLayers : []),
    roadCenterlineLayer,
    fovLayer,
    cctvHitLayer,
    cctvIconLayer,
    cctvLabelLayer,
    ...(showVehicles ? [scatterLayer, textLayer] : []),
    snapNodeLayer,
    snapNodeLabelLayer,
  ].filter(Boolean), [
    congestionLayer, resolvingLayer, showVehicles, extraLayers, roadCenterlineLayer, fovLayer,
    cctvHitLayer, cctvIconLayer, cctvLabelLayer,
    scatterLayer, textLayer, snapNodeLayer, snapNodeLabelLayer,
  ]);

  return (
    <DeckGL
      viewState={viewState}
      onViewStateChange={({ viewState: vs }) => onViewStateChange(vs)}
      controller={{ maxZoom: 20 }}
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
            html: `<b>📍 ${d.node_name || d.node_id}</b><br/><span style="color:#9ca3af;font-size:11px">${t("map.nodeSnapHint")}</span>`,
            style: {
              background: "#111827", color: "#fbbf24",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        if (d.severity) {
          const sevColor = { minor: "#fbbf24", medium: "#f97316", severe: "#ef4444" }[d.severity];
          return {
            html: `<b style="color:${sevColor}">⚠ ${t(`congestion.${d.severity}`)}</b><br/>`
              + `<span style="color:#9ca3af;font-size:11px">`
              + `${t("congestion.cameras", { n: d.camera_count })} · ${t("congestion.vehicles", { n: d.total_vehicles })}`
              + `</span>`,
            style: {
              background: "#111827", color: "#f9fafb",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        if (d.id) {
          return {
            html: `<b>📷 ${cctvLabel(d)}</b><br/><span style="color:#9ca3af;font-size:11px">${t("map.clickHint")}</span>`,
            style: {
              background: "#111827", color: "#fbbf24",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        return null;
      }}
    >
      <Map
        ref={mapRef}
        mapStyle={MAP_STYLES[mapMode] ?? MAP_STYLES.dark}
        onLoad={() => {
          const map = mapRef.current?.getMap?.();
          if (!map) return;
          const textField = lang === "ko" ? ["coalesce", ["get", "name:ko"], ["get", "name"]] : ["coalesce", ["get", "name:en"], ["get", "name"]];
          map.getStyle()?.layers?.forEach((layer) => {
            if (layer.type === "symbol" && layer.layout?.["text-field"] !== undefined) {
              map.setLayoutProperty(layer.id, "text-field", textField);
            }
          });
        }}
      />

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
