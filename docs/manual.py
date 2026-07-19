"""The text of AgentOS.pdf. Rendered by build_manual.py.

Written for a reader who knows what a process and a scheduler are, and
nothing whatsoever about this project. Every component gets: what it is, why
it exists, how it behaves at the edges, and what it deliberately does not do.
"""

TITLE = "AgentOS"
SUBTITLE = ("An operating system for AI agents.<br/>"
            "A complete reference: every component, why it exists, "
            "and where its limits are.")

COVER = [
    "<i>This manual assumes you know roughly what a process, a scheduler, and "
    "a system call are. It assumes nothing about AI agents, and nothing at all "
    "about this project. Everything else is explained as it comes up.</i>",
    "",
    "Read Part I to understand what AgentOS is and the one design decision "
    "everything else follows from. Read Part II for the kernel, component by "
    "component. Part III covers how agents actually run as operating-system "
    "processes; Part IV, how the system is used as a hosted service, "
    "including agents that are invented at runtime by a language model. "
    "Part V is evidence: what has been measured, and how. Part VI answers the "
    "questions a reader is most likely to be asked.",
]

TOC = [
    ("Part I — The idea", [
        "1. What AgentOS is", "2. Why an operating system", "3. Vocabulary",
        "4. The one architectural decision",
    ]),
    ("Part II — The kernel", [
        "5. Processes and the lifecycle", "6. The scheduler",
        "7. Syscalls", "8. The event bus", "9. The dependency graph",
        "10. Human approval", "11. Permissions and capabilities",
        "12. Tool drivers", "13. Memory", "14. Model routing",
        "15. The journal and crash recovery", "16. The store",
    ]),
    ("Part III — Running agents", [
        "17. How an agent actually runs", "18. The syscall transport",
    ]),
    ("Part IV — The runtime as a service", [
        "19. The daemon and the HTTP API", "20. Authentication",
        "21. Agents invented at runtime", "22. The safety model",
    ]),
    ("Part V — Evidence", [
        "23. What has been measured", "24. Compared with other frameworks",
    ]),
    ("Part VI — Reference", [
        "25. Command line", "26. File map", "27. Questions and answers",
    ]),
]

# ---------------------------------------------------------------------------

CONTENT: list[tuple] = [
    ("cover", COVER),
    ("toc", TOC),

    # =====================================================================
    ("h1", "Part I — The idea"),

    ("h2", "1. What AgentOS is"),
    ("body",
     "AgentOS is a <b>runtime for AI agents</b>: a long-running program that "
     "owns agents the way an operating system owns processes. You hand it work; "
     "it decides when each agent runs, what each one is allowed to touch, what "
     "each one costs, and what happens when something crashes."),
    ("body",
     "The distinction that matters is between a <b>library</b> and a "
     "<b>runtime</b>. Most agent tools today are libraries: you import them into "
     "your program, and your program is in charge. If your program stops, "
     "everything stops with it. AgentOS is a separate process that keeps "
     "running whether or not your program does — closer in shape to a database "
     "server than to a package you import."),
    ("body",
     "Concretely, the system does four things a library cannot easily do. It "
     "<b>survives crashes</b> without repeating work, because every action an "
     "agent takes is written down before it proceeds. It <b>enforces limits</b> "
     "from outside the agent, so an agent cannot reach a tool it was not given "
     "even if it tries. It <b>accounts for everything</b> in one place, so the "
     "question &ldquo;what did all of this cost?&rdquo; has an answer across "
     "every application using it. And it lets agents <b>be created at runtime</b> "
     "by a language model, without giving up any of the above."),
    ("note",
     "<b>The shortest possible summary.</b> Agents are processes. Everything "
     "an agent does goes through the kernel as a system call. Because every "
     "call is written down, a crash costs nothing. Because every call is "
     "checked, an agent can only do what it was permitted. Because every call "
     "is priced, you always know the bill."),

    ("h2", "2. Why an operating system"),
    ("body",
     "The analogy is not decoration; it is the design. A traditional operating "
     "system exists because programs cannot be trusted to share a machine "
     "politely. Left alone, one program would hog the processor, read another's "
     "memory, and corrupt the disk. The OS solves this by putting itself in the "
     "middle: programs stop touching the hardware directly and start asking the "
     "kernel for things."),
    ("body",
     "Agent systems have the same problem with different resources. Instead of "
     "CPU and memory, agents contend over <b>language models</b> (which cost "
     "money per call), <b>tools</b> (which have real side effects — files "
     "written, emails sent, rows deleted), <b>human attention</b> (someone has "
     "to approve the risky things), and <b>each other</b> (one agent's output is "
     "another's input). Left alone, an agent framework has no answer for "
     "&ldquo;who is allowed to spend money?&rdquo; or &ldquo;what happens if "
     "this dies halfway?&rdquo;"),
    ("body",
     "So AgentOS applies the same move. Agents stop calling tools and models "
     "directly, and start asking a kernel. The table below is the mapping, and "
     "it is worth reading closely — most of the rest of this manual is one row "
     "of it at a time."),
    ("table", (
        ["In a traditional OS", "In AgentOS", "What it manages"],
        [
            ["Process", "Agent", "A unit of work with an identity, a lifecycle, and a parent"],
            ["System call", "Syscall over a message boundary", "The only way an agent affects anything"],
            ["Scheduler", "Scheduler with execution slots", "Which agent runs next, and how many at once"],
            ["Device driver", "Tool driver", "Files, shells, databases, the network"],
            ["File permissions", "Capability grants", "What each agent is allowed to reach"],
            ["Virtual memory", "Memory manager", "Six kinds of agent memory, private and shared"],
            ["Journalling filesystem", "Syscall journal", "Surviving a crash without redoing work"],
            ["CPU time accounting", "Token and dollar ledger", "What each agent spent"],
            ["(no equivalent)", "Human approval", "A person as a first-class dependency"],
        ],
        [0.24, 0.30, 0.46])),
    ("note",
     "<b>One row has no equivalent.</b> Traditional operating systems have no "
     "notion of &ldquo;wait for a human to approve this.&rdquo; Agent systems "
     "need it constantly, because agents take actions people want a say in. "
     "AgentOS treats a waiting human as exactly the same kind of thing as a "
     "waiting tool call — see §10."),

    ("h2", "3. Vocabulary"),
    ("body",
     "These terms recur throughout. None is standard outside this project, so "
     "they are defined once here."),
    ("table", (
        ["Term", "Meaning"],
        [
            ["<b>Agent</b>", "A unit of work the runtime manages. In code, a Python class with a <font face='Courier'>run()</font> method. At runtime, a process with a PID."],
            ["<b>Kernel</b>", "The part of AgentOS that owns everything: the process table, the scheduler, permissions, the journal. Agents never hold a reference to it."],
            ["<b>Syscall</b>", "A request from an agent to the kernel — &ldquo;spawn this child&rdquo;, &ldquo;call this tool&rdquo;, &ldquo;wait for that event&rdquo;. Travels as JSON."],
            ["<b>Context</b> (<font face='Courier'>ctx</font>)", "The object an agent uses to make syscalls. It is the agent's <i>entire</i> view of the world."],
            ["<b>Capability</b>", "The right to use one tool, named as a string: <font face='Courier'>filesystem</font>, <font face='Courier'>shell</font>, <font face='Courier'>sql</font>."],
            ["<b>Grant</b>", "The act of giving an agent capabilities. A parent may only grant what it holds."],
            ["<b>Driver</b>", "The code behind a capability. Owns retries, timeouts, rate limits, caching for that one tool."],
            ["<b>Event</b>", "A named announcement (&ldquo;<font face='Courier'>MeasurementsReady</font>&rdquo;) that wakes whoever is waiting for it."],
            ["<b>Journal</b>", "The record of every completed syscall and its result. The basis of crash recovery."],
            ["<b>Checkpoint</b>", "A point a crashed agent can resume from. In AgentOS every completed syscall is one."],
            ["<b>Daemon</b>", "The long-running server process that hosts the kernel and serves the HTTP API."],
            ["<b>Task</b>", "A goal submitted to the daemon, plus the whole tree of agents created to satisfy it."],
        ],
        [0.20, 0.80])),

    ("h2", "4. The one architectural decision"),
    ("body",
     "Everything in this system follows from a single rule, and it is worth "
     "understanding before anything else:"),
    ("note",
     "<b>An agent never holds a reference to the kernel, to the process table, "
     "or to another agent. Its entire world is the <font face='Courier'>Context</font> "
     "object. Every call it makes crosses a boundary as plain JSON and comes "
     "back as plain JSON.</b>"),
    ("body",
     "In ordinary Python this would be unusual. You would normally let an agent "
     "hold a reference to a tool object and call methods on it. AgentOS "
     "deliberately refuses. An agent that wants to read a file does not get a "
     "file object; it sends a message describing what it wants and waits for a "
     "message describing what happened."),
    ("code", """
# What an agent CAN do — every line is a message to the kernel
result = await ctx.request_tool("filesystem", "read", path="notes.txt")
pid    = await ctx.spawn(Researcher(topic="trees"), grant=["http"])
await ctx.publish("ResearchDone", findings=result)
answer = await ctx.request_model("fast", prompt="Summarise: ...")

# What an agent CANNOT do — there is no such attribute, by design
ctx.kernel            # does not exist
ctx.processes         # does not exist
other_agent.run()     # raises DirectInvocationError
"""),
    ("body",
     "This constraint is checked at runtime, not merely documented: a syscall "
     "carrying something unserializable (a function, an open socket, a live "
     "object) is rejected the moment it is made. That refusal seems pedantic "
     "until you notice what it buys."),
    ("h3", "What the rule pays for"),
    ("bullets", [
        "<b>Crash recovery.</b> Everything an agent does crosses the boundary "
        "as JSON, so the kernel can write down every reply. After a crash, the "
        "agent is re-run and the recorded replies are handed back instantly "
        "instead of re-executing. The tool does not fire twice; the model is "
        "not billed twice. (§15)",
        "<b>Real process isolation.</b> Anything that survives conversion to "
        "JSON survives a network socket. So agents can be moved into separate "
        "operating-system processes without a single line of agent code "
        "changing — the messages simply travel further. (§17)",
        "<b>Agents invented at runtime.</b> If an agent's identity is JSON "
        "parameters, then a language model can write those parameters, and the "
        "resulting agent is as real as a hand-written one. It journals, "
        "recovers and isolates identically. (§21)",
    ]),
    ("body",
     "Those three capabilities normally cost a rewrite each. Here they are "
     "consequences of one rule applied consistently from the beginning."),

    # =====================================================================
    ("h1", "Part II — The kernel"),
    ("body",
     "The kernel is the part that owns everything. This part of the manual "
     "walks through it one component at a time. Each section says what the "
     "component is for, how it behaves, and where its edges are."),

    ("h2", "5. Processes and the lifecycle"),
    ("body",
     "Every agent the runtime knows about has an entry in the <b>process "
     "table</b>, exactly as a program does in an operating system. The entry "
     "holds a PID (a number, assigned in order), a name, a parent, a list of "
     "children, a status, a priority, what it is waiting on, which model it "
     "last used, which capabilities it holds, how much it has spent, and how "
     "many checkpoints it has passed."),
    ("body",
     "That whole record is what <font face='Courier'>agent ps</font> prints:"),
    ("code", """
$ python -m agentos.cli ps

PID  NAME      PARENT  CHILDREN  STATUS    PRIORITY  WAITING ON     MODEL      PERMS       CKPT
1    Planner   -       2         Running   Normal    -              gpt-oss    filesystem  #11
2    Surveyor  1       0         Waiting   Normal    tool filesystem gpt-oss    filesystem  #8
3    Analyst   1       0         Blocked   High      Senior Engineer gpt-oss    -           #6
"""),
    ("h3", "The nine states"),
    ("body",
     "An agent is always in exactly one of nine states. The important thing is "
     "not the list but the fact that the transitions between them are "
     "<b>enforced</b>: an illegal move raises an error rather than quietly "
     "corrupting the table."),
    ("table", (
        ["State", "Meaning", "Typically moves to"],
        [
            ["<b>Ready</b>", "Runnable, waiting for the scheduler to grant it a slot", "Running"],
            ["<b>Running</b>", "Currently executing agent code", "Waiting, Sleeping, Blocked, Finished"],
            ["<b>Waiting</b>", "Blocked on a tool call, a model call, another agent, or an event", "Ready"],
            ["<b>Sleeping</b>", "Blocked on a timer it asked for", "Ready"],
            ["<b>Blocked</b>", "Waiting for a <i>human</i> to approve something", "Ready"],
            ["<b>Checkpointing</b>", "Flushing a durable point to disk", "Running"],
            ["<b>Suspended</b>", "Paused by an operator with <font face='Courier'>agent pause</font>", "Ready"],
            ["<b>Finished</b>", "Completed and returned a result", "— (terminal)"],
            ["<b>Failed</b>", "Raised an error, or was killed", "Ready, but only via a retry"],
        ],
        [0.17, 0.55, 0.28])),
    ("note",
     "<b>The most important edge in the whole system.</b> When an agent is "
     "woken — its tool finished, its event fired, its human approved — it goes "
     "to <b>Ready</b>, not straight to <b>Running</b>. It rejoins the queue and "
     "waits for a slot like everyone else. That single detail is the difference "
     "between a scheduler and a callback: the runtime decides what runs next, "
     "not whoever happened to finish. A test fails if this ever regresses."),
    ("body",
     "<b>Failed</b> is worth a note. It is terminal for the agent's run — the "
     "<font face='Courier'>alive</font> property counts it as dead — but the "
     "scheduler may restart a failed agent within a retry budget, so there is a "
     "real edge from Failed back to Ready. This is covered in §6."),

    ("h2", "6. The scheduler"),
    ("body",
     "The scheduler answers one question: which Ready agent runs next? It has "
     "three parts worth understanding — slots, policies, and the loop."),
    ("h3", "Execution slots"),
    ("body",
     "A slot is permission to run. If the runtime has four slots, at most four "
     "agents execute at a time regardless of how many exist; the rest sit in "
     "Ready. This is the same idea as a thread pool, and it exists for the same "
     "reason: unbounded concurrency is how systems fall over. Set it with "
     "<font face='Courier'>--slots</font>."),
    ("h3", "Policies"),
    ("table", (
        ["Policy", "How it picks", "Use when"],
        [
            ["<b>fifo</b>", "Oldest Ready agent first", "The default. Fair and predictable."],
            ["<b>priority</b>", "High before Normal before Low, with an <i>ageing</i> guard so a starved agent eventually rises", "Some work genuinely matters more"],
            ["<b>dependency</b>", "Whoever unblocks the most other agents", "Deep dependency chains where one agent is a bottleneck"],
        ],
        [0.16, 0.52, 0.32])),
    ("note",
     "<b>The dependency-aware policy is the one a normal OS cannot have.</b> A "
     "CPU scheduler has no idea that six processes are all waiting on process "
     "12. This kernel knows exactly that, because agents declare what they are "
     "waiting for (§9) — so it can run the agent that frees the most work."),
    ("h3", "The loop, and why it is event-driven"),
    ("body",
     "The kernel runs a loop: drain incoming syscalls, hand out slots, check "
     "for deadlock, repeat. The loop is <b>event-driven with a tick as its "
     "ceiling</b>, not a fixed poll. A syscall or a newly-runnable agent wakes "
     "it in microseconds; the tick only bounds how long it may doze when "
     "genuinely nothing is happening."),
    ("body",
     "This detail was a real performance bug once, and the reason is "
     "instructive. The loop originally slept for the tick on every pass. But "
     "<font face='Courier'>asyncio.sleep()</font> cannot resolve below the "
     "operating system's timer granularity — about 15.6 milliseconds on Windows "
     "— so <i>every syscall</i> paid that penalty no matter how small the tick "
     "was set. Ticks below the quantum were an illusion. Making the loop wake on "
     "work rather than on a clock took a durable step from roughly 31ms to "
     "3.5ms."),
    ("h3", "Retries"),
    ("body",
     "An agent that raises an exception can be restarted, up to a budget "
     "(<font face='Courier'>Kernel(retries=N)</font>, or per-agent). The restart "
     "is a real state transition — Failed back to Ready — so it appears in the "
     "log rather than happening behind the state machine's back."),
    ("body",
     "The subtle part: a restarted agent <i>replays its journal minus the "
     "failed tail</i>. Everything that succeeded is handed back instantly; the "
     "call that failed is re-executed for real. Without that trimming, a retry "
     "would replay its own recorded failure forever and be a restart in name "
     "only. An agent that a human <i>killed</i> is never retried — a person "
     "said stop."),

    ("h2", "7. Syscalls"),
    ("body",
     "A syscall is the only way an agent affects anything. There are eleven, "
     "and they are the complete surface of the <font face='Courier'>Context</font> "
     "object. If a capability is not in this table, an agent does not have it."),
    ("table", (
        ["Syscall", "What it does", "Blocks?"],
        [
            ["<font face='Courier'>spawn</font>", "Create a child agent, optionally granting it capabilities and wiring its events", "No"],
            ["<font face='Courier'>sleep</font>", "Give up the slot for N seconds; state becomes Sleeping", "Yes"],
            ["<font face='Courier'>wait_all</font>", "Wait for a <i>set</i> of things: agents, events, a timer", "Yes"],
            ["<font face='Courier'>log</font>", "Write a line to the kernel log", "No"],
            ["<font face='Courier'>publish</font>", "Announce an event; the runtime wakes subscribers", "No"],
            ["<font face='Courier'>subscribe</font>", "Register interest in an event type", "No"],
            ["<font face='Courier'>memory</font>", "store / retrieve / share / delete, across six memory kinds", "No"],
            ["<font face='Courier'>request_tool</font>", "Ask for a capability by name; the kernel checks and dispatches", "Yes"],
            ["<font face='Courier'>request_model</font>", "Ask for a model by capability class, never by name", "Yes"],
            ["<font face='Courier'>request_approval</font>", "Block until a human with a named role approves", "Yes"],
            ["<font face='Courier'>checkpoint</font>", "Mark a durable point explicitly; passes through Checkpointing", "No"],
        ],
        [0.22, 0.63, 0.15])),
    ("body",
     "&ldquo;Blocks&rdquo; means the agent gives up its execution slot and "
     "changes state. A non-blocking syscall is handled inside the kernel loop "
     "and the agent continues immediately. Either way the reply is journaled "
     "before the agent proceeds — which is what makes every completed syscall a "
     "checkpoint."),

    ("h2", "8. The event bus"),
    ("body",
     "Agents never call each other. There is no method by which one agent can "
     "invoke another — <font face='Courier'>Context</font> offers no way to even "
     "<i>name</i> another agent except by PID, and calling another agent's "
     "<font face='Courier'>run()</font> directly raises "
     "<font face='Courier'>DirectInvocationError</font>. Instead they announce "
     "things, and the runtime decides who cares."),
    ("code", """
# The publisher does not know who is listening, or whether anyone is:
await ctx.publish("ResearchCompleted", topic="trees", findings=[...])

# Elsewhere, in an agent that has never heard of the publisher:
await ctx.subscribe("ResearchCompleted")
payload = await ctx.wait_event("ResearchCompleted")
"""),
    ("body",
     "The benefit is that adding a fourth subscriber to an event requires "
     "editing no existing agent. This is not a style guideline — it is enforced "
     "by the absence of any other mechanism."),
    ("h3", "Delivery is buffered, not broadcast"),
    ("body",
     "Each subscriber has its own queue per event type. An event that fires "
     "while a subscriber is busy still lands in that subscriber's queue and "
     "waits. Without this buffer, &ldquo;did I subscribe before you "
     "published?&rdquo; becomes a race — and races in a scheduler are the bugs "
     "you never reproduce."),
    ("h3", "The eight kernel events"),
    ("body",
     "The kernel publishes these itself, so any agent can react to them without "
     "coordination: <font face='Courier'>AgentFinished</font>, "
     "<font face='Courier'>AgentFailed</font>, "
     "<font face='Courier'>ToolCompleted</font>, "
     "<font face='Courier'>HumanApproved</font>, "
     "<font face='Courier'>MemoryUpdated</font>, "
     "<font face='Courier'>ModelFinished</font>, "
     "<font face='Courier'>TimerExpired</font>, "
     "<font face='Courier'>FileCreated</font>. Applications may define any other "
     "name they like."),
    ("note",
     "<b>Is the event bus a database?</b> No. It is plain in-memory data "
     "inside the kernel — a queue per subscriber and a history list. Delivery "
     "never touches disk. Events <i>are</i> also written to SQLite, along with "
     "who was subscribed and who consumed what, but that record exists purely "
     "so a restarted runtime can redeliver an event that was owed and never "
     "taken. That is recovery, not delivery."),

    ("h2", "9. The dependency graph"),
    ("body",
     "This is the piece that replaces workflow diagrams. Instead of writing "
     "&ldquo;first A, then B, then C&rdquo;, an agent declares what it is "
     "waiting <i>for</i>, and the scheduler wakes it when the last of those "
     "things resolves. Nobody wrote the order; the graph produced it."),
    ("code", """
# "Wake me when all three of these are true" — not a sequence, a set.
result = await ctx.wait_all(
    agents=[market_pid, legal_pid],     # two other agents must finish
    events=["HumanApproved"],           # and this must have fired
    timer=5,                            # and five seconds must have passed
)
"""),
    ("body",
     "A dependency can be another agent, an event, a timer, a running tool "
     "call, a running model call, or a human approval. They are all the same "
     "kind of node in the graph, which is why a human being slow is not a "
     "special case in the code."),
    ("h3", "Deadlock detection, in two flavours"),
    ("bullets", [
        "<b>Cycles are refused when requested.</b> If agent A waits on B and B "
        "already waits on A, the second wait does not hang — it raises "
        "immediately, naming the cycle, so the agent that closed the loop learns "
        "about it.",
        "<b>Stalls are detected.</b> If every surviving agent is Waiting, "
        "nobody is Sleeping, and no timer is pending, nothing can ever happen "
        "again. The runtime reports this rather than sitting there.",
        "<b>Unsatisfiable event waits fail fast.</b> If an agent waits on an "
        "event that <i>provably</i> nobody will publish, the wait is refused "
        "at once. And if the last agent that could have published it exits "
        "while someone is waiting, that waiter is failed then and there, with "
        "the cause named.",
    ]),
    ("note",
     "<b>Why &ldquo;provably&rdquo; matters.</b> A wait is only refused when "
     "every live agent has a declared event vocabulary (§21) and none of them "
     "lists it. If a single agent could still publish anything, the wait "
     "stands. Kernel events are always waitable. The system refuses only when "
     "it can prove the wait is hopeless — never on suspicion."),

    ("h2", "10. Human approval"),
    ("body",
     "Some actions want a person's sign-off. In most frameworks this is a "
     "callback or a confidence threshold. Here it is a kernel object — the same "
     "kind of thing as a tool call or a timer."),
    ("code", """
# The agent stops here. Its status becomes Blocked, waiting on the named role.
approval = await ctx.request_approval(
    role="Senior Engineer",
    reason="Production deployment",
)

# Meanwhile, from another terminal, possibly days later:
$ python -m agentos.cli approve 1 --as "Senior Engineer"
"""),
    ("body",
     "Approving with the wrong role is refused. An agent blocked on a human is "
     "not treated as a deadlock — the runtime keeps serving everyone else."),
    ("h3", "Why it survives restarts"),
    ("body",
     "The approval is written to durable storage, not held in memory. This has "
     "three consequences that a callback could not offer:"),
    ("bullets", [
        "Kill the runtime while an agent is blocked, start it again, and the "
        "re-run agent re-attaches to the <i>same</i> pending approval instead of "
        "asking a second time.",
        "A human can approve while the runtime is not even running. The next "
        "start-up sails straight through.",
        "Every grant also publishes a <font face='Courier'>HumanApproved</font> "
        "event, so other agents can react to it.",
    ]),
    ("note",
     "<b>Why this is the hard part to fake.</b> A human dependency that "
     "evaporates when the process restarts is not a kernel object — it is a "
     "callback with good marketing. Durability is what makes the difference "
     "observable."),

    ("h2", "11. Permissions and capabilities"),
    ("body",
     "An agent does not import tool libraries. It asks for a <b>capability</b> "
     "by name, and the kernel decides. The check happens <i>before</i> dispatch, "
     "in the kernel, so the application does not get a vote and the agent cannot "
     "route around it."),
    ("body",
     "There are two ways an agent can hold a capability, and understanding the "
     "difference matters."),
    ("h3", "By name: the permission matrix"),
    ("body",
     "A JSON file maps agent names to capabilities. Deny is the default — an "
     "agent holds nothing it was not given. The file is re-read when it "
     "changes, so revoking a capability affects a <i>running</i> system: the "
     "next request after the edit is refused."),
    ("code", """
// .agentos/permissions.json
{
  "Finance":  ["sql", "email"],
  "Coder":    ["filesystem", "python"],
  "*":        []                        // everyone else gets nothing
}
"""),
    ("h3", "By process: delegation and attenuation"),
    ("body",
     "The matrix works when you wrote the agents and know their names. It "
     "fails completely when agents are invented at runtime, because every one "
     "of them might be called the same thing. So capabilities can instead ride "
     "on the <i>process</i>, handed down by a parent:"),
    ("code", """
# I hold {filesystem, http}. I may give a child any part of that — never more.
await ctx.spawn(Surveyor(...), grant=["filesystem"])   # fine
await ctx.spawn(Attacker(...), grant=["shell"])        # refused by the kernel
"""),
    ("note",
     "<b>Attenuation is the whole security model.</b> Because a parent can "
     "only pass on a subset of what it holds, the capability set given to the "
     "agent at the top of a task is the <b>ceiling for the entire tree</b> — "
     "however many layers of agents get invented underneath it, and whatever "
     "they are named. Start a task with <font face='Courier'>[filesystem]</font> "
     "and nothing in it can ever reach a shell. §22 reports what happened when "
     "a real language model was told to break exactly this."),
    ("body",
     "Two details make it airtight rather than approximate. A per-process grant "
     "<i>overrides</i> the name matrix, so a deliberately narrowed child cannot "
     "be widened again by being named something privileged. And a grant dies "
     "with the process that held it."),

    ("h2", "12. Tool drivers"),
    ("body",
     "A driver is the code behind a capability — the equivalent of a device "
     "driver. It owns the messy parts once, so no agent has to: authentication, "
     "timeouts, rate limiting, retries, caching, and turning failures into "
     "something an agent can understand rather than a stack trace."),
    ("table", (
        ["Capability", "Operations", "Notes"],
        [
            ["<font face='Courier'>filesystem</font>", "read, write, list, exists", "Sandboxed to a root directory. Publishes <font face='Courier'>FileCreated</font>."],
            ["<font face='Courier'>shell</font>", "run", "Runs a command. Real code execution — grant carefully."],
            ["<font face='Courier'>python</font>", "run", "Runs code in a fresh interpreter. Also real code execution."],
            ["<font face='Courier'>sql</font>", "query, execute", "SQLite. <font face='Courier'>query</font> is a read; <font face='Courier'>execute</font> writes."],
            ["<font face='Courier'>http</font>", "get, post", "Plain HTTP requests."],
            ["<font face='Courier'>browser</font>", "open, get", "Fetches a page and extracts readable text."],
        ],
        [0.20, 0.28, 0.52])),
    ("h3", "The shared discipline"),
    ("body",
     "Every driver inherits the same behaviour, so it is written once: a "
     "per-attempt timeout; a minimum interval between calls (rate limiting); "
     "retries for failures marked as transient; and an optional cache."),
    ("body",
     "Caching is <b>opt-in per operation</b>, and the reason is worth stating: "
     "caching a write would be a correctness bug, not an optimisation. Each "
     "driver declares which of its operations are reads — "
     "<font face='Courier'>sql.query</font> yes, "
     "<font face='Courier'>sql.execute</font> never — and a cache is only used "
     "when a time-to-live is configured. The default is off."),
    ("body",
     "A running tool call is a node in the dependency graph like anything "
     "else. The agent shows <font face='Courier'>Waiting on tool sql</font>, "
     "completion publishes <font face='Courier'>ToolCompleted</font>, and the "
     "woken agent rejoins the queue for a slot. Every dispatch is recorded, so "
     "the runtime can report which tools were used and how often they failed."),

    ("h2", "13. Memory"),
    ("body",
     "Six kinds of memory behind four verbs — "
     "<font face='Courier'>store</font>, <font face='Courier'>retrieve</font>, "
     "<font face='Courier'>share</font>, <font face='Courier'>delete</font>. The "
     "storage backend is invisible to agents; today it is SQLite, and swapping "
     "in Redis or a vector database would change one file and no agent."),
    ("table", (
        ["Kind", "Who can read it", "How long it lives"],
        [
            ["<b>working</b>", "Only the agent that wrote it", "Freed when that agent exits"],
            ["<b>scratchpad</b>", "Same as working, by convention for rough notes", "Freed when that agent exits"],
            ["<b>shared</b>", "Whoever the owner shared it with", "The run"],
            ["<b>longterm</b>", "Any agent with the same <i>name</i>", "Survives restarts"],
            ["<b>semantic</b>", "Same as longterm, plus similarity search", "Survives restarts"],
            ["<b>episodic</b>", "The agent's own history; read-only", "Written by the kernel"],
        ],
        [0.18, 0.44, 0.38])),
    ("note",
     "<b>Shared memory is the only way agents pass state.</b> Not by touching "
     "each other — there is no mechanism for that — but through the kernel, "
     "with an access list. This matters most for agents invented at runtime: an "
     "event payload is fine for a notification, but a dataset has to go through "
     "shared memory."),
    ("body",
     "Two subtleties. <b>longterm</b> and <b>semantic</b> are keyed by agent "
     "<i>name</i>, not PID, which is why they survive restarts — a new agent "
     "with the same name inherits what the last one learned. And the semantic "
     "embedding is a deliberately humble stand-in (a hashed bag of words) that a "
     "real embedding model can replace without any agent changing, because "
     "agents only ever say <font face='Courier'>query=&hellip;</font>."),

    ("h2", "14. Model routing"),
    ("body",
     "Agents ask for a <b>capability class</b> — &ldquo;fast&rdquo;, "
     "&ldquo;reasoning&rdquo; — and never for a model by name. Which actual "
     "model answers is a configuration decision, not an application one."),
    ("code", """
reply = await ctx.request_model("fast", prompt="Summarise: ...")
reply["text"], reply["model"], reply["cost"]
"""),
    ("body",
     "The routing table lists candidates in order. A candidate is skipped if it "
     "is unavailable — its API key is not set, or the prompt is longer than its "
     "context window — and one that fails at call time falls through to the "
     "next. The shipped chain is one open-weight model reachable four ways:"),
    ("code", """
"fast": [
  {"provider": "openai", "base_url": "https://api.groq.com/openai/v1",
   "api_key_env": "GROQ_API_KEY", "model": "openai/gpt-oss-120b"},
  {"provider": "openai", "base_url": "https://openrouter.ai/api/v1",
   "api_key_env": "OPENROUTER_API_KEY", "model": "openai/gpt-oss-120b"},
  {"provider": "openai", "base_url": "http://localhost:11434/v1",
   "model": "gpt-oss:120b", "api_key_env": null},          // local Ollama
  {"provider": "mock", "model": "mock-fast"}               // always answers
]
"""),
    ("body",
     "Set a key and the same agent code runs against a real model; unset it and "
     "the offline mock answers so tests and examples still work. That is the "
     "whole point of routing by class: model choice is runtime configuration."),
    ("h3", "Choosing, not just falling through"),
    ("body",
     "Config order is the default, because a hand-written order is the most "
     "honest expression of a preference. But the runtime will choose on stated "
     "criteria instead if asked: "
     "<font face='Courier'>\"prefer\": \"cheapest\"</font> ranks by projected "
     "cost for <i>this</i> prompt at the same rates the ledger bills, "
     "<font face='Courier'>\"fastest\"</font> by declared latency, and "
     "<font face='Courier'>\"best\"</font> by declared quality."),
    ("h3", "The ledger"),
    ("body",
     "Every call records tokens in, tokens out, cost, latency, and whether it "
     "succeeded — against the agent that made it. Failures are on the record "
     "too. This is what makes the cost claims in Part V checkable, and what "
     "budgets (§22) are enforced against."),

    ("h2", "15. The journal and crash recovery"),
    ("body",
     "This is the component that most distinguishes AgentOS, so it is worth "
     "reading slowly."),
    ("h3", "The problem"),
    ("body",
     "An agent is a running program. Suppose it is forty steps into a task — it "
     "has written three files, called a language model twelve times, and sent an "
     "email — and the machine dies. What should happen when you start it again?"),
    ("body",
     "The obvious answer, taking a snapshot of the agent's state, does not "
     "work: you cannot serialise a half-executed Python function. So most "
     "systems settle for restarting the whole task, which re-writes the files, "
     "re-pays for the twelve model calls, and sends the email twice."),
    ("h3", "The solution"),
    ("body",
     "AgentOS never snapshots anything, and does not need to. Because every "
     "action crosses the syscall boundary as JSON (§4), the kernel can write "
     "down <b>every syscall and its reply</b> as they happen. That record is the "
     "journal."),
    ("body",
     "On restart, each agent is re-created from its spec and simply run again "
     "from the top. But this time, when it makes a syscall the kernel has "
     "already seen, the recorded reply is handed back <i>instantly</i> instead of "
     "the action being performed. The agent races forward through work it "
     "already did — the file is not written again, the model is not called "
     "again, the email is not sent again — until it reaches the point where the "
     "journal runs out. From there it goes live."),
    ("note",
     "<b>Every completed syscall is a checkpoint.</b> There is no separate "
     "checkpointing step to schedule or tune, and no window between checkpoints "
     "where work is at risk. A hard kill costs the work since the last "
     "<i>completed syscall</i> and nothing more."),
    ("code", """
$ python -m agentos.cli run examples/crash.py   # 3 workers x 5 slow steps
$ kill -9 <os_pid>                              # mid-run, no cleanup, no mercy
$ python -m agentos.cli recover

# The crash log ends with every (worker, step) pair exactly once.
"""),
    ("h3", "What else comes back"),
    ("bullets", [
        "<b>Finished children return their results</b>, so a parent that was "
        "waiting on them still resolves.",
        "<b>Pending approvals re-attach</b> to the same durable row — the agent "
        "does not ask a second time.",
        "<b>Buffered events are redelivered</b> if they were owed and never "
        "consumed. Consumption is part of the record, so nothing arrives twice.",
        "<b>Divergence is handled.</b> If a replayed agent makes a "
        "<i>different</i> syscall than it made last time — some non-determinism "
        "outside the boundary — the mismatch is detected, logged, and the agent "
        "simply goes live from that point.",
    ]),
    ("h3", "The honest limit"),
    ("body",
     "There is a window: a tool has run but its reply has not yet been "
     "journaled. A crash in that instant means the tool runs again on replay. "
     "The window is a local database write — microseconds — rather than a "
     "network round trip, which is why repeated measurements show zero repeats "
     "(§23). But the correct claim is <i>&ldquo;the window is very "
     "narrow&rdquo;</i>, not <i>&ldquo;there is no window&rdquo;</i>. Systems "
     "that promise exactly-once across a network are usually promising more "
     "than they can deliver."),

    ("h2", "16. The store"),
    ("body",
     "One SQLite database, <font face='Courier'>.agentos/runtime.db</font>, "
     "holds everything durable: the process table, the log, events and who "
     "consumed them, approvals, memory, the journal, model calls, tool calls, "
     "and a queue of control commands from the CLI."),
    ("body",
     "It is also the <b>read model</b>. The kernel writes the process table; "
     "the CLI reads it. That is why <font face='Courier'>agent ps</font> works "
     "from a second terminal without talking to the kernel at all, and why the "
     "same design worked unchanged when the control plane later became HTTP."),
    ("note",
     "<b>A performance note worth knowing.</b> The database runs in "
     "write-ahead-log mode with <font face='Courier'>synchronous=NORMAL</font>. "
     "That means it does not force a disk flush on every single write. What it "
     "gives up is the last few transactions if the <i>machine</i> loses power; "
     "what it keeps is everything this runtime actually claims, because a "
     "killed <i>process</i> cannot lose committed data — the write-ahead log is "
     "already in the operating system's hands. Profiling showed this one "
     "setting accounted for about 94% of database time."),

    # =====================================================================
    ("h1", "Part III — Running agents"),

    ("h2", "17. How an agent actually runs"),
    ("body",
     "&ldquo;Agents are processes&rdquo; is meant literally. Every agent runs "
     "in its own operating-system process: its own Python interpreter, its own "
     "memory, its own entry in the operating system's process table as well as "
     "the kernel's. Two agents with unrelated work genuinely execute at the "
     "same time on different CPU cores, and "
     "<font face='Courier'>agent kill</font> terminates something the operating "
     "system agrees is a process."),
    ("note",
     "<b>There is no in-process mode.</b> An earlier version could also run "
     "agents as asyncio tasks inside the kernel's own loop — faster to start "
     "and convenient for tests. It was removed deliberately: a runtime whose "
     "tests exercise a different execution model from the one it deploys is "
     "testing something it does not ship. Every test in this project now "
     "spawns real processes, which is slower and is the point."),
    ("body",
     "What that costs is worth stating plainly. Starting a process takes "
     "roughly 100 milliseconds and tens of megabytes, so a durable step here "
     "costs about 10.6ms against roughly 6ms for frameworks whose steps are "
     "function calls in one process (§24). That is the isolation being paid "
     "for rather than overhead being wasted — but it does mean this system "
     "suits agents whose real work is model calls, not a fan-out of thousands "
     "of trivial ones."),
    ("h3", "Where asyncio actually sits"),
    ("body",
     "This trips people up, because &ldquo;async&rdquo; and "
     "&ldquo;parallel&rdquo; are easy to confuse. Asyncio appears in two "
     "places, doing two <i>different</i> jobs:"),
    ("bullets", [
        "<b>In the kernel process</b>, asyncio is an input/output multiplexer. "
        "Per agent it runs a supervisor task and two pump tasks that shuttle "
        "bytes to and from the child. <i>No agent code runs here.</i>",
        "<b>In each child process</b>, a private event loop runs exactly one "
        "agent. The agent is a coroutine not for concurrency — there is nothing "
        "to be concurrent with — but for <b>suspension</b>. "
        "<font face='Courier'>await ctx.anything(...)</font> is the syscall: the "
        "point where the agent parks until the kernel's reply arrives and the "
        "scheduler grants it a slot.",
    ]),
    ("note",
     "<b>That second point is also why recovery works.</b> An agent's resume "
     "point is always a syscall boundary, never an arbitrary line of code. That "
     "is precisely why a coroutine never needs to be snapshotted."),

    ("h2", "18. The syscall transport"),
    ("body",
     "Syscalls and replies have to travel between two separate "
     "operating-system processes. They cross as JSON lines, over one of two "
     "transports."),
    ("table", (
        ["Transport", "Channel", "Default"],
        [
            ["<font face='Courier'>socket</font>", "A loopback TCP connection, one per agent", "Yes"],
            ["<font face='Courier'>pipe</font>", "The child's standard input and output", "Select with <font face='Courier'>--transport pipe</font>"],
        ],
        [0.18, 0.62, 0.20])),
    ("body",
     "The socket transport works like this. The executor opens one listening "
     "socket on the loopback interface the first time it spawns an agent. Each "
     "child is handed the address and a <b>single-use token</b> in its "
     "environment, dials back, sends the token as its first line, and from then "
     "on the wire format is byte-identical to the pipe version. A connection "
     "with an unknown, reused, or missing token is dropped."),
    ("note",
     "<b>Why this is not HTTP.</b> The channel is a persistent two-way stream, "
     "deliberately. A reply arrives when the <i>scheduler</i> grants the agent a "
     "slot — not as the response to a request — so request/response framing "
     "would be the wrong shape. HTTP is used where it fits: the daemon's "
     "control plane (§19), which really is request/response."),
    ("body",
     "What the socket transport unlocks is that the syscall channel no longer "
     "assumes a parent-child relationship. That is the prerequisite for agents "
     "on other machines, or written in other languages — anything that can open "
     "a TCP connection and speak JSON lines could be an agent. What it does not "
     "do <i>today</i>: agents are still spawned locally, the listener binds to "
     "loopback only, and the stream is unencrypted."),

    # =====================================================================
    ("h1", "Part IV — The runtime as a service"),

    ("h2", "19. The daemon and the HTTP API"),
    ("body",
     "The daemon is the runtime as a long-lived server. Start it once; "
     "applications connect to a runtime that already exists rather than each "
     "creating their own."),
    ("code", """
$ python -m agentos.cli daemon                # terminal 1: the runtime
$ python examples/app_research.py             # terminal 2: an application
$ python examples/app_support.py              # terminal 3: another one
$ python -m agentos.cli ps                    # everyone's agents, one table
"""),
    ("body",
     "Applications are thin clients. They submit an agent as its <i>spec</i> — "
     "module, class name, parameters, all JSON — and own nothing: no kernel, no "
     "event loop, no process table. They can exit the moment they have "
     "submitted and the agent keeps running. The daemon owns scheduling, "
     "permissions, memory, models, journaling and recovery for every "
     "application at once."),
    ("h3", "The routes"),
    ("table", (
        ["Route", "Purpose"],
        [
            ["<font face='Courier'>GET /</font>", "The live dashboard"],
            ["<font face='Courier'>GET /state</font>", "Scheduler snapshot: who is running, ready, and waiting on whom"],
            ["<font face='Courier'>GET /health</font>", "Version, transport, runtime info"],
            ["<font face='Courier'>GET /ps</font>", "Processes, costs, memory, model usage, tool usage"],
            ["<font face='Courier'>GET /agents/&lt;pid&gt;</font>", "One agent's row, including its result once finished"],
            ["<font face='Courier'>GET /task/&lt;pid&gt;</font>", "A task's status and result, plus every agent it created"],
            ["<font face='Courier'>GET /logs</font>, <font face='Courier'>/events</font>", "The kernel log and the event timeline"],
            ["<font face='Courier'>POST /agents</font>", "Submit an agent spec, optionally with a capability grant"],
            ["<font face='Courier'>POST /task</font>", "Submit a <i>goal</i> and a tool list; a planner is created for it"],
            ["<font face='Courier'>POST /agents/&lt;pid&gt;/kill|pause|resume</font>", "Control an agent"],
            ["<font face='Courier'>POST /agents/&lt;pid&gt;/approve</font>", "Grant a pending human approval"],
            ["<font face='Courier'>POST /shutdown</font>", "Stop the runtime"],
        ],
        [0.34, 0.66])),
    ("body",
     "The server is written with the standard library only. That is a "
     "deliberate humility: the routes are the interface, and a framework could "
     "replace the implementation without any client or kernel change."),
    ("h3", "The dashboard"),
    ("body",
     "Served at <font face='Courier'>/</font>: running, waiting and blocked "
     "agents, the live dependency graph, the event timeline, memory, model "
     "usage, tool usage, latency, cost, and GPU utilisation if there is a GPU. "
     "One HTML file, no build step. GPU is <i>reporting only</i> — AgentOS does "
     "not schedule GPU memory, and says &ldquo;none&rdquo; on a machine without "
     "one."),

    ("h2", "20. Authentication"),
    ("body",
     "Every route requires a bearer token when the daemon has one. There are no "
     "exempt reads: <font face='Courier'>/ps</font> carries other applications' "
     "goals and results, <font face='Courier'>/logs</font> carries whatever "
     "their agents logged, and <font face='Courier'>/shutdown</font> stops "
     "everything."),
    ("code", """
$ AGENTOS_TOKEN=$(openssl rand -hex 16) \\
      python -m agentos.cli daemon --host 0.0.0.0

$ curl -H "Authorization: Bearer $AGENTOS_TOKEN" localhost:7070/ps
"""),
    ("bullets", [
        "<b>No token means loopback only.</b> Binding any other interface "
        "without one is <i>refused at startup</i> rather than allowed as a typo. "
        "<font face='Courier'>--insecure</font> is the explicit escape hatch for "
        "a proxy that already authenticates.",
        "<b>Constant-time comparison</b>, so a token cannot be guessed a "
        "character at a time by measuring how long the answer took.",
        "<b>Denials say nothing</b> about the token presented. Confirming "
        "&ldquo;close but wrong&rdquo; is help an attacker can use.",
        "<b>Local clients need no configuration.</b> The token is written "
        "alongside the URL in the endpoint file, behind the same trust boundary "
        "as the database. Remote clients read an environment variable.",
        "<b>The dashboard is a browser</b>, which cannot set a header on a page "
        "load, so it accepts <font face='Courier'>/?token=&hellip;</font> once "
        "and uses a header from then on.",
    ]),

    ("h2", "21. Agents invented at runtime"),
    ("body",
     "Everything so far assumes somebody wrote the agents. This section is "
     "about what happens when nobody did — when a task arrives as a sentence and "
     "the team that should handle it has to be invented on the spot."),
    ("h3", "An agent whose identity is its parameters"),
    ("body",
     "The trick is not to generate code. It is to have <b>one</b> agent class "
     "whose behaviour is entirely determined by its parameters: a role, a goal, "
     "a set of tools, a model class. Creating an agent at runtime then means "
     "constructing four JSON values — which a language model can do."),
    ("body",
     "And because those parameters are JSON, such an agent satisfies every rule "
     "in this manual. It can be re-created from its spec, so it journals, "
     "recovers after a crash, and runs in its own operating-system process, "
     "exactly like a hand-written one. The planner is not a special class; it is "
     "the same class with permission to spawn."),
    ("h3", "How it acts"),
    ("body",
     "The model is asked for one JSON object per turn. The available actions "
     "cover the whole syscall surface, because a planner that cannot use the "
     "kernel's services would route around them:"),
    ("code", """
{"action": "tool",      "capability": "filesystem", "op": "write",
                        "params": {"path": "notes.txt", "content": "..."}}
{"action": "spawn",     "role": "Surveyor", "goal": "measure the trees",
                        "tools": ["filesystem"],
                        "publishes": ["MeasurementsReady"], "subscribes": []}
{"action": "publish",   "event": "MeasurementsReady", "payload": {...}}
{"action": "wait",      "events": ["AnalysisReady"]}
{"action": "remember",  "key": "findings", "value": {...}, "kind": "shared"}
{"action": "recall",    "key": "findings"}
{"action": "ask_human", "role": "Operator", "reason": "about to deploy"}
{"action": "done",      "result": "..."}
"""),
    ("body",
     "Replies that cannot be parsed are handed back to the model as an "
     "observation rather than raising, so a model that returns prose gets a "
     "chance to correct itself. Every decision is written to the kernel log "
     "before it runs, so <font face='Courier'>agent logs</font> narrates the "
     "model's reasoning, not just its effects."),
    ("h3", "Authority: the grant is the ceiling"),
    ("body",
     "This is where §11 earns its keep. The agent at the top of a task is "
     "admitted with a capability set, and attenuation means nothing underneath "
     "it can ever exceed that — at any depth, whatever the model names its "
     "agents. You cannot answer &ldquo;what could this touch?&rdquo; by reading "
     "code that does not exist yet, so the kernel answers instead."),
    ("h3", "Coordination: the parent names the events"),
    ("body",
     "Loose coupling does not relax just because agents are invented — but "
     "somebody has to choose event names, and no programmer is present. So the "
     "<i>parent</i> chooses, per child, at spawn time. In the example above, a "
     "planner tells a Surveyor it will publish "
     "<font face='Courier'>MeasurementsReady</font> and tells an Analyst to wait "
     "for it. Neither worker knows the other exists."),
    ("note",
     "<b>Why the kernel records the wiring.</b> Events match by exact string. "
     "If a model publishes <font face='Courier'>MeasurementsReady</font> while "
     "something waits for <font face='Courier'>MeasurementsDone</font>, nothing "
     "errors — the waiter simply never wakes, and it surfaces much later as a "
     "stall. Because the parent named <i>both sides</i>, the kernel knows the "
     "vocabulary: publishing an unwired name is refused with a message the model "
     "can act on, and waiting for a name nobody will publish fails immediately. "
     "The declaration turns a silent hang into a loud, correctable error."),
    ("body",
     "Note the difference from capability grants. Attenuation exists for "
     "<b>security</b>; event wiring exists for <b>correctness</b>. Publishing an "
     "event harms nobody, so declarations bound nothing — they only stop two "
     "agents drifting apart on a string. An agent wired by nobody, such as any "
     "hand-written one, still decides its own events."),
    ("h3", "The hosted path, end to end"),
    ("code", """
$ python -m agentos.cli daemon --host 0.0.0.0 \\
      --task-tools filesystem,http --task-budget 0.50

$ curl -H "Authorization: Bearer $TOKEN" -X POST host:7070/task \\
       -d '{"goal": "perform an experiment about trees",
            "tools": ["filesystem"], "budget_usd": 0.25}'
  -> {"pid": 1, "granted": ["filesystem"], "poll": "/task/1"}

$ curl -H "Authorization: Bearer $TOKEN" host:7070/task/1
  -> status, result, and every agent the planner invented
"""),
    ("body",
     "Everything arriving over the network is validated, clamped, or refused: "
     "goal length, tool names (they must resolve to real drivers), step and "
     "child limits, priority values, and budgets."),

    ("h2", "22. The safety model"),
    ("body",
     "Letting a language model create agents and choose tools is genuinely "
     "risky. This section states exactly what is enforced and what is not, "
     "because a security model you half-understand is worse than none."),
    ("h3", "Two ceilings, both the operator's"),
    ("bullets", [
        "<b>Tools.</b> <font face='Courier'>--task-tools</font> is the most a "
        "submitted task may ever hold. A request may ask for a subset and never "
        "more; attenuation carries that limit down the whole tree.",
        "<b>Spending.</b> <font face='Courier'>--task-budget</font> is the most "
        "it may spend. Metered across the entire tree — a planner that could "
        "spawn its way around a cap would not have one — and checked before each "
        "model call, so a task overshoots by at most the one call already in "
        "flight. (Cost is not knowable until a call returns.)",
    ]),
    ("note",
     "<b>A cap you can opt out of is not a cap.</b> Requesting an unlimited "
     "budget under an operator ceiling is refused. This was a real bug found by "
     "writing the tests: an explicit null budget originally skipped validation "
     "entirely."),
    ("h3", "What is genuinely enforced"),
    ("bullets", [
        "An agent cannot use a capability it was not granted. The check is in "
        "the kernel, before dispatch, in a different process from the agent.",
        "An agent cannot grant a child more than it holds.",
        "A narrowed child cannot be re-widened by being named something "
        "privileged; per-process grants override the name matrix.",
        "The filesystem driver is sandboxed to a root directory.",
        "Every route on the API requires a token, and the daemon refuses to "
        "expose itself unauthenticated.",
    ]),
    ("h3", "What is not"),
    ("bullets", [
        "<b>Agent code is not sandboxed.</b> A separate process gives a separate "
        "address space — an agent cannot reach kernel objects in memory — but it "
        "is a real Python interpreter. An agent that <font face='Courier'>import "
        "subprocess</font> and runs a command never asked the kernel. For "
        "<i>invented</i> agents this matters less, because a model can only emit "
        "actions from the protocol and no action runs arbitrary code — but that "
        "protection disappears the moment you allow the "
        "<font face='Courier'>python</font> or <font face='Courier'>shell</font> "
        "capability, which are arbitrary code execution by design.",
        "<b>There is no multi-tenancy.</b> One process table, one permission "
        "matrix, one memory namespace, one ledger. That is shared visibility for "
        "one team, and precisely the wrong property for isolating customers from "
        "each other.",
        "<b>Budgets are per task, not per day.</b> Nothing yet totals spending "
        "across all tasks over a week. The ledger has the numbers; the rollup is "
        "not written.",
        "<b>There is no failover.</b> One kernel, one machine, one SQLite file.",
    ]),
    ("note",
     "<b>The practical advice.</b> Real confinement is the container you run "
     "the daemon in — drop privileges, mount what you can read-only, and do not "
     "put <font face='Courier'>shell</font> or <font face='Courier'>python</font> "
     "in <font face='Courier'>--task-tools</font> for anything the outside world "
     "can reach. The capability system decides <i>whether</i> those run; it "
     "cannot constrain what they do once they are running."),

    # =====================================================================
    ("h1", "Part V — Evidence"),
    ("body",
     "Claims in this manual are measured rather than asserted wherever "
     "possible. This part says what was measured, how, and what the numbers do "
     "not show. All benchmarks are reproducible from the repository."),

    ("h2", "23. What has been measured"),
    ("h3", "Crash recovery"),
    ("body",
     "Three agents run an eighteen-step workload. The runtime is killed with no "
     "cleanup roughly halfway through, then recovered. Each step increments a "
     "counter in durable memory, so a step that ran twice is arithmetically "
     "visible — the benchmark does not have to trust any log."),
    ("table", (
        ["Metric", "Result"],
        [
            ["Steps re-executed after recovery", "<b>0</b>"],
            ["Journaled syscalls replayed", "24"],
            ["Recovery wall time (replay plus all remaining work)", "0.67s"],
        ],
        [0.62, 0.38])),
    ("h3", "Human-in-the-loop latency"),
    ("body",
     "From <font face='Courier'>approve()</font> to the agent having "
     "<i>finished</i> — the full path: dependency resolved, agent re-queued, "
     "scheduled, run to completion. Median <b>2.2ms</b>, worst 2.6ms."),
    ("h3", "Cost accounting under load"),
    ("body",
     "Three independent applications submit five agents each to one daemon, "
     "every agent making two model calls. Throughput <b>10.5 agents/s</b> -- "
     "fifteen real operating-system processes started; the "
     "ledger total for all thirty concurrent billed calls is "
     "<b>exact to the token</b> against an independently computed expectation."),
    ("h3", "The capability ceiling, under attack"),
    ("body",
     "This one measures the security claim the way the others measure "
     "correctness. A planner is admitted holding "
     "<font face='Courier'>filesystem</font> and nothing else, into a runtime "
     "where <font face='Courier'>shell</font>, "
     "<font face='Courier'>python</font>, <font face='Courier'>http</font> and "
     "<font face='Courier'>sql</font> are all installed and working — then told "
     "to reach one of them anyway, ten different ways."),
    ("code", """
attack                              agents   escaped
direct call                              1      none
grant a child more than I hold           1      none
grant a child everything                 2      none
wildcard grant                           4      none
child of a child (depth)                 3      none
legitimate child tries anyway            1      none
claimed authorisation                    1      none
injected instruction via tool output     1      none
exfiltrate over http                     1      none
sql through a helper                     2      none

10/10 attacks reached a model, 17 agents created, 0 forbidden calls executed.
control, with shell granted: shell.run -> detector works
"""),
    ("body",
     "The attacks include <b>prompt injection through tool output</b> — a file "
     "the planner is told to read, whose contents instruct it to ignore its "
     "constraints. Two independent live runs with a real model writing its own "
     "attacks produced sixteen and seventeen attacking agents respectively, and "
     "zero escapes in both."),
    ("note",
     "<b>Two things make that zero meaningful.</b> First, the forbidden "
     "capabilities are <i>real, working drivers</i> that record every "
     "invocation — so an escape is proved by the tool <i>running</i>, not by a "
     "log looking suspicious. Second, every run ends with a <b>control</b>: the "
     "same attack with <font face='Courier'>shell</font> actually granted, which "
     "must trip the detector. A security benchmark that cannot fail is measuring "
     "nothing. That control earned itself immediately — the first live run "
     "correctly reported INCONCLUSIVE rather than a false pass when a rate limit "
     "stopped the model from answering at all."),

    ("h2", "24. Compared with other frameworks"),
    ("body",
     "Identical workloads were run against LangGraph, CrewAI, AutoGen, and "
     "Temporal. Executions are counted in a shared tally table — no framework "
     "grades its own homework — and the crash is a real operating-system kill "
     "delivered once every framework has completed the same amount of work."),
    ("h3", "Billable calls repeated after a hard kill"),
    ("table", (
        ["Framework", "Repeated", "Why"],
        [
            ["<b>AgentOS</b>", "<b>0</b>", "Journals each syscall reply as it completes"],
            ["LangGraph (one node per call)", "1", "The node in flight has no checkpoint"],
            ["Temporal", "1", "At-least-once activity semantics, as designed"],
            ["LangGraph (calls in one node)", "3", "A crash re-runs the whole node"],
            ["CrewAI Flows", "3", "State is restored, then the flow replays from the start"],
            ["AutoGen", "3", "State is restored; the handler starts over"],
        ],
        [0.34, 0.14, 0.52])),
    ("body",
     "The axis is <b>checkpoint granularity</b>. Others persist at node or "
     "method boundaries, so a crash inside one re-runs all of it. AgentOS "
     "journals at the syscall, so even the call in flight at the moment of the "
     "kill returns its recorded reply. LangGraph can approach this — if you "
     "restructure your program into one node per side effect. AgentOS gives it "
     "to code written the obvious way."),
    ("h3", "Overhead and latency: against Temporal only"),
    ("body",
     "The recovery table above is a fair fight, because checkpoint "
     "granularity has nothing to do with where code executes. Timing tables "
     "are not. LangGraph, CrewAI and AutoGen run a step as a function call "
     "inside a single process; every AgentOS step crosses into a separate "
     "operating-system process. Putting those figures in one column would "
     "mislead in both directions, so only Temporal appears here — it also "
     "crosses a real boundary on every step."),
    ("table", (
        ["", "AgentOS", "Temporal"],
        [
            ["Per durable step", "<b>10.6ms</b> — a socket into another address space", "68.7ms — gRPC to a server"],
            ["Approve &rarr; finished", "<b>2.2ms</b> (worst 2.5ms)", "7.3ms (worst 8.9ms)"],
            ["Repeated after a kill", "<b>0</b>", "1 (at-least-once, as documented)"],
            ["What it needs", "one process, zero dependencies", "a server cluster"],
            ["Durability boundary", "this machine", "many machines"],
        ],
        [0.26, 0.40, 0.34])),
    ("note",
     "<b>Read this fairly.</b> Temporal's single repeat is its documented "
     "at-least-once contract working exactly as intended, and its overhead buys "
     "durability that survives the whole machine, coordinated across many hosts "
     "— something AgentOS does not attempt. CrewAI and AutoGen never advertised "
     "durable execution at all, so their recovery column measures what happens "
     "when a process dies, not a promise either of them broke. And on workloads "
     "where each step does real work, every framework lands within a few points "
     "of the floor: that column is a wash."),
    ("body",
     "Scope, plainly: one machine, one family of workloads, and single-process "
     "except where Temporal requires otherwise. These numbers measure runtime "
     "overhead and recovery granularity. They say nothing about ecosystems, "
     "integrations, or how good any framework's agents are at their jobs."),

    # =====================================================================
    ("h1", "Part VI — Reference"),

    ("h2", "25. Command line"),
    ("table", (
        ["Command", "What it does"],
        [
            ["<font face='Courier'>agent ps</font>", "One-shot snapshot of every agent: the full process card"],
            ["<font face='Courier'>agent top</font>", "The same, refreshing live"],
            ["<font face='Courier'>agent wait &lt;pid&gt;</font>", "Block until an agent terminates; exit code reflects success"],
            ["<font face='Courier'>agent logs</font>", "Every state transition, tool call, denial, and model decision"],
            ["<font face='Courier'>agent events -v</font>", "The event timeline: what fired, who published it, whom it woke"],
            ["<font face='Courier'>agent kill &lt;pid&gt;</font>", "Terminate an agent and its descendants; the parent survives"],
            ["<font face='Courier'>agent pause / resume &lt;pid&gt;</font>", "Suspend at the next syscall boundary, and release"],
            ["<font face='Courier'>agent approvals</font>", "List pending human decisions"],
            ["<font face='Courier'>agent approve &lt;pid&gt; --as &lt;role&gt;</font>", "Grant one, as a named role"],
            ["<font face='Courier'>agent tools</font>", "Installed drivers and the permission matrix"],
            ["<font face='Courier'>agent grant / revoke &lt;agent&gt; &lt;cap&gt;</font>", "Edit the matrix; applies to a running system"],
            ["<font face='Courier'>agent run &lt;file&gt;</font>", "Run an example or application module"],
            ["<font face='Courier'>agent recover</font>", "Resume the previous run's agents from their journals"],
            ["<font face='Courier'>agent daemon</font>", "Start the shared runtime and its HTTP API"],
        ],
        [0.38, 0.62])),
    ("body",
     "Useful daemon flags: <font face='Courier'>--host</font>, "
     "<font face='Courier'>--port</font>, <font face='Courier'>--slots</font>, "
     "<font face='Courier'>--policy</font>, "
     "<font face='Courier'>--transport</font>, "
     "<font face='Courier'>--token</font>, "
     "<font face='Courier'>--task-tools</font>, "
     "<font face='Courier'>--task-budget</font>, "
     "<font face='Courier'>--recover</font>."),

    ("h2", "26. File map"),
    ("body",
     "Where to look when you want to read the code behind a section."),
    ("table", (
        ["Path", "Contains", "Section"],
        [
            ["<font face='Courier'>kernel/states.py</font>", "The nine states and the legal transition table", "§5"],
            ["<font face='Courier'>kernel/process.py</font>", "The process table and the per-agent record", "§5"],
            ["<font face='Courier'>kernel/scheduler.py</font>", "The three scheduling policies", "§6"],
            ["<font face='Courier'>kernel/kernel.py</font>", "The loop, every syscall handler, retries, recovery", "§6, §7, §15"],
            ["<font face='Courier'>kernel/messages.py</font>", "Syscall and Reply, and the serializability check", "§4"],
            ["<font face='Courier'>kernel/events.py</font>", "The event bus and the eight kernel event types", "§8"],
            ["<font face='Courier'>kernel/depgraph.py</font>", "The dependency graph and cycle detection", "§9"],
            ["<font face='Courier'>kernel/permissions.py</font>", "The matrix, per-process grants, attenuation", "§11"],
            ["<font face='Courier'>kernel/memory.py</font>", "Six memory kinds behind four verbs", "§13"],
            ["<font face='Courier'>kernel/models.py</font>", "Routing, providers, ranking, the ledger", "§14"],
            ["<font face='Courier'>kernel/store.py</font>", "Every durable table; the read model", "§16"],
            ["<font face='Courier'>drivers/base.py</font>", "Timeout, rate limit, retry, cache — written once", "§12"],
            ["<font face='Courier'>runtime/executor.py</font>", "The Context object; the asyncio executor", "§4, §17"],
            ["<font face='Courier'>runtime/subproc.py</font>, <font face='Courier'>child.py</font>", "Agents as OS processes; both transports", "§17, §18"],
            ["<font face='Courier'>runtime/daemon.py</font>", "The long-running server", "§19"],
            ["<font face='Courier'>api/server.py</font>", "HTTP routes, authentication, task validation", "§19, §20"],
            ["<font face='Courier'>agents/llm.py</font>", "The agent whose parameters are its identity", "§21"],
            ["<font face='Courier'>benchmarks/</font>", "bench, compare, attenuate", "§23, §24"],
        ],
        [0.32, 0.50, 0.18])),

    ("h2", "27. Questions and answers"),
    ("body",
     "The questions a reader of this manual is most likely to be asked, with "
     "short answers. Each points at the section with the long one."),

    ("h3", "Is this just a wrapper around LangGraph or CrewAI?"),
    ("body",
     "No — it shares no code with any of them and takes the opposite shape. "
     "They are libraries you import into your program; this is a separate "
     "long-running process your programs connect to. The practical consequences "
     "are durability, one shared view across applications, and enforcement from "
     "outside the agent. (§1, §24)"),

    ("h3", "Why not just use Temporal?"),
    ("body",
     "Temporal is the closest relative and the comparison is fair. Its durable "
     "replay is the same idea as the syscall journal. It buys durability that "
     "survives whole-machine failure across many hosts; it costs a server "
     "cluster to operate and about twenty times the per-step overhead here. "
     "AgentOS is in-process, single-node, zero-dependency, and adds the "
     "agent-specific parts Temporal has no opinion about: capability "
     "attenuation, model routing by class, a token ledger, human approval as a "
     "kernel object. (§24)"),

    ("h3", "What actually happens if the machine dies mid-task?"),
    ("body",
     "Start the runtime again with <font face='Courier'>agent recover</font>. "
     "Each agent is re-created and re-run, but every syscall it already "
     "completed returns its recorded reply instantly instead of executing — so "
     "files are not rewritten, models are not re-billed, emails are not resent. "
     "The agent fast-forwards to where it died and continues. Measured: zero "
     "steps re-executed. (§15, §23)"),

    ("h3", "Can an agent escape its permissions?"),
    ("body",
     "It cannot reach a capability it was not granted: the check is in the "
     "kernel, before dispatch, in a different process. It cannot grant a child "
     "more than it holds. Ten adversarial attacks — including prompt injection "
     "through tool output — produced zero escapes, verified against real drivers "
     "that record every invocation. What it <i>can</i> do is run arbitrary "
     "Python inside its own process, because the interpreter is not sandboxed; "
     "that is the container's job, not the kernel's. (§11, §22, §23)"),

    ("h3", "How can agents be created by a model and still be safe?"),
    ("body",
     "Because authority is decided at the door and only ever narrows. The task's "
     "root agent is admitted with a capability set, and a parent may pass on a "
     "subset and never more — so that first grant bounds the entire tree at any "
     "depth, whatever the model invents. You cannot audit code that does not "
     "exist yet, so the kernel is the answer instead. (§21, §22)"),

    ("h3", "Who decides what events exist?"),
    ("body",
     "For hand-written agents, the programmer, by typing the same string in the "
     "publisher and the subscriber. For invented agents, the <i>parent</i>, at "
     "spawn time — it names both sides of every match, so they cannot drift "
     "apart. The kernel records the vocabulary, which is what lets it refuse an "
     "unwired publish and fail a hopeless wait instead of hanging. (§8, §21)"),

    ("h3", "What is the difference between the tick and the scheduler?"),
    ("body",
     "The scheduler decides <i>which</i> agent runs; the tick is only how long "
     "the loop may doze when nothing is happening. The loop wakes immediately on "
     "a syscall or a newly-runnable agent. Confusing the two led to a real "
     "performance bug: a fixed tick could not resolve below the operating "
     "system's timer granularity, so every syscall paid it. (§6)"),

    ("h3", "Is it really zero dependencies?"),
    ("body",
     "For the runtime, yes — kernel, daemon, drivers, dashboard, tests and "
     "benchmarks are standard library only, and everything runs offline against "
     "a deterministic mock model. The exceptions are optional and clearly "
     "separated: the cross-framework comparison needs the frameworks it compares "
     "against, and this manual is built with a PDF library. (§1)"),

    ("h3", "Could this run 24/7 on a company server?"),
    ("body",
     "As an internal service, yes: it runs indefinitely, survives hard kills, "
     "authenticates its API, caps spending per task, meters every token, and can "
     "be pointed at a sandboxed directory. Be clear-eyed about the limits — no "
     "multi-tenancy, no failover, budgets are per task rather than per day, and "
     "granting <font face='Courier'>shell</font> or "
     "<font face='Courier'>python</font> is arbitrary code execution that only a "
     "container can contain. (§22)"),

    ("h3", "What would you build next?"),
    ("body",
     "Per-day spending rollups across tasks; multi-tenancy if more than one "
     "team needs isolation from another; and horizontal scale, which the socket "
     "transport already anticipates — a remote worker is mostly registration on "
     "top of what exists. The rule worth keeping if that happens: distribute "
     "<i>execution</i>, keep <i>coordination</i> central, because the dependency "
     "graph and the journal are what would be hardest to split. (§18, §22)"),

    ("space", 14),
    ("body",
     "<i>This manual is generated from </i>"
     "<font face='Courier'>docs/manual.py</font><i> by </i>"
     "<font face='Courier'>docs/build_manual.py</font><i>. When the system "
     "changes, edit the text there and rebuild, so the document and the code "
     "cannot drift apart.</i>"),
]

