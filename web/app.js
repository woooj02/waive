"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (x) => (x == null ? "—" : "$" + Number(x).toLocaleString("en-US", { maximumFractionDigits: 0 }));
const num = (s) => Number(String(s || "").replace(/[^0-9.]/g, "")) || 0;
const FPL = { base: 15960, step: 5680, AK: [19950, 7100], HI: [18360, 6530] };
const fpl = (size, st) => { const [b, s] = FPL[st] || [FPL.base, FPL.step]; return b + s * (size - 1); };

let chosenSample = "";
let chosenFile = null;
let lastResult = null;
let atlasCache = null;

/* ---------- tabs ---------- */
function showTab(id) {
  for (const b of $$("nav.tabs button")) {
    const on = b.id === "t-" + id;
    b.setAttribute("aria-selected", on);
    $("#" + b.getAttribute("aria-controls")).hidden = !on;
  }
  if (id === "atlas") loadAtlas();
  if (id === "how") loadHow();
  if (id === "audit" && window.loadAudit) window.loadAudit();
  history.replaceState(null, "", id === "audit" ? "/" : "#" + id);
}
$$("nav.tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.id.slice(2))));

/* ---------- inputs ---------- */
async function loadSamples() {
  const list = await (await fetch("/api/samples")).json();
  $("#samples").innerHTML = list.map((s) => `
    <button type="button" class="sample" data-id="${esc(s.id)}" aria-pressed="false">
      <img src="/samples/${esc(s.files[0])}" alt="Sample bill: ${esc(s.title)}">
      <div><b>${esc(s.title)}</b>${money(s.balance)} · ${esc(s.hospital.replace(/^(UCHealth|Intermountain Health) /, ""))}</div>
    </button>`).join("");
  for (const btn of $$(".sample")) {
    btn.addEventListener("click", () => {
      const s = list.find((x) => x.id === btn.dataset.id);
      const on = btn.getAttribute("aria-pressed") !== "true";
      $$(".sample").forEach((b) => b.setAttribute("aria-pressed", "false"));
      chosenSample = on ? s.id : "";
      chosenFile = null;
      $("#drop").classList.remove("has");
      $("#drop-title").textContent = "Drop a photo or PDF here, or choose a file";
      if (on) {
        btn.setAttribute("aria-pressed", "true");
        $("#size").value = s.household.size;
        $("#income").value = s.household.income_usd.toLocaleString("en-US");
        $("#state").value = s.household.state;
        $("#insured").value = String(s.household.insured);
        $("#drop-sub").textContent = s.story;
      }
    });
  }
}

function setFile(f) {
  if (!f) return;
  chosenFile = f;
  chosenSample = "";
  $$(".sample").forEach((b) => b.setAttribute("aria-pressed", "false"));
  $("#drop").classList.add("has");
  $("#drop-title").textContent = f.name;
  $("#drop-sub").textContent = `${(f.size / 1e6).toFixed(1)} MB · ready`;
}
$("#file").addEventListener("change", (e) => setFile(e.target.files[0]));
const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));
$("#type-toggle").addEventListener("click", () => { $("#typed").hidden = !$("#typed").hidden; });

function household() {
  return {
    size: Math.max(1, parseInt($("#size").value, 10) || 1),  // over 12 is refused with a message, not clamped
    income_usd: num($("#income").value),
    state: $("#state").value,
    insured: $("#insured").value === "true",
    assets_usd: $("#assets").value ? num($("#assets").value) : null,
    programs: $$(".checks input:checked").map((c) => c.value),
  };
}

/* ---------- run a check ---------- */
const STEP_ORDER = ["read", "find", "compile", "decide"];

$("#form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const hh = household();
  const typedOn = !$("#typed").hidden && $("#t-hospital").value.trim();
  if (!chosenSample && !chosenFile && !typedOn) {
    $("#drop-sub").textContent = "Add a bill, pick a sample, or type the details first.";
    $("#drop").focus();
    return;
  }
  const fd = new FormData();
  fd.append("household", JSON.stringify(hh));
  if (chosenSample) fd.append("sample", chosenSample);
  else if (chosenFile) fd.append("file", chosenFile);
  else fd.append("typed", JSON.stringify({ hospital: $("#t-hospital").value.trim(), balance_usd: num($("#t-balance").value),
    statement_date: $("#t-date").value, service_dates: $("#t-service").value, state: hh.state }));

  $("#go").disabled = true;
  $("#go").textContent = "Checking…";
  $("#work").hidden = false;
  $("#steps").innerHTML = "";
  $("#result").hidden = true;
  $("#bar").style.width = "4%";
  $("#work").scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const resp = await fetch("/api/check", { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (line) handle(JSON.parse(line));
      }
    }
  } catch (err) {
    renderStep({ id: "error", status: "error", title: "Something went wrong", detail: String(err.message || err) });
  } finally {
    $("#go").disabled = false;
    $("#go").textContent = "Check my bill";
  }
});

function handle(ev) {
  if (ev.type === "step") renderStep(ev);
  else if (ev.type === "result") renderResult(ev);
  else if (ev.type === "need_fields") { $("#typed").hidden = false; renderStep({ id: "need", status: "error", title: ev.message }); }
  else if (ev.type === "fatal") renderStep({ id: "fatal", status: "error", title: "The check stopped", detail: ev.detail });
}

function callsMeta(calls) {
  if (!calls || !calls.length) return "";
  const secs = calls.reduce((a, c) => a + (c.seconds || 0), 0);
  const cost = calls.reduce((a, c) => a + (c.cost_usd || 0), 0);
  const cached = calls.every((c) => c.cached_result);
  return cached ? "cached · $0" : `${secs.toFixed(1)}s · $${cost.toFixed(4)}`;
}

function chips(ev) {
  const names = [...new Set((ev.calls || []).map((c) => c.label))];
  if (!names.length && ev.model) names.push(ev.model);
  const out = names.map((n) => `<span class="chip ${n.startsWith("plain code") ? "code" : ""}">${esc(n)}</span>`);
  if (ev.from_cache) out.push(`<span class="chip cache">prepared in advance</span>`);
  return out.join("");
}

function renderStep(ev) {
  let li = $(`#step-${ev.id}`);
  if (!li) {
    li = document.createElement("li");
    li.id = `step-${ev.id}`;
    $("#steps").appendChild(li);
  }
  li.className = `step ${ev.status}`;
  const notes = (ev.notes || []).length ? `<ul class="notes">${ev.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : "";
  const src = ev.source && ev.source.url ? ` · <a href="${esc(ev.source.url)}" target="_blank" rel="noopener">policy document</a>` : "";
  li.innerHTML = `<span class="dot" aria-hidden="true"></span>
    <div><b>${esc(ev.title)}</b><div class="detail">${esc(ev.detail || "")}${src}</div><div>${chips(ev)}</div>${notes}</div>
    <div class="meta">${esc(ev.status === "running" ? "working…" : callsMeta(ev.calls))}</div>`;
  const idx = STEP_ORDER.indexOf(ev.id);
  if (idx >= 0) $("#bar").style.width = `${Math.max(8, ((idx + (ev.status === "running" ? 0.5 : 1)) / STEP_ORDER.length) * 100)}%`;
}

/* ---------- result ---------- */
const KICKER = { free: "Good news", discount: "Good news", colorado_cap: "Colorado law protects you",
  not_eligible: "Probably not eligible", needs_info: "We need more information", no_policy: "Policy not found" };

function highlight(quote, keys) {
  let q = esc(quote);
  for (const k of keys) if (k) q = q.replace(new RegExp(`(${k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "i"), "<mark>$1</mark>");
  return q;
}

function deadlineText(d) {
  if (!d) return "";
  const days = Math.round((new Date(d) - new Date()) / 864e5);
  const nice = new Date(d + "T12:00:00").toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  return days >= 0 ? `Apply by ${nice} · ${days} days left` : `The application window ended ${nice}: ask anyway`;
}

function renderResult(ev) {
  lastResult = ev;
  $("#bar").style.width = "100%";
  const v = ev.verdict, p = ev.program, src = ev.source || {};
  const good = ["free", "discount", "colorado_cap"].includes(v.status);
  const main = v.reasons.find((r) => r.quote) || null;
  const tierQuote = main ? `<blockquote class="q">“${highlight(main.quote, [`${Math.round(v.discount_pct)}%`, /\d{3}%/.exec(main.quote)?.[0]])}”
      <cite>${esc(p?.hospital || "")} financial assistance policy · <a href="${esc(main.source || src.url)}" target="_blank" rel="noopener">source</a>${src.fetched ? " · fetched " + esc(src.fetched) : ""}</cite></blockquote>` : "";

  const todo = good ? [
    "Download your packet and fill in the few blanks (name, account number, signature).",
    "Send it to the hospital's financial assistance office with the documents on the checklist.",
    "Ask them in writing to pause collections while your application is reviewed.",
    "Keep paying nothing you don't agree with until you get a written decision.",
  ] : v.status === "not_eligible" ? [
    "Ask the hospital for an itemised bill and check it line by line.",
    "Ask about a payment plan, or a hardship review if the bill is large compared with your income.",
  ] : ["Fill in what's missing above and run the check again, or call the hospital's financial assistance office."];

  const co = v.colorado ? `<p style="margin:10px 0 0;font-size:14px">Colorado Hospital Discounted Care: payments on this bill can't be more than
      <b>${money(v.colorado.monthly_max_usd)}/month</b> (${v.colorado.pct_of_income}% of income) for at most ${v.colorado.payments} months.</p>` : "";

  const rules = p ? ruleRows(p) : "";
  const dropped = p && p.dropped.length ? `<p class="no" style="margin-top:12px">Dropped: ${p.dropped.length} rule(s) whose quote wasn't found in the policy text</p>
      <ul>${p.dropped.map((d) => `<li>${esc(d.rule)}: “${esc(d.quote)}”</li>`).join("")}</ul>` : "";
  const cc = src.cross_check || {};
  const dis = cc.disagreements ? Object.keys(cc.disagreements) : [];
  const crossText = cc.secondary ? (dis.length ? `Nemotron 3 Super's independent reading disagreed on ${dis.length} field(s): ${dis.map(esc).join(", ")}. Nemotron 3 Ultra settled each one with a quote from the policy.`
      : "Nemotron 3 Super's independent reading agreed with Nemotron 3 Ultra on every field that can change your answer.") : "Single-model compile, not cross-checked.";

  const r = ev.receipt || {};
  const atlasCost = (ev.source?.from_atlas ? (atlasCache?.find?.((a) => a.id === src.policy_id)) : null);
  $("#result").innerHTML = `
    <div class="verdict ${esc(v.status)}">
      <div class="kicker">${esc(KICKER[v.status] || "")}</div>
      <h2>${esc(v.headline)}</h2>
      ${good && v.expected_usd != null && v.expected_usd < v.balance_usd ? `<div class="amounts"><span class="was">${money(v.balance_usd)}</span><span class="now">${money(v.expected_usd)}</span></div>` : ""}
      ${v.deadline && good ? `<span class="deadline">⏳ ${esc(deadlineText(v.deadline))}</span>` : ""}
      ${co}
      ${v.missing.length ? `<div class="missing"><b>To be sure, we need:</b> ${v.missing.map(esc).join("; ")}</div>` : ""}
      ${v.uncertain.length ? `<div class="missing">${v.uncertain.map(esc).join("<br>")}</div>` : ""}
      <div class="actions">
        ${good ? `<button class="btn" id="dl">Download my application packet (PDF)</button>` : ""}
        ${src.url ? `<a class="btn ghost" href="${esc(src.url)}" target="_blank" rel="noopener">Read the hospital's policy</a>` : ""}
        ${src.audit_id ? `<a class="btn ghost" href="/?h=${encodeURIComponent(src.audit_id)}">See this policy's Z3 audit${src.audit_counterexamples ? ` (${src.audit_counterexamples} findings)` : ""}</a>` : ""}
      </div>
      <ol class="todo">${todo.map((t) => `<li>${esc(t)}</li>`).join("")}</ol>
    </div>
    <div class="evidence">
      <details open><summary>Why: the policy's own words</summary><div class="body">${tierQuote || (v.status === "colorado_cap" ? "" : "<p>No proven quote for your income band.</p>")}
        ${v.reasons.filter((x) => x !== main).map((x) => `<p>${esc(x.text)}${x.quote ? `<blockquote class="q">“${esc(x.quote)}”</blockquote>` : ""}${x.source && !x.quote ? ` <small>(${esc(x.source)})</small>` : ""}</p>`).join("")}</div></details>
      <details><summary>The maths <small>plain code, no AI</small></summary><div class="body"><div class="math">
        2026 poverty guideline, household of ${ev.household.size}: ${money(v.fpl_usd)}<br>
        Your income ${money(ev.household.income_usd)} ÷ ${money(v.fpl_usd)} = <b>${v.pct_fpl}%</b> of the poverty level<br>
        ${v.discount_pct ? `Policy tier: ${v.discount_pct}% off → ${money(v.balance_usd)} × ${(100 - v.discount_pct)}% = ${money(v.balance_usd * (1 - v.discount_pct / 100))}<br>` : ""}
        ${v.colorado ? `Colorado cap: ${money(ev.household.income_usd)} ÷ 12 × ${v.colorado.pct_of_income}% × ${v.colorado.payments} = ${money(v.colorado.monthly_max_usd * v.colorado.payments)}<br>` : ""}
        ${v.deadline ? `Apply by ${esc(v.deadline)}: ${esc(v.deadline_basis)}` : ""}</div></div></details>
      ${p ? `<details><summary>Every rule we read from this policy <small>${p.tiers.filter((t) => t.verified).length} tiers proven · ${p.dropped.length} dropped</small></summary><div class="body">
        <p>${esc(crossText)}</p><table class="rules"><thead><tr><th>Rule</th><th>What it says</th><th>Proof</th></tr></thead><tbody>${rules}</tbody></table>${dropped}
        ${p.unknowns.length ? `<p><b>The policy doesn't say:</b></p><ul>${p.unknowns.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>` : ""}</div></details>` : ""}
      <details><summary>Which models did this, and what it cost</summary><div class="body">${receiptHtml(r)}</div></details>
    </div>`;
  $("#result").hidden = false;
  $("#result").focus({ preventScroll: true });
  $("#result").scrollIntoView({ behavior: "smooth", block: "start" });
  const dl = $("#dl");
  if (dl) dl.addEventListener("click", downloadPacket);
}

function ruleRows(p) {
  const yes = '<span class="ok">✓ quote found</span>', no = '<span class="no">✗ not proven</span>';
  const row = (name, what, r) => `<tr><td>${esc(name)}</td><td>${esc(what)}</td><td class="q">${r.quote ? "“" + esc(r.quote) + "” " : ""}${r.quote ? (r.verified ? yes : no) : "—"}</td></tr>`;
  const rows = p.tiers.map((t) => row("Income tier", `${t.min_fpl}–${t.max_fpl}% of poverty level: ${t.discount_pct}% off`, t));
  rows.push(row("Asset test", p.asset_test.applies == null ? "not stated" : p.asset_test.applies ? `yes${p.asset_test.limit_usd ? ", limit " + money(p.asset_test.limit_usd) : ""}` : "no", p.asset_test));
  rows.push(row("Residency", p.residency.kind + (p.residency.states.length ? ": " + p.residency.states.join(", ") : ""), p.residency));
  rows.push(row("Insured patients", p.insured.eligible == null ? "not stated" : p.insured.eligible ? "can apply" : "not eligible", p.insured));
  rows.push(row("Doctors' bills", p.providers.scope.replace("_", " "), p.providers));
  rows.push(row("Hardship", p.hardship.bill_pct_of_income ? `bills over ${p.hardship.bill_pct_of_income}% of income` : "not stated", p.hardship));
  for (const x of p.presumptive) rows.push(row("Automatic if enrolled", x.program, x));
  rows.push(row("Application", p.application.window_days ? `${p.application.window_days} days` : "window not stated", p.application));
  return rows.join("");
}

function receiptHtml(r) {
  if (!r || !r.calls) return "";
  const live = r.calls.filter((c) => !c.cached_result);
  const rows = r.calls.map((c) => `<tr><td>${esc(c.step)}</td><td>${esc(c.label)}</td><td>${c.cached_result ? "cached" : c.seconds + "s"}</td>
    <td>${c.tokens_in.toLocaleString()} in${c.tokens_cached ? ` (${c.tokens_cached.toLocaleString()} cached)` : ""} / ${c.tokens_out.toLocaleString()} out</td>
    <td>$${c.cost_usd.toFixed(5)}</td></tr>`).join("");
  const save = r.all_ultra_usd > 0 ? Math.round((1 - r.cost_usd / r.all_ultra_usd) * 100) : null;
  return `<div class="receipt"><div><b>$${r.cost_usd.toFixed(4)}</b><span>this check, live calls</span></div>
    <div><b>${r.all_ultra_usd ? "$" + r.all_ultra_usd.toFixed(4) : "—"}</b><span>same calls if all went to Ultra</span></div>
    <div><b>${save != null ? save + "%" : r.cached_calls + " cached"}</b><span>${save != null ? "saved by routing each job to the right size" : "answers reused at no cost"}</span></div></div>
    <table class="rules" style="margin-top:10px"><thead><tr><th>Step</th><th>Model</th><th>Time</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>${rows}</tbody></table>
    <p style="font-size:13px;color:var(--ink-3)">Prices: Nebius Token Factory public catalog. ${live.length ? "" : "Every call in this check came from cache."}</p>`;
}

async function downloadPacket() {
  const btn = $("#dl");
  btn.disabled = true;
  btn.textContent = "Preparing…";
  try {
    const resp = await fetch("/api/packet", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(lastResult) });
    if (!resp.ok) throw new Error((await resp.json()).detail);
    const url = URL.createObjectURL(await resp.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: "waive-packet.pdf" });
    document.body.appendChild(a); a.click(); a.remove();
    btn.textContent = "Downloaded ✓";
  } catch (err) {
    btn.textContent = "Couldn't build the packet: " + err.message;
  } finally {
    btn.disabled = false;
  }
}

/* ---------- atlas ---------- */
const AXIS_MAX = 600;
async function loadAtlas() {
  if (!atlasCache) atlasCache = await (await fetch("/api/atlas")).json();
  const hh = household();
  const you = (100 * hh.income_usd) / fpl(hh.size, hh.state);
  $("#a-you").textContent = `Your household: ${Math.round(you)}% of poverty level`;
  const q = $("#a-q").value.trim().toLowerCase();
  const rows = atlasCache
    .filter((a) => !q || [a.hospital, a.system, a.city, ...(a.hospitals || [])].join(" ").toLowerCase().includes(q))
    .sort((a, b) => (b.free_to_fpl || 0) - (a.free_to_fpl || 0));
  const pct = (x) => `${Math.min(100, (100 * x) / AXIS_MAX)}%`;
  $("#atlas-chart").innerHTML = (rows.length ? rows.map((a) => `
    <div class="crow" role="listitem" tabindex="0" data-id="${esc(a.id)}" aria-label="${esc(a.hospital)}: free care to ${a.free_to_fpl ?? "unknown"}%, discounts to ${a.discount_to_fpl ?? "unknown"}% of poverty level">
      <div class="nm">${esc(a.hospital)}<small>${esc(a.city)} · ${(a.hospitals || []).length || 1} hospital(s)</small></div>
      <div class="track">
        ${a.discount_to_fpl ? `<div class="disc" style="left:0;width:${pct(a.discount_to_fpl)}"></div>` : ""}
        ${a.free_to_fpl ? `<div class="free" style="width:${pct(a.free_to_fpl)}"></div>` : ""}
        <div class="you" style="left:${pct(you)}"></div>
      </div>
      <div class="flags">${a.free_to_fpl ? "free ≤ " + a.free_to_fpl + "%" : "free: ?"}<br>${a.asset_test ? "asset test" : a.asset_test === false ? "no asset test" : ""}</div>
    </div>`).join("") : `<p>No hospitals match “${esc(q)}”.</p>`) + `
    <div class="axis"><div></div><div>${[0, 100, 200, 300, 400, 500, 600].map((x) => `<span style="left:${pct(x)}">${x}%</span>`).join("")}</div><div></div></div>`;
  for (const r of $$(".crow")) {
    const open = () => showPolicy(r.dataset.id);
    r.addEventListener("click", open);
    r.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  }
}
$("#a-q").addEventListener("input", loadAtlas);

async function showPolicy(id) {
  const e = await (await fetch("/api/atlas/" + encodeURIComponent(id))).json();
  const p = e.program;
  const d = $("#atlas-detail");
  const cost = (e.calls || []).reduce((a, c) => a + c.cost_usd, 0);
  d.innerHTML = `<h2>${esc(e.hospital)}</h2>
    <p class="sub">${esc(e.system)} · covers ${(e.hospitals || []).map(esc).join(", ")}<br>
    Source: <a href="${esc(e.source_url)}" target="_blank" rel="noopener">policy document</a> · fetched ${esc(e.fetched)} · compiled ${esc(e.compiled)} for $${cost.toFixed(4)}</p>
    <table class="rules"><thead><tr><th>Rule</th><th>What it says</th><th>Proof</th></tr></thead><tbody>${ruleRows(p)}</tbody></table>
    ${p.dropped.length ? `<p class="no">Dropped ${p.dropped.length} unproven rule(s).</p>` : ""}
    ${p.unknowns.length ? `<p><b>The policy doesn't say:</b> ${p.unknowns.map(esc).join("; ")}</p>` : ""}`;
  d.hidden = false;
  d.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ---------- how ---------- */
async function loadHow() {
  const h = await (await fetch("/api/health")).json();
  const jobs = [["Writes each policy as logic (long, precise reading)", "ultra"], ["Second, independent formalisation; checks each quote's meaning", "super"],
    ["Picks the hospital's own policy among search results", "nano"], ["Reads a bill photo (Check my bill tab)", "vl"]];
  $("#how-models").innerHTML = `<table class="plain"><thead><tr><th>Job</th><th>Model on Nebius Token Factory</th></tr></thead><tbody>
    ${jobs.map(([j, k]) => `<tr><td>${esc(j)}</td><td><code>${esc(h.models[k])}</code></td></tr>`).join("")}
    <tr><td>Every legal question: coverage, deadlines, equivalence, counterexamples</td><td>Z3 theorem prover, no model</td></tr></tbody></table>`;
  const ev = await (await fetch("/api/formal-eval")).json();
  if (ev.summary) $("#how-eval").innerHTML = formalEvalHtml(ev);
}

function formalEvalHtml(ev) {
  const s = ev.summary;
  const rows = ["nano", "super", "ultra", "repair"].filter((k) => s[k]).map((k) => `<tr><td>${esc(s[k].label)}</td><td>${s[k].findings_match_pct}%</td><td>${s[k].equivalent}/${s[k].docs}</td><td>$${s[k].cost_per_doc.toFixed(3)}</td><td>${s[k].seconds_per_doc}s</td></tr>`).join("");
  const t = s.two_models;
  return `<p>We wrote ${ev.references.length} reference models by hand (UCHealth's policy and its summary, Intermountain, Children's, HealthONE), checking every quote.
    Each model formalised the same documents; Z3 compared the audit verdicts it led to with the verdicts from our references.</p>
    <table class="plain"><thead><tr><th>Model</th><th>Audit verdicts that match ours</th><th>Logically equivalent to ours</th><th>Cost / document</th><th>Time</th></tr></thead><tbody>${rows}</tbody></table>
    ${t ? `<p><b>Two models as a confidence signal.</b> On these ${ev.references.length} documents, Nemotron 3 Ultra's and Nemotron 3 Super's models reach the same verdict on ${t.confirmed_share_pct}% of checks; on those, the verdict matches our reference ${t.confirmed_match_pct}% of the time, against ${t.disputed_match_pct}% where they disagree. That is verdict agreement on a small set, not an accuracy rate. Cost for both: $${t.cost_per_doc.toFixed(3)} per document.</p>` : ""}
    ${stabilityHtml(ev.stability)}
    <p>What we learned: no model is logically equivalent to our references on any document; they differ on details like which program a condition belongs to.
    Letting Ultra rewrite its model from the disagreement households (row "repair") made results worse and cost 4× more, so it is off.
    ${ev.adjudication ? `We also tried letting Ultra settle each disputed finding by reading the policy for that one case: it agreed with our references on ${ev.adjudication.agree_with_reference} of ${ev.adjudication.decided} decided cases, so legal verdicts stay with Z3.` : ""}
    Nemotron also surfaced three clauses our own reference for Children's had missed; we corrected the reference and say so.</p>`;
}

function stabilityHtml(st) {
  if (!st) return "";
  const u = st.ultra, sp = st.super, f = st.findings, one = f.one_super_run, maj = f.super_majority;
  return `<p><b>Repeated runs.</b> We ran each model ${st.runs} times on the same ${st.docs} documents, with fresh calls.
    Nemotron 3 Ultra's wording changed between runs, but ${u.same_verdict_every_run_pct === 100 ? "every check got the same verdict in every run" : `${u.same_verdict_every_run_pct}% of checks got the same verdict in every run`} (${u.lowest_run_pct === u.highest_run_pct ? `${u.lowest_run_pct}% match with our references each time` : `${u.lowest_run_pct}–${u.highest_run_pct}% match`}).
    Nemotron 3 Super was less steady: the same verdict in every run on ${sp.same_verdict_every_run_pct}% of checks (${sp.lowest_run_pct}–${sp.highest_run_pct}% match).</p>
    <p>So the audit replays each counterexample against <b>${st.runs} independent Super models</b> and calls it confirmed only when most of them fail for the same household or day.
    On the reference documents, each distinct counterexample Ultra found (${f.distinct_counterexamples}) was replayed against our reference model:</p>
    <table class="plain"><thead><tr><th>Confirmation rule</th><th>Confirmed: reference also fails</th><th>Disputed: reference also fails</th></tr></thead><tbody>
      <tr><td>One Super run</td><td>${one.confirmed.real} of ${one.confirmed.n} (${one.confirmed.real_pct}%)</td><td>${one.disputed.real} of ${one.disputed.n} (${one.disputed.real_pct}%)</td></tr>
      <tr><td>Most of ${st.runs} Super runs (used)</td><td>${maj.confirmed.real} of ${maj.confirmed.n} (${maj.confirmed.real_pct}%)</td><td>${maj.disputed.real} of ${maj.disputed.n} (${maj.disputed.real_pct}%)</td></tr>
    </tbody></table>
    <p>Read this as exploratory, not as validation: the rule was chosen after seeing these cases, several share the same underlying gap, and all ${st.runs} models come from the same model family, so separate runs can share one misunderstanding.
    The production reviews show that limit: 3 counterexamples that most Super models confirmed (two of them 5 of 5) were rejected when we read the policy, and they stay on the page as dismissed.
    It is also why disputed findings stay visible, marked for a person: most of them are real too. A Super model that lost rules in checking can't confirm a gap that may be its own missing rule (with no rules left it denies everyone), so it counts as unable, not as a vote.</p>`;
}

/* ---------- boot ---------- */
loadSamples();
if (location.hash === "#check") showTab("check");
if (location.hash === "#atlas") showTab("atlas");
if (location.hash === "#how") showTab("how");
