import React from "react";
import { useLang } from "../i18n/index.jsx";

const CounterPanel = React.memo(function CounterPanel({ inCount = 0, outCount = 0, vehicleCount = 0 }) {
  const { t } = useLang();
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
      <Stat label={t("counter.current")} value={vehicleCount} color="#60a5fa" />
      <Stat label={t("counter.in")}      value={inCount}      color="#34d399" />
      <Stat label={t("counter.out")}     value={outCount}     color="#fb923c" />
    </div>
  );
});

export default CounterPanel;

function Stat({ label, value, color }) {
  return (
    <div style={{ background: "#1f2937", borderRadius: 12, padding: "12px 8px", textAlign: "center" }}>
      <p style={{ margin: 0, fontSize: 24, fontWeight: 700, color }}>{value.toLocaleString()}</p>
      <p style={{ margin: "4px 0 0", fontSize: 11, color: "#9ca3af" }}>{label}</p>
    </div>
  );
}
