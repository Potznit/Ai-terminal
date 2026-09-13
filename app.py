import streamlit as st
import json
import os
import re
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
telegram_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
telegram_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")

with st.sidebar:
    st.header("⚙️ Settings & Credentials")
    if not gemini_key:
        gemini_key = st.text_input("Gemini API Key", type="password")
    if not odds_api_key:
        odds_api_key = st.text_input("The Odds API Key (Optional for Simulation)", type="password")
    if not telegram_token:
        telegram_token = st.text_input("Telegram Bot Token (Optional)", type="password")
    if not telegram_chat_id:
        telegram_chat_id = st.text_input("Telegram Chat ID (Optional)")

if not gemini_key:
    st.warning("Please configure your Gemini API Key in Streamlit Secrets to proceed.")
    st.stop()

client = genai.Client(api_key=gemini_key)

# --- CSV PAPER PORTFOLIO STORAGE ---
CSV_FILE = "paper_trades.csv"
STARTING_BANKROLL = 1000.0
FIXED_STAKE = 20.0

def load_portfolio() -> pd.DataFrame:
    if os.path.exists(CSV_FILE):
        try:
            return pd.read_csv(CSV_FILE)
        except Exception:
            pass
    return pd.DataFrame(columns=[
        "ID", "League", "Matchup", "Pick", "Bookmaker", "Odds", "EV_Pct", "Stake", "Status", "P_L"
    ])

def save_portfolio(df: pd.DataFrame):
    df.to_csv(CSV_FILE, index=False)

# --- CONSTANTS & MAPPINGS ---
FOOTBALL_LEAGUES = {
    "Premier League (England)": "soccer_epl",
    "La Liga (Spain)": "soccer_spain_la_liga",
    "Serie A (Italy)": "soccer_italy_serie_a",
    "Ligue 1 (France)": "soccer_france_ligue_one",
    "Brasileirão Série A (Brazil)": "soccer_brazil_campeonato",
    "UEFA Champions League": "soccer_uefa_champs_league",
}

AVAILABLE_BOOKMAKERS = {
    "Betfair (Exchange/Sportsbook)": "betfair_ex_uk",
    "Bet365": "bet365",
    "Pinnacle (Sharp Benchmark)": "pinnacle",
    "DraftKings": "draftkings",
    "FanDuel": "fanduel",
    "BetMGM": "betmgm",
    "William Hill": "williamhill",
    "Bovada": "bovada",
}

# --- DIRECT DATA FETCH ---
def get_football_odds_data(sport_key: str, selected_books_str: str) -> str:
    """Directly fetches upcoming odds for Gemini analysis."""
    if not odds_api_key:
        simulated_feeds = {
            "soccer_brazil_campeonato": [
                {
                    "matchup": "Flamengo vs Palmeiras",
                    "pinnacle": {"Flamengo": 2.10, "Draw": 3.25, "Palmeiras": 3.70},
                    "retail": {"bookmaker": "Betfair", "Flamengo": 2.30, "Draw": 3.10, "Palmeiras": 3.40}
                }
            ],
            "soccer_epl": [
                {
                    "matchup": "Arsenal vs Chelsea",
                    "pinnacle": {"Arsenal": 1.80, "Draw": 3.70, "Chelsea": 4.50},
                    "retail": {"bookmaker": "Bet365", "Arsenal": 1.95, "Draw": 3.50, "Chelsea": 4.20}
                },
                {
                    "matchup": "Liverpool vs Everton",
                    "pinnacle": {"Liverpool": 1.35, "Draw": 5.20, "Everton": 8.50},
                    "retail": {"bookmaker": "Betfair", "Liverpool": 1.38, "Draw": 5.00, "Everton": 8.00}
                }
            ]
        }
        return json.dumps(simulated_feeds.get(sport_key, simulated_feeds["soccer_brazil_campeonato"]))

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": odds_api_key,
        "regions": "eu,uk,us",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "bookmakers": selected_books_str,
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        if res.status_code != 200:
            return json.dumps({"error": f"API status {res.status_code}"})
        games = res.json()[:6]
        parsed = []
        for g in games:
            parsed.append({
                "matchup": f"{g.get('home_team')} vs {g.get('away_team')}",
                "bookmakers": [
                    {
                        "bookmaker": b.get("title"),
                        "lines": {o.get("name"): o.get("price") for o in b.get("markets", [{}])[0].get("outcomes", [])}
                    }
                    for b in g.get("bookmakers", [])
                ]
            })
        return json.dumps(parsed)
    except Exception as e:
        return json.dumps({"error": str(e)})

# --- USER INTERFACE ---
tab_auto, tab_portfolio, tab_stocks = st.tabs([
    "🤖 Autonomous Scanner", "📊 Paper Portfolio & Audit", "📈 Day Trading"
])

# ----------------- TAB 1: AUTONOMOUS AGENT -----------------
with tab_auto:
    st.subheader("Autonomous Quantitative Betting Agent")
    st.markdown("Scans lines, calculates no-vig probabilities against Pinnacle, and automatically commits a **$20.00 paper bet** when an edge appears.")

    col1, col2 = st.columns(2)
    selected_league_label = col1.selectbox("Target League", options=list(FOOTBALL_LEAGUES.keys()), index=0)
    sport_key = FOOTBALL_LEAGUES[selected_league_label]

    min_edge_threshold = col2.slider("Minimum +EV Threshold (%)", min_value=1.5, max_value=8.0, value=3.5, step=0.5)

    chosen_labels = st.multiselect(
        "Active Sportsbooks",
        options=list(AVAILABLE_BOOKMAKERS.keys()),
        default=["Betfair (Exchange/Sportsbook)", "Pinnacle (Sharp Benchmark)", "Bet365"]
    )
    chosen_keys = [AVAILABLE_BOOKMAKERS[label] for label in chosen_labels]
    selected_books_str = ",".join(chosen_keys)

    if st.button("🚀 Run Autonomous Decision & Auto-Bet", type="primary", use_container_width=True):
        if not chosen_keys:
            st.error("Select at least one bookmaker.")
        else:
            with st.spinner(f"Pulling odds and calculating +EV opportunities for {selected_league_label}..."):
                odds_payload = get_football_odds_data(sport_key, selected_books_str)

                system_prompt = (
                    "You are an autonomous quantitative football betting model. Evaluate the provided match odds.\n"
                    "RULES:\n"
                    "1. Derive true implied win probabilities from Pinnacle by stripping bookmaker margin.\n"
                    "2. Compare that true probability against retail bookmaker odds.\n"
                    "3. Formula: EV % = (True Probability * Retail Decimal Odds) - 1.\n"
                    "4. CRITERIA:\n"
                    f"   - Only evaluate Home or Away outright winners (exclude draws).\n"
                    f"   - Retail odds must be between 1.45 and 3.20.\n"
                    f"   - Calculated EV % must be >= {min_edge_threshold}%.\n"
                    "5. Output STRICTLY a valid JSON array of objects. Do not include markdown formatting like ```json or any conversational prose.\n"
                    'Format: [{"matchup": "Team A vs Team B", "pick": "Team A", "bookmaker": "Betfair", "odds": 2.30, "ev_pct": 5.2}]\n'
                    "If no qualifying bets are found, return exactly: []"
                )

                try:
                    response = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=f"{system_prompt}\n\nLive Odds Data:\n{odds_payload}",
                        config=types.GenerateContentConfig(temperature=0.1)
                    )
                except Exception as api_err:
                    st.error(f"Gemini API Error: {api_err}")
                    st.stop()

                raw_text = response.text.strip()
                cleaned_json = re.sub(r"^
