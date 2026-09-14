import streamlit as st
import json
import os
import requests
import pandas as pd
from datetime import datetime, timezone
import yfinance as yf
from google import genai
from google.genai import types

st.set_page_config(page_title="Autonomous AI Betting Terminal", page_icon="⚡", layout="wide")
st.title("⚡ Autonomous AI Multi-Horizon Betting Terminal")

# --- CREDENTIALS & SECRETS ---
gemini_key = st.secrets.get("GEMINI_API_KEY", "")
odds_api_key = st.secrets.get("ODDS_API_KEY", "")
telegram_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "8956869998:AAH9SEXc6qID3Ie1JDx3mffb8pHLVWxMgoE")
telegram_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "6565714528")

CSV_FILE = "paper_trades.csv"
STARTING_BANKROLL_PER_MODEL = 1000.0
MIN_EDGE_THRESHOLD = 1.5

MODELS_CONFIG = {
    "EARLY_BIRD": {"name": "Early-Bird (48h - 6d)", "max_stake_pct": 0.025, "color": "#f59e0b"},
    "CORE_EV": {"name": "Core Arbitrage (24h - 48h)", "max_stake_pct": 0.050, "color": "#10b981"},
    "LATE_STEAM": {"name": "Late-Steam (15m - 24h)", "max_stake_pct": 0.040, "color": "#3b82f6"}
}

COLUMNS = [
    "ID", "Model_Tag", "Kickoff_UTC", "League", "Matchup", "Market", "Pick", 
    "Bookmaker", "Odds", "EV_Pct", "Stake", "Status", "P_L"
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
        return (True, "Delivered") if r.status_code == 200 and data.get("ok") else (False, data.get("description", r.text))
    except Exception as e:
        return False, str(e)

with st.sidebar:
    st.header("⚙️ Credentials")
    if not gemini_key:
        gemini_key = st.text_input("Gemini API Key", type="password")
    if not odds_api_key:
        odds_api_key = st.text_input("The Odds API Key", type="password")
    st.session_state["tg_token"] = st.text_input("Telegram Bot Token", value=telegram_token, type="password")
    st.session_state["tg_chat_id"] = st.text_input("Telegram Chat ID", value=telegram_chat_id)
    st.markdown("---")
    if st.button("🔔 Send Test Ping", use_container_width=True):
        ok, res = send_telegram_alert("⚡ <b>AI Betting Terminal:</b> Webhook verified!")
        st.success("Sent!") if ok else st.error(f"Failed: {res}")

if not gemini_key:
    st.warning("Please configure your Gemini API Key to proceed.")
    st.stop()

client = genai.Client(api_key=gemini_key)

def load_portfolio() -> pd.DataFrame:
    if os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            for col in COLUMNS:
                if col not in df.columns:
                    if col == "Model_Tag":
                        df[col] = "CORE_EV"
                    elif col == "Market":
                        df[col] = "h2h"
                    else:
                        df[col] = 0.0
            return df[COLUMNS]
        except Exception:
            pass
    return pd.DataFrame(columns=COLUMNS)

def save_portfolio(df: pd.DataFrame):
    df.to_csv(CSV_FILE, index=False)

def get_model_metrics(df: pd.DataFrame, model_tag: str):
    m_df = df[df["Model_Tag"] == model_tag]
    settled = m_df[m_df["Status"].isin(["WON", "LOST", "PUSH"])]
    total_pl = settled["P_L"].sum() if not settled.empty else 0.0
    pending = m_df[m_df["Status"] == "PENDING"]["Stake"].sum()
    equity = STARTING_BANKROLL_PER_MODEL + total_pl
    available = max(0.0, equity - pending)
    decided = settled[settled["Status"].isin(["WON", "LOST"])]
    staked = decided["Stake"].sum() if not decided.empty else 0.0
    roi = (total_pl / staked * 100) if staked > 0 else 0.0
    won = len(settled[settled["Status"] == "WON"])
    total_decided = len(decided)
    win_rate = (won / total_decided * 100) if total_decided > 0 else 0.0
    return {
        "available": available,
        "equity": equity,
        "total_pl": total_pl,
        "roi": roi,
        "win_rate": win_rate,
        "won": won,
        "decided": total_decided,
        "pending": pending
    }

def calculate_kelly_stake(bankroll: float, odds: float, ev_pct: float, max_pct: float) -> float:
    if bankroll <= 1.0 or odds <= 1.01:
        return 0.0
    b = odds - 1.0
    p = (1.0 + (ev_pct / 100.0)) / odds
    q = 1.0 - p
    full_kelly = (b * p - q) / b
    quarter_kelly = max(0.0, full_kelly * 0.25)
    frac = min(max(quarter_kelly, 0.01), max_pct)
    return max(1.0, round(bankroll * frac, 2))

def get_all_active_soccer_leagues() -> list:
    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={odds_api_key}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return [s["key"] for s in r.json() if s.get("key", "").startswith("soccer_") and s.get("active", False)]
    except Exception:
        pass
    return ["soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga"]

def fetch_and_bucket_matches(leagues: list):
    now = datetime.now(timezone.utc)
    buckets = {"EARLY_BIRD": [], "CORE_EV": [], "LATE_STEAM": []}
    scanned_leagues = set()

    for key in leagues:
        url = f"https://api.the-odds-api.com/v4/sports/{key}/odds/"
        params = {
            "apiKey": odds_api_key,
            "regions": "eu,uk,us",
            "markets": "h2h,totals,spreads",
            "oddsFormat": "decimal",
            "bookmakers": "pinnacle,betfair_ex_uk,bet365"
        }
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code != 200:
                continue
            fixtures = r.json()
            if fixtures:
                scanned_leagues.add(key)

            for g in fixtures:
                c_str = g.get("commence_time")
                if not c_str:
                    continue
                try:
                    kickoff = datetime.fromisoformat(c_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                
                hours_to_kickoff = (kickoff - now).total_seconds() / 3600.0

                # Strictly pre-match: ignore matches in the past or within 15 minutes of kickoff
                if hours_to_kickoff < 0.25 or hours_to_kickoff > 144:
                    continue

                if hours_to_kickoff <= 24:
                    bucket_name = "LATE_STEAM"
                elif hours_to_kickoff <= 48:
                    bucket_name = "CORE_EV"
                else:
                    bucket_name = "EARLY_BIRD"

                bookmakers_data = []
                for b in g.get("bookmakers", []):
                    markets_dict = {}
                    for mkt in b.get("markets", []):
                        m_key = mkt.get("key")
                        outcomes = {}
                        for o in mkt.get("outcomes", []):
                            point = o.get("point")
                            label = f"{o.get('name')} {point}" if point is not None else o.get("name")
                            outcomes[label] = o.get("price")
                        markets_dict[m_key] = outcomes
                    bookmakers_data.append({"bookmaker": b.get("title"), "markets": markets_dict})

                buckets[bucket_name].append({
                    "matchup": f"{g.get('home_team')} vs {g.get('away_team')}",
                    "commence_time": c_str,
                    "league": key,
                    "bookmakers": bookmakers_data
                })
        except Exception:
            continue

    return buckets, list(scanned_leagues)

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
                home_s = next((int(s["score"]) for s in scores if s["name"] == home), None)
                away_s = next((int(s["score"]) for s in scores if s["name"] == away), None)
                if home_s is None or away_s is None:
                    continue

                total_goals = home_s + away_s
                h2h_winner = home if home_s > away_s else (away if away_s > home_s else "DRAW")

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
                                diff = (home_s - away_s) if is_home else (away_s - home_s)
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
                        settled_details.append(f"• [{df.at[idx, 'Model_Tag']}] {pick} ({m_str}): {tag}")
                        settled_count += 1
        except Exception:
            continue

    if settled_count > 0:
        save_portfolio(df)
    return df, settled_count, settled_details

# --- TABS ---
tab_auto, tab_portfolio, tab_stocks = st.tabs([
    "🤖 3-Horizon Discovery Terminal", "🏆 Model Competition Leaderboard", "📈 Day Trading"
])

# ----------------- TAB 1: SCANNER -----------------
with tab_auto:
    st.subheader("Global Quantitative Football Terminal: 3 Pre-Match Horizon Engines")
    df = load_portfolio()

    cols = st.columns(3)
    for i, (m_tag, cfg) in enumerate(MODELS_CONFIG.items()):
        m = get_model_metrics(df, m_tag)
        with cols[i]:
            st.markdown(
                f"""
                <div style="background-color: #1e293b; padding: 14px; border-radius: 8px; border-top: 4px solid {cfg['color']};">
                    <b>{cfg['name']}</b><br>
                    💰 Available: <b>${m['available']:.2f}</b><br>
                    💼 Equity: <b>${m['equity']:.2f}</b> (P/L: <b>${m['total_pl']:+.2f}</b>)<br>
                    🎯 ROI: <b>{m['roi']:+.1f}%</b> | Staked: ${m['pending']:.2f}
                </div>
                """,
                unsafe_allow_html=True
            )

    st.write("")
    if st.button("🚀 Run Pre-Match 3-Horizon Engine Sweep (All Global Leagues)", type="primary", use_container_width=True):
        with st.spinner("Fetching pre-match lines across all global competitions..."):
            active_leagues = get_all_active_soccer_leagues()
            buckets, scanned_leagues = fetch_and_bucket_matches(active_leagues)

            total_matches_checked = sum(len(v) for v in buckets.values())
            total_logged = 0

            system_prompt = (
                "You are an autonomous quantitative sports betting model evaluating pre-match football odds across multiple markets (h2h, totals, spreads).\n"
                "RULES:\n"
                "1. Identify Pinnacle lines to calculate market vig and establish true no-vig probabilities.\n"
                "2. If Pinnacle does not quote the match, skip it.\n"
                "3. Formula: EV % = (True Probability * Retail Decimal Odds) - 1.\n"
                f"4. Minimum EV % >= {MIN_EDGE_THRESHOLD}%. Decimal odds between 1.40 and 3.80. Exclude Draw.\n"
                "5. Return strictly a JSON array of objects without Markdown formatting:\n"
                '[{"matchup":"A vs B","kickoff":"YYYY-MM-DD HH:MM","league":"soccer_epl","market":"totals","pick":"Over 2.5","bookmaker":"Bet365","odds":1.95,"ev_pct":2.1}]\n'
                "If none qualify, return: []"
            )

            for m_tag, matches in buckets.items():
                if not matches:
                    continue
                m_metrics = get_model_metrics(df, m_tag)
                if m_metrics["available"] < 10.0:
                    continue

                try:
                    res = client.models.generate_content(
                        model="gemini-3.6-flash",
                        contents=f"{system_prompt}\n\nTarget Model: {m_tag}\nFixtures:\n{json.dumps(matches)}"
                    )
                    clean = res.text.strip()
                    fence = chr(96) * 3
                    if clean.startswith(fence):
                        clean = clean.lstrip(fence)
                        if clean.startswith("json"):
                            clean = clean[4:]
                    if clean.endswith(fence):
                        clean = clean.rstrip(fence)
                    picks = json.loads(clean.strip())
                except Exception:
                    picks = []

                for bet in picks:
                    m_clean = str(bet.get("matchup", "")).strip().lower()
                    p_clean = str(bet.get("pick", "")).strip().lower()
                    existing_pending = df[(df["Model_Tag"] == m_tag) & (df["Status"] == "PENDING")]
                    if any(str(r["Matchup"]).strip().lower() == m_clean and str(r["Pick"]).strip().lower() == p_clean for _, r in existing_pending.iterrows()):
                        continue

                    curr_avail = get_model_metrics(df, m_tag)["available"]
                    odds_val = float(bet.get("odds", 0.0))
                    ev_val = float(bet.get("ev_pct", 0.0))
                    stake = calculate_kelly_stake(curr_avail, odds_val, ev_val, MODELS_CONFIG[m_tag]["max_stake_pct"])
                    if stake < 1.0:
                        continue

                    k_str = str(bet.get("kickoff", "Scheduled")).replace("T", " ").replace("Z", " UTC")
                    new_row = {
                        "ID": len(df) + 1,
                        "Model_Tag": m_tag,
                        "Kickoff_UTC": k_str,
                        "League": bet.get("league", "Global"),
                        "Matchup": bet.get("matchup"),
                        "Market": bet.get("market", "h2h"),
                        "Pick": bet.get("pick"),
                        "Bookmaker": bet.get("bookmaker", "Retail"),
                        "Odds": odds_val,
                        "EV_Pct": ev_val,
                        "Stake": stake,
                        "Status": "PENDING",
                        "P_L": 0.0
                    }
                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                    total_logged += 1

                    tg_msg = (
                        f"🎯 <b>[{m_tag}] NEW PRE-MATCH +EV TRADE</b>\n\n"
                        f"⚽ <b>Match:</b> {bet.get('matchup')}\n"
                        f"📊 <b>Market:</b> {bet.get('market', 'H2H').upper()}\n"
                        f"✅ <b>Pick:</b> <code>{bet.get('pick')}</code>\n"
                        f"📈 <b>Odds:</b> {odds_val} ({bet.get('bookmaker')})\n"
                        f"🔥 <b>Edge:</b> +{ev_val}% EV\n"
                        f"💵 <b>Stake ({m_tag}):</b> ${stake:.2f}\n"
                        f"⏰ <b>Kickoff:</b> {k_str}"
                    )
                    send_telegram_alert(tg_msg)

            audit_msg = (
                f"📡 <b>PRE-MATCH 3-HORIZON AUDIT REPORT</b>\n\n"
                f"🌍 <b>Leagues Checked:</b> {len(scanned_leagues)}\n"
                f"⚽ <b>Pre-Match Fixtures Partitioned:</b>\n"
                f"• Early-Bird (48h-6d): {len(buckets['EARLY_BIRD'])}\n"
                f"• Core Arbitrage (24h-48h): {len(buckets['CORE_EV'])}\n"
                f"• Late-Steam (15m-24h): {len(buckets['LATE_STEAM'])}\n\n"
                f"⚡ <b>New Trades Logged:</b> {total_logged}"
            )
            send_telegram_alert(audit_msg)

            if total_logged > 0:
                save_portfolio(df)
                st.success(f"Discovered and logged {total_logged} new trades across the pre-match horizon engines!")
                st.rerun()
            else:
                st.info(f"Sweep complete across {len(scanned_leagues)} leagues ({total_matches_checked} fixtures evaluated). No edges >= {MIN_EDGE_THRESHOLD}% EV found. Audit delivered to Telegram.")

# ----------------- TAB 2: COMPETITION LEADERBOARD -----------------
with tab_portfolio:
    st.subheader("🏆 $1,000 Horizon Model Competition Leaderboard")
    df = load_portfolio()

    leaderboard_data = []
    for m_tag, cfg in MODELS_CONFIG.items():
        m = get_model_metrics(df, m_tag)
        leaderboard_data.append({
            "Horizon Model": cfg["name"],
            "Tag": m_tag,
            "Starting Capital": f"${STARTING_BANKROLL_PER_MODEL:.2f}",
            "Current Equity": f"${m['equity']:.2f}",
            "Net Profit/Loss": f"${m['total_pl']:+.2f}",
            "ROI": f"{m['roi']:+.2f}%",
            "Win Rate": f"{m['win_rate']:.1f}% ({m['won']}/{m['decided']})",
            "Active Staked": f"${m['pending']:.2f}"
        })

    st.dataframe(pd.DataFrame(leaderboard_data), use_container_width=True, hide_index=True)

    settled = df[df["Status"].isin(["WON", "LOST", "PUSH"])].copy()
    if not settled.empty:
        st.markdown("### 📈 Real-Time Equity Trajectories ($1,000 Baseline)")
        equity_curves = {}
        for m_tag, cfg in MODELS_CONFIG.items():
            sub = settled[settled["Model_Tag"] == m_tag].copy()
            if not sub.empty:
                sub["Equity"] = STARTING_BANKROLL_PER_MODEL + sub["P_L"].cumsum()
                equity_curves[cfg["name"]] = sub["Equity"].reset_index(drop=True)
            else:
                equity_curves[cfg["name"]] = pd.Series([STARTING_BANKROLL_PER_MODEL])
        st.line_chart(pd.DataFrame(equity_curves), use_container_width=True)

    st.divider()
    c_settle, c_reset = st.columns([3, 1])
    if c_settle.button("⚡ Settle Completed Games Across All Models", type="primary", use_container_width=True):
        with st.spinner("Grading match results..."):
            df, count, details = auto_settle_completed_bets(df, odds_api_key)
            if count > 0:
                st.success(f"Settled {count} positions!")
                st.rerun()
            else:
                st.info("No newly settled matches.")

    if c_reset.button("🗑️ Reset All 3 Models to $1,000 Each", use_container_width=True):
        fresh_df = pd.DataFrame(columns=COLUMNS)
        save_portfolio(fresh_df)
        st.success("All models reset to $1,000!")
        st.rerun()

    st.markdown("### 📋 Multi-Model Master Ledger")
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
