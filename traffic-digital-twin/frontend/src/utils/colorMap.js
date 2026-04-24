/**
 * colorMap.js — 차종별 / 방향별 RGB 색상 매핑
 * 반환값: [R, G, B, A] (0-255)  ← Deck.gl 레이어에서 직접 사용
 *
 * 기존 Streamlit app.py 색상 로직 계승:
 *   In  → 파랑,  Out → 빨강,  Unknown → 회색
 *   과속 → 밝은 빨강 (강조)
 */

export const CLASS_COLORS = {
  car:        [59,  130, 246, 220],
  truck:      [245, 158,  11, 220],
  bus:        [16,  185, 129, 220],
  motorcycle: [139,  92, 246, 220],
  unknown:    [156, 163, 175, 180],
};

export const DIRECTION_COLORS = {
  In:      [0,   120, 255, 200],   // 파랑
  Out:     [255,  50,  50, 200],   // 빨강
  Unknown: [200, 200, 200, 160],   // 회색
};

export const SPEEDING_COLOR = [255, 30, 30, 255];   // 강렬한 빨강

/**
 * 우선순위: 과속 > 방향 색상
 * @param {string} direction  "In" | "Out" | "Unknown"
 * @param {boolean} isSpeeding
 * @returns {[number, number, number, number]}
 */
export function getVehicleColor(direction, isSpeeding) {
  if (isSpeeding) return SPEEDING_COLOR;
  return DIRECTION_COLORS[direction] ?? DIRECTION_COLORS.Unknown;
}

export const LOS_BADGE_COLORS = {
  A: "bg-green-500",
  B: "bg-lime-500",
  C: "bg-yellow-400",
  D: "bg-orange-500",
  E: "bg-red-500",
  F: "bg-red-900",
};
