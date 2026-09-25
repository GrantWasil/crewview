// Crewview — read-only bridge dashboard. All untrusted text is inserted
// as DOM text nodes; nothing from the API is ever parsed as HTML. The Claude
// worker keeps the internal "opus" key; its display name comes from the API.
"use strict";
(() => {
  const POLL_MS = 2000;
  const HIDDEN_POLL_MS = 6000;
  const ACTIVE = new Set(["queued", "running"]);
  const STATUS = {
    queued: ["Queued", "active"], running: ["Running", "active"], completed: ["Completed", "ok"],
    needs_attention: ["Needs attention", "warn"], failed: ["Failed", "bad"], cancelled: ["Cancelled", "idle"],
    timed_out: ["Timed out", "bad"], interrupted: ["Interrupted", "bad"],
  };
  const OUTCOME = {
    succeeded: ["Succeeded", "ok"], failed: ["Error", "bad"], denied: ["Permission denied", "bad"],
    pending: ["Waiting for result", "active"], no_result: ["Result not recorded", "idle"],
  };
  const EDIT_TOOLS = new Set(["Write", "Edit"]);

  const $ = (id) => document.getElementById(id);
  const ui = {
    list: $("conversation-list"), meta: $("list-meta"), notice: $("list-notice"), main: $("chronology"),
    contrib: $("contributions"), search: $("search"), live: $("live"), liveText: $("live-text"),
    layout: $("layout"), jump: $("jump"), tabs: [...document.querySelectorAll(".viewtab")],
  };
  const state = {
    list: null, listTag: null, detail: null, detailTag: null, detailError: null, selected: null,
    query: "", open: new Map(), online: null, pending: false, timer: null, token: 0, seen: 0,
    mode: "team", filter: "all",
  };
  const narrow = window.matchMedia("(max-width: 759px)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // ---------- DOM helpers ----------
  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    node.append(...nodes(kids));
    return node;
  }
  // Flatten nested builder output, dropping null/false, with strings as text nodes.
  const nodes = (value) => [value].flat(Infinity).filter((kid) => kid != null && kid !== false)
    .map((kid) => (kid instanceof Node ? kid : String(kid)));

  const plural = (n, word, many = word + "s") => `${n} ${n === 1 ? word : many}`;
  const isLong = (text, chars = 1400, lines = 18) => text.length > chars || text.split("\n").length > lines;
  const isOpen = (key, fallback) => (state.open.has(key) ? state.open.get(key) : fallback);
  const now = () => Date.now() / 1000;
  // Display name of the selected conversation's Claude Code worker (e.g. Opus, Sonnet, Claude).
  const worker = () => (state.detail && state.detail.conversation.worker) || "Claude";

  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const dateFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const fullFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

  // Epoch seconds that Date/Intl can format; anything else renders as "—".
  const validTime = (ts) => typeof ts === "number" && Number.isFinite(ts) && ts > 0 && ts <= 1e11;

  function when(ts) {
    if (!validTime(ts)) return "—";
    const date = new Date(ts * 1000);
    return date.toDateString() === new Date().toDateString() ? timeFmt.format(date) : dateFmt.format(date);
  }
  function ago(ts) {
    if (!validTime(ts)) return "—";
    const s = Math.max(0, now() - ts);
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return when(ts);
  }
  function duration(sec) {
    if (sec == null || !Number.isFinite(sec) || sec < 0) return "—";
    sec = Math.round(sec);
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m}m ${String(sec % 60).padStart(2, "0")}s`;
    return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  }
  function timeEl(ts, relative = false) {
    if (!validTime(ts)) return el("span", null, "—");
    const date = new Date(ts * 1000);
    return el("time", { datetime: date.toISOString(), title: fullFmt.format(date), dataset: relative ? { ago: ts } : null },
      relative ? ago(ts) : when(ts));
  }
  function jobSeconds(job) {
    if (!validTime(job.created_at)) return null;
    return (job.finished_at || (ACTIVE.has(job.status) ? now() : job.heartbeat || job.created_at)) - job.created_at;
  }
  function wallTime(job) {
    if (ACTIVE.has(job.status) && job.created_at) return el("span", { dataset: { since: job.created_at } }, duration(jobSeconds(job)));
    return el("span", null, duration(jobSeconds(job)));
  }

  function pill(status, map = STATUS, label) {
    const [text, tone] = map[status] || [String(status || "unknown").replace(/_/g, " "), "idle"];
    return el("span", { class: "pill", dataset: { tone } }, label || text);
  }
  // Conversation-level status comes from the latest worker job only; other agents may still be working.
  function workerPill(status, name) {
    const [text] = STATUS[status] || [String(status || "unknown").replace(/_/g, " ")];
    return pill(status, STATUS, `${name || "Claude"} ${text.toLowerCase()}`);
  }

  function outcomePill(tool, status, input) {
    // A non-error result applies a Write/Edit, but for other tools it only means
    // the tool returned (a background Bash command returns as soon as it starts).
    let label;
    if (status === "succeeded") {
      label = EDIT_TOOLS.has(tool) ? "Applied" : tool !== "Bash" ? "Tool returned"
        : input && input.run_in_background === true ? "Started in background" : "Command returned";
    }
    if (status === "failed") label = EDIT_TOOLS.has(tool) ? "Not applied (error)" : tool === "Bash" ? "Command error" : "Tool error";
    return pill(status, OUTCOME, label);
  }

  function rel(path, cwd) {
    if (typeof path !== "string") return "";
    return cwd && path.startsWith(cwd + "/") ? path.slice(cwd.length + 1) : path;
  }

  function copyButton(key, label, getText) {
    const button = el("button", { type: "button", class: "btn", "aria-label": `Copy ${label}`, dataset: { key: "copy:" + key } }, "Copy");
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(getText());
        button.textContent = "Copied";
      } catch {
        button.textContent = "Copy failed";
      }
      setTimeout(() => { button.textContent = "Copy"; }, 1600);
    });
    return button;
  }

  let uid = 0;
  function collapsible(key, content, long, noun) {
    if (!long) return content;
    const id = "clamp-" + (++uid);
    const box = el("div", { class: "clamp", id }, content);
    const button = el("button", { type: "button", class: "btn link clamp-toggle", "aria-controls": id, dataset: { key: "clamp:" + key } });
    const apply = (open) => {
      box.classList.toggle("is-collapsed", !open);
      button.setAttribute("aria-expanded", String(open));
      button.textContent = open ? "Show less" : `Show full ${noun}`;
    };
    apply(isOpen(key, false));
    button.addEventListener("click", () => {
      const open = button.getAttribute("aria-expanded") !== "true";
      state.open.set(key, open);
      apply(open);
    });
    return el("div", null, box, button);
  }

  function disclosure(key, fallback, labels, build) {
    // A button-controlled region whose open state survives periodic refreshes.
    const id = "region-" + (++uid);
    const region = el("div", { id, class: "step-detail" });
    const button = el("button", { type: "button", class: "btn link", "aria-controls": id, dataset: { key: "disc:" + key } });
    const apply = (open) => {
      button.setAttribute("aria-expanded", String(open));
      button.textContent = open ? labels[1] : labels[0];
      region.hidden = !open;
      if (open && !region.childElementCount) region.append(...nodes(build()));
    };
    apply(isOpen(key, fallback));
    button.addEventListener("click", () => {
      const open = button.getAttribute("aria-expanded") !== "true";
      state.open.set(key, open);
      apply(open);
    });
    return [button, region];
  }

  function details(key, fallback, className, summary, build) {
    const node = el("details", { class: className });
    node.append(el("summary", { dataset: { key: "details:" + key } }, summary));
    if (isOpen(key, fallback)) {
      node.open = true;
      node.append(...nodes(build()));
    }
    node.addEventListener("toggle", () => {
      state.open.set(key, node.open);
      if (node.open && node.childElementCount === 1) node.append(...nodes(build()));
    });
    return node;
  }

  function codeBlock(text, label, extra = "") {
    return el("pre", { class: `code ${extra}`, tabindex: "0", "aria-label": label || "Code" }, el("code", null, text));
  }
  function longCode(key, text, label, extra) {
    return collapsible(key, codeBlock(text, label, extra), isLong(text, 3000, 30), label ? label.toLowerCase() : "code");
  }

  // ---------- Markdown-lite (DOM nodes only) ----------
  function inline(text) {
    const frag = document.createDocumentFragment();
    const pattern = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*)/g;
    let last = 0;
    let match;
    while ((match = pattern.exec(text))) {
      if (match.index > last) frag.append(text.slice(last, match.index));
      frag.append(match[1] ? el("code", null, match[1].slice(1, -1)) : el("strong", null, match[2].slice(2, -2)));
      last = pattern.lastIndex;
    }
    if (last < text.length) frag.append(text.slice(last));
    return frag;
  }
  function markdown(text) {
    const root = el("div", { class: "md" });
    const lines = text.replace(/\r\n?/g, "\n").split("\n");
    const itemRe = /^\s*([-*+]|\d+[.)])\s+(.*)$/;
    let para = [];
    const flush = () => {
      if (!para.length) return;
      const p = el("p");
      para.forEach((line, n) => { if (n) p.append(el("br")); p.append(inline(line)); });
      root.append(p);
      para = [];
    };
    for (let i = 0; i < lines.length;) {
      const line = lines[i];
      const fence = line.match(/^\s*(```+|~~~+)\s*([\w+.#-]*)/);
      if (fence) {
        flush();
        const body = [];
        for (i++; i < lines.length && !lines[i].trim().startsWith(fence[1]); i++) body.push(lines[i]);
        i++;
        root.append(codeBlock(body.join("\n"), fence[2] ? `${fence[2]} code` : "Code"));
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flush();
        root.append(el(heading[1].length <= 2 ? "h3" : "h4", null, inline(heading[2])));
        i++;
        continue;
      }
      const item = line.match(itemRe);
      if (item) {
        flush();
        const ordered = /\d/.test(item[1]);
        const list = el(ordered ? "ol" : "ul", ordered && parseInt(item[1], 10) !== 1 ? { start: parseInt(item[1], 10) } : null);
        while (i < lines.length) {
          const next = lines[i].match(itemRe);
          if (next && /\d/.test(next[1]) === ordered) {
            list.append(el("li", null, inline(next[2])));
          } else if (!next && /^\s{2,}\S/.test(lines[i]) && list.lastChild) {
            list.lastChild.append(el("br"), inline(lines[i].trim()));
          } else break;
          i++;
        }
        root.append(list);
        continue;
      }
      if (/^\s*>/.test(line)) {
        flush();
        const quote = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
        root.append(el("blockquote", null, inline(quote.join(" "))));
        continue;
      }
      if (!line.trim()) flush();
      else para.push(line);
      i++;
    }
    flush();
    return root;
  }

  // ---------- Sidebar ----------
  function renderList() {
    const data = state.list;
    if (!data) return;
    const all = data.conversations;
    const q = state.query.trim().toLowerCase();
    const items = all.filter((c) => !q || [c.title, c.project, c.cwd, c.snippet, c.mode, (STATUS[c.status] || [c.status])[0]]
      .join(" ").toLowerCase().includes(q));
    ui.meta.textContent = !all.length ? "" : q ? `${items.length} of ${plural(all.length, "conversation")}`
      : plural(all.length, "conversation") + (data.active_count ? ` · ${plural(data.active_count, "worker job")} active` : "");
    ui.notice.replaceChildren(...(data.skipped_records ? [el("p", { class: "banner warn" },
      `${plural(data.skipped_records, "job record")} could not be read (partial or malformed). They will be retried.`)] : []));
    preserve([ui.list.parentElement], () => {
      ui.list.replaceChildren(...items.map((c) => el("li", null, el("button", {
        type: "button", class: "conv-item", "aria-current": c.conversation_id === state.selected ? "true" : null,
        dataset: { key: "conv:" + c.conversation_id, id: c.conversation_id },
        onclick: () => select(c.conversation_id, true),
      },
      el("span", { class: "conv-title" }, c.title),
      el("span", { class: "conv-meta" },
        workerPill(c.status, c.worker),
        c.project ? el("span", { class: "conv-project", title: c.cwd }, c.project) : null,
        el("span", null, plural(c.request_count, `${c.worker || "Claude"} request`)),
        c.updated_at ? el("span", null, timeEl(c.updated_at, true)) : null)))));
      if (!items.length) {
        ui.list.append(el("li", { class: "note" }, all.length ? `No conversations match “${state.query.trim()}”.` : "No conversations yet."));
      }
    });
  }

  function moveListFocus(event) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const buttons = [...ui.list.querySelectorAll(".conv-item")];
    const index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    buttons[Math.max(0, Math.min(buttons.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))].focus();
  }

  // ---------- Chronology ----------
  function placeholder(title, ...lines) {
    return el("div", { class: "placeholder" }, title ? el("h2", null, title) : null, lines.map((line) => el("p", null, line)));
  }

  function mainContent() {
    if (!state.list) {
      return [state.online === false ? placeholder("Dashboard service unavailable",
        "The page couldn't reach the local dashboard service. It will keep retrying.") : placeholder(null, "Loading conversations…")];
    }
    if (!state.list.conversations.length) {
      return [placeholder("No bridge conversations yet",
        "When Codex delegates work to the Claude Code worker through Crewview, each request and the worker's reply appears here automatically.",
        state.list.state_exists ? "The bridge state folder exists but has no readable jobs yet."
          : "The bridge state folder hasn't been created yet; it appears with the first job.",
        el("code", null, state.list.state_dir))];
    }
    if (!state.selected) return [placeholder("Select a conversation", "Choose a conversation from the list to read it.")];
    if (state.detailError === "gone") return [placeholder("Conversation not found", "It is no longer present in the bridge state.")];
    if (!state.detail) return [placeholder(null, "Loading conversation…")];
    const { conversation: c, turns, team } = state.detail;
    const body = state.mode === "team" ? renderTeamFeed(team, turns)
      : el("ol", { class: "timeline", "aria-label": `${worker()} requests and replies` },
        turns.map((turn, i) => renderTurn(turn, i, turns.length, c.cwd)));
    return [renderHead(c, turns), renderTeamBar(team), body];
  }

  // ---------- Team (all agents) ----------
  const AGENT_TONES = new Set(["Astra", "Sol", "Luna", "User", "Codex"]);
  const HANDOFF_LABELS = {
    spawn_agent: "started a subagent", send_message: "sent a message", followup_task: "sent a follow-up task",
    send_input: "sent input", claude_start: "delegated work via Claude Code", claude_reply: "sent a follow-up via Claude Code",
  };
  const agentTone = (name) => (name === worker() ? "opus" : AGENT_TONES.has(name) ? name.toLowerCase() : "other");

  function nameCounts(team) {
    const counts = new Map();
    for (const item of team.items) {
      if (item.kind === "tools" || item.kind === "handoff") continue;
      if (item.kind === "opus_request") continue;
      counts.set(item.name, (counts.get(item.name) || 0) + 1);
    }
    for (const p of team.participants) if (!counts.has(p.name)) counts.set(p.name, 0);
    return counts;
  }

  function visibleItem(item) {
    const f = state.filter;
    if (f === "all" || item.name === f) return true;
    return item.kind === "handoff" && (item.target_name === f || (item.bridge && f === worker()));
  }

  function setMode(mode, focusItem) {
    state.mode = mode;
    renderDetail();
    const node = focusItem && ui.main.querySelector(`[data-item="${CSS.escape(focusItem)}"]`);
    if (node) node.scrollIntoView({ block: "start", behavior: reducedMotion.matches ? "auto" : "smooth" });
    else ui.main.scrollTop = 0;
  }

  function renderTeamBar(team) {
    const counts = nameCounts(team);
    if (state.filter !== "all" && !counts.has(state.filter)) state.filter = "all";
    const modeButton = (mode, label) => el("button", {
      type: "button", class: "seg", "aria-pressed": String(state.mode === mode), dataset: { key: "mode:" + mode },
      onclick: () => setMode(mode),
    }, label);
    const filterButton = (value, label, count) => el("button", {
      type: "button", class: "chip", "aria-pressed": String(state.filter === value), dataset: { key: "filter:" + value, tone: value === "all" ? null : agentTone(value) },
      onclick: () => { state.filter = value; renderDetail(); },
    }, label, count != null ? el("span", { class: "chip-count" }, String(count)) : null);
    const cards = team.participants.map((p) => el("li", { class: "agent-card", dataset: { tone: agentTone(p.name) } },
      el("span", { class: "agent-name" }, p.name, p.nickname ? el("span", { class: "muted small" }, ` · ${p.nickname}`) : null),
      el("span", { class: "agent-role" }, p.role + (p.via ? ` · via ${p.via}` : "")),
      el("span", { class: "agent-model mono", title: p.path || null },
        p.key === "user" ? "person" : p.models.length ? p.models.join(", ") : "model unknown"),
      el("span", { class: "agent-count" }, plural(p.message_count, "message"))));
    return el("section", { class: "team-bar", "aria-label": "Participants" },
      el("div", { class: "team-controls" },
        el("div", { class: "segmented", role: "group", "aria-label": "View" },
          modeButton("team", "All agents"), modeButton("opus", `${worker()} details`)),
        state.mode === "team" ? el("div", { class: "chips", role: "group", "aria-label": "Filter by participant" },
          filterButton("all", "All"), [...counts].map(([name, count]) => filterButton(name, name, count))) : null),
      el("ul", { class: "agent-cards" }, cards));
  }

  function renderTeamFeed(team, turns) {
    const byJob = new Map(turns.map((t) => [t.job_id, t]));
    const notices = team.coverage.map((n) => el("p", { class: `banner${n.level === "warn" ? " warn" : ""}` }, n.text));
    const items = team.items.filter(visibleItem);
    return el("div", { class: "team" },
      el("div", { class: "coverage", role: "note" }, notices),
      items.length ? el("ol", { class: "feed", "aria-label": "All agents, in time order" },
        items.map((item) => renderFeedItem(item, byJob)))
        : el("p", { class: "note" }, "Nothing from this participant yet."));
  }

  function feedHead(item, who, sub, extra) {
    return el("div", { class: "msg-head" },
      el("span", { class: "who" }, who), sub ? el("span", { class: "msg-sub" }, sub) : null, extra,
      el("span", { class: "spacer" }), el("span", { class: "step-time" }, timeEl(item.at)));
  }

  function renderFeedItem(item, byJob) {
    const li = (className, ...kids) => el("li", { class: `feed-item ${className}`, dataset: { item: item.id, tone: agentTone(item.name) } }, kids);
    if (item.kind === "human") {
      return li("", el("article", { class: "msg msg-human", "aria-label": "User request" },
        feedHead(item, "User", "Request"),
        el("div", { class: "msg-body" }, collapsible("h:" + item.id, el("div", { class: "plain-text" }, item.text), isLong(item.text), "request"))));
    }
    if (item.kind === "agent_message") {
      const final = item.phase === "final";
      return li(final ? "" : "is-commentary", el("article", { class: "msg msg-agent", "aria-label": `${item.name} ${final ? "final answer" : "update"}` },
        feedHead(item, item.name, final ? "Final answer" : "Update",
          el("span", { class: "msg-sub mono" }, item.model || "model unknown")),
        el("div", { class: "msg-body" },
          collapsible("a:" + item.id, markdown(item.text), final ? isLong(item.text, 6000, 80) : isLong(item.text, 900, 12), final ? "answer" : "update"),
          item.truncated ? el("p", { class: "muted small" }, "Long message shortened.") : null)));
    }
    if (item.kind === "handoff") {
      const target = item.target_name || item.target || "an agent";
      return li("marker", el("span", { class: "marker-text" },
        el("strong", null, item.name), " → ", el("strong", { title: item.target || null }, target), " ",
        el("span", { class: "muted" }, HANDOFF_LABELS[item.tool] || item.tool),
        item.target_model ? el("span", { class: "mono small muted" }, ` · ${item.target_model}`) : null,
        item.encrypted ? el("span", { class: "muted small" }, " · message encrypted in local log") : null),
        el("span", { class: "step-time" }, timeEl(item.at)));
    }
    if (item.kind === "tools") {
      const total = Object.values(item.counts).reduce((a, b) => a + b, 0);
      const [toggle, region] = disclosure("tools:" + item.id, false, ["Show tools", "Hide tools"],
        () => el("p", { class: "muted small mono" }, Object.entries(item.counts).map(([n, c]) => `${n} ×${c}`).join(", ")));
      return li("marker is-tools", el("span", { class: "marker-text" },
        el("strong", null, item.name), el("span", { class: "muted" }, ` used ${plural(total, "tool")}`), " ", toggle),
        el("span", { class: "step-time" }, timeEl(item.at)), region);
    }
    const t = byJob.get(item.job_id);
    if (!t) return null;
    const detailsButton = el("button", { type: "button", class: "btn link", dataset: { key: "opus-details:" + item.id },
      onclick: () => setMode("opus", "turn:" + t.job_id) }, `${worker()} details`);
    if (item.kind === "opus_request") {
      const prompt = t.prompt || "";
      return li("", el("article", { class: "msg msg-codex", "aria-label": `Request ${item.request} to ${worker()}` },
        feedHead(item, `Codex → ${worker()}`, `Request ${item.request}`, pill(t.status)),
        el("div", { class: "msg-body" },
          collapsible("tp:" + t.job_id, el("div", { class: "plain-text" }, prompt), isLong(prompt, 900, 12), "request"), detailsButton)));
    }
    const a = t.activity;
    const tools = a.events.filter((e) => e.kind === "tool_use");
    const applied = tools.filter((e) => EDIT_TOOLS.has(e.name) && a.outcomes[e.id] === "succeeded").length;
    return li("", renderReply(t), el("p", { class: "feed-foot muted small" },
      a.available ? `${plural(tools.length, "tool call")} · ${plural(applied, "edit")} applied · ` : "Final reply only · ", detailsButton),
      renderAttention(t));
  }

  function renderHead(c, turns) {
    const agentTurns = turns.reduce((n, t) => n + (t.num_turns || 0), 0);
    const anyTurns = turns.some((t) => t.num_turns != null);
    const active = turns.some((t) => ACTIVE.has(t.status));
    const total = turns.reduce((n, t) => n + (jobSeconds(t) || 0), 0);
    const reported = c.actual_models.length ? c.actual_models.join(", ") : "not reported yet";
    const name = worker();
    return el("header", { class: "conv-head" },
      el("div", { class: "eyebrow" },
        c.project ? el("span", { class: "mono", title: c.cwd }, c.project) : null,
        c.mode ? el("span", null, c.mode === "consult" ? "Consult (read-only)" : "Implement") : null,
        c.created_at ? el("span", null, "Started ", timeEl(c.created_at)) : null),
      el("h2", null, c.title),
      el("div", { class: "facts" },
        workerPill(c.status, name),
        el("span", null, `${name} model reported: `, el("strong", { class: "mono" }, reported)),
        el("span", null, el("strong", null, String(turns.length)), turns.length === 1 ? ` ${name} request` : ` ${name} requests`),
        anyTurns ? el("span", null, el("strong", null, String(agentTurns)), " Claude agent turns") : null,
        el("span", { title: `Sum of each ${name} request's time from submission to finish` }, `${name} wall time `,
          el("strong", null, active ? el("span", { dataset: { total: "1" } }, duration(total)) : duration(total)))),
      details("tech:" + c.conversation_id, false, "tech", "Technical details", () => el("dl", null,
        el("dt", null, "Conversation"), el("dd", null, c.conversation_id),
        el("dt", null, "Directory"), el("dd", null, c.cwd || "—"),
        el("dt", null, "Claude session"), el("dd", null, c.session_id || "—"),
        el("dt", null, "Codex thread"), el("dd", null, c.codex_thread_id || "not linked"),
        el("dt", null, "Latest job"), el("dd", null, c.latest_job_id))));
  }

  function renderTurn(t, index, count, cwd) {
    const prompt = t.prompt || "";
    const followUp = index > 0;
    const mentionsSol = /\bSol\b/.test(prompt) && /review/i.test(prompt);
    return el("li", { class: "turn", id: "turn-" + t.job_id, dataset: { item: "turn:" + t.job_id }, "aria-label": `Request ${index + 1} of ${count}` },
      el("div", { class: "turn-head" },
        el("h3", null, `Request ${index + 1}`), pill(t.status),
        el("span", null, timeEl(t.created_at)), el("span", null, "· ", wallTime(t))),
      el("article", { class: "msg msg-codex", "aria-label": `Codex ${followUp ? "follow-up" : "task"}` },
        el("div", { class: "msg-head" },
          el("span", { class: "who" }, "Codex"),
          el("span", { class: "msg-sub" }, followUp ? "Follow-up" : "Task"),
          mentionsSol ? el("span", { class: "tag", title: "This request's text mentions a Sol review" }, "Mentions Sol review") : null,
          el("span", { class: "spacer" }),
          copyButton("p:" + t.job_id, "Codex request", () => prompt)),
        el("div", { class: "msg-body" },
          collapsible("p:" + t.job_id, el("div", { class: "plain-text" }, prompt), isLong(prompt), "request"))),
      renderActivity(t, cwd),
      renderReply(t),
      renderAttention(t),
      details("job:" + t.job_id, false, "tech", "Request details", () => jobDetails(t)));
  }

  function renderActivity(t, cwd) {
    const a = t.activity;
    const section = el("section", { class: "activity", "aria-label": `${worker()} activity` });
    if (!a.available) {
      section.append(el("p", { class: "note" }, ACTIVE.has(t.status)
        ? "Live activity isn't captured for this request: it was started by a bridge worker without activity logging. The final reply will appear when it finishes."
        : "No activity log for this request. It ran before live capture was added, so only the final reply is shown."));
      return section;
    }
    const reply = (t.response || "").trim();
    const steps = a.events.filter((e) => ["text", "tool_use", "notice", "truncated"].includes(e.kind)
      && !(e.kind === "text" && reply && e.text.trim() === reply));
    const results = new Map(a.events.filter((e) => e.kind === "tool_result").map((e) => [e.tool_use_id, e]));
    const tools = a.events.filter((e) => e.kind === "tool_use");
    const applied = tools.filter((e) => EDIT_TOOLS.has(e.name) && a.outcomes[e.id] === "succeeded").length;
    const session = a.events.find((e) => e.kind === "session");
    const live = ACTIVE.has(t.status);
    const [toggle, region] = disclosure("act:" + t.job_id, live || steps.length <= 8,
      [`Show ${plural(steps.length, "step")}`, "Hide steps"], () => {
        if (!steps.length) return el("p", { class: "note" }, live ? "No activity recorded yet." : "No intermediate steps were recorded.");
        return el("ol", { class: "steps" }, steps.map((e) => renderStep(e, t, results, cwd)));
      });
    section.append(el("div", { class: "activity-head" },
      el("h4", null, `${worker()} activity`),
      el("span", null, plural(tools.length, "tool call")),
      el("span", null, plural(applied, "edit") + " applied"),
      session && session.model ? el("span", { class: "mono small" }, `session model ${session.model}`) : null,
      steps.length ? toggle : null));
    if (a.truncated) section.append(el("p", { class: "note" }, "The activity log reached its size limit; later steps were not recorded."));
    if (a.malformed) section.append(el("p", { class: "note" }, `${plural(a.malformed, "log line")} could not be read.`));
    section.append(steps.length ? region : el("p", { class: "note" }, live ? `Waiting for ${worker()}'s first recorded step…` : "No intermediate steps were recorded."));
    return section;
  }

  function stepTarget(e, cwd) {
    const input = e.input || {};
    switch (e.name) {
      case "Read": return [rel(input.file_path, cwd), input.offset ? `from line ${input.offset}` : ""];
      case "Write": return [rel(input.file_path, cwd), typeof input.content === "string" ? plural(input.content.split("\n").length, "line") : ""];
      case "Edit": return [rel(input.file_path, cwd), input.replace_all ? "all occurrences" : ""];
      case "Bash": return [input.description || (input.command || "").split("\n")[0], ""];
      case "Glob": return [input.pattern || "", input.path ? `in ${rel(input.path, cwd)}` : ""];
      case "Grep": return [input.pattern || "", [input.glob, input.path && rel(input.path, cwd)].filter(Boolean).join(" · ")];
      default: return [Object.entries(input).slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(" · "), ""];
    }
  }

  function toolDetail(e, result, key) {
    const input = e.input || {};
    const parts = [];
    const cut = new Set(e.truncated || []);
    if (e.name === "Write" && typeof input.content === "string") {
      parts.push(el("div", { class: "code-label" }, el("span", null, cut.has("content") ? "Recorded content (truncated)" : "Recorded content"),
        copyButton(key + ":w", "written content", () => input.content)), longCode(key + ":w", input.content, "Written content"));
    }
    if (e.name === "Edit") {
      parts.push(el("div", { class: "code-label" }, el("span", null, "Replaced" + (cut.has("old_string") ? " (truncated)" : ""))),
        longCode(key + ":o", input.old_string || "", "Replaced text", "del"),
        el("div", { class: "code-label" }, el("span", null, "With" + (cut.has("new_string") ? " (truncated)" : "")),
          copyButton(key + ":n", "new text", () => input.new_string || "")),
        longCode(key + ":n", input.new_string || "", "New text", "add"));
    }
    if (e.name === "Bash" && input.command) {
      parts.push(el("div", { class: "code-label" }, el("span", null, "Command"), copyButton(key + ":c", "command", () => input.command)),
        codeBlock(input.command, "Command", "wrap"));
    }
    if (result && (result.text || result.summary)) {
      parts.push(el("div", { class: "code-label" }, el("span", null, result.is_error ? "Tool error" : "Result" + (result.truncated ? " (excerpt)" : ""))),
        result.summary ? el("p", { class: "muted small" }, result.summary) : longCode(key + ":r", result.text, "Result", "wrap"));
    }
    return parts.length ? parts : el("p", { class: "muted small" }, "No further details were recorded.");
  }

  function renderStep(e, t, results, cwd) {
    if (e.kind === "text") {
      return el("li", { class: "step" }, el("div", { class: "step-text" },
        collapsible("s:" + t.job_id + ":" + e.seq, markdown(e.text), isLong(e.text, 1200, 14), "message")));
    }
    if (e.kind !== "tool_use") return el("li", { class: "step" }, el("p", { class: "note" }, e.text || ""));
    const status = t.activity.outcomes[e.id] || "no_result";
    const [target, desc] = stepTarget(e, cwd);
    const key = "d:" + e.id;
    const labels = e.name === "Write" || e.name === "Edit" ? ["Show change", "Hide change"]
      : e.name === "Bash" ? ["Show command", "Hide command"] : ["Show result", "Hide result"];
    const [toggle, region] = disclosure(key, false, labels, () => toolDetail(e, results.get(e.id), key));
    return el("li", { class: "step" },
      el("div", { class: "step-row" },
        el("span", { class: "tool-name" }, e.name),
        el("span", { class: "step-target", title: (e.input && (e.input.file_path || e.input.command)) || null }, target || "—"),
        desc ? el("span", { class: "step-desc" }, desc) : null,
        outcomePill(e.name, status, e.input), toggle,
        el("span", { class: "step-time" }, timeEl(e.at))),
      region);
  }

  function renderReply(t) {
    const models = t.actual_models.length ? t.actual_models.join(", ") : null;
    const response = t.response || "";
    let body;
    if (response.trim()) {
      body = collapsible("r:" + t.job_id, markdown(response), isLong(response, 6000, 80), "reply");
    } else if (ACTIVE.has(t.status)) {
      body = el("div", { class: "working" }, pill(t.status),
        el("span", null, `${worker()} is working on this request · `, wallTime(t),
          t.heartbeat ? el("span", null, " · last heartbeat ", timeEl(t.heartbeat, true)) : null));
    } else {
      body = el("p", { class: "muted" }, "No reply was recorded for this request.");
    }
    return el("article", { class: "msg msg-opus", "aria-label": `${worker()} reply` },
      el("div", { class: "msg-head" },
        el("span", { class: "who" }, worker()),
        el("span", { class: "msg-sub mono" }, models ? `reported ${models}` : `requested ${t.requested_model || "model"} · usage not reported yet`),
        el("span", { class: "spacer" }),
        response.trim() ? copyButton("r:" + t.job_id, `${worker()} reply`, () => response) : null),
      el("div", { class: "msg-body" }, body));
  }

  function denialTarget(input) {
    return input.command || input.file_path || input.pattern || Object.values(input)[0] || "";
  }

  function renderAttention(t) {
    const parts = [];
    if (t.permission_denials.length) {
      parts.push(el("div", { class: "attention", role: "note" },
        el("h4", null, `Permission denied (${t.permission_denials.length})`),
        el("ul", null, t.permission_denials.map((d) => el("li", null, el("strong", null, d.tool_name), " ",
          denialTarget(d.input) ? el("code", null, denialTarget(d.input)) : null))),
        el("p", null, "The bridge does not retry denied operations. They need a decision from you or Codex.")));
    }
    if (t.model_warning) {
      parts.push(el("div", { class: "attention", role: "note" }, el("h4", null, "Model check"), el("p", null, t.model_warning)));
    }
    if (t.error && t.status !== "completed") {
      const title = t.status === "interrupted" ? "Interrupted" : t.status === "cancelled" || t.status === "timed_out" ? "Stopped" : "Error";
      parts.push(el("div", { class: "attention bad", role: "note" }, el("h4", null, title), el("p", { class: "plain-text" }, t.error)));
    }
    return parts;
  }

  function jobDetails(t) {
    const rows = [
      ["Job", t.job_id], ["Claude session", t.session_id], ["Requested model", t.requested_model],
      ["Reported models", t.actual_models.join(", ")], ["Claude agent turns", t.num_turns],
      ["Exit code", t.exit_code], ["Limits", t.timeout_seconds ? `${duration(t.timeout_seconds)}, ${t.max_turns} turns` : null],
      ["Input tokens", t.usage.input_tokens], ["Output tokens", t.usage.output_tokens],
      ["Cache read tokens", t.usage.cache_read_input_tokens],
      ["Cost estimate", t.cost_estimate_usd != null ? `$${t.cost_estimate_usd.toFixed(4)} (Claude's token estimate, not a subscription invoice)` : null],
      ["Submitted", validTime(t.created_at) ? fullFmt.format(new Date(t.created_at * 1000)) : null],
      ["Finished", validTime(t.finished_at) ? fullFmt.format(new Date(t.finished_at * 1000)) : null],
    ];
    return el("dl", null, rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => [el("dt", null, k), el("dd", null, String(v))]));
  }

  // ---------- Contributions ----------
  function jumpToItem(id) {
    state.filter = "all";
    if (narrow.matches || window.matchMedia("(max-width: 1179px)").matches) setView("chronology");
    setMode("team", id);
  }

  function teamContributions(team) {
    if (!team.linked || !team.available) return [];
    const agents = team.participants.filter((p) => p.key !== "user" && p.key !== "opus");
    const c = team.coordination;
    return [
      section("Team", [
        el("ul", { class: "team-list" }, agents.map((p) => el("li", { dataset: { tone: agentTone(p.name) } },
          el("strong", null, p.name), el("span", { class: "muted small" },
            ` · ${p.role} · ${plural(p.message_count, "message")}${p.final_count ? `, ${p.final_count} final` : ""}`)))),
        c.names.length ? el("p", { class: "contrib-sub" },
          `${c.names.join(", ")} (main thread): ${plural(c.handoffs, "handoff")}, ${plural(c.finals, "final answer")}.`) : null,
        el("p", { class: "contrib-sub" }, "Agent messages and reviews are written replies, not file edits."),
      ]),
      team.reviews.length ? section("Reviews", team.reviews.map((r) => el("button", {
        type: "button", class: "review-link", dataset: { key: "review:" + r.id, tone: agentTone(r.name) },
        onclick: () => jumpToItem(r.id),
      }, el("span", { class: "review-meta" }, el("strong", null, r.name), " · ", timeEl(r.at)),
      el("span", { class: "review-excerpt" }, r.excerpt)))) : null,
    ].filter(Boolean);
  }

  function contribContent() {
    const d = state.detail;
    if (!d) return [placeholder(null, "Contributions appear once a conversation is selected.")];
    const k = d.contributions;
    const cwd = d.conversation.cwd;
    const out = [el("h2", null, "Contributions"), ...teamContributions(d.team),
      el("h3", { class: "contrib-author", dataset: { tone: "opus" } }, `${worker()} · Claude Code`),
      el("p", { class: "contrib-sub" }, `What ${worker()} changed and ran in this conversation, from recorded tool calls.`)];
    if (!k.requests_with_activity) {
      out.push(el("p", { class: "note" }, k.request_count === 1
        ? `This request has no activity log (it ran before live capture was added), so edits and commands can't be listed. ${worker()}'s final reply summarizes what it did.`
        : `None of these ${k.request_count} requests has an activity log (they ran before live capture was added), so edits and commands can't be listed. ${worker()}'s final replies summarize what it did.`));
    } else {
      out.push(el("div", { class: "stats" },
        stat(k.files.length, k.files.length === 1 ? "file with recorded edits" : "files with recorded edits"),
        stat(k.applied_edit_count, k.applied_edit_count === 1 ? "edit applied" : "edits applied"),
        stat(k.commands.length, k.commands.length === 1 ? "command recorded" : "commands recorded")));
      if (k.requests_with_activity < k.request_count) {
        out.push(el("p", { class: "contrib-sub" }, `${k.requests_with_activity} of ${k.request_count} requests have activity logs; the others show final replies only.`));
      }
      out.push(el("p", { class: "contrib-sub" }, "Edits are recorded Write/Edit tool calls with a successful tool result, not an independently verified git diff. Files changed by shell commands aren't listed; check the working tree for the authoritative state."));
      out.push(section("Recorded file edits", k.files.length ? k.files.map((f, i) => renderFile(f, cwd, k.files.length <= 3 || i === 0))
        : el("p", { class: "muted small" }, "No applied edits were recorded.")));
      if (k.not_applied.length) {
        out.push(section("Edits not applied", k.not_applied.map((e) => el("div", { class: "cmd" },
          el("div", { class: "cmd-row" }, outcomePill(e.tool, e.status, e.input), el("span", { class: "muted small" }, `Request ${e.request} · ${e.tool}`),
            el("span", { class: "step-target" }, rel(e.input.file_path, cwd) || "—"))))));
      }
      out.push(section("Checks & commands", k.commands.length ? [
        el("p", { class: "contrib-sub" }, "“Command returned” means the tool reported no error; read the output to judge the result."),
        k.commands.map((c) => renderCommand(c))] : el("p", { class: "muted small" }, "No commands were recorded.")));
      const others = Object.entries(k.tool_counts).filter(([name]) => !EDIT_TOOLS.has(name) && name !== "Bash");
      if (others.length) out.push(el("p", { class: "contrib-sub" }, "Other tools: " + others.map(([n, c]) => `${n} ×${c}`).join(", ")));
    }
    if (k.denials.length) {
      out.push(section("Permission issues", k.denials.map((dn) => el("div", { class: "cmd" },
        el("div", { class: "cmd-row" }, pill("denied", OUTCOME), el("span", { class: "muted small" }, `Request ${dn.request} · ${dn.tool_name}`),
          denialTarget(dn.input) ? el("span", { class: "step-target" }, denialTarget(dn.input)) : null)))));
    }
    return out;
  }

  const stat = (n, label) => el("div", { class: "stat" }, el("b", null, String(n)), el("span", null, label));
  const section = (title, ...body) => el("section", { class: "section", "aria-label": title }, el("h3", null, title), body);

  function renderFile(file, cwd, openByDefault) {
    const path = rel(file.path, cwd);
    return details("file:" + file.path, openByDefault, "file",
      [el("span", { class: "file-path", title: file.path }, path), el("span", { class: "file-count" }, plural(file.edits.length, "edit"))],
      () => file.edits.map((e) => {
        const key = "f:" + e.tool_use_id;
        const input = e.input;
        const cut = new Set(e.truncated);
        const body = e.tool === "Write"
          ? [el("div", { class: "code-label" }, el("span", null, "Wrote file" + (cut.has("content") ? " (content truncated)" : "")),
            copyButton(key + ":w", "written content", () => input.content || "")), longCode(key + ":w", input.content || "", "Written content")]
          : [el("div", { class: "code-label" }, el("span", null, "Replaced" + (input.replace_all ? " (all occurrences)" : ""))),
            longCode(key + ":o", input.old_string || "", "Replaced text", "del"),
            el("div", { class: "code-label" }, el("span", null, "With"), copyButton(key + ":n", "new text", () => input.new_string || "")),
            longCode(key + ":n", input.new_string || "", "New text", "add")];
        return el("div", { class: "edit" },
          el("div", { class: "edit-meta" }, outcomePill(e.tool, e.status), el("span", null, `Request ${e.request} · ${e.tool}`), timeEl(e.at)),
          body);
      }));
  }

  function renderCommand(c) {
    const input = c.input;
    const key = "cmd:" + c.tool_use_id;
    const [toggle, region] = disclosure(key, false, ["Show command", "Hide command"], () => [
      el("div", { class: "code-label" }, el("span", null, "Command"), copyButton(key + ":c", "command", () => input.command || "")),
      codeBlock(input.command || "", "Command", "wrap"),
      c.output ? [el("div", { class: "code-label" }, el("span", null, c.output_truncated ? "Output (excerpt)" : "Output")),
        longCode(key + ":o", c.output, "Output", "wrap")] : null]);
    return el("div", { class: "cmd" },
      el("div", { class: "cmd-row" }, outcomePill("Bash", c.status, c.input), el("span", { class: "muted small" }, `Request ${c.request}`),
        el("span", { class: "step-target" }, input.description || (input.command || "").split("\n")[0]), toggle),
      region);
  }

  // ---------- Rendering with preserved reading position ----------
  function preserve(panes, fn) {
    const tops = panes.map((pane) => pane.scrollTop);
    const key = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;
    fn();
    panes.forEach((pane, i) => { pane.scrollTop = tops[i]; });
    if (key) {
      const next = document.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (next && next !== document.activeElement) next.focus({ preventScroll: true });
    }
  }

  function selectionInside(node) {
    const selection = window.getSelection();
    return selection && !selection.isCollapsed && selection.rangeCount > 0 && node.contains(selection.getRangeAt(0).commonAncestorContainer);
  }

  function renderDetail() {
    // Never replace text the reader is selecting; render once the selection clears.
    if (selectionInside(ui.main) || selectionInside(ui.contrib)) {
      state.pending = true;
      return;
    }
    state.pending = false;
    uid = 0;
    preserve([ui.main, ui.contrib], () => {
      ui.main.replaceChildren(...mainContent());
      ui.contrib.replaceChildren(...contribContent());
    });
    document.title = state.detail ? `${state.detail.conversation.title} · Crewview` : "Crewview";
  }

  function nearBottom(pane) {
    return pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
  }

  function noteNewActivity() {
    const d = state.detail;
    const count = d.turns.reduce((n, t) => n + 1 + t.activity.events.length + (t.response ? 1 : 0), 0) + d.team.items.length;
    if (state.seen && count > state.seen && !nearBottom(ui.main)) ui.jump.hidden = false;
    state.seen = count;
  }

  function updateClocks() {
    for (const node of document.querySelectorAll("[data-since]")) node.textContent = duration(now() - Number(node.dataset.since));
    for (const node of document.querySelectorAll("[data-ago]")) node.textContent = ago(Number(node.dataset.ago));
    const total = document.querySelector("[data-total]");
    if (total && state.detail) total.textContent = duration(state.detail.turns.reduce((n, t) => n + (jobSeconds(t) || 0), 0));
  }

  // ---------- Data ----------
  async function fetchJSON(url, tag) {
    const headers = { Accept: "application/json" };
    if (tag) headers["If-None-Match"] = tag;
    const response = await fetch(url, { headers, cache: "no-store", credentials: "same-origin" });
    if (response.status === 304) return null;
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return { data: await response.json(), tag: response.headers.get("ETag") };
  }

  function hashId() {
    // A malformed fragment (e.g. "#%") means no selection rather than a crash.
    let id;
    try {
      id = decodeURIComponent(location.hash.slice(1));
    } catch {
      return null;
    }
    return /^[0-9a-f-]{36}$/.test(id) ? id : null;
  }

  async function refreshList() {
    const result = await fetchJSON("/api/conversations", state.listTag);
    if (!result) return;
    state.list = result.data;
    state.listTag = result.tag;
    if (!state.selected && state.list.conversations.length) {
      const wanted = hashId();
      const found = state.list.conversations.find((c) => c.conversation_id === wanted);
      state.selected = (found || state.list.conversations[0]).conversation_id;
    }
    renderList();
    if (!state.detail) renderDetail();
  }

  async function refreshDetail() {
    const id = state.selected;
    if (!id) return;
    const token = ++state.token;
    try {
      const result = await fetchJSON(`/api/conversations/${id}`, state.detailTag);
      if (token !== state.token || id !== state.selected) return;
      if (!result) return;
      state.detail = result.data;
      state.detailTag = result.tag;
      state.detailError = null;
      renderDetail();
      noteNewActivity();
    } catch (error) {
      if (token !== state.token || id !== state.selected) return;
      if (error.status === 404) {
        state.detail = null;
        state.detailError = "gone";
        renderDetail();
        return;
      }
      throw error;
    }
  }

  function setOnline(online) {
    if (state.online === online) return;
    state.online = online;
    ui.live.dataset.state = online ? "live" : "offline";
    ui.liveText.textContent = online ? "Local · live" : "Offline · retrying";
    if (!online && !state.list) renderDetail();
  }

  async function tick() {
    clearTimeout(state.timer);
    try {
      await refreshList();
      await refreshDetail();
      setOnline(true);
    } catch {
      setOnline(false);
    }
    updateClocks();
    state.timer = setTimeout(tick, document.hidden ? HIDDEN_POLL_MS : POLL_MS);
  }

  // ---------- Navigation ----------
  function setView(view) {
    if (view === "list" && !narrow.matches) view = "chronology";
    ui.layout.dataset.view = view;
    for (const tab of ui.tabs) tab.setAttribute("aria-pressed", String(tab.dataset.view === view));
    if (view !== "chronology") ui.jump.hidden = true;
  }

  function select(id, fromUser) {
    if (state.selected !== id) {
      state.selected = id;
      state.detail = null;
      state.detailTag = null;
      state.detailError = null;
      state.seen = 0;
      state.token++;
      ui.jump.hidden = true;
      history.replaceState(null, "", "#" + id);
      renderList();
      renderDetail();
      ui.main.scrollTop = 0;
      ui.contrib.scrollTop = 0;
      refreshDetail().catch(() => setOnline(false));
    }
    if (fromUser && narrow.matches) {
      setView("chronology");
      ui.main.focus({ preventScroll: true });
    }
  }

  ui.search.addEventListener("input", () => { state.query = ui.search.value; renderList(); });
  ui.list.addEventListener("keydown", moveListFocus);
  for (const tab of ui.tabs) tab.addEventListener("click", () => setView(tab.dataset.view));
  narrow.addEventListener("change", () => setView(ui.layout.dataset.view));
  ui.jump.addEventListener("click", () => {
    ui.main.scrollTo({ top: ui.main.scrollHeight, behavior: reducedMotion.matches ? "auto" : "smooth" });
    ui.jump.hidden = true;
  });
  ui.main.addEventListener("scroll", () => { if (!ui.jump.hidden && nearBottom(ui.main)) ui.jump.hidden = true; }, { passive: true });
  document.addEventListener("selectionchange", () => {
    if (state.pending && !selectionInside(ui.main) && !selectionInside(ui.contrib)) renderDetail();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  window.addEventListener("hashchange", () => {
    const id = hashId();
    if (id && id !== state.selected) select(id, false);
  });

  setView(narrow.matches && !hashId() ? "list" : "chronology");
  tick();
})();
