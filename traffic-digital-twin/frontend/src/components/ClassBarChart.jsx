import React, { useMemo } from "react";
import { CLASS_COLORS } from "../utils/colorMap";
import { useLang } from "../i18n/index.jsx";

function toRgb([r, g, b]) { return `rgb(${r},${g},${b})`; }

// Always show these YOLO-detectable classes even when count is 0
const DEFAULT_CLASSES = ["car", "truck", "bus", "motorcycle"];

const ClassBarChart = React.memo(function ClassBarChart({ classCounts = {} }) {
  const { t } = useLang();

  // Merge default classes (always shown) with any additional detected classes
  const entries = useMemo(() => {
    const merged = { ...classCounts };
    DEFAULT_CLASSES.forEach((cls) => {
      if (!(cls in merged)) merged[cls] = 0;
    });
    // Default classes first, then any extras (e.g. unknown)
    const keys = [
      ...DEFAULT_CLASSES,
      ...Object.keys(merged).filter((k) => !DEFAULT_CLASSES.includes(k)),
    ];
    return keys.map((k) => [k, merged[k] ?? 0]);
  }, [classCounts]);

  const total = useMemo(() => entries.reduce((s, [, n]) => s + n, 0), [entries]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {entries.map(([cls, cnt]) => {
        const pct = total > 0 ? Math.round((cnt / total) * 100) : 0;
        const color = toRgb(CLASS_COLORS[cls] ?? CLASS_COLORS.unknown);
        return (
          <div key={cls}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3 }}>
              <span style={{ fontSize: 11, color: cnt > 0 ? "#d1d5db" : "#4b5563" }}>{t(`class.${cls}`) || cls}</span>
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
