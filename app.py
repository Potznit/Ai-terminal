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

# --- DIRECT DATA FETCH (NO TOOL CALLING CONFLICT) ---
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
                    "5. Output STRICTLY a valid JSON array of objects. Do not include markdown ticks like ```json or any conversational prose.\n"
                    'Format: [{"matchup": "Team A vs Team B", "pick": "Team A", "bookmaker": "Betfair", "odds": 2.30, "ev_pct": 5.2}]\n'
                    "If no qualifying bets are found, return exactly: []"
                )

                response = client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=f"{system_prompt}\n\nLive Odds Data:\n{odds_payload}",
                    config=types.GenerateContentConfig(temperature=0.1)
                )

                # Robust JSON parser
                raw_text = response.text.strip()
                cleaned_json = re.sub(r"^```json\s*|^```\s*|```$", "", raw_text, flags=re.MULTILINE).strip()

                try:
                    accepted_bets = json.loads(cleaned_json)
                except Exception:
                    accepted_bets = []

                if not accepted_bets:
                    st.info("Scan complete: No games satisfied your parameters (odds 1.45–3.20 with EV ≥ threshold). No virtual funds committed.")
                else:
                    df = load_portfolio()
                    logged_count = 0

                    st.success(f"Discovered {len(accepted_bets)} eligible +EV setup(s)!")

                    for bet in accepted_bets:
                        is_duplicate = not df[
                            (df["Matchup"] == bet["matchup"]) & (df["Pick"] == bet["pick"]) & (df["Status"] == "PENDING")
                        ].empty

                        if not is_duplicate:
                            new_row = {
                                "ID": len(df) + 1,
                                "League": selected_league_label,
                                "Matchup": bet["matchup"],
                                "Pick": bet["pick"],
                                "Bookmaker": bet.get("bookmaker", "Retail Book"),
                                "Odds": float(bet["odds"]),
                                "EV_Pct": float(bet["ev_pct"]),
                                "Stake": FIXED_STAKE,
                                "Status": "PENDING",
                                "P_L": 0.0
                            }
                            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                            logged_count += 1

                    save_portfolio(df)

                    for bet in accepted_bets:
                        st.markdown(
                            f"🎯 **Auto-Placed Bet:** **{bet['pick']}** to win in *{bet['matchup']}*  \n"
                            f"• **Book:** {bet.get('bookmaker', 'Retail')} @ **{bet['odds']}**  \n"
                            f"• **Edge:** **+{bet['ev_pct']}% EV** | **Stake:** ${FIXED_STAKE:.2f}"
                        )
                    st.toast(f"Saved {logged_count} paper bet(s) to portfolio!")

# ----------------- TAB 2: PORTFOLIO & AUDIT -----------------
with tab_portfolio:
    st.subheader("📊 Paper Trading Performance Ledger")
    df = load_portfolio()

    settled_df = df[df["Status"].isin(["WON", "LOST"])]
    total_pl = settled_df["P_L"].sum() if not settled_df.empty else 0.0
    current_bankroll = STARTING_BANKROLL + total_pl
    total_staked = settled_df["Stake"].sum() if not settled_df.empty else 0.0
    roi = (total_pl / total_staked * 100) if total_staked > 0 else 0.0
    win_count = len(settled_df[settled_df["Status"] == "WON"])
    total_settled = len(settled_df)
    win_rate = (win_count / total_settled * 100) if total_settled > 0 else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Current Bankroll", f"${current_bankroll:.2f}")
    c2.metric("Total P/L", f"${total_pl:+.2f}")
    c3.metric("ROI", f"{roi:+.2f}%")
    c4.metric("Win Rate", f"{win_rate:.1f}% ({win_count}/{total_settled})")

    if df.empty:
        st.info("No paper trades logged yet. Run a scan to initiate your portfolio.")
    else:
        st.dataframe(df, use_container_width=True)

        pending_bets = df[df["Status"] == "PENDING"]
        if not pending_bets.empty:
            st.divider()
            st.subheader("⚖️ Grade Completed Match Results")
            bet_id_to_settle = st.selectbox(
                "Select Completed Match to Settle",
                options=pending_bets["ID"].tolist(),
                format_func=lambda x: f"Bet #{x}: {pending_bets.loc[pending_bets['ID'] == x, 'Pick'].values[0]} ({pending_bets.loc[pending_bets['ID'] == x, 'Matchup'].values[0]})"
            )

            col_w, col_l, col_p = st.columns(3)
            if col_w.button("✅ Won", use_container_width=True):
                idx = df[df["ID"] == bet_id_to_settle].index[0]
                df.at[idx, "Status"] = "WON"
                df.at[idx, "P_L"] = round((df.at[idx, "Odds"] - 1) * df.at[idx, "Stake"], 2)
                save_portfolio(df)
                st.rerun()

            if col_l.button("❌ Lost", use_container_width=True):
                idx = df[df["ID"] == bet_id_to_settle].index[0]
                df.at[idx, "Status"] = "LOST"
                df.at[idx, "P_L"] = -df.at[idx, "Stake"]
                save_portfolio(df)
                st.rerun()

            if col_p.button("🔄 Push / Postponed", use_container_width=True):
                idx = df[df["ID"] == bet_id_to_settle].index[0]
                df.at[idx, "Status"] = "PUSH"
                df.at[idx, "P_L"] = 0.0
                save_portfolio(df)
                st.rerun()

# ----------------- TAB 3: STOCK SCANNER -----------------
with tab_stocks:
    st.subheader("Intraday Market Scan")
    ticker_input = st.text_input("Stock Ticker", value="NVDA").upper()
    if st.button("Scan Ticker", type="primary", use_container_width=True):
        with st.spinner(f"Pulling data for {ticker_input}..."):
            stock = yf.Ticker(ticker_input)
            price = round(stock.fast_info.last_price, 2)
            res = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=f"Analyze {ticker_input} at current price ${price} for an intraday plan with entry, target, and stop.",
            )
            st.markdown(res.text)
