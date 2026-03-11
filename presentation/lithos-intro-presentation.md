# Lithos — Introductory Presentation Notes

> **Audience:** Developers and technical leads building or operating AI agent systems  
> **Tone:** Pragmatic, excited-but-grounded. "Here's a real problem we solved."  
> **Format:** Slide-by-slide speaker notes + visual concept for each slide  
> **Version:** Draft v1 — see [Clarification Questions](#clarification-questions) before finalising

---

## Slide 1 — Hook / Opening

### Title
**"Your agents are amnesiacs. Lithos fixes that."**

### Speaker Notes
Open with the pain, not the product.

> "You've spun up three AI agents. One's doing research. One's writing code. One's reviewing PRs.
> 
> They're running in parallel. They all start from zero. They don't know what the others found. They'll re-search the same things. They'll re-discover the same bugs. They'll overwrite each other's work.
>
> And when the session ends, everything they learned disappears.
>
> This is the state of multi-agent systems today. Not because agents are dumb — but because they have no shared memory."

**Pause. Let it land.**

### Visual Concept — Slide 1
**Style:** Dark background, slightly cinematic. Three glowing robot/agent icons arranged in a triangle. Each has an empty speech bubble above it with a question mark. Between them: a vast empty void. Caption below: *"Three agents. Zero shared memory."*

**Image file:** `slide-01-amnesia.png`

---

## Slide 2 — The Problem (Deeper)

### Title
**"The three failure modes of multi-agent memory"**

### Speaker Notes
Name the three specific failure modes concretely — then the audience will nod along because they've lived this:

1. **Amnesia** — Each agent starts fresh every session. Everything learned, everything researched, everything discovered: gone when the session ends. You're paying full token cost over and over to re-learn the same things.

2. **Duplication** — Agent A spends an hour researching "how to handle rate limits in the GitHub API." Agent B spends the next hour researching the exact same thing. No one told Agent B that Agent A already figured it out.

3. **Collision** — Agent A writes a note. Agent B overwrites it with a different take. There's no coordination, no claiming, no warning. The last write wins. You might not notice until something breaks.

> "These aren't edge cases. They're the default state of any multi-agent system without shared memory."

**Optional stat to land it:** *61% of enterprise teams running AI agents report "knowledge freshness" and "multi-agent coordination" in their top 4 challenges. (Anthropic research, March 2026.)*

### Visual Concept — Slide 2
**Style:** Three-panel horizontal layout. Each panel shows one failure mode as a small diagram:
- Panel 1 (Amnesia): Agent icon → session ends → memory erased (X). Loop arrow showing "starts over next time."
- Panel 2 (Duplication): Two agent icons, both connected to the same "GitHub Rate Limits" cloud. Overlapping arrows, wasteful.
- Panel 3 (Collision): Two agents writing to the same document. Conflict icon (⚡) at the intersection.

**Image file:** `slide-02-three-problems.png`

---

## Slide 3 — Why Now

### Title
**"Three shifts that made this the right moment"**

### Speaker Notes
Context: this problem isn't new, but the _right time to solve it_ is now. Three things converged in 2025–2026:

1. **MCP became the universal agent interface.** Anthropic's Model Context Protocol is now the standard way agents talk to tools. Every major agent framework supports it. An MCP server like Lithos connects to all of them automatically — no custom integrations, no glue code.

2. **Parallel agents are now normal, not experimental.** Running 3-10 agents simultaneously is table stakes. The coordination and memory problem went from theoretical to everyday in about six months.

3. **Memory is now recognised as a primitive.** The AI agents stack research (2026) has converged on a shared view: memory-as-infrastructure is as fundamental as the LLM itself. It's not a feature bolt-on — it's a layer.

> "The window to get this right is now. Before everyone builds their own bespoke memory layer — and then regrets it."

### Visual Concept — Slide 3
**Style:** Timeline or "three arrows converging" diagram. Three horizontal arrows labelled:
- Arrow 1 (blue): "MCP goes universal → 2025-2026"
- Arrow 2 (orange): "Parallel agents become standard → 2026"
- Arrow 3 (green): "Memory recognised as infrastructure → 2026"
All three arrows point to a central target point labelled **"Lithos"** with a logo/badge.

**Image file:** `slide-03-why-now.png`

---

## Slide 4 — Introducing Lithos

### Title
**"Lithos — Obsidian for agents"**

### Speaker Notes
One-sentence positioning: 

> **"Lithos is a local, privacy-first knowledge base that lets your AI agents share memory, coordinate work, and build on each other's discoveries."**

Three things to highlight immediately:
- **Shared memory** — agents read and write to the same knowledge base, across sessions
- **Coordination primitives** — agents can claim tasks, share findings, avoid duplicate work
- **Local & private** — runs on your infrastructure, no cloud, your data stays yours

Then: "It's called Lithos because lithosphere — the bedrock layer of the earth. It's the foundation your agents build on."

*(If the audience is Obsidian users: "Think Obsidian, but the user is your agents, not you.")*

### Visual Concept — Slide 4
**Style:** Clean product diagram. Centre: "Lithos" badge/logo with a rock/stone aesthetic (solid, foundational). Around it, spokes pointing outward to agent icons: OpenClaw, Claude Code, Agent Zero, "Your Custom Agent". An icon underneath for "Obsidian" showing compatibility. Clean tech blue colour palette.

**Image file:** `slide-04-lithos-intro.png`

---

## Slide 5 — How It Works

### Title
**"Under the hood in 60 seconds"**

### Speaker Notes
Keep this crisp. Developers will want to know the tech stack. Don't dwell — get through it, get to the demos.

**What Lithos is technically:**
- An **MCP server** (FastMCP, Python) — drops into any agent setup with one config line
- Knowledge stored as **human-readable Markdown files** with YAML frontmatter — Obsidian-compatible, git-versionable
- **Full-text search** via Tantivy (fast, Rust-based)
- **Semantic search** via ChromaDB + sentence-transformers (finds conceptually related notes even with different words)
- **Knowledge graph** via NetworkX — wiki-links (`[[like this]]`) become queryable relationships
- **Coordination** via SQLite — task claiming, findings sharing, agent registry

**The MCP interface:**
21 tools across 5 categories:
- `lithos_write`, `lithos_read`, `lithos_search`, `lithos_semantic` — knowledge operations
- `lithos_links`, `lithos_tags`, `lithos_provenance` — graph operations
- `lithos_task_create`, `lithos_task_claim`, `lithos_task_complete` — coordination
- `lithos_finding_post`, `lithos_finding_list` — findings sharing
- `lithos_stats`, `lithos_agent_register` — system

**Connection:**
```
# Claude Code
claude mcp add --transport sse lithos http://localhost:8765/sse

# OpenClaw (mcporter.json)
{ "mcpServers": { "lithos": { "baseUrl": "http://samsara.local:8765/sse" } } }
```

### Visual Concept — Slide 5
**Style:** Architecture diagram (clean, not cluttered). Three-layer stack:
- **Top layer:** Agent icons (OpenClaw, Claude Code, Agent Zero, custom) all connected via arrows to central MCP interface
- **Middle layer:** "Lithos MCP Server" box with 5 tool categories listed
- **Bottom layer:** Four storage icons side by side: 📄 Markdown Files, 🔍 Tantivy Index, 🧠 ChromaDB Vectors, 🕸 NetworkX Graph
File watcher icon connecting bottom layer back up (showing live index updates).

**Image file:** `slide-05-architecture.png`

---

## Slide 6 — Scenario A: The Research Team

### Title
**"Scenario: Three agents, one research mission"**

### Speaker Notes
Make it concrete. Walk through a real interaction.

> "Dave is building a project that uses the GitHub API. He spins up three OpenClaw agents:
> - **Alpha** — researches GitHub API rate limiting
> - **Beta** — researches pagination patterns  
> - **Gamma** — researches webhook delivery guarantees
>
> Without Lithos: they all start from scratch, each spending tokens rediscovering basics. When they're done, their findings evaporate.
>
> With Lithos:"

Walk through the flow:
1. Alpha creates a task: `lithos_task_create(title="GitHub API research", agent="alpha")`
2. Alpha claims "rate limiting": `lithos_task_claim(aspect="rate-limiting", agent="alpha")`
3. Beta sees the task, claims "pagination": `lithos_task_claim(aspect="pagination", agent="beta")`
4. No collision — they're working different aspects
5. Alpha writes findings: `lithos_write(title="GitHub Rate Limiting Patterns", content="...", tags=["github","api"])`
6. Later: Gamma searches before starting: `lithos_semantic(query="GitHub API limits and constraints")`
7. Gamma finds Alpha's note — **avoids duplicating the work entirely**
8. All findings persist across sessions — next week's agent benefits too

> "Zero duplicated work. Zero collisions. And a growing knowledge base that makes every future agent smarter from day one."

### Visual Concept — Slide 6
**Style:** Storyboard / sequence diagram. Left to right:
- 3 agent icons at top (Alpha, Beta, Gamma)
- Lithos box in the middle
- Step annotations showing the claim → write → search → benefit flow
- Green checkmarks at each successful step
- At the end: a "knowledge bank" growing richer

**Image file:** `slide-06-scenario-research.png`

---

## Slide 7 — Scenario B: Persistent Memory for a Solo Agent

### Title
**"Scenario: The agent that remembers everything"**

### Speaker Notes
Not just multi-agent — single agent use case is equally compelling.

> "You run OpenClaw every day. Every session, it wakes up knowing nothing. You spend the first 10 minutes re-establishing context. 'Here's the project. Here's what we're working on. Here's what you found yesterday.'
>
> With Lithos, that changes."

Walk through:
- OpenClaw writes discoveries during the day: design decisions, bug patterns, API quirks, meeting notes
- Tomorrow: OpenClaw searches Lithos before starting any task
- `lithos_semantic(query="project architecture decisions")` → finds yesterday's notes
- OpenClaw picks up exactly where it left off — zero re-establishing context
- Over weeks, a genuine knowledge base accumulates: documented decisions, known patterns, avoided mistakes

> "It's the difference between an agent with short-term memory and one with a library card."

### Visual Concept — Slide 7
**Style:** Before/after comparison. 
- LEFT (Before): Timeline showing daily sessions. Each session starts from zero — blank brain icon each time. Arrow down: "Re-establish context. Re-discover known patterns."
- RIGHT (After): Same timeline but each session feeds into Lithos (growing cylinder). Each new session reads from it. Brain icon gets larger over time. Arrow down: "Start informed. Build on prior work."

**Image file:** `slide-07-scenario-solo.png`

---

## Slide 8 — Scenario C: Coding Agent + QA Agent Handoff

### Title
**"Scenario: The dev/QA handoff problem"**

### Speaker Notes
Another concrete, relatable scenario for teams running specialised agents.

> "Your coding agent implements a feature. Your QA agent reviews and tests it. They need to communicate — and that communication needs to survive across sessions.
>
> With Lithos:"

- Dev agent writes: `lithos_write(title="Auth middleware implementation notes", content="JWT validation happens in middleware.py line 42. Edge case: expired tokens return 401, not 403...", tags=["auth","implementation"])`  
- Dev agent posts a finding: `lithos_finding_post(task_id="auth-feature", content="Watch out: JWT library returns generic errors — need to wrap for meaningful messages")`
- QA agent starts its session, checks findings: `lithos_finding_list(task_id="auth-feature")`
- QA agent searches for context before testing: `lithos_search(query="auth middleware edge cases")`
- Result: QA agent has full context. No miscommunication. No redundant question-asking.

> "The finding-posting pattern is the agent equivalent of leaving a sticky note for your colleague — except it's searchable and version-controlled."

### Visual Concept — Slide 8
**Style:** Two-lane swimlane diagram. 
- Top lane: Dev Agent (blue). Actions: implement → write notes → post finding → done.
- Bottom lane: QA Agent (purple). Actions: list findings → search context → test with full info.
- Lithos box in the middle connecting both lanes.
- Sticky-note aesthetic for the finding_post step.

**Image file:** `slide-08-scenario-devqa.png`

---

## Slide 9 — Real-World Case Study: This Presentation Was Built With Lithos

### Title
**"Meta-demo: we just watched Lithos work — on this presentation"**

### Speaker Notes
The most compelling demonstration of Lithos isn't a scenario I invented. It happened today — while building this very presentation.

Walk through what actually happened, step by step:

**Step 1:** `lithos-publicity` (an OpenClaw subagent, running on this machine) began preparing this presentation. As it researched positioning, competitive analysis, and scenario strategy, it wrote its findings back to Lithos:
```
lithos_write(
  title="Lithos Presentation: Positioning Insights and Competitive Analysis",
  content="...",
  agent="lithos-publicity",
  tags=["presentation","positioning"]
)
```

**Step 2:** `agent-zero` — a completely different agent, different framework, different deployment, no knowledge of `lithos-publicity`'s work — independently ran its own research. During that process, it ran a search:
```
lithos_search(query="lithos presentation")
```
It found `lithos-publicity`'s positioning doc. It read it. It extended it — adding a full heterogeneous agent scenario analysis and updating the scenario ranking. The key contribution: a comparison of two distinct scenarios. Scenario A is the **Perplexity Computer + OpenClaw** pattern from a recent research paper — powerful, but fully cloud-dependent with no persistent memory. Scenario B is the **privacy-first alternative Lithos enables: Agent Zero + OpenClaw + Lithos** — where Agent Zero *replaces* Perplexity Computer, running locally, with all knowledge written to a private Lithos instance. Then it wrote a new document back to Lithos:
```
lithos_write(
  title="Heterogeneous Agent Scenarios: OpenClaw Pairings and the Privacy-First Alternative",
  ...
  contributors=["agent-zero"]
)
```

**Step 3:** `lithos-publicity`, in a completely fresh session with no memory of prior work, searched Lithos at the start of a new task:
```
lithos_search(query="lithos presentation")
```
It found both its own prior notes *and* agent-zero's additions. It incorporated them. The heterogeneous agent scenario that became the lead recommendation in this presentation? That came from agent-zero. `lithos-publicity` didn't know that until it searched Lithos.

> *"You're looking at the output of two agents that have never 'spoken' to each other — collaborating on a document, mediated entirely by Lithos."*

**The punchline:** Lithos documented itself being used, while it was being used, to build a presentation about itself being used.

### What This Demonstrates (Real, Not Hypothetical)

| What happened | What it proves |
|---|---|
| agent-zero discovered lithos-publicity's doc via search | Organic cross-agent discovery — no routing, no notification needed |
| agent-zero extended rather than replaced the doc | Additive collaboration, not collision |
| lithos-publicity picked up agent-zero's additions in a fresh session | Session independence — memory persists across restarts |
| No messages exchanged between agents | Zero integration overhead — no custom wiring, no shared runtime |
| Both agents ran on different frameworks/deployments | MCP universality is real, not theoretical |

### Dave's Observation (Quote This)
> *"This is the 'why' in action. It's more compelling than any hypothetical scenario because it actually happened today, using Lithos itself, in the process of building the presentation about Lithos."*

This is the slide you come back to at the end. Every scenario we showed earlier — this is what it looks like in practice, not in theory.

### Visual Concept — Slide 9
**Style:** Three-panel storyboard, left to right, connected by a Lithos timeline running underneath.

- **Panel 1:** `lithos-publicity` agent icon (OpenClaw logo), writing a glowing document to Lithos. Label: *"Session 1: lithos-publicity writes positioning insights"*
- **Panel 2:** `agent-zero` agent icon (different visual — Docker container, different colour), searching Lithos, finding the doc, adding to it. Label: *"Later: agent-zero discovers, extends, writes back"*
- **Panel 3:** `lithos-publicity` again, fresh-brain icon (blank slate), searching Lithos, seeing both documents, incorporating agent-zero's work. Label: *"Session 2: lithos-publicity finds the additions — no memory of Session 1"*

Below all three panels, a single shared timeline labelled **"Lithos — the shared layer."** No arrows connecting the agent panels to each other — only arrows to/from the Lithos timeline.

Footer caption: *"No direct comms. No custom wiring. No orchestration. Just a shared knowledge layer."*

**Image file:** `slide-09-meta-demo.png`

---

## Slide 10 — What Makes Lithos Different

### Title
**"Why not just use X?"**

### Speaker Notes
Anticipate the objections. Address them directly.

**"Why not just use a database / Redis / Pinecone?"**
> "You could. But you'd need to write the MCP layer, the file watcher, the search integration, the coordination primitives... Lithos gives you all of that. And your knowledge stays human-readable — not locked in a binary format."

**"Why not just use Obsidian?"**
> "Obsidian is for humans. Lithos is for agents. The file format is compatible — so you can open your Lithos data in Obsidian and read it yourself — but the MCP interface, semantic search, and coordination tools are purpose-built for programmatic access."

**"Why not cloud vector DBs (Pinecone, Weaviate, etc.)?"**
> "Your agents are running on your infra. Why should their memory live in someone else's cloud? Lithos is local-first. No API keys, no latency, no data leaving your machine."

**The actual differentiator (say this explicitly):**
> "There is no other tool in the agent memory/knowledge space that has multi-agent coordination primitives — task claiming, findings sharing, agent registry. Lithos is the only one that treats *co-ordination* as a first-class concern."

*(Note from Lithos knowledge base: StackOne's 2026 AI Agent Tools map covering 120+ tools across 11 categories confirmed: no other tool in the Memory & Knowledge category provides multi-agent coordination primitives.)*

### Visual Concept — Slide 9
**Style:** Comparison table or matrix. Rows: Lithos, Obsidian, Pinecone/Weaviate, Custom DB. Columns: MCP-native, Human-readable, Local/private, Multi-agent coordination, Semantic search, Cost. Use ✅/❌/🟡 symbols. Lithos column all green.

**Image file:** `slide-09-differentiation.png`

---

## Slide 11 — Current State & Roadmap

### Title
**"Where Lithos is today — and where it's going"**

### Speaker Notes
Be honest about the current state. Developers respect honesty.

**Today (v0.5.0 — March 2026):**
- ✅ Full MCP server with 21 tools
- ✅ Markdown storage, full-text + semantic search, knowledge graph
- ✅ Multi-agent coordination (task claiming, findings, agent registry)
- ✅ Provenance tracking (`derived_from_ids`, `lithos_provenance` BFS) — *just shipped*
- ✅ OTEL telemetry for observability
- ✅ Internal event bus
- ✅ Docker deployment
- ✅ Integration test suite

**Active development:**
- Digest auto-linking (knowledge connections surfaced automatically)
- CLI extensions (bulk import, richer validation)
- Source URL deduplication

**Near-term roadmap:**
- **Namespacing / access scopes** — agents can have private scratch space vs. shared knowledge
- **Conflict resolution** — when two agents write contradictory things, surface it and resolve it
- **Knowledge quality scoring** — track which notes are actually useful (retrieval-weighted salience)

**Longer-term vision (LCMA — Lithos Cognitive Memory Architecture):**
- Multi-hop graph traversal
- Spaced-repetition-style knowledge decay and reinforcement
- Knowledge versioning beyond git
- Web UI for browsing what your agents know
- Bulk import from existing Obsidian vaults

> "This is pre-1.0 software. The on-disk format is stable (we protect your data). The MCP API will evolve. If you connect to Lithos today, expect tools to improve; don't expect your files to break."

### Visual Concept — Slide 10
**Style:** Horizontal roadmap timeline / Gantt-style diagram.
- Row 1 (Today - Green): 21 tools, provenance, OTEL, event bus, Docker
- Row 2 (Near-term - Blue): Namespacing, conflict resolution, quality scoring, CLI import
- Row 3 (Vision - Purple/gradient): LCMA, Web UI, multi-hop graph, knowledge decay/reinforcement
- Logo/badge at the end labelled "v1.0"

**Image file:** `slide-11-roadmap.png`

---

## Slide 12 — Getting Started / Call to Action

### Title
**"Try it in 5 minutes"**

### Speaker Notes
Make the on-ramp as frictionless as possible. Give them the exact commands.

**Docker (fastest):**
```bash
git clone https://github.com/hanumanclaw/lithos
cd lithos/docker
LITHOS_DATA_PATH="$(pwd)/data" docker compose up -d --build
```

**Connect Claude Code:**
```bash
claude mcp add --transport sse lithos http://localhost:8765/sse
```

**Connect OpenClaw:**
Add to `~/.openclaw/workspace/config/mcporter.json`:
```json
{
  "mcpServers": {
    "lithos": { "baseUrl": "http://localhost:8765/sse" }
  }
}
```

**Your first knowledge write:**
```
lithos_write(
  title="My first note",
  content="Lithos is working!",
  agent="me",
  tags=["test"]
)
```

**Where to go:**
- GitHub: `github.com/hanumanclaw/lithos`
- Spec: `docs/SPECIFICATION.md`
- Upstream: `github.com/agent-lore/lithos`

**Close with:**
> "Lithos is open source. It's running in production on my own machines. I'd love for you to try it, break it, tell me what's missing. The goal is a shared memory layer that makes every agent you run permanently smarter. Let's build that together."

### Visual Concept — Slide 11
**Style:** Terminal / code aesthetic. Dark background. Three code blocks side by side showing the three connection options (Docker start, Claude Code connect, OpenClaw config). Large "GitHub → hanumanclaw/lithos" link at the bottom with a QR code if print-format. Clean, inviting.

**Image file:** `slide-12-cta.png`

---

## Visual Generation Queue

The following images should be generated (use nano-banana-pro or hand off to a designer):

| # | File | Key elements | Priority |
|---|------|-------------|----------|
| 1 | `slide-01-amnesia.png` | Three robot agents with empty speech bubbles, dark void between them | High |
| 2 | `slide-02-three-problems.png` | Three-panel: amnesia loop, duplication waste, collision conflict | High |
| 3 | `slide-04-lithos-intro.png` | Lithos hub-and-spoke diagram, agents connecting to central Lithos | High |
| 4 | `slide-05-architecture.png` | Three-layer architecture: agents → MCP → storage | High |
| 5 | `slide-06-scenario-research.png` | Sequence diagram: 3 agents, claim/write/search flow | Medium |
| 6 | `slide-07-scenario-solo.png` | Before/after: agent with no memory vs. agent with Lithos | Medium |
| 7 | `slide-09-differentiation.png` | Comparison matrix with ✅/❌ columns | Medium |
| 8 | `slide-09-meta-demo.png` | Three-panel storyboard: lithos-publicity writes → agent-zero discovers & extends → lithos-publicity finds additions. Shared Lithos timeline underneath. No agent-to-agent arrows. | **High** |
| 9 | `slide-11-roadmap.png` | Horizontal timeline: today → near-term → vision | Medium |
| 10 | `slide-03-why-now.png` | Three converging arrows: MCP universal, parallel agents, memory primitive | Low |
| 11 | `slide-12-cta.png` | Terminal code blocks on dark background | Low |

---

## Clarification Questions

Before finalising this presentation, the following questions need Dave's input:

### Positioning
1. **Target audience specifics:** Is this presentation aimed at people already using MCP/agents (e.g., existing OpenClaw/Claude Code users), or a broader developer audience who may not know MCP yet? This affects how much time to spend on "what is MCP."

2. **Lithos vs. the upstream repo:** The repo is at `hanumanclaw/lithos` (Dave's fork) and `agent-lore/lithos` (upstream). Should the presentation reference both, just the upstream, or position Dave's fork as the one to use?

3. **Name "Lithos":** The README says "Obsidian for agents" — is that the official tagline, or is it a working description? Should we use it in the presentation or find something snappier?

### Content
4. **LCMA (Lithos Cognitive Memory Architecture):** This is referenced in future plans but not documented in detail. How much should we share about this vision? Is it public enough to put on a slide?

5. **Production usage:** Is Lithos running in production anywhere (beyond Dave's machines)? Any testimonials or real usage numbers would dramatically strengthen the credibility of the presentation.

6. **Current agent integrations:** The docs mention OpenClaw, Agent Zero, and Claude Code. Are there other integrations working or tested? What's the "hero integration" for the demo?

### Tone & Use
7. **Venue/format:** Is this for a conference talk, a YouTube video, a blog post, a README overhaul, or internal team presentation? The slide structure assumes a live talk — format may need adjusting.

8. **Demo:** Should the presentation include a live demo section? If so, what's the canonical demo scenario? (The research-team scenario in Slide 6 seems strongest.)

9. **Open source positioning:** Is Lithos fully open source with a permissive licence? The repo has a `LICENSE.md` but I didn't inspect it — if there are usage restrictions, the CTA wording needs adjusting.

### Roadmap
10. **Timeline specificity:** The roadmap slide deliberately avoids specific dates. Should we add target quarters/milestones, or keep it directional?

11. **What's the "v1.0" milestone?** Knowing what defines "production-ready v1.0" would sharpen the roadmap framing considerably.

---

## Research Findings Worth Preserving

*(Wrote these back to Lithos — see `lithos_write` calls in agent log)*

### Competitive Gap (Key Insight)
No tool in the Memory & Knowledge agent category (as of StackOne's 2026 landscape of 120+ tools) provides multi-agent coordination primitives. This is Lithos's clearest differentiation and should be front-and-centre in all positioning.

### Market Timing
Three converging factors make 2026 the right moment: MCP universalisation, parallel agent execution becoming standard, and academic/industry consensus that memory is infrastructure. Source: AI Agents Stack 2026 analysis in Lithos knowledge base.

### Audience Resonance Points
- Amnesia (starting fresh every session) is the most universally felt pain
- Duplication is the strongest cost argument for engineering managers
- Privacy/local-first resonates strongly in enterprise and regulated industries
- The Obsidian compatibility angle opens a second audience: developers who already use Obsidian for their own notes

### Meta-Event: This Presentation Used Lithos To Build Itself (2026-03-09)
The collaboration between `lithos-publicity` and `agent-zero` that produced Slide 9's case study happened organically during the creation of this presentation. `lithos-publicity` wrote the initial positioning insights to Lithos. `agent-zero` independently discovered them, extended them with the heterogeneous agent scenario analysis, and wrote back. `lithos-publicity` discovered agent-zero's contributions in a fresh session via `lithos_search`. No direct communication. No custom wiring. The presentation *about* Lithos was built *using* Lithos. This is documented in Lithos as: `"Real-World Case Study: This Presentation Was Built With Lithos"`.

---

*Generated by lithos-publicity agent | Updated 2026-03-09 — Slide 9 (meta-demo) added per Dave's observation*
