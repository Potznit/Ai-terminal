import os
import json
import requests
import pandas as pd
from google import genai
from google.genai import types

# --- CREDENTIALS FROM ENVIRONMENT ---
gemini_key = os.environ.get("GEMINI_API_KEY", "")
odds_api_key = os.environ.get("ODDS_API_KEY", "")
telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "8956869998:AAH9SEXc6qID3Ie1JDx3mffb8pHLVWxMgoE")
telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "6565714528")

CSV_FILE = "paper_trades.csv"
STARTING_BANKROLL = 1000.0
MIN_EDGE_THRESHOLD = 1.5

COLUMNS = [
    "ID", "Kickoff_UTC", "League", "Matchup", "Market", "Pick", "Bookmaker", "Odds", "EV_Pct", "Stake", "Status", "P_L"
]

def send_telegram_alert(message_html: str):
    if not telegram_token or not telegram_chat_id:
        return
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": telegram_chat_id, "text": message_html, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

def load_portfolio():
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

def save_portfolio(df):
    df.to_csv(CSV_FILE, index=False)

def get_bankroll_metrics(df):
    settled = df[df["Status"].isin(["WON", "LOST", "PUSH"])]
    total_pl = settled["P_L"].sum() if not settled.empty else 0.0
    pending = df[df["Status"] == "PENDING"]["Stake"].sum()
    available = STARTING_BANKROLL + total_pl - pending
    equity = STARTING_BANKROLL + total_pl
    total_staked = settled[settled["Status"].isin(["WON", "LOST"])]["Stake"].sum()
    roi = (total_pl / total_staked * 100) if total_staked > 0 else 0.0
    won = len(settled[settled["Status"] == "WON"])
    decided = len(settled[settled["Status"].isin(["WON", "LOST"])])
    win_rate = (won / decided * 100) if decided > 0 else 0.0
    return {
        "available": max(0.0, available),
        "equity": equity,
        "total_pl": total_pl,
        "roi": roi,
        "win_rate": win_rate,
        "win_count": won,
        "total_decided": decided
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

def get_active_soccer_leagues():
    """Dynamically fetches all active soccer competitions worldwide."""
    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={odds_api_key}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            leagues = [s["key"] for s in r.json() if s.get("key", "").startswith("soccer_") and s.get("active", False)]
            if leagues:
                return leagues
    except Exception:
        pass
    return [
        "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", 
        "soccer_germany_bundesliga", "soccer_france_ligue_one", "soccer_portugal_primeira_liga"
    ]

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
                h2h_winner = home if home_score > away_score else (away if away_score > home_score else "DRAW")

                for idx in df[pending_mask].index:
                    m = str(df.at[idx, "Matchup"])
                    if home.lower() in m.lower() and away.lower() in m.lower():
                        pick = str(df.at[idx, "Pick"]).strip()
                        stake = float(df.at[idx, "Stake"])
                        odds = float(df.at[idx, "Odds"])
                        market = str(df.at[idx, "Market"]).lower()
                        status = "LOST"
                        gain = -stake

                        if market == "totals":
                            pick_parts = pick.split()
                            if len(pick_parts) >= 2:
                                direction = pick_parts[0].lower()
                                line = float(pick_parts[1])
                                if total_goals == line:
                                    status = "PUSH"
                                    gain = 0.0
                                elif (direction == "over" and total_goals > line) or (direction == "under" and total_goals < line):
                                    status = "WON"
                                    gain = round((odds - 1.0) * stake, 2)
                        elif market == "spreads":
                            pick_parts = pick.rsplit(" ", 1)
                            if len(pick_parts) == 2:
                                team_name = pick_parts[0]
                                handicap = float(pick_parts[1])
                                is_home = home.lower() in team_name.lower()
                                goal_diff = (home_s - away_s) if is_home else (away_s - home_s)
                                if goal_diff + handicap > 0:
                                    status = "WON"
                                    gain = round((odds - 1.0) * stake, 2)
                                elif goal_diff + handicap == 0:
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
                        outcome_tag = f"WON ✅ (+${gain:.2f})" if status == "WON" else ("PUSH 🔄" if status == "PUSH" else "LOST ❌")
                        settled_details.append(f"• [{market.upper()}] <b>{pick}</b> ({m}): {outcome_tag}")
                        settled_count += 1
        except Exception:
            continue
    return df, settled_count, settled_details

def run_scanner(df):
    metrics = get_bankroll_metrics(df)
    if metrics["available"] < 10.0 or not odds_api_key or not gemini_key:
        return df

    active_leagues = get_active_soccer_leagues()
    all_matches = []
    scanned_league_names = []
    selected_books = "betfair_ex_uk,pinnacle,bet365"

    for sport_key in active_leagues:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        params = {
            "apiKey": odds_api_key,
            "regions": "eu,uk,us",
            "markets": "h2h,totals,spreads",
            "oddsFormat": "decimal",
            "bookmakers": selected_books
        }
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                fixtures = r.json()
                if fixtures:
                    scanned_league_names.append(sport_key.replace("soccer_", ""))
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
                        "league": sport_key,
                        "bookmakers": bookmakers_data
                    })
        except Exception:
            continue

    total_scanned_count = len(scanned_league_names)
    total_matches_count = len(all_matches)

    if not all_matches:
        send_telegram_alert(
            f"📡 <b>SCAN REPORT:</b> Checked {total_scanned_count} leagues. No upcoming matches currently quoted by Pinnacle."
        )
        return df

    client = genai.Client(api_key=gemini_key)
    prompt = (
        "You are an autonomous quantitative sports betting model evaluating multi-market football odds.\n"
        "MARKETS INCLUDED: 'h2h' (Match Winner), 'totals' (Over/Under Goals), 'spreads' (Handicap).\n"
        "RULES:\n"
        "1. Identify Pinnacle lines for each market, calculate market vig, and derive true no-vig probabilities.\n"
        "2. If Pinnacle does not quote the match, skip it.\n"
        "3. Compare that true probability against retail bookmakers (Betfair, Bet365).\n"
        "4. Formula: EV % = (True Probability * Retail Decimal Odds) - 1.\n"
        f"5. SELECTION CRITERIA:\n"
        f"   - Minimum EV % >= {MIN_EDGE_THRESHOLD}%.\n"
        "   - Decimal odds must be between 1.40 and 3.80.\n"
        "   - Exclude match winner Draw (only Home/Away, Totals Over/Under, or Team Spreads).\n"
        "6. Return strictly a clean JSON array of objects without Markdown formatting:\n"
        '[{"matchup":"Team A vs Team B","kickoff":"YYYY-MM-DD HH:MM","league":"soccer_epl","market":"totals","pick":"Over 2.5","bookmaker":"Bet365","odds":1.95,"ev_pct":2.1}]\n'
        "If no bets qualify, return exactly: []"
    )

    try:
        res = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=f"{prompt}\n\nData:\n{json.dumps(all_matches)}"
        )
        clean_text = res.text.strip()
        fence = chr(96) * 3
        if clean_text.startswith(fence):
            clean_text = clean_text.lstrip(fence)
            if clean_text.startswith("json"):
                clean_text = clean_text[4:]
        if clean_text.endswith(fence):
            clean_text = clean_text.rstrip(fence)
        picks = json.loads(clean_text.strip())
    except Exception:
        picks = []

    new_trades_count = 0
    for bet in picks:
        # Robust deduplication: check if pick is already logged and PENDING
        matchup_clean = str(bet.get("matchup", "")).strip().lower()
        pick_clean = str(bet.get("pick", "")).strip().lower()

        existing_pending = df[df["Status"] == "PENDING"]
        is_duplicate = False
        for _, row in existing_pending.iterrows():
            if str(row["Matchup"]).strip().lower() == matchup_clean and str(row["Pick"]).strip().lower() == pick_clean:
                is_duplicate = True
                break

        if not is_duplicate:
            current_bankroll = get_bankroll_metrics(df)["available"]
            odds_val = float(bet.get("odds", 0.0))
            ev_val = float(bet.get("ev_pct", 0.0))
            stake = calculate_kelly_stake(current_bankroll, odds_val, ev_val)
            if stake < 1.0:
                continue

            k_str = str(bet.get("kickoff", "TBD")).replace("T", " ").replace("Z", " UTC")
            new_row = {
                "ID": len(df) + 1,
                "Kickoff_UTC": k_str,
                "League": bet.get("league", "Global Soccer"),
                "Matchup": bet.get("matchup"),
                "Market": bet.get("market", "h2h"),
                "Pick": bet.get("pick"),
                "Bookmaker": bet.get("bookmaker", "Retail Book"),
                "Odds": odds_val,
                "EV_Pct": ev_val,
                "Stake": stake,
                "Status": "PENDING",
                "P_L": 0.0
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            new_trades_count += 1

            tg_msg = (
                f"🎯 <b>NEW +EV POSITION COMMITTED</b>\n\n"
                f"⚽ <b>Match:</b> {bet.get('matchup')}\n"
                f"📊 <b>Market:</b> {bet.get('market', 'H2H').upper()}\n"
                f"✅ <b>Pick:</b> <code>{bet.get('pick')}</code>\n"
                f"📈 <b>Odds:</b> {odds_val} ({bet.get('bookmaker')})\n"
                f"🔥 <b>Edge:</b> +{ev_val}% EV\n"
                f"💵 <b>Quarter-Kelly Stake:</b> ${stake:.2f}\n"
                f"⏰ <b>Kickoff:</b> {k_str}"
            )
            send_telegram_alert(tg_msg)

    # Send scan summary report to Telegram
    sample_leagues = ", ".join(scanned_league_names[:5])
    summary_msg = (
        f"📡 <b>HOURLY GLOBAL SCAN AUDIT</b>\n\n"
        f"🌍 <b>Leagues Checked:</b> {total_scanned_count} ({sample_leagues}...)\n"
        f"⚽ <b>Fixtures Evaluated:</b> {total_matches_count} matches\n"
        f"🎯 <b>Markets:</b> Match Winner, Over/Under Goals, Spreads\n"
        f"⚡ <b>New Qualified Positions:</b> {new_trades_count}"
    )
    send_telegram_alert(summary_msg)

    return df

if __name__ == "__main__":
    df = load_portfolio()
    df, settled_count, details = auto_settle(df)
    if settled_count > 0:
        m = get_bankroll_metrics(df)
        d_text = "\n".join(details)
        send_telegram_alert(
            f"⚖️ <b>[CRON] MULTI-MARKET SETTLEMENT REPORT</b>\n\n"
            f"Settled: {settled_count} position(s)\n{d_text}\n\n"
            f"💼 <b>Total Equity:</b> ${m['equity']:.2f}\n"
            f"📈 <b>Realized P/L:</b> ${m['total_pl']:+.2f}\n"
            f"🎯 <b>Win Rate:</b> {m['win_rate']:.1f}% ({m['win_count']}/{m['total_decided']})"
        )
    df = run_scanner(df)
    save_portfolio(df)
