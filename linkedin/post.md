Everyone is comparing models.

The teams shipping reliable agents have moved on to comparing harnesses.

A harness is the runtime around the model: what it can see, what it can do, what it's stopped from doing, what it remembers. Same weights, two harnesses — two different products. I've been deep in DeepSeek's new harness (DSH) this week, and it's the most explicit articulation of this discipline I've seen shipped.

Worth stealing:

𝗖𝗼𝗺𝗽𝗼𝘀𝗶𝘁𝗶𝗼𝗻 𝗼𝘃𝗲𝗿 𝗰𝗼𝗻𝗳𝗶𝗴𝘂𝗿𝗮𝘁𝗶𝗼𝗻
~230 packages composed over an empty root as ordered patch layers. Tools, compaction, sandboxing, goals, delegation — all plugins. Nothing is load-bearing by being hardcoded, so you can delete what you disagree with.

𝗖𝗼𝗻𝘁𝗲𝘅𝘁 𝗶𝘀 𝗮 𝗯𝘂𝗱𝗴𝗲𝘁, 𝗻𝗼𝘁 𝗮 𝗵𝗼𝗽𝗲
A token meter replays the session log deterministically with zero model calls, so compaction, telemetry and the UI agree on one number. Oversized tool output is pruned to head + marker + tail. Old history condenses to a summary, but originals stay in the log. Lossy for the model, lossless for the audit.

𝗧𝗵𝗲 𝗱𝗲𝘁𝗮𝗶𝗹 𝘁𝗵𝗮𝘁 𝗴𝗮𝘃𝗲 𝗶𝘁 𝗮𝘄𝗮𝘆
223 of 230 package READMEs carry a "KV Cache effect" section. Not a footnote — an obligation to prove your component appends to the prompt rather than mutating its reusable prefix. Cache invalidation is the silent tax on agent latency and cost; here it's a reviewable contract.

𝗚𝘂𝗮𝗿𝗱𝗿𝗮𝗶𝗹𝘀 𝗮𝘀 𝘁𝘆𝗽𝗲𝗱 𝗲𝗿𝗿𝗼𝗿𝘀, 𝗻𝗼𝘁 𝗽𝗿𝗼𝗺𝗽𝘁 𝗽𝗹𝗲𝗮𝘀
The filesystem won't let an agent edit a file it hasn't read, and refuses again if the file changed since that read. "Please be careful" becomes FS_NOT_OBSERVED plus a remedy. Sandbox and approval policy fail closed: a missing approver denies, it doesn't shrug.

(I hit this writing this post: my own edit was rejected because the file had changed since I read it. Annoying. Also exactly right.)

𝗠𝗲𝗺𝗼𝗿𝘆 𝘄𝗶𝘁𝗵 𝗮 𝗰𝗼𝗻𝘀𝗶𝘀𝘁𝗲𝗻𝗰𝘆 𝗺𝗼𝗱𝗲𝗹
Goals persist across turns, resume, fork and restart, with compare-and-set updates that reject stale views. Delegation is plural by design: one-shot children, continuable ones, forks that inherit the conversation, scripted workflows, fresh-agent loops — different failure modes, chosen explicitly.

But what I keep returning to is structural: every package documents a "Model Experience" — what the model sees, what it costs, what it invalidates.

That's the thesis. The model is a user of your system. Its context window is a UI you're designing whether you admit it or not. And most agent failures I'm called in to debug aren't reasoning failures — they're interface failures: ambiguous tool contracts, truncated output, guardrails written as suggestions, state the agent couldn't know was stale.

Prompt engineering was what you say to the model.
Context engineering was what you put in front of it.
Harness engineering is the system it lives inside — and it decides whether your demo survives production.

Your moat isn't the model. Everyone rents the same ones.

What's cost you most — tools, context, or state?

#AIAgents #HarnessEngineering #DeepSeek #ContextEngineering #AgenticAI
