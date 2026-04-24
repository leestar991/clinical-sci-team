'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { QueryClientProvider } from '@/components/query-client-provider';
import { SidebarProvider } from '@/components/ui/sidebar';
import { ArtifactsProvider } from '@/components/workspace/artifacts/context';
import GameCanvas from './GameCanvas';
import Tooltip from './Tooltip';
import ChatEmbed from './ChatEmbed';
import { SubtaskDialog } from './SubtaskDialog';
import { DEFAULT_MAP_CONFIG, MAP_VARIANTS } from '../config/constants';
import { fetchAgentValleyData, type AgentValleyData } from '../data/realData';
import { useSubtasks } from '../hooks/useSubtasks';
import type { Agent } from '../types';
import type { Subtask } from '../types/subtask';
import '../styles/agent-valley.css';

const UI_W = 28 * 32;
const UI_H = 20 * 32;
const MAP_TOP_GAP = 54;
const MAP_SCALE_BOOST = 1.8;

export default function AgentValleyApp() {
  const [tooltip, setTooltip] = useState<any>(null);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [selectedSubtask, setSelectedSubtask] = useState<Subtask | null>(null);
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [activeMapId, setActiveMapId] = useState(DEFAULT_MAP_CONFIG.id);
  const [cursorState, setCursorState] = useState('normal');
  const [canvasRefreshTrigger, setCanvasRefreshTrigger] = useState(0);
  const layoutRef = useRef<HTMLDivElement>(null);
  const gameEngineRef = useRef<any>(null);
  const [mapSize, setMapSize] = useState<{ w: number; h: number } | null>(null);
  const [agentData, setAgentData] = useState<AgentValleyData | null>(null);
  const [isLoadingData, setIsLoadingData] = useState(true);
  const [isChatting, setIsChatting] = useState(false); // Track if AI is responding
  const [isWaitingForUser, setIsWaitingForUser] = useState(false); // Track if waiting for user input
  const [viewport, setViewport] = useState(() => ({
    w: typeof window !== 'undefined' ? window.innerWidth : UI_W,
    h: typeof window !== 'undefined' ? window.innerHeight : UI_H,
  }));

  // Use subtasks hook to get subtasks from thread messages
  const { subtasks } = useSubtasks(agentData?.thread?.values?.messages, {
    sceneW: 896,
    sceneH: 640,
  });

  // Fetch real agent data
  useEffect(() => {
    let mounted = true;

    const loadData = async () => {
      console.log('[AgentValleyApp] Loading data...');
      setIsLoadingData(true);
      const data = await fetchAgentValleyData();
      console.log('[AgentValleyApp] Data loaded:', data);
      if (mounted) {
        setAgentData(data);
        setIsLoadingData(false);
      }
    };

    // Load data immediately
    loadData();

    // Refresh every 5 seconds for real-time subtask updates
    const interval = setInterval(() => {
      loadData();
    }, 5000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    const el = layoutRef.current;
    if (!el) return;
    const obs = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      if (width > 0 && height > 0) setViewport({ w: width, h: height });
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  const handleLayoutChange = useCallback(({ sceneW, sceneH }: { sceneW: number; sceneH: number }) => {
    if (sceneW > 0 && sceneH > 0) {
      setMapSize({ w: sceneW, h: sceneH });
    }
  }, []);

  const activeMapConfig = useMemo(
    () => MAP_VARIANTS.find((map) => map.id === activeMapId) || DEFAULT_MAP_CONFIG,
    [activeMapId]
  );

  const handleNpcHover = useCallback((data: any, pos: any) => {
    setTooltip({ data, pos });
  }, []);

  const handleNpcLeave = useCallback(() => {
    setTooltip(null);
  }, []);

  const handleNpcClick = useCallback((data: any) => {
    // Extract npcId from the data
    const npcId = data?.agent?.id || data?.id;

    if (!npcId) {
      return;
    }

    // Check if it's a subtask
    if (typeof npcId === 'string' && npcId.startsWith('subtask:')) {
      const subtaskId = npcId.replace('subtask:', '');
      const subtask = subtasks.get(subtaskId);
      if (subtask) {
        setSelectedSubtask(subtask);

        // Hide exclamation mark when user views the subtask result
        if (gameEngineRef.current) {
          gameEngineRef.current.showExclamationMark(npcId, false);
        }
      }
    } else {
      // Main agent: open chat dialog
      if (agentData?.thread?.thread_id) {
        setSelectedThreadId(agentData.thread.thread_id);
      }
    }
  }, [agentData, subtasks, gameEngineRef]);

  const handleCursorStateChange = useCallback((nextState: string) => {
    setCursorState(nextState || 'normal');
  }, []);

  const handleCloseChat = useCallback(() => {
    setSelectedThreadId(null);

    // Don't reset chatting state if AI is still responding
    if (!isChatting) {
      setIsWaitingForUser(false);
    }
  }, [isChatting, isWaitingForUser]);

  const handleChatStatusChange = useCallback((isLoading: boolean) => {
    setIsChatting(isLoading);
  }, []);

  const handleWaitingForUser = useCallback((isWaiting: boolean) => {
    setIsWaitingForUser(isWaiting);
  }, []);

  const mapBaseW = mapSize?.w ?? UI_W;
  const mapBaseH = mapSize?.h ?? UI_H;
  const totalH = mapBaseH * MAP_SCALE_BOOST + MAP_TOP_GAP + 24;
  const scale = Math.min(
    viewport.w / (mapBaseW * MAP_SCALE_BOOST),
    viewport.h / totalH,
  );
  const mapW = Math.round(mapBaseW * scale * MAP_SCALE_BOOST);
  const mapH = Math.round(mapBaseH * scale * MAP_SCALE_BOOST);
  const topGap = Math.round(MAP_TOP_GAP * scale);

  useEffect(() => {
    setMapSize(null);
  }, [activeMapConfig.id]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement).tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      if ((event.target as HTMLElement).isContentEditable) return;
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      if (event.key === 'Escape') {
        setConsoleOpen(false);
        setSelectedThreadId(null);
        return;
      }
      if (event.key === 'c' || event.key === 'C') { setConsoleOpen((v) => !v); return; }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  return (
    <QueryClientProvider>
      <SidebarProvider>
        <ArtifactsProvider>
          <div
            className={`app-layout agent-town-cursor cursor-${cursorState}`}
            data-map-theme={activeMapConfig.id}
            ref={layoutRef}
          >
      <nav className="town-hud">
        <button
          type="button"
          className={`town-hud-btn town-hud-console ${consoleOpen ? 'is-on' : ''}`}
          onClick={() => setConsoleOpen((prev) => !prev)}
          title={`${consoleOpen ? 'Close' : 'Open'} Console  [C]`}
        >
          <span className="town-hud-pip" />
          <span className="town-hud-icon">☰</span>
          <span className="town-hud-label">CMD</span>
        </button>
      </nav>

      <div
        className="content-stack"
        style={{
          width: mapW,
          height: topGap + mapH,
          alignItems: 'center',
        }}
      >
        <div style={{ height: topGap }} />
        <div style={{ width: mapW, height: mapH, position: 'relative', zIndex: 1 }}>
          <GameCanvas
            key={activeMapConfig.id}
            onNpcHover={handleNpcHover}
            onNpcLeave={handleNpcLeave}
            onNpcClick={handleNpcClick}
            onCursorStateChange={handleCursorStateChange}
            onLayoutChange={handleLayoutChange}
            mapConfig={activeMapConfig}
            refreshTrigger={canvasRefreshTrigger}
            gameEngineRef={gameEngineRef}
            agentData={agentData}
            isLoadingData={isLoadingData}
            isChatting={isChatting}
            isWaitingForUser={isWaitingForUser}
          />
        </div>
      </div>

      <Tooltip data={tooltip} />

      {selectedThreadId && (
        <ChatEmbed
          threadId={selectedThreadId}
          onClose={handleCloseChat}
          onChatStatusChange={handleChatStatusChange}
          onWaitingForUser={handleWaitingForUser}
        />
      )}

      {selectedSubtask && (
        <SubtaskDialog
          subtask={selectedSubtask}
          onClose={() => setSelectedSubtask(null)}
        />
      )}

      {consoleOpen && (
        <div className="console-overlay">
          <div className="console-panel">
            <div className="console-header">
              <span>Agent Valley Console</span>
              <button onClick={() => setConsoleOpen(false)}>×</button>
            </div>
            <div className="console-content">
              {agentData?.thread ? (
                <>
                  <div className="console-line">
                    <span className="console-label">Thread ID:</span>
                    <span className="console-value">{agentData.thread.thread_id}</span>
                  </div>
                  <div className="console-line">
                    <span className="console-label">Agent:</span>
                    <span className="console-value">{agentData.agentName}</span>
                  </div>
                  <div className="console-line">
                    <span className="console-label">Character:</span>
                    <span className="console-value">{agentData.charName}</span>
                  </div>
                  <div className="console-line">
                    <span className="console-label">Updated:</span>
                    <span className="console-value">
                      {new Date(agentData.thread.updated_at).toLocaleString()}
                    </span>
                  </div>
                </>
              ) : (
                <div className="console-line">No active thread</div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
    </ArtifactsProvider>
    </SidebarProvider>
    </QueryClientProvider>
  );
}
