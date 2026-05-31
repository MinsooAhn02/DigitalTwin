/**
 * HistoryPanel.jsx — 히스토리 시계열 분석 패널 ([C])
 *
 * - 카메라 선택 + 기간 선택 → /history/series fetch
 * - Recharts LineChart 2개: 시간대별 차량 수 / 평균 속도
 * - 피크타임 강조 (ReferenceLine) + CSV 내보내기
 */

import { useEffect, useMemo, useState } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, Legend,
} from "recharts";

const API_BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

// 기간 옵션: 라벨키 → { hours, bucket_s }
const RANGES = {
  "hist.range.6h":  { hours: 6,   bucket_s: 300 },    // 5분 버킷
  "hist.range.24h": { hours: 24,  bucket_s: 900 },    // 15분 버킷
  "hist.range.7d":  { hours: 168, bucket_s: 3600 },   // 1시간 버킷
};

function fmtTime(ts, hours) {
  const d = new Date(ts * 1000);
  if (hours > 48) {
    return `${d.getMonth() + 1}/${d.getDate()}`;
  }
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export default function HistoryPanel({ lang, t }) {
  const [cameras, setCameras]   = useState([]);
  const [camKey, setCamKey]     = useState("");
  const [rangeKey, setRangeKey] = useState("hist.range.24h");
  const [series, setSeries]     = useState([]);
  const [peak, setPeak]         = useState(null);
  const [loading, setLoading]   = useState(false);

  const range = RANGES[rangeKey];

  // 카메라 목록 (탭 진입 시 + 주기 갱신)
  useEffect(() => {
    let alive = true;
    const load = () => {
      fetch(`${API_BASE}/history/cameras`)
        .then((r) => r.json())
        .then((list) => {
          if (!alive) return;
          setCameras(list ?? []);
          setCamKey((prev) => prev || (list?.[0]?.cam_key ?? ""));
        })
        .catch(() => {});
    };
    load();
    const timer = setInterval(load, 30000);
    return () => { alive = false; clearInterval(timer); };
  }, []);

  // 시계열 + 피크 (카메라/기간 변경 시 + 주기 갱신)
  useEffect(() => {
    if (!camKey) { setSeries([]); setPeak(null); return; }
    let alive = true;
    const load = () => {
      setLoading(true);
      Promise.all([
        fetch(`${API_BASE}/history/series?cam_key=${encodeURIComponent(camKey)}&hours=${range.hours}&bucket_s=${range.bucket_s}`).then((r) => r.json()),
        fetch(`${API_BASE}/history/peak?cam_key=${encodeURIComponent(camKey)}&hours=${range.hours}`).then((r) => r.json()),
      ])
        .then(([s, p]) => {
          if (!alive) return;
          setSeries(Array.isArray(s) ? s : []);
          setPeak(p && p.ts ? p : null);
        })
        .catch(() => {})
        .finally(() => { if (alive) setLoading(false); });
    };
    load();
    const timer = setInterval(load, 30000);
    return () => { alive = false; clearInterval(timer); };
  }, [camKey, rangeKey]);

  const chartData = useMemo(
    () => series.map((d) => ({ ...d, label: fmtTime(d.ts, range.hours) })),
    [series, range.hours]
  );

  const hasSpeed = useMemo(
    () => chartData.some((d) => d.avg_speed_kph != null),
    [chartData]
  );

  const peakLabel = peak ? fmtTime(peak.ts, range.hours) : null;
  const camName = (c) => (lang === "en"
    ? (c.name || `CCTV ${c.cam_key.slice(0, 6)}`)
    : (c.name_ko || c.name || c.cam_key));

  const csvHref = camKey
    ? `${API_BASE}/history/export.csv?cam_key=${encodeURIComponent(camKey)}&hours=${range.hours}`
    : "#";

  return (
    <div style={{ background: "#1f2937", borderRadius: 12, padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
      {/* 컨트롤 */}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <select
          value={camKey}
          onChange={(e) => setCamKey(e.target.value)}
          style={selectStyle}
        >
          {cameras.length === 0 && <option value="">{t("hist.noCamera")}</option>}
          {cameras.map((c) => (
            <option key={c.cam_key} value={c.cam_key}>{camName(c)}</option>
          ))}
        </select>

        <div style={{ display: "flex", gap: 4 }}>
          {Object.keys(RANGES).map((rk) => (
            <button key={rk} onClick={() => setRangeKey(rk)} style={rangeBtn(rangeKey === rk)}>
              {t(rk)}
            </button>
          ))}
          <a
            href={csvHref}
            style={{ ...rangeBtn(false), marginLeft: "auto", textDecoration: "none", color: "#60a5fa", pointerEvents: camKey ? "auto" : "none", opacity: camKey ? 1 : 0.4 }}
          >
            {t("hist.exportCsv")}
          </a>
        </div>
      </div>

      {/* 피크 배지 */}
      {peak && (
        <div style={{ fontSize: 11, color: "#fbbf24", fontWeight: 600 }}>
          ⛰ {t("hist.peakAt", { n: peak.vehicle_count, time: peakLabel })}
        </div>
      )}

      {chartData.length === 0 ? (
        <div style={{ fontSize: 11, color: "#6b7280", textAlign: "center", padding: "20px 0" }}>
          {loading ? t("hist.loading") : t("hist.empty")}
        </div>
      ) : (
        <>
          {/* 차량 수 — 평균 + 최댓값 */}
          <ChartBlock label={t("hist.vehicleCount")}>
            <LineChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: -24 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="label" tick={{ fontSize: 9, fill: "#9ca3af" }} interval="preserveStartEnd" />
              <YAxis tick={{ fontSize: 9, fill: "#9ca3af" }} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} />
              <Legend wrapperStyle={{ fontSize: 10 }} iconSize={8} />
              {peakLabel && <ReferenceLine x={peakLabel} stroke="#fbbf24" strokeDasharray="4 2" />}
              <Line name={t("hist.peak")} type="monotone" dataKey="peak_count" stroke="#f472b6" strokeWidth={1.5} strokeDasharray="4 2" dot={false} isAnimationActive={false} />
              <Line name={t("hist.avg")} type="monotone" dataKey="vehicle_count" stroke="#60a5fa" strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ChartBlock>

          {/* 평균 속도 (live 기록이 있을 때만) */}
          {hasSpeed && (
            <ChartBlock label={t("hist.avgSpeed")}>
              <LineChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: -24 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis dataKey="label" tick={{ fontSize: 9, fill: "#9ca3af" }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 9, fill: "#9ca3af" }} />
                <Tooltip contentStyle={tooltipStyle} />
                <Line type="monotone" dataKey="avg_speed_kph" stroke="#f97316" strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
              </LineChart>
            </ChartBlock>
          )}
        </>
      )}
    </div>
  );
}

function ChartBlock({ label, children }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "#9ca3af", marginBottom: 4 }}>{label}</div>
      <ResponsiveContainer width="100%" height={140}>
        {children}
      </ResponsiveContainer>
    </div>
  );
}

const selectStyle = {
  width: "100%", boxSizing: "border-box",
  background: "#111827", border: "1px solid #374151", borderRadius: 6,
  padding: "6px 10px", fontSize: 12, color: "#f9fafb", outline: "none",
};

const rangeBtn = (active) => ({
  padding: "4px 10px", fontSize: 11, border: "none", borderRadius: 4, cursor: "pointer",
  background: active ? "#3b82f6" : "#111827",
  color: active ? "#fff" : "#9ca3af",
});

const tooltipStyle = {
  background: "#111827", border: "1px solid #374151", borderRadius: 6,
  fontSize: 11, color: "#f9fafb",
};
