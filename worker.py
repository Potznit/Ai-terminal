import os
import json
import requests
import pandas as pd
from datetime import datetime, timezone
from google import genai
from google.genai import types

# --- CREDENTIALS FROM ENVIRONMENT ---
gemini_key = os.environ.get("GEMINI_API_KEY", "")
odds_api_key = os.environ.get("ODDS_API_KEY", "")
telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "8956869998:AAH9SEXc6qID3Ie1JDx3mffb8pHLVWxMgoE")
telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "6565714528")

CSV_FILE = "paper_trades.csv"
STARTING_BANKROLL_PER_MODEL = 1000.0
MIN_EDGE_THRESHOLD = 1.5

MODELS_CONFIG = {
    "EARLY_BIRD": {"name": "Early-Bird (48h - 6d)", "max_stake_pct": 0.025},
    "CORE_EV": {"name": "Core Arbitrage (24h - 48h)", "max_stake_pct": 0.050},
    "LATE_STEAM": {"name": "Late-Steam (15m - 24h)", "max_stake_pct": 0.040}
}

COLUMNS = [
    "ID", "Model_Tag", "Kickoff_UTC", "League", "Matchup", "Market", "Pick", 
    "Bookmaker", "Odds", "EV_Pct", "Stake", "Status", "P_L"
]

def send_telegram_alert(message_html: str):
    if not telegram_token or not telegram_chat_id:
        print("Telegram token or Chat ID is missing!")
        return
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": telegram_chat_id, "text": message_html, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"Telegram dispatch status: {r.status_code}")
    except Exception as e:
        print(f"Telegram exception: {e}")

def load_portfolio():
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

def save_portfolio(df):
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

def get_active_soccer_leagues():
    print("Fetching active soccer competitions globally...")
    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={odds_api_key}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            leagues = [s["key"] for s in r.json() if s.get("key", "").startswith("soccer_") and s.get("active", False)]
            if leagues:
                print(f"Found {len(leagues)} active leagues.")
                return leagues
    except Exception as e:
        print(f"Error fetching sports list: {e}")
    return ["soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga"]

def auto_settle(df):
    pending_mask = df["Status"] == "PENDING"
    if not pending_mask.any() or not odds_api_key:
        return df, 0, []

    settled_count = 0
    settled_details = []
    unique_leagues = df.loc[pending_mask, "League"].unique()

    for sport_key in unique_leagues:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/?apiKey={odds_api_key}&daysFrom=3"
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
    return df, settled_count, settled_details

def run_scanner(df):
    if not odds_api_key or not gemini_key:
        print("API keys missing from environment. Exiting.")
        return df

    active_leagues = get_active_soccer_leagues()
    now = datetime.now(timezone.utc)
    buckets = {"EARLY_BIRD": [], "CORE_EV": [], "LATE_STEAM": []}
    scanned_leagues = set()

    print(f"Beginning multi-market odds queries for {len(active_leagues)} leagues...")
    for key in active_leagues:
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

                hours = (kickoff - now).total_seconds() / 3600.0
                if hours < 0.25 or hours > 144:
                    continue

                bucket = "LATE_STEAM" if hours <= 24 else ("CORE_EV" if hours <= 48 else "EARLY_BIRD")

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

                buckets[bucket].append({
                    "matchup": f"{g.get('home_team')} vs {g.get('away_team')}",
                    "commence_time": c_str,
                    "league": key,
                    "bookmakers": bookmakers_data
                })
        except Exception:
            continue

    print(f"Partitioned matches: Early={len(buckets['EARLY_BIRD'])}, Core={len(buckets['CORE_EV'])}, Late={len(buckets['LATE_STEAM'])}")
    client = genai.Client(api_key=gemini_key)
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

    new_trades_count = 0
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

            k_str = str(bet.get("kickoff", "TBD")).replace("T", " ").replace("Z", " UTC")
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
            new_trades_count += 1

            tg_msg = (
                f"🎯 <b>[{m_tag}] NEW PRE-MATCH +EV TRADE</b>\n\n"
                f"⚽ <b>Match:</b> {bet.get('matchup')}\n"
                f"📊 <b>Market:</b> {bet.get('market', 'H2H').upper()}\n"
                f"✅ <b>Pick:</b> <code>{bet.get('pick')}</code>\n"
                f"📈 <b>Odds:</b> {odds_val} ({bet.get('bookmaker')})\n"
                f"🔥 <b>Edge:</b> +{ev_val}% EV\n"
                f"💵 <b>Quarter-Kelly Stake:</b> ${stake:.2f}\n"
                f"⏰ <b>Kickoff:</b> {k_str}"
            )
            send_telegram_alert(tg_msg)

    summary_msg = (
        f"📡 <b>HOURLY PRE-MATCH 3-HORIZON AUDIT</b>\n\n"
        f"🌍 <b>Leagues Checked:</b> {len(scanned_leagues)}\n"
        f"• Early-Bird (48h-6d): {len(buckets['EARLY_BIRD'])}\n"
        f"• Core Arbitrage (24h-48h): {len(buckets['CORE_EV'])}\n"
        f"• Late-Steam (15m-24h): {len(buckets['LATE_STEAM'])}\n\n"
        f"⚡ <b>New Trades Logged:</b> {new_trades_count}"
    )
    send_telegram_alert(summary_msg)
    return df

if __name__ == "__main__":
    df = load_portfolio()
    df, settled_count, details = auto_settle(df)
    if settled_count > 0:
        d_text = "\n".join(details)
        send_telegram_alert(f"⚖️ <b>[CRON] SETTLEMENT REPORT</b>\n\nSettled: {settled_count}\n{d_text}")
    df = run_scanner(df)
    save_portfolio(df)
