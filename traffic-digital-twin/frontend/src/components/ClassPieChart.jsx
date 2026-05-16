import { PieChart, Pie, Cell, Tooltip, Legend, ResponsiveContainer } from "recharts";
import { CLASS_COLORS } from "../utils/colorMap";
import { useLang } from "../i18n/index.jsx";

function toHex([r, g, b]) {
  return `rgb(${r},${g},${b})`;
}

export default function ClassPieChart({ classCounts = {} }) {
  const { t } = useLang();
  const data  = Object.entries(classCounts).map(([name, value]) => ({ name, value }));

  if (data.length === 0) {
    return <p className="text-gray-500 text-sm text-center py-8">{t("chart.noVehicles")}</p>;
  }

  return (
    <ResponsiveContainer width="100%" height={200}>
      <PieChart>
        <Pie
          data={data}
          dataKey="value"
          nameKey="name"
          cx="50%" cy="50%"
          innerRadius={50}
          outerRadius={80}
          paddingAngle={3}
        >
          {data.map((entry) => (
            <Cell
              key={entry.name}
              fill={toHex(CLASS_COLORS[entry.name] ?? CLASS_COLORS.unknown)}
            />
          ))}
        </Pie>
        <Tooltip
          contentStyle={{ background: "#1f2937", border: "none", borderRadius: 8 }}
          labelStyle={{ color: "#f9fafb" }}
        />
        <Legend
          formatter={(value) => (
            <span style={{ color: "#d1d5db", fontSize: 12 }}>{value}</span>
          )}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}
