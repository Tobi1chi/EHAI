// EHAI business-tool bridge only. Pi owns the model loop and its native history.
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";

export default function ehaiTools(pi) {
  const spec = JSON.parse(readFileSync(process.env.EHAI_PI_TOOL_SPEC, "utf8"));
  const pending = new Map();
  let finished = false;
  const emit = (event) => process.stdout.write(`${JSON.stringify(event)}\n`);
  let inputHashes = [];
  pi.on("context", (event) => {
    inputHashes = event.messages.filter((message) => message.role === "user")
      .flatMap((message) => typeof message.content === "string"
        ? [message.content]
        : message.content.filter((block) => block.type === "text").map((block) => block.text))
      .map((text) => createHash("sha256").update(text).digest("hex"));
  });
  pi.on("before_provider_request", () => {
    emit({ type: "ehai_context_prepared", nonce: spec.nonce, input_hashes: inputHashes });
    inputHashes = [];
  });
  pi.registerCommand("ehai-tool-result", {
    description: "Private host response channel; not an Agent tool",
    handler: async (args) => {
      const value = JSON.parse(Buffer.from(args, "base64").toString("utf8"));
      const entry = pending.get(value.call_id);
      if (!entry || value.nonce !== spec.nonce) throw new Error("Unknown EHAI tool response");
      pending.delete(value.call_id);
      if (value.finish === true) finished = true;
      if (value.is_error === true) {
        entry.reject(new Error(JSON.stringify(value.result)));
        return;
      }
      entry.resolve({
        content: [{ type: "text", text: JSON.stringify(value.result) }],
        details: {},
      });
    },
  });
  for (const tool of spec.tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      constrainedSampling: { type: "json_schema", strict: "require" },
      executionMode: "sequential",
      execute: async (callId, args, signal, _onUpdate, ctx) => {
        if (finished) throw new Error("EHAI result already submitted; no further tools allowed");
        if (signal?.aborted) throw new Error("EHAI execution cancelled");
        return await new Promise((resolve, reject) => {
          const cancel = () => { pending.delete(callId); reject(new Error("EHAI execution cancelled")); };
          signal?.addEventListener("abort", cancel, { once: true });
          pending.set(callId, {
            resolve: (value) => { signal?.removeEventListener("abort", cancel); resolve(value); },
            reject: (error) => { signal?.removeEventListener("abort", cancel); reject(error); },
          });
          const assistant = [...ctx.sessionManager.getBranch()].reverse()
            .find((entry) => entry.type === "message" && entry.message.role === "assistant")?.message;
          const batch = Array.isArray(assistant?.content)
            ? assistant.content.filter((block) => block.type === "toolCall").map((block) => block.id)
            : [];
          emit({ type: "ehai_tool_call", nonce: spec.nonce, call_id: callId, name: tool.name,
            arguments: args, batch_call_ids: batch });
        });
      },
    });
  }
  // Finish is a host-validated role result, not a model-generated final sentence.
  // Abort only after Pi has appended the tool results for this native turn.
  pi.on("turn_end", (_event, ctx) => { if (finished) ctx.abort(); });
}
