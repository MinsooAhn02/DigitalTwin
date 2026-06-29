/**
 * useWebSocket — real-time WebSocket data hook
 *
 * Returns:
 *   frameData      : latest FrameAnalytics JSON (null initially)
 *   isConnected    : connection state boolean
 *   error          : error message string | null
 *   cameraReady    : camera-switch completion event counter (triggers useEffect on change)
 *   cameraReadyInfo: latest camera_ready payload { camera_key, roi, name }
 *   cameraStatus   : null | { type: "retrying", message } | { type: "failed", message }
 */

import { useEffect, useRef, useState, useCallback } from "react";

const WS_URL = import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws";
const RECONNECT_DELAY_MS = 3000;

export function useWebSocket() {
  const [frameData,        setFrameData]        = useState(null);
  const [isConnected,      setIsConnected]      = useState(false);
  const [error,            setError]            = useState(null);
  const [cameraReady,      setCameraReady]      = useState(0);
  const [cameraReadyInfo,  setCameraReadyInfo]  = useState(null);
  const [calibrating,      setCalibrating]      = useState(null);  // { elapsed_s } | null
  const [autoCalibInfo,    setAutoCalibInfo]    = useState(null);
  const [backgroundStatus, setBackgroundStatus] = useState({});
  const [congestionClusters, setCongestionClusters] = useState([]);
  // null | { type: "retrying", message } | { type: "failed", message }
  const [cameraStatus,     setCameraStatus]     = useState(null);

  const wsRef      = useRef(null);
  const retryTimer = useRef(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setIsConnected(true);
      setError(null);
    };

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        // 메시지 타입 분기
        if (data.type === "calibrating") {
          setCalibrating({ elapsed_s: data.elapsed_s ?? 0 });
        } else if (data.type === "camera_ready") {
          setCalibrating(null);
          setCameraStatus(null);
          setCameraReadyInfo({ camera_key: data.camera_key, roi: data.roi, name: data.name, calibrated: data.calibrated ?? false, road_name: data.road_name ?? null, road_lanes: data.road_lanes ?? null, road_max_spd: data.road_max_spd ?? null, road_bearing: data.road_bearing ?? null, name_bearing: data.name_bearing ?? null, snap_lat: data.snap_lat ?? null, snap_lon: data.snap_lon ?? null, road_width_m: data.road_width_m ?? null, road_pts: data.road_pts ?? null, snap_along_m: data.snap_along_m ?? null, roi_gps_ring: data.roi_gps_ring ?? null });
          setAutoCalibInfo(null); // 카메라 전환 시 이전 자동 캘리브 정보 초기화
          setCameraReady((n) => n + 1);
        } else if (data.type === "camera_error") {
          const retrying = data.retrying ?? false;
          setCameraStatus(retrying
            ? { type: "retrying", message: data.message ?? "" }
            : { type: "failed",   message: data.message ?? "" }
          );
          setError(`Camera switch failed: ${data.message ?? ""}`);
          if (!retrying) {
            // 최종 실패 시에만 switching 해제 (retrying=true면 아직 시도 중)
            setCameraReady((n) => n + 1);
          }
        } else if (data.type === "roi_updated") {
          // Phase 3: ROI 변경 시 GPS ring 업데이트
          setCameraReadyInfo((prev) => prev ? { ...prev, roi_gps_ring: data.roi_gps_ring ?? null } : prev);
        } else if (data.type === "auto_calibrated") {
          setCameraReadyInfo((prev) => {
            const base = prev ?? {};
            // road_pts/snap_along_m은 학습된 값으로 갱신; name_bearing은 덮어쓰지 않음.
            return {
              ...base,
              calibrated: true,
              ...(data.road_pts      != null && { road_pts:      data.road_pts }),
              ...(data.snap_along_m  != null && { snap_along_m:  data.snap_along_m }),
              ...(data.roi_gps_ring  != null && { roi_gps_ring:  data.roi_gps_ring }),
            };
          });
          setAutoCalibInfo({
            cam_h_m:       data.cam_h_m       ?? null,
            near_m:        data.near_m        ?? null,
            far_m:         data.far_m         ?? null,
            road_width_m:  data.road_width_m  ?? null,
            pitch_deg:     data.pitch_deg     ?? null,
            heading:       data.heading       ?? null,
            road_length_m: data.road_length_m ?? null,
            focal_px:      data.focal_px      ?? null,
            residual_px:   data.residual_px   ?? null,
            direction_source: data.direction_source ?? null,
            image_curve_sign: data.image_curve_sign ?? null,
            image_curve_px: data.image_curve_px ?? null,
            map_ft_sign: data.map_ft_sign ?? null,
            map_tf_sign: data.map_tf_sign ?? null,
            map_ft_curve_m: data.map_ft_curve_m ?? null,
            map_tf_curve_m: data.map_tf_curve_m ?? null,
          });
          setCalibrating(null);
        } else if (data.type === "background_status") {
          setBackgroundStatus(data.cameras ?? {});
        } else if (data.type === "congestion_clusters") {
          setCongestionClusters(data.clusters ?? []);
        } else {
          // 일반 frame analytics 데이터
          setFrameData(data);
        }
      } catch {
        // 파싱 실패는 무시
      }
    };

    ws.onerror = () => setError("WebSocket connection error");

    ws.onclose = () => {
      setIsConnected(false);
      // 자동 재연결
      retryTimer.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(retryTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { frameData, isConnected, error, cameraReady, cameraReadyInfo, calibrating, autoCalibInfo, backgroundStatus, congestionClusters, cameraStatus };
}
