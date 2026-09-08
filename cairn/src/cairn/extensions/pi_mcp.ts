import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

export default async function (pi: any) {
  const child = spawn(process.env.CAIRN_PYTHON!, ["-m", "cairn.extensions.mcp_proxy", "--json-lines"], {
    cwd: process.cwd(), env: process.env, stdio: ["pipe", "pipe", "inherit"], windowsHide: true,
  });
  let nextId = 0;
  let closed = false;
  const pending = new Map<number, {resolve: Function, reject: Function, timer: any}>();
  const fail = (message: string) => {
    closed = true;
    for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(new Error(message)); }
    pending.clear();
  };
  child.on("error", () => fail("Unable to start Cairn MCP bridge"));
  child.on("exit", () => fail("Cairn MCP bridge exited"));
  child.stdin.on("error", () => fail("Cairn MCP bridge input closed"));
  createInterface({input: child.stdout}).on("line", line => {
    try {
      const result = JSON.parse(line), entry = pending.get(result.id);
      if (!entry) return;
      pending.delete(result.id); clearTimeout(entry.timer);
      if (result.error) entry.reject(new Error(result.error)); else entry.resolve(result.result);
    } catch { fail("Invalid MCP bridge response"); }
  });
  const request = (payload: any): Promise<any> => new Promise((resolve, reject) => {
    if (closed) { reject(new Error("Cairn MCP bridge is closed")); return; }
    const id = ++nextId;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("MCP request timed out")); }, 90000);
    pending.set(id, {resolve, reject, timer});
    child.stdin.write(JSON.stringify({id, ...payload}) + "\n");
  });
  const shutdown = () => { child.stdin.end(); fail("MCP session closed"); };
  pi.on("session_shutdown", shutdown);
  try {
    const tools = await request({op: "list"});
    for (const tool of tools) {
      pi.registerTool({
        name: tool.name, label: tool.description?.split("\n")[0] || tool.name,
        description: tool.description || tool.name, parameters: tool.inputSchema,
        async execute(_id: string, args: any) {
          const result = await request({op: "call", name: tool.name, arguments: args});
          if (result.isError) {
            throw new Error(result.content.filter((item: any) => item.type === "text").map((item: any) => item.text).join("\n") || "MCP tool returned an error");
          }
          // Preserve text/image tool content and expose structured MCP results.
          return {content: result.content.filter((item: any) => ["text", "image"].includes(item.type)),
                  details: {isError: !!result.isError, structuredContent: result.structuredContent}};
        },
      });
    }
  } catch (error) { shutdown(); throw error; }
}
