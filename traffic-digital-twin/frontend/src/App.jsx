import { useReducer, useEffect, useState, useCallback, useMemo, memo } from "react";
import { FlyToInterpolator } from "deck.gl";
import { useWebSocket } from "./hooks/useWebSocket";
import MapView from "./components/MapView";
import { updateTrailMap, useTrailLayer } from "./components/TrailLayer";
import SpeedGauge    from "./components/SpeedGauge";
import LOSBadge      from "./components/LOSBadge";
import ClassPieChart from "./components/ClassPieChart";
import CounterPanel  from "./components/CounterPanel";
import CctvPlayer    from "./components/CctvPlayer";

const INITIAL_VIEW = {
  longitude: 127.0386,
  latitude:  37.4626,
  zoom:      14,
  pitch:     0,
  bearing:   0,
};

// 현재 뷰포트에서 bbox 계산
function viewBbox(vs) {
  const span = Math.min(360 / Math.pow(2, vs.zoom) * 2, 1.5);
  return {
    minX: (vs.longitude - span).toFixed(4),
    maxX: (vs.longitude + span).toFixed(4),
    minY: (vs.latitude  - span * 0.65).toFixed(4),
    maxY: (vs.latitude  + span * 0.65).toFixed(4),
  };
}

function trailReducer(state, vehicles) {
  return updateTrailMap(state, vehicles);
}

export default function App() {
  const { frameData, isConnected, error, cameraReady } = useWebSocket();
  const [trailMap, dispatchTrail]         = useReducer(trailReducer, new Map());
  const [cctvList, setCctvList]           = useState([]);
  const [selectedCctv, setSelectedCctv]   = useState(null);
  const [viewState, setViewState]         = useState(INITIAL_VIEW);
  const [cctvLoading, setCctvLoading]     = useState(false);
  const [switching, setSwitching]         = useState(false);
  const [guideVisible, setGuideVisible]   = useState(true);
  // 보정 상태: null = 비활성, "awaiting" = 지도 클릭 대기
  const [calMode, setCalMode]             = useState(null);
  const [pendingGps, setPendingGps]       = useState(null);

  // camera_ready 신호 수신 시 switching 상태 해제
  useEffect(() => {
    if (cameraReady > 0) setSwitching(false);
  }, [cameraReady]);

  const activeData  = selectedCctv ? frameData : null;
  const vehicles    = activeData?.vehicles      ?? [];
  const avgSpeed    = activeData?.avg_speed_kph ?? 0;
  const losGrade    = activeData?.los_grade     ?? "A";
  const inCount     = activeData?.in_count      ?? 0;
  const outCount    = activeData?.out_count     ?? 0;
  const vehicleCnt  = activeData?.vehicle_count ?? 0;
  const classCounts = activeData?.class_counts ?? {};

  useEffect(() => {
    if (activeData) dispatchTrail(vehicles);
  }, [activeData]);

  const fetchCctvs = useCallback((vs) => {
    const { minX, maxX, minY, maxY } = viewBbox(vs ?? viewState);
    setCctvLoading(true);
    fetch(`http://localhost:8000/cctvs?minX=${minX}&maxX=${maxX}&minY=${minY}&maxY=${maxY}`)
      .then((r) => r.json())
      .then(setCctvList)
      .catch(() => {})
      .finally(() => setCctvLoading(false));
  }, [viewState]);

  // 초기 로드
  useEffect(() => { fetchCctvs(INITIAL_VIEW); }, []);

  // CCTV 로드 후 5초 뒤 안내 팝업 자동 소멸
  useEffect(() => {
    if (cctvList.length === 0) return;
    const t = setTimeout(() => setGuideVisible(false), 5000);
    return () => clearTimeout(t);
  }, [cctvList.length]);

  const trailLayer = useTrailLayer(trailMap, vehicles);

  // 보정 모드: 지도 클릭으로 GPS 좌표 수집
  const handleMapClick = useCallback((info) => {
    if (calMode !== "awaiting" || !info.coordinate) return;
    const [lon, lat] = info.coordinate;
    setPendingGps({ lat, lon });
    setCalMode(null);
  }, [calMode]);

  const handleCctvClick = useCallback((cctv) => {
    if (calMode === "awaiting") return; // 보정 중 카메라 전환 방지
    setSelectedCctv(cctv);
    setViewState({
      longitude: cctv.lon,
      latitude:  cctv.lat,
      zoom: 18,
      pitch: 45,
      bearing: 0,
      transitionDuration: 1200,
      transitionInterpolator: new FlyToInterpolator(),
    });
    // 라이브 카메라 전환 — switching 해제는 WS camera_ready 신호로 처리
    if (cctv.cctvurl) {
      setSwitching(true);
      fetch("http://localhost:8000/switch-camera", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ cctvurl: cctv.cctvurl, lat: cctv.lat, lon: cctv.lon, name: cctv.name ?? "" }),
      }).catch(() => setSwitching(false));
    }
  }, [calMode]);

  // CCTV 로드 후 5초 안에 선택 안 했을 때만 안내 표시
  const noCameraSelected = guideVisible && cctvList.length > 0 && !selectedCctv;

  const speedingVehicles = useMemo(() => vehicles.filter((v) => v.is_speeding), [vehicles]);
  const bottlenecks      = useMemo(() => vehicles.filter((v) => v.is_bottleneck), [vehicles]);

  return (
    <div style={{ display: "flex", height: "100vh", background: "#111827", color: "#f9fafb", fontFamily: "system-ui, sans-serif", overflow: "hidden" }}>

      {/* 왼쪽: 지도 */}
      <div style={{ flex: 1, position: "relative" }}>
        <MapView
          vehicles={vehicles}
          extraLayers={[trailLayer]}
          cctvList={cctvList}
          selectedCctv={selectedCctv}
          viewState={viewState}
          onViewStateChange={setViewState}
          onCctvClick={handleCctvClick}
          calibrationMode={calMode === "awaiting"}
          onMapClick={handleMapClick}
        />

        {/* 연결 상태 칩 */}
        <div style={{
          position: "absolute", top: 12, left: 12,
          display: "flex", alignItems: "center", gap: 8,
          background: "rgba(17,24,39,0.85)", padding: "6px 14px",
          borderRadius: 999, fontSize: 13, backdropFilter: "blur(4px)",
        }}>
          <span style={{
            width: 8, height: 8, borderRadius: "50%",
            background: isConnected ? "#34d399" : "#f87171",
            display: "inline-block",
          }} />
          {isConnected ? "실시간 연결 중" : (error ?? "재연결 중…")}
        </div>

        {/* 선택된 CCTV 표시 */}
        {selectedCctv && (
          <div style={{
            position: "absolute", top: 12, left: "50%",
            transform: "translateX(-50%)",
            display: "flex", alignItems: "center", gap: 6,
            background: "rgba(17,24,39,0.90)", padding: "6px 16px",
            borderRadius: 999, fontSize: 13, backdropFilter: "blur(4px)",
            border: "1px solid #fbbf24", whiteSpace: "nowrap",
          }}>
            <span style={{ color: "#fbbf24", fontSize: 15 }}>📷</span>
            <span style={{ color: "#fde68a" }}>{selectedCctv.name || selectedCctv.id}</span>
            <button
              onClick={() => setSelectedCctv(null)}
              style={{ background: "none", border: "none", color: "#6b7280", cursor: "pointer", padding: 0, marginLeft: 4, fontSize: 14 }}
            >✕</button>
          </div>
        )}

        {/* 카메라 미선택 안내 */}
        {noCameraSelected && (
          <div style={{
            position: "absolute", top: "50%", left: "50%",
            transform: "translate(-50%, -50%)",
            background: "rgba(17,24,39,0.92)", padding: "18px 28px",
            borderRadius: 12, fontSize: 14, backdropFilter: "blur(6px)",
            border: "1px solid #374151", textAlign: "center", pointerEvents: "none",
          }}>
            <div style={{ fontSize: 28, marginBottom: 8 }}>📷</div>
            <div style={{ color: "#f9fafb", fontWeight: 600, marginBottom: 4 }}>지도에서 CCTV를 클릭하세요</div>
            <div style={{ color: "#9ca3af", fontSize: 12 }}>클릭하면 해당 카메라의 실시간 차량 탐지가 시작됩니다</div>
          </div>
        )}

        {/* 카메라 전환 중 */}
        {switching && (
          <div style={{
            position: "absolute", top: 52, left: "50%",
            transform: "translateX(-50%)",
            background: "rgba(17,24,39,0.90)", padding: "6px 16px",
            borderRadius: 999, fontSize: 12, backdropFilter: "blur(4px)",
            border: "1px solid #374151", whiteSpace: "nowrap", color: "#a5b4fc",
          }}>
            ⏳ 스트림 연결 중…
          </div>
        )}

        {/* CCTV 새로고침 버튼 */}
        <div style={{
          position: "absolute", bottom: 56, right: 16,
          display: "flex", flexDirection: "column", gap: 6,
        }}>
          <button
            onClick={() => fetchCctvs()}
            disabled={cctvLoading}
            title="현재 화면 기준 CCTV 새로고침"
            style={{
              background: cctvLoading ? "#374151" : "rgba(17,24,39,0.90)",
              color: cctvLoading ? "#6b7280" : "#fbbf24",
              border: "1px solid #374151",
              borderRadius: 8, padding: "7px 12px",
              fontSize: 12, cursor: cctvLoading ? "default" : "pointer",
              backdropFilter: "blur(4px)", whiteSpace: "nowrap",
            }}
          >
            {cctvLoading ? "로딩 중…" : "📷 CCTV 새로고침"}
          </button>
          <div style={{ fontSize: 10, color: "#6b7280", textAlign: "center" }}>
            {cctvList.length}개 카메라
          </div>
        </div>

        {/* zoom out 시 안내 */}
        {viewState.zoom < 15 && (
          <div style={{
            position: "absolute", bottom: 16, right: 16,
            background: "rgba(17,24,39,0.85)", padding: "6px 14px",
            borderRadius: 8, fontSize: 12, color: "#9ca3af",
            backdropFilter: "blur(4px)",
          }}>
            zoom 15 이상으로 확대하면 차량이 표시됩니다
          </div>
        )}

        {/* 범례 */}
        <Legend cctvCount={cctvList.length} />

        {/* 보정 모드 안내 배너 */}
        {calMode === "awaiting" && (
          <div style={{
            position: "absolute", top: 52, left: "50%",
            transform: "translateX(-50%)",
            background: "rgba(30,58,138,0.95)", padding: "8px 20px",
            borderRadius: 999, fontSize: 13, backdropFilter: "blur(4px)",
            border: "1px solid #3b82f6", whiteSpace: "nowrap", color: "#bfdbfe", zIndex: 30,
          }}>
            🗺 지도에서 동일 지점을 클릭하세요
            <button
              onClick={() => { setCalMode(null); setPendingGps(null); }}
              style={{ background: "none", border: "none", color: "#94a3b8", cursor: "pointer", marginLeft: 10, fontSize: 14 }}
            >✕</button>
          </div>
        )}

        {/* CCTV 실시간 영상 — 지도 좌하단 플로팅 패널 */}
        <CctvPlayer
          cctv={selectedCctv}
          onClose={() => { setSelectedCctv(null); setCalMode(null); setPendingGps(null); }}
          pendingGps={pendingGps}
          onNeedGps={() => { setPendingGps(null); setCalMode("awaiting"); }}
          onCancelGps={() => { setCalMode(null); setPendingGps(null); }}
        />
      </div>

      {/* 오른쪽: 사이드바 */}
      <aside style={{
        width: 360, display: "flex", flexDirection: "column", gap: 12,
        padding: 16, background: "#111827",
        borderLeft: "1px solid #1f2937", overflowY: "auto",
      }}>
        <h1 style={{ margin: 0, fontSize: 16, fontWeight: 700, letterSpacing: "-0.02em" }}>
          🛣️ 교통 디지털 트윈
        </h1>

        <CounterPanel inCount={inCount} outCount={outCount} vehicleCount={vehicleCnt} />

        <Card>
          <div style={{ display: "flex", justifyContent: "space-around", alignItems: "center" }}>
            <SpeedGauge avgSpeed={avgSpeed} />
            <LOSBadge grade={losGrade} />
          </div>
        </Card>

        <Card label="차종 분포">
          <ClassPieChart classCounts={classCounts} />
        </Card>

        <AlertPanel
          speeding={speedingVehicles}
          bottlenecks={bottlenecks}
        />
      </aside>
    </div>
  );
}

function Card({ children, label }) {
  return (
    <div style={{ background: "#1f2937", borderRadius: 12, padding: 16 }}>
      {label && <p style={{ margin: "0 0 8px", fontSize: 12, color: "#9ca3af" }}>{label}</p>}
      {children}
    </div>
  );
}

function Legend({ cctvCount }) {
  const items = [
    { color: "#0078ff", label: "진입 (In)" },
    { color: "#ff3232", label: "진출 (Out)" },
    { color: "#ff1e1e", label: "과속", bold: true },
    { color: "#c8c8c8", label: "Unknown" },
    { color: "#fbbf24", label: `CCTV (${cctvCount})`, square: true },
    { color: "#22d3ee", label: "시야 범위 (선택 시)", square: true, opacity: 0.4 },
  ];
  return (
    <div style={{
      position: "absolute", bottom: 16, left: 16,
      background: "rgba(17,24,39,0.85)", borderRadius: 8,
      padding: "8px 14px", fontSize: 12,
    }}>
      {items.map((it) => (
        <div key={it.label} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
          <span style={{
            width: 10, height: 10,
            borderRadius: it.square ? 2 : "50%",
            background: it.color,
            opacity: it.opacity ?? 1,
            display: "inline-block",
            border: it.square ? "1.5px solid #fff" : "none",
          }} />
          <span style={{ color: "#d1d5db", fontWeight: it.bold ? 700 : 400 }}>{it.label}</span>
        </div>
      ))}
    </div>
  );
}

const AlertPanel = memo(function AlertPanel({ speeding, bottlenecks }) {
  const total = speeding.length + bottlenecks.length;
  if (total === 0) return null;

  return (
    <Card label={`경보 (${total})`}>
      <ul style={{ margin: 0, padding: 0, listStyle: "none", maxHeight: 160, overflowY: "auto", contain: "layout" }}>
        {speeding.map((v) => (
          <AlertItem key={`sp-${v.track_id}`} id={v.track_id} cls={v.class_name}
            tag="과속" tagColor="#f87171" extra={`${v.speed_kph?.toFixed(0)} km/h`} />
        ))}
        {bottlenecks.map((v) => (
          <AlertItem key={`bn-${v.track_id}`} id={v.track_id} cls={v.class_name}
            tag="병목" tagColor="#a78bfa" extra={`${v.dwell_frames}f`} />
        ))}
      </ul>
    </Card>
  );
}, (prev, next) => {
  // track_id 집합이 동일하고 값도 같으면 리렌더 스킵
  if (prev.speeding.length !== next.speeding.length) return false;
  if (prev.bottlenecks.length !== next.bottlenecks.length) return false;
  const prevIds = new Set(prev.speeding.map((v) => v.track_id));
  if (!next.speeding.every((v) => prevIds.has(v.track_id))) return false;
  const prevBnIds = new Set(prev.bottlenecks.map((v) => v.track_id));
  if (!next.bottlenecks.every((v) => prevBnIds.has(v.track_id))) return false;
  return true;
});

function AlertItem({ id, cls, tag, tagColor, extra }) {
  return (
    <li style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12, padding: "3px 0", borderBottom: "1px solid #374151" }}>
      <span style={{ color: "#d1d5db" }}>#{id} {cls}</span>
      <span>
        <span style={{ color: tagColor, fontWeight: 600 }}>{tag}</span>
        <span style={{ color: "#6b7280", marginLeft: 6 }}>{extra}</span>
      </span>
    </li>
  );
}
