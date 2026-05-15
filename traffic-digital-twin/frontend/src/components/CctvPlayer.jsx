import { useEffect, useRef, useState, useCallback } from "react";
import Hls from "hls.js";
import RoiEditor from "./RoiEditor";
import CalibrationMode from "./CalibrationMode";

const PANEL_W = 720;
const DEFAULT_RUNTIME_CONFIG = {
  captureIntervalMs: 33,
  captureWidth: 640,
  captureQuality: 0.92,
  maxInFlight: 2,
};

export default function CctvPlayer({ cctv, onClose, pendingGps, onNeedGps, onCancelGps }) {
  const videoRef  = useRef(null);
  const canvasRef = useRef(null);      // 숨김 캔버스 (프레임 캡처용)
  const hlsRef    = useRef(null);
  const panelRef  = useRef(null);
  const wsRef       = useRef(null);    // YOLO 탐지 WebSocket
  const inFlightRef = useRef(0);       // 전송 중 프레임 수 (최대 MAX_IN_FLIGHT)
  const intervalRef = useRef(null);

  const [hlsError, setHlsError]       = useState(null);
  const [hlsLoading, setHlsLoading]   = useState(true);
  const [tab, setTab]                 = useState("live");
  const [pos, setPos]                 = useState(null);
  const [annotatedUrl, setAnnotatedUrl] = useState(null);   // Object URL
  const [yoloStatus, setYoloStatus]   = useState("idle");  // idle | loading | running | error
  const [runtimeConfig, setRuntimeConfig] = useState(DEFAULT_RUNTIME_CONFIG);
  const [roiEditing, setRoiEditing]       = useState(false);
  const [currentRoi, setCurrentRoi]       = useState(null);  // 현재 저장된 ROI
  const [calibrating, setCalibrating]     = useState(false);
  const [calibrated, setCalibrated]       = useState(false);

  useEffect(() => {
    fetch("http://localhost:8000/runtime-config")
      .then((r) => r.json())
      .then((config) => setRuntimeConfig({ ...DEFAULT_RUNTIME_CONFIG, ...config }))
      .catch(() => setRuntimeConfig(DEFAULT_RUNTIME_CONFIG));
  }, []);

  // 카메라 변경 시 ROI/보정 초기화
  useEffect(() => {
    setCurrentRoi(null);
    setRoiEditing(false);
    setCalibrating(false);
    setCalibrated(false);
    onCancelGps?.();
  }, [cctv?.cctvurl]);  // eslint-disable-line react-hooks/exhaustive-deps

  // ── HLS 스트림 ────────────────────────────────────────────────────
  useEffect(() => {
    if (!cctv?.cctvurl || !videoRef.current) return;
    setHlsError(null);
    setHlsLoading(true);
    if (hlsRef.current) { hlsRef.current.destroy(); hlsRef.current = null; }

    const video = videoRef.current;

    if (Hls.isSupported()) {
      const hls = new Hls({ enableWorker: true, lowLatencyMode: true, backBufferLength: 0 });
      hlsRef.current = hls;
      hls.on(Hls.Events.MANIFEST_PARSED, () => setHlsLoading(false));
      hls.on(Hls.Events.ERROR, (_, d) => {
        if (!d.fatal) return;
        if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
          // HLS 토큰 만료 가능성 → 백엔드에서 신선한 URL 받아 재로드
          fetch(
            `http://localhost:8000/cctv-refresh?name=${encodeURIComponent(cctv.name || "")}&lat=${cctv.lat}&lon=${cctv.lon}`
          )
            .then((r) => r.json())
            .then(({ cctvurl }) => {
              if (cctvurl) hls.loadSource(cctvurl);
              hls.startLoad();
            })
            .catch(() => hls.startLoad());
        } else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
          hls.recoverMediaError();
        } else {
          setHlsError("스트림 연결 실패");
          setHlsLoading(false);
        }
      });
      hls.loadSource(cctv.cctvurl);
      hls.attachMedia(video);
      video.play().catch(() => {});
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = cctv.cctvurl;
      video.addEventListener("loadedmetadata", () => setHlsLoading(false), { once: true });
      video.play().catch(() => {});
    } else {
      setHlsError("HLS 미지원 브라우저");
      setHlsLoading(false);
    }
    return () => { if (hlsRef.current) { hlsRef.current.destroy(); hlsRef.current = null; } };
  }, [cctv?.cctvurl]);

  // ── YOLO WebSocket 연결 ───────────────────────────────────────────
  useEffect(() => {
    if (tab !== "yolo" || !cctv) return;
    setYoloStatus("loading");

    const ws = new WebSocket("ws://localhost:8000/ws/detect");
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen  = () => setYoloStatus("running");
    ws.onerror = () => { setYoloStatus("error"); };
    ws.onclose = () => { inFlightRef.current = 0; };

    ws.onmessage = (e) => {
      inFlightRef.current = Math.max(0, inFlightRef.current - 1);
      const blob = new Blob([e.data], { type: "image/jpeg" });
      const url  = URL.createObjectURL(blob);
      setAnnotatedUrl((prev) => { if (prev) URL.revokeObjectURL(prev); return url; });
    };

    return () => { ws.close(); wsRef.current = null; inFlightRef.current = 0; };
  }, [tab, cctv]);

  // ── 프레임 캡처 & 전송 ───────────────────────────────────────────
  const captureAndSend = useCallback(() => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    const ws = wsRef.current;

    if (
      inFlightRef.current >= runtimeConfig.maxInFlight ||
      !video || !canvas || !ws ||
      ws.readyState !== WebSocket.OPEN ||
      video.readyState < 2 ||
      video.paused
    ) return;

    inFlightRef.current++;

    const srcW = video.videoWidth  || 640;
    const srcH = video.videoHeight || 360;
    const scale = Math.min(1, runtimeConfig.captureWidth / srcW);
    const w = Math.round(srcW * scale);
    const h = Math.round(srcH * scale);
    canvas.width = w;
    canvas.height = h;
    canvas.getContext("2d").drawImage(video, 0, 0, w, h);

    canvas.toBlob((blob) => {
      if (!blob) { inFlightRef.current = Math.max(0, inFlightRef.current - 1); return; }
      blob.arrayBuffer().then((buf) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(buf);
        } else {
          inFlightRef.current = Math.max(0, inFlightRef.current - 1);
        }
      });
    }, "image/jpeg", runtimeConfig.captureQuality);
  }, [runtimeConfig]);

  // YOLO 탭 활성화 시 캡처 인터벌 시작
  useEffect(() => {
    if (tab !== "yolo") {
      if (intervalRef.current) { clearInterval(intervalRef.current); intervalRef.current = null; }
      return;
    }
    intervalRef.current = setInterval(captureAndSend, runtimeConfig.captureIntervalMs);
    return () => { clearInterval(intervalRef.current); intervalRef.current = null; };
  }, [tab, captureAndSend, runtimeConfig.captureIntervalMs]);

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

  const yoloStatusLabel = {
    idle:    { text: "대기",       color: "#64748b" },
    loading: { text: "모델 로드…", color: "#fbbf24" },
    running: { text: "탐지 중",    color: "#22d3ee" },
    error:   { text: "연결 실패",  color: "#f87171" },
  }[yoloStatus];

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
          { key: "live", label: "📷 실시간" },
          { key: "yolo", label: "🤖 YOLO 탐지" },
          { key: "roi",  label: "🎯 ROI 편집" },
          { key: "cal",  label: "🔧 보정" },
        ].map(({ key, label }) => (
          <button key={key} onClick={() => {
            setTab(key);
            setRoiEditing(key === "roi");
            if (key !== "cal") { setCalibrating(false); onCancelGps?.(); }
            if (key === "cal") setCalibrating(true);
          }} style={{
            flex: 1, padding: "7px 0", background: tab === key ? "#1e293b" : "transparent",
            border: "none", borderBottom: tab === key ? "2px solid #38bdf8" : "2px solid transparent",
            color: tab === key ? "#f1f5f9" : "#64748b", fontSize: 11, fontWeight: 600, cursor: "pointer",
          }}>
            {label}
            {key === "yolo" && tab === "yolo" && (
              <span style={{ marginLeft: 6, fontSize: 10, color: yoloStatusLabel.color }}>
                ● {yoloStatusLabel.text}
              </span>
            )}
            {key === "roi" && currentRoi && (
              <span style={{ marginLeft: 6, fontSize: 10, color: "#22d3ee" }}>● 설정됨</span>
            )}
            {key === "cal" && calibrated && (
              <span style={{ marginLeft: 6, fontSize: 10, color: "#34d399" }}>● 완료</span>
            )}
          </button>
        ))}
      </div>

      {/* 영상 영역 (16:9) */}
      <div style={{ position: "relative", aspectRatio: "16/9", background: "#000" }}>

        {/* 실시간 HLS — 보정 탭에서도 영상 표시 필요 */}
        <video ref={videoRef} muted playsInline style={{
          width: "100%", height: "100%", objectFit: "contain",
          display: (tab === "live" || tab === "cal") ? "block" : "none",
        }} />

        {/* YOLO 어노테이션 결과 */}
        {tab === "yolo" && (
          annotatedUrl
            ? <img src={annotatedUrl} alt="YOLO" style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }} />
            : <div style={{
                width: "100%", height: "100%", display: "flex", flexDirection: "column",
                alignItems: "center", justifyContent: "center", color: "#475569", fontSize: 13,
              }}>
                <div style={{ fontSize: 32, marginBottom: 10 }}>🤖</div>
                {yoloStatus === "loading" ? "YOLO 모델 로드 중… (최초 1회)" : "영상에서 프레임 캡처 중…"}
              </div>
        )}

        {/* HLS 로딩 */}
        {tab === "live" && hlsLoading && !hlsError && (
          <div style={{
            position: "absolute", inset: 0, display: "flex", flexDirection: "column",
            alignItems: "center", justifyContent: "center",
            background: "rgba(0,0,0,0.75)", color: "#94a3b8", fontSize: 13,
          }}>
            <div style={{ fontSize: 28, marginBottom: 8 }}>⏳</div>스트림 연결 중…
          </div>
        )}

        {/* HLS 에러 */}
        {hlsError && (
          <div style={{
            position: "absolute", inset: 0, display: "flex", flexDirection: "column",
            alignItems: "center", justifyContent: "center",
            background: "rgba(0,0,0,0.88)", color: "#f87171", fontSize: 12, padding: 16, textAlign: "center",
          }}>
            <div style={{ fontSize: 24, marginBottom: 8 }}>⚠️</div>{hlsError}
          </div>
        )}

        {/* ROI 편집 오버레이 */}
        {roiEditing && cctv?.cctvurl && (
          <RoiEditor
            videoRef={videoRef}
            cctvurl={cctv.cctvurl}
            initialRoi={currentRoi}
            onClose={() => { setRoiEditing(false); setTab("live"); }}
            onSaved={(roi) => { setCurrentRoi(roi); setRoiEditing(false); setTab("live"); }}
          />
        )}

        {/* 보정 오버레이 */}
        {calibrating && cctv?.cctvurl && (
          <CalibrationMode
            videoRef={videoRef}
            cctvurl={cctv.cctvurl}
            pendingGps={pendingGps}
            onNeedGps={onNeedGps}
            onCancelGps={onCancelGps}
            onClose={() => { setCalibrating(false); setTab("live"); onCancelGps?.(); }}
            onSaved={() => { setCalibrated(true); setCalibrating(false); setTab("live"); onCancelGps?.(); }}
          />
        )}
      </div>

      {/* 숨김 캔버스 (프레임 캡처 전용) */}
      <canvas ref={canvasRef} style={{ display: "none" }} />
    </div>
  );
}
