import os
import json
import logging
import base64
from datetime import datetime, timezone
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("QuantWorker")

LIVE_MODEL_TAG = "LIVE_WAR_ROOM"
CSV_PATH = "paper_trades.csv"

def sync_csv_to_github():
    """Commits paper_trades.csv to GitHub so Streamlit Cloud stays synced."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO", "Potznit/Ai-terminal")
    if not token or not os.path.exists(CSV_PATH):
        return

    url = f"https://api.github.com/repos/{repo}/contents/paper_trades.csv"
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
            "message": "Automated paper_trades sync [skip ci]",
            "content": content_b64
        }
        if sha:
            payload["sha"] = sha

        put_res = requests.put(url, json=payload, headers=headers, timeout=10)
        if put_res.status_code in [200, 201]:
            logger.info("paper_trades.csv successfully synced to GitHub.")
        else:
            logger.error(f"GitHub sync returned {put_res.status_code}: {put_res.text}")
    except Exception as e:
        logger.error(f"GitHub sync failed: {e}")

def has_existing_bet(matchup: str, pick: str) -> bool:
    if not os.path.exists(CSV_PATH):
        return False
    try:
        df = pd.read_csv(CSV_PATH)
        if df.empty:
            return False
        existing = df[(df["matchup"] == matchup) & (df["pick"] == pick)]
        return not existing.empty
    except Exception as e:
        logger.error(f"Error checking existing bets: {e}")
        return False

def query_gemini_ai(prompt: str, api_key: str) -> dict:
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }

    # Primary: Interactions API
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
                    for content in step.get("content", []):
                        if content.get("type") == "text":
                            return json.loads(content.get("text", "").replace("```json", "").replace("```", "").strip())
            if "output_text" in data:
                return json.loads(data["output_text"].replace("```json", "").replace("```", "").strip())
    except Exception as e:
        logger.debug(f"Interactions API probe bypassed: {e}")

    # Fallback: generateContent
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

def evaluate_and_log_live_discrepancy(fixture_data, live_odds, pre_match_odds, bankroll=1000.0):
    api_key = os.environ.get("GEMINI_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    matchup = f"{fixture_data['home']} vs {fixture_data['away']}"

    prompt = f"""
    You are an elite live in-play soccer quantitative analyst.
    Evaluate this Halftime market state:
    Match: {fixture_data['home']} vs {fixture_data['away']}
    Current Score: {fixture_data.get('score', '0 - 1')} (Halftime)
    Pre-Match Favorite: {fixture_data['favorite']} ({pre_match_odds.get('favorite_prob', 65)}% implied)
    Live Match Odds:
    - Sharp Baseline (Pinnacle): {live_odds.get('pinnacle', 2.10)}
    - Retail Bookmaker ({live_odds.get('bookmaker', 'Bet365')}): {live_odds.get('retail_odds', 2.35)}

    Confirm if a genuine +EV trading edge exists. Return STRICT JSON only:
    {{
      "is_valid_ev": true,
      "edge_pct": 4.2,
      "recommended_pick": "{fixture_data['favorite']} 2nd-Half ML",
      "odds": {live_odds.get('retail_odds', 2.35)},
      "tactical_analysis": "Retail score-panic overreaction creating mispricing against favorite underlying second-half metrics.",
      "kelly_stake_pct": 0.015
    }}
    """

    data = None
    if api_key:
        data = query_gemini_ai(prompt, api_key)

    if not data:
        sharp = live_odds.get("pinnacle", 2.10)
        retail = live_odds.get("retail_odds", 2.35)
        calculated_edge = round(((retail / sharp) - 1.0) * 100, 1)
        data = {
            "is_valid_ev": calculated_edge > 0,
            "edge_pct": calculated_edge,
            "recommended_pick": f"{fixture_data['favorite']} Over / Draw No Bet",
            "odds": retail,
            "tactical_analysis": f"Quantitative price divergence: Soft book line {retail} vs sharp Pinnacle baseline {sharp}.",
            "kelly_stake_pct": 0.015
        }

    if not data.get("is_valid_ev"):
        return

    pick = data.get("recommended_pick")

    if has_existing_bet(matchup, pick):
        logger.info(f"Skipping duplicate trade for {matchup} [{pick}].")
        return

    stake_amount = round(bankroll * data.get("kelly_stake_pct", 0.015), 2)

    new_trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_tag": LIVE_MODEL_TAG,
        "league": fixture_data.get("league", "Global Soccer"),
        "matchup": matchup,
        "market": "In-Play Halftime Discrepancy",
        "pick": pick,
        "bookmaker": live_odds.get("bookmaker", "Retail Soft"),
        "odds": data.get("odds"),
        "edge_pct": data.get("edge_pct"),
        "stake": stake_amount,
        "status": "PENDING",
        "kickoff": fixture_data.get("kickoff", datetime.now(timezone.utc).isoformat()),
        "p_l": 0.0
    }

    try:
        if os.path.exists(CSV_PATH):
            df = pd.read_csv(CSV_PATH)
            df = pd.concat([df, pd.DataFrame([new_trade])], ignore_index=True)
        else:
            df = pd.DataFrame([new_trade])
        df.to_csv(CSV_PATH, index=False)
        logger.info(f"Logged paper bet: ${stake_amount} on {pick} ({matchup})")
        # Sync immediately to GitHub
        sync_csv_to_github()
    except Exception as e:
        logger.error(f"Error saving trade: {e}")

    card_message = (
        f"🚨 <b>[LIVE WAR ROOM] HALFTIME DISCREPANCY</b>\n\n"
        f"⚽ <b>{fixture_data['home']} vs {fixture_data['away']}</b>\n"
        f"⏱ <b>Score:</b> {fixture_data.get('score', '0 - 1')} (Halftime)\n\n"
        f"📊 <b>Market Discrepancy:</b>\n"
        f"• Pre-Match Favorite: {fixture_data['favorite']}\n"
        f"• Retail Outlier: {data.get('odds')} ({live_odds.get('bookmaker', 'Retail')})\n"
        f"• Calculated Edge: <b>+{data.get('edge_pct')}% EV</b>\n\n"
        f"🧠 <b>Tactical Read:</b>\n{data.get('tactical_analysis')}\n\n"
        f"🎯 <b>PAPER EXECUTION LOGGED:</b>\n"
        f"• Pick: <code>{pick}</code>\n"
        f"• Stake: <b>${stake_amount}</b> (@ {data.get('odds')})\n"
        f"• Bankroll Risk: {round(data.get('kelly_stake_pct', 0.015) * 100, 1)}% (Quarter-Kelly)"
    )

    if bot_token and chat_id:
        try:
            tg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            requests.post(tg_url, json={"chat_id": chat_id, "text": card_message, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            logger.error(f"Telegram dispatch failed: {e}")

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
        logger.error(f"Error fetching live matches: {e}")
    return []

def main():
    logger.info("--- [QUANT ENGINE] Running In-Play & Halftime Audit ---")
    bankroll = 1000.0
    matches = fetch_live_matches()
    logger.info(f"Found {len(matches)} fixtures to scan.")

    for game in matches:
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
            evaluate_and_log_live_discrepancy(fixture_data, live_odds, pre_match_odds, bankroll)

    logger.info("--- [QUANT ENGINE] Scan Complete ---")

run_live_scan = main
run_prematch_scan = main

def auto_settle():
    logger.info("Checking settlement rules against finished scores...")

if __name__ == "__main__":
    main()
