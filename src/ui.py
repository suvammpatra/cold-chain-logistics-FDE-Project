import os
import sys
import uuid
import json
import time
import hmac
import socket
import urllib
from pathlib import Path
from dotenv import load_dotenv
import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

# ==========================================
# 1. IMMEDIATE PATH & ENVIRONMENT RESOLUTION
# ==========================================
script_dir = Path(__file__).resolve().parent  # points to src/
project_root = script_dir.parent              # climbs to project root

if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

load_dotenv(project_root / ".env")

# Import the compiled graph (and the LLM it uses, for the status panel)
from src.orchestrator import fde_agent, llm, AGENT_LLM_SETTING

# ==========================================
# 2. SQL CREDENTIALS MAPPING FROM .ENV
# ==========================================
from scripts.db_config import get_target

# Server/port/database follow DB_TARGET in .env (local -> 127.0.0.1:1433/master,
# aws -> SSH tunnel 127.0.0.1:14330/SupplyChainDB). Never use "localhost": on Windows it
# resolves to IPv6 ::1 first, while the Docker container only listens on IPv4 127.0.0.1.
db_target = get_target()
db_host, db_port, db_name = db_target.host, db_target.port, db_target.database
db_user = os.getenv("SQL_AGENT_USER", "USR_FDE_RO")
db_password = os.getenv("SQL_AGENT_PASSWORD")

# The audit trail lives on ONE central server (AWS by default), independent of DB_TARGET.
# The agent login may only INSERT there; only the admin login of that server can read it.
audit_target = get_target(os.getenv("AUDIT_DB_TARGET", "aws"))

# Engine for the Agent to write logs using its standard credentials
connection_string = (
    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
    f"SERVER={audit_target.host},{audit_target.port};"
    f"DATABASE={audit_target.database};"
    f"UID={db_user};"
    f"PWD={db_password};"
    f"Encrypt=no;"
    f"TrustServerCertificate=yes;"
)

log_params = urllib.parse.quote_plus(connection_string)

log_engine = create_engine(f"mssql+pyodbc:///?odbc_connect={log_params}")

def write_audit_log(session_id, node_name, tool_name, content):
    """Silently writes agent execution traces to the SQL audit table using agent permissions."""
    try:
        with log_engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO FDE_VIEWS.AgentAuditLog (SessionID, NodeExecuted, ToolName, Content)
                VALUES (:session_id, :node_name, :tool_name, :content)
            """), {
                "session_id": session_id,
                "node_name": node_name,
                "tool_name": tool_name,
                "content": content
            })
            conn.commit()
    except Exception as e:
        print(f"Audit Log Failed (Silent): {e}")

# ==========================================
# 3. UI CONSTANTS & HELPERS
# ==========================================
SYSTEM_PROMPT_PATH = project_root / "src" / "prompts" / "system_prompt.txt"

TOOL_LABELS = {
    "query_telemetry_db": ("🗄️", "Telemetry database"),
    "fetch_corridor_conditions": ("🌦️", "Corridor conditions"),
    "search_compliance_sop": ("📘", "Compliance SOP search"),
}

SUGGESTED_PROMPTS = [
    ("🌡️ Temperature breaches", "Which shipments have breached the 4°C cold-chain limit, and what does the SOP tell us to do?"),
    ("🚨 Tier 2 escalations", "Show High Risk shipments with a delay probability above 0.65 that need Tier 2 escalation."),
    ("⚓ Port congestion", "Is port congestion above 7.0 anywhere? Check corridor conditions and recommend a diversion."),
    ("📊 Fleet risk overview", "Summarize overall fleet risk: how many shipments are High, Moderate and Low risk?"),
]

MAX_TRACE_CHARS = 4000  # keep very large tool outputs from flooding the chat


def tool_label(name: str) -> tuple[str, str]:
    return TOOL_LABELS.get(name, ("🔧", name))


def llm_display_name() -> str:
    model = getattr(llm, "model_name", None) or getattr(llm, "model", None) or "unknown model"
    provider = {"QROQ": "Groq", "GROQ": "Groq", "OPENAI": "OpenAI"}.get(AGENT_LLM_SETTING, "Ollama (local)")
    return f"{provider} · {model}"


@st.cache_data(ttl=15, show_spinner=False)
def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


@st.cache_data(show_spinner=False)
def load_system_prompt() -> str | None:
    try:
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def render_trace(trace: dict) -> None:
    icon, label = tool_label(trace["name"])
    if trace["type"] == "tool_input":
        st.markdown(f"{icon} **{label}** · request")
        st.json(trace["args"], expanded=False)
    else:
        st.markdown(f"{icon} **{label}** · result")
        content = str(trace["content"])
        if len(content) > MAX_TRACE_CHARS:
            content = content[:MAX_TRACE_CHARS] + f"\n… ({len(content) - MAX_TRACE_CHARS:,} more characters)"
        st.code(content, language="text")


def trace_summary(traces: list, elapsed: float | None) -> str:
    calls = sum(1 for t in traces if t["type"] == "tool_input")
    timing = f" · {elapsed:.1f}s" if elapsed is not None else ""
    return f"🔍 Agent trace · {calls} tool call{'s' if calls != 1 else ''}{timing}"


def status_row(label: str, value: str, ok: bool | None = None) -> str:
    dot = "" if ok is None else f'<span class="dot {"ok" if ok else "down"}"></span>'
    return f'<div class="status-row"><span class="muted">{label}</span><span>{dot}{value}</span></div>'


# ==========================================
# 4. PAGE CONFIGURATION & ENTERPRISE THEME
# ==========================================
st.set_page_config(
    page_title="FDE Supply Chain Dispatch Console",
    page_icon="🧊",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .stApp { background-color: #0B0E14; color: #E2E8F0; }
    div[data-testid="stSidebar"] { background-color: #111622; border-right: 1px solid #1E293B; }
    .stMarkdown code { background-color: #1E293B !important; color: #38BDF8 !important; }
    .block-container { padding-top: 2rem; max-width: 1200px; }

    .hero {
        padding: 1.3rem 1.6rem; margin-bottom: 1.2rem;
        border: 1px solid #1E293B; border-radius: 14px;
        background: linear-gradient(135deg, rgba(56,189,248,0.10), rgba(99,102,241,0.05));
    }
    .hero-title { font-size: 1.7rem; font-weight: 700; color: #F1F5F9; line-height: 1.2; }
    .hero-sub { color: #94A3B8; margin-top: 0.35rem; font-size: 0.95rem; }
    .chip {
        display: inline-block; padding: 2px 10px; margin: 10px 6px 0 0;
        border: 1px solid #1E293B; border-radius: 999px;
        background: #0F172A; color: #CBD5E1; font-size: 0.75rem;
    }

    .status-row { display: flex; justify-content: space-between; gap: 8px; font-size: 0.82rem; padding: 3px 0; }
    .status-row span:last-child { text-align: right; color: #E2E8F0; }
    .muted { color: #64748B; }
    .dot { height: 8px; width: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
    .dot.ok { background: #22C55E; box-shadow: 0 0 6px rgba(34,197,94,0.6); }
    .dot.down { background: #EF4444; box-shadow: 0 0 6px rgba(239,68,68,0.6); }

    div[data-testid="stMetric"] {
        background: #111622; border: 1px solid #1E293B; border-radius: 12px; padding: 0.8rem 1rem;
    }
    div[data-testid="stChatMessage"] { border-radius: 12px; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 5. MULTI-USER STATE & THREAD MANAGEMENT
# ==========================================
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

if "ui_messages" not in st.session_state:
    st.session_state.ui_messages = []

if "system_prompt_sent" not in st.session_state:
    st.session_state.system_prompt_sent = False

thread_config = {"configurable": {"thread_id": st.session_state.thread_id}}

# ==========================================
# 6. SIDEBAR NAVIGATION & METADATA
# ==========================================
with st.sidebar:
    logo = script_dir / "image_L25X5q.png"
    st.image(str(logo) if logo.exists() else "https://cdn-icons-png.flaticon.com/512/2830/2830305.png", width=56)
    st.title("FDE Command Center")
    st.caption("Cold-chain incident response")

    app_mode = st.radio("System Mode", ["🧊 Dispatch Console", "🛡️ Security & Audit Logs"])

    st.divider()
    st.markdown("**System status**")
    telemetry_ok = port_is_open(db_host, db_port)
    audit_ok = port_is_open(audit_target.host, audit_target.port)
    st.markdown(
        status_row("Telemetry DB", f"{db_target.name.upper()} · {db_name}", telemetry_ok)
        + status_row("Audit store", f"{audit_target.name.upper()} · {audit_target.database}", audit_ok)
        + status_row("Reasoner", llm_display_name())
        + status_row("Embeddings", os.getenv("Embeddings_model", "LOCAL").split("#")[0].strip()),
        unsafe_allow_html=True,
    )
    if not (telemetry_ok and audit_ok):
        hint = "Start the SSH tunnel" if "aws" in (db_target.name, audit_target.name) else "Start the Docker container"
        st.caption(f"⚠️ A database is unreachable. {hint}.")

    st.divider()
    st.caption(f"Session `{st.session_state.thread_id[:8]}` · {len(st.session_state.ui_messages)} messages")
    if st.button("🧹 New session", width="stretch", help="Clear the conversation and start a fresh agent thread"):
        st.session_state.ui_messages = []
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.system_prompt_sent = False
        st.rerun()

# ==========================================
# 7. VIEW ROUTING (DISPATCH VS AUDIT)
# ==========================================

if app_mode == "🧊 Dispatch Console":
    # ------------------------------------------
    # TAB 1: CHAT UI & AGENT EXECUTION
    # ------------------------------------------
    st.markdown(f"""
    <div class="hero">
        <div class="hero-title">🧊 Cold-Chain Incident Control</div>
        <div class="hero-sub">Ask about fleet telemetry, corridor hazards and SOP compliance. The agent queries live systems and cites the rules it applies.</div>
        <span class="chip">🗄️ Telemetry · {db_target.name.upper()}</span>
        <span class="chip">🌦️ Live corridor weather</span>
        <span class="chip">📘 SOP knowledge base</span>
    </div>
    """, unsafe_allow_html=True)

    # Empty state: suggested starting questions
    if not st.session_state.ui_messages:
        st.markdown("##### Try one of these")
        cols = st.columns(2)
        for i, (title, prompt) in enumerate(SUGGESTED_PROMPTS):
            with cols[i % 2]:
                if st.button(f"**{title}**  \n{prompt}", key=f"suggest_{i}", width="stretch"):
                    st.session_state.pending_prompt = prompt
                    st.rerun()

    # Conversation history
    for entry in st.session_state.ui_messages:
        with st.chat_message(entry["role"], avatar="👤" if entry["role"] == "user" else "🤖"):
            if entry.get("traces"):
                with st.expander(trace_summary(entry["traces"], entry.get("elapsed")), expanded=False):
                    for trace in entry["traces"]:
                        render_trace(trace)
            if entry.get("error"):
                st.error(entry["content"])
            else:
                st.markdown(entry["content"])

    typed_input = st.chat_input("Ask about fleet telemetry, corridor updates or compliance thresholds…")
    user_input = typed_input or st.session_state.pop("pending_prompt", None)

    if user_input:
        st.session_state.ui_messages.append({"role": "user", "content": user_input})
        with st.chat_message("user", avatar="👤"):
            st.markdown(user_input)

        # First turn of a thread: send the business system prompt once (the checkpointer keeps it)
        new_messages = [HumanMessage(content=user_input)]
        if not st.session_state.system_prompt_sent:
            system_prompt = load_system_prompt()
            if system_prompt:
                new_messages.insert(0, SystemMessage(content=system_prompt))

        with st.chat_message("assistant", avatar="🤖"):
            final_response = ""
            current_traces = []
            run_error = None
            started = time.perf_counter()

            with st.status("🧠 Reasoning about your request…", expanded=True) as status:
                try:
                    events = fde_agent.stream(
                        {"messages": new_messages},
                        config=thread_config,
                        stream_mode="updates"
                    )
                    st.session_state.system_prompt_sent = True

                    for event in events:
                        for node_name, node_state in event.items():

                            if node_name == "reasoner":
                                latest_msg = node_state["messages"][-1]

                                # A. Intercept Tool Call Requests (Inputs)
                                if hasattr(latest_msg, "tool_calls") and latest_msg.tool_calls:
                                    for tool_call in latest_msg.tool_calls:
                                        icon, label = tool_label(tool_call["name"])
                                        status.update(label=f"{icon} Calling {label}…")
                                        trace = {"type": "tool_input", "name": tool_call["name"], "args": tool_call["args"]}
                                        render_trace(trace)
                                        current_traces.append(trace)

                                        write_audit_log(
                                            session_id=st.session_state.thread_id,
                                            node_name="reasoner",
                                            tool_name=tool_call['name'],
                                            content=json.dumps(tool_call['args'])
                                        )

                                # B. Intercept Final Generation
                                if latest_msg.content:
                                    final_response = latest_msg.content
                                    status.update(label="📝 Writing the resolution report…")

                                    write_audit_log(
                                        session_id=st.session_state.thread_id,
                                        node_name="reasoner_final",
                                        tool_name="LLM Text Synthesis",
                                        content=final_response
                                    )

                            elif node_name == "tools":
                                for msg in node_state.get("messages", []):
                                    if isinstance(msg, ToolMessage):
                                        trace = {"type": "tool_output", "name": msg.name, "content": msg.content}
                                        render_trace(trace)
                                        current_traces.append(trace)

                                        write_audit_log(
                                            session_id=st.session_state.thread_id,
                                            node_name="tools",
                                            tool_name=msg.name,
                                            content=msg.content
                                        )

                    elapsed = time.perf_counter() - started
                    status.update(label=trace_summary(current_traces, elapsed).replace("🔍", "✅"),
                                  state="complete", expanded=False)

                except Exception as e:
                    elapsed = time.perf_counter() - started
                    run_error = f"{type(e).__name__}: {e}"
                    status.update(label="❌ The agent run failed", state="error", expanded=True)

            if run_error:
                message = f"The agent could not complete this request.\n\n`{run_error}`"
                st.error(message)
                st.session_state.ui_messages.append(
                    {"role": "assistant", "content": message, "traces": current_traces, "elapsed": elapsed, "error": True}
                )
            elif final_response:
                st.markdown(final_response)
                st.session_state.ui_messages.append(
                    {"role": "assistant", "content": final_response, "traces": current_traces, "elapsed": elapsed}
                )
            else:
                message = "The agent finished without writing an answer. Try rephrasing the question."
                st.warning(message)
                st.session_state.ui_messages.append(
                    {"role": "assistant", "content": message, "traces": current_traces, "elapsed": elapsed, "error": True}
                )


elif app_mode == "🛡️ Security & Audit Logs":
    # ------------------------------------------
    # TAB 2: AUDIT LOG VIEWER (ADMIN ONLY)
    # ------------------------------------------
    st.markdown(f"""
    <div class="hero">
        <div class="hero-title">🛡️ Agent Audit Trail</div>
        <div class="hero-sub">Every tool call, tool result and final answer from the Dispatch Console, stored in
        <code>FDE_VIEWS.AgentAuditLog</code>. Restricted to the {audit_target.name.upper()} database administrator.</div>
        <span class="chip">📍 {audit_target.name.upper()} · {audit_target.database}</span>
        <span class="chip">🔒 Agent login: write-only</span>
    </div>
    """, unsafe_allow_html=True)

    def load_audit_logs(engine) -> pd.DataFrame:
        with engine.connect() as conn:
            return pd.read_sql("""
                SELECT LogID, Timestamp, SessionID, NodeExecuted, ToolName, Content
                FROM FDE_VIEWS.AgentAuditLog
                ORDER BY Timestamp DESC
            """, conn)

    if "audit_engine" not in st.session_state:
        # ---------- Login gate ----------
        left, _ = st.columns([3, 2])
        with left:
            with st.form("admin_auth_form", border=True):
                st.markdown("#### 🔐 Administrator sign-in")
                input_user = st.text_input("Admin Username", value="")
                input_pass = st.text_input("Admin Password", type="password", value="")
                submit_admin = st.form_submit_button("Authenticate & Load Logs", type="primary", width="stretch")

            if submit_admin:
                # Gate 1 (app): must match the audit server's admin login (AWS_SQL_USER / AWS_SQL_PASSWORD)
                # Gate 2 (database): the query runs with the entered credentials, so SQL Server
                # itself rejects anyone without SELECT on FDE_VIEWS.AgentAuditLog.
                is_admin = (
                    hmac.compare_digest(input_user.encode(), audit_target.user.encode())
                    and hmac.compare_digest(input_pass.encode(), audit_target.password.encode())
                )

                if is_admin:
                    try:
                        # Build an isolated admin connection string for viewing data
                        admin_params = urllib.parse.quote_plus(
                            "DRIVER={ODBC Driver 18 for SQL Server};"
                            f"SERVER={audit_target.host},{audit_target.port};"
                            f"DATABASE={audit_target.database};"
                            f"UID={input_user};"
                            f"PWD={input_pass};"
                            "Encrypt=no;"
                            "TrustServerCertificate=yes;"
                        )
                        admin_engine = create_engine(f"mssql+pyodbc:///?odbc_connect={admin_params}")
                        st.session_state.audit_df = load_audit_logs(admin_engine)
                        st.session_state.audit_engine = admin_engine  # kept in server memory for this session only
                        st.session_state.audit_user = input_user
                        st.rerun()
                    except Exception as e:
                        st.error(f"Database Query Failed: {e}")
                else:
                    st.error("❌ Invalid Administrator Credentials.")
    else:
        # ---------- Authenticated view ----------
        bar_left, bar_refresh, bar_logout = st.columns([6, 1.3, 1.3])
        with bar_left:
            st.success(f"Signed in as **{st.session_state.audit_user}** on {audit_target.name.upper()} · {audit_target.database}")
        with bar_refresh:
            if st.button("🔄 Refresh", width="stretch"):
                try:
                    st.session_state.audit_df = load_audit_logs(st.session_state.audit_engine)
                except Exception as e:
                    st.error(f"Database Query Failed: {e}")
        with bar_logout:
            if st.button("🚪 Sign out", width="stretch"):
                st.session_state.audit_engine.dispose()
                for key in ("audit_engine", "audit_df", "audit_user"):
                    st.session_state.pop(key, None)
                st.rerun()

        df = st.session_state.audit_df

        if df.empty:
            st.info("No audit logs yet. Ask the agent something in the Dispatch Console, then click Refresh.")
        else:
            df = df.copy()
            df["Timestamp"] = pd.to_datetime(df["Timestamp"])

            # Summary metrics
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Events", f"{len(df):,}")
            m2.metric("Sessions", f"{df['SessionID'].nunique():,}")
            m3.metric("Tool calls", f"{(df['NodeExecuted'] == 'reasoner').sum():,}")
            m4.metric("Last activity", df["Timestamp"].max().strftime("%d %b, %H:%M"))

            # Filters
            f1, f2, f3 = st.columns([2, 2, 3])
            with f1:
                sessions = ["All sessions"] + df["SessionID"].dropna().unique().tolist()
                session_filter = st.selectbox(
                    "Session", sessions,
                    format_func=lambda s: s if s == "All sessions" else f"{s[:8]}…"
                )
            with f2:
                tool_filter = st.multiselect("Tool", sorted(df["ToolName"].dropna().unique().tolist()))
            with f3:
                search = st.text_input("Search content", placeholder="e.g. breach, Long Beach, 4.0")

            view = df
            if session_filter != "All sessions":
                view = view[view["SessionID"] == session_filter]
            if tool_filter:
                view = view[view["ToolName"].isin(tool_filter)]
            if search:
                view = view[view["Content"].fillna("").str.contains(search, case=False, regex=False)]

            st.caption(f"Showing {len(view):,} of {len(df):,} events")
            st.dataframe(
                view,
                column_config={
                    "LogID": st.column_config.NumberColumn("ID", format="%d", width="small"),
                    "Timestamp": st.column_config.DatetimeColumn("Execution Time", format="DD MMM YYYY, h:mm a"),
                    "SessionID": st.column_config.TextColumn("Session Token", width="small"),
                    "NodeExecuted": st.column_config.TextColumn("Graph Node", width="small"),
                    "ToolName": "Tool Triggered",
                    "Content": st.column_config.TextColumn("Raw Payload Data", width="large"),
                },
                hide_index=True,
                width="stretch",
                height=520
            )

            st.download_button(
                "⬇️ Download filtered logs (CSV)",
                data=view.to_csv(index=False).encode("utf-8"),
                file_name="agent_audit_log.csv",
                mime="text/csv",
            )
