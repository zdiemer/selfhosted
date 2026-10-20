// Stdio MCP server that claude spawns (via --mcp-config) to ask for tool
// permission: --permission-prompt-tool mcp__gw__approve. It forwards the ask
// to the gateway over the approvals unix socket and blocks until a human
// answers on Signal/WhatsApp. The verdict is returned as the JSON string
// payload claude's permission-prompt contract expects.
import net from "node:net";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const socketPath = process.env.GW_SOCKET!;
const chatKey = process.env.GW_CHAT_KEY!;

function askGateway(toolName: string, input: unknown, operation?: "job"): Promise<string> {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(socketPath);
    let buf = "";
    sock.on("connect", () => {
      sock.write(JSON.stringify({ chatKey, toolName, input, operation }) + "\n");
    });
    sock.on("data", (d) => {
      buf += d.toString("utf8");
      const nl = buf.indexOf("\n");
      if (nl >= 0) {
        resolve(buf.slice(0, nl));
        sock.end();
      }
    });
    sock.on("error", reject);
    sock.on("close", () => {
      if (!buf.includes("\n"))
        reject(new Error("gateway closed without a verdict"));
    });
  });
}

const server = new McpServer({ name: "gw", version: "1.0.0" });

const jobCall = async (name: string, input: unknown) => {
  try {
    const text = await askGateway(name, input, "job");
    const result = JSON.parse(text);
    return { content: [{ type: "text" as const, text }], isError: Boolean(result.error) };
  } catch (error) {
    return { content: [{ type: "text" as const, text: String(error) }], isError: true };
  }
};
server.tool("task_list", "List this conversation's tracked CLI subagents/tasks, with short IDs and progress.", {},
  () => jobCall("tasks", {}));
server.tool("task_read", "Inspect a tracked task: original task/session IDs, summary and output path.",
  { id: z.string() }, (input) => jobCall("task", input));
server.tool("job_start",
  "Start a tracked shell job independent of this conversation's lifetime. Requires ordinary shell approval. Returns a stable ID; completion wakes this originating session. Prefer for long builds/tests. Container restarts mark interrupted, never automatically rerun. Use checkpoint for a file the command maintains with progress/resume instructions.",
  { title: z.string().min(1).max(200), command: z.string().min(1).max(32000),
    checkpoint: z.string().optional() },
  (input) => jobCall("start", input));
server.tool("job_list", "List this chat's durable jobs and statuses.", {},
  () => jobCall("list", {}));
server.tool("job_read", "Read a job's status and recent output; inspect its checkpoint before resuming interrupted work.",
  { id: z.string() }, (input) => jobCall("read", input));
server.tool("job_cancel", "Explicitly cancel a durable job and its command process group.",
  { id: z.string() }, (input) => jobCall("cancel", input));

server.tool(
  "approve",
  "Relay a tool-permission request to the user over chat",
  {
    tool_name: z.string(),
    input: z.record(z.string(), z.unknown()).optional(),
    tool_use_id: z.string().optional(),
  },
  async ({ tool_name, input }) => {
    let verdict: string;
    try {
      verdict = await askGateway(tool_name, input ?? {});
    } catch (err) {
      verdict = JSON.stringify({
        behavior: "deny",
        message: `gateway unreachable: ${String(err)}`,
      });
    }
    return { content: [{ type: "text", text: verdict }] };
  },
);

await server.connect(new StdioServerTransport());
