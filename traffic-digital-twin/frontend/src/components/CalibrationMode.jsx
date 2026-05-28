import { useRef, useEffect, useState, useCallback } from "react";

const API_BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

/**
 * CalibrationMode — 4-point pixel→GPS 보정 UI
 *
 * 캔버스 오버레이만 렌더링. 컨트롤 UI는 onStateChange를 통해
 * 부모(CctvPlayer)에서 영상 밖에 렌더링됨.
 */

function getVideoRect(video) {
  if (!video) return null;
  const cw = video.parentElement?.clientWidth  || video.clientWidth;
  const ch = video.parentElement?.clientHeight || video.clientHeight;
  const vw = video.videoWidth  || cw;
  const vh = video.videoHeight || ch;
  const videoAR = vw / vh;
  const containerAR = cw / ch;

  let rw, rh, ox, oy;
  if (videoAR > containerAR) {
    rw = cw; rh = cw / videoAR;
    ox = 0;  oy = (ch - rh) / 2;
  } else {
    rh = ch; rw = ch * videoAR;
    ox = (cw - rw) / 2; oy = 0;
  }
  return { rw, rh, ox, oy, cw, ch, vw, vh };
}

const COLORS = ["#f87171", "#fb923c", "#facc15", "#34d399"];

export default function CalibrationMode({
  videoRef,
  cctvurl,
  pendingGps,
  onNeedGps,
  onCancelGps,
  onClose,
  onSaved,
  onStateChange,  // (state) → 부모에서 컨트롤 UI 렌더링용
}) {
  const canvasRef = useRef(null);
  const [pairs, setPairs]   = useState([]);
  const [step, setStep]     = useState(0);
  const stepRef             = useRef(0);
  const [saving, setSaving] = useState(false);
  const [error, setError]   = useState(null);

  useEffect(() => { stepRef.current = step; }, [step]);

  const pairIdx     = Math.floor(step / 2);
  const isPixelStep = step % 2 === 0 && step < 8;
  const isGpsStep   = step % 2 === 1 && step < 8;
  const isDone      = pairs.length === 4 && pairs.every((p) => p.gps);

  // 상태 변경 시 부모에게 전달
  useEffect(() => {
    onStateChange?.({ pairs, step, pairIdx, isPixelStep, isGpsStep, isDone, saving, error });
  }, [pairs, step, saving, error]); // eslint-disable-line react-hooks/exhaustive-deps

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const video  = videoRef.current;
    if (!canvas || !video) return;
    const r = getVideoRect(video);
    if (!r) return;
    canvas.width  = r.cw;
    canvas.height = r.ch;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, r.cw, r.ch);

    pairs.forEach(({ pixel }, i) => {
      const cx = r.ox + (pixel[0] / r.vw) * r.rw;
      const cy = r.oy + (pixel[1] / r.vh) * r.rh;
      ctx.beginPath();
      ctx.arc(cx, cy, 9, 0, Math.PI * 2);
      ctx.fillStyle = COLORS[i] + "cc";
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.fillStyle = "#fff";
      ctx.font = "bold 11px system-ui";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(i + 1, cx, cy);
    });
  }, [pairs, videoRef]);

  useEffect(() => { draw(); }, [draw]);

  useEffect(() => {
    const obs = new ResizeObserver(draw);
    if (canvasRef.current?.parentElement) obs.observe(canvasRef.current.parentElement);
    return () => obs.disconnect();
  }, [draw]);

  useEffect(() => {
    if (!pendingGps) return;
    const cur = stepRef.current;
    if (cur % 2 !== 1) return;
    const idx = Math.floor(cur / 2);
    setPairs((prev) => {
      const next = [...prev];
      next[idx] = { ...next[idx], gps: [pendingGps.lat, pendingGps.lon] };
      return next;
    });
    onCancelGps();
    setStep(cur + 1);
  }, [pendingGps, onCancelGps]);

  const handleCanvasClick = useCallback((e) => {
    const cur = stepRef.current;
    if (cur % 2 !== 0 || cur >= 8) return;
    const canvas = canvasRef.current;
    const video  = videoRef.current;
    if (!canvas || !video) return;
    const r = getVideoRect(video);
    if (!r) return;
    const rect = canvas.getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    if (cx < r.ox || cx > r.ox + r.rw || cy < r.oy || cy > r.oy + r.rh) return;
    const u = Math.round(((cx - r.ox) / r.rw) * r.vw);
    const v = Math.round(((cy - r.oy) / r.rh) * r.vh);
    const idx = Math.floor(cur / 2);
    setPairs((prev) => {
      const next = [...prev];
      next[idx] = { pixel: [u, v], gps: null };
      return next;
    });
    setStep(cur + 1);
    onNeedGps(idx + 1);
  }, [videoRef, onNeedGps]);

  // 부모에서 호출할 수 있는 액션을 ref로 노출
  const handleReset = useCallback(() => {
    setPairs([]); setStep(0); setError(null); onCancelGps();
  }, [onCancelGps]);

  const handleSave = useCallback(async () => {
    if (pairs.length < 4 || pairs.some((p) => !p.gps)) return;
    setSaving(true);
    setError(null);
    try {
      const video = videoRef.current;
      const frameWidth  = video?.videoWidth  || 640;
      const frameHeight = video?.videoHeight || 360;
      const res = await fetch(`${API_BASE}/calibration`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cctvurl,
          pixel_pts:    pairs.map((p) => p.pixel),
          gps_pts:      pairs.map((p) => p.gps),
          frame_width:  frameWidth,
          frame_height: frameHeight,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const result = await res.json();

      const [lat1, lon1] = pairs[0].gps;
      const [lat2, lon2] = pairs[3].gps;
      const rawBearing = Math.atan2(lon2 - lon1, lat2 - lat1) * 180 / Math.PI;
      const heading = (rawBearing + 360) % 360;

      // 사용자가 직접 찍은 GPS 4점 → 항상 유효한 폴리곤 (homography 코너 계산 사용 안 함)
      // 중심 기준 각도 정렬로 convex 순서 보장
      const lonLatPts = pairs.map((p) => [p.gps[1], p.gps[0]]); // [lon, lat]
      const cx = lonLatPts.reduce((s, p) => s + p[0], 0) / lonLatPts.length;
      const cy = lonLatPts.reduce((s, p) => s + p[1], 0) / lonLatPts.length;
      const sorted = [...lonLatPts].sort((a, b) =>
        Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx)
      );
      const gpsRing = [...sorted, sorted[0]];
      onSaved(heading, gpsRing);
    } catch (err) {
      setError(err.message ?? "저장 실패");
    } finally {
      setSaving(false);
    }
  }, [pairs, cctvurl, videoRef, onSaved]);

  // 부모가 reset/save를 호출할 수 있도록 onStateChange에 actions 포함
  useEffect(() => {
    onStateChange?.({
      pairs, step, pairIdx, isPixelStep, isGpsStep, isDone, saving, error,
      actions: { reset: handleReset, save: handleSave, close: () => { onCancelGps(); onClose(); } },
    });
  }, [pairs, step, saving, error, handleReset, handleSave]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <canvas
      ref={canvasRef}
      onClick={handleCanvasClick}
      style={{
        position: "absolute", inset: 0,
        width: "100%", height: "100%",
        cursor: isPixelStep ? "crosshair" : "default",
      }}
    />
  );
}

export const CALIB_COLORS = COLORS;
