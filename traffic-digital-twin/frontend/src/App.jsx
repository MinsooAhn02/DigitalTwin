import { useReducer, useEffect, useState, useCallback } from "react";
import { FlyToInterpolator } from "deck.gl";
import { useWebSocket } from "./hooks/useWebSocket";
import MapView from "./components/MapView";
import { updateTrailMap, useTrailLayer } from "./components/TrailLayer";
import SpeedGauge    from "./components/SpeedGauge";
import LOSBadge      from "./components/LOSBadge";
import ClassPieChart from "./components/ClassPieChart";
import CounterPanel  from "./components/CounterPanel";

// 실제 replay 데이터가 있는 GPS 중심
const DATA_CENTER = { lat: 37.4626, lon: 127.0386 };
const DATA_RADIUS = 0.05; // 약 5km, 이 범위 밖이면 "데이터 없음" 안내

const INITIAL_VIEW = {
  longitude: DATA_CENTER.lon,
  latitude:  DATA_CENTER.lat,
  zoom:      18,
  pitch:     45,
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
  const { frameData, isConnected, error } = useWebSocket();
  const [trailMap, dispatchTrail]         = useReducer(trailReducer, new Map());
  const [cctvList, setCctvList]           = useState([]);
  const [selectedCctv, setSelectedCctv]   = useState(null);
  const [viewState, setViewState]         = useState(INITIAL_VIEW);
  const [cctvLoading, setCctvLoading]     = useState(false);

  const vehicles    = frameData?.vehicles      ?? [];
  const avgSpeed    = frameData?.avg_speed_kph ?? 0;
  const losGrade    = frameData?.los_grade     ?? "A";
  const inCount     = frameData?.in_count      ?? 0;
  const outCount    = frameData?.out_count     ?? 0;
  const vehicleCnt  = frameData?.vehicle_count ?? 0;
  const classCounts = frameData?.class_counts  ?? {};

  useEffect(() => {
    if (frameData) dispatchTrail(vehicles);
  }, [frameData]);

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

  const trailLayer = useTrailLayer(trailMap, vehicles);

  const handleCctvClick = useCallback((cctv) => {
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
  }, []);

  const flyToData = useCallback(() => {
    setSelectedCctv(null);
    setViewState({
      ...INITIAL_VIEW,
      transitionDuration: 1000,
      transitionInterpolator: new FlyToInterpolator(),
    });
  }, []);

  // 현재 뷰 중심이 데이터 범위 밖인지 확인
  const outOfDataRange =
    Math.abs(viewState.latitude  - DATA_CENTER.lat) > DATA_RADIUS ||
    Math.abs(viewState.longitude - DATA_CENTER.lon) > DATA_RADIUS;

  const speedingVehicles   = vehicles.filter((v) => v.is_speeding);
  const tailgatingVehicles = vehicles.filter((v) => v.is_tailgating);
  const bottlenecks        = vehicles.filter((v) => v.is_bottleneck);

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

        {/* 데이터 범위 밖 안내 */}
        {outOfDataRange && (
          <div style={{
            position: "absolute", top: 12, right: 12,
            background: "rgba(17,24,39,0.90)", padding: "8px 14px",
            borderRadius: 8, fontSize: 12, backdropFilter: "blur(4px)",
            border: "1px solid #374151", maxWidth: 220,
          }}>
            <div style={{ color: "#9ca3af", marginBottom: 6 }}>
              이 위치는 재생 데이터 범위 밖입니다
            </div>
            <button onClick={flyToData} style={{
              background: "#1d4ed8", color: "#fff", border: "none",
              borderRadius: 6, padding: "4px 10px", fontSize: 12,
              cursor: "pointer", width: "100%",
            }}>
              데이터 위치로 이동
            </button>
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
      </div>

      {/* 오른쪽: 사이드바 */}
      <aside style={{
        width: 288, display: "flex", flexDirection: "column", gap: 12,
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
          tailgating={tailgatingVehicles}
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
            display: "inline-block",
            border: it.square ? "1.5px solid #fff" : "none",
          }} />
          <span style={{ color: "#d1d5db", fontWeight: it.bold ? 700 : 400 }}>{it.label}</span>
        </div>
      ))}
    </div>
  );
}

function AlertPanel({ speeding, tailgating, bottlenecks }) {
  const total = speeding.length + tailgating.length + bottlenecks.length;
  if (total === 0) return null;

  return (
    <Card label={`경보 (${total})`}>
      <ul style={{ margin: 0, padding: 0, listStyle: "none", maxHeight: 160, overflowY: "auto" }}>
        {speeding.map((v) => (
          <AlertItem key={`sp-${v.track_id}`} id={v.track_id} cls={v.class_name}
            tag="과속" tagColor="#f87171" extra={`${v.speed_kph?.toFixed(0)} km/h`} />
        ))}
        {tailgating.map((v) => (
          <AlertItem key={`tg-${v.track_id}`} id={v.track_id} cls={v.class_name}
            tag="꼬리물기" tagColor="#fbbf24" extra={`${v.headway_m?.toFixed(1)} m`} />
        ))}
        {bottlenecks.map((v) => (
          <AlertItem key={`bn-${v.track_id}`} id={v.track_id} cls={v.class_name}
            tag="병목" tagColor="#a78bfa" extra={`${v.dwell_frames}f`} />
        ))}
      </ul>
    </Card>
  );
}

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
