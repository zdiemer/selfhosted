// Point the gateway's runtime and state dirs at scratch paths BEFORE any
// module reads config.ts. Tests bind a real approval socket, and binding the
// live one would strand the running gateway's in-flight approvals — the socket
// is unlinked on start, and the old process keeps an inode nobody can reach.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const root = fs.mkdtempSync(path.join(os.tmpdir(), "gw-test-"));
process.env.GW_RUNTIME_DIR = path.join(root, "runtime");
process.env.GW_STATE_DIR = path.join(root, "state");
