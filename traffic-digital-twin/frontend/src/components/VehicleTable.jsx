import React, { useState, useMemo } from "react";
import { useLang } from "../i18n/index.jsx";

const VehicleTable = React.memo(function VehicleTable({ vehicles = [], calibrated = false }) {
  const { t } = useLang();
  const [logOpen, setLogOpen] = useState(false);

  const speeds = useMemo(() => vehicles.map((v) => v.speed_kph).filter((s) => s > 0), [vehicles]);
  const { minSpd, maxSpd, avgSpd } = useMemo(() => ({
    minSpd: speeds.length ? Math.min(...speeds).toFixed(1) : "—",
    maxSpd: speeds.length ? Math.max(...speeds).toFixed(1) : "—",
    avgSpd: speeds.length ? (speeds.reduce((a, b) => a + b, 0) / speeds.length).toFixed(1) : "—",
  }), [speeds]);

  if (vehicles.length === 0) {
    return <p style={{ color: "#6b7280", fontSize: 12, textAlign: "center", padding: "12px 0" }}>{t("chart.noVehicles")}</p>;
  }

  return (
    <div>
      <div>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
          <thead>
            <tr style={{ color: "#6b7280", borderBottom: "1px solid #374151" }}>
              <th style={{ textAlign: "left",   padding: "3px 4px", fontWeight: 600 }}>{t("table.col.id")}</th>
              <th style={{ textAlign: "right",  padding: "3px 4px", fontWeight: 600 }}>{t("table.col.speed")}</th>
              <th style={{ textAlign: "center", padding: "3px 4px", fontWeight: 600 }}>{t("table.col.status")}</th>
              <th style={{ textAlign: "right",  padding: "3px 4px", fontWeight: 600 }}>{t("table.col.dwell")}</th>
            </tr>
          </thead>
          <tbody>
            {vehicles.map((v) => (
              <tr key={v.track_id} style={{ borderBottom: "1px solid #1f2937" }}>
                <td style={{ padding: "4px 4px", color: "#e2e8f0" }}>
                  <span style={{ color: "#94a3b8" }}>#{v.track_id}</span> {v.class_name}
                </td>
                <td style={{ padding: "4px 4px", textAlign: "right", color: v.is_speeding ? "#f87171" : calibrated ? "#d1d5db" : "#fbbf24" }}
                    title={!calibrated ? "캘리브레이션 미설정 — 근사값" : undefined}>
                  {!calibrated && <span style={{ fontSize: 9, marginRight: 1 }}>~</span>}
                  {v.speed_kph?.toFixed(1)} <span style={{ color: "#4b5563" }}>km/h</span>
                </td>
                <td style={{ padding: "4px 4px", textAlign: "center" }}>
                  {v.is_speeding   && <span title={t("table.status.speeding")}   style={{ color: "#f87171", marginRight: 2 }}>⚠</span>}
                  {v.is_bottleneck && <span title={t("table.status.bottleneck")} style={{ color: "#a78bfa" }}>🐢</span>}
                  {v.is_parked     && <span title={t("table.status.parked")}     style={{ color: "#6b7280" }}>🅿</span>}
                </td>
                <td style={{ padding: "4px 4px", textAlign: "right", color: "#6b7280" }}>
                  {v.dwell_frames}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 6 }}>
        <button
          onClick={() => setLogOpen((o) => !o)}
          style={{
            width: "100%", background: "none", border: "1px solid #374151",
            borderRadius: 6, padding: "4px 0", color: "#64748b",
            fontSize: 11, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 4,
          }}
        >
          {t("table.speedLog")} {logOpen ? "▲" : "▼"}
        </button>

        {logOpen && (
          <div style={{
            marginTop: 6, padding: "8px 12px", background: "#1e293b",
            borderRadius: 8, display: "flex", justifyContent: "space-around", fontSize: 12,
          }}>
            {[["min", minSpd], ["avg", avgSpd], ["max", maxSpd]].map(([label, val]) => (
              <div key={label} style={{ textAlign: "center" }}>
                <div style={{ color: "#64748b", fontSize: 10, marginBottom: 2 }}>{label}</div>
                <div style={{ color: label === "max" ? "#f87171" : label === "min" ? "#34d399" : "#94a3b8", fontWeight: 700, fontSize: 14 }}>
                  {val}
                </div>
                {val !== "—" && <div style={{ color: "#4b5563", fontSize: 10 }}>km/h</div>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
});

export default VehicleTable;
