import { expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

test("a killed gateway recovers a parked conversation without another user message", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "gateway-recovery-process-"));
  const src = path.resolve(import.meta.dir, "../src");
  const stateFile = path.join(root, "state/state.json");
  const common = `
    import {handleInbound, resumeInterrupted} from ${JSON.stringify(path.join(src, "router.ts"))};
    import {registerTransport} from ${JSON.stringify(path.join(src, "transport.ts"))};
    import {getChat, updateChat} from ${JSON.stringify(path.join(src, "state.ts"))};
    import {isRunning, isWaiting} from ${JSON.stringify(path.join(src, "claude.ts"))};
    registerTransport("restart-process", {chunkLimit:10000, async send(_key,text){console.log(text);return 1;}});
    const key="restart-process:owner";
  `;
  const env = {
    ...process.env, GW_STATE_DIR: path.join(root, "state"), GW_RUNTIME_DIR: path.join(root, "runtime"),
    PATH: `${path.join(import.meta.dir, "fixtures/fake-claude-bin")}:${process.env.PATH}`,
    FAKE_CLAUDE_LOG: path.join(root, "launches.log"),
  };
  const first = Bun.spawn(["bun", "-e", common + `
    updateChat(key,{cwd:${JSON.stringify(root)}});
    handleInbound(key,"Continue the background investigation",{sender:"test",allowed:true,owner:true,mentioned:true});
    setInterval(()=>{},1000);
  `], { env: { ...env, FAKE_CLAUDE_PARK: "1" }, stdout: "ignore", stderr: "ignore" });
  try {
    const deadline = Date.now() + 3000;
    while (true) {
      const state = fs.existsSync(stateFile) ? JSON.parse(fs.readFileSync(stateFile, "utf8")) : {};
      if (state["restart-process:owner"]?.recovery?.sessionId &&
          state["restart-process:owner"]?.recovery?.pendingUserReply === false) break;
      if (Date.now() > deadline) throw new Error("first gateway did not park");
      await Bun.sleep(10);
    }
    first.kill("SIGKILL");
    await first.exited;
    const second = Bun.spawn(["bun", "-e", common + `
      resumeInterrupted("restart-process");
      const end=Date.now()+3000;
      while(getChat(key).recovery || isRunning(key)){
        if(Date.now()>end) throw new Error("recovery did not finish");
        await Bun.sleep(10);
      }
      console.log("RECOVERED");
    `], { env: { ...env, FAKE_CLAUDE_PARK: "0" }, stdout: "pipe", stderr: "pipe" });
    const output = await new Response(second.stdout).text();
    expect(await second.exited).toBe(0);
    expect(output).toContain("RECOVERED");
    expect(output).toContain("(resume fake-ses)");
    expect(fs.readFileSync(path.join(root, "launches.log"), "utf8").trim().split("\n").length).toBe(2);
    expect(JSON.parse(fs.readFileSync(stateFile, "utf8"))["restart-process:owner"].recovery).toBeUndefined();
  } finally {
    first.kill();
    await first.exited;
  }
});
