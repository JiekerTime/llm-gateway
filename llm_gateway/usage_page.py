"""Built-in usage analysis page."""

USAGE_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LLM Gateway Usage</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2933;
      --muted: #65758b;
      --line: #d9e0e8;
      --accent: #1677ff;
      --accent-soft: #e8f2ff;
      --warn: #ad5b00;
      --danger: #b42318;
      --ok: #087443;
      --shadow: 0 8px 24px rgba(31, 41, 51, 0.08);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }

    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
    }

    main {
      width: min(1320px, calc(100vw - 32px));
      margin: 20px auto 40px;
    }

    .controls {
      display: grid;
      grid-template-columns: minmax(220px, 1.6fr) repeat(3, minmax(120px, 0.6fr)) auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 16px;
    }

    label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }

    input, button {
      min-height: 36px;
      border-radius: 6px;
      border: 1px solid var(--line);
      font: inherit;
      letter-spacing: 0;
    }

    input {
      width: 100%;
      padding: 8px 10px;
      background: var(--panel);
      color: var(--text);
    }

    button {
      padding: 8px 14px;
      background: var(--accent);
      border-color: var(--accent);
      color: white;
      font-weight: 700;
      cursor: pointer;
    }

    button:disabled {
      opacity: 0.6;
      cursor: wait;
    }

    .status {
      min-height: 20px;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 12px;
    }

    .status.error { color: var(--danger); }

    .summary {
      display: grid;
      grid-template-columns: repeat(5, minmax(130px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }

    .metric, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }

    .metric {
      padding: 14px;
      min-width: 0;
    }

    .metric .label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }

    .metric .value {
      margin-top: 6px;
      font-size: 24px;
      font-weight: 750;
      overflow-wrap: anywhere;
    }

    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      align-items: start;
    }

    .panel {
      overflow: hidden;
      min-width: 0;
    }

    .panel h2 {
      margin: 0;
      padding: 12px 14px;
      font-size: 14px;
      border-bottom: 1px solid var(--line);
    }

    .rows {
      display: grid;
      gap: 0;
    }

    .row {
      display: grid;
      grid-template-columns: minmax(140px, 1.3fr) 96px 132px 90px;
      gap: 10px;
      align-items: center;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
    }

    .row:last-child { border-bottom: 0; }
    .row.head {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      background: #fbfcfd;
    }

    .key {
      min-width: 0;
      font-weight: 650;
      overflow-wrap: anywhere;
    }

    .sub {
      display: block;
      color: var(--muted);
      font-weight: 500;
      font-size: 12px;
      margin-top: 2px;
    }

    .number {
      font-variant-numeric: tabular-nums;
      text-align: right;
      white-space: nowrap;
    }

    .bar {
      height: 8px;
      background: var(--accent-soft);
      border-radius: 999px;
      overflow: hidden;
    }

    .fill {
      display: block;
      height: 100%;
      width: 0;
      background: var(--accent);
      border-radius: inherit;
    }

    .heavy .row {
      grid-template-columns: minmax(170px, 1.4fr) 120px 116px 116px 90px;
    }

    .badge {
      display: inline-block;
      padding: 2px 7px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }

    .empty {
      padding: 22px 14px;
      color: var(--muted);
    }

    @media (max-width: 960px) {
      header {
        align-items: flex-start;
        flex-direction: column;
      }
      .controls, .summary, .grid {
        grid-template-columns: 1fr;
      }
      .row, .heavy .row {
        grid-template-columns: 1fr;
        gap: 6px;
      }
      .row.head { display: none; }
      .number { text-align: left; }
    }
  </style>
</head>
<body>
  <header>
    <h1>LLM Gateway Usage</h1>
    <span id="period" class="status"></span>
  </header>

  <main>
    <section class="controls" aria-label="Usage filters">
      <label>
        API Key
        <input id="apiKey" type="password" autocomplete="off" placeholder="X-API-Key">
      </label>
      <label>
        Since
        <input id="since" value="24h" placeholder="24h, 7d, ISO time">
      </label>
      <label>
        Caller Prefix
        <input id="caller" placeholder="gp/">
      </label>
      <label>
        Limit
        <input id="limit" type="number" min="1" max="100" value="20">
      </label>
      <button id="refresh" type="button">Refresh</button>
    </section>

    <div id="status" class="status">Enter an API key and refresh.</div>

    <section class="summary" aria-label="Usage totals">
      <div class="metric"><div class="label">Calls</div><div id="mCalls" class="value">-</div></div>
      <div class="metric"><div class="label">Total Tokens</div><div id="mTotal" class="value">-</div></div>
      <div class="metric"><div class="label">Prompt Tokens</div><div id="mPrompt" class="value">-</div></div>
      <div class="metric"><div class="label">Completion Tokens</div><div id="mCompletion" class="value">-</div></div>
      <div class="metric"><div class="label">Cache Hit Tokens</div><div id="mCache" class="value">-</div></div>
    </section>

    <section class="grid">
      <div class="panel"><h2>Services</h2><div id="services" class="rows"></div></div>
      <div class="panel"><h2>Sources</h2><div id="sources" class="rows"></div></div>
      <div class="panel"><h2>Callers</h2><div id="callers" class="rows"></div></div>
      <div class="panel"><h2>Models</h2><div id="models" class="rows"></div></div>
      <div class="panel"><h2>Role Cards</h2><div id="roles" class="rows"></div></div>
      <div class="panel heavy" style="grid-column: 1 / -1;"><h2>Recent Heavy Calls</h2><div id="heavy" class="rows"></div></div>
    </section>
  </main>

  <script>
    const els = {
      apiKey: document.getElementById("apiKey"),
      since: document.getElementById("since"),
      caller: document.getElementById("caller"),
      limit: document.getElementById("limit"),
      refresh: document.getElementById("refresh"),
      status: document.getElementById("status"),
      period: document.getElementById("period")
    };

    els.apiKey.value = localStorage.getItem("llmGatewayApiKey") || "";
    els.since.value = localStorage.getItem("llmGatewayUsageSince") || "24h";
    els.caller.value = localStorage.getItem("llmGatewayUsageCaller") || "";

    function fmt(value) {
      return Number(value || 0).toLocaleString();
    }

    function pct(value, max) {
      if (!max) return 0;
      return Math.max(2, Math.round((Number(value || 0) / max) * 100));
    }

    function sourceDetail(row) {
      if (!row.top_callers || !row.top_callers.length) return "";
      return row.top_callers.slice(0, 3).map(item => item.caller).join(", ");
    }

    function renderRows(targetId, rows, keyField, maxTokens) {
      const target = document.getElementById(targetId);
      target.innerHTML = "";
      target.insertAdjacentHTML("beforeend", '<div class="row head"><div>Name</div><div class="number">Calls</div><div>Share</div><div class="number">Tokens</div></div>');
      if (!rows || !rows.length) {
        target.insertAdjacentHTML("beforeend", '<div class="empty">No data in this window.</div>');
        return;
      }
      rows.forEach(row => {
        const key = row[keyField] || "unknown";
        const detail = keyField === "source" ? sourceDetail(row) : "";
        const width = pct(row.total_tokens, maxTokens);
        target.insertAdjacentHTML("beforeend", `
          <div class="row">
            <div class="key">${escapeHtml(key)}${detail ? `<span class="sub">${escapeHtml(detail)}</span>` : ""}</div>
            <div class="number">${fmt(row.calls)}</div>
            <div class="bar"><span class="fill" style="width:${width}%"></span></div>
            <div class="number">${fmt(row.total_tokens)}</div>
          </div>
        `);
      });
    }

    function renderHeavy(rows) {
      const target = document.getElementById("heavy");
      target.innerHTML = "";
      target.insertAdjacentHTML("beforeend", '<div class="row head"><div>Caller</div><div>Model</div><div class="number">Prompt</div><div class="number">Completion</div><div class="number">Total</div></div>');
      if (!rows || !rows.length) {
        target.insertAdjacentHTML("beforeend", '<div class="empty">No large calls in this window.</div>');
        return;
      }
      rows.forEach(row => {
        const session = row.session_id ? `session ${row.session_id}` : row.service || row.source;
        target.insertAdjacentHTML("beforeend", `
          <div class="row">
            <div class="key">${escapeHtml(row.caller || "unknown")}<span class="sub">${escapeHtml(session || "")}</span></div>
            <div><span class="badge">${escapeHtml(row.model || "unknown")}</span></div>
            <div class="number">${fmt(row.prompt_tokens)}</div>
            <div class="number">${fmt(row.completion_tokens)}</div>
            <div class="number">${fmt(row.total_tokens)}</div>
          </div>
        `);
      });
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    async function refresh() {
      const apiKey = els.apiKey.value.trim();
      const since = els.since.value.trim() || "24h";
      const caller = els.caller.value.trim();
      const limit = els.limit.value || "20";

      if (!apiKey) {
        setStatus("API key is required.", true);
        return;
      }

      localStorage.setItem("llmGatewayApiKey", apiKey);
      localStorage.setItem("llmGatewayUsageSince", since);
      localStorage.setItem("llmGatewayUsageCaller", caller);

      els.refresh.disabled = true;
      setStatus("Loading usage data...", false);
      try {
        const params = new URLSearchParams({ since, limit });
        if (caller) params.set("caller", caller);
        const response = await fetch(`/usage/sources?${params.toString()}`, {
          headers: { "X-API-Key": apiKey }
        });
        if (!response.ok) {
          throw new Error(`${response.status} ${response.statusText}`);
        }
        const data = await response.json();
        render(data);
        setStatus("Usage data loaded.", false);
      } catch (error) {
        setStatus(`Failed to load usage data: ${error.message}`, true);
      } finally {
        els.refresh.disabled = false;
      }
    }

    function render(data) {
      const total = data.total || {};
      document.getElementById("mCalls").textContent = fmt(total.calls);
      document.getElementById("mTotal").textContent = fmt(total.total_tokens);
      document.getElementById("mPrompt").textContent = fmt(total.prompt_tokens);
      document.getElementById("mCompletion").textContent = fmt(total.completion_tokens);
      document.getElementById("mCache").textContent = fmt(total.cache_hit_tokens);
      els.period.textContent = data.period || "";

      const max = Math.max(
        ...(data.by_source || []).map(row => row.total_tokens || 0),
        ...(data.by_service || []).map(row => row.total_tokens || 0),
        ...(data.by_caller || []).map(row => row.total_tokens || 0),
        1
      );
      renderRows("services", data.by_service, "service", max);
      renderRows("sources", data.by_source, "source", max);
      renderRows("callers", data.by_caller, "caller", max);
      renderRows("models", data.by_model, "model", Math.max(...(data.by_model || []).map(row => row.total_tokens || 0), 1));
      renderRows("roles", data.by_role_card, "role_card", Math.max(...(data.by_role_card || []).map(row => row.total_tokens || 0), 1));
      renderHeavy(data.recent_heavy_calls || []);
    }

    function setStatus(message, error) {
      els.status.textContent = message;
      els.status.classList.toggle("error", Boolean(error));
    }

    els.refresh.addEventListener("click", refresh);
    [els.apiKey, els.since, els.caller, els.limit].forEach(input => {
      input.addEventListener("keydown", event => {
        if (event.key === "Enter") refresh();
      });
    });

    if (els.apiKey.value) refresh();
  </script>
</body>
</html>
"""
