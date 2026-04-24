'use client';

import { useRef, useEffect, useState } from 'react';
import GameEngine from '../engine/GameEngine';
import type { AgentValleyData } from '../data/realData';
import type { Agent } from '../types';
import { useSubtasks } from '../hooks/useSubtasks';

interface GameCanvasProps {
  onNpcHover?: (data: any, pos: { x: number; y: number }) => void;
  onNpcLeave?: () => void;
  onNpcClick?: (data: any) => void;
  onCursorStateChange?: (state: string) => void;
  onLayoutChange?: (layout: { sceneW: number; sceneH: number }) => void;
  mapConfig: any;
  refreshTrigger?: number;
  gameEngineRef?: React.MutableRefObject<any>;
  agentData: AgentValleyData | null;
  isLoadingData?: boolean;
  isChatting?: boolean;
  isWaitingForUser?: boolean;
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
  agentData,
  isLoadingData = false,
  isChatting = false,
  isWaitingForUser = false,
}: GameCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const loadingRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<GameEngine | null>(null);
  const [progress, setProgress] = useState(0);
  const [loadText, setLoadText] = useState('Loading assets...');
  const [engineReady, setEngineReady] = useState(false); // Track if engine is fully initialized

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
    let hideLoadingTimer: NodeJS.Timeout | null = null;

    setEngineReady(false);

    const engine = new GameEngine({ mapConfig });
    engineRef.current = engine;
    if (gameEngineRef) gameEngineRef.current = engine;

    engine.onNpcHover = (...args) => cbRef.current.onNpcHover?.(...args);
    engine.onNpcLeave = (...args) => cbRef.current.onNpcLeave?.(...args);
    engine.onNpcClick = (...args) => cbRef.current.onNpcClick?.(...args);
    engine.onCursorStateChange = (...args) => cbRef.current.onCursorStateChange?.(...args);
    engine.onLayoutChange = (layout) => cbRef.current.onLayoutChange?.(layout);

    (async () => {
      try {
        if (aborted) return;
        engine.init(containerRef.current!);

        if (aborted) return;
        await engine.loadAssets((p, label) => {
          if (aborted) return;
          setProgress(p);
          if (label) setLoadText(label);
        });
        if (aborted) return;

        console.log('[GameCanvas] Engine fully initialized and ready');
        setEngineReady(true); // Mark engine as ready
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
      if (hideLoadingTimer) clearTimeout(hideLoadingTimer);
      setEngineReady(false);
      engine.destroy();
      engineRef.current = null;
      if (gameEngineRef) gameEngineRef.current = null;
      cbRef.current.onCursorStateChange?.('normal');
    };
  }, [mapConfig, gameEngineRef]); // Only re-initialize when map config changes

  // Use subtasks hook to monitor stream messages
  const { subtasks } = useSubtasks(agentData?.thread?.values?.messages, {
    sceneW: 896,
    sceneH: 640,
  });

  // Log subtasks changes
  useEffect(() => {
    console.log('[GameCanvas] Subtasks changed, count:', subtasks.size);
    if (subtasks.size > 0) {
      console.log('[GameCanvas] Subtasks:', Array.from(subtasks.values()));
    }
  }, [subtasks]);

  // Separate effect to handle agent data updates (without re-initializing engine)
  useEffect(() => {
    // Wait for engine to be fully initialized and data to be loaded
    if (!engineRef.current || !engineReady || isLoadingData) {
      return;
    }

    const agents: Agent[] = [];

    // 1. Create main agent
    const agentStatus = isChatting ? 'working' : 'idle';

    const mainAgent: Agent = agentData?.thread ? {
      id: agentData.thread.thread_id,
      name: agentData.agentName,
      session_key: agentData.thread.thread_id,
      provider: 'anthropic',
      model: 'claude-sonnet-4',
      status: agentStatus,
      charName: agentData.charName,
      position: { x: 350, y: 500 },
      first_seen_at: agentData.thread.updated_at,
    } : {
      id: 'agent-1',
      name: agentData?.agentName || 'AI Assistant',
      session_key: undefined,
      provider: 'anthropic',
      model: 'claude-sonnet-4',
      status: agentStatus,
      charName: agentData?.charName || 'Alex',
      position: { x: 350, y: 500 },
      first_seen_at: new Date().toISOString(),
    };

    agents.push(mainAgent);

    // 2. Create subtask agents
    for (const [id, subtask] of subtasks.entries()) {
      const subtaskAgent: Agent = {
        id: `subtask:${id}`,
        name: subtask.description,
        session_key: undefined,
        provider: 'anthropic',
        model: 'claude-sonnet-4',
        status: subtask.status === 'working' || subtask.status === 'spawning' ? 'working' : 'idle',
        charName: subtask.charName,
        position: subtask.position,
        first_seen_at: new Date(subtask.createdAt).toISOString(),
        is_subagent: true,
        parent_agent_id: mainAgent.id,
      };
      agents.push(subtaskAgent);
    }

    engineRef.current.updateData(agents, []);

    // Update exclamation marks
    // Main agent
    if (isWaitingForUser) {
      engineRef.current.showExclamationMark(mainAgent.id, true);
    } else {
      engineRef.current.showExclamationMark(mainAgent.id, false);
    }

    // Subtask agents
    for (const [id, subtask] of subtasks.entries()) {
      if (subtask.status === 'completed') {
        engineRef.current.showExclamationMark(`subtask:${id}`, true);
      } else {
        engineRef.current.showExclamationMark(`subtask:${id}`, false);
      }
    }
  }, [agentData, isLoadingData, engineReady, isChatting, isWaitingForUser, subtasks]);

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
