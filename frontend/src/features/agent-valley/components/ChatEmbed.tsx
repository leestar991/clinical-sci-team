'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { type PromptInputMessage, PromptInputProvider } from '@/components/ai-elements/prompt-input';
import { ArtifactTrigger } from '@/components/workspace/artifacts';
import { ChatBox } from '@/components/workspace/chats';
import { ExportTrigger } from '@/components/workspace/export-trigger';
import { InputBox } from '@/components/workspace/input-box';
import {
  MessageList,
  MESSAGE_LIST_DEFAULT_PADDING_BOTTOM,
  MESSAGE_LIST_FOLLOWUPS_EXTRA_PADDING_BOTTOM,
} from '@/components/workspace/messages';
import { ThreadContext } from '@/components/workspace/messages/context';
import { ThreadTitle } from '@/components/workspace/thread-title';
import { TodoList } from '@/components/workspace/todo-list';
import { TokenUsageIndicator } from '@/components/workspace/token-usage-indicator';
import { useI18n } from '@/core/i18n/hooks';
import { useModels } from '@/core/models/hooks';
import { useNotification } from '@/core/notification/hooks';
import { useThreadSettings } from '@/core/settings';
import { useThreadStream } from '@/core/threads/hooks';
import { textOfMessage } from '@/core/threads/utils';
import { env } from '@/env';
import { cn } from '@/lib/utils';

interface ChatEmbedProps {
  threadId: string;
  onClose: () => void;
  onChatStatusChange?: (isLoading: boolean) => void;
  onWaitingForUser?: (isWaiting: boolean) => void;
}

export default function ChatEmbed({ threadId, onClose, onChatStatusChange, onWaitingForUser }: ChatEmbedProps) {
  const { t } = useI18n();
  const [showFollowups, setShowFollowups] = useState(false);
  const [settings, setSettings] = useThreadSettings(threadId);
  const [mounted, setMounted] = useState(false);
  const { tokenUsageEnabled } = useModels();

  useEffect(() => {
    setMounted(true);
  }, []);

  const { showNotification } = useNotification();

  const [thread, sendMessage, isUploading] = useThreadStream({
    threadId,
    context: settings.context,
    isMock: false,
    onFinish: (state) => {
      // Update chat status
      onChatStatusChange?.(false);

      // Find the last assistant message (not tool message)
      const lastAssistantMessage = state.messages
        .slice()
        .reverse()
        .find(msg => msg.role === 'assistant' || msg.type === 'ai');

      // Always show exclamation mark when AI finishes responding
      if (lastAssistantMessage) {
        onWaitingForUser?.(true);
      } else {
        // Show exclamation mark anyway if there are messages
        if (state.messages.length > 0) {
          onWaitingForUser?.(true);
        }
      }

      if (document.hidden || !document.hasFocus()) {
        let body = 'Conversation finished';
        const lastMessage = state.messages.at(-1);
        if (lastMessage) {
          const textContent = textOfMessage(lastMessage);
          if (textContent) {
            body =
              textContent.length > 200
                ? textContent.substring(0, 200) + '...'
                : textContent;
          }
        }
        showNotification(state.title, { body });
      }
    },
  });

  // Track previous isLoading state to detect transitions
  const prevIsLoadingRef = useRef(thread.isLoading);
  const userHasSentMessageRef = useRef(false); // Track if user has sent a message in this session

  // Monitor thread streaming status
  useEffect(() => {
    const wasLoading = prevIsLoadingRef.current;
    const isLoading = thread.isLoading;
    const messagesCount = thread.messages.length;

    console.log('[ChatEmbed] Thread state changed:', {
      wasLoading,
      isLoading,
      messagesCount,
      userHasSentMessage: userHasSentMessageRef.current,
      transition: wasLoading !== isLoading ? (isLoading ? 'started' : 'finished') : 'no change',
    });

    // Update ref for next comparison
    prevIsLoadingRef.current = isLoading;

    // Only trigger chat status change if user has sent a message
    // This prevents triggering on initial history load
    if (isLoading && userHasSentMessageRef.current) {
      // AI is responding to user's message
      console.log('[ChatEmbed] Stream is loading (AI is responding to user)');
      onChatStatusChange?.(true);
      onWaitingForUser?.(false);
    } else if (wasLoading && !isLoading && messagesCount > 0 && userHasSentMessageRef.current) {
      // Stream just finished (transition from loading to not loading)
      console.log('[ChatEmbed] Stream just finished (wasLoading=true, isLoading=false)');
      onChatStatusChange?.(false);

      // Check if last message is from assistant (AI finished responding)
      const lastMessage = thread.messages[messagesCount - 1];
      console.log('[ChatEmbed] Last message:', {
        role: lastMessage?.role,
        type: lastMessage?.type,
        content: lastMessage?.content?.substring(0, 50),
      });

      if (lastMessage?.role === 'assistant' || lastMessage?.type === 'ai') {
        console.log('[ChatEmbed] ✅ AI finished responding, waiting for user input');
        onWaitingForUser?.(true);
      } else {
        console.log('[ChatEmbed] ❌ Last message is not from assistant, role:', lastMessage?.role);
      }
    } else if (isLoading && !userHasSentMessageRef.current) {
      console.log('[ChatEmbed] 📚 Loading history, not triggering chat status change');
    }
  }, [thread.isLoading, thread.messages.length, onChatStatusChange, onWaitingForUser]);

  const handleSubmit = useCallback(
    (message: PromptInputMessage) => {
      // Mark that user has sent a message
      userHasSentMessageRef.current = true;

      // Hide exclamation mark when user sends a message
      onWaitingForUser?.(false);

      // Set to loading immediately
      onChatStatusChange?.(true);

      void sendMessage(threadId, message);
    },
    [sendMessage, threadId, onWaitingForUser, onChatStatusChange],
  );

  const handleStop = useCallback(async () => {
    await thread.stop();
  }, [thread]);

  const messageListPaddingBottom = showFollowups
    ? MESSAGE_LIST_DEFAULT_PADDING_BOTTOM +
      MESSAGE_LIST_FOLLOWUPS_EXTRA_PADDING_BOTTOM
    : undefined;

  return (
    <div className="card-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="agent-card-chat-embed" onClick={(e) => e.stopPropagation()}>
        <button className="agent-card-close" onClick={onClose}>
          CLOSE
        </button>

        <div className="chat-embed-content">
          <PromptInputProvider>
            <ThreadContext.Provider value={{ thread, isMock: false }}>
              <ChatBox threadId={threadId}>
                <div className="relative flex size-full min-h-0 justify-between">
                  <header className="absolute top-0 right-0 left-0 z-30 flex h-12 shrink-0 items-center px-4 bg-background/80 shadow-xs backdrop-blur">
                    <div className="flex w-full items-center text-sm font-medium">
                      <ThreadTitle threadId={threadId} thread={thread} />
                    </div>
                    <div className="flex items-center gap-2">
                      <TokenUsageIndicator
                        enabled={tokenUsageEnabled}
                        messages={thread.messages}
                      />
                      <ExportTrigger threadId={threadId} />
                      <ArtifactTrigger />
                    </div>
                  </header>
                  <main className="flex min-h-0 max-w-full grow flex-col">
                    <div className="flex size-full justify-center">
                      <MessageList
                        className="size-full pt-10"
                        threadId={threadId}
                        thread={thread}
                        paddingBottom={messageListPaddingBottom}
                        tokenUsageEnabled={tokenUsageEnabled}
                      />
                    </div>
                    <div className="absolute right-0 bottom-0 left-0 z-30 flex justify-center px-4">
                      <div className="relative w-full max-w-(--container-width-md)">
                        <div className="absolute -top-4 right-0 left-0 z-0">
                          <div className="absolute right-0 bottom-0 left-0">
                            <TodoList
                              className="bg-background/5"
                              todos={thread.values.todos ?? []}
                              hidden={
                                !thread.values.todos || thread.values.todos.length === 0
                              }
                            />
                          </div>
                        </div>
                        {mounted ? (
                          <InputBox
                            className="bg-background/5 w-full -translate-y-4"
                            isNewThread={false}
                            threadId={threadId}
                            autoFocus={false}
                            status={
                              thread.error
                                ? 'error'
                                : thread.isLoading
                                  ? 'streaming'
                                  : 'ready'
                            }
                            context={settings.context}
                            disabled={
                              env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === 'true' ||
                              isUploading
                            }
                            onContextChange={(context) =>
                              setSettings('context', context)
                            }
                            onFollowupsVisibilityChange={setShowFollowups}
                            onSubmit={handleSubmit}
                            onStop={handleStop}
                          />
                        ) : (
                          <div
                            aria-hidden="true"
                            className="bg-background/5 h-32 w-full -translate-y-4 rounded-2xl border"
                          />
                        )}
                        {env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === 'true' && (
                          <div className="text-muted-foreground/67 w-full translate-y-12 text-center text-xs">
                            {t.common.notAvailableInDemoMode}
                          </div>
                        )}
                      </div>
                    </div>
                  </main>
                </div>
              </ChatBox>
            </ThreadContext.Provider>
          </PromptInputProvider>
        </div>
      </div>
    </div>
  );
}
