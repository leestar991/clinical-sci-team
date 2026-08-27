import { expect, test } from "@rstest/core";

import {
  MODE_REASONING_EFFORT,
  reasoningEffortForMode,
} from "@/core/threads/types";

test("every input mode maps to a reasoning effort", () => {
  expect(MODE_REASONING_EFFORT).toEqual({
    flash: "minimal",
    thinking: "low",
    pro: "medium",
    ultra: "high",
  });
});

test("flash resolves to minimal rather than being left unset", () => {
  // Regression guard: the two submit paths in hooks.ts used to fall back to
  // `undefined` for flash while the mode picker wrote "minimal". A brand-new
  // thread whose first request was flash therefore sent no effort at all and
  // the provider applied its own default (`high` on DeepSeek).
  expect(reasoningEffortForMode("flash")).toBe("minimal");
});

test("each mode resolves to its own effort", () => {
  expect(reasoningEffortForMode("thinking")).toBe("low");
  expect(reasoningEffortForMode("pro")).toBe("medium");
  expect(reasoningEffortForMode("ultra")).toBe("high");
});

test("an unset mode resolves to the pro default", () => {
  // getResolvedMode() defaults an unset mode to "pro" for thinking-capable
  // models, so the effort fallback has to agree with it.
  expect(reasoningEffortForMode(undefined)).toBe("medium");
});
