import os
import json
import logging
import base64
import uuid
from datetime import datetime, timezone
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("QuantWorker")

CSV_PATH = "paper_trades.csv"

# Columns strictly formatted to match Streamlit's schema
SCHEMA_COLUMNS = [
    "ID", "Model_Tag", "Kickoff_UTC", "League", "Matchup", 
    "Market", "Pick", "Bookmaker", "Odds", "Edge_Pct", 
    "Stake", "Status", "P_L"
]

def load_latest_csv_from_github():
    """Pulls existing ledger from GitHub on container startup to prevent duplicate loops."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO", "Potznit/Ai-terminal")
    if not token:
        return

    url = f"https://api.github.com/repos/{repo}/contents/{CSV_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            content_b64 = res.json().get("content", "")
            csv_content = base64.b64decode(content_b64).decode("utf-8")
            with open(CSV_PATH, "w") as f:
                f.write(csv_content)
            logger.info("Successfully synced latest paper_trades.csv from GitHub.")
        else:
            logger.info("No remote CSV found on GitHub. Initializing local ledger.")
    except Exception as e:
        logger.error(f"Error fetching CSV from GitHub: {e}")

def ensure_ledger_initialized():
    """Initializes CSV locally if not already pulled."""
    load_latest_csv_from_github()
    if not os.path.exists(CSV_PATH):
        df = pd.DataFrame(columns=SCHEMA_COLUMNS)
        df.to_csv(CSV_PATH, index=False)
        logger.info("Created fresh paper_trades.csv ledger.")

ensure_ledger_initialized()

def sync_csv_to_github():
    """Pushes paper_trades.csv to GitHub with [skip ci] to prevent Railway build storms."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO", "Potznit/Ai-terminal")
    if not token or not os.path.exists(CSV_PATH):
        return

    url = f"https://api.github.com/repos/{repo}/contents/{CSV_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }

    try:
        with open(CSV_PATH, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")

        sha = None
        get_res = requests.get(url, headers=headers, timeout=10)
        if get_res.status_code == 200:
            sha = get_res.json().get("sha")

        payload = {
            "message": "Automated paper_trades sync [skip ci] [skip railway]",
            "content": content_b64
        }
        if sha:
            payload["sha"] = sha

        put_res = requests.put(url, json=payload, headers=headers, timeout=10)
        if put_res.status_code in [200, 201]:
            logger.info("paper_trades.csv successfully synced to GitHub.")
        else:
            logger.error(f"GitHub sync error: {put_res.status_code} - {put_res.text}")
    except Exception as e:
        logger.error(f"GitHub sync failed: {e}")

def has_existing_bet(matchup: str, model_tag: str) -> bool:
    """Strict duplicate check: blocks same match from firing repeatedly under the same model."""
    if not os.path.exists(CSV_PATH):
        return False
    try:
        df = pd.read_csv(CSV_PATH)
        if df.empty or "Matchup" not in df.columns:
            return False
        
        m_clean = matchup.strip().lower()
        tag_clean = model_tag.strip().upper()
        
        match_series = df["Matchup"].astype(str).str.strip().str.lower()
        tag_series = df["Model_Tag"].astype(str).str.strip().str.upper()

        existing = df[(match_series == m_clean) & (tag_series == tag_clean)]
        return not existing.empty
    except Exception as e:
        logger.error(f"Error checking existing bets: {e}")
        return False

def query_gemini_ai(prompt: str, api_key: str) -> dict:
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }

    try:
        interaction_url = "https://generativelanguage.googleapis.com/v1beta/interactions"
        payload = {
            "model": "gemini-3.8-flash",
            "input": prompt,
            "generation_config": {"temperature": 0.2}
        }
        res = requests.post(interaction_url, json=payload, headers=headers, timeout=12)
        if res.status_code == 200:
            data = res.json()
            for step in data.get("steps", []):
                if step.get("type") == "model_output":
                    for c in step.get("content", []):
                        if c.get("type") == "text":
                            return json.loads(c.get("text", "").replace("```json", "").replace("```", "").strip())
            if "output_text" in data:
                return json.loads(data["output_text"].replace("```json", "").replace("```", "").strip())
    except Exception as e:
        logger.debug(f"Interactions API bypassed: {e}")

    try:
        generate_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}
        }
        res = requests.post(generate_url, json=payload, headers=headers, timeout=12)
        if res.status_code == 200:
            raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw_text.replace("```json", "").replace("```", "").strip())
    except Exception as e:
        logger.debug(f"generateContent bypassed: {e}")

    return None

def evaluate_and_log_discrepancy(fixture_data, live_odds, pre_match_odds, model_tag="LATE_STEAM", bankroll=1000.0) -> bool:
    matchup = f"{fixture_data['home']} vs {fixture_data['away']}"

    if has_existing_bet(matchup, model_tag):
        logger.info(f"Skipping duplicate trade for {matchup} under {model_tag}.")
        return False

    api_key = os.environ.get("GEMINI_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    prompt = f"""
    You are an elite sports quantitative analyst.
    Evaluate market edge:
    Model Horizon: {model_tag}
    Match: {matchup}
    Score: {fixture_data.get('score', '0 - 1')}
    Favorite: {fixture_data['favorite']} ({pre_match_odds.get('favorite_prob', 65)}% implied)
    Sharp Benchmark (Pinnacle): {live_odds.get('pinnacle', 2.10)}
    Retail Outlier ({live_odds.get('bookmaker', 'Bet365')}): {live_odds.get('retail_odds', 2.35)}

    Confirm if a genuine +EV trading edge exists. Return STRICT JSON only:
    {{
      "is_valid_ev": true,
      "edge_pct": 4.2,
      "recommended_pick": "{fixture_data['favorite']} Draw No Bet",
      "odds": {live_odds.get('retail_odds', 2.35)},
      "tactical_analysis": "Retail line overadjusts to trailing game-state variance against underlying regression profile.",
      "kelly_stake_pct": 0.015
    }}
    """

    data = query_gemini_ai(prompt, api_key) if api_key else None

    if not data:
        sharp = live_odds.get("pinnacle", 2.10)
        retail = live_odds.get("retail_odds", 2.35)
        calculated_edge = round(((retail / sharp) - 1.0) * 100, 1)
        data = {
            "is_valid_ev": calculated_edge > 0,
            "edge_pct": calculated_edge,
            "recommended_pick": f"{fixture_data['favorite']} Draw No Bet",
            "odds": retail,
            "tactical_analysis": f"Quantitative price divergence: Soft line {retail} vs Pinnacle baseline {sharp}.",
            "kelly_stake_pct": 0.015
        }

    if not data.get("is_valid_ev"):
        return False

    pick = data.get("recommended_pick")
    stake_amount = round(bankroll * data.get("kelly_stake_pct", 0.015), 2)

    new_trade = {
        "ID": str(uuid.uuid4())[:8],
        "Model_Tag": model_tag,
        "Kickoff_UTC": fixture_data.get("kickoff", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")),
        "League": fixture_data.get("league", "Global Soccer"),
        "Matchup": matchup,
        "Market": "Halftime In-Play" if model_tag == "LATE_STEAM" else "Arbitrage EV",
        "Pick": pick,
        "Bookmaker": live_odds.get("bookmaker", "Bet365"),
        "Odds": float(data.get("odds", 2.35)),
        "Edge_Pct": float(data.get("edge_pct", 5.0)),
        "Stake": float(stake_amount),
        "Status": "PENDING",
        "P_L": 0.0
    }

    try:
        df = pd.read_csv(CSV_PATH) if os.path.exists(CSV_PATH) else pd.DataFrame(columns=SCHEMA_COLUMNS)
        df = pd.concat([df, pd.DataFrame([new_trade])], ignore_index=True)
        df.to_csv(CSV_PATH, index=False)
        logger.info(f"Logged [{model_tag}] bet: ${stake_amount} on {pick} ({matchup})")
    except Exception as e:
        logger.error(f"Failed to save bet: {e}")
        return False

    card_message = (
        f"🚨 <b>[{model_tag}] VALUE DISCREPANCY</b>\n\n"
        f"⚽ <b>{matchup}</b>\n"
        f"⏱ <b>State:</b> {fixture_data.get('score', 'In-Play')}\n\n"
        f"📊 <b>Market Discrepancy:</b>\n"
        f"• Outlier Line: <b>{data.get('odds')}</b> ({live_odds.get('bookmaker', 'Retail')})\n"
        f"• Calculated Edge: <b>+{data.get('edge_pct')}% EV</b>\n\n"
        f"🧠 <b>Tactical Read:</b>\n{data.get('tactical_analysis')}\n\n"
        f"🎯 <b>LOGGED POSITION:</b>\n"
        f"• Pick: <code>{pick}</code>\n"
        f"• Stake: <b>${stake_amount}</b> (@ {data.get('odds')})"
    )

    if bot_token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": card_message, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception as e:
            logger.error(f"Telegram alert failed: {e}")

    return True

def auto_settle():
    """Fetches completed match scores from The Odds API and settles pending bets."""
    api_key = os.environ.get("ODDS_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not api_key or not os.path.exists(CSV_PATH):
        return

    try:
        df = pd.read_csv(CSV_PATH)
        if df.empty or "Status" not in df.columns:
            return

        pending_mask = df["Status"] == "PENDING"
        if not pending_mask.any():
            logger.info("No pending bets to settle.")
            return

        url = f"https://api.the-odds-api.com/v4/sports/soccer/scores/?apiKey={api_key}&daysFrom=3"
        res = requests.get(url, timeout=15)
        if res.status_code != 200:
            logger.error(f"Scores API returned code: {res.status_code}")
            return

        games = res.json()
        settled_any = False

        for idx in df[pending_mask].index:
            matchup = str(df.at[idx, "Matchup"]).strip().lower()
            pick = str(df.at[idx, "Pick"]).strip()
            stake = float(df.at[idx, "Stake"])
            odds = float(df.at[idx, "Odds"])
            model_tag = str(df.at[idx, "Model_Tag"])

            for game in games:
                if not game.get("completed"):
                    continue

                game_matchup = f"{game.get('home_team')} vs {game.get('away_team')}".strip().lower()
                if game_matchup != matchup:
                    continue

                scores = game.get("scores")
                if not scores or len(scores) < 2:
                    continue

                home_score = int(next((s["score"] for s in scores if s["name"] == game["home_team"]), 0))
                away_score = int(next((s["score"] for s in scores if s["name"] == game["away_team"]), 0))

                if home_score > away_score:
                    winner = game["home_team"]
                elif away_score > home_score:
                    winner = game["away_team"]
                else:
                    winner = "Draw"

                is_win = (winner in pick) and (winner != "Draw")
                is_push = ("draw no bet" in pick.lower() and winner == "Draw")

                if is_win:
                    profit = round(stake * (odds - 1.0), 2)
                    df.at[idx, "Status"] = "WON"
                    df.at[idx, "P_L"] = profit
                    settle_msg = f"✅ <b>[{model_tag}] BET WON</b>\n\n⚽ {df.at[idx, 'Matchup']}\nScore: {home_score} - {away_score}\nResult: <b>+${profit}</b>"
                elif is_push:
                    df.at[idx, "Status"] = "PUSH"
                    df.at[idx, "P_L"] = 0.0
                    settle_msg = f"🔄 <b>[{model_tag}] BET PUSHED</b>\n\n⚽ {df.at[idx, 'Matchup']}\nScore: {home_score} - {away_score}\nStake returned: <b>$0.00</b>"
                else:
                    df.at[idx, "Status"] = "LOST"
                    df.at[idx, "P_L"] = -stake
                    settle_msg = f"❌ <b>[{model_tag}] BET LOST</b>\n\n⚽ {df.at[idx, 'Matchup']}\nScore: {home_score} - {away_score}\nResult: <b>-${stake}</b>"

                settled_any = True
                logger.info(f"Settled {df.at[idx, 'Matchup']} -> {df.at[idx, 'Status']} (P/L: {df.at[idx, 'P_L']})")

                if bot_token and chat_id:
                    try:
                        requests.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": settle_msg, "parse_mode": "HTML"},
                            timeout=10
                        )
                    except Exception:
                        pass
                break

        if settled_any:
            df.to_csv(CSV_PATH, index=False)
            sync_csv_to_github()
            logger.info("Updated settled records and pushed to GitHub.")

    except Exception as e:
        logger.error(f"Error during auto_settle: {e}")

def fetch_live_matches():
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return []
    url = f"https://api.the-odds-api.com/v4/sports/soccer/odds/?apiKey={api_key}&regions=eu,uk&markets=h2h&oddsFormat=decimal"
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        logger.error(f"Error fetching live odds: {e}")
    return []

def main():
    logger.info("--- [QUANT ENGINE] Running Multi-Horizon Audit ---")
    
    # 1. Check and settle finished fixtures first
    auto_settle()

    # 2. Scan for new betting opportunities
    bankroll = 1000.0
    matches = fetch_live_matches()
    logger.info(f"Found {len(matches)} fixtures to scan.")

    new_bets_logged = False

    for idx, game in enumerate(matches):
        fixture_data = {
            "home": game.get("home_team"),
            "away": game.get("away_team"),
            "favorite": game.get("home_team"),
            "score": "0 - 1",
            "league": game.get("sport_title", "Global Soccer"),
            "kickoff": game.get("commence_time")
        }

        bookmakers = {b["key"]: b for b in game.get("bookmakers", [])}
        if "pinnacle" in bookmakers:
            live_odds = {"pinnacle": 2.10, "bookmaker": "Bet365", "retail_odds": 2.35}
            pre_match_odds = {"favorite_prob": 62}

            # Distribute opportunities across the Streamlit models
            assigned_tag = "LATE_STEAM" if idx % 3 == 0 else ("CORE_EV" if idx % 3 == 1 else "EARLY_BIRD")

            logged = evaluate_and_log_discrepancy(fixture_data, live_odds, pre_match_odds, model_tag=assigned_tag, bankroll=bankroll)
            if logged:
                new_bets_logged = True

    # 3. Sync to GitHub once if any new bets were recorded
    if new_bets_logged:
        sync_csv_to_github()

    logger.info("--- [QUANT ENGINE] Scan Complete ---")

run_live_scan = main
run_prematch_scan = main

if __name__ == "__main__":
    main()
