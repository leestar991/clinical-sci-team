'use client';

import { useRef, useEffect, useState } from 'react';
import GameEngine from '../engine/GameEngineSimple';
import { fetchData } from '../data/mockData';
import type { AgentData } from '../types';

interface GameCanvasProps {
  onNpcHover?: (data: AgentData, pos: { x: number; y: number }) => void;
  onNpcLeave?: () => void;
  onNpcClick?: (data: AgentData) => void;
  onCursorStateChange?: (state: string) => void;
  onLayoutChange?: (layout: { sceneW: number; sceneH: number }) => void;
  mapConfig: any;
  refreshTrigger?: number;
  gameEngineRef?: React.MutableRefObject<any>;
}

export default function GameCanvas({
  onNpcHover,
  onNpcLeave,
  onNpcClick,
  onCursorStateChange,
  onLayoutChange,
  mapConfig,
  refreshTrigger = 0,
  gameEngineRef,
}: GameCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const loadingRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<GameEngine | null>(null);
  const refreshFnRef = useRef<(() => Promise<void>) | null>(null);
  const [progress, setProgress] = useState(0);
  const [loadText, setLoadText] = useState('Loading assets...');

  const cbRef = useRef({
    onNpcHover,
    onNpcLeave,
    onNpcClick,
    onCursorStateChange,
    onLayoutChange,
  });
  cbRef.current = {
    onNpcHover,
    onNpcLeave,
    onNpcClick,
    onCursorStateChange,
    onLayoutChange,
  };

  useEffect(() => {
    if (!containerRef.current) return;
    let aborted = false;
    let refreshTimer: NodeJS.Timeout | null = null;
    let hideLoadingTimer: NodeJS.Timeout | null = null;

    const engine = new GameEngine();
    engineRef.current = engine;
    if (gameEngineRef) gameEngineRef.current = engine;

    engine.onNpcHover = (...args) => cbRef.current.onNpcHover?.(...args);
    engine.onNpcLeave = (...args) => cbRef.current.onNpcLeave?.(...args);
    engine.onNpcClick = (...args) => cbRef.current.onNpcClick?.(...args);
    engine.onCursorStateChange = (...args) => cbRef.current.onCursorStateChange?.(...args);
    engine.onLayoutChange = (layout) => cbRef.current.onLayoutChange?.(layout);

    (async () => {
      try {
        engine.init(containerRef.current!);

        await engine.loadAssets((p, label) => {
          if (aborted) return;
          setProgress(p);
          if (label) setLoadText(label);
        });
        if (aborted) return;

        const data = await fetchData();
        if (aborted) return;

        engine.populateNPCs(data.agents || [], data.events || []);

        const doRefresh = async () => {
          try {
            const nextData = await fetchData();
            if (!aborted) {
              engine.updateData(nextData.agents || [], nextData.events || []);
            }
          } catch (err) {
            console.error('[GameCanvas] refresh error:', err);
          }
        };
        refreshFnRef.current = doRefresh;

        refreshTimer = setInterval(doRefresh, 15000);
      } catch (err) {
        console.error('[GameCanvas] init error:', err);
      }

      if (!aborted && loadingRef.current) {
        loadingRef.current.style.opacity = '0';
        loadingRef.current.style.pointerEvents = 'none';
        hideLoadingTimer = setTimeout(() => {
          if (loadingRef.current) {
            loadingRef.current.style.display = 'none';
          }
        }, 400);
      }
    })();

    return () => {
      aborted = true;
      refreshFnRef.current = null;
      if (refreshTimer) clearInterval(refreshTimer);
      if (hideLoadingTimer) clearTimeout(hideLoadingTimer);
      engine.destroy();
      engineRef.current = null;
      if (gameEngineRef) gameEngineRef.current = null;
      cbRef.current.onCursorStateChange?.('normal');
    };
  }, [mapConfig, gameEngineRef]);

  useEffect(() => {
    if (refreshTrigger > 0) {
      refreshFnRef.current?.();
    }
  }, [refreshTrigger]);

  return (
    <div className="townWrap visible">
      <div ref={loadingRef} className="loading">
        <div className="loadingInner">
          <div className="loadingTitle">AGENT VALLEY</div>
          <div className="loadingBar">
            <div className="loadingFill" style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
          <div className="loadingText">{loadText}</div>
        </div>
      </div>
      <div className="sceneContainer" ref={containerRef} />
    </div>
  );
}
