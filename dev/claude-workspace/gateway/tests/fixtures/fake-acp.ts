// A scripted ACP server, for testing the client without a real agent.
//
// Speaks the wire shape captured from the real ones (muse-acp 0.4.3 and
// gemini-cli 0.60.0 --acp): JSON-RPC 2.0, one message per line, on stdio.
//
// Behaviour is driven by env so one fixture covers every case:
//   FAKE_LOAD_SESSION=0   report loadSession: false, so resume is unavailable
//   FAKE_LOAD_FAILS=1     fail session/load, so the run must fall back to new
//   FAKE_PERMISSION=1     ask for permission before answering
//   FAKE_PERMISSION_OPTS  JSON array of options to offer (default: the usual 4)
//   FAKE_REPLY            the text to answer with (default "hello from fake")
//   FAKE_EXIT_EARLY=1     die right after initialize, to test a dead agent
//   FAKE_NO_CONFIG=1      advertise NO config options at all
//   FAKE_EMPTY_MODELS=1   advertise a model option with an empty option list,
//                         the way muse-acp does before `muse login`
//   FAKE_SET_CONFIG_FAILS=1  reject session/set_config_option
//
// session/load always replays a prior exchange before answering, the way
// codex-acp does, and session/prompt narrates before its tool call — neither
// belongs in the reply.

const REPLAYED = "OLD REPLY FROM HISTORY";
const NARRATION = "Let me look first.";

const enc = (o: unknown) => process.stdout.write(JSON.stringify(o) + "\n");

const loadSession = process.env.FAKE_LOAD_SESSION !== "0";
const loadFails = process.env.FAKE_LOAD_FAILS === "1";
const wantsPermission = process.env.FAKE_PERMISSION === "1";
const reply = process.env.FAKE_REPLY ?? "hello from fake";
const options = process.env.FAKE_PERMISSION_OPTS
  ? JSON.parse(process.env.FAKE_PERMISSION_OPTS)
  : [
      { optionId: "a1", name: "Yes", kind: "allow_once" },
      { optionId: "a2", name: "Yes, always", kind: "allow_always" },
      { optionId: "r1", name: "No", kind: "reject_once" },
      { optionId: "r2", name: "No, never", kind: "reject_always" },
    ];

// What the client answered our permission request with.
let permissionAnswer: { id: number; resolve: (v: unknown) => void } | null =
  null;
const answers = new Map<number, (v: unknown) => void>();
let nextId = 1000;

function askPermission(sessionId: string): Promise<unknown> {
  const id = nextId++;
  return new Promise((resolve) => {
    answers.set(id, resolve);
    enc({
      jsonrpc: "2.0",
      id,
      method: "session/request_permission",
      params: {
        sessionId,
        toolCall: {
          toolCallId: "tc-1",
          title: "Write(/tmp/fake)",
          kind: "edit",
          rawInput: { file_path: "/tmp/fake" },
        },
        options,
      },
    });
  });
}

// Shaped like the real thing: muse-acp 0.4.3 emits `id` (not the spec's
// `configId`), category "model" and category "thought_level".
const noConfig = process.env.FAKE_NO_CONFIG === "1";
const emptyModels = process.env.FAKE_EMPTY_MODELS === "1";
let modelValue = "fake-default";
let effortValue = "medium";

function configOptions() {
  if (noConfig) return undefined;
  return [
    {
      id: "model",
      name: "Model",
      category: "model",
      type: "select",
      currentValue: emptyModels ? "" : modelValue,
      options: emptyModels
        ? []
        : [
            { value: "fake-default", name: "Fake Default" },
            { value: "fake-fast", name: "Fake Fast" },
          ],
    },
    {
      id: "reasoning_effort",
      name: "Reasoning Effort",
      category: "thought_level",
      type: "select",
      currentValue: effortValue,
      options: [{ value: "low" }, { value: "medium" }, { value: "high" }],
    },
  ];
}

/** Every session/set_config_option the client sent, for assertions. */
const setCalls: { configId: string; value: string }[] = [];

let buf = "";
process.stdin.on("data", (d) => {
  buf += d.toString("utf8");
  let nl: number;
  while ((nl = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (line) void handle(JSON.parse(line));
  }
});

async function handle(msg: {
  id?: number;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
}): Promise<void> {
  // A response to our permission request.
  if (msg.id !== undefined && msg.method === undefined) {
    answers.get(msg.id)?.(msg.result);
    answers.delete(msg.id);
    return;
  }

  switch (msg.method) {
    case "initialize":
      enc({
        jsonrpc: "2.0",
        id: msg.id,
        result: {
          protocolVersion: 1,
          agentCapabilities: { loadSession },
          agentInfo: { name: "fake-acp", version: "0" },
        },
      });
      if (process.env.FAKE_EXIT_EARLY === "1") process.exit(3);
      return;

    case "session/new":
      enc({
        jsonrpc: "2.0",
        id: msg.id,
        result: { sessionId: "fake-new-1", configOptions: configOptions() },
      });
      return;

    case "session/set_config_option": {
      const configId = msg.params?.configId as string;
      const value = msg.params?.value as string;
      setCalls.push({ configId, value });
      if (process.env.FAKE_SET_CONFIG_FAILS === "1") {
        enc({
          jsonrpc: "2.0",
          id: msg.id,
          error: { code: -32000, message: `invalid_model: ${value}` },
        });
        return;
      }
      if (configId === "model") modelValue = value;
      else effortValue = value;
      enc({
        jsonrpc: "2.0",
        id: msg.id,
        result: { configOptions: configOptions() },
      });
      return;
    }

    case "session/load":
      if (loadFails) {
        enc({
          jsonrpc: "2.0",
          id: msg.id,
          error: { code: -32000, message: "no such session" },
        });
        return;
      }
      for (const update of [
        { sessionUpdate: "user_message_chunk", content: { type: "text", text: "old question" } },
        { sessionUpdate: "agent_message_chunk", content: { type: "text", text: REPLAYED } },
        { sessionUpdate: "tool_call", toolCallId: "old-tc", title: "Old tool", status: "completed" },
      ])
        enc({
          jsonrpc: "2.0",
          method: "session/update",
          params: { sessionId: msg.params?.sessionId, update },
        });
      enc({
        jsonrpc: "2.0",
        id: msg.id,
        result: { configOptions: configOptions() },
      });
      return;

    case "session/prompt": {
      const sessionId = msg.params?.sessionId as string;
      enc({
        jsonrpc: "2.0",
        method: "session/update",
        params: {
          sessionId,
          update: {
            sessionUpdate: "agent_message_chunk",
            content: { type: "text", text: NARRATION },
          },
        },
      });
      // Echo what we were asked, so a test can assert the prompt text.
      const sent = (msg.params?.prompt as { text?: string }[] | undefined)?.[0]
        ?.text;
      enc({
        jsonrpc: "2.0",
        method: "session/update",
        params: {
          sessionId,
          update: {
            sessionUpdate: "tool_call",
            toolCallId: "tc-1",
            title: "Write(/tmp/fake)",
            kind: "edit",
            status: "pending",
            rawInput: { file_path: "/tmp/fake", prompt: sent },
          },
        },
      });

      let denied = false;
      let permission = "";
      if (wantsPermission) {
        const outcome = (await askPermission(sessionId)) as {
          outcome?: { outcome?: string; optionId?: string };
        };
        permission = `[permission:${outcome?.outcome?.outcome}:${outcome?.outcome?.optionId}]`;
        denied = String(outcome?.outcome?.optionId ?? "").startsWith("r");
      }

      enc({
        jsonrpc: "2.0",
        method: "session/update",
        params: {
          sessionId,
          update: {
            sessionUpdate: "tool_call_update",
            toolCallId: "tc-1",
            status: denied ? "failed" : "completed",
          },
        },
      });
      enc({
        jsonrpc: "2.0",
        method: "session/update",
        params: {
          sessionId,
          update: {
            sessionUpdate: "agent_message_chunk",
            content: {
              type: "text",
              text: `${reply}${permission} [model:${modelValue}][effort:${effortValue}][sets:${setCalls
                .map((c) => `${c.configId}=${c.value}`)
                .join(",")}]`,
            },
          },
        },
      });
      enc({
        jsonrpc: "2.0",
        id: msg.id,
        result: { stopReason: "end_turn" },
      });
      return;
    }

    default:
      if (msg.id !== undefined)
        enc({
          jsonrpc: "2.0",
          id: msg.id,
          error: { code: -32601, message: "unknown" },
        });
  }
}

void permissionAnswer;
