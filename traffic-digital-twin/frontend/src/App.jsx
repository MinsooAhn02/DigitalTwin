import { useReducer, useEffect, useState, useCallback, useMemo, useRef } from "react";
import { FlyToInterpolator } from "deck.gl";
import { useWebSocket } from "./hooks/useWebSocket";
import MapView from "./components/MapView";
import { updateTrailMap, useTrailLayer } from "./components/TrailLayer";
import ClassBarChart  from "./components/ClassBarChart";
import VehicleTable   from "./components/VehicleTable";
import CounterPanel  from "./components/CounterPanel";
import CctvPlayer    from "./components/CctvPlayer";
import { useLang }   from "./i18n/index.jsx";

const INITIAL_VIEW = {
  longitude: 127.0386,
  latitude:  37.4626,
  zoom:      12,
  pitch:     0,
  bearing:   0,
};

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
  const { frameData, isConnected, error, cameraReady, cameraReadyInfo, autoCalibInfo } = useWebSocket();
  const { t, lang, setLang } = useLang();
  const [trailMap, dispatchTrail]         = useReducer(trailReducer, new Map());
  const [cctvList, setCctvList]           = useState([]);
  const [selectedCctv, setSelectedCctv]   = useState(null);
  const [viewState, setViewState]         = useState(INITIAL_VIEW);
  const [cctvLoading, setCctvLoading]     = useState(false);
  const [switching, setSwitching]         = useState(false);
  const [guideVisible, setGuideVisible]   = useState(true);
  const [calMode, setCalMode]             = useState(null);
  const [isCalibrated, setIsCalibrated]   = useState(false);
  const [pendingGps, setPendingGps]       = useState(null);
  const [snapNodes, setSnapNodes]         = useState([]);
  const [calibTabActive, setCalibTabActive] = useState(false);
  const [mapMode, setMapMode]             = useState("dark");
  const switchDebounceRef                 = useRef(null);
  const switchTimeoutRef                  = useRef(null);

  useEffect(() => {
    if (cameraReady > 0) {
      setSwitching(false);
      if (switchTimeoutRef.current) clearTimeout(switchTimeoutRef.current);
    }
  }, [cameraReady]);

  // 카메라 선택 시 주변 노드링크 노드 fetch (캘리브레이션 GPS 스냅용)
  useEffect(() => {
    if (!selectedCctv?.lat || !selectedCctv?.lon) { setSnapNodes([]); return; }
    fetch(`http://localhost:8000/nodelink/nodes?lat=${selectedCctv.lat}&lon=${selectedCctv.lon}&radius_km=0.3`)
      .then((r) => r.json())
      .then(({ nodes }) => setSnapNodes(nodes ?? []))
      .catch(() => setSnapNodes([]));
  }, [selectedCctv?.lat, selectedCctv?.lon]);

  // calibTabActive 켜질 때 zoom > 16이면 위성사진 없음 → 16으로 축소 + pitch 0
  useEffect(() => {
    if (!calibTabActive) return;
    setViewState((prev) => {
      if (prev.zoom <= 16) return prev;
      return {
        ...prev,
        zoom: 16,
        pitch: 0,
        transitionDuration: 800,
        transitionInterpolator: new FlyToInterpolator(),
      };
    });
  }, [calibTabActive]);

  useEffect(() => {
    if (!cameraReadyInfo) return;
    setIsCalibrated(cameraReadyInfo.calibrated ?? false);
    const bearing = cameraReadyInfo.name_bearing ?? cameraReadyInfo.road_bearing ?? null;
    if (bearing != null) {
      setSelectedCctv((prev) => prev ? { ...prev, heading: bearing } : prev);
    }
  }, [cameraReadyInfo]);

  useEffect(() => {
    if (!cameraReadyInfo?.camera_key || !selectedCctv) return;
    fetch(`http://localhost:8000/calibration/${cameraReadyInfo.camera_key}`)
      .then((r) => r.json())
      .then(({ calibration }) => {
        if (!calibration?.gps_pts) return;
        const ring = [
          ...calibration.gps_pts.map(([lat, lon]) => [lon, lat]),
          [calibration.gps_pts[0][1], calibration.gps_pts[0][0]],
        ];
        setSelectedCctv((prev) => prev ? { ...prev, calibGpsRing: ring } : prev);
      })
      .catch(() => {});
  }, [cameraReadyInfo]);

  const activeData   = selectedCctv ? frameData : null;
  const vehicles     = activeData?.vehicles       ?? [];
  const inCount      = activeData?.in_count       ?? 0;
  const outCount     = activeData?.out_count      ?? 0;
  const vehicleCnt   = activeData?.vehicle_count  ?? 0;
  const classCounts  = activeData?.class_counts   ?? {};
  const avgSpeed     = activeData?.avg_speed_kph  ?? 0;
  const ourAvgKph    = activeData?.our_avg_kph    ?? 0;   // 10분 rolling avg (ITS 비교용)
  const itsSpeed     = activeData?.its_speed_kph  ?? null;
  const speedErrPct  = activeData?.speed_error_pct ?? null;
  const speedScale     = activeData?.speed_scale           ?? 1.0;
  const scaleConverged = activeData?.speed_scale_converged ?? false;
  const roadName     = cameraReadyInfo?.road_name  ?? null;
  const roadLanes    = cameraReadyInfo?.road_lanes  ?? null;
  const roadMaxSpd   = cameraReadyInfo?.road_max_spd ?? null;

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

  useEffect(() => { fetchCctvs(INITIAL_VIEW); }, []);

  useEffect(() => {
    if (cctvList.length === 0) return;
    const timer = setTimeout(() => setGuideVisible(false), 5000);
    return () => clearTimeout(timer);
  }, [cctvList.length]);

  const trailLayer = useTrailLayer(trailMap, vehicles);

  const handleMapClick = useCallback((info) => {
    if (calMode !== "awaiting") return;
    // 스냅 노드 클릭 시 DB의 정확한 GPS 사용
    if (info.object?.node_id) {
      setPendingGps({ lat: info.object.lat, lon: info.object.lon });
      setCalMode(null);
      return;
    }
    if (!info.coordinate) return;
    const [lon, lat] = info.coordinate;
    setPendingGps({ lat, lon });
    setCalMode(null);
  }, [calMode]);

  const handleCctvClick = useCallback((cctv) => {
    if (calMode === "awaiting") return;
    setSelectedCctv(cctv);
    setViewState({
      longitude: cctv.lon,
      latitude:  cctv.lat,
      zoom: 18, pitch: 45, bearing: 0,
      transitionDuration: 1200,
      transitionInterpolator: new FlyToInterpolator(),
    });
    if (!cctv.cctvurl) return;
    if (switchDebounceRef.current) clearTimeout(switchDebounceRef.current);
    if (switchTimeoutRef.current) clearTimeout(switchTimeoutRef.current);
    switchDebounceRef.current = setTimeout(() => {
      setSwitching(true);
      switchTimeoutRef.current = setTimeout(() => setSwitching(false), 10000);
      fetch("http://localhost:8000/switch-camera", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ cctvurl: cctv.cctvurl, lat: cctv.lat, lon: cctv.lon, name: cctv.name ?? "" }),
      }).catch(() => setSwitching(false));
    }, 300);
  }, [calMode]);

  const handleCalibSaved = useCallback((heading, calibGpsRing) => {
    if (!selectedCctv) return;
    const updated = { ...selectedCctv, heading, ...(calibGpsRing ? { calibGpsRing } : {}) };
    setSelectedCctv(updated);
    setCctvList((prev) => prev.map((c) => c.id === updated.id ? updated : c));
  }, [selectedCctv]);

  const noCameraSelected = guideVisible && cctvList.length > 0 && !selectedCctv;
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
          snapNodes={calMode === "awaiting" ? snapNodes : []}
          onMapClick={handleMapClick}
          mapMode={calibTabActive && mapMode !== "satellite" ? "satellite" : mapMode}
          onMapModeChange={setMapMode}
          fovNearM={autoCalibInfo?.near_m ?? null}
          fovFarM={autoCalibInfo?.far_m ?? null}
          fovRoadWidthM={autoCalibInfo?.road_width_m ?? null}
          fovHeadingDeg={
            cameraReadyInfo?.name_bearing
            ?? cameraReadyInfo?.road_bearing
            ?? autoCalibInfo?.heading
            ?? null
          }
        />

        {/* 연결 상태 칩 */}
        <div style={{
          position: "absolute", top: 12, left: 12,
          display: "flex", alignItems: "center", gap: 8,
          background: "rgba(17,24,39,0.85)", padding: "6px 14px",
          borderRadius: 999, fontSize: 13, backdropFilter: "blur(4px)",
        }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: isConnected ? "#34d399" : "#f87171", display: "inline-block" }} />
          {isConnected ? t("app.connected") : (error ?? t("app.reconnecting"))}
        </div>

        {/* 선택된 CCTV 표시 */}
        {selectedCctv && (
          <div style={{
            position: "absolute", top: 12, left: "50%", transform: "translateX(-50%)",
            display: "flex", alignItems: "center", gap: 6,
            background: "rgba(17,24,39,0.90)", padding: "6px 16px",
            borderRadius: 999, fontSize: 13, backdropFilter: "blur(4px)",
            border: "1px solid #fbbf24", whiteSpace: "nowrap",
          }}>
            <span style={{ color: "#fbbf24", fontSize: 15 }}>📷</span>
            <span style={{ color: "#fde68a" }}>{selectedCctv.name || selectedCctv.id}</span>
            <button onClick={() => setSelectedCctv(null)} style={{ background: "none", border: "none", color: "#6b7280", cursor: "pointer", padding: 0, marginLeft: 4, fontSize: 14 }}>✕</button>
          </div>
        )}

        {/* 카메라 미선택 안내 */}
        {noCameraSelected && (
          <div style={{
            position: "absolute", top: "50%", left: "50%", transform: "translate(-50%, -50%)",
            background: "rgba(17,24,39,0.92)", padding: "18px 28px",
            borderRadius: 12, fontSize: 14, backdropFilter: "blur(6px)",
            border: "1px solid #374151", textAlign: "center", pointerEvents: "none",
          }}>
            <div style={{ fontSize: 28, marginBottom: 8 }}>📷</div>
            <div style={{ color: "#f9fafb", fontWeight: 600, marginBottom: 4 }}>{t("app.clickCctv")}</div>
            <div style={{ color: "#9ca3af", fontSize: 12 }}>{t("app.clickCctvSub")}</div>
          </div>
        )}

        {/* 카메라 전환 중 */}
        {switching && (
          <div style={{
            position: "absolute", top: 52, left: "50%", transform: "translateX(-50%)",
            background: "rgba(17,24,39,0.90)", padding: "6px 16px",
            borderRadius: 999, fontSize: 12, backdropFilter: "blur(4px)",
            border: "1px solid #374151", whiteSpace: "nowrap", color: "#a5b4fc",
          }}>
            {t("app.switching")}
          </div>
        )}

        {/* CCTV 검색 */}
        <CctvSearch cctvList={cctvList} onSelect={handleCctvClick} viewState={viewState} />

        {/* CCTV 새로고침 버튼 */}
        <div style={{ position: "absolute", bottom: 56, right: 16, display: "flex", flexDirection: "column", gap: 6 }}>
          <button
            onClick={() => fetchCctvs()}
            disabled={cctvLoading}
            title={t("app.refreshCctv")}
            style={{
              background: cctvLoading ? "#374151" : "rgba(17,24,39,0.90)",
              color: cctvLoading ? "#6b7280" : "#fbbf24",
              border: "1px solid #374151", borderRadius: 8, padding: "7px 12px",
              fontSize: 12, cursor: cctvLoading ? "default" : "pointer",
              backdropFilter: "blur(4px)", whiteSpace: "nowrap",
            }}
          >
            {cctvLoading ? t("app.loading") : t("app.refreshCctv")}
          </button>
          <div style={{ fontSize: 10, color: "#6b7280", textAlign: "center" }}>
            {t("app.nCameras", { n: cctvList.length })}
          </div>
        </div>

        {/* zoom out 안내 */}
        {viewState.zoom < 15 && (
          <div style={{
            position: "absolute", bottom: 16, right: 16,
            background: "rgba(17,24,39,0.85)", padding: "6px 14px",
            borderRadius: 8, fontSize: 12, color: "#9ca3af", backdropFilter: "blur(4px)",
          }}>
            {t("app.zoomHint")}
          </div>
        )}

        <Legend t={t} cctvCount={cctvList.length} />

        {/* 보정 모드 안내 배너 */}
        {calMode === "awaiting" && (
          <div style={{
            position: "absolute", top: 52, left: "50%", transform: "translateX(-50%)",
            background: "rgba(30,58,138,0.95)", padding: "8px 20px",
            borderRadius: 999, fontSize: 13, backdropFilter: "blur(4px)",
            border: "1px solid #3b82f6", whiteSpace: "nowrap", color: "#bfdbfe", zIndex: 30,
          }}>
            {t("app.mapClickBanner")}
            <button
              onClick={() => { setCalMode(null); setPendingGps(null); }}
              style={{ background: "none", border: "none", color: "#94a3b8", cursor: "pointer", marginLeft: 10, fontSize: 14 }}
            >✕</button>
          </div>
        )}

        <CctvPlayer
          cctv={selectedCctv}
          onClose={() => { setSelectedCctv(null); setCalMode(null); setPendingGps(null); }}
          pendingGps={pendingGps}
          onNeedGps={() => { setPendingGps(null); setCalMode("awaiting"); }}
          onCancelGps={() => { setCalMode(null); setPendingGps(null); }}
          onCalibSaved={handleCalibSaved}
          onCalibTabChange={setCalibTabActive}
        />
      </div>

      {/* 오른쪽: 사이드바 */}
      <aside style={{
        width: 360, display: "flex", flexDirection: "column", gap: 12,
        padding: 16, background: "#111827",
        borderLeft: "1px solid #1f2937", overflowY: "auto",
      }}>
        {/* 헤더: 타이틀 + 언어 토글 */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <h1 style={{ margin: 0, fontSize: 16, fontWeight: 700, letterSpacing: "-0.02em" }}>
            {t("app.title")}
          </h1>
          <button
            onClick={() => setLang(lang === "en" ? "ko" : "en")}
            title={lang === "en" ? "한국어로 전환" : "Switch to English"}
            style={{
              background: "rgba(30,41,59,0.9)", border: "1px solid #334155",
              borderRadius: 6, padding: "3px 10px", cursor: "pointer",
              fontSize: 12, fontWeight: 700,
              color: lang === "en" ? "#38bdf8" : "#fbbf24",
            }}
          >
            {lang === "en" ? "KO" : "EN"}
          </button>
        </div>

        <CounterPanel inCount={inCount} outCount={outCount} vehicleCount={vehicleCnt} />

        {roadName && (
          <Card label="도로 정보">
            <div style={{ fontSize: 12, lineHeight: 1.6 }}>
              <div style={{ fontWeight: 600, color: "#e2e8f0", marginBottom: 4 }}>{roadName}</div>
              <div style={{ display: "flex", gap: 16, color: "#94a3b8" }}>
                {roadLanes != null && roadLanes > 0 && (
                  <span>차로 <b style={{ color: "#e2e8f0" }}>{roadLanes}</b>개</span>
                )}
                {roadMaxSpd != null && roadMaxSpd > 0 && (
                  <span>제한속도 <b style={{ color: "#fbbf24" }}>{roadMaxSpd}</b> km/h</span>
                )}
              </div>
            </div>
          </Card>
        )}

        {autoCalibInfo?.cam_h_m != null && (
          <Card label="자동 캘리브레이션 추정값">
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 12px", fontSize: 12, color: "#94a3b8" }}>
              <span>카메라 높이</span>
              <b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.cam_h_m} m</b>
              <span>도로 폭</span>
              <b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.road_width_m} m</b>
              <span>근거리</span>
              <b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.near_m} m</b>
              <span>원거리</span>
              <b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.far_m} m</b>
              <span>카메라 틸트</span>
              <b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.pitch_deg}°</b>
            </div>
          </Card>
        )}

        {itsSpeed !== null && (
          <Card label="ITS 구간속도 비교">
            <div style={{ display: "flex", justifyContent: "space-around", alignItems: "center", fontSize: 12 }}>
              <div style={{ textAlign: "center" }}>
                <div style={{ color: "#64748b", fontSize: 10, marginBottom: 2 }}>측정 평균 (10분)</div>
                <div style={{ color: "#94a3b8", fontWeight: 700, fontSize: 18 }}>
                  {ourAvgKph > 0 ? ourAvgKph.toFixed(1) : "—"}
                </div>
                {ourAvgKph > 0 && <div style={{ color: "#4b5563", fontSize: 10 }}>km/h</div>}
              </div>
              <div style={{ color: "#374151", fontSize: 18 }}>vs</div>
              <div style={{ textAlign: "center" }}>
                <div style={{ color: "#64748b", fontSize: 10, marginBottom: 2 }}>ITS 구간속도</div>
                <div style={{ color: "#94a3b8", fontWeight: 700, fontSize: 18 }}>{itsSpeed.toFixed(1)}</div>
                <div style={{ color: "#4b5563", fontSize: 10 }}>km/h</div>
              </div>
              {speedErrPct !== null && ourAvgKph > 0 && (
                <div style={{ textAlign: "center" }}>
                  <div style={{ color: "#64748b", fontSize: 10, marginBottom: 2 }}>오차</div>
                  <div style={{
                    fontWeight: 700, fontSize: 16,
                    color: Math.abs(speedErrPct) < 10 ? "#34d399" : Math.abs(speedErrPct) < 20 ? "#fbbf24" : "#f87171",
                  }}>
                    {speedErrPct > 0 ? "+" : ""}{speedErrPct.toFixed(1)}%
                  </div>
                </div>
              )}
            </div>
            <div style={{ marginTop: 6, textAlign: "center", fontSize: 10, color: "#4b5563" }}>
              보정 계수&nbsp;
              <span style={{
                color: scaleConverged ? "#34d399" : Math.abs(speedScale - 1) < 0.05 ? "#94a3b8" : "#fbbf24",
                fontWeight: 700,
              }}>
                ×{speedScale.toFixed(3)}
              </span>
              &nbsp;
              {scaleConverged
                ? <span style={{ color: "#34d399" }}>✓ 수렴 (저장됨)</span>
                : <span style={{ color: "#6b7280" }}>학습 중…</span>
              }
            </div>
          </Card>
        )}

        <Card label={t("app.classDist")}>
          <ClassBarChart classCounts={classCounts} />
        </Card>

        <Card label={t("app.vehicleList")}>
          <VehicleTable vehicles={vehicles} calibrated={isCalibrated} />
        </Card>
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

function CctvSearch({ cctvList, onSelect, viewState }) {
  const [query,   setQuery]   = useState("");
  const [open,    setOpen]    = useState(false);
  const [sortBy,  setSortBy]  = useState("name"); // "name" | "dist"
  const ref = useRef(null);

  const distKm = (c) => {
    if (!viewState || c.lat == null || c.lon == null) return Infinity;
    const dlat = (c.lat - viewState.latitude) * 110.574;
    const dlon = (c.lon - viewState.longitude) * 111.320 * Math.cos((viewState.latitude * Math.PI) / 180);
    return Math.hypot(dlat, dlon);
  };

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = q
      ? cctvList.filter((c) => (c.name || c.id).toLowerCase().includes(q))
      : [...cctvList];
    if (sortBy === "dist") return filtered.sort((a, b) => distKm(a) - distKm(b));
    return filtered.sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id, "ko"));
  }, [query, cctvList, sortBy, viewState]);

  useEffect(() => {
    const close = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const handleSelect = (c) => {
    onSelect(c);
    setQuery(c.name || c.id);
    setOpen(false);
  };

  const btnStyle = (active) => ({
    padding: "3px 8px", fontSize: 11, border: "none", borderRadius: 4, cursor: "pointer",
    background: active ? "#3b82f6" : "#1f2937",
    color: active ? "#fff" : "#9ca3af",
  });

  return (
    <div ref={ref} style={{ position: "absolute", top: 12, right: 60, zIndex: 20, width: 260 }}>
      <div style={{ position: "relative" }}>
        <input
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }}
          placeholder="CCTV 검색…"
          style={{
            width: "100%", boxSizing: "border-box",
            background: "rgba(17,24,39,0.90)", border: "1px solid #374151",
            borderRadius: 8, padding: "7px 34px 7px 12px",
            fontSize: 13, color: "#f9fafb", backdropFilter: "blur(4px)", outline: "none",
          }}
        />
        <span style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", color: "#6b7280", pointerEvents: "none" }}>🔍</span>
      </div>

      {open && results.length > 0 && (
        <div style={{
          position: "absolute", top: "calc(100% + 4px)", left: 0, right: 0,
          background: "rgba(17,24,39,0.97)", border: "1px solid #374151",
          borderRadius: 8, overflow: "hidden", backdropFilter: "blur(8px)",
          boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
        }}>
          <div style={{ display: "flex", gap: 4, padding: "6px 10px", borderBottom: "1px solid #1f2937", alignItems: "center" }}>
            <span style={{ fontSize: 10, color: "#6b7280", marginRight: 4 }}>정렬</span>
            <button style={btnStyle(sortBy === "name")} onClick={() => setSortBy("name")}>이름순</button>
            <button style={btnStyle(sortBy === "dist")} onClick={() => setSortBy("dist")}>거리순</button>
            <span style={{ marginLeft: "auto", fontSize: 10, color: "#4b5563" }}>{results.length}개</span>
          </div>
          <div style={{ maxHeight: 340, overflowY: "auto" }}>
            {results.map((c) => {
              const km = distKm(c);
              const distLabel = km < Infinity ? (km < 1 ? `${(km * 1000).toFixed(0)}m` : `${km.toFixed(1)}km`) : null;
              return (
                <button
                  key={c.id}
                  onClick={() => handleSelect(c)}
                  style={{
                    display: "flex", justifyContent: "space-between", alignItems: "center",
                    width: "100%", textAlign: "left",
                    padding: "9px 14px", fontSize: 12, background: "none",
                    border: "none", borderBottom: "1px solid #1f2937",
                    color: "#f9fafb", cursor: "pointer",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "#1f2937")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "none")}
                >
                  <span><span style={{ color: "#fbbf24", marginRight: 6 }}>📷</span>{c.name || c.id}</span>
                  {distLabel && <span style={{ color: "#4b5563", fontSize: 10, flexShrink: 0, marginLeft: 6 }}>{distLabel}</span>}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function Legend({ t, cctvCount }) {
  const items = [
    { color: "#0078ff", key: "legend.in" },
    { color: "#ff3232", key: "legend.out" },
    { color: "#ff1e1e", key: "legend.speeding", bold: true },
    { color: "#c8c8c8", key: "legend.unknown" },
    { color: "#fbbf24", key: null, label: `CCTV (${cctvCount})`, square: true },
    { color: "#22d3ee", key: "legend.fov", square: true, opacity: 0.4 },
  ];
  return (
    <div style={{
      position: "absolute", bottom: 16, left: 16,
      background: "rgba(17,24,39,0.85)", borderRadius: 8, padding: "8px 14px", fontSize: 12,
    }}>
      {items.map((it) => (
        <div key={it.key ?? it.label} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
          <span style={{
            width: 10, height: 10, borderRadius: it.square ? 2 : "50%",
            background: it.color, opacity: it.opacity ?? 1, display: "inline-block",
            border: it.square ? "1.5px solid #fff" : "none",
          }} />
          <span style={{ color: "#d1d5db", fontWeight: it.bold ? 700 : 400 }}>
            {it.key ? t(it.key) : it.label}
          </span>
        </div>
      ))}
    </div>
  );
}

