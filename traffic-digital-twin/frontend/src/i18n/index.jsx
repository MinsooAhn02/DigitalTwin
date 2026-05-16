import { createContext, useContext, useState } from "react";

const TRANSLATIONS = {
  en: {
    // ── App ──────────────────────────────────────────────────────────
    "app.title":          "🛣️ Traffic Digital Twin",
    "app.connected":      "Live Connected",
    "app.reconnecting":   "Reconnecting…",
    "app.clickCctv":      "Click a CCTV camera on the map",
    "app.clickCctvSub":   "Real-time vehicle detection will start for that camera",
    "app.switching":      "⏳ Connecting stream…",
    "app.refreshCctv":    "📷 Refresh CCTV",
    "app.loading":        "Loading…",
    "app.nCameras":       "{{n}} cameras",
    "app.zoomHint":       "Zoom ≥ 15 to display vehicles",
    "app.mapClickBanner": "🗺 Click the same point on the map",
    "app.classDist":      "Vehicle Distribution",
    "app.alerts":         "Alerts ({{n}})",
    "app.speeding":       "Speeding",
    "app.bottleneck":     "Bottleneck",
    // ── Legend ───────────────────────────────────────────────────────
    "legend.in":       "Inbound (In)",
    "legend.out":      "Outbound (Out)",
    "legend.speeding": "Speeding",
    "legend.unknown":  "Unknown",
    "legend.fov":      "FOV (when selected)",
    // ── Sidebar widgets ──────────────────────────────────────────────
    "counter.current": "Current",
    "counter.in":      "In",
    "counter.out":     "Out",
    "gauge.avgSpeed":  "Avg Speed",
    "los.label":       "Level of Service",
    "los.A": "Free Flow",
    "los.B": "Good",
    "los.C": "Fair",
    "los.D": "Slow",
    "los.E": "Congested",
    "los.F": "Gridlock",
    "chart.noVehicles": "No vehicles detected",
    // ── MapView ──────────────────────────────────────────────────────
    "map.dir":        "Direction",
    "map.speed":      "Speed",
    "map.dwell":      "Dwell",
    "map.parked":     "🅿 Parked",
    "map.speeding":   "🚨 Speeding",
    "map.bottleneck": "⚠ Bottleneck",
    "map.clickHint":  "Click → switch live · show FOV",
    "map.modeToggle": "Map: {{mode}} → {{next}}",
    // ── CctvPlayer tabs / header ─────────────────────────────────────
    "cctv.tab.live": "📷 Live",
    "cctv.tab.yolo": "🤖 YOLO Detection",
    "cctv.tab.cal":  "🔧 Calibration",
    "cctv.tab.roi":  "🎯 ROI Editor",
    "cctv.yolo.idle":    "Idle",
    "cctv.yolo.loading": "Loading model…",
    "cctv.yolo.running": "Detecting",
    "cctv.yolo.error":   "Connection failed",
    "cctv.roi.set":      "● Configured",
    "cctv.cal.done":     "● Done",
    "cctv.yolo.msg.loading": "Loading YOLO model… (first time only)",
    "cctv.yolo.msg.wait":    "Capturing frames from video…",
    "cctv.stream.loading":     "Connecting stream…",
    "cctv.stream.error":       "Stream connection failed",
    "cctv.stream.unsupported": "HLS not supported in this browser",
    // ── CalibBar ─────────────────────────────────────────────────────
    "calib.title":         "🔧 Camera Calibration",
    "calib.step.pixel":    "Point {{n}}/4 — Click on video",
    "calib.step.gps":      "Point {{n}}/4 — Click on map",
    "calib.step.done":     "Calibration complete — press Save",
    "calib.reset":         "Reset",
    "calib.save":          "✓ Save",
    "calib.saving":        "Saving…",
    "calib.cancel":        "Cancel",
    "calib.point":         "Pt{{n}}",
    "calib.notClicked":    "Not set",
    "calib.awaitingMap":   "Awaiting map",
    "calib.gpsSet":        "✓ GPS",
    // ── RoiBar ───────────────────────────────────────────────────────
    "roi.title":       "🎯 ROI Editor",
    "roi.vertices":    "{{n}} vertices{{done}}",
    "roi.vertDone":    " ✓",
    "roi.reset":       "Reset",
    "roi.save":        "✓ Save (include area)",
    "roi.saving":      "Saving…",
    "roi.cancel":      "Cancel",
    // ── ROI hint keys (set inside RoiEditor, rendered by RoiBar) ─────
    "roi.hint.start":   "Only vehicles inside the green area are detected · Click to add vertices, double-click to finish",
    "roi.hint.addMore": "Double-click to complete polygon / keep clicking to add vertices",
    "roi.hint.tooFew":  "Need at least 3 vertices",
    "roi.hint.ready":   "Press Save to apply",
    "roi.hint.error":   "Save failed — check server connection",
    "roi.hint.reset":   "Click to add vertices, double-click to finish",
  },

  ko: {
    // ── App ──────────────────────────────────────────────────────────
    "app.title":          "🛣️ 교통 디지털 트윈",
    "app.connected":      "실시간 연결 중",
    "app.reconnecting":   "재연결 중…",
    "app.clickCctv":      "지도에서 CCTV를 클릭하세요",
    "app.clickCctvSub":   "클릭하면 해당 카메라의 실시간 차량 탐지가 시작됩니다",
    "app.switching":      "⏳ 스트림 연결 중…",
    "app.refreshCctv":    "📷 CCTV 새로고침",
    "app.loading":        "로딩 중…",
    "app.nCameras":       "{{n}}개 카메라",
    "app.zoomHint":       "zoom 15 이상으로 확대하면 차량이 표시됩니다",
    "app.mapClickBanner": "🗺 지도에서 동일 지점을 클릭하세요",
    "app.classDist":      "차종 분포",
    "app.alerts":         "경보 ({{n}})",
    "app.speeding":       "과속",
    "app.bottleneck":     "병목",
    // ── Legend ───────────────────────────────────────────────────────
    "legend.in":       "진입 (In)",
    "legend.out":      "진출 (Out)",
    "legend.speeding": "과속",
    "legend.unknown":  "Unknown",
    "legend.fov":      "시야 범위 (선택 시)",
    // ── Sidebar widgets ──────────────────────────────────────────────
    "counter.current": "현재 차량",
    "counter.in":      "진입 (In)",
    "counter.out":     "진출 (Out)",
    "gauge.avgSpeed":  "평균 속도",
    "los.label":       "서비스 수준",
    "los.A": "원활",
    "los.B": "양호",
    "los.C": "보통",
    "los.D": "지체",
    "los.E": "심각",
    "los.F": "마비",
    "chart.noVehicles": "탐지된 차량 없음",
    // ── MapView ──────────────────────────────────────────────────────
    "map.dir":        "방향",
    "map.speed":      "속도",
    "map.dwell":      "체류",
    "map.parked":     "🅿 주차",
    "map.speeding":   "🚨 과속",
    "map.bottleneck": "⚠ 병목",
    "map.clickHint":  "클릭 → 실시간 전환 · 시야 범위 표시",
    "map.modeToggle": "현재: {{mode}} → {{next}}로 전환",
    // ── CctvPlayer ───────────────────────────────────────────────────
    "cctv.tab.live": "📷 실시간",
    "cctv.tab.yolo": "🤖 YOLO 탐지",
    "cctv.tab.cal":  "🔧 보정",
    "cctv.tab.roi":  "🎯 ROI 편집",
    "cctv.yolo.idle":    "대기",
    "cctv.yolo.loading": "모델 로드…",
    "cctv.yolo.running": "탐지 중",
    "cctv.yolo.error":   "연결 실패",
    "cctv.roi.set":      "● 설정됨",
    "cctv.cal.done":     "● 완료",
    "cctv.yolo.msg.loading": "YOLO 모델 로드 중… (최초 1회)",
    "cctv.yolo.msg.wait":    "영상에서 프레임 캡처 중…",
    "cctv.stream.loading":     "스트림 연결 중…",
    "cctv.stream.error":       "스트림 연결 실패",
    "cctv.stream.unsupported": "HLS 미지원 브라우저",
    // ── CalibBar ─────────────────────────────────────────────────────
    "calib.title":         "🔧 카메라 보정",
    "calib.step.pixel":    "점 {{n}}/4 — 영상에서 클릭",
    "calib.step.gps":      "점 {{n}}/4 — 지도에서 위치 클릭",
    "calib.step.done":     "보정 완료 — 저장 버튼을 누르세요",
    "calib.reset":         "초기화",
    "calib.save":          "✓ 저장",
    "calib.saving":        "저장 중…",
    "calib.cancel":        "취소",
    "calib.point":         "점{{n}}",
    "calib.notClicked":    "미클릭",
    "calib.awaitingMap":   "지도 대기",
    "calib.gpsSet":        "✓ GPS",
    // ── RoiBar ───────────────────────────────────────────────────────
    "roi.title":       "🎯 ROI 편집",
    "roi.vertices":    "꼭짓점 {{n}}개{{done}}",
    "roi.vertDone":    " ✓",
    "roi.reset":       "초기화",
    "roi.save":        "✓ 저장 (포함 영역)",
    "roi.saving":      "저장 중…",
    "roi.cancel":      "취소",
    // ── ROI hints ────────────────────────────────────────────────────
    "roi.hint.start":   "초록 영역 내 차량만 감지됩니다 · 클릭으로 꼭짓점 추가, 더블클릭으로 완성",
    "roi.hint.addMore": "더블클릭으로 polygon 완성 / 계속 클릭해서 꼭짓점 추가",
    "roi.hint.tooFew":  "꼭짓점을 3개 이상 찍어야 합니다",
    "roi.hint.ready":   "저장 버튼을 눌러 적용하세요",
    "roi.hint.error":   "저장 실패 — 서버 연결을 확인하세요",
    "roi.hint.reset":   "클릭으로 꼭짓점 추가, 더블클릭으로 완성",
  },
};

const LangContext = createContext({ lang: "en", setLang: () => {} });

export function LangProvider({ children }) {
  const [lang, setLang] = useState("en");
  return (
    <LangContext.Provider value={{ lang, setLang }}>
      {children}
    </LangContext.Provider>
  );
}

export function useLang() {
  const { lang, setLang } = useContext(LangContext);
  const t = (key, params) => {
    let str = TRANSLATIONS[lang]?.[key] ?? TRANSLATIONS.en[key] ?? key;
    if (params) {
      Object.entries(params).forEach(([k, v]) => {
        str = str.replace(new RegExp(`\\{\\{${k}\\}\\}`, "g"), String(v));
      });
    }
    return str;
  };
  return { lang, setLang, t };
}
