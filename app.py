import os
import sqlite3
import json
import re
import datetime as dt
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    PlainTextResponse,
)
from fastapi.middleware.cors import CORSMiddleware

# --- Optional Gmail imports ---
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request as GoogleRequest

    HAS_GMAIL = True
except Exception:
    HAS_GMAIL = False

# ---------------- CONFIG ----------------

DB_PATH = "expense_app.db"
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

AMOUNT_REGEX = re.compile(
    r"(?:₹|INR|Rs\.?)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)

# state -> Flow (for OAuth)
OAUTH_FLOWS: Dict[str, "Flow"] = {}

# ---------------- FASTAPI APP & DB ----------------

app = FastAPI(title="One-Page Expense + Todo App", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,               -- 'gmail','sms','manual'
            date TEXT NOT NULL,                 -- 'YYYY-MM-DD'
            merchant TEXT,
            description TEXT,
            amount REAL NOT NULL,
            raw TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()
    conn.close()


@app.on_event("startup")
def on_startup():
    init_db()

# ---------------- SETTINGS HELPERS ----------------


def get_setting(conn, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str):
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, value),
    )
    conn.commit()

# ---------------- EXPENSE HELPERS ----------------


def store_expense(
    conn,
    source: str,
    date: str,
    merchant: str,
    description: str,
    amount: float,
    raw: str = "",
):
    conn.execute(
        """
        INSERT INTO expenses (source,date,merchant,description,amount,raw)
        VALUES (?,?,?,?,?,?)
        """,
        (source, date, merchant, description, amount, raw),
    )
    conn.commit()


def parse_amount(text: str) -> Optional[float]:
    matches = [m.group(1) for m in AMOUNT_REGEX.finditer(text or "")]
    if not matches:
        return None
    nums = []
    for m in matches:
        try:
            nums.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return max(nums) if nums else None


def summarize_expenses(
    conn, days: int = 60, source: str = "all"
) -> Dict[str, Any]:
    params = []
    query = "SELECT date, amount, source, merchant, description FROM expenses WHERE 1=1"

    if days > 0:
        start_date = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
        query += " AND date >= ?"
        params.append(start_date)

    if source != "all":
        query += " AND source = ?"
        params.append(source)

    rows = conn.execute(query, params).fetchall()

    total = 0.0
    by_month: Dict[str, float] = {}
    by_day: Dict[str, float] = {}

    for r in rows:
        amount = float(r["amount"])
        total += amount
        month_key = r["date"][:7]
        by_month[month_key] = by_month.get(month_key, 0.0) + amount
        by_day[r["date"]] = by_day.get(r["date"], 0.0) + amount

    # full list for UI
    rows_full = conn.execute(
        """
        SELECT id, source, date, merchant, description, amount
        FROM expenses
        WHERE 1=1
        """
        + (" AND date >= ?" if days > 0 else "")
        + ("" if source == "all" else " AND source = ?")
        + " ORDER BY date DESC, id DESC",
        params,
    ).fetchall()

    expenses_list = [
        dict(
            id=r["id"],
            source=r["source"],
            date=r["date"],
            merchant=r["merchant"] or "",
            description=r["description"] or "",
            amount=float(r["amount"]),
        )
        for r in rows_full
    ]

    return {
        "total": total,
        "by_month": by_month,
        "by_day": by_day,
        "expenses": expenses_list,
    }


def build_insights(summary: Dict[str, Any]) -> list:
    insights = []
    by_month = summary.get("by_month", {})
    if by_month:
        months = sorted(by_month.keys())
        last = months[-1]
        last_spend = by_month[last]
        y, m = map(int, last.split("-"))
        now = dt.date.today()
        if now.year == y and now.month == m:
            days_so_far = now.day
        else:
            # days in that month
            days_so_far = (dt.date(y, (m % 12) + 1, 1) - dt.timedelta(days=1)).day
        avg = last_spend / days_so_far if days_so_far else 0
        insights.append(
            f"Spending in {last}: ₹{last_spend:,.0f} (avg ₹{avg:,.0f} per day)."
        )
        if len(months) >= 2:
            prev = months[-2]
            prev_spend = by_month[prev]
            if prev_spend > 0:
                diff = last_spend - prev_spend
                pct = diff / prev_spend * 100
                direction = "higher" if diff > 0 else "lower"
                insights.append(
                    f"{last} is {abs(pct):.1f}% {direction} than {prev}."
                )
    return insights

# ---------------- TODO HELPERS ----------------


def get_todos(conn):
    rows = conn.execute(
        "SELECT id,text,done,created_at FROM todos ORDER BY done, created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]

# ---------------- GMAIL HELPERS ----------------


def get_gmail_client_config(conn) -> Optional[Dict[str, Any]]:
    raw = get_setting(conn, "gmail_client_config")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def get_gmail_credentials(conn) -> Optional["Credentials"]:
    if not HAS_GMAIL:
        return None
    raw = get_setting(conn, "gmail_token")
    if not raw:
        return None
    info = json.loads(raw)
    creds = Credentials.from_authorized_user_info(info, scopes=GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        set_setting(conn, "gmail_token", creds.to_json())
    return creds


def sync_gmail_expenses(conn, creds: "Credentials", days: int = 60) -> int:
    """Fetch recent Gmail messages and insert as expenses. Returns new count."""
    service = build("gmail", "v1", credentials=creds)
    query = (
        f"newer_than:{days}d "
        '("payment successful" OR "payment of Rs" OR "debited" OR "order placed" OR "invoice")'
    )
    user_id = "me"
    next_page_token = None
    added = 0

    import base64
    from email.utils import parsedate_to_datetime

    while True:
        resp = (
            service.users()
            .messages()
            .list(
                userId=user_id,
                q=query,
                pageToken=next_page_token,
                maxResults=50,
            )
            .execute()
        )
        msgs = resp.get("messages", [])
        if not msgs:
            break

        for m in msgs:
            full = (
                service.users()
                .messages()
                .get(userId=user_id, id=m["id"], format="full")
                .execute()
            )
            payload = full.get("payload", {})
            headers = payload.get("headers", [])

            subject = next(
                (h["value"] for h in headers if h["name"].lower() == "subject"),
                "",
            )
            from_ = next(
                (h["value"] for h in headers if h["name"].lower() == "from"), ""
            )
            date_raw = next(
                (h["value"] for h in headers if h["name"].lower() == "date"), ""
            )

            # parse date
            try:
                dt_obj = parsedate_to_datetime(date_raw)
                date_str = dt_obj.date().isoformat()
            except Exception:
                date_str = dt.date.today().isoformat()

            body = ""
            if "parts" in payload:
                for part in payload["parts"]:
                    if part.get("mimeType") == "text/plain":
                        data = part.get("body", {}).get("data")
                        if data:
                            body = base64.urlsafe_b64decode(
                                data.encode("utf-8")
                            ).decode("utf-8", errors="ignore")
                            break
            else:
                data = payload.get("body", {}).get("data")
                if data:
                    body = base64.urlsafe_b64decode(data.encode("utf-8")).decode(
                        "utf-8", errors="ignore"
                    )

            amount = parse_amount(subject + "\n" + body)
            if amount is None:
                continue

            merchant = from_.split("<")[0].strip() or from_
            description = subject
            raw = json.dumps({"subject": subject, "from": from_})[:1000]

            store_expense(conn, "gmail", date_str, merchant, description, amount, raw)
            added += 1

        next_page_token = resp.get("nextPageToken")
        if not next_page_token:
            break

    return added

# ---------------- HTML / UI ROUTES ----------------


@app.get("/", response_class=HTMLResponse)
def index():
    # Professional-ish dashboard UI
    return HTMLResponse(
        f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Expense & Todo Tracker</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="theme-color" content="#020617" />
  <link rel="manifest" href="/manifest.webmanifest" />
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    if ("serviceWorker" in navigator) {{
      window.addEventListener("load", () => {{
        navigator.serviceWorker.register("/sw.js").catch(console.error);
      }});
    }}
  </script>
</head>
<body class="min-h-screen bg-slate-950 text-slate-100">
  <div class="max-w-6xl mx-auto px-4 py-6 space-y-5">
    <!-- Top bar -->
    <header class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 pb-4">
      <div>
        <h1 class="text-2xl font-semibold tracking-tight">Expense & Todo Tracker</h1>
        <p class="text-xs text-slate-400 mt-1">
          Unified dashboard for expenses (Gmail / SMS / manual) and daily tasks.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <span class="px-3 py-1 text-[11px] rounded-full bg-emerald-500/10 text-emerald-300 border border-emerald-500/40">
          Single-file · SQLite · PWA-ready
        </span>
        <button onclick="window.location.reload()"
                class="px-3 py-1 rounded-xl border border-slate-700 text-xs text-slate-200 hover:bg-slate-900">
          Refresh
        </button>
      </div>
    </header>

    <!-- Filters / actions -->
    <section class="bg-slate-900/80 border border-slate-800 rounded-2xl p-4 space-y-3 shadow-sm">
      <div class="flex flex-wrap gap-3 items-end">
        <div>
          <label class="block text-[11px] mb-1 text-slate-400">Look back (days)</label>
          <input id="days" type="number" value="60" min="1" max="365"
                 class="w-24 px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-sm focus:outline-none focus:ring-1 focus:ring-emerald-500" />
        </div>
        <div>
          <label class="block text-[11px] mb-1 text-slate-400">Source</label>
          <select id="sourceFilter"
                  class="px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-sm focus:outline-none focus:ring-1 focus:ring-emerald-500">
            <option value="all">All</option>
            <option value="gmail">Gmail</option>
            <option value="sms">SMS</option>
            <option value="manual">Manual</option>
          </select>
        </div>
        <button id="btnLoad"
                class="px-4 py-2 rounded-xl bg-emerald-500 text-slate-950 text-sm font-medium hover:bg-emerald-400 active:scale-95">
          Load summary
        </button>
        <button id="btnSyncGmail"
                class="px-4 py-2 rounded-xl bg-sky-500 text-slate-950 text-sm font-medium hover:bg-sky-400 active:scale-95">
          Sync from Gmail
        </button>
        <button id="btnConnectGmail"
                class="px-4 py-2 rounded-xl bg-slate-800 text-slate-100 text-xs border border-slate-600 hover:bg-slate-700">
          Connect Gmail (settings)
        </button>
      </div>

      <!-- KPI row -->
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-3" id="kpiRow" hidden>
        <div class="bg-slate-950/60 border border-slate-800 rounded-2xl p-3">
          <div class="text-[11px] uppercase tracking-wide text-slate-400">Total in range</div>
          <div id="kpiTotal" class="mt-1 text-xl font-semibold">₹0</div>
          <div id="kpiCount" class="mt-1 text-[11px] text-slate-500"></div>
        </div>
        <div class="bg-slate-950/60 border border-slate-800 rounded-2xl p-3">
          <div class="text-[11px] uppercase tracking-wide text-slate-400">Latest month</div>
          <div id="kpiMonthTotal" class="mt-1 text-xl font-semibold">₹0</div>
          <div id="kpiMonthLabel" class="mt-1 text-[11px] text-slate-500"></div>
        </div>
        <div class="bg-slate-950/60 border border-slate-800 rounded-2xl p-3">
          <div class="text-[11px] uppercase tracking-wide text-slate-400">Insights</div>
          <ul id="insightsList" class="mt-1 text-[11px] text-slate-300 space-y-1"></ul>
        </div>
      </div>
    </section>

    <!-- Main layout -->
    <section class="grid grid-cols-1 lg:grid-cols-[2.1fr,minmax(260px,1fr)] gap-4">
      <!-- Left: Expenses -->
      <div class="space-y-4">
        <!-- Expense table -->
        <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-4 shadow-sm">
          <div class="flex items-center justify-between mb-2">
            <h2 class="text-sm font-semibold">Expenses</h2>
            <span class="text-[11px] text-slate-500">Latest first</span>
          </div>
          <div class="overflow-x-auto rounded-xl border border-slate-900">
            <table class="min-w-full text-xs">
              <thead class="bg-slate-950 border-b border-slate-800 text-[10px] uppercase text-slate-400">
                <tr>
                  <th class="py-2 px-3 text-left">Date</th>
                  <th class="py-2 px-3 text-right">Amount</th>
                  <th class="py-2 px-3 text-left">Source</th>
                  <th class="py-2 px-3 text-left">Merchant</th>
                  <th class="py-2 px-3 text-left">Description</th>
                </tr>
              </thead>
              <tbody id="expenseBody" class="divide-y divide-slate-900 bg-slate-950/40"></tbody>
            </table>
          </div>
          <div id="noExpenses" class="text-xs text-slate-500 mt-2" hidden>No expenses yet.</div>
        </div>

        <!-- Add expense -->
        <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-4 space-y-3 shadow-sm">
          <div class="flex items-center justify-between">
            <h2 class="text-sm font-semibold">Add expense (SMS / manual)</h2>
            <span class="text-[11px] text-slate-500">Paste SMS text and amount if needed</span>
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label class="block text-[11px] mb-1 text-slate-400">Source</label>
              <select id="newSource"
                      class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-sm">
                <option value="sms">SMS</option>
                <option value="manual">Manual</option>
              </select>
            </div>
            <div>
              <label class="block text-[11px] mb-1 text-slate-400">Date</label>
              <input id="newDate" type="date"
                     class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-sm" />
            </div>
            <div>
              <label class="block text-[11px] mb-1 text-slate-400">Amount</label>
              <input id="newAmount" type="number" step="0.01"
                     class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-sm" />
            </div>
            <div>
              <label class="block text-[11px] mb-1 text-slate-400">Merchant</label>
              <input id="newMerchant" type="text"
                     class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-sm" />
            </div>
          </div>
          <div>
            <label class="block text-[11px] mb-1 text-slate-400">Description / SMS text</label>
            <textarea id="newDescription" rows="2"
                      class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-sm"></textarea>
          </div>
          <button id="btnAddExpense"
                  class="px-4 py-2 rounded-xl bg-emerald-500 text-slate-950 text-sm font-medium hover:bg-emerald-400 active:scale-95">
            Save expense
          </button>
        </div>
      </div>

      <!-- Right: Todos & Settings -->
      <div class="space-y-4">
        <!-- Todos -->
        <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-4 shadow-sm">
          <h2 class="text-sm font-semibold mb-2">Todo list</h2>
          <div class="flex gap-2 mb-3">
            <input id="todoText" type="text" placeholder="Next action..."
                   class="flex-1 px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-sm" />
            <button id="btnAddTodo"
                    class="px-3 py-2 rounded-xl bg-emerald-500 text-slate-950 text-xs font-medium hover:bg-emerald-400">
              Add
            </button>
          </div>
          <ul id="todoList" class="space-y-1 text-xs"></ul>
        </div>

        <!-- Settings -->
        <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-4 shadow-sm space-y-2">
          <h2 class="text-sm font-semibold mb-1">Backend settings</h2>
          <p class="text-[11px] text-slate-400 mb-2">
            Paste your Gmail OAuth client JSON (Desktop app) if you want Gmail sync.
            It is stored in SQLite on the server.
          </p>
          <textarea id="gmailConfig" rows="5"
                    class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-[11px] font-mono"
                    placeholder='{{"installed":{{"client_id":"...","client_secret":"...","redirect_uris":["http://localhost"]}}}}'></textarea>
          <button id="btnSaveSettings"
                  class="mt-2 px-3 py-2 rounded-xl bg-slate-800 text-slate-100 text-xs border border-slate-600 hover:bg-slate-700">
            Save Gmail config
          </button>
          <p id="gmailStatus" class="mt-2 text-[11px] text-slate-400"></p>
        </div>
      </div>
    </section>

    <div id="status" class="text-[11px] text-slate-400"></div>
  </div>

<script>
const fmt = (n) => "₹" + (n || 0).toLocaleString("en-IN", {{minimumFractionDigits: 2, maximumFractionDigits: 2}});

async function api(path, options={{}}) {{
  const res = await fetch(path, Object.assign({{headers: {{"Content-Type":"application/json"}}}}, options));
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}}

async function loadState() {{
  const days = Number(document.getElementById("days").value) || 60;
  const source = document.getElementById("sourceFilter").value;
  const data = await api(`/api/state?days=${{days}}&source=${{encodeURIComponent(source)}}`);

  const kpiRow = document.getElementById("kpiRow");
  const total = data.summary.total || 0;
  document.getElementById("kpiTotal").textContent = fmt(total);
  document.getElementById("kpiCount").textContent = `${{data.summary.expenses.length}} records`;

  const months = Object.entries(data.summary.by_month || {{}}).sort((a,b)=>a[0].localeCompare(b[0]));
  if (months.length) {{
    const [m, amt] = months[months.length-1];
    document.getElementById("kpiMonthTotal").textContent = fmt(amt);
    document.getElementById("kpiMonthLabel").textContent = m;
  }} else {{
    document.getElementById("kpiMonthTotal").textContent = fmt(0);
    document.getElementById("kpiMonthLabel").textContent = "-";
  }}

  const insightsList = document.getElementById("insightsList");
  insightsList.innerHTML = "";
  (data.insights || []).forEach(t => {{
    const li = document.createElement("li");
    li.textContent = "• " + t;
    insightsList.appendChild(li);
  }});
  if (!data.insights || !data.insights.length) {{
    const li = document.createElement("li");
    li.textContent = "No insights yet.";
    insightsList.appendChild(li);
  }}
  kpiRow.hidden = false;

  const tbody = document.getElementById("expenseBody");
  tbody.innerHTML = "";
  if (!data.summary.expenses.length) {{
    document.getElementById("noExpenses").hidden = false;
  }} else {{
    document.getElementById("noExpenses").hidden = true;
    data.summary.expenses.forEach(e => {{
      const tr = document.createElement("tr");
      tr.className = "hover:bg-slate-950/60";
      tr.innerHTML = `
        <td class="py-2 px-3 whitespace-nowrap">${{e.date}}</td>
        <td class="py-2 px-3 text-right whitespace-nowrap">${{fmt(e.amount)}}</td>
        <td class="py-2 px-3 whitespace-nowrap text-[10px] text-slate-400">${{e.source}}</td>
        <td class="py-2 px-3 whitespace-nowrap max-w-[120px] truncate">${{e.merchant}}</td>
        <td class="py-2 px-3 whitespace-nowrap max-w-[220px] truncate">${{e.description}}</td>`;
      tbody.appendChild(tr);
    }});
  }}

  const todoList = document.getElementById("todoList");
  todoList.innerHTML = "";
  data.todos.forEach(t => {{
    const li = document.createElement("li");
    li.className = "flex items-center gap-2";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!t.done;
    cb.onchange = async () => {{
      await api("/api/todos/toggle", {{method:"POST", body:JSON.stringify({{id: t.id}})}});
      loadState();
    }};
    const span = document.createElement("span");
    span.textContent = t.text;
    if (t.done) span.className = "line-through text-slate-500";
    const del = document.createElement("button");
    del.textContent = "×";
    del.className = "text-[10px] text-slate-500 hover:text-red-400";
    del.onclick = async () => {{
      await api("/api/todos/delete", {{method:"POST", body:JSON.stringify({{id:t.id}})}});
      loadState();
    }};
    li.appendChild(cb);
    li.appendChild(span);
    li.appendChild(del);
    todoList.appendChild(li);
  }});

  document.getElementById("gmailConfig").value = data.gmail_config || "";
  document.getElementById("gmailStatus").textContent = data.gmail_status;
}}

async function addExpense() {{
  const source = document.getElementById("newSource").value;
  const date = document.getElementById("newDate").value || new Date().toISOString().slice(0,10);
  const amt = parseFloat(document.getElementById("newAmount").value || "0");
  const merch = document.getElementById("newMerchant").value;
  const desc = document.getElementById("newDescription").value;
  if (!amt) {{
    alert("Amount is required");
    return;
  }}
  await api("/api/expenses/add", {{
    method:"POST",
    body: JSON.stringify({{source, date, amount:amt, merchant:merch, description:desc}})
  }});
  document.getElementById("newAmount").value = "";
  document.getElementById("newDescription").value = "";
  await loadState();
}}

async function addTodo() {{
  const txt = document.getElementById("todoText").value.trim();
  if (!txt) return;
  await api("/api/todos/add", {{method:"POST", body:JSON.stringify({{text:txt}})}});
  document.getElementById("todoText").value = "";
  await loadState();
}}

async function saveSettings() {{
  const cfg = document.getElementById("gmailConfig").value.trim();
  await api("/api/settings/gmail", {{method:"POST", body:JSON.stringify({{client_config_json: cfg}})}});
  alert("Saved Gmail config. Now click 'Connect Gmail'.");
  await loadState();
}}

function connectGmail() {{
  window.location.href = "/api/gmail/start";
}}

async function syncGmail() {{
  const status = document.getElementById("status");
  status.textContent = "Syncing from Gmail…";
  try {{
    const res = await api("/api/gmail/sync", {{method:"POST"}});
    status.textContent = `Synced ${{res.added}} expenses from Gmail.`;
    await loadState();
  }} catch (e) {{
    alert("Gmail sync error: " + e.message);
    status.textContent = "Gmail sync failed.";
  }}
}}

document.getElementById("btnLoad").onclick = loadState;
document.getElementById("btnAddExpense").onclick = addExpense;
document.getElementById("btnAddTodo").onclick = addTodo;
document.getElementById("btnSaveSettings").onclick = saveSettings;
document.getElementById("btnConnectGmail").onclick = connectGmail;
document.getElementById("btnSyncGmail").onclick = syncGmail;

loadState().catch(console.error);
</script>
</body>
</html>
        """
    )

@app.get("/manifest.webmanifest", response_class=PlainTextResponse)
def manifest():
    return PlainTextResponse(
        json.dumps(
            {
                "name": "Expense & Todo Tracker",
                "short_name": "Expenses",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#020617",
                "theme_color": "#020617",
                "icons": [],
            }
        ),
        media_type="application/manifest+json",
    )


@app.get("/sw.js", response_class=PlainTextResponse)
def service_worker():
    return PlainTextResponse(
        """
self.addEventListener('install', event => {
  self.skipWaiting();
});
self.addEventListener('activate', event => {
  clients.claim();
});
self.addEventListener('fetch', event => {
  // passthrough network – could add caching here
});
        """,
        media_type="application/javascript",
    )

# ---------------- JSON API ROUTES ----------------


@app.get("/api/state", response_class=JSONResponse)
def api_state(days: int = 60, source: str = "all"):
    conn = get_db()
    summary = summarize_expenses(conn, days=days, source=source)
    insights = build_insights(summary)
    todos = get_todos(conn)
    cfg = get_gmail_client_config(conn)
    gmail_status = (
        "Gmail support not installed."
        if not HAS_GMAIL
        else ("Gmail connected." if get_gmail_credentials(conn) else "Gmail not connected.")
    )
    return {
        "summary": summary,
        "insights": insights,
        "todos": todos,
        "gmail_config": json.dumps(cfg) if cfg else "",
        "gmail_status": gmail_status,
    }


@app.post("/api/expenses/add", response_class=JSONResponse)
async def api_add_expense(request: Request):
    data = await request.json()
    source = data.get("source", "manual")
    date = data.get("date") or dt.date.today().isoformat()
    amount = float(data.get("amount") or 0)
    if amount <= 0:
        raise HTTPException(400, "Amount must be > 0")
    merchant = data.get("merchant", "")
    description = data.get("description", "")

    conn = get_db()
    store_expense(conn, source, date, merchant, description, amount, description)
    return {"ok": True}


@app.post("/api/todos/add", response_class=JSONResponse)
async def api_add_todo(request: Request):
    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Text required")
    conn = get_db()
    conn.execute("INSERT INTO todos(text) VALUES (?)", (text,))
    conn.commit()
    return {"ok": True}


@app.post("/api/todos/toggle", response_class=JSONResponse)
async def api_toggle_todo(request: Request):
    data = await request.json()
    todo_id = int(data.get("id"))
    conn = get_db()
    row = conn.execute("SELECT done FROM todos WHERE id=?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Todo not found")
    new_done = 0 if row["done"] else 1
    conn.execute("UPDATE todos SET done=? WHERE id=?", (new_done, todo_id))
    conn.commit()
    return {"ok": True}


@app.post("/api/todos/delete", response_class=JSONResponse)
async def api_delete_todo(request: Request):
    data = await request.json()
    todo_id = int(data.get("id"))
    conn = get_db()
    conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
    conn.commit()
    return {"ok": True}


@app.post("/api/settings/gmail", response_class=JSONResponse)
async def api_save_gmail_settings(request: Request):
    data = await request.json()
    cfg = (data.get("client_config_json") or "").strip()
    conn = get_db()
    if not cfg:
        set_setting(conn, "gmail_client_config", "")
        return {"ok": True}
    try:
        parsed = json.loads(cfg)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")
    set_setting(conn, "gmail_client_config", json.dumps(parsed))
    return {"ok": True}

# ---------------- GMAIL ROUTES ----------------


@app.get("/api/gmail/start")
def api_gmail_start():
    # 1) Check libs
    if not HAS_GMAIL:
        return HTMLResponse(
            "<h3>Gmail support is not installed on this server.</h3>"
            "<p>Install google-api-python-client, google-auth-httplib2, google-auth-oauthlib and restart.</p>",
            status_code=500,
        )

    conn = get_db()
    cfg = get_gmail_client_config(conn)

    # 2) Check config
    if not cfg:
        return HTMLResponse(
            "<h3>No Gmail client config saved.</h3>"
            "<p>Go back → Settings → paste OAuth client JSON → Save Gmail config.</p>",
            status_code=400,
        )

    redirect_uri = f"{BASE_URL}/api/gmail/callback"

    try:
        flow = Flow.from_client_config(
            cfg,
            scopes=GMAIL_SCOPES,
            redirect_uri=redirect_uri,
        )
        # FIX: no include_granted_scopes=True (that caused the 400)
        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )
    except Exception as e:
        return HTMLResponse(
            f"<h3>Error building Google auth URL</h3><pre>{e}</pre>",
            status_code=500,
        )

    OAUTH_FLOWS[state] = flow
    return RedirectResponse(auth_url)


@app.get("/api/gmail/callback")
def api_gmail_callback(state: str, code: str):
    if state not in OAUTH_FLOWS:
        return HTMLResponse("Auth state expired. Try again.", status_code=400)

    flow = OAUTH_FLOWS.pop(state)
    flow.fetch_token(code=code)
    creds = flow.credentials

    conn = get_db()
    set_setting(conn, "gmail_token", creds.to_json())
    return HTMLResponse(
        "<h1>Gmail connected ✅</h1><p>You can close this tab and return to the app.</p>"
    )


@app.post("/api/gmail/sync", response_class=JSONResponse)
def api_gmail_sync():
    if not HAS_GMAIL:
        raise HTTPException(500, "Gmail libraries not installed on server.")
    conn = get_db()
    creds = get_gmail_credentials(conn)
    if not creds:
        raise HTTPException(400, "Gmail not connected. Save config + Connect first.")
    added = sync_gmail_expenses(conn, creds, days=60)
    return {"added": added}
