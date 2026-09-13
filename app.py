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
telegram_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
telegram_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")

with st.sidebar:
    st.header("⚙️ Settings & Credentials")
    if not gemini_key:
        gemini_key = st.text_input("Gemini API Key", type="password")
    if not odds_api_key:
        odds_api_key = st.text_input("The Odds API Key", type="password")
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
STAKE_PERCENT = 0.10  # 10% Dynamic allocation

COLUMNS = [
    "ID", "Kickoff_UTC", "League", "Matchup", "Pick", "Bookmaker", "Odds", "EV_Pct", "Stake", "Status", "P_L"
]

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
    df.to_csv(CSV_FILE, index=False)

def get_bankroll_metrics(df: pd.DataFrame):
    settled_df = df[df["Status"].isin(["WON", "LOST", "PUSH"])]
    total_realized_pl = settled_df["P_L"].sum() if not settled_df.empty else 0.0
    pending_stakes = df[df["Status"] == "PENDING"]["Stake"].sum()
    
    available_bankroll = STARTING_BANKROLL + total_realized_pl - pending_stakes
    total_equity = STARTING_BANKROLL + total_realized_pl
    
    total_staked_settled = settled_df[settled_df["Status"].isin(["WON", "LOST"])]["Stake"].sum()
    roi = (total_realized_pl / total_staked_settled * 100) if total_staked_settled > 0 else 0.0
    
    win_count = len(settled_df[settled_df["Status"] == "WON"])
    total_decided = len(settled_df[settled_df["Status"].isin(["WON", "LOST"])])
    win_rate = (win_count / total_decided * 100) if total_decided > 0 else 0.0

    return {
        "available_bankroll": max(0.0, available_bankroll),
        "total_equity": total_equity,
        "total_pl": total_realized_pl,
        "roi": roi,
        "win_rate": win_rate,
        "win_count": win_count,
        "total_decided": total_decided,
        "pending_stakes": pending_stakes
    }

# --- QUANTITATIVE LEAGUE TIERS ---
MAJOR_LEAGUES = {
    "Premier League (England)": "soccer_epl",
    "Championship (England 2nd)": "soccer_england_efl_cup",
    "La Liga (Spain)": "soccer_spain_la_liga",
    "Segunda División (Spain 2nd)": "soccer_spain_segunda_division",
    "Serie A (Italy)": "soccer_italy_serie_a",
    "Serie B (Italy 2nd)": "soccer_italy_serie_b",
    "Bundesliga (Germany)": "soccer_germany_bundesliga",
    "2. Bundesliga (Germany 2nd)": "soccer_germany_bundesliga2",
    "Ligue 1 (France)": "soccer_france_ligue_one",
    "Ligue 2 (France 2nd)": "soccer_france_ligue_two",
    "Brasileirão Série A (Brazil)": "soccer_brazil_campeonato",
    "UEFA Champions League": "soccer_uefa_champs_league",
    "UEFA Europa League": "soccer_uefa_europa_league"
}

MEDIUM_LEAGUES = {
    "Eredivisie (Netherlands)": "soccer_netherlands_eredivisie",
    "Primeira Liga (Portugal)": "soccer_portugal_primeira_liga",
    "Pro League (Belgium)": "soccer_belgium_first_div",
    "Süper Lig (Turkey)": "soccer_turkey_super_league",
    "Premiership (Scotland)": "soccer_spl",
    "Major League Soccer (USA)": "soccer_usa_mls",
    "Liga MX (Mexico)": "soccer_mexico_ligamx",
    "Primera División (Argentina)": "soccer_argentina_primera_division",
    "J1 League (Japan)": "soccer_japan_j_league",
    "Copa Libertadores": "soccer_conmebol_copa_libertadores"
}

MINOR_LEAGUES = {
    "League One (England 3rd)": "soccer_england_league1",
    "League Two (England 4th)": "soccer_england_league2",
    "Bundesliga (Austria)": "soccer_austria_bundesliga",
    "Super League (Switzerland)": "soccer_switzerland_superleague",
    "Superliga (Denmark)": "soccer_denmark_superliga",
    "Ekstraklasa (Poland)": "soccer_poland_ekstraklasa",
    "Allsvenskan (Sweden)": "soccer_sweden_allsvenskan",
    "Eliteserien (Norway)": "soccer_norway_eliteserien",
    "A-League (Australia)": "soccer_australia_aleague"
}

ALL_LEAGUES_MAP = {**MAJOR_LEAGUES, **MEDIUM_LEAGUES, **MINOR_LEAGUES}

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

# --- DIRECT DATA FETCH ACROSS LEAGUES ---
def fetch_odds_for_leagues(sport_keys: list, selected_books_str: str) -> str:
    if not odds_api_key:
        simulated = [
            {
                "matchup": "Arsenal vs Chelsea",
                "commence_time": "2026-09-19T14:00:00Z",
                "league": "soccer_epl",
                "bookmakers": [
                    {"bookmaker": "Pinnacle", "lines": {"Arsenal": 1.80, "Draw": 3.70, "Chelsea": 4.50}},
                    {"bookmaker": "Bet365", "lines": {"Arsenal": 1.95, "Draw": 3.50, "Chelsea": 4.20}}
                ]
            }
        ]
        return json.dumps(simulated)

    all_matches = []
    for key in sport_keys:
        url = f"https://api.the-odds-api.com/v4/sports/{key}/odds/"
        params = {
            "apiKey": odds_api_key,
            "regions": "eu,uk,us",
            "markets": "h2h",
            "oddsFormat": "decimal",
            "bookmakers": selected_books_str,
        }
        try:
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                for g in res.json()[:6]:
                    all_matches.append({
                        "matchup": f"{g.get('home_team')} vs {g.get('away_team')}",
                        "commence_time": g.get("commence_time", "Unknown"),
                        "league": key,
                        "bookmakers": [
                            {
                                "bookmaker": b.get("title"),
                                "lines": {o.get("name"): o.get("price") for o in b.get("markets", [{}])[0].get("outcomes", [])}
                            }
                            for b in g.get("bookmakers", [])
                        ]
                    })
        except Exception:
            continue
    return json.dumps(all_matches)

# --- SCORE CHECKER ENGINE ---
def auto_settle_completed_bets(df: pd.DataFrame, api_key: str):
    pending_mask = df["Status"] == "PENDING"
    if not pending_mask.any() or not api_key:
        return df, 0

    settled_count = 0
    unique_leagues = df.loc[pending_mask, "League"].unique()
    
    for league in unique_leagues:
        sport_key = league if league in ALL_LEAGUES_MAP.values() else ALL_LEAGUES_MAP.get(league, "soccer_epl")
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/?apiKey={api_key}&daysFrom=3"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            scores_data = res.json()
            
            for item in scores_data:
                if not item.get("completed"):
                    continue
                
                home = item.get("home_team")
                away = item.get("away_team")
                scores = item.get("scores")
                if not scores or len(scores) < 2:
                    continue
                
                home_score = next((int(s["score"]) for s in scores if s["name"] == home), None)
                away_score = next((int(s["score"]) for s in scores if s["name"] == away), None)
                if home_score is None or away_score is None:
                    continue
                
                if home_score > away_score:
                    winning_team = home
                elif away_score > home_score:
                    winning_team = away
                else:
                    winning_team = "DRAW"

                for idx in df[pending_mask].index:
                    m_str = str(df.at[idx, "Matchup"])
                    if home in m_str and away in m_str:
                        pick = str(df.at[idx, "Pick"])
                        stake = float(df.at[idx, "Stake"])
                        odds = float(df.at[idx, "Odds"])
                        
                        if winning_team == "DRAW":
                            df.at[idx, "Status"] = "LOST"
                            df.at[idx, "P_L"] = -stake
                            settled_count += 1
                        elif pick.strip().lower() in winning_team.lower() or winning_team.lower() in pick.strip().lower():
                            df.at[idx, "Status"] = "WON"
                            df.at[idx, "P_L"] = round((odds - 1.0) * stake, 2)
                            settled_count += 1
                        else:
                            df.at[idx, "Status"] = "LOST"
                            df.at[idx, "P_L"] = -stake
                            settled_count += 1
        except Exception:
            continue

    if settled_count > 0:
        save_portfolio(df)
    return df, settled_count

# --- USER INTERFACE ---
tab_auto, tab_portfolio, tab_stocks = st.tabs([
    "🤖 Autonomous Scanner", "📊 Paper Portfolio & Audit", "📈 Day Trading"
])

# ----------------- TAB 1: AUTONOMOUS AGENT -----------------
with tab_auto:
    st.subheader("Tier-Segmented Autonomous Quantitative Scanner")
    df_current = load_portfolio()
    metrics = get_bankroll_metrics(df_current)
    
    current_dynamic_stake = round(metrics["available_bankroll"] * STAKE_PERCENT, 2)
    st.info(f"💰 Available Bankroll: **${metrics['available_bankroll']:.2f}** | Next 10% Stake: **${current_dynamic_stake:.2f}** | Active at Risk: **${metrics['pending_stakes']:.2f}**")

    col1, col2 = st.columns(2)

    tier_scope = col1.radio(
        "Market Tier / Scope",
        options=[
            "🏆 Major Leagues (Tier 1 & 2nd Divisions)",
            "🥈 Medium Leagues (Competitive Domestic)",
            "🥉 Minor Leagues (Lower Divisions & Regional)",
            "🎯 Single Specific League"
        ],
        index=0
    )

    if tier_scope == "🏆 Major Leagues (Tier 1 & 2nd Divisions)":
        target_keys = list(MAJOR_LEAGUES.values())
        scan_title = f"Major Leagues ({len(target_keys)} competitions)"
    elif tier_scope == "🥈 Medium Leagues (Competitive Domestic)":
        target_keys = list(MEDIUM_LEAGUES.values())
        scan_title = f"Medium Leagues ({len(target_keys)} competitions)"
    elif tier_scope == "🥉 Minor Leagues (Lower Divisions & Regional)":
        target_keys = list(MINOR_LEAGUES.values())
        scan_title = f"Minor Leagues ({len(target_keys)} competitions)"
    else:
        chosen_league = col1.selectbox("Select Competition", options=list(ALL_LEAGUES_MAP.keys()))
        target_keys = [ALL_LEAGUES_MAP[chosen_league]]
        scan_title = chosen_league

    min_edge_threshold = col2.slider("Minimum +EV Threshold (%)", min_value=1.0, max_value=8.0, value=3.0, step=0.5)

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
        elif current_dynamic_stake <= 1.0:
            st.error("Available bankroll depleted. Settle existing matches before taking new bets.")
        else:
            with st.spinner(f"Querying fixtures and calculating +EV opportunities for {scan_title}..."):
                odds_payload = fetch_odds_for_leagues(target_keys, selected_books_str)

                system_prompt = (
                    "You are an autonomous quantitative football betting model. Evaluate the provided match odds.\n"
                    "RULES:\n"
                    "1. Derive true implied win probabilities from Pinnacle by stripping bookmaker margin (vig).\n"
                    "2. Compare that true probability against retail bookmaker odds.\n"
                    "3. Formula: EV % = (True Probability * Retail Decimal Odds) - 1.\n"
                    "4. CRITERIA:\n"
                    "   - Only evaluate Home or Away outright winners (exclude draws).\n"
                    "   - Retail odds must be between 1.45 and 3.20.\n"
                    f"   - Calculated EV % must be >= {min_edge_threshold}%.\n"
                    "5. Output STRICTLY a valid JSON array of objects. Do not include markdown formatting like ```json or conversational text.\n"
                    'Format: [{"matchup": "Team A vs Team B", "kickoff": "YYYY-MM-DD HH:MM", "league": "League/Key", "pick": "Team A", "bookmaker": "Betfair", "odds": 2.30, "ev_pct": 5.2}]\n'
                    "If no qualifying bets are found, return exactly: []"
                )

                try:
                    response = client.models.generate_content(
                        model="gemini-3.6-flash",
                        contents=f"{system_prompt}\n\nLive Odds Data:\n{odds_payload}",
                        config=types.GenerateContentConfig(temperature=0.1)
                    )
                except Exception as api_err:
                    st.error(f"Gemini API Error: {api_err}")
                    st.stop()

                raw_text = response.text.strip()
                if raw_text.startswith("```"):
                    lines = raw_text.splitlines()
                    if lines and lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    raw_text = "\n".join(lines).strip()

                try:
                    accepted_bets = json.loads(raw_text)
                except Exception:
                    accepted_bets = []

                if not accepted_bets:
                    st.info(f"Scan complete across {scan_title}: No fixtures met your parameters (odds 1.45–3.20, EV ≥ {min_edge_threshold}%). Bankroll preserved.")
                else:
                    df = load_portfolio()
                    logged_count = 0

                    st.success(f"Discovered {len(accepted_bets)} eligible +EV opportunity(ies) across {scan_title}!")

                    for bet in accepted_bets:
                        is_duplicate = not df[
                            (df["Matchup"] == bet.get("matchup")) & (df["Pick"] == bet.get("pick")) & (df["Status"] == "PENDING")
                        ].empty

                        if not is_duplicate:
                            metrics_now = get_bankroll_metrics(df)
                            calculated_stake = round(metrics_now["available_bankroll"] * STAKE_PERCENT, 2)
                            if calculated_stake < 1.0:
                                break

                            kickoff_str = bet.get("kickoff", bet.get("commence_time", "Scheduled"))
                            # Format ISO timestamps cleanly if present
                            if "T" in str(kickoff_str):
                                kickoff_str = kickoff_str.replace("T", " ").replace("Z", " UTC")

                            new_row = {
                                "ID": len(df) + 1,
                                "Kickoff_UTC": kickoff_str,
                                "League": bet.get("league", scan_title),
                                "Matchup": bet.get("matchup"),
                                "Pick": bet.get("pick"),
                                "Bookmaker": bet.get("bookmaker", "Retail Book"),
                                "Odds": float(bet.get("odds", 0.0)),
                                "EV_Pct": float(bet.get("ev_pct", 0.0)),
                                "Stake": calculated_stake,
                                "Status": "PENDING",
                                "P_L": 0.0
                            }
                            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                            logged_count += 1

                    save_portfolio(df)

                    for bet in accepted_bets:
                        st.markdown(
                            f"🎯 **Auto-Placed Bet:** **{bet.get('pick')}** to win in *{bet.get('matchup')}* ({bet.get('league')})  \n"
                            f"• **Kickoff:** `{bet.get('kickoff', 'TBD')}`  \n"
                            f"• **Book:** {bet.get('bookmaker', 'Retail')} @ **{bet.get('odds')}** | **Edge:** **+{bet.get('ev_pct')}% EV**  \n"
                            f"• **Stake (10%):** ${current_dynamic_stake:.2f}"
                        )
                    st.toast(f"Logged {logged_count} paper bet(s) to portfolio!")

# ----------------- TAB 2: PORTFOLIO & AUDIT -----------------
with tab_portfolio:
    st.subheader("📊 Paper Trading Performance Ledger")
    df = load_portfolio()
    metrics = get_bankroll_metrics(df)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Available Bankroll", f"${metrics['available_bankroll']:.2f}", help="Cash remaining to bet (Starting Bankroll + Realized P/L - Active Stakes)")
    c2.metric("Total Equity", f"${metrics['total_equity']:.2f}", help="Starting Bankroll + Settled P/L")
    c3.metric("ROI", f"{metrics['roi']:+.2f}%")
    c4.metric("Win Rate", f"{metrics['win_rate']:.1f}% ({metrics['win_count']}/{metrics['total_decided']})")

    # CONTROLS
    col_auto, col_reset = st.columns([3, 1])
    if col_auto.button("⚡ Auto-Check Scores & Grade Results", type="primary", use_container_width=True):
        with st.spinner("Fetching completed scores from The Odds API..."):
            df, count = auto_settle_completed_bets(df, odds_api_key)
            if count > 0:
                st.success(f"Graded {count} completed match(es)!")
                st.rerun()
            else:
                st.info("No newly completed matches found yet. Check game dates below.")

    if col_reset.button("🗑️ Reset Ledger to $1,000", use_container_width=True):
        fresh_df = pd.DataFrame(columns=COLUMNS)
        save_portfolio(fresh_df)
        st.success("Ledger reset to $0.00 P/L and $1,000.00 bankroll!")
        st.rerun()

    if df.empty:
        st.info("Ledger is completely empty ($1,000.00 available). Run an autonomous scan to begin forward testing.")
    else:
        st.dataframe(df, use_container_width=True)

        pending_bets = df[df["Status"] == "PENDING"]
        if not pending_bets.empty:
            st.divider()
            st.subheader("⚖️ Manual Settlement Fallback")
            bet_id_to_settle = st.selectbox(
                "Select Match to Manually Settle",
                options=pending_bets["ID"].tolist(),
                format_func=lambda x: f"Bet #{x}: {pending_bets.loc[pending_bets['ID'] == x, 'Pick'].values[0]} ({pending_bets.loc[pending_bets['ID'] == x, 'Matchup'].values[0]}) — Kickoff: {pending_bets.loc[pending_bets['ID'] == x, 'Kickoff_UTC'].values[0]}"
            )

            col_w, col_l, col_p = st.columns(3)
            if col_w.button("✅ Won", use_container_width=True):
                idx = df[df["ID"] == bet_id_to_settle].index[0]
                df.at[idx, "Status"] = "WON"
                df.at[idx, "P_L"] = round((df.at[idx, "Odds"] - 1.0) * df.at[idx, "Stake"], 2)
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
            try:
                res = client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=f"Analyze {ticker_input} at current price ${price} for an intraday plan with entry, target, and stop.",
                )
                st.markdown(res.text)
            except Exception as e:
                st.error(f"Error fetching analysis: {e}")
