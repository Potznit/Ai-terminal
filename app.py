import streamlit as st
import json
import os
import requests
import pandas as pd
import yfinance as yf
from google import genai
from google.genai import types

st.set_page_config(page_title="Autonomous AI Betting Terminal", page_icon="⚡", layout="wide")
st.title("⚡ Autonomous AI Market & Football Terminal")

# --- SECRETS & CONFIGURATION ---
gemini_key = st.secrets.get("GEMINI_API_KEY", "")
odds_api_key = st.secrets.get("ODDS_API_KEY", "")
telegram_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "8956869998:AAH9SEXc6qID3Ie1JDx3mffb8pHLVWxMgoE")
telegram_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "6565714528")

# --- CSV PAPER PORTFOLIO STORAGE ---
CSV_FILE = "paper_trades.csv"
STARTING_BANKROLL = 1000.0
STAKE_PERCENT = 0.10

COLUMNS = [
    "ID", "Kickoff_UTC", "League", "Matchup", "Pick", "Bookmaker", "Odds", "EV_Pct", "Stake", "Status", "P_L"
]

# --- TELEGRAM DISPATCH HELPER ---
def send_telegram_alert(message_html: str):
    """Sends formatted HTML alerts directly to Telegram."""
    token = telegram_token or st.session_state.get("tg_token", "")
    chat_id = telegram_chat_id or st.session_state.get("tg_chat_id", "")
    if not token or not chat_id:
        return False
    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message_html,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=8)
        return r.status_code == 200
    except Exception:
        return False

with st.sidebar:
    st.header("⚙️ Settings & Credentials")
    if not gemini_key:
        gemini_key = st.text_input("Gemini API Key", type="password")
    if not odds_api_key:
        odds_api_key = st.text_input("The Odds API Key", type="password")
    
    st.session_state["tg_token"] = st.text_input("Telegram Bot Token", value=telegram_token, type="password")
    st.session_state["tg_chat_id"] = st.text_input("Telegram Chat ID", value=telegram_chat_id)

    st.markdown("---")
    if st.button("🔔 Send Test Telegram Ping", use_container_width=True):
        success = send_telegram_alert("⚡ <b>AI Betting Terminal:</b> Telegram webhook connection verified successfully!")
        if success:
            st.success("Test ping sent to your Telegram!")
        else:
            st.error("Failed. Verify your Bot Token and Chat ID.")

if not gemini_key:
    st.warning("Please configure your Gemini API Key in Streamlit Secrets or sidebar to proceed.")
    st.stop()

client = genai.Client(api_key=gemini_key)

def load_portfolio() -> pd.DataFrame:
    if os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            for col in COLUMNS:
                if col not in df.columns:
                    df[col] = "N/A" if col == "Kickoff_UTC" else 0.0
            return df[COLUMNS]
        except Exception:
            pass
    return pd.DataFrame(columns=COLUMNS)

def save_portfolio(df: pd.DataFrame):
    df.to_csv(CSV_FILE, index=False
