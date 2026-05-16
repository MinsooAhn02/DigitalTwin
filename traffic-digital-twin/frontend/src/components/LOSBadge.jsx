import { LOS_BADGE_COLORS } from "../utils/colorMap";
import { useLang } from "../i18n/index.jsx";

export default function LOSBadge({ grade = "A" }) {
  const { t } = useLang();
  const bg    = LOS_BADGE_COLORS[grade] ?? "bg-gray-500";
  return (
    <div className="flex flex-col items-center gap-1">
      <div className={`${bg} text-white text-4xl font-black w-16 h-16 rounded-full flex items-center justify-center shadow-lg`}>
        {grade}
      </div>
      <p className="text-xs text-gray-400">{t(`los.${grade}`) || ""}</p>
      <p className="text-xs text-gray-500">{t("los.label")}</p>
    </div>
  );
}
