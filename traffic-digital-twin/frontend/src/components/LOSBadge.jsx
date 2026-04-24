/**
 * LOSBadge.jsx — 혼잡도 LOS 등급 배지 (A ~ F)
 */

import { LOS_BADGE_COLORS } from "../utils/colorMap";

const LOS_LABELS = {
  A: "원활",
  B: "양호",
  C: "보통",
  D: "지체",
  E: "심각",
  F: "마비",
};

export default function LOSBadge({ grade = "A" }) {
  const bg = LOS_BADGE_COLORS[grade] ?? "bg-gray-500";
  return (
    <div className="flex flex-col items-center gap-1">
      <div className={`${bg} text-white text-4xl font-black w-16 h-16 rounded-full flex items-center justify-center shadow-lg`}>
        {grade}
      </div>
      <p className="text-xs text-gray-400">{LOS_LABELS[grade] ?? ""}</p>
      <p className="text-xs text-gray-500">서비스 수준</p>
    </div>
  );
}
