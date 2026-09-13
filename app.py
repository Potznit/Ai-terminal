import streamlit as st
import json
import requests
import yfinance as yf
from google import genai
from google.genai import types

st.set_page_config(page_title="AI Edge Terminal", page_icon="⚡", layout="centered")
st.title("⚡ AI Market & Sports Terminal")

# --- SECRETS & KEYS ---
gemini_key = st.secrets.get("GEMINI_API_KEY", "")
odds_api_key = st.secrets.get("ODDS_API_KEY", "")
telegram_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
telegram_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")

with st.sidebar:
    st.header("Configuration")
    if not gemini_key:
        gemini_key = st.text_input("Gemini API Key", type="password")
    if not odds_api_key:
        odds_api_key = st.text_input("The Odds API Key", type="password")
    if not telegram_token:
        telegram_token = st.text_input("Telegram Bot Token", type="password")
    if not telegram_chat_id:
        telegram_chat_id = st.text_input("Telegram Chat ID")

if not gemini_key:
    st.warning("Please configure your Gemini API Key in secrets or the sidebar.")
    st.stop()

client = genai.Client(api_key=gemini_key)

# --- NOTIFICATION SYSTEM ---
def send_telegram_alert(message: str) -> bool:
    if telegram_token and telegram_chat_id:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {"chat_id": telegram_chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=5)
            return True
        except Exception:
            return False
    return False

# --- BOOKMAKER MAPPING ---
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

# --- TOOLS ---
def fetch_market_indicators(ticker: str) -> str:
    """Fetches real-time price, day change, and 5-day range."""
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

def scan_sports_odds(sport_key: str, selected_books_str: str) -> str:
    """Fetches odds filtered by selected bookmakers."""
    if not odds_api_key:
        simulated_feed = [
            {
                "matchup": "Arsenal vs Chelsea",
                "simulated_notice": "No live Odds API key provided. Using benchmark sample.",
                "odds_comparison": {
                    "Pinnacle (Sharp Benchmark)": {"Arsenal": 1.75, "Draw": 3.80, "Chelsea": 4.90},
                    "Betfair": {"Arsenal": 1.95, "Draw": 3.65, "Chelsea": 4.20}
                }
            }
        ]
        return json.dumps(simulated_feed)

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": odds_api_key,
        "regions": "eu,uk,us",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "bookmakers": selected_books_str,
    }
    res = requests.get(url, params=params, timeout=10)
    if res.status_code != 200:
        return json.dumps({"error": f"API returned status {res.status_code}"})
    
    games = res.json()[:4]
    parsed_matches = []
    for g in games:
        parsed_matches.append({
            "matchup": f"{g.get('home_team')} vs {g.get('away_team')}",
            "start_time": g.get("commence_time"),
            "bookmaker_lines": [
                {
                    "bookmaker": b.get("title"),
                    "lines": {o.get("name"): o.get("price") for o in b.get("markets", [{}])[0].get("outcomes", [])}
                }
                for b in g.get("bookmakers", [])
            ]
        })
    return json.dumps(parsed_matches)

# --- UI TABS ---
tab_trading, tab_sports = st.tabs(["📈 Day Trading", "⚽ +EV Sports Scanner"])

# 1. Trading Tab
with tab_trading:
    st.subheader("Intraday Market Scan")
    ticker_input = st.text_input("Stock Ticker", value="NVDA").upper()
    
    if st.button("Scan Ticker", type="primary", use_container_width=True):
        with st.spinner(f"Analyzing technicals for {ticker_input}..."):
            config = types.GenerateContentConfig(
                system_instruction=(
                    "You are an intraday execution specialist. Evaluate current price position "
                    "relative to the 5-day range and provide a trade setup with entry, target, and invalidation stop-loss."
                ),
                tools=[fetch_market_indicators],
                temperature=0.2,
            )
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=f"Analyze {ticker_input} live price metrics.",
                config=config,
            )
            st.markdown(response.text)

# 2. Sports +EV Tab
with tab_sports:
    st.subheader("Expected Value (+EV) Sports Scanner")
    
    col_sport, col_edge = st.columns(2)
    sport = col_sport.selectbox(
        "League",
        ["soccer_epl", "soccer_spain_la_liga", "soccer_uefa_champs_league", "basketball_nba"],
        index=0
    )
    min_edge = col_edge.slider("Minimum Edge (+EV %)", 1.0, 10.0, 3.0, step=0.5)

    chosen_labels = st.multiselect(
        "Select Sportsbooks to Monitor",
        options=list(AVAILABLE_BOOKMAKERS.keys()),
        default=["Betfair (Exchange/Sportsbook)", "Pinnacle (Sharp Benchmark)", "Bet365"]
    )
    
    chosen_keys = [AVAILABLE_BOOKMAKERS[label] for label in chosen_labels]
    selected_books_str = ",".join(chosen_keys)

    notify = st.checkbox("Send Alert to Telegram", value=True)

    if st.button("Run +EV Scan", type="primary", use_container_width=True):
        if not chosen_keys:
            st.error("Please select at least one bookmaker.")
        else:
            with st.spinner("Fetching lines and computing +EV edge..."):
                config = types.GenerateContentConfig(
                    system_instruction=(
                        "You are a quantitative sports betting model. Use Pinnacle as the fair-price benchmark to derive true probability (removing the vig/margin). "
                        "Compare this true probability against other selected bookmakers (e.g., Betfair) to find discrepancies where the payout offers positive Expected Value (+EV).\n"
                        "Format your response with:\n"
                        "- Matchup\n"
                        "- Value Bet (Team / Outcome)\n"
                        "- Bookmaker Offering the Line\n"
                        "- Fair Odds vs Bookmaker Odds\n"
                        "- Calculated EV %"
                    ),
                    tools=[scan_sports_odds],
                    temperature=0.1,
                )
                
                prompt = (
                    f"Scan {sport} using only these bookmakers: {selected_books_str}. "
                    f"Return only bets offering at least {min_edge}% +EV."
                )
                
                analysis = client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=prompt,
                    config=config,
                )
                
                st.markdown(analysis.text)
                
                if notify and telegram_token:
                    sent = send_telegram_alert(f"🚨 *+EV Betting Alert:*\n\n{analysis.text}")
                    if sent:
                        st.success("Alert sent to your Telegram!")
                    else:
                        st.info("Could not send Telegram alert. Check bot token and chat ID.")
