import { useEffect, useState, useRef } from 'react';
import type { Message } from '@langchain/langgraph-sdk';
import type { Subtask } from '../types/subtask';
import { PositionAllocator } from '../utils/positionAllocator';
import { CharacterAllocator } from '../utils/characterAllocator';

interface UseSubtasksOptions {
  sceneW?: number;
  sceneH?: number;
}

export function useSubtasks(
  messages: Message[] | undefined,
  options: UseSubtasksOptions = {}
) {
  const { sceneW = 896, sceneH = 640 } = options;

  const [subtasks, setSubtasks] = useState<Map<string, Subtask>>(new Map());
  const positionAllocatorRef = useRef(new PositionAllocator());
  const characterAllocatorRef = useRef(new CharacterAllocator());

  useEffect(() => {
    console.log('[useSubtasks] Messages received:', messages?.length || 0);

    if (!messages || messages.length === 0) {
      console.log('[useSubtasks] No messages, skipping');
      return;
    }

    const newSubtasks = new Map<string, Subtask>();
    const existingSubtasks = subtasks;

    console.log('[useSubtasks] Processing', messages.length, 'messages');

    // 遍历所有消息，识别子任务
    for (const message of messages) {
      // 1. 识别新的子任务（AI 消息中的 tool_calls）
      if (message.type === 'ai' && message.tool_calls) {
        console.log('[useSubtasks] Found AI message with tool_calls:', message.tool_calls.length);

        for (const toolCall of message.tool_calls) {
          console.log('[useSubtasks] Tool call:', {
            name: toolCall.name,
            id: toolCall.id,
            args: toolCall.args,
          });

          // 只处理 name === 'task' 的工具调用
          if (toolCall.name === 'task' && toolCall.id) {
            const taskId = toolCall.id;

            console.log('[useSubtasks] Found task tool call:', taskId);

            // 如果子任务已存在，保留它
            if (existingSubtasks.has(taskId)) {
              newSubtasks.set(taskId, existingSubtasks.get(taskId)!);
              console.log('[useSubtasks] Reusing existing subtask:', taskId);
            } else {
              // 创建新子任务
              const description = String(toolCall.args?.description || toolCall.args?.prompt || 'Subtask');
              const charName = characterAllocatorRef.current.allocate();
              const position = positionAllocatorRef.current.allocate(sceneW, sceneH);

              const subtask: Subtask = {
                id: taskId,
                name: toolCall.name,
                description: description.substring(0, 50), // 限制长度
                status: 'spawning',
                charName,
                position,
                createdAt: Date.now(),
              };

              console.log('[useSubtasks] ✅ Created new subtask:', subtask);
              newSubtasks.set(taskId, subtask);
            }
          }
        }
      }

      // 2. 更新子任务状态（Tool 消息）
      if (message.type === 'tool' && message.tool_call_id) {
        const taskId = message.tool_call_id;
        const existingTask = newSubtasks.get(taskId) || existingSubtasks.get(taskId);

        if (existingTask) {
          const content = typeof message.content === 'string'
            ? message.content
            : JSON.stringify(message.content);

          let status: Subtask['status'] = 'working';
          let result: string | undefined;
          let error: string | undefined;

          // 解析结果
          if (content.includes('Task Succeeded') || content.includes('succeeded')) {
            status = 'completed';
            result = content.split('Result:')[1]?.trim() || content;
          } else if (content.includes('Task failed') || content.includes('failed') || content.includes('error')) {
            status = 'failed';
            error = content;
          } else if (content.includes('timed out') || content.includes('timeout')) {
            status = 'failed';
            error = 'Task timed out';
          } else {
            status = 'working';
          }

          const updatedTask: Subtask = {
            ...existingTask,
            status,
            result,
            error,
          };

          newSubtasks.set(taskId, updatedTask);
        }
      }
    }

    // 只在有变化时更新状态
    if (newSubtasks.size !== subtasks.size ||
        Array.from(newSubtasks.values()).some((task, i) => {
          const oldTask = Array.from(subtasks.values())[i];
          return !oldTask || task.status !== oldTask.status;
        })) {
      console.log('[useSubtasks] 🔄 Updating subtasks, count:', newSubtasks.size);
      setSubtasks(newSubtasks);
    } else {
      console.log('[useSubtasks] No changes, keeping existing subtasks');
    }
  }, [messages, sceneW, sceneH]);

  return { subtasks };
}
