
import sys
import logging
from apscheduler.schedulers.blocking import BlockingScheduler

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("QuantEngine")

# Import the existing routines from your worker
import worker

sched = BlockingScheduler()

# 1. Halftime & In-Play Scan: Runs every 2 minutes
@sched.scheduled_job('interval', minutes=2)
def live_inplay_tick():
    logger.info("Executing Live Halftime / In-Play Scan...")
    try:
        # Calls the live scanner routine in worker.py
        if hasattr(worker, "run_live_scan"):
            worker.run_live_scan()
        else:
            worker.main()
    except Exception as e:
        logger.error(f"Error during live scan: {e}")

# 2. Pre-Match 3-Horizon Scan: Runs every 2 hours
@sched.scheduled_job('interval', hours=2)
def prematch_tick():
    logger.info("Executing Pre-Match 3-Horizon Audit...")
    try:
        if hasattr(worker, "run_prematch_scan"):
            worker.run_prematch_scan()
        else:
            worker.main()
    except Exception as e:
        logger.error(f"Error during pre-match scan: {e}")

# 3. Match Settlement Check: Runs every 15 minutes
@sched.scheduled_job('interval', minutes=15)
def settlement_tick():
    logger.info("Checking match scores and auto-settling...")
    try:
        if hasattr(worker, "auto_settle"):
            worker.auto_settle()
    except Exception as e:
        logger.error(f"Error during settlement run: {e}")

if __name__ == "__main__":
    logger.info("Starting Autonomous Betting Service...")
    # Run once immediately on startup
    try:
        worker.main()
    except Exception as e:
        logger.error(f"Initial run error: {e}")
    
    # Start the persistent background scheduler
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping scheduler...")
        sys.exit(0)
