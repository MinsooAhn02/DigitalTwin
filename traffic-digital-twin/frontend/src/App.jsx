/**
 * App.jsx — 대시보드 메인 레이아웃
 *
 * Streamlit app.py의 기능을 React + Deck.gl로 재구현:
 *   · In(파랑) / Out(빨강) / Unknown(회색) 방향별 색상 (기존 로직 계승)
 *   · 과속 차량 강렬한 빨강 강조
 *   · 궤적(Trail) PathLayer 오버레이
 *   · 실시간 WebSocket 연결 상태 표시
 *
 * ┌─────────────────────────────────────┬────────────┐
 * │                                     │ Counter    │
 * │         MapView (GPU)               │ Speed      │
 * │                                     │ LOS        │
 * │                                     │ Pie Chart  │
 * │                                     │ Alerts     │
 * └─────────────────────────────────────┴────────────┘
 */

import { useReducer, useEffect } from "react";
import { useWebSocket } from "./hooks/useWebSocket";
import MapView from "./components/MapView";
import { updateTrailMap, useTrailLayer } from "./components/TrailLayer";
import SpeedGauge    from "./components/SpeedGauge";
import LOSBadge      from "./components/LOSBadge";
import ClassPieChart from "./components/ClassPieChart";
import CounterPanel  from "./components/CounterPanel";

function trailReducer(state, vehicles) {
  return updateTrailMap(state, vehicles);
}

export default function App() {
  const { frameData, isConnected, error } = useWebSocket();
  const [trailMap, dispatchTrail] = useReducer(trailReducer, new Map());

  const vehicles    = frameData?.vehicles      ?? [];
  const avgSpeed    = frameData?.avg_speed_kph ?? 0;
  const losGrade    = frameData?.los_grade     ?? "A";
  const inCount     = frameData?.in_count      ?? 0;
  const outCount    = frameData?.out_count     ?? 0;
  const vehicleCnt  = frameData?.vehicle_count ?? 0;
  const classCounts = frameData?.class_counts  ?? {};

  // 렌더 중 직접 dispatch 금지 → useEffect로 분리
  useEffect(() => {
    if (frameData) dispatchTrail(vehicles);
  }, [frameData]);

  const trailLayer = useTrailLayer(trailMap, vehicles);

  const speedingVehicles   = vehicles.filter((v) => v.is_speeding);
  const tailgatingVehicles = vehicles.filter((v) => v.is_tailgating);
  const bottlenecks        = vehicles.filter((v) => v.is_bottleneck);

  return (
    <div style={{ display: "flex", height: "100vh", background: "#111827", color: "#f9fafb", fontFamily: "system-ui, sans-serif", overflow: "hidden" }}>

      {/* 왼쪽: 지도 */}
      <div style={{ flex: 1, position: "relative" }}>
        <MapView vehicles={vehicles} extraLayers={[trailLayer]} />

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

        {/* 범례 */}
        <Legend />
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

/* ── 서브 컴포넌트 ──────────────────────────────────────────────────── */

function Card({ children, label }) {
  return (
    <div style={{ background: "#1f2937", borderRadius: 12, padding: 16 }}>
      {label && <p style={{ margin: "0 0 8px", fontSize: 12, color: "#9ca3af" }}>{label}</p>}
      {children}
    </div>
  );
}

function Legend() {
  const items = [
    { color: "#0078ff", label: "진입 (In)" },
    { color: "#ff3232", label: "진출 (Out)" },
    { color: "#ff1e1e", label: "과속", bold: true },
    { color: "#c8c8c8", label: "Unknown" },
  ];
  return (
    <div style={{
      position: "absolute", bottom: 16, left: 16,
      background: "rgba(17,24,39,0.85)", borderRadius: 8,
      padding: "8px 14px", fontSize: 12,
    }}>
      {items.map((it) => (
        <div key={it.label} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
          <span style={{ width: 10, height: 10, borderRadius: "50%", background: it.color, display: "inline-block" }} />
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
