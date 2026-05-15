/**
 * useWebSocket.js — 실시간 WebSocket 데이터 수신 훅
 *
 * 반환값:
 *   frameData    : 최신 FrameAnalytics JSON (null 초기값)
 *   isConnected  : 연결 상태 boolean
 *   error        : 에러 메시지 string | null
 *   cameraReady  : 카메라 전환 완료 이벤트 카운터 (변경될 때마다 useEffect 트리거용)
 */

import { useEffect, useRef, useState, useCallback } from "react";

const WS_URL = import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws";
const RECONNECT_DELAY_MS = 3000;

export function useWebSocket() {
  const [frameData,   setFrameData]   = useState(null);
  const [isConnected, setIsConnected] = useState(false);
  const [error,       setError]       = useState(null);
  const [cameraReady, setCameraReady] = useState(0);

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
        if (data.type === "camera_ready") {
          setCameraReady((n) => n + 1);
        } else if (data.type === "camera_error") {
          setError(`카메라 전환 실패: ${data.message ?? ""}`);
          setCameraReady((n) => n + 1); // 에러여도 로딩 상태 해제
        } else {
          // 일반 frame analytics 데이터
          setFrameData(data);
        }
      } catch {
        // 파싱 실패는 무시
      }
    };

    ws.onerror = () => setError("WebSocket 연결 오류");

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

  return { frameData, isConnected, error, cameraReady };
}
