import streamlit as st
import json
import os
import requests
import pandas as pd
import yfinance as yf
from google import genai
from google.genai import types

st.set_page_config(page_title="Autonomous AI Betting Terminal", page_icon="⚡", layout="wide")
st.title("⚡ Autonomous AI Global Betting Terminal")

# --- SECRETS & CONFIGURATION ---
gemini_key = st.secrets.get("GEMINI_API_KEY", "")
odds_api_key = st.secrets.get("ODDS_API_KEY", "")
telegram_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "8956869998:AAH9SEXc6qID3Ie1JDx3mffb8pHLVWxMgoE")
telegram_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "6565714528")

CSV_FILE = "paper_trades.csv"
STARTING_BANKROLL = 1000.0
MIN_EDGE_THRESHOLD = 1.5

COLUMNS = [
    "ID", "Kickoff_UTC", "League", "Matchup", "Market", "Pick", "Bookmaker", "Odds", "EV_Pct", "Stake", "Status", "P_L"
]

def send_telegram_alert(message_html: str):
    token = st.session_state.get("tg_token", telegram_token).strip()
    chat_id = str(st.session_state.get("tg_chat_id", telegram_chat_id)).strip()
    if not token or not chat_id:
        return False, "Token or Chat ID is empty."
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message_html, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=8)
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            return True, "Delivered"
        return False, f"Telegram Error ({r.status_code}): {data.get('description', r.text)}"
    except Exception as e:
        return False, f"Connection exception: {str(e)}"

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
        success, detail = send_telegram_alert("⚡ <b>AI Betting Terminal:</b> Telegram webhook connection verified successfully!")
        if success:
            st.success("Test ping sent to your Telegram!")
        else:
            st.error(f"Failed: {detail}")

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
                    if col == "Market":
                        df[col] = "h2h"
                    elif col == "Kickoff_UTC":
                        df[col] = "N/A"
                    else:
                        df[col] = 0.0
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

def calculate_kelly_stake(bankroll: float, decimal_odds: float, ev_pct: float) -> float:
    if bankroll <= 1.0 or decimal_odds <= 1.01:
        return 0.0
    b = decimal_odds - 1.0
    true_p = (1.0 + (ev_pct / 100.0)) / decimal_odds
    q = 1.0 - true_p
    full_kelly = (b * true_p - q) / b
    quarter_kelly = max(0.0, full_kelly * 0.25)
    fraction = min(max(quarter_kelly, 0.01), 0.05)
    stake = round(bankroll * fraction, 2)
    return max(1.0, stake)

def build_odds_bracket_matrix(df: pd.DataFrame) -> pd.DataFrame:
    settled = df[df["Status"].isin(["WON", "LOST", "PUSH"])].copy()
    brackets = [
        ("Conservative (1.40 - 1.75)", 1.40, 1.75),
        ("Balanced (1.76 - 2.20)", 1.76, 2.20),
        ("Mild Underdogs (2.21 - 2.80)", 2.21, 2.80),
        ("Bold / Longshots (2.81 - 3.80)", 2.81, 3.80)
    ]
    rows = []
    for label, low, high in brackets:
        b_df = settled[(settled["Odds"] >= low) & (settled["Odds"] <= high)]
        decided = b_df[b_df["Status"].isin(["WON", "LOST"])]
        total_bets = len(b_df)
        total_decided = len(decided)
        won_count = len(decided[decided["Status"] == "WON"])
        win_rate = (won_count / total_decided * 100) if total_decided > 0 else 0.0
        expected_win_rate = ((1.0 / b_df["Odds"]).mean() * 100) if total_bets > 0 else 0.0
        avg_ev = b_df["EV_Pct"].mean() if total_bets > 0 else 0.0
        pl = b_df["P_L"].sum() if total_bets > 0 else 0.0
        total_staked = decided["Stake"].sum() if total_decided > 0 else 0.0
        roi = (pl / total_staked * 100) if total_staked > 0 else 0.0
        rows.append({
            "Bracket": label,
            "Bets": total_bets,
            "Record (W-L)": f"{won_count}-{total_decided - won_count}",
            "Actual Win Rate": f"{win_rate:.1f}%",
            "Expected Win Rate": f"{expected_win_rate:.1f}%",
            "Avg EV Edge": f"+{avg_ev:.2f}%",
            "Realized P/L": f"${pl:+.2f}",
            "ROI": f"{roi:+.2f}%"
        })
    return pd.DataFrame(rows)

def get_all_active_soccer_leagues() -> list:
    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={odds_api_key}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return [s["key"] for s in r.json() if s.get("key", "").startswith("soccer_") and s.get("active", False)]
    except Exception:
        pass
    return ["soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga"]

def fetch_global_multi_market_odds(leagues: list):
    all_matches = []
    scanned_league_names = []
    selected_books = "pinnacle,betfair_ex_uk,bet365"
    for key in leagues:
        url = f"https://api.the-odds-api.com/v4/sports/{key}/odds/"
        params = {
            "apiKey": odds_api_key,
            "regions": "eu,uk,us",
            "markets": "h2h,totals,spreads",
            "oddsFormat": "decimal",
            "bookmakers": selected_books,
        }
        try:
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                fixtures = res.json()
                if fixtures:
                    scanned_league_names.append(key.replace("soccer_", ""))
                for g in fixtures[:6]:
                    bookmakers_data = []
                    for b in g.get("bookmakers", []):
                        markets_dict = {}
                        for mkt in b.get("markets", []):
                            m_key = mkt.get("key")
                            outcomes = {}
                            for o in mkt.get("outcomes", []):
                                name = o.get("name")
                                point = o.get("point")
                                label = f"{name} {point}" if point is not None else name
                                outcomes[label] = o.get("price")
                            markets_dict[m_key] = outcomes
                        bookmakers_data.append({"bookmaker": b.get("title"), "markets": markets_dict})

                    all_matches.append({
                        "matchup": f"{g.get('home_team')} vs {g.get('away_team')}",
                        "commence_time": g.get("commence_time", "Unknown"),
                        "league": key,
                        "bookmakers": bookmakers_data
                    })
        except Exception:
            continue
    return json.dumps(all_matches), scanned_league_names, len(all_matches)

def auto_settle_completed_bets(df: pd.DataFrame, api_key: str):
    pending_mask = df["Status"] == "PENDING"
    if not pending_mask.any() or not api_key:
        return df, 0, []

    settled_count = 0
    settled_details = []
    unique_leagues = df.loc[pending_mask, "League"].unique()

    for sport_key in unique_leagues:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/?apiKey={api_key}&daysFrom=3"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            for item in res.json():
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

                total_goals = home_score + away_score
                h2h_winner = home if home_score > away_score else (away if away_score > home_score else "DRAW")

                for idx in df[pending_mask].index:
                    m_str = str(df.at[idx, "Matchup"])
                    if home.lower() in m_str.lower() and away.lower() in m_str.lower():
                        pick = str(df.at[idx, "Pick"]).strip()
                        stake = float(df.at[idx, "Stake"])
                        odds = float(df.at[idx, "Odds"])
                        market = str(df.at[idx, "Market"]).lower()
                        status = "LOST"
                        gain = -stake

                        if market == "totals":
                            parts = pick.split()
                            if len(parts) >= 2:
                                direction = parts[0].lower()
                                line = float(parts[1])
                                if total_goals == line:
                                    status = "PUSH"
                                    gain = 0.0
                                elif (direction == "over" and total_goals > line) or (direction == "under" and total_goals < line):
                                    status = "WON"
                                    gain = round((odds - 1.0) * stake, 2)
                        elif market == "spreads":
                            parts = pick.rsplit(" ", 1)
                            if len(parts) == 2:
                                team_name = parts[0]
                                handicap = float(parts[1])
                                is_home = home.lower() in team_name.lower()
                                diff = (home_score - away_score) if is_home else (away_score - home_score)
                                if diff + handicap > 0:
                                    status = "WON"
                                    gain = round((odds - 1.0) * stake, 2)
                                elif diff + handicap == 0:
                                    status = "PUSH"
                                    gain = 0.0
                        else:
                            if h2h_winner == "DRAW":
                                status = "LOST"
                            elif pick.lower() in h2h_winner.lower() or h2h_winner.lower() in pick.lower():
                                status = "WON"
                                gain = round((odds - 1.0) * stake, 2)

                        df.at[idx, "Status"] = status
                        df.at[idx, "P_L"] = gain
                        tag = f"WON ✅ (+${gain:.2f})" if status == "WON" else ("PUSH 🔄" if status == "PUSH" else "LOST ❌")
                        settled_details.append(f"• [{market.upper()}] <b>{pick}</b> ({m_str}): {tag}")
                        settled_count += 1
        except Exception:
            continue

    if settled_count > 0:
        save_portfolio(df)
    return df, settled_count, settled_details

# --- UI TABS ---
tab_auto, tab_portfolio, tab_stocks = st.tabs([
    "🤖 Global Autonomous Scanner", "📊 Paper Portfolio & Audit", "📈 Day Trading"
])

# ----------------- TAB 1: SCANNER -----------------
with tab_auto:
    st.subheader("Global Quantitative Football Scanner (All Leagues & All Markets)")
    df_current = load_portfolio()
    metrics = get_bankroll_metrics(df_current)

    st.markdown(
        f"""
        <div style="background-color: #1e293b; padding: 12px 18px; border-radius: 8px; margin-bottom: 15px; border: 1px solid #334155;">
            💰 <b>Available Bankroll:</b> ${metrics['available_bankroll']:.2f} &nbsp;|&nbsp; 
            💼 <b>Total Equity:</b> ${metrics['total_equity']:.2f} &nbsp;|&nbsp; 
            ⏳ <b>Active at Risk:</b> ${metrics['pending_stakes']:.2f}
        </div>
        """,
        unsafe_allow_html=True
    )

    if st.button("🚀 Scan All In-Season Leagues Globally (Winner, Totals, Spreads)", type="primary", use_container_width=True):
        if metrics["available_bankroll"] < 10.0:
            st.error("Bankroll depleted. Settle existing matches before taking new bets.")
        else:
            with st.spinner("Discovering active leagues worldwide and fetching multi-market odds lines..."):
                active_leagues = get_all_active_soccer_leagues()
                odds_payload, scanned_names, total_matches = fetch_global_multi_market_odds(active_leagues)

                system_prompt = (
                    "You are an autonomous quantitative football betting model evaluating global soccer odds across multiple betting markets simultaneously.\n"
                    "MARKETS PROVIDED: 'h2h' (Match Winner), 'totals' (Over/Under Goals), 'spreads' (Handicap).\n"
                    "MATHEMATICAL INSTRUCTIONS:\n"
                    "1. For each match and market, locate Pinnacle lines as the sharp market-maker benchmark and derive true no-vig probabilities.\n"
                    "2. If Pinnacle does not quote the match, skip it.\n"
                    "3. Compare true probability against retail bookmakers (Betfair, Bet365).\n"
                    "4. Formula: EV % = (True Probability * Retail Decimal Odds) - 1.\n"
                    "5. CRITERIA:\n"
                    f"   - Calculated EV % must be >= {MIN_EDGE_THRESHOLD}%.\n"
                    "   - Retail odds must be between 1.40 and 3.80.\n"
                    "   - Exclude match winner Draw (evaluate Home/Away winner, Over/Under goals, or Team Spreads).\n"
                    "6. Output STRICTLY a valid JSON array of objects without Markdown code fences:\n"
                    '[{"matchup":"Team A vs Team B","kickoff":"YYYY-MM-DD HH:MM","league":"soccer_epl","market":"totals","pick":"Over 2.5","bookmaker":"Bet365","odds":1.95,"ev_pct":2.1}]\n'
                    "If no bets qualify, return exactly: []"
                )

                try:
                    response = client.models.generate_content(
                        model="gemini-3.6-flash",
                        contents=f"{system_prompt}\n\nLive Global Odds Data:\n{odds_payload}"
                    )
                except Exception as api_err:
                    st.error(f"Gemini API Error: {api_err}")
                    st.stop()

                clean_text = response.text.strip()
                fence = chr(96) * 3
                if clean_text.startswith(fence):
                    clean_text = clean_text.lstrip(fence)
                    if clean_text.startswith("json"):
                        clean_text = clean_text[4:]
                if clean_text.endswith(fence):
                    clean_text = clean_text.rstrip(fence)
                clean_text = clean_text.strip()

                try:
                    accepted_bets = json.loads(clean_text)
                except Exception:
                    accepted_bets = []

                df = load_portfolio()
                logged = 0

                for bet in accepted_bets:
                    m_clean = str(bet.get("matchup", "")).strip().lower()
                    p_clean = str(bet.get("pick", "")).strip().lower()
                    existing_pending = df[df["Status"] == "PENDING"]
                    is_dup = any(
                        str(r["Matchup"]).strip().lower() == m_clean and str(r["Pick"]).strip().lower() == p_clean 
                        for _, r in existing_pending.iterrows()
                    )

                    if not is_dup:
                        curr_b = get_bankroll_metrics(df)["available_bankroll"]
                        b_odds = float(bet.get("odds", 0.0))
                        b_ev = float(bet.get("ev_pct", 0.0))
                        stake = calculate_kelly_stake(curr_b, b_odds, b_ev)
                        if stake < 1.0:
                            continue

                        k_str = str(bet.get("kickoff", "Scheduled")).replace("T", " ").replace("Z", " UTC")
                        new_row = {
                            "ID": len(df) + 1,
                            "Kickoff_UTC": k_str,
                            "League": bet.get("league", "Global Soccer"),
                            "Matchup": bet.get("matchup"),
                            "Market": bet.get("market", "h2h"),
                            "Pick": bet.get("pick"),
                            "Bookmaker": bet.get("bookmaker", "Retail Book"),
                            "Odds": b_odds,
                            "EV_Pct": b_ev,
                            "Stake": stake,
                            "Status": "PENDING",
                            "P_L": 0.0
                        }
                        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                        logged += 1

                        tg_msg = (
                            f"🎯 <b>NEW +EV TRADE COMMITTED</b>\n\n"
                            f"⚽ <b>Match:</b> {bet.get('matchup')}\n"
                            f"📊 <b>Market:</b> {bet.get('market', 'H2H').upper()}\n"
                            f"✅ <b>Pick:</b> <code>{bet.get('pick')}</code>\n"
                            f"📈 <b>Odds:</b> {b_odds} ({bet.get('bookmaker')})\n"
                            f"🔥 <b>Edge:</b> +{b_ev}% EV\n"
                            f"💵 <b>Quarter-Kelly Stake:</b> ${stake:.2f}\n"
                            f"⏰ <b>Kickoff:</b> {k_str}"
                        )
                        send_telegram_alert(tg_msg)

                # Send explicit scan audit directly to Telegram on every manual scan
                sample_leagues = ", ".join(scanned_names[:5])
                scan_audit_tg = (
                    f"📡 <b>MANUAL SCAN AUDIT REPORT</b>\n\n"
                    f"🌍 <b>Leagues Checked:</b> {len(scanned_names)} ({sample_leagues}...)\n"
                    f"⚽ <b>Fixtures Evaluated:</b> {total_matches} matches\n"
                    f"🎯 <b>Markets:</b> Match Winner, Over/Under Goals, Spreads\n"
                    f"⚡ <b>New Qualified Trades:</b> {logged}"
                )
                send_telegram_alert(scan_audit_tg)

                if logged > 0:
                    save_portfolio(df)
                    st.success(f"Discovered and logged {logged} new position(s) across {len(scanned_names)} active leagues!")
                    st.rerun()
                else:
                    st.info(f"Scan complete across {len(scanned_names)} active leagues ({total_matches} fixtures checked). No edges $\\ge {MIN_EDGE_THRESHOLD}\\%$ EV were unhedged right now. Audit sent to Telegram.")

# ----------------- TAB 2: PORTFOLIO & AUDIT -----------------
with tab_portfolio:
    st.subheader("📊 Paper Trading Performance Ledger & Equity Curve")
    df = load_portfolio()
    metrics = get_bankroll_metrics(df)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Available Bankroll", f"${metrics['available_bankroll']:.2f}")
    c2.metric("Total Equity", f"${metrics['total_equity']:.2f}")
    c3.metric("ROI", f"{metrics['roi']:+.2f}%")
    c4.metric("Win Rate", f"{metrics['win_rate']:.1f}% ({metrics['win_count']}/{metrics['total_decided']})")

    settled_history = df[df["Status"].isin(["WON", "LOST", "PUSH"])].copy()
    if not settled_history.empty:
        settled_history["Cumulative_Equity"] = STARTING_BANKROLL + settled_history["P_L"].cumsum()
        st.line_chart(settled_history["Cumulative_Equity"], use_container_width=True)

    # --- ODDS CALIBRATION AUDIT TABLE ---
    st.divider()
    st.markdown("### 🎯 Odds Range Calibration Audit")
    st.caption("Segments settled bets by risk profile to verify mathematical consistency across conservative vs. bold odds brackets.")
    bracket_matrix = build_odds_bracket_matrix(df)
    st.dataframe(bracket_matrix, use_container_width=True, hide_index=True)

    st.divider()
    col_auto, col_reset = st.columns([3, 1])
    if col_auto.button("⚡ Auto-Check Scores & Grade All Markets", type="primary", use_container_width=True):
        with st.spinner("Fetching latest fixture scores..."):
            df, count, details = auto_settle_completed_bets(df, odds_api_key)
            if count > 0:
                st.success(f"Graded {count} completed position(s)!")
                st.rerun()
            else:
                st.info("No newly settled matches found.")

    if col_reset.button("🗑️ Reset Ledger to $1,000", use_container_width=True):
        fresh_df = pd.DataFrame(columns=COLUMNS)
        save_portfolio(fresh_df)
        st.success("Ledger reset successfully!")
        st.rerun()

    st.markdown("### 📋 Trade Execution Ledger")
    st.dataframe(df, use_container_width=True)

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
