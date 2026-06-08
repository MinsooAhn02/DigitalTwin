import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import RoiEditor from "./RoiEditor";
import CalibrationMode, { CALIB_COLORS } from "./CalibrationMode";
import { useLang } from "../i18n/index.jsx";

const PANEL_W = 720;
const API_BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const MJPEG_URL      = `${API_BASE}/video-stream`;
const MJPEG_YOLO_URL = `${API_BASE}/video-stream-yolo`;
const HLS_PROXY = (url) => `${API_BASE}/hls-proxy?url=${encodeURIComponent(url)}`;
const DEFAULT_RUNTIME_CONFIG = {
  captureIntervalMs: 33,
  captureWidth: 640,
  captureQuality: 0.92,
  maxInFlight: 2,
};

// ── Calibration info bar (above video, outside 16:9 div) ─────────────────
function CalibBar({ calibState, t }) {
  if (!calibState) return null;
  const { pairs, pairIdx, isPixelStep, isGpsStep, isDone, saving, error, actions } = calibState;

  const stepMsg = isDone
    ? t("calib.step.done")
    : isPixelStep
      ? t("calib.step.pixel", { n: pairIdx + 1 })
      : isGpsStep
        ? t("calib.step.gps", { n: pairIdx + 1 })
        : "";

  return (
    <div style={{
      background: "#0f172a", borderTop: "1px solid #1e3a5f",
      padding: "8px 12px", display: "flex", flexDirection: "column", gap: 6,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: "#38bdf8" }}>{t("calib.title")}</span>
        <span style={{ flex: 1, fontSize: 11, color: "#94a3b8" }}>{stepMsg}</span>
        {error && <span style={{ fontSize: 11, color: "#f87171" }}>⚠ {error}</span>}
        <button onClick={actions?.reset} style={btnStyle}>{t("calib.reset")}</button>
        <button
          onClick={actions?.save}
          disabled={!isDone || saving}
          style={{ ...btnStyle, background: isDone ? "#0369a1" : "#374151", color: isDone ? "#fff" : "#6b7280", cursor: isDone ? "pointer" : "default", border: "none" }}
        >{saving ? t("calib.saving") : t("calib.save")}</button>
        <button onClick={actions?.close} style={btnStyle}>{t("calib.cancel")}</button>
      </div>

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {[0, 1, 2, 3].map((i) => {
          const p = pairs[i];
          const color = CALIB_COLORS[i];
          return (
            <div key={i} style={{
              display: "flex", alignItems: "center", gap: 4,
              background: "#1e293b", borderRadius: 5, padding: "3px 8px",
              border: `1px solid ${p?.gps ? color : "#374151"}`, fontSize: 11,
            }}>
              <span style={{ color, fontWeight: 700 }}>●</span>
              <span style={{ color: "#e2e8f0" }}>{t("calib.point", { n: i + 1 })}</span>
              {p?.pixel
                ? <span style={{ color: "#64748b" }}>({p.pixel[0]}, {p.pixel[1]})</span>
                : <span style={{ color: "#475569" }}>{t("calib.notClicked")}</span>}
              {p?.gps
                ? <span style={{ color: "#34d399", marginLeft: 2 }}>{t("calib.gpsSet")}</span>
                : p?.pixel
                  ? <span style={{ color: "#fbbf24", marginLeft: 2 }}>{t("calib.awaitingMap")}</span>
                  : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── ROI control bar (below video, outside 16:9 div) ───────────────────────
function RoiBar({ roiState, t }) {
  if (!roiState) return null;
  const { points, closed, hint, saving, actions } = roiState;
  const vertLabel = t("roi.vertices", { n: points.length, done: closed ? t("roi.vertDone") : "" });
  return (
    <div style={{
      background: "#0f172a", borderTop: "1px solid #1e3a5f",
      padding: "8px 12px", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
    }}>
      <span style={{ fontSize: 12, fontWeight: 700, color: "#22c55e" }}>{t("roi.title")}</span>
      <span style={{ flex: 1, fontSize: 11, color: "#94a3b8" }}>{t(hint)}</span>
      <span style={{ fontSize: 11, color: "#64748b" }}>{vertLabel}</span>
      <button onClick={actions?.reset} style={btnStyle}>{t("roi.reset")}</button>
      <button
        onClick={actions?.save}
        disabled={points.length < 3 || saving}
        style={{ ...btnStyle, background: points.length >= 3 ? "#0369a1" : "#374151", color: points.length >= 3 ? "#fff" : "#6b7280", cursor: points.length >= 3 ? "pointer" : "default", border: "none" }}
      >{saving ? t("roi.saving") : t("roi.save")}</button>
      <button onClick={actions?.close} style={btnStyle}>{t("roi.cancel")}</button>
    </div>
  );
}

// ── 스트림 로딩/오류 오버레이 ──────────────────────────────────────────
function StreamOverlay({ loading, cameraStatus, t }) {
  if (cameraStatus?.type === "failed") {
    return (
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.88)", color: "#f87171", fontSize: 13, gap: 8 }}>
        <div style={{ fontSize: 28 }}>⚠</div>
        <div>{t("cctv.stream.failed")}</div>
        {cameraStatus.message ? <div style={{ fontSize: 11, color: "#64748b", maxWidth: 260, textAlign: "center" }}>{cameraStatus.message}</div> : null}
      </div>
    );
  }
  if (cameraStatus?.type === "retrying") {
    return (
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.85)", color: "#fbbf24", fontSize: 13, gap: 8 }}>
        <div style={{ fontSize: 28 }}>🔄</div>
        <div>{t("cctv.stream.retrying")}</div>
      </div>
    );
  }
  if (loading) {
    return (
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.85)", color: "#94a3b8", fontSize: 13, gap: 8 }}>
        <div style={{ fontSize: 28 }}>⏳</div>
        <div>{t("cctv.stream.loading")}</div>
      </div>
    );
  }
  return null;
}

const btnStyle = {
  fontSize: 11, padding: "3px 8px", borderRadius: 5,
  border: "1px solid #374151", background: "#1e293b",
  color: "#e2e8f0", cursor: "pointer",
};

export default function CctvPlayer({ cctv, onClose, pendingGps, onNeedGps, onCancelGps, onCalibSaved, onCalibTabChange, switching, cameraStatus }) {
  const { t } = useLang();
  const videoRef  = useRef(null);
  const hlsRef    = useRef(null);
  const panelRef  = useRef(null);

  const [hlsError, setHlsError]       = useState(null);
  const [hlsLoading, setHlsLoading]   = useState(true);
  const [tab, setTab]                 = useState("live");
  const [pos, setPos]                 = useState(null);
  const [runtimeConfig, setRuntimeConfig] = useState(DEFAULT_RUNTIME_CONFIG);
  const [roiEditing, setRoiEditing]       = useState(false);
  const [currentRoi, setCurrentRoi]       = useState(null);
  const [calibrating, setCalibrating]     = useState(false);
  const [calibrated, setCalibrated]       = useState(false);
  const [calibState, setCalibState]       = useState(null);
  const [roiState, setRoiState]           = useState(null);
  const [streamKey, setStreamKey]         = useState(0);
  const [mjpegLoading, setMjpegLoading]   = useState(false);
  const [yoloLoading, setYoloLoading]     = useState(false);

  // camera_ready 도착 시 (cameraStatus → null) 로딩 해제
  useEffect(() => {
    if (cameraStatus === null) {
      setMjpegLoading(false);
      setYoloLoading(false);
    }
  }, [cameraStatus]);

  useEffect(() => {
    fetch(`${API_BASE}/runtime-config`)
      .then((r) => r.json())
      .then((config) => setRuntimeConfig({ ...DEFAULT_RUNTIME_CONFIG, ...config }))
      .catch(() => setRuntimeConfig(DEFAULT_RUNTIME_CONFIG));
  }, []);

  useEffect(() => {
    setCurrentRoi(null); setRoiEditing(false);
    setCalibrating(false); setCalibrated(false);
    setCalibState(null); setRoiState(null);
    onCancelGps?.(); onCalibTabChange?.(false);
    // 카메라 URL 변경 시 MJPEG img를 강제 재연결해 이전 카메라 화면이 굳는 현상 방지
    setStreamKey(k => k + 1);
    setMjpegLoading(true);
    setYoloLoading(true);
    // 최대 30s 대기 (backend HLS redirect + 연결에 시간 소요)
    const t = setTimeout(() => { setMjpegLoading(false); setYoloLoading(false); }, 30000);
    return () => clearTimeout(t);
  }, [cctv?.cctvurl]);  // eslint-disable-line react-hooks/exhaustive-deps

  // ── HLS 스트림 (10-4: watchdog 개선) ─────────────────────────────
  useEffect(() => {
    if (!cctv?.cctvurl || !videoRef.current) return;
    setHlsError(null);
    setHlsLoading(true);
    if (hlsRef.current) { hlsRef.current.destroy(); hlsRef.current = null; }

    const video = videoRef.current;
    video.src = "";
    video.load();

    let loadTimeout = null;

    if (Hls.isSupported()) {
      const hls = new Hls({ enableWorker: true, lowLatencyMode: true, backBufferLength: 0 });
      hlsRef.current = hls;
      loadTimeout = setTimeout(() => setHlsLoading(false), 15000);
      hls.on(Hls.Events.MANIFEST_PARSED, () => { clearTimeout(loadTimeout); setHlsLoading(false); });

      let networkErrCount = 0;
      hls.on(Hls.Events.ERROR, (_, d) => {
        if (!d.fatal) {
          if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
            networkErrCount++;
            if (networkErrCount >= 3) {
              // 라이브 엣지로 점프 후 재생 재개
              hls.startLoad(-1);
              video.play().catch(() => {});
              networkErrCount = 0;
            }
          }
          return;
        }
        networkErrCount = 0;
        if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
          fetch(
            `${API_BASE}/cctv-refresh?name=${encodeURIComponent(cctv.name || "")}&lat=${cctv.lat}&lon=${cctv.lon}`
          )
            .then((r) => r.json())
            .then(({ cctvurl }) => {
              if (cctvurl) hls.loadSource(HLS_PROXY(cctvurl));
              hls.startLoad(-1);
              video.play().catch(() => {});
            })
            .catch(() => { hls.startLoad(-1); video.play().catch(() => {}); });
        } else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
          hls.recoverMediaError();
          video.play().catch(() => {});
        } else {
          setHlsError(t("cctv.stream.error"));
          setHlsLoading(false);
        }
      });

      // watchdog: 5초마다 currentTime 진행 여부 확인 → 미진행 시 라이브 엣지 복귀
      let lastTime = -1;
      const stallTimer = setInterval(() => {
        if (!video || video.paused || video.readyState < 2) return;
        if (video.currentTime === lastTime) {
          hls.stopLoad();
          hls.startLoad(-1);  // 라이브 엣지로 점프
          video.play().catch(() => {});
        }
        lastTime = video.currentTime;
      }, 5000);

      // 브라우저 네이티브 stalled / waiting 이벤트
      const handleStall = () => {
        hls.stopLoad();
        hls.startLoad(-1);
        video.play().catch(() => {});
      };
      video.addEventListener("stalled", handleStall);
      video.addEventListener("waiting", handleStall);

      hls.loadSource(HLS_PROXY(cctv.cctvurl));
      hls.attachMedia(video);
      video.play().catch(() => {});

      return () => {
        clearTimeout(loadTimeout);
        clearInterval(stallTimer);
        video.removeEventListener("stalled", handleStall);
        video.removeEventListener("waiting", handleStall);
        if (hlsRef.current) { hlsRef.current.destroy(); hlsRef.current = null; }
      };
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = HLS_PROXY(cctv.cctvurl);
      video.addEventListener("loadedmetadata", () => setHlsLoading(false), { once: true });
      video.play().catch(() => {});
    } else {
      setHlsError(t("cctv.stream.unsupported"));
      setHlsLoading(false);
    }
    return () => {
      clearTimeout(loadTimeout);
      if (hlsRef.current) { hlsRef.current.destroy(); hlsRef.current = null; }
    };
  }, [cctv?.cctvurl]); // eslint-disable-line react-hooks/exhaustive-deps


  // ── 드래그 ────────────────────────────────────────────────────────
  const handleMouseDown = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const rect   = panelRef.current.getBoundingClientRect();
    const parent = panelRef.current.parentElement.getBoundingClientRect();
    const origin = { mx: e.clientX, my: e.clientY, px: rect.left - parent.left, py: rect.top - parent.top };
    const onMove = (e) => setPos({ x: Math.max(0, origin.px + e.clientX - origin.mx), y: Math.max(0, origin.py + e.clientY - origin.my) });
    const onUp   = () => { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  };

  if (!cctv) return null;

  const panelStyle = {
    position: "absolute", width: PANEL_W, zIndex: 20,
    background: "#0f172a", borderRadius: 10, overflow: "hidden",
    border: "1px solid #1e3a5f", boxShadow: "0 8px 32px rgba(0,0,0,0.7)",
    userSelect: "none",
    ...(pos ? { left: pos.x, top: pos.y } : { left: 16, bottom: 130 }),
  };

  return (
    <div ref={panelRef} style={panelStyle}>

      {/* 헤더 */}
      <div onMouseDown={handleMouseDown} style={{
        display: "flex", alignItems: "center", gap: 6,
        padding: "8px 12px", background: "#1e293b",
        borderBottom: "1px solid #1e3a5f", cursor: "grab",
      }}>
        <span style={{ fontSize: 12, color: "#475569" }}>⠿</span>
        <span style={{ color: "#fbbf24" }}>📷</span>
        <span style={{ color: "#e2e8f0", fontSize: 12, fontWeight: 600, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {cctv.name || "CCTV"}
        </span>
        <span style={{ fontSize: 9, color: "#22d3ee", background: "#0e4f6a", borderRadius: 4, padding: "2px 6px", fontWeight: 700 }}>LIVE</span>
        <button onClick={onClose} style={{ background: "none", border: "none", color: "#6b7280", cursor: "pointer", fontSize: 18, lineHeight: 1, padding: "0 0 0 8px" }}>✕</button>
      </div>

      {/* 탭 */}
      <div style={{ display: "flex", background: "#0f172a", borderBottom: "1px solid #1e3a5f" }}>
        {[
          { key: "live", labelKey: "cctv.tab.live" },
          { key: "yolo", labelKey: "cctv.tab.yolo" },
          { key: "cal",  labelKey: "cctv.tab.cal"  },
          { key: "roi",  labelKey: "cctv.tab.roi"  },
        ].map(({ key, labelKey }) => (
          <button key={key} onClick={() => {
            setTab(key);
            setRoiEditing(key === "roi");
            if (key !== "cal") { setCalibrating(false); setCalibState(null); onCancelGps?.(); onCalibTabChange?.(false); }
            if (key === "cal") { setCalibrating(true); onCalibTabChange?.(true); }
            if (key !== "roi") setRoiState(null);
          }} style={{
            flex: 1, padding: "7px 0", background: tab === key ? "#1e293b" : "transparent",
            border: "none", borderBottom: tab === key ? "2px solid #38bdf8" : "2px solid transparent",
            color: tab === key ? "#f1f5f9" : "#64748b", fontSize: 11, fontWeight: 600, cursor: "pointer",
          }}>
            {t(labelKey)}

            {key === "roi" && currentRoi && (
              <span style={{ marginLeft: 6, fontSize: 10, color: "#22d3ee" }}>{t("cctv.roi.set")}</span>
            )}
            {key === "cal" && calibrated && (
              <span style={{ marginLeft: 6, fontSize: 10, color: "#34d399" }}>{t("cctv.cal.done")}</span>
            )}
          </button>
        ))}
      </div>

      {/* 보정 컨트롤 바 — 영상 위, 비디오 div 밖 */}
      {calibrating && <CalibBar calibState={calibState} t={t} />}

      {/* 영상 영역 (16:9) */}
      <div style={{ position: "relative", aspectRatio: "16/9", background: "#000" }}>

        {/* live 탭: MJPEG 스트림 (HLS CORS 우회) */}
        {tab === "live" && (
          <>
            <img
              key={streamKey}
              src={`${MJPEG_URL}?k=${streamKey}`}
              alt="live"
              onLoad={() => setMjpegLoading(false)}
              style={{ width: "100%", height: "100%", objectFit: "contain", display: "block", background: "#000" }}
            />
            <StreamOverlay
              loading={mjpegLoading || switching}
              cameraStatus={cameraStatus}
              t={t}
            />
          </>
        )}

        {/* cal / roi 탭: MJPEG 배경 + 투명 video (오버레이 dimension 기준용) */}
        {(tab === "cal" || tab === "roi") && (
          <img
            key={streamKey}
            src={`${MJPEG_URL}?k=${streamKey}`}
            alt="cal-bg"
            style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "contain" }}
          />
        )}
        <video ref={videoRef} muted playsInline style={{
          position: "absolute", inset: 0,
          width: "100%", height: "100%", objectFit: "contain",
          opacity: 0, pointerEvents: "none",
        }} />

        {tab === "yolo" && (
          <>
            <img
              key={streamKey}
              src={`${MJPEG_YOLO_URL}?k=${streamKey}`}
              alt="YOLO"
              onLoad={() => setYoloLoading(false)}
              style={{ width: "100%", height: "100%", objectFit: "contain", display: "block", background: "#000" }}
            />
            <StreamOverlay
              loading={yoloLoading || switching}
              cameraStatus={cameraStatus}
              t={t}
            />
          </>
        )}

        {(tab === "cal" || tab === "roi") && hlsLoading && !hlsError && (
          <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,1)", color: "#94a3b8", fontSize: 13 }}>
            <div style={{ fontSize: 28, marginBottom: 8 }}>⏳</div>{t("cctv.stream.loading")}
          </div>
        )}

        {hlsError && (tab === "cal" || tab === "roi") && (
          <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.88)", color: "#f87171", fontSize: 12, padding: 16, textAlign: "center" }}>
            <div style={{ fontSize: 24, marginBottom: 8 }}>⚠️</div>{hlsError}
          </div>
        )}

        {/* ROI 오버레이 — canvas only */}
        {roiEditing && cctv?.cctvurl && (
          <RoiEditor
            videoRef={videoRef}
            cctvurl={cctv.cctvurl}
            initialRoi={currentRoi}
            onClose={() => { setRoiEditing(false); setRoiState(null); setTab("live"); }}
            onSaved={(roi) => { setCurrentRoi(roi); setRoiEditing(false); setRoiState(null); setTab("live"); }}
            onStateChange={setRoiState}
          />
        )}

        {/* 보정 오버레이 — canvas only */}
        {calibrating && cctv?.cctvurl && (
          <CalibrationMode
            videoRef={videoRef}
            cctvurl={cctv.cctvurl}
            pendingGps={pendingGps}
            onNeedGps={onNeedGps}
            onCancelGps={onCancelGps}
            onClose={() => { setCalibrating(false); setCalibState(null); setTab("live"); onCancelGps?.(); }}
            onSaved={(heading, gpsRing) => {
              setCalibrated(true); setCalibrating(false); setCalibState(null);
              setTab("roi"); setRoiEditing(true);
              onCancelGps?.(); onCalibTabChange?.(false);
              onCalibSaved?.(heading, gpsRing);
            }}
            onStateChange={setCalibState}
          />
        )}
      </div>

      {/* ROI 컨트롤 바 — 영상 아래, 비디오 div 밖 */}
      {roiEditing && <RoiBar roiState={roiState} t={t} />}

    </div>
  );
}
