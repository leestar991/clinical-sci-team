export interface Subtask {
  id: string;                    // tool_call.id
  name: string;                  // tool_call.name
  description: string;           // 任务描述
  status: 'spawning' | 'working' | 'completed' | 'failed';
  charName: string;              // 分配的角色名称
  position: { x: number; y: number };
  result?: string;               // 完成结果
  error?: string;                // 错误信息
  createdAt: number;             // 创建时间戳
}
