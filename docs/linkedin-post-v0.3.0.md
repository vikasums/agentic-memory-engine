# LinkedIn post — v0.3.0 launch

Draft saved 2026-09-26. Not posted yet.

Every number below comes from `VALIDATION_RESULTS.md`, which is generated from the JSON
run reports in `reports/`. Re-check them before posting if the runs have been regenerated
since.

---

**I built a lightweight memory engine for AI assistants. Here's what I learned testing it.**

Most AI assistants forget you as soon as the chat ends. The usual fixes are heavy: big frameworks, lots of dependencies, and memory that only grows. Tell an assistant you moved from Seattle to Berlin and it may remember both, then guess which one is true.

So I built something small. It's an open-source, lightweight memory engine that sits beside any AI app and does three things well:

🧠 **It updates itself.** When you change your mind, the old fact gets replaced instead of piling up.
⏳ **It forgets on purpose.** Short-lived details, like a travel date or a one-off request, expire on their own.
⚡ **It's fast.** One call returns a short summary of who the user is, in under 50ms once it's cached.

It runs on 5 small core packages. No big framework needed.

**How we tested it**

I didn't want to just say "it works," so I built a test setup around it. It plays 50 everyday situations against the engine: people changing their minds, details expiring, several users at once. Then it checks every result three separate ways. I ran it at 1, 2, 4 and 6 minutes.

The results: 42 to 44 of the 50 situations passed on every run. It never mixed up one person's memories with another's, and every change was fully recorded, on all four runs.

**What I learned**

1️⃣ **Your test can lie to you.** Our first score was 6%. The engine was mostly fine. The checker was wrong and counted things as failures when they weren't. After fixing it, the real score was about 86%.
2️⃣ **Measure it, don't assume it.** At one point our reports showed "PASS" because it was typed into the report, not because a test proved it. Now every number comes from a real run.
3️⃣ **Say what doesn't work yet.** The part that pulls facts out of what people say still misses a lot, around 6 out of 10 sentences, and it doesn't always give the same answer twice. That's the next thing I'm fixing.

**Try it and tell me what breaks**

It's free and open source: https://github.com/vikasums/agentic-memory-engine

If you're building a chatbot, an agent or any assistant that should remember people, please try it. Tell me what's confusing, what's missing or where it falls over. Honest feedback is the most useful thing you can give me. 🙏

#AI #OpenSource #LLM #AIAgents #BuildInPublic

---

## Notes before posting

- "About 86%" is 43 of 50 on average across the four runs. Swap in "42–44 of 50" for the
  exact range.
- Nothing here claims a comparison against other memory tools such as Mem0 or Zep, because
  no such benchmark was run. "How we tested" describes what was actually measured. A
  head-to-head comparison would need a benchmark first.
- Roughly 330 words. LinkedIn truncates after about 3 lines, so the hook and the
  Seattle/Berlin example are deliberately first.
- Source numbers: 43/50, 42/50, 44/50, 43/50 across the 1, 2, 4 and 6 minute runs;
  criterion G (audit completeness) and criterion H (user isolation) pass on all four;
  extraction misses 66–69 of 111 utterances per run.
