import type { Message, Thread } from "@langchain/langgraph-sdk";

import type { Todo } from "../todos";

export interface GoalState {
  objective: string;
  status: "active";
  created_at: string;
  updated_at: string;
  continuation_count: number;
  max_continuations: number;
  no_progress_count: number;
  max_no_progress_continuations: number;
  last_evaluation?: {
    satisfied: boolean;
    blocker:
      | "none"
      | "missing_evidence"
      | "needs_user_input"
      | "run_failed"
      | "external_wait"
      | "goal_not_met_yet";
    reason: string;
    evidence_summary?: string;
    run_id?: string;
    evaluated_at?: string;
    progress_key?: string;
    stand_down_reason?: string;
  };
}

export interface AgentThreadState extends Record<string, unknown> {
  title: string;
  messages: Message[];
  artifacts?: string[];
  todos?: Todo[];
  goal?: GoalState | null;
}

export type InputMode = "flash" | "thinking" | "pro" | "ultra";

export type ReasoningEffort = "minimal" | "low" | "medium" | "high";

/**
 * The reasoning depth each input mode implies.
 *
 * Kept here as the single source of truth because three call sites need it: the
 * mode picker in `input-box.tsx` and both submit paths in `hooks.ts`. They used
 * to inline the same ternary chain, which is how `flash` came to mean `minimal`
 * in one place and `undefined` in the others — leaving a brand-new thread's
 * first flash request with no effort at all, so the provider applied its own
 * default (`high` on DeepSeek).
 */
export const MODE_REASONING_EFFORT: Record<InputMode, ReasoningEffort> = {
  flash: "minimal",
  thinking: "low",
  pro: "medium",
  ultra: "high",
};

export function reasoningEffortForMode(
  mode: InputMode | undefined,
): ReasoningEffort {
  return MODE_REASONING_EFFORT[mode ?? "pro"];
}

export interface AgentThreadContext extends Record<string, unknown> {
  thread_id: string;
  model_name: string | undefined;
  thinking_enabled: boolean;
  is_plan_mode: boolean;
  subagent_enabled: boolean;
  reasoning_effort?: ReasoningEffort;
  agent_name?: string;
}

export interface AgentThread extends Thread<AgentThreadState> {
  context?: AgentThreadContext;
}

export interface RunMessage {
  run_id: string;
  seq?: number;
  content: Message;
  metadata: {
    caller: string;
    [key: string]: unknown;
  };
  created_at: string;
}

export interface ThreadTokenUsageResponse {
  thread_id: string;
  total_tokens: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_runs: number;
  by_model: Record<string, { tokens: number; runs: number }>;
  by_caller: {
    lead_agent: number;
    subagent: number;
    middleware: number;
  };
}
