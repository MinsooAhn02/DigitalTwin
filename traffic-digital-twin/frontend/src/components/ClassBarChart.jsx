import React, { useMemo } from "react";
import { CLASS_COLORS } from "../utils/colorMap";
import { useLang } from "../i18n/index.jsx";

function toRgb([r, g, b]) { return `rgb(${r},${g},${b})`; }

const ClassBarChart = React.memo(function ClassBarChart({ classCounts = {} }) {
  const { t } = useLang();
  const entries = useMemo(() => Object.entries(classCounts), [classCounts]);
  const total   = useMemo(() => entries.reduce((s, [, n]) => s + n, 0), [entries]);

  if (total === 0) {
    return <p style={{ color: "#6b7280", fontSize: 12, textAlign: "center", padding: "12px 0" }}>{t("chart.noVehicles")}</p>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {entries.map(([cls, cnt]) => {
        const pct = Math.round((cnt / total) * 100);
        const color = toRgb(CLASS_COLORS[cls] ?? CLASS_COLORS.unknown);
        return (
          <div key={cls}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3 }}>
              <span style={{ fontSize: 11, color: "#d1d5db" }}>{t(`class.${cls}`) || cls}</span>
              <span style={{ fontSize: 11, color: "#9ca3af" }}>{t("chart.unit", { n: cnt, pct })}</span>
            </div>
            <div style={{ height: 8, borderRadius: 4, background: "#374151", overflow: "hidden" }}>
              <div style={{ width: `${pct}%`, height: "100%", borderRadius: 4, background: color, transition: "width 0.4s" }} />
            </div>
          </div>
        );
      })}
    </div>
  );
});

export default ClassBarChart;
