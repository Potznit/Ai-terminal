# --- BOOKMAKER MAPPING ---
# Display Name -> The Odds API bookmaker key
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

# --- TOOL UPDATE: ACCEPTS SELECTED BOOKMAKERS ---
def scan_sports_odds(sport_key: str, selected_books_str: str) -> str:
    """Fetches odds specifically filtered by the selected bookmakers."""
    if not odds_api_key:
        return json.dumps([
            {
                "matchup": "Arsenal vs Chelsea",
                "simulated_notice": "No live Odds API key provided.",
                "odds_comparison": {
                    "Pinnacle (Benchmark)": {"Arsenal": 1.75, "Draw": 3.80, "Chelsea": 4.90},
                    "Betfair": {"Arsenal": 1.92, "Draw": 3.65, "Chelsea": 4.20}
                }
            }
        ])

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": odds_api_key,
        "regions": "eu,uk,us",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "bookmakers": selected_books_str,  # <-- Filters only the chosen platforms
    }
    
    res = requests.get(url, params=params, timeout=10)
    if res.status_code != 200:
        return json.dumps({"error": f"API returned status {res.status_code}"})
    
    games = res.json()[:4]
    parsed_matches = []
    for g in games:
        parsed_matches.append({
            "matchup": f"{g.get('home_team')} vs {g.get('away_team')}",
            "start_time": g.get("commence_time"),
            "bookmaker_lines": [
                {
                    "bookmaker": b.get("title"),
                    "lines": {o.get("name"): o.get("price") for o in b.get("markets", [{}])[0].get("outcomes", [])}
                }
                for b in g.get("bookmakers", [])
            ]
        })
    return json.dumps(parsed_matches)

# --- UI SECTION ---
with tab_sports:
    st.subheader("Expected Value (+EV) Sports Scanner")
    
    col_sport, col_edge = st.columns(2)
    sport = col_sport.selectbox(
        "League",
        ["soccer_epl", "soccer_spain_la_liga", "soccer_uefa_champs_league", "basketball_nba"],
        index=0
    )
    min_edge = col_edge.slider("Minimum Edge (+EV %)", 1.0, 10.0, 3.0, step=0.5)

    # Multi-select button/dropdown for betting companies
    chosen_labels = st.multiselect(
        "Select Sportsbooks to Monitor",
        options=list(AVAILABLE_BOOKMAKERS.keys()),
        default=["Betfair (Exchange/Sportsbook)", "Pinnacle (Sharp Benchmark)", "Bet365"]
    )
    
    # Map friendly names back to API keys
    chosen_keys = [AVAILABLE_BOOKMAKERS[label] for label in chosen_labels]
    selected_books_str = ",".join(chosen_keys)

    notify = st.checkbox("Send Alert to Telegram", value=True)

    if st.button("Run +EV Scan", type="primary", use_container_width=True):
        if not chosen_keys:
            st.error("Please select at least one bookmaker.")
        else:
            with st.spinner("Fetching lines from selected bookmakers..."):
                config = types.GenerateContentConfig(
                    system_instruction=(
                        "You are a quantitative betting model. Use Pinnacle as the fair-price benchmark to derive true probability, "
                        "then check the other user-selected bookmakers (e.g. Betfair) to find discrepancies where the payout offers positive Expected Value (+EV).\n"
                        "Format response with:\n"
                        "- Matchup\n"
                        "- Value Bet (Team)\n"
                        "- Bookmaker Offering the Line\n"
                        "- Fair Odds vs Bookmaker Odds\n"
                        "- Calculated EV %"
                    ),
                    tools=[scan_sports_odds],
                    temperature=0.1,
                )
                
                prompt = (
                    f"Scan {sport} using only these bookmakers: {selected_books_str}. "
                    f"Return only bets offering at least {min_edge}% +EV."
                )
                
                analysis = client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=prompt,
                    config=config,
                )
                
                st.markdown(analysis.text)
                
                if notify and telegram_token:
                    send_telegram_alert(f"🚨 *+EV Alert:*\n\n{analysis.text}")
