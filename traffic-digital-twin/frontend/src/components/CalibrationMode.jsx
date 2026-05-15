import { useRef, useEffect, useState, useCallback } from "react";

/**
 * CalibrationMode — 4-point pixel→GPS 보정 UI
 *
 * 동작 흐름:
 *  홀수 단계 (0,2,4,6): 영상에서 픽셀 좌표 클릭
 *  짝수 단계 (1,3,5,7): 지도에서 GPS 좌표 클릭 (App.jsx가 pendingGps로 전달)
 *
 * props:
 *  videoRef         — <video> ref
 *  cctvurl          — 현재 카메라 URL
 *  pendingGps       — App.jsx가 지도 클릭으로 넘겨준 {lat,lon} | null
 *  onNeedGps(n)     — 지도 클릭 모드 요청 (n번째 점 표시용)
 *  onCancelGps()    — 지도 클릭 모드 해제
 *  onClose()        — 보정 취소
 *  onSaved()        — 보정 완료
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
}) {
  const canvasRef = useRef(null);
  // pairs: [{pixel:[u,v], gps:[lat,lon]|null}, ...]
  const [pairs, setPairs]   = useState([]);
  const [step, setStep]     = useState(0);
  const stepRef             = useRef(0);
  const [saving, setSaving] = useState(false);
  const [error, setError]   = useState(null);

  // stepRef를 step과 동기화 (stale closure 방지)
  useEffect(() => { stepRef.current = step; }, [step]);

  // ── 캔버스에 현재 points 그리기 ─────────────────────────────────────
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

  // ── 지도 GPS 좌표 수신 ────────────────────────────────────────────────
  useEffect(() => {
    if (!pendingGps) return;
    const cur = stepRef.current;
    if (cur % 2 !== 1) return; // GPS 입력 단계가 아니면 무시

    const pairIdx = Math.floor(cur / 2);
    setPairs((prev) => {
      const next = [...prev];
      next[pairIdx] = { ...next[pairIdx], gps: [pendingGps.lat, pendingGps.lon] };
      return next;
    });
    onCancelGps();
    setStep(cur + 1);
  }, [pendingGps, onCancelGps]);

  // ── 영상 클릭: 픽셀 좌표 기록 ────────────────────────────────────────
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

    // letterbox 영역 안인지 확인
    if (cx < r.ox || cx > r.ox + r.rw || cy < r.oy || cy > r.oy + r.rh) return;

    const u = Math.round(((cx - r.ox) / r.rw) * r.vw);
    const v = Math.round(((cy - r.oy) / r.rh) * r.vh);

    const pairIdx = Math.floor(cur / 2);
    setPairs((prev) => {
      const next = [...prev];
      next[pairIdx] = { pixel: [u, v], gps: null };
      return next;
    });
    setStep(cur + 1);
    onNeedGps(pairIdx + 1);
  }, [videoRef, onNeedGps]);

  const handleReset = () => {
    setPairs([]);
    setStep(0);
    setError(null);
    onCancelGps();
  };

  const handleSave = async () => {
    if (pairs.length < 4 || pairs.some((p) => !p.gps)) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch("http://localhost:8000/calibration", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cctvurl,
          pixel_pts: pairs.map((p) => p.pixel),
          gps_pts:   pairs.map((p) => p.gps),
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      onSaved();
    } catch (err) {
      setError(err.message ?? "저장 실패");
    } finally {
      setSaving(false);
    }
  };

  const pairIdx     = Math.floor(step / 2);
  const isPixelStep = step % 2 === 0 && step < 8;
  const isGpsStep   = step % 2 === 1 && step < 8;
  const isDone      = pairs.length === 4 && pairs.every((p) => p.gps);

  return (
    <div style={{ position: "absolute", inset: 0, zIndex: 10 }}>

      {/* 클릭 가능 캔버스 — 전체 영역 커버 (RoiEditor와 동일 패턴) */}
      <canvas
        ref={canvasRef}
        onClick={handleCanvasClick}
        style={{
          position: "absolute", inset: 0,
          width: "100%", height: "100%",
          cursor: isPixelStep ? "crosshair" : "default",
        }}
      />

      {/* 안내 배너 — 상단 absolute overlay */}
      <div style={{
        position: "absolute", top: 0, left: 0, right: 0,
        background: isGpsStep ? "rgba(30,58,138,0.95)" : "rgba(15,23,42,0.88)",
        padding: "7px 12px",
        display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
        borderBottom: "1px solid #334155",
      }}>
        <span style={{ fontSize: 12, color: "#e2e8f0", fontWeight: 600 }}>
          🔧 카메라 보정 ({Math.min(pairIdx + 1, 4)}/4)
        </span>
        {!isDone && (
          <span style={{ fontSize: 11, color: COLORS[Math.min(pairIdx, 3)] }}>
            {isPixelStep && `▶ 영상에서 ${pairIdx + 1}번 점 클릭`}
            {isGpsStep   && `▶ 지도에서 동일 지점 클릭`}
          </span>
        )}
        {isDone && <span style={{ fontSize: 11, color: "#34d399" }}>✔ 4쌍 완료</span>}
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button onClick={handleReset} disabled={saving} style={btn("#475569")}>초기화</button>
          <button onClick={handleSave} disabled={!isDone || saving} style={btn(isDone && !saving ? "#0ea5e9" : "#1e293b")}>
            {saving ? "저장 중…" : "저장"}
          </button>
          <button onClick={() => { onCancelGps(); onClose(); }} disabled={saving} style={btn("#6b7280")}>✕</button>
        </div>
      </div>

      {/* 수집된 점 목록 — 우측 상단 */}
      <div style={{
        position: "absolute", top: 44, right: 8,
        background: "rgba(15,23,42,0.85)", borderRadius: 8, padding: "8px 12px",
        fontSize: 11, color: "#94a3b8", minWidth: 170,
      }}>
        {[0, 1, 2, 3].map((i) => {
          const p = pairs[i];
          return (
            <div key={i} style={{ marginBottom: 4, display: "flex", gap: 6, alignItems: "center" }}>
              <span style={{ color: COLORS[i], fontWeight: 700 }}>●{i + 1}</span>
              {p ? (
                <>
                  <span style={{ color: "#e2e8f0" }}>({p.pixel[0]}, {p.pixel[1]})</span>
                  {p.gps
                    ? <span style={{ color: "#34d399" }}>GPS ✔</span>
                    : <span style={{ color: "#fbbf24" }}>지도 대기…</span>
                  }
                </>
              ) : (
                <span style={{ color: "#475569" }}>미설정</span>
              )}
            </div>
          );
        })}
      </div>

      {/* 에러 배너 — 하단 */}
      {error && (
        <div style={{
          position: "absolute", bottom: 0, left: 0, right: 0,
          padding: "6px 14px", background: "#7f1d1d", color: "#fca5a5", fontSize: 12,
        }}>
          ⚠ {error}
        </div>
      )}
    </div>
  );
}

function btn(bg) {
  return {
    background: bg, border: "none", borderRadius: 6,
    padding: "4px 10px", color: "#f1f5f9", fontSize: 11,
    cursor: "pointer", fontWeight: 600,
  };
}
