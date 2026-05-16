/**
 * RoiEditor.jsx — 영상 위 ROI polygon 편집 오버레이
 *
 * Props:
 *   videoRef     : <video> ref (좌표 변환에 사용)
 *   cctvurl      : 카메라 URL (저장 키)
 *   initialRoi   : 초기 polygon (정규화 좌표 [[x,y], ...])
 *   onClose      : 편집 종료 콜백
 *   onSaved      : 저장 완료 후 콜백(newRoi)
 */

import { useRef, useState, useEffect, useCallback } from "react";

const API = "http://localhost:8000";

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
    rw = cw;
    rh = cw / videoAR;
    ox = 0;
    oy = (ch - rh) / 2;
  } else {
    rh = ch;
    rw = ch * videoAR;
    ox = (cw - rw) / 2;
    oy = 0;
  }
  return { rw, rh, ox, oy, cw, ch };
}

function toNorm(px, rect) {
  return [
    Math.max(0, Math.min(1, (px[0] - rect.ox) / rect.rw)),
    Math.max(0, Math.min(1, (px[1] - rect.oy) / rect.rh)),
  ];
}

function toCanvas(norm, rect) {
  return [norm[0] * rect.rw + rect.ox, norm[1] * rect.rh + rect.oy];
}

export default function RoiEditor({ videoRef, cctvurl, initialRoi, onClose, onSaved }) {
  const canvasRef   = useRef(null);
  const [points, setPoints] = useState(initialRoi ?? []);
  const [closed, setClosed] = useState((initialRoi?.length ?? 0) >= 3);
  const [saving, setSaving] = useState(false);
  const [hint, setHint]     = useState("초록 영역 내 차량만 감지됩니다 · 클릭으로 꼭짓점 추가, 더블클릭으로 완성");

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const video  = videoRef.current;
    if (!canvas || !video) return;

    const rect = getVideoRect(video);
    if (!rect) return;
    canvas.width  = rect.cw;
    canvas.height = rect.ch;

    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, rect.cw, rect.ch);

    if (points.length === 0) return;

    const pxPts = points.map((n) => toCanvas(n, rect));

    // 반투명 채우기 (초록 = 감지 포함 영역)
    ctx.beginPath();
    ctx.moveTo(pxPts[0][0], pxPts[0][1]);
    pxPts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
    if (closed) ctx.closePath();
    ctx.fillStyle = "rgba(0,220,100,0.22)";
    ctx.fill();

    // 테두리
    ctx.beginPath();
    ctx.moveTo(pxPts[0][0], pxPts[0][1]);
    pxPts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
    if (closed) ctx.closePath();
    ctx.strokeStyle = "#22c55e";
    ctx.lineWidth = 2;
    ctx.setLineDash(closed ? [] : [6, 3]);
    ctx.stroke();
    ctx.setLineDash([]);

    // 꼭짓점
    pxPts.forEach(([x, y], i) => {
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fillStyle = i === 0 ? "#22c55e" : "#fff";
      ctx.fill();
      ctx.strokeStyle = "#22c55e";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    });
  }, [points, closed]);

  useEffect(() => {
    draw();
  }, [draw]);

  // 캔버스 리사이즈 시 재그리기
  useEffect(() => {
    const obs = new ResizeObserver(draw);
    if (canvasRef.current?.parentElement) obs.observe(canvasRef.current.parentElement);
    return () => obs.disconnect();
  }, [draw]);

  const handleClick = (e) => {
    if (closed) return;
    const canvas = canvasRef.current;
    const video  = videoRef.current;
    if (!canvas || !video) return;
    const rect    = canvas.getBoundingClientRect();
    const videoR  = getVideoRect(video);
    if (!videoR) return;
    const px      = [e.clientX - rect.left, e.clientY - rect.top];
    const norm    = toNorm(px, videoR);
    setPoints((prev) => [...prev, norm]);
    if (points.length >= 2) setHint("더블클릭으로 polygon 완성 / 계속 클릭해서 꼭짓점 추가");
  };

  const handleDblClick = (e) => {
    e.preventDefault();
    if (points.length < 3) {
      setHint("꼭짓점을 3개 이상 찍어야 합니다");
      return;
    }
    setClosed(true);
    setHint("저장 버튼을 눌러 적용하세요");
  };

  const handleSave = async () => {
    if (points.length < 3) return;
    setSaving(true);
    try {
      const res = await fetch(`${API}/roi`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cctvurl, polygon: points }),
      });
      if (res.ok) {
        onSaved?.(points);
        onClose?.();
      }
    } catch {
      setHint("저장 실패 — 서버 연결을 확인하세요");
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setPoints([]);
    setClosed(false);
    setHint("클릭으로 꼭짓점 추가, 더블클릭으로 완성");
  };

  return (
    <div style={{ position: "absolute", inset: 0, zIndex: 10 }}>
      {/* 클릭 가능 캔버스 */}
      <canvas
        ref={canvasRef}
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%", cursor: closed ? "default" : "crosshair" }}
        onClick={handleClick}
        onDoubleClick={handleDblClick}
      />

      {/* 컨트롤 바 */}
      <div style={{
        position: "absolute", bottom: 8, left: "50%", transform: "translateX(-50%)",
        display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", justifyContent: "center",
        background: "rgba(15,23,42,0.88)", borderRadius: 8, padding: "6px 12px",
        border: "1px solid #1e3a5f", maxWidth: "90%",
      }}>
        <span style={{ fontSize: 11, color: "#94a3b8" }}>{hint}</span>
        <button
          onClick={handleReset}
          style={{ fontSize: 11, padding: "4px 10px", borderRadius: 6, border: "1px solid #374151", background: "#1e293b", color: "#e2e8f0", cursor: "pointer" }}
        >초기화</button>
        <button
          onClick={handleSave}
          disabled={points.length < 3 || saving}
          style={{
            fontSize: 11, padding: "4px 10px", borderRadius: 6, border: "none",
            background: points.length >= 3 ? "#0369a1" : "#374151",
            color: points.length >= 3 ? "#fff" : "#6b7280",
            cursor: points.length >= 3 ? "pointer" : "default",
          }}
        >{saving ? "저장 중…" : "✓ 저장 (포함 영역)"}</button>
        <button
          onClick={onClose}
          style={{ fontSize: 11, padding: "4px 10px", borderRadius: 6, border: "1px solid #374151", background: "#1e293b", color: "#94a3b8", cursor: "pointer" }}
        >취소</button>
      </div>

      {/* 꼭짓점 수 표시 */}
      <div style={{
        position: "absolute", top: 8, right: 8,
        fontSize: 11, color: "#94a3b8",
        background: "rgba(15,23,42,0.75)", borderRadius: 6, padding: "3px 8px",
      }}>
        꼭짓점 {points.length}개{closed ? " (완성)" : ""}
      </div>
    </div>
  );
}
