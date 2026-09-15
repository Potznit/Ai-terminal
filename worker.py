import os
import json
import pandas as pd
from datetime import datetime, timezone
import google.generativeai as genai
import requests

LIVE_MODEL_TAG = "LIVE_WAR_ROOM"
CSV_PATH = "paper_trades.csv"

def evaluate_and_log_live_discrepancy(fixture_data, live_odds, pre_match_odds, bankroll=1000.0):
    """
    Evaluates in-play game-state discrepancies (e.g. Halftime Trailing Favorite),
    prompts Gemini Flash for tactical verification, logs paper bet, and sends Telegram alert.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    # Prompt Gemini for tactical and mathematical confirmation
    prompt = f"""
    You are an elite live in-play sports quantitative trader.
    Evaluate this Halftime market state:
    Match: {fixture_data['home']} vs {fixture_data['away']}
    Current Score: {fixture_data.get('score', '0 - 1')} (Halftime)
    Pre-Match Implied Probability for {fixture_data['favorite']}: {pre_match_odds.get('favorite_prob', 65)}%
    Live 2nd-Half / Match Odds:
    - Sharp Benchmark (Pinnacle): {live_odds.get('pinnacle', 2.10)}
    - Target Retail Book ({live_odds.get('bookmaker', 'Bet365')}): {live_odds.get('retail_odds', 2.35)}

    Determine if a true +EV discrepancy exists. Return STRICT JSON only:
    {{
      "is_valid_ev": true,
      "edge_pct": 4.2,
      "recommended_pick": "{fixture_data['favorite']} 2nd-Half ML",
      "odds": {live_odds.get('retail_odds', 2.35)},
      "tactical_analysis": "2-3 concise sentences on retail score-panic vs underlying possession dominance.",
      "kelly_stake_pct": 0.015
    }}
    """
    
    try:
        response = model.generate_content(prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
    except Exception as e:
        print(f"Error parsing Gemini live analysis: {e}")
        return

    if not data.get("is_valid_ev"):
        print(f"No verifiable edge on {fixture_data['home']} vs {fixture_data['away']}.")
        return

    # 1. Calculate Quarter-Kelly Fake Money Stake
    stake_amount = round(bankroll * data.get("kelly_stake_pct", 0.01), 2)

    # 2. Append Paper Bet to Ledger
    new_trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_tag": LIVE_MODEL_TAG,
        "league": fixture_data.get("league", "Global Soccer"),
        "matchup": f"{fixture_data['home']} vs {fixture_data['away']}",
        "market": "In-Play Halftime Discrepancy",
        "pick": data["recommended_pick"],
        "bookmaker": live_odds.get("bookmaker", "Retail Soft"),
        "odds": data["odds"],
        "edge_pct": data["edge_pct"],
        "stake": stake_amount,
        "status": "PENDING",
        "kickoff": fixture_data.get("kickoff", datetime.now(timezone.utc).isoformat()),
        "p_l": 0.0
    }

    if os.path.exists(CSV_PATH):
        df = pd.read_csv(CSV_PATH)
        df = pd.concat([df, pd.DataFrame([new_trade])], ignore_index=True)
    else:
        df = pd.DataFrame([new_trade])
    
    df.to_csv(CSV_PATH, index=False)
    print(f"Logged live paper trade: ${stake_amount} on {data['recommended_pick']}")

    # 3. Dispatch Live Telegram War Room Card
    card_message = (
        f"🚨 <b>[LIVE WAR ROOM] HALFTIME DISCREPANCY</b>\n\n"
        f"⚽ <b>{fixture_data['home']} vs {fixture_data['away']}</b>\n"
        f"⏱ <b>Score:</b> {fixture_data.get('score', '0 - 1')} (Halftime)\n\n"
        f"📊 <b>Market Discrepancy:</b>\n"
        f"• Pre-Match Favorite: {fixture_data['favorite']}\n"
        f"• Retail Outlier: {data['odds']} ({live_odds.get('bookmaker', 'Retail')})\n"
        f"• Calculated Edge: <b>+{data['edge_pct']}% EV</b>\n\n"
        f"🧠 <b>Tactical Read:</b>\n{data['tactical_analysis']}\n\n"
        f"🎯 <b>PAPER EXECUTION LOGGED:</b>\n"
        f"• Pick: <code>{data['recommended_pick']}</code>\n"
        f"• Fake Stake: <b>${stake_amount}</b> (@ {data['odds']})\n"
        f"• Bankroll Risk: {round(data.get('kelly_stake_pct', 0.01) * 100, 1)}% (Quarter-Kelly)"
    )

    if bot_token and chat_id:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": card_message, "parse_mode": "HTML"})
