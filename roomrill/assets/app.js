"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    mode: "connecting", token: null, established: false, online: false, busy: false,
    state: null, roomId: null, stages: [], selectedStage: null, timer: null,
    manualLightDirty: false, manualMediaDirty: false, signatures: new Map(),
    submission: null,
  };

  class APIError extends Error {
    constructor(message, code = "request_failed", status = 0) {
      super(message);
      this.name = "APIError";
      this.code = code;
      this.status = status;
    }
  }

  const setText = (id, value) => { $(id).textContent = String(value ?? "—"); };
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  };
  const human = (value) => String(value ?? "Unknown").replaceAll("_", " ");
  const capital = (value) => {
    const text = human(value);
    return text.charAt(0).toUpperCase() + text.slice(1);
  };
  const utc = (value) => {
    if (!value) return "—";
    return String(value).replace("T", " ").replace(/(?:\.\d+)?(?:Z|\+00:00)$/, " UTC");
  };
  const lightText = (value) => value && value.on ? `On · ${value.brightness}%` : value && value.on === false ? "Off · 0%" : "Unknown";
  const targetText = (scope, value) => scope === "light" ? lightText(value) : capital(value);
  const targetsText = (targets) => Object.entries(targets || {}).map(([scope, value]) => `${capital(scope)}: ${targetText(scope, value)}`).join(" · ") || "No targets";
  const versionText = (value) => value?.version !== undefined ? `Version ${value.version}` : "Version unknown";
  const currentRoom = () => ui.state?.rooms?.find((room) => room.id === ui.roomId);
  const isFixture = () => ui.state?.mode === "synthetic-local-simulator" || ui.state?.mode === "demo";
  const writable = () => ui.mode === "live" && ui.online && !ui.busy && ui.state?.health?.mutations_allowed !== false;
  const announce = (message) => setText("page-status", message);
  const showError = (message) => {
    setText("action-error-message", message);
    $("action-error").hidden = false;
  };
  const clearError = () => { $("action-error").hidden = true; };

  async function request(path, body, timeout = 8000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const headers = {Accept: "application/json"};
      const options = {cache: "no-store", credentials: "same-origin", headers, signal: controller.signal};
      if (body !== undefined) {
        options.method = "POST";
        headers["Content-Type"] = "application/json";
        headers["X-Roomrill-Token"] = ui.token;
        options.body = JSON.stringify(body);
      }
      const response = await fetch(path, options);
      const contentType = response.headers.get("content-type") || "";
      if (!contentType.includes("application/json")) {
        if (path === "api/session" && (response.ok || [404, 405, 501].includes(response.status))) {
          throw new APIError("No local session is available at this address.", "static_host", response.status);
        }
        throw new APIError(`The server returned a non-JSON response (${response.status}).`, "invalid_response", response.status);
      }
      let result;
      try { result = await response.json(); }
      catch { throw new APIError("The server returned invalid JSON.", "invalid_response", response.status); }
      if (!response.ok) {
        if (path === "api/session" && [404, 405, 501].includes(response.status)) {
          throw new APIError("No local session is available at this address.", "static_host", response.status);
        }
        throw new APIError(result?.error?.message || `Request failed (${response.status}).`, result?.error?.code, response.status);
      }
      return result;
    } catch (error) {
      if (error instanceof APIError) throw error;
      throw new APIError(error.name === "AbortError" ? "The server did not respond in time." : "The local server could not be reached.", "connection_failed");
    } finally { clearTimeout(timer); }
  }

  function checkState(state) {
    if (!state || !Array.isArray(state.rooms) || !Array.isArray(state.leases) || !Array.isArray(state.commands) || !Array.isArray(state.events)) {
      throw new APIError("The server returned an unsupported state format.", "invalid_state");
    }
    return state;
  }

  function setConnection(online, message = "") {
    ui.online = online;
    const dot = $("connection-dot");
    dot.className = `connection-dot ${ui.mode === "recorded" ? "recorded" : online ? "live" : "offline"}`;
    setText("mode-label", ui.mode === "recorded" ? "Recorded synthetic example" : online ? "Synthetic local simulator" : ui.established ? "Simulator offline" : "No local connection");
    $("connection-notice").hidden = !message;
    setText("connection-message", message);
    $("retry").hidden = ui.mode === "recorded";
    syncControls();
  }

  async function establishSession() {
    const session = await request("api/session");
    if (!session || typeof session.token !== "string" || !session.token) throw new APIError("The server returned an invalid session.", "invalid_session");
    ui.token = session.token;
    ui.established = true;
    ui.mode = "live";
  }

  async function loadRecording() {
    const recording = await request("recorded.json");
    if (recording?.format !== 1 || !Array.isArray(recording.stages) || !recording.stages.length) throw new APIError("No supported recording is available.", "invalid_recording");
    const seen = new Set();
    for (const stage of recording.stages) {
      if (typeof stage.id !== "string" || !stage.id || seen.has(stage.id) || typeof stage.label !== "string") throw new APIError("The recording has invalid saved stages.", "invalid_recording");
      checkState(stage.state);
      seen.add(stage.id);
    }
    ui.mode = "recorded";
    ui.stages = recording.stages;
    $("saved-stage").replaceChildren(...ui.stages.map((stage) => {
      const option = node("option", "", stage.label);
      option.value = stage.id;
      return option;
    }));
    $("recording-panel").hidden = false;
    chooseStage(ui.stages[0].id);
    setConnection(false);
  }

  function chooseStage(id) {
    const stage = ui.stages.find((entry) => entry.id === id);
    if (!stage) return;
    ui.selectedStage = stage.id;
    $("saved-stage").value = stage.id;
    ui.manualLightDirty = false;
    ui.manualMediaDirty = false;
    applyState(stage.state);
    announce(`Saved stage: ${stage.label}. Read-only recording.`);
  }

  async function connect() {
    clearTimeout(ui.timer);
    $("retry").disabled = true;
    try {
      await establishSession();
      applyState(checkState(await request("api/state")));
      setConnection(true);
      announce("Local simulator connected. State refreshed.");
    } catch (error) {
      if (!ui.established && error.code === "static_host") {
        try { await loadRecording(); return; }
        catch (recordingError) {
          setConnection(false, `No local simulator or saved recording is available. ${recordingError.message} Run “roomrill demo --serve” and open its printed URL.`);
        }
      } else {
        setConnection(false, `${error.message} ${ui.established ? "Displayed state is the last received snapshot. Controls are disabled until a fresh connection returns." : "Run “roomrill demo --serve” and open its printed numeric loopback URL."}`);
      }
    } finally {
      $("retry").disabled = false;
      if (ui.mode !== "recorded") schedulePoll();
    }
  }

  function schedulePoll() {
    clearTimeout(ui.timer);
    ui.timer = setTimeout(poll, 2000);
  }

  async function poll() {
    if (ui.mode === "recorded") return;
    if (ui.busy || document.hidden) { schedulePoll(); return; }
    if (!ui.established || !ui.online) { await connect(); return; }
    try {
      applyState(checkState(await request("api/state")));
      setConnection(true);
    } catch (error) {
      setConnection(false, `${error.message} Displayed state is the last received snapshot. Controls are disabled until a fresh connection returns.`);
    }
    schedulePoll();
  }

  async function mutate(path, body, message, options = {}) {
    if (!writable()) return false;
    ui.busy = true;
    clearError();
    syncControls();
    try {
      const result = await request(path, body, options.timeout || 8000);
      if (options.draft) return result;
      applyState(checkState(result));
      setConnection(true);
      announce(message);
      return true;
    } catch (error) {
      if (!error.status || error.status >= 500) {
        if (options.draft && error.code === "model_not_configured") {
          setText("draft-feedback", error.message);
          $("draft-feedback").classList.add("error-text");
        } else {
          setConnection(false, `${error.message} ${options.draft ? "No draft was filled." : "The result may be unconfirmed; a fresh state will be loaded before controls are enabled."}`);
        }
      }
      if (options.draft) {
        setText("draft-feedback", error.message);
        $("draft-feedback").classList.add("error-text");
      } else showError(error.message);
      if (error.status === 403 || error.code === "token_refused") {
        setConnection(false, "The local session changed. Reconnecting before enabling controls. Review fresh state before retrying the action.");
        ui.token = null;
      }
      return false;
    } finally {
      ui.busy = false;
      syncControls();
      schedulePoll();
    }
  }

  function syncControls() {
    const disabled = !writable() || !currentRoom();
    document.querySelectorAll("[data-mutation]").forEach((element) => { element.disabled = disabled; });
    $("manual-light-brightness").disabled = disabled || !$("manual-light-on").checked;
    $("lease-light-on").disabled = disabled || !$("lease-use-light").checked;
    $("lease-brightness").disabled = disabled || !$("lease-use-light").checked || !$("lease-light-on").checked;
    $("lease-media").disabled = disabled || !$("lease-use-media").checked;
    $("advance-seconds").disabled = disabled || !isFixture();
    $("advance-form").querySelector("button").disabled = disabled || !isFixture();
    $("advance-note").textContent = isFixture() ? "Synthetic clock only. This is not a wall-time latency measurement." : "Time advancement is available only in demo fixture-clock mode.";
    $("manual-light-output").textContent = $("manual-light-on").checked ? `${$("manual-light-brightness").value}%` : "0%";
    $("lease-brightness-output").textContent = $("lease-light-on").checked ? `${$("lease-brightness").value}%` : "0%";
  }

  function applyState(state) {
    ui.state = state;
    const unhealthy = state.health?.mutations_allowed === false;
    $("health-notice").hidden = !unhealthy;
    if (unhealthy) {
      const accepted = typeof state.clock === "string" && state.clock !== state.now ? ` Accepted clock: ${utc(state.clock)}.` : "";
      setText("health-message", `${state.health.message || "The simulator has disabled mutations."}${accepted} Controls remain disabled until the server permits mutations.`);
    }
    if (!state.rooms.some((room) => room.id === ui.roomId)) {
      ui.roomId = state.rooms[0]?.id ?? null;
      ui.manualLightDirty = false;
      ui.manualMediaDirty = false;
    }
    const roomSignature = JSON.stringify(state.rooms.map((room) => [room.id, room.name]));
    if (ui.signatures.get("rooms") !== roomSignature) {
      $("room-select").replaceChildren(...state.rooms.map((room) => {
        const option = node("option", "", room.name || room.id);
        option.value = room.id;
        return option;
      }));
      ui.signatures.set("rooms", roomSignature);
    }
    $("room-select").value = ui.roomId || "";
    setText("fixture-time", utc(state.now));
    if (state.now) $("fixture-time").dateTime = state.now;
    setText("clock-label", isFixture() ? "Fixture clock · UTC" : "Clock · UTC");
    renderRoom();
    renderLeases();
    renderEvidence();
    const raw = JSON.stringify(state, null, 2);
    if ($("raw-state").textContent !== raw) setText("raw-state", raw);
    syncControls();
  }

  function renderRoom() {
    const room = currentRoom();
    if (!room) {
      setText("room-link-state", "No room");
      return;
    }
    setText("room-link-state", room.online ? "Connected" : "Disconnected");
    $("room-link-state").className = `status-pill ${room.online ? "online" : "offline"}`;
    setText("toggle-link", room.online ? "Disconnect" : "Reconnect");
    $("automatic-delivery").checked = Boolean(ui.state.automatic_delivery);
    for (const scope of ["light", "media"]) {
      const slot = room.scopes?.[scope] || {};
      setText(`${scope}-device`, targetText(scope, slot.device?.value));
      setText(`${scope}-device-version`, versionText(slot.device));
      setText(`${scope}-observed`, targetText(scope, slot.observed?.value));
      setText(`${scope}-observed-version`, versionText(slot.observed));
      const winner = slot.winner;
      setText(`${scope}-winner`, winner ? `${winner.owner} · priority ${winner.priority}` : "No eligible lease");
      const reasons = {
        highest_priority_then_newer_sequence: winner ? `Highest priority; later submission breaks ties. Sequence ${winner.sequence}.` : "No eligible lease.",
        no_eligible_lease: "No eligible lease.",
      };
      setText(`${scope}-reason`, slot.reason ? (reasons[slot.reason] || capital(slot.reason)) : "No decision reason supplied.");
      $(`${scope}-reason`).hidden = !winner;
      const ownership = slot.ownership;
      setText(`${scope}-ownership`, ownership ? `Owned by ${ownership.lease_id}. ${ownership.restore_policy === "compare" ? "Guarded restore" : "Leave state"}; acknowledged version ${ownership.last_version}.` : "No automatic ownership.");
    }
    const light = room.scopes?.light?.device?.value;
    if (!ui.manualLightDirty && light) {
      $("manual-light-on").checked = Boolean(light.on);
      if (light.on) $("manual-light-brightness").value = light.brightness;
    }
    if (!ui.manualMediaDirty && room.scopes?.media?.device?.value) $("manual-media").value = room.scopes.media.device.value;
    const pending = Object.values(room.scopes || {}).flatMap((slot) => slot.pending || []);
    renderChanged("pending-list", pending, () => pending.length ? pending.map(renderPending) : [node("li", "empty-state", "No pending commands for this room.")]);
  }

  function renderChanged(id, value, render) {
    const signature = JSON.stringify(value);
    if (ui.signatures.get(id) === signature) return;
    $(id).replaceChildren(...render());
    ui.signatures.set(id, signature);
  }

  function badge(text, status) { return node("span", `status-pill ${status || ""}`, capital(text)); }
  function meta(parts) {
    const list = node("div", "item-meta");
    for (const part of parts.filter((value) => value !== null && value !== undefined && value !== "")) list.append(node("span", "", part));
    return list;
  }
  function rawDetails(value, label = "Raw details") {
    const details = node("details", "item-raw");
    details.append(node("summary", "", label), node("pre", "", JSON.stringify(value, null, 2)));
    return details;
  }
  function action(text, callback) {
    const button = node("button", "button small secondary", text);
    button.type = "button";
    button.dataset.mutation = "";
    button.addEventListener("click", callback);
    return button;
  }
  function renderPending(command) {
    const item = node("li", "command-item");
    const top = node("div", "item-top");
    const title = node("div", "item-title");
    title.append(node("h3", "", `${capital(command.scope)} · ${command.kind === "restore" ? "Restore" : "Requested state"}`), badge("pending", "pending"));
    top.append(title, action("Deliver", () => mutate("api/deliver", {id: command.id}, "Command delivery completed. Inspect its receipt or refusal reason in Evidence.")));
    item.append(top, node("p", "item-description", targetText(command.scope, command.target)), meta([`Expected version ${command.expected_version}`, `Deadline ${utc(command.deadline)}`]), node("p", "command-id item-detail", command.id), rawDetails(command));
    return item;
  }

  const endedStatuses = new Set(["cancelled", "canceled", "expired", "ended", "suppressed", "closed"]);
  function leaseEnded(lease) { return endedStatuses.has(lease.status); }
  function renderLeases() {
    const filter = $("lease-filter").value;
    const leases = ui.state.leases.filter((lease) => lease.room === ui.roomId).filter((lease) => filter === "all" || (filter === "ended" ? leaseEnded(lease) : !leaseEnded(lease))).slice().sort((a, b) => (b.sequence || 0) - (a.sequence || 0));
    renderChanged("lease-list", [ui.roomId, filter, leases], () => leases.length ? leases.map(renderLease) : [node("li", "empty-state", filter === "all" ? "No leases for this room. Create one to request a state." : `No ${filter} leases for this room.`)]);
  }

  function renderLease(lease) {
    const item = node("li", "lease-item");
    const top = node("div", "item-top");
    const title = node("div", "item-title");
    title.append(node("h3", "", lease.owner), badge(lease.status || "Unknown"));
    const actions = node("div", "item-actions");
    actions.append(action("Compete", () => copyLease(lease)));
    if (!leaseEnded(lease)) actions.append(action("Cancel", () => mutate("api/cancel", {id: lease.id}, `Lease ${lease.id} cancelled.`)));
    top.append(title, actions);
    item.append(top, node("p", "item-description", targetsText(lease.targets)), meta([`Priority ${lease.priority}`, capital(lease.role), `${lease.restore_policy === "compare" ? "Guarded restore" : "Leave state"}`, `Ends ${utc(lease.expires_at)}`]));
    const resolutionLabels = {winner: "Current winner", lower_priority: "Lower priority", later_submission_sequence: "A later request won the tie", scope_suppressed: "Suppressed by a manual or external change", cancelled: "Cancelled", expired: "Expired", no_eligible_lease: "No eligible lease"};
    const resolutions = Object.entries(lease.resolution || {}).map(([scope, decision]) => `${capital(scope)}: ${resolutionLabels[decision.reason] || capital(decision.reason)}`);
    if (resolutions.length) item.append(node("p", "item-detail", resolutions.join(" · ")));
    if (lease.suppressed_scopes?.length) item.append(node("p", "item-detail", `Suppressed: ${lease.suppressed_scopes.map(capital).join(", ")}. Existing requests do not override a manual change.`));
    item.append(node("p", "command-id item-detail", lease.id), rawDetails(lease));
    return item;
  }

  function fillTargets(targets) {
    $("lease-use-light").checked = Object.hasOwn(targets, "light");
    $("lease-use-media").checked = Object.hasOwn(targets, "media");
    if (targets.light) {
      $("lease-light-on").checked = Boolean(targets.light.on);
      if (targets.light.on) $("lease-brightness").value = targets.light.brightness;
    }
    if (targets.media) $("lease-media").value = targets.media;
  }

  function copyLease(lease) {
    if (!writable()) return;
    fillTargets(lease.targets || {});
    const maximum = $("lease-role").value === "member" ? 50 : 100;
    $("lease-priority").value = Math.min(maximum, Number(lease.priority) + 1);
    $("lease-duration").value = lease.duration_seconds || 120;
    $("lease-restore").value = lease.restore_policy;
    ui.submission = null;
    setText("lease-feedback", `Copied ${lease.owner}’s targets. Review owner, priority and duration, then create a separate lease.`);
    syncControls();
    $("lease-owner").focus();
  }

  function renderEvidence() {
    const commands = ui.state.commands.filter((command) => command.room === ui.roomId).slice().reverse();
    renderChanged("command-list", [ui.roomId, commands], () => commands.length ? commands.map(renderCommand) : [node("li", "empty-state", "No commands or receipts for this room.")]);
    const events = ui.state.events.filter((event) => !event.room || event.room === ui.roomId).slice().reverse();
    renderChanged("event-list", [ui.roomId, events], () => events.length ? events.map(renderEvent) : [node("li", "empty-state", "No events for this room.")]);
  }

  function renderCommand(command) {
    const item = node("li", "command-item");
    const top = node("div", "item-top");
    const title = node("div", "item-title");
    title.append(node("h3", "", `${capital(command.scope)} · ${command.kind === "restore" ? "Restore" : "Requested state"}`), badge(command.status, command.status));
    top.append(title, node("span", "command-id", command.id));
    item.append(top, node("p", "item-description", targetText(command.scope, command.target)), node("p", "item-detail", capital(command.reason || "No reason supplied")), meta([`Deciding lease: ${command.lease_id || "Closure proof"}`, `Expected version ${command.expected_version}`, `Deadline ${utc(command.deadline)}`]));
    if (command.status === "applied") {
      const before = command.before?.version ?? command.before_version;
      const after = command.after?.version ?? command.after_version;
      const receipt = before !== undefined && after !== undefined ? `Receipt · version ${before} → ${after}` : "Applied receipt · see recorded proof below";
      item.append(node("p", "receipt-line", receipt));
    }
    item.append(rawDetails(command, command.status === "applied" ? "Receipt & command details" : "Command details"));
    return item;
  }

  function renderEvent(event) {
    const item = node("li", "event-item");
    const top = node("div", "item-top");
    top.append(node("h3", "event-kind", capital(event.kind)), node("time", "event-time", utc(event.at)));
    const parts = [];
    for (const key of ["scope", "lease_id", "command_id", "reason"]) if (event[key] !== undefined) parts.push(`${capital(key)}: ${key === "reason" ? human(event[key]) : event[key]}`);
    item.append(top, meta(parts), rawDetails(event, "Event details"));
    return item;
  }

  function numeric(id) { return Number($(id).value); }
  function newId() { return globalThis.crypto?.randomUUID?.() || `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
  function lightFrom(onId, brightnessId) { return $(onId).checked ? {on: true, brightness: numeric(brightnessId)} : {on: false, brightness: 0}; }

  $("room-select").addEventListener("change", () => {
    ui.roomId = $("room-select").value;
    ui.manualLightDirty = false;
    ui.manualMediaDirty = false;
    applyState(ui.state);
  });
  $("saved-stage").addEventListener("change", () => chooseStage($("saved-stage").value));
  $("retry").addEventListener("click", connect);
  $("dismiss-error").addEventListener("click", clearError);
  $("lease-filter").addEventListener("change", () => ui.state && renderLeases());
  $("manual-light-on").addEventListener("change", () => { ui.manualLightDirty = true; syncControls(); });
  $("manual-light-brightness").addEventListener("input", () => { ui.manualLightDirty = true; syncControls(); });
  $("manual-media").addEventListener("change", () => { ui.manualMediaDirty = true; });
  $("manual-light-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (await mutate("api/manual", {room: ui.roomId, scope: "light", value: lightFrom("manual-light-on", "manual-light-brightness")}, "Manual light applied. Existing light leases are suppressed.")) { ui.manualLightDirty = false; renderRoom(); syncControls(); }
  });
  $("manual-media-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (await mutate("api/manual", {room: ui.roomId, scope: "media", value: $("manual-media").value}, "Manual media applied. Existing media leases are suppressed.")) { ui.manualMediaDirty = false; renderRoom(); }
  });
  $("toggle-link").addEventListener("click", () => {
    const room = currentRoom();
    if (room) mutate("api/link", {room: room.id, online: !room.online}, room.online ? "Room disconnected. Last observed state is retained." : "Room reconnected and reconciled.");
  });
  $("automatic-delivery").addEventListener("change", () => mutate("api/delivery", {automatic: $("automatic-delivery").checked}, "Automatic delivery setting updated."));
  $("reconcile").addEventListener("click", () => mutate("api/reconcile", {}, "Simulator reconciled."));
  $("advance-form").addEventListener("submit", (event) => {
    event.preventDefault();
    mutate("api/advance", {seconds: numeric("advance-seconds")}, "Fixture time advanced. Review lease and command state.");
  });
  $("lease-role").addEventListener("change", () => {
    const maximum = $("lease-role").value === "member" ? 50 : 100;
    $("lease-priority").max = maximum;
    if (numeric("lease-priority") > maximum) $("lease-priority").value = maximum;
    setText("role-hint", `${capital($("lease-role").value)} priority: 0–${maximum}. Role labels are local input.`);
  });
  ["lease-use-light", "lease-use-media", "lease-light-on"].forEach((id) => $(id).addEventListener("change", syncControls));
  $("lease-brightness").addEventListener("input", syncControls);
  $("lease-form").addEventListener("input", () => { ui.submission = null; setText("lease-feedback", ""); });
  $("lease-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const targets = {};
    if ($("lease-use-light").checked) targets.light = lightFrom("lease-light-on", "lease-brightness");
    if ($("lease-use-media").checked) targets.media = $("lease-media").value;
    if (!Object.keys(targets).length) { setText("lease-feedback", "Select at least one requested scope: Light or Media."); $("lease-use-light").focus(); return; }
    const fields = {room: ui.roomId, owner: $("lease-owner").value.trim(), role: $("lease-role").value, priority: numeric("lease-priority"), duration_seconds: numeric("lease-duration"), restore_policy: $("lease-restore").value, targets};
    if (!fields.owner) { setText("lease-feedback", "Enter an owner label."); $("lease-owner").focus(); return; }
    const signature = JSON.stringify(fields);
    if (!ui.submission || ui.submission.signature !== signature) ui.submission = {signature, id: newId()};
    const id = ui.submission.id;
    if (await mutate("api/leases", {id, ...fields}, "Lease created. Inspect the current winner and resulting commands.")) {
      ui.submission = null;
      setText("lease-feedback", `Created lease ${id}.`);
    }
  });
  $("draft-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("draft-feedback").classList.remove("error-text");
    setText("draft-feedback", "Requesting a draft…");
    const result = await mutate("api/draft", {text: $("draft-text").value}, "", {draft: true, timeout: 60000});
    if (!result) return;
    const draft = result.draft;
    if (!draft || result.requires_confirmation !== true || !draft.targets || !ui.state.rooms.some((room) => room.id === draft.room)) {
      setText("draft-feedback", "The server returned an unsupported draft. No fields were changed.");
      $("draft-feedback").classList.add("error-text");
      return;
    }
    ui.roomId = draft.room;
    ui.manualLightDirty = false;
    ui.manualMediaDirty = false;
    $("lease-priority").value = draft.priority;
    $("lease-duration").value = draft.duration_seconds;
    $("lease-restore").value = draft.restore_policy;
    fillTargets(draft.targets);
    ui.submission = null;
    applyState(ui.state);
    setText("draft-feedback", `Draft filled${result.model ? ` · ${result.model}` : ""}. Review the editable form, then select Create lease. Nothing has been created.`);
    setText("lease-feedback", "Review the draft’s room, targets, priority, duration and restore policy before creating a lease.");
    $("lease-priority").focus();
  });
  function evidenceView(kind) {
    const commands = kind === "commands";
    $("command-list").hidden = !commands;
    $("event-list").hidden = commands;
    $("show-commands").classList.toggle("active", commands);
    $("show-events").classList.toggle("active", !commands);
    $("show-commands").setAttribute("aria-pressed", String(commands));
    $("show-events").setAttribute("aria-pressed", String(!commands));
  }
  $("show-commands").addEventListener("click", () => evidenceView("commands"));
  $("show-events").addEventListener("click", () => evidenceView("events"));
  document.addEventListener("visibilitychange", () => { if (!document.hidden && ui.mode !== "recorded" && !ui.busy) { clearTimeout(ui.timer); poll(); } });
  window.addEventListener("pagehide", () => { clearTimeout(ui.timer); });
  syncControls();
  connect();
})();
