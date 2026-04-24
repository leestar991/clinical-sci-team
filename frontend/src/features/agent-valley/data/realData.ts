import { getAPIClient } from '@/core/api';
import type { AgentThread, AgentThreadState } from '@/core/threads/types';

export async function fetchLatestThread(): Promise<AgentThread | null> {
  try {
    console.log('[fetchLatestThread] Fetching latest thread...');
    const apiClient = getAPIClient();
    const response = await apiClient.threads.search<AgentThreadState>({
      limit: 1,
      sortBy: 'updated_at',
      sortOrder: 'desc',
      select: ['thread_id', 'updated_at', 'values', 'metadata'],
    });

    console.log('[fetchLatestThread] Response:', response);
    if (response && response.length > 0) {
      const thread = response[0];
      console.log('[fetchLatestThread] Found thread:', thread.thread_id);
      console.log('[fetchLatestThread] Thread values:', thread.values);
      console.log('[fetchLatestThread] Messages count:', thread.values?.messages?.length || 0);

      // Log tool_calls in messages
      if (thread.values?.messages) {
        const messagesWithToolCalls = thread.values.messages.filter((msg: any) =>
          msg.type === 'ai' && msg.tool_calls && msg.tool_calls.length > 0
        );
        console.log('[fetchLatestThread] Messages with tool_calls:', messagesWithToolCalls.length);
        messagesWithToolCalls.forEach((msg: any) => {
          console.log('[fetchLatestThread] Tool calls:', msg.tool_calls);
        });
      }

      return thread;
    }
    console.log('[fetchLatestThread] No threads found');
    return null;
  } catch (error) {
    console.error('[fetchLatestThread] Error:', error);
    return null;
  }
}

export interface AgentValleyData {
  thread: AgentThread | null;
  agentName: string;
  charName: string;
}

export async function fetchAgentValleyData(): Promise<AgentValleyData> {
  const thread = await fetchLatestThread();

  if (!thread) {
    return {
      thread: null,
      agentName: 'AI Assistant',
      charName: 'Alex',
    };
  }

  // Extract agent name from metadata or use default
  const agentName = thread.metadata?.agent_name || thread.values?.title || 'AI Assistant';

  // Map agent name to character
  const charName = getCharacterForAgent(agentName);

  return {
    thread,
    agentName,
    charName,
  };
}

function getCharacterForAgent(agentName: string): string {
  // Map different agent types to characters
  const lowerName = agentName.toLowerCase();

  if (lowerName.includes('research') || lowerName.includes('analyst')) {
    return 'Alex';
  } else if (lowerName.includes('code') || lowerName.includes('developer')) {
    return 'Bob';
  } else if (lowerName.includes('design') || lowerName.includes('creative')) {
    return 'Lucy';
  } else if (lowerName.includes('data') || lowerName.includes('science')) {
    return 'Adam';
  } else {
    return 'Alex'; // Default
  }
}
