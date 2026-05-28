/**
 * RoiEditor — canvas-only ROI polygon editor.
 * Controls are rendered by the parent via onStateChange.
 * `hint` in state is a translation key, resolved by the parent RoiBar.
 */

import { useRef, useState, useEffect, useCallback } from "react";

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

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

export default function RoiEditor({ videoRef, cctvurl, initialRoi, onClose, onSaved, onStateChange }) {
  const canvasRef = useRef(null);
  const [points, setPoints] = useState(initialRoi ?? []);
  const [closed, setClosed] = useState((initialRoi?.length ?? 0) >= 3);
  const [saving, setSaving] = useState(false);
  // hint stores a translation key (e.g. "roi.hint.start") — resolved by parent
  const [hint, setHint] = useState("roi.hint.start");

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

    ctx.beginPath();
    ctx.moveTo(pxPts[0][0], pxPts[0][1]);
    pxPts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
    if (closed) ctx.closePath();
    ctx.fillStyle = "rgba(0,220,100,0.22)";
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(pxPts[0][0], pxPts[0][1]);
    pxPts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
    if (closed) ctx.closePath();
    ctx.strokeStyle = "#22c55e";
    ctx.lineWidth = 2;
    ctx.setLineDash(closed ? [] : [6, 3]);
    ctx.stroke();
    ctx.setLineDash([]);

    pxPts.forEach(([x, y], i) => {
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fillStyle = i === 0 ? "#22c55e" : "#fff";
      ctx.fill();
      ctx.strokeStyle = "#22c55e";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    });
  }, [points, closed, videoRef]);

  useEffect(() => { draw(); }, [draw]);

  useEffect(() => {
    const obs = new ResizeObserver(draw);
    if (canvasRef.current?.parentElement) obs.observe(canvasRef.current.parentElement);
    return () => obs.disconnect();
  }, [draw]);

  const handleReset = useCallback(() => {
    setPoints([]);
    setClosed(false);
    setHint("roi.hint.reset");
  }, []);

  const handleSave = useCallback(async () => {
    if (points.length < 3) return;
    setSaving(true);
    try {
      const res = await fetch(`${API}/roi`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cctvurl, polygon: points }),
      });
      if (res.ok) { onSaved?.(points); onClose?.(); }
    } catch {
      setHint("roi.hint.error");
    } finally {
      setSaving(false);
    }
  }, [points, cctvurl, onSaved, onClose]);

  useEffect(() => {
    onStateChange?.({
      points, closed, hint, saving,
      actions: { reset: handleReset, save: handleSave, close: onClose },
    });
  }, [points, closed, hint, saving, handleReset, handleSave, onClose]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleClick = (e) => {
    if (closed) return;
    const canvas = canvasRef.current;
    const video  = videoRef.current;
    if (!canvas || !video) return;
    const rect   = canvas.getBoundingClientRect();
    const videoR = getVideoRect(video);
    if (!videoR) return;
    const px   = [e.clientX - rect.left, e.clientY - rect.top];
    const norm = toNorm(px, videoR);
    setPoints((prev) => [...prev, norm]);
    if (points.length >= 2) setHint("roi.hint.addMore");
  };

  const handleDblClick = (e) => {
    e.preventDefault();
    if (points.length < 3) { setHint("roi.hint.tooFew"); return; }
    setClosed(true);
    setHint("roi.hint.ready");
  };

  return (
    <canvas
      ref={canvasRef}
      style={{ position: "absolute", inset: 0, width: "100%", height: "100%", cursor: closed ? "default" : "crosshair" }}
      onClick={handleClick}
      onDoubleClick={handleDblClick}
    />
  );
}
