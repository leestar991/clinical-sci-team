'use client';

import { useEffect, useState } from 'react';
import { buildMockHistory } from '../data/mockData';
import type { AgentData } from '../types';

interface AgentCardProps {
  data: AgentData;
  onClose: () => void;
}

export default function AgentCard({ data, onClose }: AgentCardProps) {
  console.log('[AgentCard] Rendering with data:', data);
  const { agent, charName, state, events = [] } = data;
  const [messages, setMessages] = useState<any[]>([]);

  useEffect(() => {
    console.log('[AgentCard] Loading history for agent:', agent.name);
    const history = buildMockHistory(agent, events);
    console.log('[AgentCard] History loaded:', history.length, 'messages');
    setMessages(history);
  }, [agent, events]);

  return (
    <div
      className="card-overlay"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="agent-card"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          className="agent-card-close"
          onClick={onClose}
        >
          CLOSE
        </button>

        <div className="agent-card-content">
          <div className="agent-card-header">
            <h2 className="agent-card-name">{agent.name}</h2>

            <div className="agent-card-meta">
              <span className={`agent-card-badge status-${state}`}>
                {state.toUpperCase()}
              </span>
              {agent.model && (
                <span className="agent-card-badge model">
                  {agent.model}
                </span>
              )}
            </div>

            {agent.session_key && (
              <div className="agent-card-session">
                Session: {agent.session_key}
              </div>
            )}
          </div>

          <div className="agent-card-section">
            <h3 className="agent-card-section-title">Conversation History</h3>

            {messages.length === 0 ? (
              <div className="agent-card-empty">
                No conversation history available
              </div>
            ) : (
              <div className="agent-card-messages">
                {messages.map((msg, idx) => (
                  <div
                    key={idx}
                    className={`agent-card-message role-${msg.role}`}
                  >
                    <div className="agent-card-message-role">
                      {msg.role}
                    </div>
                    <div className="agent-card-message-content">
                      {msg.content}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
