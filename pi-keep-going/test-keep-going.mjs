// Run: node test-keep-going.mjs   (Node 22.6+ strips the TypeScript types itself)
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
// self-contained: a temp config file, never your real ~/.pi/agent/keep-going.json
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "keep-going-test-"));
process.env.KEEP_GOING_CONFIG = path.join(tmp, "keep-going.json");
const m = await import(new URL("./keep-going.ts", import.meta.url).href);
const cfg = { enabled: true, max: 2, message: "continue" };
const ok = { outcome: "completed", continue: false, assistantText: "I fixed part A, next is B." };
let n = 0; const t = (name, got, want) => { assert.deepEqual(got, want, name); n++; };
t("continue on plain stop", m.decide(cfg, 0, ok, "tui"), { action: "continue" });
t("continue in rpc/desktop", m.decide(cfg, 0, ok, "rpc"), { action: "continue" });
t("off", m.decide({ ...cfg, enabled: false }, 0, ok, "tui").reason, "off");
t("print mode", m.decide(cfg, 0, ok, "print").reason, "non-interactive");
t("aborted", m.decide(cfg, 0, { ...ok, outcome: "aborted" }, "tui").reason, "aborted");
t("error still continues", m.decide(cfg, 0, { ...ok, outcome: "error" }, "tui"), { action: "continue" });
t("already continuing", m.decide(cfg, 0, { ...ok, continue: true }, "tui").reason, "already continuing");
t("question", m.decide(cfg, 0, { ...ok, assistantText: "Should I use A or B?  \n" }, "tui").reason, "waiting for the user's answer");
t("limit", m.decide(cfg, 2, ok, "tui").reason, "limit");
t("count below limit", m.decide(cfg, 1, ok, "tui"), { action: "continue" });
t("lastAssistantText parts", m.lastAssistantText([{ role: "user", content: "x" }, { role: "assistant", content: [{ type: "text", text: "a" }, { type: "toolCall" }, { type: "text", text: "b?" }] }]), "ab?");
t("lastAssistantText none", m.lastAssistantText([]), "");
// config round trip + clamping
const f = process.env.KEEP_GOING_CONFIG;
t("missing file -> defaults", m.loadConfig(f), { enabled: true, max: 5, message: "continue" });
m.saveConfig({ enabled: false, max: 3, message: "go on" }, f);
t("saved", m.loadConfig(f), { enabled: false, max: 3, message: "go on" });
fs.writeFileSync(f, '{"max": 999, "message": "  "}');
t("clamp + blank message", m.loadConfig(f), { enabled: true, max: 50, message: "continue" });
fs.writeFileSync(f, "not json");
t("garbage -> defaults", m.loadConfig(f).max, 5);
// handler wiring through a mock ExtensionAPI
fs.writeFileSync(f, '{"enabled":true,"max":2}');
const handlers = {}; const cmds = {};
m.default({ on: (e, h) => (handlers[e] = h), registerCommand: (n, o) => (cmds[n] = o) });
const ctx = { mode: "tui", hasUI: true, ui: { notify: (...a) => (ctx.last = a) } };
const ev = { outcome: "completed", continue: false, context: { contextMessages: [{ role: "assistant", content: "half done" }] } };
const r1 = handlers.agent_before_settle(ev, ctx), r2 = handlers.agent_before_settle(ev, ctx), r3 = handlers.agent_before_settle(ev, ctx);
t("1st continues", r1.continue, true); t("entry text", r1.entries[0].content, "continue");
t("2nd continues", r2.continue, true); t("3rd blocked by limit", r3, undefined);
t("limit notice", ctx.last[1], "warning");
handlers.input({ source: "interactive", text: "hi" });
t("user input resets", handlers.agent_before_settle(ev, ctx).continue, true);
handlers.input({ source: "extension", text: "x" });   // extension-sourced input must NOT reset
handlers.agent_before_settle(ev, ctx);
t("extension input does not reset", handlers.agent_before_settle(ev, ctx), undefined);
await cmds.keepgoing.handler("off", ctx); t("cmd off persisted", m.loadConfig(f).enabled, false);
t("off -> no continue", handlers.agent_before_settle(ev, ctx), undefined);
await cmds.keepgoing.handler("on", ctx); await cmds.keepgoing.handler("max 7", ctx); await cmds.keepgoing.handler("text keep at it", ctx);
t("cmd on/max/text", m.loadConfig(f), { enabled: true, max: 7, message: "keep at it" });
await cmds.keepgoing.handler("max abc", ctx); t("bad arg -> usage", ctx.last[1], "warning");
console.log(n + " checks passed");
