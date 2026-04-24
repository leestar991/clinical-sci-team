// Mock Data for Agent Valley

import { Agent, AgentEvent, FetchDataResponse } from '../types';
import { getRandomCharacter } from '../config/constants';

// Mock agents data
const mockAgents: Agent[] = [
  {
    id: 'agent-1',
    name: 'Research Agent',
    session_key: 'thread:research-001',
    provider: 'anthropic',
    model: 'claude-sonnet-4',
    status: 'working',
    charName: 'Alex',
    position: { x: 400, y: 300 },
    first_seen_at: new Date().toISOString(),
  },
  {
    id: 'agent-2',
    name: 'Code Review Agent',
    session_key: 'thread:review-001',
    provider: 'anthropic',
    model: 'claude-opus-4',
    status: 'idle',
    charName: 'Lucy',
    position: { x: 600, y: 400 },
    first_seen_at: new Date(Date.now() - 3600000).toISOString(),
  },
  {
    id: 'subagent:agent-1:task-1',
    name: 'Data Collector',
    parent_agent_id: 'agent-1',
    provider: 'anthropic',
    model: 'claude-sonnet-4',
    status: 'working',
    charName: 'Bob',
    position: { x: 450, y: 350 },
    is_subagent: true,
    subagent_tool_call_id: 'tool-call-1',
    subagent_description: 'Collecting research data',
  },
];

const mockEvents: AgentEvent[] = [
  {
    id: 'event-1',
    agent_id: 'agent-1',
    status: 'running',
    start_time: new Date().toISOString(),
    total_tokens: 1500,
    conversations: [
      {
        role: 'user',
        text: 'Please research the latest AI developments',
        timestamp: new Date().toISOString(),
      },
      {
        role: 'assistant',
        text: 'I will research the latest AI developments for you.',
        timestamp: new Date().toISOString(),
      },
    ],
  },
];

export async function fetchData(): Promise<FetchDataResponse> {
  // Simulate API delay
  await new Promise(resolve => setTimeout(resolve, 100));

  return {
    agents: mockAgents,
    events: mockEvents,
  };
}

export function buildMockHistory(agent: Agent, events: AgentEvent[]) {
  const messages = [];

  events.forEach(event => {
    if (event.conversations) {
      event.conversations.forEach(conv => {
        messages.push({
          id: `${event.id}-${Math.random()}`,
          role: conv.role,
          content: conv.text || conv.content || conv.content_text || '',
          timestamp: new Date(conv.timestamp || event.start_time),
        });
      });
    }
  });

  return messages;
}

export function buildMockAssistantReply(userMessage: string, agent: Agent) {
  return {
    text: `Mock response to: "${userMessage}"`,
    timestamp: new Date().toISOString(),
  };
}

export function saveLastViewed(identity: string) {
  try {
    const stored = localStorage.getItem('agent-town:last-viewed:v1');
    const map = stored ? JSON.parse(stored) : {};
    map[identity] = Date.now();
    localStorage.setItem('agent-town:last-viewed:v1', JSON.stringify(map));
  } catch (e) {
    console.error('Failed to save last viewed:', e);
  }
}
