"use strict";
/* Policy audit: findings from Z3, one hospital at a time. Uses $, $$, esc, money from app.js. */
(() => {
  let index = null;
  let current = null;
  const ORDER = { counterexample: 0, unknown: 1, proved: 2, not_stated: 3, "n/a": 4 };
  const STATUS = { counterexample: ["Counterexample", "f-bad"], proved: ["Proved", "f-ok"], not_stated: ["Not stated", "f-na"],
    "n/a": ["Doesn't apply", "f-na"], unknown: ["Solver timed out", "f-na"] };
  const SEV = { gap: "Conflicts with the law as written", allows_denial: "Allows a denial the law forbids",
    timing: "Timeline conflict", ambiguity: "Gap in the policy's own wording", info: "" };

  async function loadAudit() {
    if (!index) index = await (await fetch("/api/audit")).json();
    const ps = index.policies || [];
    if (!ps.length) { $("#a-detail").innerHTML = "<p>No audits yet.</p>"; return; }
    const tot = (k) => ps.reduce((a, p) => a + p[k], 0);
    const secs = ps.reduce((a, p) => a + p.solver_seconds, 0);
    const hand = ps.some((p) => p.model_source !== "nemotron");
    $("#a-stats").innerHTML = `
      <div><b>${ps.length}</b><span>hospital policies</span></div>
      <div class="f-ok"><b>${tot("proved")}</b><span>requirements proved</span></div>
      <div class="f-bad"><b>${tot("counterexamples")}</b><span>counterexamples found</span></div>
      <div><b>${(secs * 1000).toFixed(0)} ms</b><span>total Z3 time</span></div>
      ${hand ? `<p class="src-note">Formal models: <b>hand-written references</b> (every quote checked against the policy). The Nemotron-formalised models replace these once they're run and scored.</p>` : ""}`;
    const SHORT = { co_hdc_coverage: "Covers all HDC-eligible", co_hdc_assets: "Ignores savings", co_hdc_public: "No Medicaid-first rule",
      co_eca_182: "No collections before day 182", co_window_181: "181-day window", co_window_insured: "Insured: +45 days",
      co_plan_4pct: "4% payment cap", fed_window_240: "501(r): 240-day window", fed_eca_120: "501(r): 120-day wait",
      monotone: "No income-band gaps", summary_matches_policy: "Summary matches policy" };
    const cols = Object.keys(index.checks || {});
    const sym = { counterexample: ["✗", "f-bad", "counterexample"], proved: ["✓", "f-ok", "proved"], not_stated: ["–", "f-na", "not stated"],
      "n/a": ["n/a", "f-na", "doesn't apply"], unknown: ["?", "f-na", "solver timed out"] };
    $("#a-matrix").innerHTML = `<thead><tr><th scope="col">Hospital</th>${cols.map((c) => `<th scope="col" title="${esc(index.checks[c])}">${esc(SHORT[c] || index.checks[c])}</th>`).join("")}</tr></thead>
      <tbody>${ps.map((p) => `<tr><th scope="row"><button class="linkish" data-open="${esc(p.id)}">${esc(p.hospital)}</button></th>${cols.map((c) => {
        const [t, k, l] = sym[p.checks?.[c]] || ["", "", "not checked"];
        return `<td class="${k}" title="${esc(l)}"><span aria-label="${esc(l)}">${t}</span></td>`; }).join("")}</tr>`).join("")}</tbody>`;
    $$("[data-open]").forEach((b) => b.addEventListener("click", () => { open(b.dataset.open); $("#a-detail").scrollIntoView({ behavior: "smooth" }); }));
    $("#a-list").innerHTML = ps.map((p) => `
      <button class="a-item" data-id="${esc(p.id)}" aria-pressed="false">
        <b>${esc(p.hospital)}</b>
        <span class="counts"><i class="f-bad">${p.counterexamples} ✗</i><i class="f-ok">${p.proved} ✓</i>${p.not_stated ? `<i class="f-na">${p.not_stated} not stated</i>` : ""}</span>
        <small>${p.nonprofit ? "Nonprofit: Colorado law + 501(r)" : "For-profit: Colorado law"}</small>
      </button>`).join("");
    $$(".a-item").forEach((b) => b.addEventListener("click", () => open(b.dataset.id)));
    const want = new URLSearchParams(location.search).get("h");
    open(want && ps.some((p) => p.id === want) ? want : (current?.id || ps[0].id), false);
  }

  async function open(id, focus = true) {
    $$(".a-item").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.id === id)));
    current = await (await fetch(`/api/audit/${encodeURIComponent(id)}`)).json();
    render(current.findings);
    if (focus) $("#a-detail").focus({ preventScroll: true });
  }

  function render(findings, live) {
    const d = current;
    const fs = [...findings].sort((a, b) => ORDER[a.status] - ORDER[b.status]);
    $("#a-detail").innerHTML = `
      <div class="a-head">
        <div>
          <h2>${esc(d.hospital)}</h2>
          <p class="sub"><a href="${esc(d.source_url)}" target="_blank" rel="noopener">The hospital's policy</a> ·
            ${d.nonprofit ? "nonprofit" : "for-profit"} · model: <span class="chip ${d.model_source === "nemotron" ? "" : "code"}">${esc(d.model_source)}</span>
            ${d.summary_source ? ` · plain-language summary also checked` : ""}</p>
        </div>
        <div class="a-prove">
          <button class="btn ghost" id="a-reprove" type="button">Re-prove with Z3 now</button>
          <small id="a-live">${live ? esc(live) : `last run: ${(d.solver_seconds * 1000).toFixed(0)} ms`}</small>
        </div>
      </div>
      <div class="findings">${fs.map(card).join("")}</div>
      <details class="model"><summary>The formal model Z3 checked <small>${d.model.requires.length} requirements, ${d.model.rules.length} rules</small></summary>${modelHtml(d.model)}</details>`;
    $("#a-reprove").addEventListener("click", reprove);
  }

  async function reprove() {
    const b = $("#a-reprove");
    b.disabled = true; b.textContent = "Proving…";
    try {
      const r = await (await fetch(`/api/audit/${encodeURIComponent(current.id)}/prove`, { method: "POST" })).json();
      const keep = current.findings.filter((f) => f.check === "summary_matches_policy");
      render([...r.findings, ...keep], `Z3 ${r.z3} re-checked ${r.findings.length} findings just now in ${(r.seconds * 1000).toFixed(0)} ms`);
    } catch (e) {
      b.disabled = false; b.textContent = "Re-prove with Z3 now";
      $("#a-live").textContent = "Couldn't reach the solver. Showing the stored results.";
    }
  }

  function card(f) {
    const [label, cls] = STATUS[f.status] || [f.status, "grey"];
    const isCe = f.status === "counterexample";
    const title = f.check === "summary_matches_policy" ? "The plain-language summary says the same as the policy" : f.title;
    return `<article class="finding ${cls}">
      <header><span class="st ${cls}">${label}</span>${isCe && SEV[f.severity] ? `<span class="sev">${SEV[f.severity]}</span>` : ""}<h3>${esc(title)}</h3></header>
      <p class="fsum">${esc(f.summary)}</p>
      ${isCe ? visual(f) : ""}
      ${(f.clauses?.length || f.law?.length) ? `<details><summary>Evidence <small>${f.domain ? "checked over " + esc(f.domain) : ""}</small></summary>${evidence(f)}</details>` : ""}
    </article>`;
  }

  function visual(f) {
    const h = f.household || {};
    if ("policy_day" in h) return timeline(h);
    if (f.household_b) {
      const o = f.policy_outcome || {};
      return `<div class="pair">${hh(f.household, o.a)}<div class="vs">vs</div>${hh(f.household_b, o.b)}</div>`;
    }
    if (!f.household) return "";
    const o = f.policy_outcome || {};
    if (o.a && o.b) {  // one household, two documents (policy vs summary)
      const tag = (x) => x.covered ? (x.best_stated_discount_pct ? `${x.best_stated_discount_pct}% off` : "gets help") : "no help";
      return `<div class="pair one">${hh(f.household)}<div class="outs"><div class="out ${o.a.covered ? "yes" : "no"}">policy: ${tag(o.a)}</div><div class="out ${o.b.covered ? "yes" : "no"}">summary: ${tag(o.b)}</div></div></div>`;
    }
    return `<div class="pair one">${hh(f.household, o)}</div>`;
  }

  function hh(d, o) {
    const chips = [`${d.size} ${d.size === 1 ? "person" : "people"}`, `${money(d.income_usd)} a year`, `${Number(d.fpl_pct).toFixed(1)}% of poverty line`,
      d.co_resident ? "Colorado resident" : "lives outside Colorado"];
    if (d.co_resident && !d.in_service_area) chips.push("outside the service area");
    chips.push(d.insured ? (d.underinsured ? "under-insured" : "insured (not under-insured)") : "uninsured");
    if (d.assets_usd) chips.push(`${money(d.assets_usd)} savings`);
    if (!d.applied_public_programs) chips.push("no Medicaid application");
    if (d.presumptive_program) chips.push("on SNAP / WIC");
    chips.push(d.emergency ? "emergency care" : d.medically_necessary ? "non-emergency care" : "elective care");
    if (d.bill_usd) chips.push(`owes ${money(d.bill_usd)}`);
    const out = o ? `<div class="out ${o.covered ? "yes" : "no"}">${o.covered ? (o.best_stated_discount_pct ? `${o.best_stated_discount_pct}% off` : "gets help") : "gets no help"}</div>` : "";
    return `<div class="hh"><div class="chips">${chips.map((c) => `<span>${esc(c)}</span>`).join("")}</div>${out}</div>`;
  }

  function timeline(h) {
    const max = Math.max(400, h.law_day + 20, h.policy_day + 20);
    const x = (d) => (100 * d / max).toFixed(2) + "%";
    const lo = Math.min(h.policy_day, h.law_day), hi = Math.max(h.policy_day, h.law_day);
    return `<div class="tl" role="img" aria-label="Timeline: first bill on day ${h.first_statement_day}, policy allows day ${h.policy_day}, law requires day ${h.law_day}">
      <div class="tl-bar"><i class="gap" style="left:${x(lo)};width:${x(hi - lo)}"></i>
        <b class="m bill" style="left:${x(h.first_statement_day)}"><span>first bill<br>day ${h.first_statement_day}</span></b>
        <b class="m pol" style="left:${x(h.policy_day)}"><span>policy<br>day ${h.policy_day}</span></b>
        <b class="m law" style="left:${x(h.law_day)}"><span>law<br>day ${h.law_day}</span></b></div>
      <div class="tl-axis"><span>day 0: care</span><span>day ${max}</span></div></div>`;
  }

  function evidence(f) {
    const pol = (f.clauses || []).filter((c) => c.quote || c.label).map((c) => `
      <blockquote class="q ${c.holds === false ? "fails" : ""}">${c.quote ? `“${esc(c.quote)}”` : esc(c.label)}
        <cite>${c.model ? esc(c.model) + " · " : ""}${esc(c.label)}${c.holds === false ? " · <b>not met by this household</b>" : c.kind === "rule" ? " · applies" : ""}</cite></blockquote>`).join("");
    const law = (f.law || []).map((l) => `<blockquote class="q law">“${esc(l.quote)}”<cite><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.source)}</a></cite></blockquote>`).join("");
    return `<div class="ev">${pol ? `<div><h4>The policy says</h4>${pol}</div>` : ""}${law ? `<div><h4>The law says</h4>${law}</div>` : ""}</div>`;
  }

  function atomText(a) {
    if (a.op === "true") return a.var;
    if (a.op === "false") return "not " + a.var;
    return `${a.var} ${a.op} ${a.value}`;
  }
  const cnf = (cls) => cls.map((c) => c.map(atomText).join(" or ")).map((t, i, all) => all.length > 1 && t.includes(" or ") ? `(${t})` : t).join(" and ");

  function modelHtml(m) {
    const rows = [
      ...m.requires.map((r) => ["requires", r.label + (r.discretionary ? " (may deny)" : ""), cnf(r.cond), r.quote]),
      ...m.rules.map((r) => ["rule", `${r.label} → ${r.discount_pct == null ? "help, amount not stated" : r.discount_pct + "% off"}`, cnf(r.when), r.quote]),
      ...(m.timing.application || []).map((t) => ["applications until", `${t.days} days after ${t.anchor.replace("_", " ")}`, "", t.quote]),
      ...(m.timing.eca_not_before || []).map((t) => ["no collections before", `${t.days} days after ${t.anchor.replace("_", " ")}`, "", t.quote]),
    ];
    return `<table class="rules"><thead><tr><th></th><th>Meaning</th><th>Logic</th><th>Policy's words</th></tr></thead><tbody>
      ${rows.map(([k, l, c, q]) => `<tr><td>${esc(k)}</td><td>${esc(l)}</td><td><code>${esc(c)}</code></td><td class="q">${esc(q)}</td></tr>`).join("")}</tbody></table>
      ${(m.notes || []).map((n) => `<p class="sub">${esc(n)}</p>`).join("")}`;
  }

  window.loadAudit = loadAudit;
  if (!location.hash || location.hash === "#audit") loadAudit();
})();
