import { expect, test } from "bun:test";
import { trackNativeTasks, nativeTasksFor, interruptNativeTasks, taskAlias, findNativeTask, taskDetails, taskActionPrompt } from "../src/native-tasks.ts";

test("native task history retains completed and interrupted tasks", () => {
  const chat = "native:owner";
  const session = "native-session";
  trackNativeTasks(chat, session, process.pid, {
    type: "system", subtype: "background_tasks_changed",
    tasks: [{ task_id: "agent-1", description: "implementation" }, { task_id: "agent-2" }],
  });
  trackNativeTasks(chat, session, process.pid, {
    type: "system", subtype: "background_tasks_changed", tasks: [{ task_id: "agent-2" }],
  });
  expect(nativeTasksFor(chat).find((t) => t.id === "agent-1")?.state).toBe("awaiting-report");
  trackNativeTasks(chat, session, process.pid, {
    type: "system", subtype: "task_notification", task_id: "agent-1",
    status: "completed", output_file: "/tmp/result",
  });
  expect(interruptNativeTasks(chat, process.pid)).toBe(1);
  const history = nativeTasksFor(chat);
  expect(history.find((t) => t.id === "agent-1")?.state).toBe("completed");
  expect(history.find((t) => t.id === "agent-2")?.state).toBe("interrupted");
  expect(nativeTasksFor("native:someone-else")).toEqual([]);
});

test("task progress keeps stable aliases and origin information through completion", () => {
  const key = "native-progress:owner";
  const base = { type: "system", task_id: "agent-123" };
  trackNativeTasks(key, "origin", process.pid, { ...base, subtype: "task_started",
    description: "Fix parser", task_type: "local_agent" }, { cwd: "/tmp/repo", session: "parser" });
  const task = nativeTasksFor(key)[0];
  const alias = taskAlias(task);
  trackNativeTasks(key, "origin", process.pid, { ...base, subtype: "task_progress",
    last_tool_name: "Bash", usage: { tool_uses: 4, total_tokens: 9000, duration_ms: 120000 } });
  const updated = findNativeTask(key, "@" + alias);
  expect(updated.cwd).toBe("/tmp/repo");
  expect(taskDetails(updated)).toContain("4 tools");
  expect(taskDetails(updated)).toContain("9k tokens");
  expect(taskDetails(updated)).toContain("Bash");
  expect(taskActionPrompt(updated, "cancel")).toContain("Stop only task agent-123");
  expect(() => findNativeTask("native-progress:other", alias)).toThrow();
  trackNativeTasks(key, "origin", process.pid, { ...base, subtype: "task_notification",
    status: "completed", summary: "Parser fixed; tests pass." });
  expect(taskDetails(findNativeTask(key, alias))).toContain("Parser fixed; tests pass.");
});
