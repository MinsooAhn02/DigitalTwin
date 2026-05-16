import { useLang } from "../i18n/index.jsx";

export default function CounterPanel({ inCount = 0, outCount = 0, vehicleCount = 0 }) {
  const { t } = useLang();
  return (
    <div className="grid grid-cols-3 gap-3">
      <Stat label={t("counter.current")} value={vehicleCount} color="text-blue-400" />
      <Stat label={t("counter.in")}      value={inCount}      color="text-green-400" />
      <Stat label={t("counter.out")}     value={outCount}     color="text-orange-400" />
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div className="bg-gray-800 rounded-xl p-3 text-center">
      <p className={`text-2xl font-bold ${color}`}>{value.toLocaleString()}</p>
      <p className="text-xs text-gray-500 mt-1">{label}</p>
    </div>
  );
}
