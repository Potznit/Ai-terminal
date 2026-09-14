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
STAKE_PERCENT = 0.10
MIN_EDGE_THRESHOLD = 3.0

COLUMNS = [
    "ID", "Kickoff_UTC", "League", "Matchup", "Pick", "Bookmaker", "Odds", "EV_Pct", "Stake", "Status", "P_L"
]

MAJOR_LEAGUES = {
    "Premier League": "soccer_epl",
    "La Liga": "soccer_spain_la_liga",
    "Serie A": "soccer_italy_serie_a",
    "Bundesliga": "soccer_germany_bundesliga",
    "Ligue 1": "soccer_france_ligue_one",
    "Champions League": "soccer_uefa_champs_league",
    "Brasileirão Série A": "soccer_brazil_campeonato"
}

def send_telegram_alert(message_html: str):
    if not telegram_token or not telegram_chat_id:
        return
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": telegram_chat_id, "text": message_html, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception:
        pass

def load_portfolio():
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

def auto_settle(df):
    pending_mask = df["Status"] == "PENDING"
    if not pending_mask.any() or not odds_api_key:
        return df, 0, []

    settled_count = 0
    settled_details = []
    unique_leagues = df.loc[pending_mask, "League"].unique()

    for league in unique_leagues:
        sport_key = MAJOR_LEAGUES.get(league, "soccer_epl")
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

                winning = home if home_s > away_s else (away if away_s > home_s else "DRAW")

                for idx in df[pending_mask].index:
                    m = str(df.at[idx, "Matchup"])
                    if home in m and away in m:
                        pick = str(df.at[idx, "Pick"])
                        stake = float(df.at[idx, "Stake"])
                        odds = float(df.at[idx, "Odds"])
                        if winning == "DRAW":
                            df.at[idx, "Status"] = "LOST"
                            df.at[idx, "P_L"] = -stake
                            settled_details.append(f"• <b>{pick}</b> ({m}): LOST ❌")
                        elif pick.strip().lower() in winning.lower() or winning.lower() in pick.strip().lower():
                            df.at[idx, "Status"] = "WON"
                            gain = round((odds - 1.0) * stake, 2)
                            df.at[idx, "P_L"] = gain
                            settled_details.append(f"• <b>{pick}</b> ({m}): WON ✅ (+${gain:.2f})")
                        else:
                            df.at[idx, "Status"] = "LOST"
                            df.at[idx, "P_L"] = -stake
                            settled_details.append(f"• <b>{pick}</b> ({m}): LOST ❌")
                        settled_count += 1
        except Exception:
            continue
    return df, settled_count, settled_details

def run_scanner(df):
    metrics = get_bankroll_metrics(df)
    calculated_stake = round(metrics["available"] * STAKE_PERCENT, 2)
    if calculated_stake < 1.0:
        return df

    all_matches = []
    selected_books = "betfair_ex_uk,pinnacle,bet365"
    for sport_key in MAJOR_LEAGUES.values():
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        params = {"apiKey": odds_api_key, "regions": "eu,uk,us", "markets": "h2h", "oddsFormat": "decimal", "bookmakers": selected_books}
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                for g in r.json()[:6]:
                    all_matches.append({
                        "matchup": f"{g.get('home_team')} vs {g.get('away_team')}",
                        "commence_time": g.get("commence_time", "Unknown"),
                        "league": sport_key,
                        "bookmakers": [
                            {"bookmaker": b.get("title"), "lines": {o.get("name"): o.get("price") for o in b.get("markets", [{}])[0].get("outcomes", [])}}
                            for b in g.get("bookmakers", [])
                        ]
                    })
        except Exception:
            continue

    if not all_matches or not gemini_key:
        return df

    client = genai.Client(api_key=gemini_key)
    prompt = (
        "You are an autonomous quantitative football betting model. Evaluate match odds.\n"
        "1. Derive true win prob from Pinnacle (vig removed).\n"
        "2. EV % = (True Prob * Retail Decimal Odds) - 1.\n"
        f"3. Retail odds 1.45 to 3.20. Minimum EV >= {MIN_EDGE_THRESHOLD}%.\n"
        "4. Output STRICTLY JSON array: [{\"matchup\":\"A vs B\",\"kickoff\":\"YYYY-MM-DD HH:MM\",\"league\":\"EPL\",\"pick\":\"A\",\"bookmaker\":\"Betfair\",\"odds\":2.30,\"ev_pct\":5.2}]\n"
        "If none qualify, return []"
    )

    try:
        res = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"{prompt}\n\nData:\n{json.dumps(all_matches)}",
            config=types.GenerateContentConfig(temperature=0.1)
        )
        clean_text = res.text.strip()
        fence = chr(96) * 3
        if clean_text.startswith(fence):
            clean_text = clean_text.lstrip(fence)
            if clean_text.startswith("json"):
                clean_text = clean_text[4:]
        if clean_text.endswith(fence):
            clean_text = clean_text.rstrip(fence)
        clean_text = clean_text.strip()
        picks = json.loads(clean_text)
    except Exception:
        picks = []

    for bet in picks:
        dup = not df[(df["Matchup"] == bet.get("matchup")) & (df["Pick"] == bet.get("pick")) & (df["Status"] == "PENDING")].empty
        if not dup:
            k_str = str(bet.get("kickoff", "TBD")).replace("T", " ").replace("Z", " UTC")
            new_row = {
                "ID": len(df) + 1,
                "Kickoff_UTC": k_str,
                "League": bet.get("league", "Major League"),
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
            tg_msg = (
                f"🤖 <b>[AUTONOMOUS SCAN] NEW BET PLACED</b>\n\n"
                f"⚽ <b>Match:</b> {bet.get('matchup')}\n"
                f"🏆 <b>Pick:</b> <code>{bet.get('pick')}</code>\n"
                f"📊 <b>Odds:</b> {bet.get('odds')} ({bet.get('bookmaker')})\n"
                f"📈 <b>Edge:</b> +{bet.get('ev_pct')}% EV\n"
                f"💵 <b>Stake:</b> ${calculated_stake:.2f}\n"
                f"⏰ <b>Kickoff:</b> {k_str}"
            )
            send_telegram_alert(tg_msg)
    return df

if __name__ == "__main__":
    df = load_portfolio()
    df, settled_count, details = auto_settle(df)
    if settled_count > 0:
        m = get_bankroll_metrics(df)
        d_text = "\n".join(details)
        send_telegram_alert(
            f"⚖️ <b>[CRON] MATCH SETTLEMENT REPORT</b>\n\n"
            f"Settled: {settled_count} match(es)\n{d_text}\n\n"
            f"💼 <b>Total Equity:</b> ${m['equity']:.2f}\n"
            f"📈 <b>Realized P/L:</b> ${m['total_pl']:+.2f}\n"
            f"🎯 <b>Win Rate:</b> {m['win_rate']:.1f}% ({m['win_count']}/{m['total_decided']})"
        )
    df = run_scanner(df)
    save_portfolio(df)
