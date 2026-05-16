import { RadialBarChart, RadialBar, ResponsiveContainer } from "recharts";
import { useLang } from "../i18n/index.jsx";

const MAX_SPEED = 120;

export default function SpeedGauge({ avgSpeed = 0 }) {
  const { t } = useLang();
  const pct   = Math.min(avgSpeed / MAX_SPEED, 1);
  const color = avgSpeed > 100 ? "#ef4444" : avgSpeed > 70 ? "#f59e0b" : "#10b981";

  return (
    <div className="flex flex-col items-center">
      <ResponsiveContainer width={140} height={140}>
        <RadialBarChart
          cx="50%" cy="50%"
          innerRadius="70%" outerRadius="100%"
          startAngle={225} endAngle={-45}
          data={[{ value: pct * 100, fill: color }]}
        >
          <RadialBar dataKey="value" cornerRadius={6} background={{ fill: "#374151" }} />
        </RadialBarChart>
      </ResponsiveContainer>
      <p className="text-2xl font-bold -mt-10" style={{ color }}>
        {avgSpeed.toFixed(0)}
        <span className="text-sm font-normal text-gray-400"> km/h</span>
      </p>
      <p className="text-xs text-gray-500 mt-1">{t("gauge.avgSpeed")}</p>
    </div>
  );
}
