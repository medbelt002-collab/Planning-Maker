const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const api = async (url, opts = {}) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
};
const toast = (msg, kind = "ok") => {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  setTimeout(() => t.classList.add("hidden"), 4000);
};

// ---------------- tabs ----------------
$$(".tab").forEach((b) =>
  b.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    $$(".panel").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "journal") loadJournal();
    if (b.dataset.tab === "reservations") loadReservations();
    if (b.dataset.tab === "archives") loadArchives();
    if (b.dataset.tab === "settings") loadConfig();
    if (b.dataset.tab === "dashboard") loadDashboard();
    if (b.dataset.tab === "noshow") loadNoShow();
  })
);

// ---------------- status / dashboard ----------------
async function loadStatus() {
  try {
    const s = await api("/api/status");
    const pill = $("#login-pill");
    pill.textContent = s.logged_in ? "● Connecté" : "● Non connecté";
    pill.className = "pill " + (s.logged_in ? "pill-on" : "pill-off");
    $("#lastsync").textContent = "Dernière synchro : " + (s.last_sync_at || "—");
    $("#s-login").textContent = s.logged_in ? "Connecté" : (s.has_session ? "Session (à vérifier)" : "Non connecté");
    $("#s-planning").textContent = s.has_planning ? "Oui" : "Non";
    $("#s-lastsync").textContent = s.last_sync_at || "—";
    $("#s-dur").textContent = s.last_sync_duration ?? "—";
    $("#s-cree").textContent = s.last_cree_le || "—";
    $("#s-auto").textContent = s.auto_sync_enabled ? `Oui (${s.auto_sync_minutes} min)` : "Non";
  } catch (e) {}
}
async function loadDashboard() {
  try {
    const d = await api("/api/dashboard");
    $("#d-res").textContent = d.reservations;
    $("#d-pers").textContent = d.personnes;
    $("#d-studios").textContent = d.studios;
    $("#d-chambres").textContent = d.chambres;
    $("#d-attente").textContent = d.en_attente;
    $("#d-verif").textContent = d.verified;
  } catch (e) {}
}
async function loadLog() {
  try {
    const l = await api("/api/log");
    const c = $("#console");
    c.textContent = l.lines.join("\n");
    c.scrollTop = c.scrollHeight;
  } catch (e) {}
}

// ---------------- login ----------------
$("#btn-login").addEventListener("click", async () => {
  try {
    const r = await api("/api/login/start", { method: "POST" });
    toast(r.message || "Navigateur ouvert.");
    $("#btn-login-done").classList.remove("hidden");
    $("#btn-login").disabled = true;
  } catch (e) { toast(e.message, "err"); }
});
$("#btn-login-done").addEventListener("click", async () => {
  $("#btn-login-done").disabled = true;
  try {
    const r = await api("/api/login/finish", { method: "POST" });
    toast(r.message || "OK", r.ok ? "ok" : "err");
    // verify the saved session works
    const c = await api("/api/login/check");
    toast(c.logged_in ? "Session valide ✅" : "Session enregistrée (vérification incertaine).", c.logged_in ? "ok" : "err");
  } catch (e) { toast(e.message, "err"); }
  $("#btn-login-done").classList.add("hidden");
  $("#btn-login-done").disabled = false;
  $("#btn-login").disabled = false;
  loadStatus();
});
$("#btn-check").addEventListener("click", async () => {
  try { const r = await api("/api/login/check"); toast(r.logged_in ? "Session valide ✅" : "Session invalide ❌", r.logged_in ? "ok" : "err"); }
  catch (e) { toast(e.message, "err"); }
  loadStatus();
});
$("#btn-debug").addEventListener("click", async () => {
  try { const r = await api("/api/debug/dump", { method: "POST" }); toast(JSON.stringify(r).slice(0, 120)); }
  catch (e) { toast(e.message, "err"); }
});

// ---------------- upload ----------------
$("#btn-upload").addEventListener("click", async () => {
  const f = $("#file-planning").files[0];
  if (!f) return toast("Choisissez un fichier .xlsx", "err");
  const fd = new FormData();
  fd.append("file", f);
  try {
    const r = await api("/api/planning/upload", { method: "POST", body: fd });
    $("#upload-info").textContent = `Importé — feuilles: ${(r.sheets || []).join(", ")}, ${r.rows_imported} ligne(s).`;
    toast("Planning importé ✅");
    loadStatus();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------- mode toggle ----------------
$$('input[name=mode]').forEach((r) =>
  r.addEventListener("change", () => {
    $("#periode-box").classList.toggle("hidden", $('input[name=mode]:checked').value !== "periode");
  })
);

// ---------------- sync ----------------
let logTimer = null;
$("#btn-sync").addEventListener("click", async () => {
  const mode = $('input[name=mode]:checked').value;
  const payload = { mode };
  if (mode === "periode") { payload.date_from = $("#date-from").value; payload.date_to = $("#date-to").value; }
  $("#btn-sync").disabled = true;
  $("#sync-info").textContent = "Synchronisation en cours…";
  logTimer = setInterval(loadLog, 1000);
  try {
    const r = await api("/api/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    renderReport(r.simulation);
    renderChanges(r.simulation);
    $("#sync-info").textContent = r.applied ? "Appliqué automatiquement." : "Simulation prête — voir Résultats.";
    toast("Synchronisation terminée ✅");
    // switch to results
    $$(".tab").forEach((x) => x.classList.remove("active"));
    $$(".panel").forEach((x) => x.classList.remove("active"));
    document.querySelector('[data-tab=results]').classList.add("active");
    $("#tab-results").classList.add("active");
  } catch (e) { toast(e.message, "err"); $("#sync-info").textContent = "Erreur."; }
  clearInterval(logTimer); loadLog();
  $("#btn-sync").disabled = false;
  loadStatus(); loadDashboard();
});

function renderReport(sim) {
  const c = sim.counts || {};
  $("#report").classList.remove("muted");
  $("#report").innerHTML = `
    <div><b>${c["analysées"] ?? 0}</b>analysées</div>
    <div><b>${c.residence ?? 0}</b>Résidence 16</div>
    <div><b>${(sim.changes || []).filter(x=>x.kind==='ajout').length}</b>ajoutées</div>
    <div><b>${(sim.changes || []).filter(x=>x.kind==='fusion').length}</b>fusions</div>
    <div><b>${(sim.changes || []).filter(x=>x.kind==='modification').length}</b>modifiées</div>
    <div><b>${(sim.changes || []).filter(x=>x.kind==='suppression').length}</b>annulées</div>
    <div><b>${c.en_attente ?? 0}</b>en attente</div>`;
}

function renderChanges(sim) {
  const tb = $("#changes-tbl tbody");
  tb.innerHTML = "";
  const an = $("#anomalies");
  an.innerHTML = (sim.anomalies || []).map(a => `<div class="anom">⚠️ Réf ${a.ref} — ${a.detail}</div>`).join("");
  (sim.changes || []).forEach((ch) => {
    const tr = document.createElement("tr");
    const title = ch.before ? avantApres(ch) : "";
    tr.innerHTML = `
      <td><input type="checkbox" class="chk-ch" value="${ch.ref}"></td>
      <td><span class="link" data-ref="${ch.ref}" title="${title}">${ch.ref}</span></td>
      <td><span class="badge b-${ch.kind}">${ch.kind}</span></td>
      <td title="${(ch.detail||'').replace(/"/g,'&quot;')}">${ch.detail || ""}</td>
      <td><button class="btn btn-ghost btn-verif" data-ref="${ch.ref}">👁️ Vérifier</button></td>`;
    tb.appendChild(tr);
  });
  $$(".btn-verif").forEach(b => b.addEventListener("click", () => openBooking(b.dataset.ref)));
}
function avantApres(ch) {
  const b = ch.before || {}, a = ch.after || {};
  const keys = ch.fields || Object.keys(a);
  return "Réf " + ch.ref + "\n" + keys.map(k => `${k}: ${b[k] ?? '—'} → ${a[k] ?? '—'}`).join("\n");
}
function openBooking(ref) {
  fetch("/api/booking-url/" + encodeURIComponent(ref)).then(r => r.json()).then(d => {
    if (d && d.url) window.open(d.url, "_blank");
  }).catch(() => {});
  fetch("/api/verify/" + ref, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ verified: true }) });
  toast("Réf " + ref + " marquée comme vérifiée.");
}
$("#btn-verify-sel").addEventListener("click", () => {
  const refs = [...$$(".chk-ch:checked")].map(c => c.value);
  if (!refs.length) return toast("Sélectionnez au moins une ligne.", "err");
  refs.forEach(openBooking);
});
$("#btn-apply").addEventListener("click", async () => {
  $("#btn-apply").disabled = true;
  try {
    const r = await api("/api/apply", { method: "POST" });
    toast(`Appliqué: +${r.added} ~${r.updated} -${r.deleted}. Téléchargement…`);
    window.location = "/api/planning/download";
    loadStatus(); loadDashboard();
  } catch (e) { toast(e.message, "err"); }
  $("#btn-apply").disabled = false;
});

// ---------------- journal ----------------
async function loadJournal() {
  const rows = await api("/api/journal");
  const tb = $("#journal-tbl tbody"); tb.innerHTML = "";
  rows.forEach(j => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${j.ts}</td>` +
      `<td><span class="link" onclick="openBooking('${j.ref}')">${j.ref}</span></td>` +
      `<td>${j.nom || ""}</td>` +
      `<td>${j.blassa || ""}</td>` +
      `<td><span class="badge b-${j.kind}">${j.kind}</span></td>` +
      `<td title="${(j.detail || '').replace(/"/g, '&quot;')}">${j.detail || ""}</td>`;
    tb.appendChild(tr);
  });
}

// ---------------- reservations ----------------
function resRow(r) {
  const tr = document.createElement("tr");
  if (r.canceled) tr.className = "res-canceled";
  tr.innerHTML = `<td><span class="link" onclick="openBooking('${r.reference}')">${r.reference}</span></td>
    <td>${r.nom || ""}</td><td>${r.section || ""}</td><td>${r.room_code || ""}</td>
    <td>${r.date_arrivee || ""}</td><td>${r.date_depart || ""}</td><td>${r.nb_personnes || ""}</td>
    <td>${r.status_raw || ""}</td>
    <td class="${r.verified ? 'verif-yes' : 'verif-no'}">${r.verified ? '✔' : '—'}</td>`;
  return tr;
}
async function loadReservations() {
  const q = $("#search").value;
  const rows = await api("/api/reservations?q=" + encodeURIComponent(q));
  const tb = $("#res-tbl tbody"); tb.innerHTML = "";
  const active = rows.filter(r => !r.canceled);
  const canceled = rows.filter(r => r.canceled);
  active.forEach(r => tb.appendChild(resRow(r)));
  if (canceled.length) {
    const sep = document.createElement("tr");
    sep.className = "res-sep";
    sep.innerHTML = `<td colspan="9">Annulées / Refusées — ${canceled.length}</td>`;
    tb.appendChild(sep);
    canceled.forEach(r => tb.appendChild(resRow(r)));
  }
}
$("#search").addEventListener("input", () => { clearTimeout(window._st); window._st = setTimeout(loadReservations, 300); });
window.openBooking = openBooking;

// ---------------- archives ----------------
async function loadArchives() {
  const rows = await api("/api/archives");
  const tb = $("#arch-tbl tbody"); tb.innerHTML = "";
  rows.forEach(a => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${a.name}</td><td>${a.date}</td><td>${(a.size/1024).toFixed(0)} Ko</td>
      <td><a class="link" href="/api/archives/${encodeURIComponent(a.name)}">Télécharger</a></td>`;
    tb.appendChild(tr);
  });
}

// ---------------- verify planning ----------------
async function loadCheckPlanning() {
  const f = $("#file-check").files[0];
  if (!f) return toast("Choisissez un fichier .xlsx", "err");
  const fd = new FormData();
  fd.append("file", f);
  $("#check-info").textContent = "Vérification…";
  try {
    const r = await api("/api/planning/check", { method: "POST", body: fd });

    $("#check-nb1").textContent = r.not_in_booking.length;
    $("#check-nb2").textContent = r.canceled_still.length;
    $("#check-nb3").textContent = (r.mismatched || []).length;
    $("#check-nb5").textContent = (r.overstay || []).length;

    const t1 = $("#check-tbl-1 tbody"); t1.innerHTML = "";
    r.not_in_booking.forEach(x => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><span class="link" onclick="openBooking('${x.ref}')">${x.ref}</span></td><td>${x.nom || ""}</td><td>${x.sheet || ""}</td><td>${x.section || ""}</td>`;
      t1.appendChild(tr);
    });

    const t2 = $("#check-tbl-2 tbody"); t2.innerHTML = "";
    r.canceled_still.forEach(x => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><span class="link" onclick="openBooking('${x.ref}')">${x.ref}</span></td><td>${x.status || ""}</td><td>${x.nom || ""}</td><td>${x.sheet || ""}</td><td>${x.section || ""}</td>`;
      t2.appendChild(tr);
    });

    const t3 = $("#check-tbl-3 tbody"); t3.innerHTML = "";
    (r.mismatched || []).forEach(x => {
      const tr = document.createElement("tr");
      const prob = x.issues.map(i => i.field).join(", ");
      const plan = Object.entries(x.plan || {})
        .map(([k, v]) => `${k}: ${v || "—"}`).join("<br>");
      const book = Object.entries(x.booking || {})
        .map(([k, v]) => `${k}: ${v || "—"}`).join("<br>");
      tr.innerHTML = `<td><span class="link" onclick="openBooking('${x.ref}')">${x.ref}</span></td>
        <td>${prob}</td><td>${x.plan?.nom || ""}</td><td>${plan}</td><td>${book}</td>`;
      t3.appendChild(tr);
    });

    const t5 = $("#check-tbl-5 tbody"); t5.innerHTML = "";
    (r.overstay || []).forEach(x => {
      const tr = document.createElement("tr");
      tr.className = "row-overstay";
      tr.innerHTML = `<td><span class="link" onclick="openBooking('${x.ref}')">${x.ref}</span></td>
        <td>${x.nom || ""}</td><td>${x.depart_prevu || ""}</td><td>${x.depart_reel || ""}</td>
        <td>${x.extra_days || ""}</td><td>${x.personnes || ""}</td>`;
      t5.appendChild(tr);
    });

    const corr = $("#check-corrected");
    corr.innerHTML = "";
    if (r.corrected_file) {
      corr.innerHTML = `<a class="btn btn-primary" href="/api/planning/check/download/${encodeURIComponent(r.corrected_file)}">⬇️ Télécharger le planning corrigé (overstay)</a>`;
    }

    let info = `Total ${r.total} référence(s). `;
    if (r.live_available === false) info += "⚠️ Données live Booking non disponibles (hors-ligne). ";
    $("#check-info").textContent = info.trim();
    toast("Vérification terminée ✅");
  } catch (e) { toast(e.message, "err"); $("#check-info").textContent = ""; }
}
$("#btn-check-planning").addEventListener("click", loadCheckPlanning);

// ---------------- No Show (live) ----------------
async function loadNoShow() {
  $("#noshow-info").textContent = "Chargement…";
  try {
    const r = await api("/api/no-show");
    $("#noshow-nb").textContent = (r.no_show || []).length;
    const tb = $("#noshow-tbl tbody"); tb.innerHTML = "";
    (r.no_show || []).forEach(x => {
      const tr = document.createElement("tr");
      tr.className = "row-noshow";
      tr.innerHTML = `<td><span class="link" onclick="openBooking('${x.ref}')">${x.ref}</span></td>
        <td>${x.nom || ""}</td><td>${x.arrivee || ""}</td><td>${x.personnes || ""}</td>
        <td>${x.room || ""}</td><td>${x.status || ""}</td>`;
      tb.appendChild(tr);
    });
    $("#noshow-info").textContent = `Aujourd'hui : ${r.today || ""} — ${r.no_show.length} No Show.`;
  } catch (e) { toast(e.message, "err"); $("#noshow-info").textContent = ""; }
}
$("#btn-no-show").addEventListener("click", loadNoShow);

// ---------------- settings ----------------
let CONFIG = null;
async function loadConfig() {
  CONFIG = await api("/api/config");
  $("#cfg-residence").value = CONFIG.residence;
  $("#cfg-threshold").value = CONFIG.group_threshold;
  $("#cfg-oncancel").value = CONFIG.on_cancel;
  $("#cfg-autoapply").checked = CONFIG.auto_apply;
  $("#cfg-autosync").checked = CONFIG.auto_sync_enabled;
  $("#cfg-autominutes").value = CONFIG.auto_sync_minutes;
  $("#residence-badge").textContent = CONFIG.residence;
  renderStatusRules(CONFIG.status_rules || []);
}
function renderStatusRules(rules) {
  const tb = $("#status-tbl tbody"); tb.innerHTML = "";
  rules.forEach((r, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="text" value="${r.match || ''}" data-i="${i}" data-k="match"></td>
      <td><input type="text" value="${r.label || ''}" data-i="${i}" data-k="label"></td>
      <td><select data-i="${i}" data-k="action">
        ${["add","delete","ignore"].map(a=>`<option ${r.action===a?'selected':''}>${a}</option>`).join("")}</select></td>
      <td><input type="text" value="${r.note || ''}" data-i="${i}" data-k="note"></td>
      <td><select data-i="${i}" data-k="nuitees">
        ${["dates","one_per_person"].map(a=>`<option ${r.nuitees===a?'selected':''}>${a}</option>`).join("")}</select></td>
      <td><button class="btn btn-ghost del-status" data-i="${i}">✕</button></td>`;
    tb.appendChild(tr);
  });
  $$(".del-status").forEach(b => b.addEventListener("click", () => {
    CONFIG.status_rules.splice(+b.dataset.i, 1); renderStatusRules(CONFIG.status_rules);
  }));
}
$("#btn-add-status").addEventListener("click", () => {
  CONFIG.status_rules = CONFIG.status_rules || [];
  CONFIG.status_rules.push({ match: "", label: "", action: "add", note: "", nuitees: "dates" });
  renderStatusRules(CONFIG.status_rules);
});
$("#btn-save-config").addEventListener("click", async () => {
  // gather status rules from inputs
  const rules = CONFIG.status_rules.map(x => ({ ...x }));
  $$("#status-tbl [data-i]").forEach(el => { rules[+el.dataset.i][el.dataset.k] = el.value; });
  const patch = {
    residence: $("#cfg-residence").value,
    group_threshold: +$("#cfg-threshold").value,
    on_cancel: $("#cfg-oncancel").value,
    auto_apply: $("#cfg-autoapply").checked,
    auto_sync_enabled: $("#cfg-autosync").checked,
    auto_sync_minutes: +$("#cfg-autominutes").value,
    status_rules: rules,
  };
  try { await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
    $("#cfg-info").textContent = "Enregistré ✅"; toast("Paramètres enregistrés ✅"); loadStatus();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------- init ----------------
loadStatus(); loadDashboard(); loadLog();
setInterval(loadStatus, 8000);
setInterval(loadLog, 3000);
