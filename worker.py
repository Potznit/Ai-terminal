import os
import json
import logging
import base64
import uuid
import time
from datetime import datetime, timezone
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("QuantWorker")

CSV_PATH = "paper_trades.csv"
STARTING_BANKROLL = 1000.0

TIER1_SOCCER_LEAGUES = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_brazil_campeonato",
    "soccer_argentina_primera_division",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "soccer_efl_champ",
    "soccer_mexico_ligamx",
    "soccer_usa_mls"
]

SCHEMA_COLUMNS = [
    "ID", "Model_Tag", "Kickoff_UTC", "League", "Matchup", 
    "Market", "Pick", "Bookmaker", "Odds", "Edge_Pct", 
    "Stake", "Status", "P_L"
]

depleted_logged_this_cycle = set()
last_settle_timestamp = 0

def load_latest_csv_from_github():
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
            logger.info("Synced latest paper_trades.csv from GitHub.")
    except Exception as e:
        logger.error(f"Error fetching CSV from GitHub: {e}")

def ensure_ledger_initialized():
    load_latest_csv_from_github()
    if not os.path.exists(CSV_PATH):
        df = pd.DataFrame(columns=SCHEMA_COLUMNS)
        df.to_csv(CSV_PATH, index=False)

ensure_ledger_initialized()

def sync_csv_to_github():
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

def get_model_available_bank(model_tag: str, starting_capital: float = STARTING_BANKROLL) -> float:
    if not os.path.exists(CSV_PATH):
        return starting_capital
    try:
        df = pd.read_csv(CSV_PATH)
        if df.empty or "Model_Tag" not in df.columns or "Status" not in df.columns:
            return starting_capital

        m_trades = df[df["Model_Tag"] == model_tag]
        if m_trades.empty:
            return starting_capital

        settled = m_trades[m_trades["Status"].isin(["WON", "LOST", "PUSH"])]
        pending = m_trades[m_trades["Status"] == "PENDING"]

        net_settled_pl = float(settled["P_L"].sum()) if not settled.empty else 0.0
        active_staked = float(pending["Stake"].sum()) if not pending.empty else 0.0

        total_equity = starting_capital + net_settled_pl
        return total_equity - active_staked
    except Exception:
        return starting_capital

def has_existing_bet(matchup: str, model_tag: str) -> bool:
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
    except Exception:
        return False

def query_gemini_ai(prompt: str, api_key: str) -> dict:
    if not api_key:
        logger.warning("[Gemini AI] No GEMINI_API_KEY set in environment variables.")
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json"
        }
    }

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=12)
        if res.status_code == 200:
            raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw_text.strip())
        else:
            logger.error(f"[Gemini API Error] HTTP {res.status_code}: {res.text}")
    except Exception as e:
        logger.error(f"[Gemini API Exception] Failed to query: {e}")

    return None

def fetch_live_match_score(sport_key: str, home_team: str, away_team: str, api_key: str) -> str:
    """Queries live match scores if fixture is in-play during halftime interval."""
    if not api_key or not sport_key:
        return None
    try:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/?apiKey={api_key}&daysFrom=1"
        res = requests.get(url, timeout=8)
        if res.status_code == 200:
            games = res.json()
            h_low = home_team.lower()
            a_low = away_team.lower()
            for g in games:
                gh = g.get("home_team", "").lower()
                ga = g.get("away_team", "").lower()
                if (h_low in gh or gh in h_low) and (a_low in ga or ga in a_low):
                    scores = g.get("scores")
                    if scores and len(scores) >= 2:
                        hs = next((s["score"] for s in scores if s["name"] == g.get("home_team")), None)
                        as_ = next((s["score"] for s in scores if s["name"] == g.get("away_team")), None)
                        if hs is not None and as_ is not None:
                            return f"{hs} - {as_}"
    except Exception as e:
        logger.debug(f"Live score lookup skipped: {e}")
    return None

def evaluate_and_log_discrepancy(fixture_data, live_odds, pre_match_odds, model_tag, bankroll=1000.0) -> bool:
    global depleted_logged_this_cycle
    matchup = f"{fixture_data['home']} vs {fixture_data['away']}"

    available_in_bank = get_model_available_bank(model_tag, bankroll)
    target_stake = 15.00

    if available_in_bank < target_stake:
        if model_tag not in depleted_logged_this_cycle:
            logger.info(f"[{model_tag}] Bank depleted (Available: ${available_in_bank:.2f}). Pausing trades until settlements.")
            depleted_logged_this_cycle.add(model_tag)
        return False

    if has_existing_bet(matchup, model_tag):
        return False

    api_key = os.environ.get("GEMINI_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    prompt = f"""
    You are an elite sports quantitative trading analyst.
    Evaluate the following soccer market discrepancy:
    Model Horizon: {model_tag}
    Match: {matchup} ({fixture_data.get('league', 'Soccer')})
    Current Match State: {fixture_data.get('score')}
    Target Pick: {fixture_data['target_pick']}
    Sharp Benchmark (Pinnacle/Exchange): {live_odds.get('pinnacle')}
    Retail Outlier ({live_odds.get('bookmaker')}): {live_odds.get('retail_odds')}
    Calculated Edge: +{fixture_data.get('raw_edge')}% EV

    Provide a concise, highly analytical 2-sentence tactical breakdown explaining why this market gap offers positive expectancy.
    Return STRICT JSON ONLY:
    {{
      "is_valid_ev": true,
      "edge_pct": {fixture_data.get('raw_edge')},
      "recommended_pick": "{fixture_data['target_pick']}",
      "odds": {live_odds.get('retail_odds')},
      "tactical_analysis": "<Your 2-sentence tactical analysis here>",
      "kelly_stake_pct": 0.015
    }}
    """

    data = query_gemini_ai(prompt, api_key) if api_key else None

    if not data:
        sharp = live_odds.get("pinnacle")
        retail = live_odds.get("retail_odds")
        calculated_edge = round(((retail / sharp) - 1.0) * 100, 1)
        data = {
            "is_valid_ev": calculated_edge >= 3.0,
            "edge_pct": calculated_edge,
            "recommended_pick": fixture_data["target_pick"],
            "odds": retail,
            "tactical_analysis": f"Quantitative price divergence: Soft line {retail} vs Benchmark {sharp}.",
            "kelly_stake_pct": 0.015
        }

    if not data.get("is_valid_ev"):
        return False

    pick = data.get("recommended_pick")
    stake_amount = target_stake

    new_trade = {
        "ID": str(uuid.uuid4())[:8],
        "Model_Tag": model_tag,
        "Kickoff_UTC": fixture_data.get("kickoff"),
        "League": fixture_data.get("league", "Global Soccer"),
        "Matchup": matchup,
        "Market": "Halftime In-Play" if model_tag == "HALFTIME_LIVE" else "Pre-Match +EV",
        "Pick": pick,
        "Bookmaker": live_odds.get("bookmaker", "Retail Book"),
        "Odds": float(data.get("odds")),
        "Edge_Pct": float(data.get("edge_pct")),
        "Stake": float(stake_amount),
        "Status": "PENDING",
        "P_L": 0.0
    }

    try:
        df = pd.read_csv(CSV_PATH) if os.path.exists(CSV_PATH) else pd.DataFrame(columns=SCHEMA_COLUMNS)
        df = pd.concat([df, pd.DataFrame([new_trade])], ignore_index=True)
        df.to_csv(CSV_PATH, index=False)
        remaining_bank = available_in_bank - stake_amount
        logger.info(f"Logged [{model_tag}] bet: ${stake_amount} on {pick} ({matchup}) | Bank Remaining: ${remaining_bank:.2f}")
    except Exception as e:
        logger.error(f"Failed to record bet: {e}")
        return False

    card_message = (
        f"🚨 <b>[{model_tag}] VALUE DISCREPANCY</b>\n\n"
        f"⚽ <b>{matchup}</b>\n"
        f"⏱ <b>State:</b> {fixture_data.get('score')}\n\n"
        f"📊 <b>Market Discrepancy:</b>\n"
        f"• Retail Outlier: <b>{data.get('odds')}</b> ({live_odds.get('bookmaker')})\n"
        f"• Sharp Baseline: <b>{live_odds.get('pinnacle')}</b>\n"
        f"• Calculated Edge: <b>+{data.get('edge_pct')}% EV</b>\n\n"
        f"🧠 <b>Tactical Read:</b>\n{data.get('tactical_analysis')}\n\n"
        f"🎯 <b>LOGGED POSITION:</b>\n"
        f"• Pick: <code>{pick}</code>\n"
        f"• Stake: <b>${stake_amount}</b> (@ {data.get('odds')})\n"
        f"• Bank Remaining: <b>${available_in_bank - stake_amount:.2f}</b>"
    )

    if bot_token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": card_message, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception:
            pass

    return True

def auto_settle_targeted(force=False):
    """Hourly targeted settlement to avoid burning credits on scores."""
    global last_settle_timestamp
    now_ts = time.time()
    
    if not force and (now_ts - last_settle_timestamp < 3600):
        return

    last_settle_timestamp = now_ts
    api_key = os.environ.get("ODDS_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not api_key or not os.path.exists(CSV_PATH):
        return

    try:
        df = pd.read_csv(CSV_PATH)
        if df.empty or "Status" not in df.columns:
            return

        pending_df = df[df["Status"] == "PENDING"]
        if pending_df.empty:
            return

        logger.info(f"[auto_settle] Checking settlements for {len(pending_df)} pending positions...")

        completed_games = []
        for l_key in TIER1_SOCCER_LEAGUES:
            sc_url = f"https://api.the-odds-api.com/v4/sports/{l_key}/scores/?apiKey={api_key}&daysFrom=3"
            try:
                res = requests.get(sc_url, timeout=8)
                if res.status_code == 200:
                    events = res.json()
                    if isinstance(events, list):
                        for ev in events:
                            if ev.get("completed") and ev.get("scores"):
                                completed_games.append(ev)
            except Exception:
                pass
            time.sleep(0.08)

        if not completed_games:
            return

        settled_count = 0
        for idx in pending_df.index:
            raw_matchup = str(df.at[idx, "Matchup"]).strip()
            pick = str(df.at[idx, "Pick"]).strip()
            stake = float(df.at[idx, "Stake"])
            odds = float(df.at[idx, "Odds"])
            model_tag = str(df.at[idx, "Model_Tag"])

            if " vs " not in raw_matchup:
                continue

            t_a, t_b = [t.strip().lower() for t in raw_matchup.split(" vs ", 1)]

            for game in completed_games:
                g_home = str(game.get("home_team", "")).lower()
                g_away = str(game.get("away_team", "")).lower()

                match_found = (
                    (t_a in g_home or g_home in t_a or any(w in g_home for w in t_a.split() if len(w) > 3)) and
                    (t_b in g_away or g_away in t_b or any(w in g_away for w in t_b.split() if len(w) > 3))
                )
                if not match_found:
                    continue

                scores = game.get("scores")
                if not scores or len(scores) < 2:
                    continue

                home_score = None
                away_score = None
                for s in scores:
                    if s.get("name") == game.get("home_team"):
                        home_score = int(s.get("score", 0))
                    elif s.get("name") == game.get("away_team"):
                        away_score = int(s.get("score", 0))

                if home_score is None or away_score is None:
                    continue

                if home_score > away_score:
                    winner = game["home_team"]
                elif away_score > home_score:
                    winner = game["away_team"]
                else:
                    winner = "Draw"

                is_draw_pick = "draw" in pick.lower()
                is_win = (winner.lower() in pick.lower()) and not (winner == "Draw" and not is_draw_pick)
                is_push = ("draw no bet" in pick.lower() and winner == "Draw")

                if is_win:
                    profit = round(stake * (odds - 1.0), 2)
                    df.at[idx, "Status"] = "WON"
                    df.at[idx, "P_L"] = profit
                    msg = f"✅ <b>[{model_tag}] BET WON</b>\n\n⚽ {raw_matchup}\nFinal: <b>{home_score} - {away_score}</b>\nProfit: <b>+${profit}</b>"
                elif is_push:
                    df.at[idx, "Status"] = "PUSH"
                    df.at[idx, "P_L"] = 0.0
                    msg = f"🔄 <b>[{model_tag}] BET PUSHED</b>\n\n⚽ {raw_matchup}\nFinal: <b>{home_score} - {away_score}</b>"
                else:
                    df.at[idx, "Status"] = "LOST"
                    df.at[idx, "P_L"] = -stake
                    msg = f"❌ <b>[{model_tag}] BET LOST</b>\n\n⚽ {raw_matchup}\nFinal: <b>{home_score} - {away_score}</b>\nLoss: <b>-${stake}</b>"

                settled_count += 1
                logger.info(f"[auto_settle] Settled {raw_matchup} -> {df.at[idx, 'Status']} (P/L: {df.at[idx, 'P_L']})")

                if bot_token and chat_id:
                    try:
                        requests.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                            timeout=10
                        )
                    except Exception:
                        pass
                break

        if settled_count > 0:
            df.to_csv(CSV_PATH, index=False)
            sync_csv_to_github()
            logger.info(f"[auto_settle] Settled {settled_count} bets and synced to GitHub.")

    except Exception as e:
        logger.error(f"[auto_settle] Error: {e}")

def fetch_tier1_soccer_odds():
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        logger.error("[ODDS API] ODDS_API_KEY is not set.")
        return []

    probe = requests.get(f"https://api.the-odds-api.com/v4/sports/?apiKey={api_key}", timeout=10)
    remaining = probe.headers.get("x-requests-remaining")
    used = probe.headers.get("x-requests-used")
    logger.info(f"[ODDS API QUOTA] Remaining: {remaining} | Used: {used}")

    if probe.status_code != 200:
        logger.error(f"[ODDS API ERROR] HTTP {probe.status_code}: {probe.text}")
        return []

    all_odds = []
    for skey in TIER1_SOCCER_LEAGUES:
        url = f"https://api.the-odds-api.com/v4/sports/{skey}/odds/?apiKey={api_key}&regions=eu,uk&markets=h2h&oddsFormat=decimal"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                all_odds.extend(res.json())
            elif res.status_code in [401, 429]:
                logger.error(f"[ODDS API LIMIT] HTTP {res.status_code} on {skey}: {res.text}")
                break
        except Exception:
            pass
        time.sleep(0.08)

    return all_odds

def run_scan_cycle() -> int:
    """Runs the audit and calculates the adaptive sleep duration (in seconds) based on match timing."""
    global depleted_logged_this_cycle
    depleted_logged_this_cycle = set()

    logger.info("--- [QUANT ENGINE] Running Multi-Horizon Audit ---")
    auto_settle_targeted()

    bankroll = STARTING_BANKROLL
    matches = fetch_tier1_soccer_odds()
    logger.info(f"Loaded {len(matches)} fixtures across Tier-1 leagues.")

    now = datetime.now(timezone.utc)
    new_bets_logged = False
    halftime_matches_count = 0

    has_live_game = False
    min_minutes_to_next_kickoff = 999999
    odds_api_key = os.environ.get("ODDS_API_KEY")

    for game in matches:
        commence_time_str = game.get("commence_time")
        if not commence_time_str:
            continue

        commence_time = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
        minutes_since_kickoff = (now - commence_time).total_seconds() / 60

        if 0 <= minutes_since_kickoff <= 115:
            has_live_game = True

        if minutes_since_kickoff < 0:
            mins_until = -minutes_since_kickoff
            if mins_until < min_minutes_to_next_kickoff:
                min_minutes_to_next_kickoff = mins_until

        is_halftime = 45 <= minutes_since_kickoff <= 65
        is_future = minutes_since_kickoff < 0

        if not is_halftime and not is_future:
            continue

        hours_to_kickoff = -minutes_since_kickoff / 60

        if hours_to_kickoff > 144:
            continue

        bookmakers = {b["key"]: b for b in game.get("bookmakers", [])}

        if is_halftime:
            halftime_matches_count += 1
            has_pinny = "pinnacle" in bookmakers
            logger.info(f"[HALFTIME AUDIT] Match in break: {game.get('home_team')} vs {game.get('away_team')} (Elapsed: ~{round(minutes_since_kickoff)}m) | Pinnacle live: {has_pinny} | Books: {len(bookmakers)}")

        sharp_key = "pinnacle" if "pinnacle" in bookmakers else None
        if not sharp_key and is_halftime:
            for alt in ["betfair_ex_uk", "betfair_ex_eu", "betonlineag", "unibet_eu"]:
                if alt in bookmakers:
                    sharp_key = alt
                    break

        if not sharp_key:
            continue

        sharp_odds = {}
        for m in bookmakers[sharp_key].get("markets", []):
            if m["key"] == "h2h":
                sharp_odds = {o["name"]: o["price"] for o in m["outcomes"]}

        if not sharp_odds:
            continue

        if is_halftime:
            assigned_tag = "HALFTIME_LIVE"
            sport_key = game.get("sport_key")
            live_score = fetch_live_match_score(sport_key, game.get("home_team"), game.get("away_team"), odds_api_key)
            if live_score:
                state_label = f"Halftime Break ({live_score}, ~{round(minutes_since_kickoff)}m)"
            else:
                state_label = f"Halftime Break (~{round(minutes_since_kickoff)}m)"
        elif hours_to_kickoff > 48:
            assigned_tag = "EARLY_BIRD"
            state_label = f"Pre-Match ({round(hours_to_kickoff)}h to KO)"
        elif hours_to_kickoff > 12:
            assigned_tag = "CORE_EV"
            state_label = f"Pre-Match ({round(hours_to_kickoff)}h to KO)"
        else:
            assigned_tag = "LATE_STEAM"
            state_label = f"Pre-Match ({round(hours_to_kickoff)}h to KO)"

        for b_key, b_data in bookmakers.items():
            if b_key == sharp_key:
                continue
            for m in b_data.get("markets", []):
                if m["key"] == "h2h":
                    for outcome in m["outcomes"]:
                        target_side = outcome["name"]
                        retail_price = outcome["price"]
                        sharp_price = sharp_odds.get(target_side)

                        if sharp_price and retail_price > sharp_price:
                            edge = round(((retail_price / sharp_price) - 1.0) * 100, 1)
                            
                            if edge >= 3.0:
                                fixture_data = {
                                    "home": game.get("home_team"),
                                    "away": game.get("away_team"),
                                    "target_pick": target_side,
                                    "raw_edge": edge,
                                    "score": state_label,
                                    "league": game.get("sport_title", "Global Soccer"),
                                    "kickoff": commence_time_str
                                }
                                live_odds = {
                                    "pinnacle": sharp_price,
                                    "bookmaker": b_data.get("title", b_key),
                                    "retail_odds": retail_price
                                }
                                pre_match_odds = {"favorite_prob": round((1.0 / sharp_price) * 100)}

                                logged = evaluate_and_log_discrepancy(
                                    fixture_data, live_odds, pre_match_odds, 
                                    model_tag=assigned_tag, bankroll=bankroll
                                )
                                if logged:
                                    new_bets_logged = True

    if halftime_matches_count == 0:
        logger.info("[HALFTIME AUDIT] No matches currently in the 45-65 min halftime window.")

    if new_bets_logged:
        sync_csv_to_github()

    logger.info("--- [QUANT ENGINE] Scan Complete ---")

    if has_live_game:
        adaptive_sleep = 480
        logger.info("[ADAPTIVE SLEEP] Active in-play matches detected. Sleeping 8 minutes.")
    elif min_minutes_to_next_kickoff <= 60:
        adaptive_sleep = 600
        logger.info(f"[ADAPTIVE SLEEP] Kickoff soon ({round(min_minutes_to_next_kickoff)}m). Sleeping 10 minutes.")
    elif min_minutes_to_next_kickoff < 999999:
        target_sleep = max(900, min((min_minutes_to_next_kickoff - 30) * 60, 4500))
        adaptive_sleep = int(target_sleep)
        logger.info(f"[ADAPTIVE SLEEP] Dead zone: next kickoff in {round(min_minutes_to_next_kickoff / 60, 1)}h. Sleeping {round(adaptive_sleep / 60)} minutes.")
    else:
        adaptive_sleep = 1800
        logger.info("[ADAPTIVE SLEEP] No fixtures found. Sleeping 30 minutes.")

    return adaptive_sleep

if __name__ == "__main__":
    logger.info("Starting Quant Worker Daemon...")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": "🟢 <b>Quant Engine Online</b>: Dynamic Adaptive Scheduling active.", "parse_mode": "HTML"},
                timeout=10
            )
        except Exception:
            pass

    auto_settle_targeted(force=True)

    while True:
        try:
            sleep_duration = run_scan_cycle()
        except Exception as e:
            logger.error(f"Error during scan cycle: {e}")
            sleep_duration = 600

        time.sleep(sleep_duration)
