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

// 라이트/위성 모드에서 더 진한 색상
const DIRECTION_COLORS_CONTRAST = {
  In:      [0,   80, 200, 230],
  Out:     [200,  20,  20, 230],
  Unknown: [80,  80,  80, 200],
};

/**
 * 우선순위: 과속 > 방향 색상
 * @param {string} direction  "In" | "Out" | "Unknown"
 * @param {boolean} highContrast  라이트/위성 모드에서 true
 * @returns {[number, number, number, number]}
 */
export function getVehicleColor(direction, highContrast = false) {
  const palette = highContrast ? DIRECTION_COLORS_CONTRAST : DIRECTION_COLORS;
  return palette[direction] ?? palette.Unknown;
}

// 정체 구간 클러스터 심각도 색상 ([B]) — [R,G,B] (alpha 는 레이어에서 부여)
// congestion.py 의 minor/medium/severe 와 1:1 대응
export const SEVERITY_COLORS = {
  minor:  [251, 191, 36],   // 노랑
  medium: [249, 115, 22],   // 주황
  severe: [239, 68, 68],    // 빨강
};

export function getSeverityColor(severity, alpha = 255) {
  const rgb = SEVERITY_COLORS[severity] ?? SEVERITY_COLORS.minor;
  return [...rgb, alpha];
}

export const LOS_BADGE_COLORS = {
  A: "bg-green-500",
  B: "bg-lime-500",
  C: "bg-yellow-400",
  D: "bg-orange-500",
  E: "bg-red-500",
  F: "bg-red-900",
};
