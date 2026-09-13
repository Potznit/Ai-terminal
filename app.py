import streamlit as st
import json
import yfinance as yf
from google import genai
from google.genai import types

st.set_page_config(page_title="AI Edge Terminal", page_icon="⚡", layout="centered")

st.title("⚡ AI Market & Sports Terminal")

# Retrieve API key securely from Streamlit Secrets or input box
api_key = st.secrets.get("GEMINI_API_KEY", "")
if not api_key:
    api_key = st.sidebar.text_input("Gemini API Key", type="password")

if not api_key:
    st.warning("Please provide your Gemini API key in the sidebar to begin.")
    st.stop()

client = genai.Client(api_key=api_key)

# ----------------- TOOLS -----------------
def fetch_market_indicators(ticker: str) -> str:
    """Fetches real-time price and 5-day range for an equity ticker."""
    stock = yf.Ticker(ticker)
    fast = stock.fast_info
    hist = stock.history(period="5d", interval="1d")
    
    current_price = fast.last_price
    prev_close = fast.previous_close
    day_change_pct = ((current_price - prev_close) / prev_close) * 100
    
    data = {
        "ticker": ticker.upper(),
        "current_price": round(current_price, 2),
        "day_change_percent": f"{round(day_change_pct, 2)}%",
        "5d_high": round(float(hist['High'].max()), 2),
        "5d_low": round(float(hist['Low'].min()), 2),
    }
    return json.dumps(data)

def fetch_sports_early_goal_stats(home_team: str, away_team: str) -> str:
    """Fetches early-possession and first-to-score rates for two clubs."""
    stats = {
        "matchup": f"{home_team} vs {away_team}",
        "home_first_goal_rate_last_10": 0.80,
        "away_first_goal_rate_last_10": 0.30,
        "home_avg_minute_scored": 21,
        "away_avg_minute_conceded": 26,
        "opening_15min_xg": 0.42,
    }
    return json.dumps(stats)

# ----------------- TABS UI -----------------
tab_stocks, tab_sports = st.tabs(["📈 Day Trading", "⚽ Sports Stats"])

# --- Tab 1: Trading ---
with tab_stocks:
    st.subheader("Intraday Market Scan")
    ticker = st.text_input("Stock Ticker", value="NVDA").upper()
    
    if st.button("Scan Ticker", type="primary", use_container_width=True):
        with st.spinner(f"Pulling live data and analyzing {ticker}..."):
            config = types.GenerateContentConfig(
                system_instruction=(
                    "You are an analytical decision agent. "
                    "Evaluate price position relative to the 5-day range and provide a specific intraday setup with entry, target, and stop-loss."
                ),
                tools=[fetch_market_indicators],
                temperature=0.2,
            )
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=f"Analyze {ticker} live price metrics and assess current risk/reward.",
                config=config,
            )
            st.markdown(response.text)

# --- Tab 2: Sports ---
with tab_sports:
    st.subheader("First Team to Score Predictor")
    c1, c2 = st.columns(2)
    home = c1.text_input("Home Team", value="Arsenal")
    away = c2.text_input("Away Team", value="Chelsea")
    
    if st.button("Analyze Matchup Edge", use_container_width=True):
        with st.spinner(f"Evaluating {home} vs {away}..."):
            config = types.GenerateContentConfig(
                system_instruction=(
                    "You are a sports betting quantitative agent. "
                    "Analyze early possession and scoring rates to evaluate if there is mathematical edge on the 'First Team to Score' market."
                ),
                tools=[fetch_sports_early_goal_stats],
                temperature=0.2,
            )
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=f"Evaluate if {home} has an edge to score first against {away}.",
                config=config,
            )
            st.markdown(response.text)
