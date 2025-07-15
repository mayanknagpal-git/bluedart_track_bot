# bot.py

import logging
import requests
import os
import json
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, 
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio
import streamlit as st

# --- CONFIG ---
TELEGRAM_BOT_TOKEN = st.secrets["TELEGRAM_BOT_TOKEN"]
TRACKING_DATA_FILE = "tracking_data.json"

# --- DATA STORAGE ---
# {user_id: {awb: last_status}}
user_trackings = {}

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- PERSISTENCE FUNCTIONS ---
def load_tracking_data():
    """Load tracking data from JSON file"""
    global user_trackings
    try:
        if os.path.exists(TRACKING_DATA_FILE):
            with open(TRACKING_DATA_FILE, 'r') as f:
                data = json.load(f)
                # Convert string keys back to integers for user_id
                user_trackings = {int(k): v for k, v in data.items()}
                logger.info(f"Loaded {len(user_trackings)} user tracking records from {TRACKING_DATA_FILE}")
        else:
            user_trackings = {}
            logger.info(f"No existing tracking data file found. Starting fresh.")
    except Exception as e:
        logger.error(f"Error loading tracking data: {e}")
        user_trackings = {}

def save_tracking_data():
    """Save tracking data to JSON file"""
    try:
        # Convert integer keys to strings for JSON serialization
        data_to_save = {str(k): v for k, v in user_trackings.items()}
        with open(TRACKING_DATA_FILE, 'w') as f:
            json.dump(data_to_save, f, indent=2)
        logger.debug(f"Saved tracking data to {TRACKING_DATA_FILE}")
    except Exception as e:
        logger.error(f"Error saving tracking data: {e}")

# --- BLUEDART SCRAPER ---
def get_bluedart_status(awb):
    url = f"https://www.bluedart.com/trackdartresultthirdparty?trackFor=0&trackNo={awb}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        
        # Look for tracking information in various possible locations
        # Method 1: Look for table with tracking data
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    cell_texts = [cell.get_text(strip=True) for cell in cells]
                    # Look for status-related keywords
                    for i, text in enumerate(cell_texts):
                        if any(keyword in text.lower() for keyword in ["status", "activity", "scan"]):
                            if i + 1 < len(cell_texts) and cell_texts[i + 1]:
                                return cell_texts[i + 1]
        
        # Method 2: Look for div or span elements with tracking info
        tracking_divs = soup.find_all(["div", "span"], class_=lambda x: x and any(keyword in x.lower() for keyword in ["status", "track", "scan", "activity"]))
        for div in tracking_divs:
            text = div.get_text(strip=True)
            if text and len(text) > 5 and not any(skip in text.lower() for skip in ["javascript", "window", "function"]):
                return text
        
        # Method 3: Look for specific tracking status patterns
        all_text = soup.get_text()
        lines = [line.strip() for line in all_text.split('\n') if line.strip()]
        
        # Common BlueDart status messages
        status_patterns = [
            "Shipment Delivered",
            "Out for Delivery",
            "Pickup Employee Is Out To P/U Shipment",
            "Shipment Picked Up",
            "In Transit",
            "Reached at Destination",
            "Shipment Booked",
            "Ready for Pickup",
            "Delivered",
            "Picked Up"
        ]
        
        for line in lines:
            for pattern in status_patterns:
                if pattern.lower() in line.lower():
                    return line
        
        # Method 4: Look for the most recent tracking entry
        for line in lines:
            if len(line) > 10 and len(line) < 200:  # Reasonable length for status
                if any(keyword in line.lower() for keyword in ["delivered", "pickup", "transit", "booked", "scan"]):
                    if not any(skip in line.lower() for skip in ["javascript", "window", "function", "analytics"]):
                        return line
        
        # If no specific status found, check if AWB is valid
        if "invalid" in all_text.lower() or "not found" in all_text.lower():
            return "Invalid AWB or shipment not found"
        
        return "Status not found - please check AWB number"
        
    except Exception as e:
        logger.error(f"Error scraping AWB {awb}: {e}")
        return "Error fetching status"

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        
        await update.message.reply_text(
            "Welcome! Use /add <AWB> to track a shipment, /remove <AWB> to stop tracking, /list to see your tracked AWBs."
        )
    except Exception as e:
        logger.error(f"Error in start command: {e}")

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        
        user_id = update.effective_user.id
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /add <AWB>")
            return
        awb = context.args[0]
        status = get_bluedart_status(awb)
        if user_id not in user_trackings:
            user_trackings[user_id] = {}
        user_trackings[user_id][awb] = status
        save_tracking_data()  # Save after adding
        await update.message.reply_text(f"Tracking AWB {awb}. Current status: {status}")
    except Exception as e:
        logger.error(f"Error in add command: {e}")
        if update.message:
            await update.message.reply_text("Sorry, there was an error adding the AWB for tracking.")

async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        
        user_id = update.effective_user.id
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /remove <AWB>")
            return
        awb = context.args[0]
        if user_id in user_trackings and awb in user_trackings[user_id]:
            del user_trackings[user_id][awb]
            if not user_trackings[user_id]:  # Remove user if no AWBs left
                del user_trackings[user_id]
            save_tracking_data()  # Save after removing
            await update.message.reply_text(f"Stopped tracking AWB {awb}.")
        else:
            await update.message.reply_text(f"AWB {awb} not found in your tracking list.")
    except Exception as e:
        logger.error(f"Error in remove command: {e}")
        if update.message:
            await update.message.reply_text("Sorry, there was an error removing the AWB.")

async def list_awbs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        
        user_id = update.effective_user.id
        if user_id not in user_trackings or not user_trackings[user_id]:
            await update.message.reply_text("You are not tracking any AWBs.")
            return
        msg = "Your tracked AWBs:\n"
        for awb, status in user_trackings[user_id].items():
            msg += f"{awb}: {status}\n"
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in list command: {e}")
        if update.message:
            await update.message.reply_text("Sorry, there was an error listing your AWBs.")

# --- PERIODIC CHECK ---
async def check_statuses(app):
    changes_made = False
    for user_id, awbs in user_trackings.items():
        for awb, last_status in list(awbs.items()):
            new_status = get_bluedart_status(awb)
            if new_status != last_status:
                user_trackings[user_id][awb] = new_status
                changes_made = True
                logger.info(f"Status change for AWB {awb}: {last_status} -> {new_status}")
                try:
                    await app.bot.send_message(
                        chat_id=user_id,
                        text=f"Status update for AWB {awb}: {new_status}"
                    )
                except Exception as e:
                    logger.error(f"Failed to send message to {user_id}: {e}")
    
    # Save data if any changes were made
    if changes_made:
        save_tracking_data()

# --- MAIN ---
async def main():
    # Load existing tracking data
    load_tracking_data()
    
    # Log startup summary
    total_users = len(user_trackings)
    total_awbs = sum(len(awbs) for awbs in user_trackings.values())
    if total_users > 0:
        logger.info(f"Resuming tracking for {total_users} users with {total_awbs} AWB(s)")
    else:
        logger.info("Starting fresh with no existing tracking data")
    
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("list", list_awbs))

    # Initialize the app first
    await app.initialize()
    
    # Scheduler for periodic status check and backup
    scheduler = AsyncIOScheduler()
    
    # Create a wrapper function for the async check_statuses
    async def status_check_wrapper():
        await check_statuses(app)
    
    scheduler.add_job(status_check_wrapper, "interval", minutes=5)
    scheduler.add_job(save_tracking_data, "interval", minutes=30)  # Backup every 30 minutes
    scheduler.start()

    logger.info("Bot started.")
    
    try:
        await app.start()
        await app.updater.start_polling()
        # Keep the bot running
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
    finally:
        # Save data before shutdown
        save_tracking_data()
        scheduler.shutdown()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    st.title("🎈 Bluedart Track Bot")
    st.write(
        "Add tracking with live alerts for Bluedart! Head over to @bluedart_track_bot on telegram."
    )

    asyncio.run(main())
