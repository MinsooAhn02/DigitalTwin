/**
 * MapView.jsx — Deck.gl WebGL2 GPU 가속 지도
 *
 * · ScatterplotLayer : 차량 위치
 *     - In  → 파랑, Out → 빨강, 과속 → 강렬한 빨강
 * · TextLayer        : track_id 레이블
 * · 추가 레이어는 extraLayers prop으로 주입 (TrailLayer 등)
 *
 * 초기 뷰포트: 실제 데이터 GPS 중심 (lat 37.462, lon 127.038)
 */

import { useState, useMemo } from "react";
import DeckGL from "@deck.gl/react";
import { ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import Map from "react-map-gl/maplibre";
import { getVehicleColor } from "../utils/colorMap";

const MAP_STYLE =
  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

// 실제 데이터 중심좌표 (real_world_track_data.json 기준)
const INITIAL_VIEW = {
  longitude: 127.0386,
  latitude:  37.4626,
  zoom:      18,
  pitch:     45,
  bearing:   0,
};

export default function MapView({ vehicles = [], extraLayers = [] }) {
  const [viewState, setViewState] = useState(INITIAL_VIEW);

  // 과속 차량이 위에 렌더링되도록 정렬
  const sorted = useMemo(
    () => [...vehicles].sort((a, b) => (a.is_speeding ? 1 : 0) - (b.is_speeding ? 1 : 0)),
    [vehicles]
  );

  const scatterLayer = new ScatterplotLayer({
    id:           "vehicles",
    data:         sorted,
    getPosition:  (d) => [d.lon, d.lat],
    getRadius:    (d) => (d.is_speeding ? 5 : 3),
    getFillColor: (d) => getVehicleColor(d.direction, d.is_speeding),
    pickable:     true,
    radiusUnits:  "meters",
    radiusMinPixels: 5,
    updateTriggers: {
      getFillColor: vehicles,
      getRadius:    vehicles,
    },
  });

  const textLayer = new TextLayer({
    id:           "vehicle-labels",
    data:         sorted,
    getPosition:  (d) => [d.lon, d.lat],
    getText:      (d) => `#${d.track_id}`,
    getSize:      11,
    getColor:     [255, 255, 255, 200],
    getPixelOffset: [0, -14],
    updateTriggers: { getText: vehicles },
  });

  return (
    <DeckGL
      viewState={viewState}
      onViewStateChange={({ viewState: vs }) => setViewState(vs)}
      controller
      layers={[...extraLayers, scatterLayer, textLayer]}
      style={{ position: "relative", width: "100%", height: "100%" }}
      getTooltip={({ object: d }) =>
        d && {
          html: `
            <b>#${d.track_id}</b> &nbsp; <span style="color:#aaa">${d.class_name}</span><br/>
            방향: <b>${d.direction}</b><br/>
            속도: <b>${d.speed_kph?.toFixed(1) ?? "—"} km/h</b>
            ${d.is_speeding ? ' &nbsp;<span style="color:#f87171">🚨 과속</span>' : ""}<br/>
            체류: ${d.dwell_frames}f
            ${d.is_bottleneck ? ' &nbsp;<span style="color:#fbbf24">⚠ 병목</span>' : ""}
            ${d.is_tailgating ? '<br/><span style="color:#fbbf24">⚠ 꼬리물기</span>' : ""}
          `,
          style: {
            background: "#111827",
            color: "#f9fafb",
            fontSize: "12px",
            borderRadius: "6px",
            padding: "8px",
          },
        }
      }
    >
      <Map mapStyle={MAP_STYLE} />
    </DeckGL>
  );
}
