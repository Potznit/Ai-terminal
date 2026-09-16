import os
import io
import time
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="AI Terminal", page_icon="⚡", layout="wide")

GITHUB_CSV_URL = "https://raw.githubusercontent.com/Potznit/Ai-terminal/main/paper_trades.csv"
LOCAL_CSV_PATH = "paper_trades.csv"

SCHEMA_COLUMNS = [
    "ID", "Model_Tag", "Kickoff_UTC", "League", "Matchup", 
    "Market", "Pick", "Bookmaker", "Odds", "Edge_Pct", 
    "Stake", "Status", "P_L"
]

MODELS = [
    {"name": "Early-Bird (48h - 6d)", "tag": "EARLY_BIRD", "starting_capital": 1000.0},
    {"name": "Core Arbitrage (24h - 48h)", "tag": "CORE_EV", "starting_capital": 1000.0},
    {"name": "Late-Steam (15m - 24h)", "tag": "LATE_STEAM", "starting_capital": 1000.0},
    {"name": "Live War Room (Halftime)", "tag": "HALFTIME_LIVE", "starting_capital": 1000.0},
]

def load_data():
    # 1. Attempt to fetch latest live ledger directly from GitHub
    try:
        url_with_cache_buster = f"{GITHUB_CSV_URL}?t={int(time.time())}"
        headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
        res = requests.get(url_with_cache_buster, headers=headers, timeout=5)
        if res.status_code == 200 and len(res.text.strip()) > 0:
            df = pd.read_csv(io.StringIO(res.text))
            for col in SCHEMA_COLUMNS:
                if col not in df.columns:
                    df[col] = 0.0 if col in ["Odds", "Edge_Pct", "Stake", "P_L"] else ""
            return df
    except Exception:
        pass

    # 2. Fallback to local copy if network fetch fails
    if os.path.exists(LOCAL_CSV_PATH):
        try:
            df = pd.read_csv(LOCAL_CSV_PATH)
            for col in SCHEMA_COLUMNS:
                if col not in df.columns:
                    df[col] = 0.0 if col in ["Odds", "Edge_Pct", "Stake", "P_L"] else ""
            return df
        except Exception:
            return pd.DataFrame(columns=SCHEMA_COLUMNS)

    return pd.DataFrame(columns=SCHEMA_COLUMNS)

def highlight_status_row(row):
    status = str(row.get("Status", "")).upper()
    if status == "WON":
        return ["color: #10b981; font-weight: 600;"] * len(row)  # Green
    elif status == "LOST":
        return ["color: #ef4444; font-weight: 600;"] * len(row)  # Red
    elif status == "PENDING":
        return ["color: #ffffff;"] * len(row)                   # White
    elif status == "PUSH":
        return ["color: #94a3b8;"] * len(row)                   # Gray
    return [""] * len(row)

st.title("⚡ Autonomous AI Multi-Horizon Betting Terminal")

df = load_data()

st.subheader("🏆 $1,000 Horizon Model Competition Leaderboard")

leaderboard_data = []
for m in MODELS:
    tag = m["tag"]
    start_cap = m["starting_capital"]
    
    m_trades = df[df["Model_Tag"] == tag] if not df.empty and "Model_Tag" in df.columns else pd.DataFrame()
    settled = m_trades[m_trades["Status"].isin(["WON", "LOST", "PUSH"])] if not m_trades.empty else pd.DataFrame()
    pending = m_trades[m_trades["Status"] == "PENDING"] if not m_trades.empty else pd.DataFrame()
    
    net_p_l = float(settled["P_L"].sum()) if not settled.empty else 0.0
    active_staked = float(pending["Stake"].sum()) if not pending.empty else 0.0
    current_equity = start_cap + net_p_l
    roi = (net_p_l / start_cap) * 100 if start_cap > 0 else 0.0
    
    total_bets = len(settled)
    wins = len(settled[settled["Status"] == "WON"]) if not settled.empty else 0
    win_rate = (wins / total_bets) * 100 if total_bets > 0 else 0.0
    
    leaderboard_data.append({
        "Horizon Model": m["name"],
        "Tag": tag,
        "Starting Capital": f"${start_cap:,.2f}",
        "Current Equity": f"${current_equity:,.2f}",
        "Net Profit/Loss": f"{'+' if net_p_l >= 0 else ''}${net_p_l:,.2f}",
        "ROI": f"{roi:+.2f}%",
        "Win Rate": f"{win_rate:.1f}% ({wins}/{total_bets})",
        "Active Staked": f"${active_staked:,.2f}"
    })

st.dataframe(pd.DataFrame(leaderboard_data), use_container_width=True, hide_index=True)

col1, col2 = st.columns([1, 4])
with col1:
    if st.button("🔄 Refresh Live Data"):
        st.cache_data.clear()
        st.rerun()

st.divider()
st.subheader("📋 Multi-Model Master Ledger")

if df.empty:
    st.info("Ledger is currently empty. Waiting for upcoming model opportunities...")
else:
    display_df = df.copy()
    display_df = display_df.sort_values(by="Kickoff_UTC", ascending=False)
    styled_df = display_df.style.apply(highlight_status_row, axis=1)
    st.dataframe(styled_df, use_container_width=True, hide_index=True)
