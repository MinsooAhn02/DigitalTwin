import { useMemo } from "react";
import DeckGL from "@deck.gl/react";
import { ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import Map from "react-map-gl/maplibre";
import { getVehicleColor } from "../utils/colorMap";

const MAP_STYLE =
  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

const VEHICLE_MIN_ZOOM = 15;

export default function MapView({
  vehicles = [],
  extraLayers = [],
  cctvList = [],
  selectedCctv = null,
  viewState,
  onViewStateChange,
  onCctvClick,
}) {
  const showVehicles = viewState.zoom >= VEHICLE_MIN_ZOOM;

  const sorted = useMemo(
    () => [...vehicles].sort((a, b) => (a.is_speeding ? 1 : 0) - (b.is_speeding ? 1 : 0)),
    [vehicles]
  );

  const cctvMarkerLayer = new ScatterplotLayer({
    id:              "cctvs",
    data:            cctvList,
    getPosition:     (d) => [d.lon, d.lat],
    getRadius:       20,
    getFillColor:    (d) =>
      selectedCctv?.id === d.id ? [34, 211, 238, 240] : [251, 191, 36, 210],
    getLineColor:    [255, 255, 255, 220],
    lineWidthMinPixels: 2,
    stroked:         true,
    radiusUnits:     "meters",
    radiusMinPixels: 16,
    pickable:        true,
    onClick:         ({ object }) => object && onCctvClick?.(object),
    updateTriggers:  { getFillColor: [selectedCctv] },
  });

  const cctvLabelLayer = new TextLayer({
    id:             "cctv-labels",
    data:           cctvList,
    getPosition:    (d) => [d.lon, d.lat],
    getText:        (d) => d.name || d.id,
    getSize:        11,
    getColor:       [253, 230, 138, 230],
    getPixelOffset: [0, -26],
    fontFamily:     '"Segoe UI", system-ui, sans-serif',
    fontWeight:     600,
  });

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
    id:             "vehicle-labels",
    data:           sorted,
    getPosition:    (d) => [d.lon, d.lat],
    getText:        (d) => `#${d.track_id}`,
    getSize:        11,
    getColor:       [255, 255, 255, 200],
    getPixelOffset: [0, -14],
    updateTriggers: { getText: vehicles },
  });

  return (
    <DeckGL
      viewState={viewState}
      onViewStateChange={({ viewState: vs }) => onViewStateChange(vs)}
      controller
      layers={[
        ...(showVehicles ? extraLayers : []),
        cctvMarkerLayer,
        cctvLabelLayer,
        ...(showVehicles ? [scatterLayer, textLayer] : []),
      ]}
      style={{ position: "relative", width: "100%", height: "100%" }}
      getTooltip={({ object: d }) => {
        if (!d) return null;

        if (d.track_id !== undefined) {
          return {
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
              background: "#111827", color: "#f9fafb",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        if (d.id) {
          return {
            html: `<b>📷 ${d.name || ""}</b><br/><span style="color:#9ca3af;font-size:11px">클릭하여 실시간 전환</span>`,
            style: {
              background: "#111827", color: "#fbbf24",
              fontSize: "12px", borderRadius: "6px", padding: "8px",
            },
          };
        }

        return null;
      }}
    >
      <Map mapStyle={MAP_STYLE} />
    </DeckGL>
  );
}
