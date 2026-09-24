/**
 * keep-going.ts — when the agent stops, tell it "continue".
 *
 * Fires on `agent_before_settle` (the last point before Pi goes idle): if this
 * hook is on, it appends a "continue" message and asks for one more model
 * request, so a run that stopped mid-task picks itself back up — CLI and any
 * front-end that loads ~/.pi/agent/extensions.
 *
 * It does NOT continue when:
 *   - you aborted the run (Esc) — you meant it;
 *   - the agent's last message ends with a question — it is waiting for YOU;
 *   - it already auto-continued `max` times in a row (default 5; any real
 *     message from you resets the count) — the runaway-cost guard;
 *   - Pi runs in print/json mode (scripts; there "continue" would race the prompt).
 *
 * Control (shared by every session, stored in ~/.pi/agent/keep-going.json):
 *   /keepgoing            show status
 *   /keepgoing on|off     enable / disable
 *   /keepgoing max N      consecutive auto-continues allowed (1-50)
 *   /keepgoing text ...   change the message that is sent
 *
 * Try out: pi --extension ./keep-going.ts   (or leave it in extensions/)
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export interface KeepGoingConfig {
	enabled: boolean;
	max: number;
	message: string;
}

export const DEFAULTS: KeepGoingConfig = { enabled: true, max: 5, message: "continue" };
const CONFIG_PATH =
	process.env.KEEP_GOING_CONFIG ?? path.join(os.homedir(), ".pi", "agent", "keep-going.json");

export function loadConfig(file = CONFIG_PATH): KeepGoingConfig {
	try {
		const raw = JSON.parse(fs.readFileSync(file, "utf-8"));
		const max = Number(raw.max);
		return {
			enabled: raw.enabled !== false,
			max: Number.isFinite(max) ? Math.min(50, Math.max(1, Math.trunc(max))) : DEFAULTS.max,
			message: typeof raw.message === "string" && raw.message.trim() ? raw.message : DEFAULTS.message,
		};
	} catch {
		return { ...DEFAULTS };
	}
}

export function saveConfig(cfg: KeepGoingConfig, file = CONFIG_PATH): void {
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.writeFileSync(file, `${JSON.stringify(cfg, null, 2)}\n`);
}

/** Text of the last assistant message in a list of agent messages ("" if none). */
export function lastAssistantText(messages: unknown[] | undefined): string {
	for (let i = (messages?.length ?? 0) - 1; i >= 0; i--) {
		const m = messages![i] as { role?: string; content?: unknown };
		if (m?.role !== "assistant") continue;
		if (typeof m.content === "string") return m.content;
		if (Array.isArray(m.content))
			return m.content
				.map((p: { type?: string; text?: string }) => (p?.type === "text" ? (p.text ?? "") : ""))
				.join("");
		return "";
	}
	return "";
}

export type Decision = { action: "continue" } | { action: "skip"; reason: string };

/** Pure decision: should the agent be told to continue? `count` = auto-continues so far. */
export function decide(
	cfg: KeepGoingConfig,
	count: number,
	ev: { outcome: string; continue: boolean; assistantText: string },
	mode: string,
): Decision {
	if (!cfg.enabled) return { action: "skip", reason: "off" };
	if (mode === "print" || mode === "json") return { action: "skip", reason: "non-interactive" };
	if (ev.outcome === "aborted") return { action: "skip", reason: "aborted" };
	if (ev.continue) return { action: "skip", reason: "already continuing" };
	if (ev.assistantText.trimEnd().endsWith("?") || ev.assistantText.trimEnd().endsWith("？"))
		return { action: "skip", reason: "waiting for the user's answer" };
	if (count >= cfg.max) return { action: "skip", reason: "limit" };
	return { action: "continue" };
}

export default function (pi: ExtensionAPI) {
	let count = 0;

	pi.on("session_start", () => {
		count = 0;
	});

	// a real message from the user (typed, or sent by a front-end) resets the guard
	pi.on("input", (event) => {
		if (event.source !== "extension") count = 0;
		return { action: "continue" as const };
	});

	pi.on("agent_before_settle", (event, ctx) => {
		const cfg = loadConfig();
		const d = decide(
			cfg,
			count,
			{
				outcome: event.outcome,
				continue: event.continue,
				assistantText: lastAssistantText(event.context?.contextMessages),
			},
			ctx.mode,
		);
		if (d.action === "skip") {
			if (d.reason === "limit" && ctx.hasUI)
				ctx.ui.notify(
					`keep-going: stopped after ${cfg.max} automatic continues — send a message to reset.`,
					"warning",
				);
			if (d.reason === "aborted") count = 0;
			return;
		}
		count++;
		return {
			entries: [
				{
					type: "custom_message" as const,
					customType: "keep-going",
					content: cfg.message,
					display: true,
				},
			],
			continue: true,
		};
	});

	pi.registerCommand("keepgoing", {
		description: "Auto-continue when the agent stops: on | off | max N | text ... | (status)",
		handler: async (args, ctx) => {
			const cfg = loadConfig();
			const [cmd, ...rest] = args.trim().split(/\s+/);
			const arg = rest.join(" ");
			if (cmd === "on" || cmd === "off") cfg.enabled = cmd === "on";
			else if (cmd === "max" && Number.isFinite(Number(arg)) && Number(arg) >= 1)
				cfg.max = Math.min(50, Math.trunc(Number(arg)));
			else if (cmd === "text" && arg.trim()) cfg.message = arg.trim();
			else if (cmd) {
				ctx.ui.notify("Usage: /keepgoing [on|off|max N|text MESSAGE]", "warning");
				return;
			}
			if (cmd) {
				saveConfig(cfg);
				count = 0;
			}
			ctx.ui.notify(
				`keep-going is ${cfg.enabled ? "ON" : "OFF"} — up to ${cfg.max} automatic "${cfg.message}" in a row (used ${count}).`,
				"info",
			);
		},
	});
}
