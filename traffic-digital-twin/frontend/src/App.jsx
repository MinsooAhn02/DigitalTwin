import { useReducer, useEffect, useState, useCallback, useMemo, useRef } from "react";
import { FlyToInterpolator } from "deck.gl";
import { useWebSocket } from "./hooks/useWebSocket";
import MapView from "./components/MapView";
import { updateTrailMap, useTrailLayer } from "./components/TrailLayer";
import ClassBarChart  from "./components/ClassBarChart";
import VehicleTable   from "./components/VehicleTable";
import CounterPanel  from "./components/CounterPanel";
import CctvPlayer    from "./components/CctvPlayer";
import HistoryPanel  from "./components/HistoryPanel";
import { useLang }   from "./i18n/index.jsx";

const API_BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

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
  const { frameData, isConnected, error, cameraReady, cameraReadyInfo, autoCalibInfo, backgroundStatus, congestionClusters, cameraStatus } = useWebSocket();
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
  const [monitoredCams, setMonitoredCams] = useState(new Set());
  const [sidebarTab, setSidebarTab]       = useState("live");  // "live" | "monitor"
  const [cctvDrawerOpen, setCctvDrawerOpen] = useState(false);
  const [cctvDrawerQuery, setCctvDrawerQuery] = useState("");
  const switchDebounceRef                 = useRef(null);
  const switchTimeoutRef                  = useRef(null);

  useEffect(() => {
    if (cameraReady > 0) {
      setSwitching(false);
      if (switchTimeoutRef.current) clearTimeout(switchTimeoutRef.current);
    }
  }, [cameraReady]);

  // 라이브 시청 상태(카메라 선택 × 페이지 visible)를 백엔드에 보고 →
  // 미시청 시 라이브 YOLO 중단(GPU 절약). 백그라운드 모니터는 영향 없음.
  useEffect(() => {
    const report = () => {
      const active = selectedCctv != null && document.visibilityState === "visible";
      fetch(`${API_BASE}/viewer-active`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active }),
      }).catch(() => {});
    };
    report();
    document.addEventListener("visibilitychange", report);
    return () => document.removeEventListener("visibilitychange", report);
  }, [selectedCctv]);

  // 카메라 선택 시 주변 노드링크 노드 fetch (캘리브레이션 GPS 스냅용)
  useEffect(() => {
    if (!selectedCctv?.lat || !selectedCctv?.lon) { setSnapNodes([]); return; }
    fetch(`${API_BASE}/nodelink/nodes?lat=${selectedCctv.lat}&lon=${selectedCctv.lon}&radius_km=0.3`)
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
    fetch(`${API_BASE}/calibration/${cameraReadyInfo.camera_key}`)
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
  const roadName     = cameraReadyInfo?.road_name  ?? null;
  const roadLanes    = cameraReadyInfo?.road_lanes  ?? null;
  const roadMaxSpd   = cameraReadyInfo?.road_max_spd ?? null;

  useEffect(() => {
    if (activeData) dispatchTrail(vehicles);
  }, [activeData]);

  const viewStateRef = useRef(viewState);
  useEffect(() => { viewStateRef.current = viewState; }, [viewState]);

  const fetchCctvs = useCallback((vs) => {
    const { minX, maxX, minY, maxY } = viewBbox(vs ?? viewStateRef.current);
    setCctvLoading(true);
    fetch(`${API_BASE}/cctvs?minX=${minX}&maxX=${maxX}&minY=${minY}&maxY=${maxY}`)
      .then((r) => r.json())
      .then((data) => {
        if (data && data.length > 0) {
          // 성공 시 캐시 저장 (기존 캐시와 병합, id 기준 중복 제거)
          try {
            const prev = JSON.parse(localStorage.getItem("cctvCache") || "[]");
            const merged = [...prev];
            for (const item of data) {
              if (!merged.find(c => c.id === item.id)) merged.push(item);
            }
            localStorage.setItem("cctvCache", JSON.stringify(merged));
          } catch (_) {}
          setCctvList(data);
        } else {
          // API 실패(빈 배열) → 캐시에서 복원
          try {
            const cached = JSON.parse(localStorage.getItem("cctvCache") || "[]");
            if (cached.length > 0) setCctvList(cached);
          } catch (_) {}
        }
      })
      .catch(() => {
        try {
          const cached = JSON.parse(localStorage.getItem("cctvCache") || "[]");
          if (cached.length > 0) setCctvList(cached);
        } catch (_) {}
      })
      .finally(() => setCctvLoading(false));
  }, []);

  useEffect(() => { fetchCctvs(INITIAL_VIEW); }, []);

  useEffect(() => {
    if (cctvList.length === 0) return;
    const timer = setTimeout(() => setGuideVisible(false), 5000);
    return () => clearTimeout(timer);
  }, [cctvList.length]);

  const trailLayer = useTrailLayer(trailMap);

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
      fetch(`${API_BASE}/switch-camera`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ cctvurl: cctv.cctvurl, lat: cctv.lat, lon: cctv.lon, name: cctv.name_ko ?? cctv.name ?? "" }),
      }).catch(() => setSwitching(false));
    }, 300);
  }, [calMode]);

  const handleCalibSaved = useCallback((heading, calibGpsRing) => {
    if (!selectedCctv) return;
    const updated = { ...selectedCctv, heading, ...(calibGpsRing ? { calibGpsRing } : {}) };
    setSelectedCctv(updated);
    setCctvList((prev) => prev.map((c) => c.id === updated.id ? updated : c));
  }, [selectedCctv]);

  const monitoredCamsRef = useRef(monitoredCams);
  useEffect(() => { monitoredCamsRef.current = monitoredCams; }, [monitoredCams]);

  const handleToggleMonitor = useCallback((c) => {
    const camKey = c.cam_key;
    if (!camKey || !c.cctvurl) return;
    if (monitoredCamsRef.current.has(camKey)) {
      fetch(`${API_BASE}/background/remove/${camKey}`, { method: "POST" }).catch(() => {});
      setMonitoredCams((prev) => { const s = new Set(prev); s.delete(camKey); return s; });
    } else {
      fetch(`${API_BASE}/background/add`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cam_key: camKey,
          name:    c.name_en || c.name || c.id,
          name_ko: c.name_ko || c.name || c.id,
          url:     c.cctvurl,
          lat:     c.lat ?? 0,
          lon:     c.lon ?? 0,
        }),
      }).catch(() => {});
      setMonitoredCams((prev) => new Set([...prev, camKey]));
    }
  }, []);

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
          fovNearM={autoCalibInfo?.near_m ?? (cameraReadyInfo?.road_width_m ? 15 : null)}
          fovFarM={autoCalibInfo?.far_m ?? (cameraReadyInfo?.road_width_m ? 75 : null)}
          fovRoadWidthM={autoCalibInfo?.road_width_m ?? cameraReadyInfo?.road_width_m ?? null}
          fovHeadingDeg={
            autoCalibInfo?.heading
            ?? cameraReadyInfo?.name_bearing
            ?? cameraReadyInfo?.road_bearing
            ?? null
          }
          fovSnapLat={cameraReadyInfo?.snap_lat ?? selectedCctv?.lat ?? null}
          fovSnapLon={cameraReadyInfo?.snap_lon ?? selectedCctv?.lon ?? null}
          fovRoadPts={cameraReadyInfo?.road_pts ?? null}
          fovSnapAlongM={cameraReadyInfo?.snap_along_m ?? null}
          fovRoiGpsRing={cameraReadyInfo?.roi_gps_ring ?? null}
          backgroundStatus={backgroundStatus}
          congestionClusters={congestionClusters}
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
            <span style={{ color: "#fde68a" }}>{cctvDisplayName(selectedCctv, lang)}</span>
            {selectedCctv.cam_key && selectedCctv.cctvurl && (
              <button
                onClick={() => handleToggleMonitor(selectedCctv)}
                title={monitoredCams.has(selectedCctv.cam_key) ? t("app.stopMonitor") : t("app.monitor")}
                style={{ background: "none", border: "none", cursor: "pointer", padding: "0 4px", fontSize: 13, opacity: 0.9 }}
              >
                <span style={{ color: monitoredCams.has(selectedCctv.cam_key) ? "#22c55e" : "#4b5563" }}>📡</span>
              </button>
            )}
            <button onClick={() => { setSelectedCctv(null); fetch(`${API_BASE}/stop-camera`, { method: "POST" }).catch(() => {}); }} style={{ background: "none", border: "none", color: "#6b7280", cursor: "pointer", padding: 0, marginLeft: 2, fontSize: 14 }}>✕</button>
          </div>
        )}

        {/* 카메라 미선택 안내 — 하단 중앙 힌트 */}
        {noCameraSelected && (() => {
          const isLight = mapMode === "light";
          return (
            <div style={{
              position: "absolute", bottom: 24, left: "50%", transform: "translateX(-50%)",
              background: isLight ? "rgba(255,255,255,0.92)" : "rgba(17,24,39,0.88)",
              padding: "12px 20px",
              borderRadius: 10, fontSize: 14, backdropFilter: "blur(6px)",
              border: `1px solid ${isLight ? "#cbd5e1" : "#374151"}`,
              boxShadow: isLight ? "0 2px 12px rgba(0,0,0,0.12)" : "0 2px 12px rgba(0,0,0,0.4)",
              pointerEvents: "none",
              display: "flex", alignItems: "center", gap: 10, whiteSpace: "nowrap",
            }}>
              <span style={{ fontSize: 22, filter: isLight ? "none" : "drop-shadow(0 0 4px #38bdf8)" }}>📷</span>
              <div>
                <div style={{ color: isLight ? "#0f172a" : "#f9fafb", fontWeight: 700, fontSize: 14 }}>{t("app.clickCctv")}</div>
                <div style={{ color: isLight ? "#475569" : "#94a3b8", fontSize: 12, marginTop: 2 }}>{t("app.clickCctvSub")}</div>
              </div>
            </div>
          );
        })()}

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
          <div style={{ fontSize: 11, color: "#9ca3af", textAlign: "center" }}>
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

        {/* ── CCTV 드로어 ── */}
        {/* 토글 탭 버튼 */}
        <button
          onClick={() => setCctvDrawerOpen(o => !o)}
          style={{
            position: "absolute", left: cctvDrawerOpen ? 280 : 0, top: "50%",
            transform: "translateY(-50%)",
            transition: "left 0.25s ease",
            background: "rgba(17,24,39,0.92)", border: "1px solid #374151",
            borderLeft: cctvDrawerOpen ? "1px solid #374151" : "none",
            borderRadius: cctvDrawerOpen ? "0 8px 8px 0" : "0 8px 8px 0",
            color: "#fbbf24", cursor: "pointer",
            padding: "14px 6px", fontSize: 13, writingMode: "vertical-rl",
            backdropFilter: "blur(4px)", zIndex: 20, letterSpacing: 1,
          }}
          title="CCTV 목록"
        >
          {cctvDrawerOpen ? "◀ 닫기" : "📷 CCTV"}
        </button>

        {/* 드로어 패널 */}
        <div style={{
          position: "absolute", left: 0, top: 0, bottom: 0,
          width: 280,
          transform: cctvDrawerOpen ? "translateX(0)" : "translateX(-100%)",
          transition: "transform 0.25s ease",
          background: "rgba(15,23,42,0.97)", borderRight: "1px solid #1f2937",
          display: "flex", flexDirection: "column",
          zIndex: 19, backdropFilter: "blur(8px)",
        }}>
          <div style={{ padding: "12px 12px 8px", borderBottom: "1px solid #1f2937", flexShrink: 0 }}>
            <div style={{ color: "#fbbf24", fontWeight: 700, fontSize: 13, marginBottom: 8 }}>
              📷 CCTV 목록 ({cctvList.length})
            </div>
            <input
              value={cctvDrawerQuery}
              onChange={e => setCctvDrawerQuery(e.target.value)}
              placeholder="카메라 검색..."
              style={{
                width: "100%", boxSizing: "border-box",
                background: "#1f2937", border: "1px solid #374151",
                borderRadius: 6, color: "#f9fafb", fontSize: 12,
                padding: "6px 10px", outline: "none",
              }}
            />
          </div>
          <div style={{ flex: 1, overflowY: "auto", padding: "6px 0" }}>
            {cctvList
              .filter(c => {
                const q = cctvDrawerQuery.trim().toLowerCase();
                if (!q) return true;
                return (c.name_ko || c.name || "").toLowerCase().includes(q) ||
                       (c.name_en || "").toLowerCase().includes(q);
              })
              .map(c => (
                <button
                  key={c.id}
                  onClick={() => { handleCctvClick(c); setCctvDrawerOpen(false); }}
                  style={{
                    width: "100%", background: selectedCctv?.id === c.id ? "#1e3a5f" : "none",
                    border: "none", borderBottom: "1px solid #1f2937",
                    color: selectedCctv?.id === c.id ? "#93c5fd" : "#d1d5db",
                    cursor: "pointer", padding: "8px 12px",
                    textAlign: "left", fontSize: 12, lineHeight: 1.4,
                  }}
                >
                  <div style={{ fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {c.name_ko || c.name}
                  </div>
                  {c.name_en && (
                    <div style={{ color: "#6b7280", fontSize: 10 }}>{c.name_en}</div>
                  )}
                </button>
              ))
            }
            {cctvList.length === 0 && (
              <div style={{ color: "#4b5563", fontSize: 12, textAlign: "center", padding: "24px 12px" }}>
                {cctvLoading ? "로딩 중…" : "지도를 이동하거나 새로고침하세요"}
              </div>
            )}
          </div>
        </div>

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
          onClose={() => {
            setSelectedCctv(null); setCalMode(null); setPendingGps(null);
            fetch(`${API_BASE}/stop-camera`, { method: "POST" }).catch(() => {});
          }}
          pendingGps={pendingGps}
          onNeedGps={() => { setPendingGps(null); setCalMode("awaiting"); }}
          onCancelGps={() => { setCalMode(null); setPendingGps(null); }}
          onCalibSaved={handleCalibSaved}
          onCalibTabChange={setCalibTabActive}
          switching={switching}
          cameraStatus={cameraStatus}
          cameraReadyInfo={cameraReadyInfo}
        />
      </div>

      {/* 오른쪽: 사이드바 */}
      <aside style={{ width: 360, display: "flex", flexDirection: "column", background: "#111827", borderLeft: "1px solid #1f2937" }}>
        {/* 헤더 */}
        <div style={{ padding: "14px 16px 0", flexShrink: 0 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
            <h1 style={{ margin: 0, fontSize: 15, fontWeight: 700, letterSpacing: "-0.02em" }}>{t("app.title")}</h1>
            <button
              onClick={() => setLang(lang === "en" ? "ko" : "en")}
              title={lang === "en" ? t("lang.switchToKo") : t("lang.switchToEn")}
              style={{ background: "rgba(30,41,59,0.9)", border: "1px solid #334155", borderRadius: 6, padding: "3px 10px", cursor: "pointer", fontSize: 12, fontWeight: 700, color: lang === "en" ? "#38bdf8" : "#fbbf24" }}
            >
              {lang === "en" ? "KO" : "EN"}
            </button>
          </div>

          {/* 탭 바 */}
          <div style={{ display: "flex", gap: 2, background: "#0f172a", borderRadius: 8, padding: 3, marginBottom: 12 }}>
            {[
              { key: "live",    label: t("tab.live") },
              { key: "monitor", label: t("tab.background"), badge: monitoredCams.size || null },
              { key: "history", label: t("tab.history") },
            ].map(tab => (
              <button key={tab.key} onClick={() => setSidebarTab(tab.key)}
                style={{
                  flex: 1, padding: "6px 0", borderRadius: 6, border: "none", cursor: "pointer",
                  background: sidebarTab === tab.key ? "#1f2937" : "none",
                  color: sidebarTab === tab.key ? "#f9fafb" : "#6b7280",
                  fontSize: 12, fontWeight: sidebarTab === tab.key ? 600 : 400,
                  position: "relative",
                }}
              >
                {tab.label}
                {tab.badge && (
                  <span style={{ position: "absolute", top: 2, right: 6, background: "#3b82f6", color: "#fff", borderRadius: 999, fontSize: 9, padding: "1px 4px", fontWeight: 700 }}>
                    {tab.badge}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>

        {/* 탭 콘텐츠: flex column, no outer scroll — each tab manages its own scroll */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>

          {/* ── Live View 탭 ── */}
          {sidebarTab === "live" && <>
            {/* 고정 상단 패널 (크기 고정, 찌부 없음) */}
            <div style={{ flexShrink: 0, display: "flex", flexDirection: "column", gap: 10, padding: "0 16px 0" }}>
              <CounterPanel inCount={inCount} outCount={outCount} vehicleCount={vehicleCnt} />

              {roadName && (
                <CollapsibleCard label={t("app.roadInfo")}>
                  <div style={{ fontSize: 12, lineHeight: 1.6 }}>
                    <div style={{ fontWeight: 600, color: "#e2e8f0", marginBottom: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={roadName}>{roadName}</div>
                    <div style={{ display: "flex", gap: 16, color: "#94a3b8" }}>
                      {roadLanes != null && roadLanes > 0 && <span>{t("app.roadLanes", { n: roadLanes })}</span>}
                      {roadMaxSpd != null && roadMaxSpd > 0 && (
                        <span><b style={{ color: "#fbbf24" }}>{t("app.roadSpeedLimit", { n: roadMaxSpd })}</b></span>
                      )}
                    </div>
                  </div>
                </CollapsibleCard>
              )}

              {autoCalibInfo?.cam_h_m != null && (
                <CollapsibleCard label={t("app.autoCalib")} defaultOpen={false} description={t("app.autoCalibDesc")}>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 12px", fontSize: 12, color: "#94a3b8" }}>
                    <span>{t("app.calibCamH")}</span><b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.cam_h_m} m</b>
                    <span>{t("app.calibRoadW")}</span><b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.road_width_m} m</b>
                    <span>{t("app.calibNear")}</span><b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.near_m} m</b>
                    <span>{t("app.calibFar")}</span><b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.far_m} m</b>
                    {autoCalibInfo.road_length_m != null && <>
                      <span>{t("app.calibRoadLen")}</span><b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.road_length_m} m</b>
                    </>}
                    <span>{t("app.calibTilt")}</span><b style={{ color: "#e2e8f0", textAlign: "right" }}>{autoCalibInfo.pitch_deg}°</b>
                  </div>
                </CollapsibleCard>
              )}

              {itsSpeed !== null && (
                <CollapsibleCard label={t("app.itsCompare")} defaultOpen={false} description={t("app.itsCompareDesc")}>
                  <div style={{ display: "flex", justifyContent: "space-around", alignItems: "center", fontSize: 12 }}>
                    <div style={{ textAlign: "center" }}>
                      <div style={{ color: "#64748b", fontSize: 10, marginBottom: 2 }}>{t("app.itsMeasured")}</div>
                      <div style={{ color: "#94a3b8", fontWeight: 700, fontSize: 18 }}>{ourAvgKph > 0 ? ourAvgKph.toFixed(1) : "—"}</div>
                      {ourAvgKph > 0 && <div style={{ color: "#4b5563", fontSize: 10 }}>km/h</div>}
                    </div>
                    <div style={{ color: "#374151", fontSize: 18 }}>vs</div>
                    <div style={{ textAlign: "center" }}>
                      <div style={{ color: "#64748b", fontSize: 10, marginBottom: 2 }}>{t("app.itsSegment")}</div>
                      <div style={{ color: "#94a3b8", fontWeight: 700, fontSize: 18 }}>{itsSpeed.toFixed(1)}</div>
                      <div style={{ color: "#4b5563", fontSize: 10 }}>km/h</div>
                    </div>
                    {speedErrPct !== null && ourAvgKph > 0 && (
                      <div style={{ textAlign: "center" }}>
                        <div style={{ color: "#64748b", fontSize: 10, marginBottom: 2 }}>{t("app.itsError")}</div>
                        <div style={{ fontWeight: 700, fontSize: 16, color: Math.abs(speedErrPct) < 10 ? "#34d399" : Math.abs(speedErrPct) < 20 ? "#fbbf24" : "#f87171" }}>
                          {speedErrPct > 0 ? "+" : ""}{speedErrPct.toFixed(1)}%
                        </div>
                      </div>
                    )}
                  </div>
                  <div style={{ marginTop: 6, textAlign: "center", fontSize: 10, color: "#4b5563" }}>
                    {t("app.scaleFactor")}&nbsp;
                    <span style={{ color: Math.abs(speedScale - 1) < 0.05 ? "#94a3b8" : "#fbbf24", fontWeight: 700 }}>×{speedScale.toFixed(3)}</span>
                  </div>
                </CollapsibleCard>
              )}

              <CollapsibleCard label={t("app.classDist")}>
                <ClassBarChart classCounts={classCounts} />
              </CollapsibleCard>
            </div>

            {/* 차량 리스트: 남은 공간을 차지하며 내부 스크롤 */}
            <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "10px 16px 8px" }}>
              <CollapsibleCard label={t("app.vehicleList")}>
                <VehicleTable vehicles={vehicles} calibrated={isCalibrated} />
              </CollapsibleCard>
            </div>
          </>}

          {/* ── Background 탭 ── */}
          {sidebarTab === "monitor" && (
            <div style={{ flex: 1, overflowY: "auto", padding: "0 16px 8px" }}>
              <MonitorPanel
                monitoredCams={monitoredCams}
                backgroundStatus={backgroundStatus}
                cctvList={cctvList}
                selectedCctv={selectedCctv}
                lang={lang}
                t={t}
                onToggleMonitor={handleToggleMonitor}
                onRemove={(camKey) => {
                  const c = cctvList.find((x) => x.cam_key === camKey);
                  if (c) handleToggleMonitor(c);
                  else {
                    fetch(`${API_BASE}/background/remove/${camKey}`, { method: "POST" }).catch(() => {});
                    setMonitoredCams((prev) => { const s = new Set(prev); s.delete(camKey); return s; });
                  }
                }}
              />
            </div>
          )}

          {/* ── History 탭 ── */}
          {sidebarTab === "history" && (
            <div style={{ flex: 1, overflowY: "auto", padding: "0 16px 8px" }}>
              <HistoryPanel lang={lang} t={t} />
            </div>
          )}
        </div>

        {/* CctvSearch: 하단 고정 */}
        <div style={{ padding: "10px 16px 14px", borderTop: "1px solid #1f2937", background: "#111827", flexShrink: 0 }}>
          <CctvSearch cctvList={cctvList} onSelect={handleCctvClick} viewState={viewState} />
        </div>
      </aside>
    </div>
  );
}

function CollapsibleCard({ children, label, defaultOpen = true, description = null }) {
  const [open, setOpen] = useState(defaultOpen);
  const [infoOpen, setInfoOpen] = useState(false);
  return (
    <div style={{ background: "#1f2937", borderRadius: 12, overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 16px" }}>
        <button onClick={() => setOpen(v => !v)} style={{
          flex: 1, display: "flex", alignItems: "center", background: "none", border: "none",
          cursor: "pointer", color: "#9ca3af", fontSize: 12, textAlign: "left", padding: 0,
        }}>
          <span>{label}</span>
        </button>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {description && (
            <button
              onClick={(e) => { e.stopPropagation(); setInfoOpen(true); }}
              style={{
                background: "none", border: "1px solid #374151", borderRadius: "50%",
                width: 16, height: 16, display: "flex", alignItems: "center", justifyContent: "center",
                cursor: "pointer", color: "#6b7280", fontSize: 10, padding: 0, lineHeight: 1,
              }}
              title="What is this?"
            >ℹ</button>
          )}
          <button onClick={() => setOpen(v => !v)} style={{
            background: "none", border: "none", cursor: "pointer",
            color: "#4b5563", fontSize: 9, padding: 0,
          }}>
            {open ? "▲" : "▼"}
          </button>
        </div>
      </div>
      {open && <div style={{ padding: "0 16px 14px" }}>{children}</div>}

      {infoOpen && (
        <div
          onClick={() => setInfoOpen(false)}
          style={{
            position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
            zIndex: 9999, display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: "#1f2937", borderRadius: 12, padding: 20, maxWidth: 340, width: "90%",
              border: "1px solid #374151", boxShadow: "0 20px 60px rgba(0,0,0,0.6)",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: "#e2e8f0" }}>{label}</span>
              <button
                onClick={() => setInfoOpen(false)}
                style={{ background: "none", border: "none", cursor: "pointer", color: "#6b7280", fontSize: 16, padding: 0 }}
              >×</button>
            </div>
            <p style={{ fontSize: 12, color: "#94a3b8", lineHeight: 1.7, margin: 0 }}>{description}</p>
          </div>
        </div>
      )}
    </div>
  );
}

const BG_STATUS_DOT = { normal: "#22c55e", busy: "#f97316", congested: "#ef4444", loading: "#9ca3af", error: "#6b7280" };

function cctvDisplayName(c, lang) {
  if (lang === "en") return c.name_en || (c.cam_key ? `CCTV ${c.cam_key.slice(0, 6)}` : c.id);
  return c.name_ko || c.name || c.id;
}

function MonitorPanel({ monitoredCams, backgroundStatus, cctvList, selectedCctv, lang, t, onToggleMonitor, onRemove }) {
  const [isAdding, setIsAdding] = useState(false);
  const [addQuery, setAddQuery] = useState("");
  const inputRef = useRef(null);

  const addResults = useMemo(() => {
    const q = addQuery.trim().toLowerCase();
    const available = cctvList.filter(c => c.cam_key && c.cctvurl && !monitoredCams.has(c.cam_key));
    if (!q) return available.slice(0, 6);
    return available.filter(c => cctvDisplayName(c, lang).toLowerCase().includes(q)).slice(0, 6);
  }, [cctvList, monitoredCams, addQuery, lang]);

  useEffect(() => {
    if (isAdding) setTimeout(() => inputRef.current?.focus(), 50);
  }, [isAdding]);

  const hasCams = monitoredCams.size > 0;

  return (
    <div style={{ background: "#1f2937", borderRadius: 12, padding: 14 }}>
      {/* 헤더 */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: hasCams || isAdding ? 10 : 6 }}>
        <span style={{ fontSize: 12, color: "#9ca3af", fontWeight: 600 }}>
          {t("app.bgMonitor")}
          {hasCams && <span style={{ color: "#6b7280", fontWeight: 400, marginLeft: 4 }}>({monitoredCams.size})</span>}
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          {/* 현재 보는 카메라 빠른 추가 */}
          {selectedCctv?.cam_key && selectedCctv?.cctvurl && !monitoredCams.has(selectedCctv.cam_key) && (
            <button
              onClick={() => onToggleMonitor(selectedCctv)}
              title={t("app.monitor")}
              style={{ background: "rgba(34,197,94,0.12)", border: "1px solid #22c55e", borderRadius: 6, padding: "3px 8px", fontSize: 11, color: "#4ade80", cursor: "pointer" }}
            >
              📡 {t("app.monitor")}
            </button>
          )}
          <button
            onClick={() => { setIsAdding(v => !v); setAddQuery(""); }}
            style={{ background: isAdding ? "transparent" : "rgba(59,130,246,0.12)", border: `1px solid ${isAdding ? "#4b5563" : "#3b82f6"}`, borderRadius: 6, padding: "3px 10px", fontSize: 11, color: isAdding ? "#9ca3af" : "#60a5fa", cursor: "pointer" }}
          >
            {isAdding ? t("calib.cancel") : `+ ${t("app.monitor")}`}
          </button>
        </div>
      </div>

      {/* 카메라 추가 검색 */}
      {isAdding && (
        <div style={{ marginBottom: hasCams ? 10 : 0 }}>
          <div style={{ position: "relative", marginBottom: 4 }}>
            <input
              ref={inputRef}
              value={addQuery}
              onChange={e => setAddQuery(e.target.value)}
              placeholder={t("cctv.search.placeholder")}
              style={{
                width: "100%", boxSizing: "border-box",
                background: "#111827", border: "1px solid #374151",
                borderRadius: 6, padding: "6px 28px 6px 10px",
                fontSize: 12, color: "#f9fafb", outline: "none",
              }}
            />
            <span style={{ position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)", color: "#6b7280", fontSize: 12 }}>🔍</span>
          </div>
          <div style={{ borderRadius: 6, overflow: "hidden", border: "1px solid #374151" }}>
            {addResults.length === 0
              ? <div style={{ padding: "8px 10px", fontSize: 11, color: "#6b7280", background: "#111827" }}>{t("app.bgMonitorEmpty")}</div>
              : addResults.map(c => (
                <button key={c.id} onClick={() => onToggleMonitor(c)}
                  style={{
                    display: "flex", justifyContent: "space-between", alignItems: "center",
                    width: "100%", textAlign: "left", padding: "8px 10px",
                    fontSize: 11, background: "#111827", border: "none",
                    borderBottom: "1px solid #1f2937", color: "#d1d5db", cursor: "pointer",
                  }}
                  onMouseEnter={e => e.currentTarget.style.background = "#1e293b"}
                  onMouseLeave={e => e.currentTarget.style.background = "#111827"}
                >
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    📷 {cctvDisplayName(c, lang)}
                  </span>
                  <span style={{ color: "#3b82f6", fontSize: 16, flexShrink: 0, marginLeft: 6, fontWeight: 700, lineHeight: 1 }}>+</span>
                </button>
              ))
            }
          </div>
        </div>
      )}

      {/* 모니터링 중 목록 */}
      {!hasCams && !isAdding && (
        <div style={{ fontSize: 11, color: "#6b7280", textAlign: "center", paddingTop: 2 }}>
          {t("app.bgMonitorEmpty")}
        </div>
      )}
      {hasCams && (
        <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          {[...monitoredCams].map(camKey => {
            const info = backgroundStatus[camKey];
            const cam = cctvList.find(c => c.cam_key === camKey);
            const dispName = lang === "en"
              ? (info?.name || cam?.name_en || `CCTV ${camKey.slice(0, 6)}`)
              : (info?.name_ko || cam?.name_ko || cam?.name || camKey);
            const dotColor = BG_STATUS_DOT[info?.status] ?? "#9ca3af";
            return (
              <div key={camKey} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
                <span style={{ width: 8, height: 8, borderRadius: "50%", background: dotColor, flexShrink: 0 }} />
                <span style={{ flex: 1, color: "#d1d5db", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      title={dispName}>{dispName}</span>
                <span style={{ color: "#9ca3af", flexShrink: 0, fontSize: 11 }}>
                  {info ? t("app.bgVehicles", { n: info.vehicle_count ?? 0 }) : "…"}
                </span>
                <button onClick={() => onRemove(camKey)}
                  style={{ background: "none", border: "none", color: "#6b7280", cursor: "pointer", padding: 0, fontSize: 14, lineHeight: 1 }}
                  title={t("app.stopMonitor")}>×</button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function CctvSearch({ cctvList, onSelect, viewState }) {
  const [query,   setQuery]   = useState("");
  const [open,    setOpen]    = useState(false);
  const [sortBy,  setSortBy]  = useState("name"); // "name" | "dist"
  const ref = useRef(null);
  const { t, lang } = useLang();

  const getDisplayName = (c) => cctvDisplayName(c, lang);

  const distKm = (c) => {
    if (!viewState || c.lat == null || c.lon == null) return Infinity;
    const dlat = (c.lat - viewState.latitude) * 110.574;
    const dlon = (c.lon - viewState.longitude) * 111.320 * Math.cos((viewState.latitude * Math.PI) / 180);
    return Math.hypot(dlat, dlon);
  };

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = q
      ? cctvList.filter((c) => getDisplayName(c).toLowerCase().includes(q))
      : [...cctvList];
    if (sortBy === "dist") return filtered.sort((a, b) => distKm(a) - distKm(b));
    return filtered.sort((a, b) =>
      getDisplayName(a).localeCompare(getDisplayName(b), lang === "ko" ? "ko" : "en")
    );
  }, [query, cctvList, sortBy, viewState, lang]);

  useEffect(() => {
    const close = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const handleSelect = (c) => {
    onSelect(c);
    setQuery(getDisplayName(c));
    setOpen(false);
  };

  const btnStyle = (active) => ({
    padding: "3px 8px", fontSize: 11, border: "none", borderRadius: 4, cursor: "pointer",
    background: active ? "#3b82f6" : "#1f2937",
    color: active ? "#fff" : "#9ca3af",
  });

  return (
    <div ref={ref} style={{ position: "relative", width: "100%" }}>
      <div style={{ position: "relative" }}>
        <input
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }}
          placeholder={t("cctv.search.placeholder")}
          style={{
            width: "100%", boxSizing: "border-box",
            background: "rgba(17,24,39,0.90)", border: "1px solid #374151",
            borderRadius: 8, padding: "7px 34px 7px 12px",
            fontSize: 13, color: "#f9fafb", outline: "none",
          }}
        />
        <span style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", color: "#6b7280", pointerEvents: "none" }}>🔍</span>
      </div>

      {open && results.length > 0 && (
        <div style={{
          position: "absolute", bottom: "calc(100% + 4px)", left: 0, right: 0,
          background: "rgba(17,24,39,0.97)", border: "1px solid #374151",
          borderRadius: 8, overflow: "hidden",
          boxShadow: "0 -8px 24px rgba(0,0,0,0.5)",
          zIndex: 50,
        }}>
          <div style={{ display: "flex", gap: 4, padding: "6px 10px", borderBottom: "1px solid #1f2937", alignItems: "center" }}>
            <span style={{ fontSize: 10, color: "#6b7280", marginRight: 4 }}>{t("cctv.search.sort")}</span>
            <button style={btnStyle(sortBy === "name")} onClick={() => setSortBy("name")}>{t("cctv.search.sortName")}</button>
            <button style={btnStyle(sortBy === "dist")} onClick={() => setSortBy("dist")}>{t("cctv.search.sortDist")}</button>
            <span style={{ marginLeft: "auto", fontSize: 10, color: "#4b5563" }}>{t("cctv.search.count", { n: results.length })}</span>
          </div>
          <div style={{ maxHeight: 240, overflowY: "auto" }}>
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
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    <span style={{ color: "#fbbf24", marginRight: 6 }}>📷</span>{getDisplayName(c)}
                  </span>
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

