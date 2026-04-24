/**
 * TrailLayer.jsx — 차량 이동 궤적 PathLayer
 *
 * 상위 컴포넌트에서 trailMap (Map<track_id, [lon,lat][]>) 을 내려받아
 * Deck.gl PathLayer로 렌더링한다.
 * MapView 내부 layers 배열에 주입하는 방식으로 사용:
 *   const trail = useTrailLayer(trailMap);
 *   <DeckGL layers={[..., trail]} />
 */

import { useMemo } from "react";
import { PathLayer } from "@deck.gl/layers";
import { DIRECTION_COLORS } from "../utils/colorMap";

const MAX_TRAIL_LENGTH = 60;   // 최대 60 프레임(약 2초) 궤적 유지

/**
 * 궤적 맵을 갱신하는 헬퍼 (useReducer 등과 함께 사용)
 * @param {Map<number, [number,number][]>} prev
 * @param {Array<{track_id, lon, lat, class_name}>} vehicles
 */
export function updateTrailMap(prev, vehicles) {
  const next = new Map(prev);
  const activeIds = new Set(vehicles.map((v) => v.track_id));

  for (const id of next.keys()) {
    if (!activeIds.has(id)) next.delete(id);
  }

  for (const v of vehicles) {
    const trail = next.get(v.track_id) ?? { path: [], direction: v.direction };
    trail.path.push([v.lon, v.lat]);
    if (trail.path.length > MAX_TRAIL_LENGTH) trail.path.shift();
    trail.direction = v.direction;   // 최신 direction 유지
    next.set(v.track_id, trail);
  }
  return next;
}

/**
 * @param {Map<number, {path, direction}>} trailMap
 * @param {Array} vehicles
 */
export function useTrailLayer(trailMap, vehicles) {
  return useMemo(() => {
    const data = [...trailMap.entries()].map(([id, t]) => ({
      id,
      path:      t.path,
      direction: t.direction ?? "Unknown",
    }));

    return new PathLayer({
      id:           "trails",
      data,
      getPath:      (d) => d.path,
      getColor:     (d) => {
        const base = DIRECTION_COLORS[d.direction] ?? DIRECTION_COLORS.Unknown;
        return [base[0], base[1], base[2], 130];   // 반투명 궤적
      },
      getWidth:     2,
      widthUnits:   "pixels",
      jointRounded: true,
      capRounded:   true,
      updateTriggers: { getColor: [...trailMap.values()].map((t) => t.direction) },
    });
  }, [trailMap, vehicles]);
}
