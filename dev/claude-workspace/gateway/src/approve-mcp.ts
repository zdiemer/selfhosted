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

function askGateway(toolName: string, input: unknown): Promise<string> {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(socketPath);
    let buf = "";
    sock.on("connect", () => {
      sock.write(JSON.stringify({ chatKey, toolName, input }) + "\n");
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
