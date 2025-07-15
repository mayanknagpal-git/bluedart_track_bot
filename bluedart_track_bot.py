# bot.py

import asyncio
import json
import logging
import os

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
)
import streamlit as st

# --- CONFIG ---
TELEGRAM_BOT_TOKEN = st.secrets["TELEGRAM_BOT_TOKEN"]
TRACKING_DATA_FILE = "tracking_data.json"

# --- DATA STORAGE ---
# {user_id: {awb: last_status}}
user_trackings = {}
# Global scheduler instance
scheduler = None

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set logging level to INFO for production, DEBUG for development
logger.setLevel(logging.INFO)

# Create a console handler with a higher log level
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)

# Create a logging format
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)

# Add the handler to the logger
logger.addHandler(ch)

# --- PERSISTENCE FUNCTIONS ---
def load_tracking_data():
    global user_trackings
    try:
        if os.path.exists(TRACKING_DATA_FILE):
            with open(TRACKING_DATA_FILE, 'r') as f:
                data = json.load(f)
                user_trackings = {int(k): v for k, v in data.items()}
                logger.info(f"Loaded {len(user_trackings)} user tracking records from {TRACKING_DATA_FILE}")
        else:
            user_trackings = {}
            logger.info(f"No existing tracking data file found. Starting fresh.")
    except Exception as e:
        logger.error(f"Error loading tracking data: {e}")
        user_trackings = {}

def save_tracking_data():
    try:
        data_to_save = {str(k): v for k, v in user_trackings.items()}
        with open(TRACKING_DATA_FILE, 'w') as f:
            json.dump(data_to_save, f, indent=2)
        logger.debug(f"Saved tracking data to {TRACKING_DATA_FILE}")
    except Exception as e:
        logger.error(f"Error saving tracking data: {e}")

# --- BLUEDART SCRAPER ---
def fetch_bluedart_details(awb):
    """Fetch BlueDart shipment details with improved parsing"""
    url = f"https://www.bluedart.com/trackdartresultthirdparty?trackFor=0&trackNo={awb}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    logger.info(f"Fetching details for AWB: {awb}")
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        logger.debug(f"HTTP response status: {resp.status_code}")
        
        if resp.status_code != 200:
            logger.error(f"HTTP error {resp.status_code} for AWB {awb}")
            return None, []
        
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()

        # Extract main details with improved parsing
        def get_detail(label, alternatives=None):
            """Get detail value with multiple search strategies"""
            if alternatives is None:
                alternatives = []
            
            search_terms = [label] + alternatives
            
            # Method 1: Find by table structure (most reliable)
            for table in soup.find_all("table"):
                for row in table.find_all("tr"):
                    cells = row.find_all(["td", "th"])
                    if len(cells) >= 2:
                        for i, cell in enumerate(cells):
                            cell_text = cell.get_text(strip=True)
                            # Check if any search term matches
                            for term in search_terms:
                                if term.lower() in cell_text.lower():
                                    if i + 1 < len(cells):
                                        value = cells[i + 1].get_text(strip=True)
                                        if value and value != "N/A":
                                            logger.debug(f"Found {label}: {value}")
                                            return value
            
            # Method 2: Find by string content with better parsing
            for term in search_terms:
                elements = soup.find_all(string=lambda s: s and term.lower() in s.lower())
                for el in elements:
                    parent = el.find_parent()
                    if parent:
                        # Look for next sibling with meaningful text
                        for sibling in parent.find_next_siblings():
                            text = sibling.get_text(strip=True)
                            if text and len(text) > 1 and not any(skip in text.lower() for skip in ["window", "function", "script"]):
                                logger.debug(f"Found {label} via string search: {text}")
                                return text
            
            logger.warning(f"Could not find value for: {label}")
            return "N/A"

        # Extract status from the main details table (most reliable)
        def get_latest_status():
            """Get the current status from the main details table"""
            # First priority: Get status from the main details table
            status_from_details = get_detail("Status", ["Current Status", "Shipment Status"])
            if status_from_details != "N/A":
                logger.debug(f"Found status from details table: {status_from_details}")
                return status_from_details
            
            # Second priority: Look for status in a dedicated status field
            # Try to find elements that might contain the status
            for table in soup.find_all("table"):
                for row in table.find_all("tr"):
                    cells = row.find_all(["td", "th"])
                    if len(cells) >= 2:
                        first_cell = cells[0].get_text(strip=True)
                        if first_cell.lower() == "status":
                            status_value = cells[1].get_text(strip=True)
                            if status_value and len(status_value) > 2:
                                logger.debug(f"Found status from dedicated status row: {status_value}")
                                return status_value
            
            # Third priority: Extract from tracking history (most recent entry)
            history_entries = []
            
            # Find the table with "Status and Scans" or similar
            scan_table = None
            for table in soup.find_all("table"):
                table_text = table.get_text().lower()
                if any(keyword in table_text for keyword in ["status and scans", "scan", "activity", "tracking history"]):
                    scan_table = table
                    break
            
            if scan_table:
                rows = scan_table.find_all("tr")
                # Extract all history entries
                for row in rows[1:]:  # Skip header row
                    cols = [td.get_text(strip=True) for td in row.find_all("td")]
                    if len(cols) >= 4:  # Location, Details, Date, Time
                        location, details_text, date, time = cols
                        if details_text and len(details_text) > 3:
                            history_entries.append({
                                'date': date,
                                'time': time,
                                'location': location,
                                'details': details_text
                            })
            
            # Use the most recent entry as fallback
            if history_entries:
                most_recent_status = history_entries[0]['details']
                logger.debug(f"Found status from most recent history: {most_recent_status}")
                return most_recent_status
            
            # Last resort: Look for common status patterns in the entire page
            all_text = soup.get_text()
            lines = [line.strip() for line in all_text.split('\n') if line.strip()]
            
            # Look for status patterns that might appear in the page
            for line in lines:
                if "status" in line.lower() and len(line) > 10 and len(line) < 100:
                    # Clean the line and check if it looks like a status
                    clean_line = ' '.join(line.split())
                    if not any(skip in clean_line.lower() for skip in ["window", "function", "script", "analytics"]):
                        logger.debug(f"Found potential status line: {clean_line}")
                        return clean_line
            
            logger.warning("Could not determine status")
            return "Status not available"

        # Extract details with improved field matching
        status = get_latest_status()
        # Ensure status is a string before calling lower()
        status_str = str(status) if status is not None else "N/A"
        is_delivered = "delivered" in status_str.lower()
        
        details = {
            "Waybill No": awb,
            "Status": status,
            "Pickup Date": get_detail("Pickup Date", ["Pickup", "Pick Up Date", "Booking Date"]),
            "From": get_detail("From", ["Origin", "Source", "Consignor"]),
            "To": get_detail("To", ["Destination", "Consignee", "Delivery To"]),
            "Reference No": get_detail("Reference No", ["Reference", "Ref No", "Customer Reference"]),
        }
        
        # Add delivery-specific fields if delivered
        if is_delivered:
            details["Date of Delivery"] = get_detail("Date of Delivery", ["Delivery Date", "Delivered Date"])
            details["Time of Delivery"] = get_detail("Time of Delivery", ["Delivery Time", "Delivered Time"])
            details["Recipient"] = get_detail("Recipient", ["Received By", "Delivered To"])
            details["Is Delivered"] = True
        else:
            details["Expected Delivery"] = get_detail("Expected Date of Delivery", ["Expected Delivery", "Delivery Date", "EDD"])
            details["Is Delivered"] = False
        
        # Log the extraction results
        logger.info(f"Extracted details for AWB {awb}: {details}")

        # Clean up any details with potential JS or dirty data
        for key in details:
            # Skip processing boolean values
            if key == "Is Delivered":
                continue
            
            if details[key] and isinstance(details[key], str) and any(ph in details[key].lower() for ph in ["window", "function", "script", "analytics", "more\n\t\t\t"]):
                details[key] = "Information unavailable"
            elif not details[key] or details[key] == "":
                details[key] = "N/A"
            # Clean up excessive whitespace and newlines
            elif details[key] != "N/A" and isinstance(details[key], str):
                details[key] = ' '.join(details[key].split())

        # Extract tracking history
        history = []
        # Find the table with "Status and Scans"
        scan_table = None
        for table in soup.find_all("table"):
            if "Status and Scans" in table.get_text():
                scan_table = table
                break
        if not scan_table:
            # fallback: pick the table with 4 columns and date/time
            for table in soup.find_all("table"):
                headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
                if len(headers) == 4 and "date" in headers and "time" in headers:
                    scan_table = table
                    break
        if scan_table:
            rows = scan_table.find_all("tr")[1:]  # skip header
            for row in rows:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cols) == 4:
                    location, details_text, date, time = cols
                    history.append(f"{date}, {time} — {location}: {details_text}")

        return details, history
    except Exception as e:
        logger.error(f"Error scraping AWB {awb}: {e}")
        return None, []

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "🚀 **Welcome to BlueDart Tracking Bot!**\n\n"
        "📦 I can help you track your BlueDart shipments with real-time updates.\n\n"
        "**Quick Start:**\n"
        "• `/add <AWB>` - Add shipment to tracking\n"
        "• `/list` - View all tracked shipments\n"
        "• `/track <AWB>` - Get instant shipment details\n\n"
        "Use `/help` for detailed command information.",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed help information"""
    try:
        if not update.message:
            return
        
        help_text = (
            "📚 **BlueDart Tracking Bot - Help**\n\n"
            "**🔍 Tracking Commands:**\n"
            "• `/track <AWB>` - Get instant details for any AWB\n"
            "• `/add <AWB>` - Add AWB to your tracking list\n"
            "• `/remove <AWB>` - Remove AWB from tracking\n"
            "• `/list` - View all your tracked shipments\n\n"
            "**📋 Information Commands:**\n"
            "• `/completeTracking <AWB>` - Get full tracking history\n"
            "• `/clear` - Clear all tracking data\n"
            "• `/help` - Show this help message\n\n"
            "**🔧 Features:**\n"
            "• ✅ Real-time status updates\n"
            "• ✅ Persistent tracking across bot restarts\n"
            "• ✅ Interactive buttons for easy navigation\n"
            "• ✅ Automatic notifications on status changes\n\n"
            "**💡 Tips:**\n"
            "• Use `/track` for one-time checks\n"
            "• Use `/add` for continuous monitoring\n"
            "• Click buttons for detailed information\n"
            "• Get instant updates on status changes\n\n"
            "**📞 Support:**\n"
            "If you encounter issues, please check your AWB number is correct."
        )
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in help command: {e}")
        if update.message:
            await update.message.reply_text("Sorry, there was an error displaying help.")

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add AWB to tracking with comprehensive error handling"""
    try:
        if not update.message:
            return
        
        user_id = update.effective_user.id
        username = update.effective_user.username or "unknown"
        
        if len(context.args) != 1:
            logger.info(f"User {user_id} ({username}) used /add with invalid args")
            await update.message.reply_text(
                "❌ **Usage:** `/add <AWB>`\n\n"
                "**Example:** `/add 90147628351`",
                parse_mode='Markdown'
            )
            return
        
        awb = context.args[0].strip()
        logger.info(f"User {user_id} ({username}) adding AWB: {awb}")
        
        # Check if already tracking
        if user_id in user_trackings and awb in user_trackings[user_id]:
            await update.message.reply_text(
                f"ℹ️ **AWB {awb} is already in your tracking list!**\n\n"
                f"Use `/list` to see all tracked shipments.",
                parse_mode='Markdown'
            )
            return
        
        # Show loading message
        loading_msg = await update.message.reply_text("🔄 Fetching shipment details...")
        
        details, _ = fetch_bluedart_details(awb)
        if not details:
            await loading_msg.edit_text(
                f"❌ **Could not fetch details for AWB: {awb}**\n\n"
                f"• Please check if the AWB number is correct\n"
                f"• The shipment might not be in the system yet\n"
                f"• Try again later if the issue persists",
                parse_mode='Markdown'
            )
            return
        
        # Check if item is already delivered
        if details.get("Is Delivered", False):
            logger.info(f"AWB {awb} is already delivered, cannot add to tracking")
            
            # Remove from tracking if it was being tracked
            if user_id in user_trackings and awb in user_trackings[user_id]:
                del user_trackings[user_id][awb]
                if not user_trackings[user_id]:
                    del user_trackings[user_id]
                save_tracking_data()
                removal_msg = "\n🗑️ **Removed from tracking** (item delivered)\n"
            else:
                removal_msg = ""
            
            msg = (
                f"✅ **Shipment Delivered!**\n\n"
                f"📋 **Waybill No:** {details['Waybill No']}\n"
                f"📊 **Status:** {details['Status']}\n"
                f"📍 **From:** {details['From']} → {details['To']}\n"
                f"📅 **Pickup Date:** {details['Pickup Date']}\n"
                f"📅 **Delivery Date:** {details.get('Date of Delivery', 'N/A')}\n"
                f"⏰ **Delivery Time:** {details.get('Time of Delivery', 'N/A')}\n"
                f"👤 **Recipient:** {details.get('Recipient', 'N/A')}\n"
                f"🔖 **Reference No:** {details['Reference No']}\n"
                f"{removal_msg}\n"
                f"ℹ️ **Cannot add delivered items to tracking.**"
            )
            
            keyboard = [
                [InlineKeyboardButton("📜 Show Tracking History", callback_data=f"history_{awb}")]
            ]
            
            await loading_msg.edit_text(
                msg, 
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            return
        
        # Add to tracking (only if not delivered)
        if user_id not in user_trackings:
            user_trackings[user_id] = {}
        
        user_trackings[user_id][awb] = details["Status"]
        save_tracking_data()
        
        logger.info(f"Successfully added AWB {awb} for user {user_id} ({username})")
        
        msg = (
            f"✅ **Added to Tracking!**\n\n"
            f"📋 **Waybill No:** {details['Waybill No']}\n"
            f"📊 **Status:** {details['Status']}\n"
            f"📍 **From:** {details['From']} → {details['To']}\n"
            f"📅 **Pickup Date:** {details['Pickup Date']}\n"
            f"📅 **Expected Delivery:** {details.get('Expected Delivery', 'N/A')}\n"
            f"🔖 **Reference No:** {details['Reference No']}\n\n"
            f"🔔 You'll receive notifications when the status changes!"
        )
        
        keyboard = [
            [InlineKeyboardButton("📜 Show Tracking History", callback_data=f"history_{awb}")],
            [InlineKeyboardButton("📦 View All Tracked", callback_data="back_to_list")]
        ]
        
        await loading_msg.edit_text(
            msg, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error in add command: {e}")
        if update.message:
            await update.message.reply_text(
                "❌ Sorry, there was an error adding the AWB to tracking. Please try again."
            )

async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove AWB from tracking with confirmation"""
    try:
        if not update.message:
            return
        
        user_id = update.effective_user.id
        if len(context.args) != 1:
            await update.message.reply_text(
                "❌ **Usage:** `/remove <AWB>`\n\n"
                "**Example:** `/remove 90147628351`",
                parse_mode='Markdown'
            )
            return
        
        awb = context.args[0].strip()
        
        if user_id in user_trackings and awb in user_trackings[user_id]:
            # Get details before removing for confirmation
            details, _ = fetch_bluedart_details(awb)
            
            del user_trackings[user_id][awb]
            if not user_trackings[user_id]:
                del user_trackings[user_id]
            save_tracking_data()
            
            msg = (
                f"✅ **Removed from Tracking!**\n\n"
                f"📋 **AWB:** {awb}\n"
            )
            
            if details:
                msg += (
                    f"📍 **From:** {details['From']} → {details['To']}\n"
                    f"📊 **Last Status:** {details['Status']}\n\n"
                )
            
            msg += f"🔕 You'll no longer receive notifications for this shipment."
            
            keyboard = [
                [InlineKeyboardButton("➕ Add Back to Tracking", callback_data=f"add_track_{awb}")],
                [InlineKeyboardButton("📦 View All Tracked", callback_data="back_to_list")]
            ]
            
            await update.message.reply_text(
                msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        else:
            # Show user's current tracking list for reference
            if user_id in user_trackings and user_trackings[user_id]:
                tracked_awbs = list(user_trackings[user_id].keys())
                awb_list = "\n".join([f"• {awb}" for awb in tracked_awbs])
                
                await update.message.reply_text(
                    f"❌ **AWB {awb} not found in your tracking list.**\n\n"
                    f"📋 **Your currently tracked AWBs:**\n{awb_list}\n\n"
                    f"Use `/list` to see detailed information.",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    f"❌ **AWB {awb} not found.**\n\n"
                    f"📦 You don't have any shipments in your tracking list.\n"
                    f"Use `/add <AWB>` to start tracking shipments.",
                    parse_mode='Markdown'
                )
                
    except Exception as e:
        logger.error(f"Error in remove command: {e}")
        if update.message:
            await update.message.reply_text(
                "❌ Sorry, there was an error removing the AWB from tracking. Please try again."
            )

async def list_awbs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all tracked AWBs with improved UX"""
    try:
        if not update.message:
            return
        
        user_id = update.effective_user.id
        if user_id not in user_trackings or not user_trackings[user_id]:
            await update.message.reply_text(
                "📦 **No Tracked Shipments**\n\n"
                "📝 You don't have any shipments in your tracking list yet.\n\n"
                "**Get Started:**\n"
                "• Use `/add <AWB>` to track a shipment\n"
                "• Use `/track <AWB>` for one-time checks\n"
                "• Use `/help` for more information",
                parse_mode='Markdown'
            )
            return
        
        # Show loading message for better UX
        loading_msg = await update.message.reply_text(
            f"🔄 Loading {len(user_trackings[user_id])} shipment(s)..."
        )
        
        msg = f"📦 **Your Tracked Shipments** ({len(user_trackings[user_id])})\n\n"
        keyboard = []
        
        for i, awb in enumerate(user_trackings[user_id], 1):
            details, _ = fetch_bluedart_details(awb)
            if not details:
                msg += f"{i}. **{awb}**\n   ❌ Error fetching data\n\n"
                continue
            
            # Get status emoji
            status_emoji = "📊"
            if "delivered" in details['Status'].lower():
                status_emoji = "✅"
            elif "out for delivery" in details['Status'].lower():
                status_emoji = "🚚"
            elif "picked up" in details['Status'].lower():
                status_emoji = "📦"
            
            # Create concise list entry
            msg += (
                f"{i}. **{awb}**\n"
                f"   📍 {details['From']} → {details['To']}\n"
                f"   {status_emoji} {details['Status']}\n\n"
            )
            
            # Add button for each AWB
            keyboard.append([InlineKeyboardButton(f"📋 {awb}", callback_data=f"details_{awb}")])
        
        keyboard.append([InlineKeyboardButton("🔄 Refresh All", callback_data="refresh_list")])
        
        await loading_msg.edit_text(
            msg, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error in list command: {e}")
        if update.message:
            await update.message.reply_text(
                "❌ Sorry, there was an error loading your tracking list. Please try again."
            )

async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track a specific AWB and show details immediately"""
    try:
        if not update.message:
            return
        
        if len(context.args) != 1:
            await update.message.reply_text(
                "❌ **Usage:** `/track <AWB>`\n\n"
                "**Example:** `/track 90147628351`",
                parse_mode='Markdown'
            )
            return
        
        awb = context.args[0].strip()
        
        # Show loading message
        loading_msg = await update.message.reply_text("🔄 Fetching shipment details...")
        
        details, _ = fetch_bluedart_details(awb)
        
        if not details:
            await loading_msg.edit_text(
                f"❌ **Could not fetch details for AWB: {awb}**\n\n"
                f"• Please check if the AWB number is correct\n"
                f"• The shipment might not be in the system yet\n"
                f"• Try again later if the issue persists\n\n"
                f"**Need help?** Use `/help` for more information.",
                parse_mode='Markdown'
            )
            return
        
        # Check if item is delivered
        is_delivered = details.get("Is Delivered", False)
        user_id = update.effective_user.id
        is_tracking = user_id in user_trackings and awb in user_trackings[user_id]
        
        if is_delivered:
            # Auto-remove from tracking if delivered
            if is_tracking:
                del user_trackings[user_id][awb]
                if not user_trackings[user_id]:
                    del user_trackings[user_id]
                save_tracking_data()
                logger.info(f"Auto-removed delivered AWB {awb} from user {user_id} tracking")
                removal_msg = "\n🗑️ **Removed from tracking** (item delivered)\n"
            else:
                removal_msg = ""
            
            msg = (
                f"📋 **Shipment Details - {awb}**\n\n"
                f"✅ **Status:** {details['Status']}\n"
                f"📍 **From:** {details['From']}\n"
                f"📍 **To:** {details['To']}\n"
                f"📅 **Pickup Date:** {details['Pickup Date']}\n"
                f"📅 **Delivery Date:** {details.get('Date of Delivery', 'N/A')}\n"
                f"⏰ **Delivery Time:** {details.get('Time of Delivery', 'N/A')}\n"
                f"👤 **Recipient:** {details.get('Recipient', 'N/A')}\n"
                f"🔖 **Reference No:** {details['Reference No']}\n"
                f"{removal_msg}\n"
                f"ℹ️ **Delivered items cannot be added to tracking.**"
            )
            
            keyboard = [
                [InlineKeyboardButton("📜 Show Tracking History", callback_data=f"history_{awb}")],
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"details_{awb}")]
            ]
        else:
            # Get status emoji for non-delivered items
            status_emoji = "📊"
            if "out for delivery" in details['Status'].lower():
                status_emoji = "🚚"
            elif "picked up" in details['Status'].lower():
                status_emoji = "📦"
            
            msg = (
                f"📋 **Shipment Details - {awb}**\n\n"
                f"{status_emoji} **Status:** {details['Status']}\n"
                f"📍 **From:** {details['From']}\n"
                f"📍 **To:** {details['To']}\n"
                f"📅 **Pickup Date:** {details['Pickup Date']}\n"
                f"📅 **Expected Delivery:** {details.get('Expected Delivery', 'N/A')}\n"
                f"🔖 **Reference No:** {details['Reference No']}\n"
            )
            
            if is_tracking:
                keyboard = [
                    [InlineKeyboardButton("📜 Show Tracking History", callback_data=f"history_{awb}")],
                    [InlineKeyboardButton("✅ Already Tracking", callback_data="noop")],
                    [InlineKeyboardButton("🔄 Refresh", callback_data=f"details_{awb}")]
                ]
            else:
                keyboard = [
                    [InlineKeyboardButton("📜 Show Tracking History", callback_data=f"history_{awb}")],
                    [InlineKeyboardButton("➕ Add to Tracking", callback_data=f"add_track_{awb}")],
                    [InlineKeyboardButton("🔄 Refresh", callback_data=f"details_{awb}")]
                ]
        
        await loading_msg.edit_text(
            msg, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error in track command: {e}")
        if update.message:
            await update.message.reply_text("Sorry, there was an error tracking the AWB.")

async def clear_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all tracking data with confirmation"""
    try:
        if not update.message:
            return
        
        user_id = update.effective_user.id
        
        # Check if user has any tracking data
        if user_id not in user_trackings or not user_trackings[user_id]:
            await update.message.reply_text(
                "📦 **No Tracking Data Found**\n\n"
                "📝 You don't have any shipments in your tracking list.\n\n"
                "**Get Started:**\n"
                "• Use `/add <AWB>` to track a shipment\n"
                "• Use `/track <AWB>` for one-time checks",
                parse_mode='Markdown'
            )
            return
        
        # Show confirmation message with current tracking info
        awb_count = len(user_trackings[user_id])
        awb_list = "\n".join([f"• {awb}" for awb in list(user_trackings[user_id].keys())[:5]])
        
        if awb_count > 5:
            awb_list += f"\n• ... and {awb_count - 5} more"
        
        msg = (
            f"⚠️ **Clear All Tracking Data?**\n\n"
            f"📊 **You are currently tracking {awb_count} shipment(s):**\n"
            f"{awb_list}\n\n"
            f"❗ **This action cannot be undone!**\n"
            f"All tracking data will be permanently removed."
        )
        
        keyboard = [
            [InlineKeyboardButton("🗑️ Yes, Clear All", callback_data="confirm_clear")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_clear")]
        ]
        
        await update.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error in clear command: {e}")
        if update.message:
            await update.message.reply_text(
                "❌ Sorry, there was an error processing the clear request. Please try again."
            )

async def complete_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /completeTracking <AWB>")
        return
    awb = context.args[0]
    _, history = fetch_bluedart_details(awb)
    if not history:
        await update.message.reply_text("No tracking history found.")
        return
    msg = "Tracking History:\n" + "\n".join(f"{h}" for h in history)
    await update.message.reply_text(msg)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("details_"):
        awb = query.data.split("_", 1)[1]
        details, _ = fetch_bluedart_details(awb)
        if not details:
            await query.edit_message_text("❌ Error fetching details for this AWB.")
            return
        
        # Check if item is delivered to show appropriate fields
        is_delivered = details.get("Is Delivered", False)
        
        msg = (
            f"📋 **Shipment Details - {awb}**\n\n"
            f"📊 **Status:** {details['Status']}\n"
            f"📍 **From:** {details['From']}\n"
            f"📍 **To:** {details['To']}\n"
            f"📅 **Pickup Date:** {details['Pickup Date']}\n"
        )
        
        if is_delivered:
            msg += (
                f"📅 **Delivery Date:** {details.get('Date of Delivery', 'N/A')}\n"
                f"⏰ **Delivery Time:** {details.get('Time of Delivery', 'N/A')}\n"
                f"👤 **Recipient:** {details.get('Recipient', 'N/A')}\n"
            )
        else:
            msg += f"📅 **Expected Delivery:** {details.get('Expected Delivery', 'N/A')}\n"
        
        msg += f"🔖 **Reference No:** {details['Reference No']}\n"
        
        keyboard = [
            [InlineKeyboardButton("📜 Show Tracking History", callback_data=f"history_{awb}")],
            [InlineKeyboardButton("🔄 Refresh Details", callback_data=f"details_{awb}")],
            [InlineKeyboardButton("📦 Back to List", callback_data="back_to_list")]
        ]
        
        await query.edit_message_text(
            msg, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data.startswith("history_"):
        awb = query.data.split("_", 1)[1]
        _, history = fetch_bluedart_details(awb)
        if not history:
            await query.edit_message_text("❌ No tracking history found.")
            return
        
        msg = f"📜 **Tracking History - {awb}**\n\n"
        for h in history:
            msg += f"{h}\n"
        
        keyboard = [
            [InlineKeyboardButton("📋 Show Details", callback_data=f"details_{awb}")],
            [InlineKeyboardButton("🔄 Refresh History", callback_data=f"history_{awb}")],
            [InlineKeyboardButton("📦 Back to List", callback_data="back_to_list")]
        ]
        
        await query.edit_message_text(
            msg, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data == "back_to_list":
        user_id = query.from_user.id
        if user_id not in user_trackings or not user_trackings[user_id]:
            await query.edit_message_text("You are not tracking any AWBs.")
            return
        
        msg = "📦 **Your Tracked Shipments:**\n\n"
        keyboard = []
        
        for i, awb in enumerate(user_trackings[user_id], 1):
            details, _ = fetch_bluedart_details(awb)
            if not details:
                msg += f"{i}. **{awb}**\n   ❌ Error fetching data\n\n"
                continue
            
            msg += (
                f"{i}. **{awb}**\n"
                f"   📍 {details['From']} → {details['To']}\n"
                f"   📊 Status: {details['Status']}\n\n"
            )
            
            keyboard.append([InlineKeyboardButton(f"📋 Details - {awb}", callback_data=f"details_{awb}")])
        
        keyboard.append([InlineKeyboardButton("🔄 Refresh All", callback_data="refresh_list")])
        
        await query.edit_message_text(
            msg, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data == "refresh_list":
        await query.answer("🔄 Refreshing...")
        # Trigger the same logic as back_to_list
        query.data = "back_to_list"
        await button(update, context)
    
    elif query.data == "confirm_clear":
        user_id = query.from_user.id
        user_trackings.pop(user_id, None)
        save_tracking_data()
        await query.edit_message_text("🗑️ **All tracking data cleared!**\n\nYou can start fresh by using `/add <AWB>`.", parse_mode='Markdown')
    
    elif query.data == "cancel_clear":
        await query.edit_message_text("❌ **Clear operation canceled.** Your tracking data is safe.")
    
    elif query.data.startswith("add_track_"):
        awb = query.data.split("_", 2)[2]
        user_id = query.from_user.id
        
        # Check if already tracking
        if user_id in user_trackings and awb in user_trackings[user_id]:
            await query.answer("ℹ️ AWB already in your tracking list")
            return
        
        # Add to tracking
        details, _ = fetch_bluedart_details(awb)
        if not details:
            await query.answer("❌ Error adding AWB to tracking")
            return
        
        if user_id not in user_trackings:
            user_trackings[user_id] = {}
        
        user_trackings[user_id][awb] = details["Status"]
        save_tracking_data()
        
        await query.answer("✅ AWB added to tracking list!")
        
        # Update the message to show it's been added
        keyboard = [
            [InlineKeyboardButton("📜 Show Tracking History", callback_data=f"history_{awb}")],
            [InlineKeyboardButton("✅ Added to Tracking", callback_data=f"noop")],
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"details_{awb}")]
        ]
        
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif query.data == "noop":
        await query.answer()
    
    # Legacy support for old complete_ callbacks
    elif query.data.startswith("complete_"):
        awb = query.data.split("_", 1)[1]
        query.data = f"history_{awb}"
        await button(update, context)

# --- PERIODIC CHECK ---
async def check_statuses(app):
    """Check for status updates on all tracked AWBs"""
    total_awbs = sum(len(awbs) for awbs in user_trackings.values())
    if total_awbs == 0:
        logger.debug("No AWBs to check")
        return
    
    logger.info(f"Checking status for {total_awbs} AWBs across {len(user_trackings)} users")
    
    changes_made = False
    checked_count = 0
    
    for user_id, awbs in user_trackings.items():
        for awb, last_status in list(awbs.items()):
            checked_count += 1
            details, _ = fetch_bluedart_details(awb)
            new_status = details["Status"] if details else "N/A"
            is_delivered = details.get("Is Delivered", False) if details else False
            
            # Check for status changes
            if new_status != last_status:
                changes_made = True
                logger.info(f"Status change detected - AWB {awb}: {last_status} -> {new_status}")
                
                # If item is delivered, remove from tracking and send special notification
                if is_delivered:
                    del user_trackings[user_id][awb]
                    if not user_trackings[user_id]:
                        del user_trackings[user_id]
                    
                    logger.info(f"Auto-removed delivered AWB {awb} from user {user_id} tracking")
                    
                    try:
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🎉 **Shipment Delivered!**\n\n"
                                f"📋 **AWB:** {awb}\n"
                                f"✅ **Status:** {new_status}\n"
                                f"📅 **Delivery Date:** {details.get('Date of Delivery', 'N/A')}\n"
                                f"⏰ **Delivery Time:** {details.get('Time of Delivery', 'N/A')}\n"
                                f"👤 **Recipient:** {details.get('Recipient', 'N/A')}\n\n"
                                f"🗑️ **Removed from tracking** (delivery complete)\n\n"
                                f"Use `/track {awb}` to see full delivery details."
                            ),
                            parse_mode='Markdown'
                        )
                        logger.info(f"Delivery notification sent to user {user_id} for AWB {awb}")
                    except Exception as e:
                        logger.error(f"Failed to send delivery notification to user {user_id} for AWB {awb}: {e}")
                else:
                    # Regular status update
                    user_trackings[user_id][awb] = new_status
                    
                    try:
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🔔 **Status Update!**\n\n"
                                f"📋 **AWB:** {awb}\n"
                                f"📊 **New Status:** {new_status}\n\n"
                                f"Use `/track {awb}` for full details."
                            ),
                            parse_mode='Markdown'
                        )
                        logger.info(f"Status notification sent to user {user_id} for AWB {awb}")
                    except Exception as e:
                        logger.error(f"Failed to send status notification to user {user_id} for AWB {awb}: {e}")
    
    logger.info(f"Status check completed: {checked_count} AWBs checked, {sum(1 for user in user_trackings.values() for awb, status in user.items() if status != user_trackings.get(list(user_trackings.keys())[0], {}).get(awb, status))} changes detected")
    
    if changes_made:
        save_tracking_data()
        logger.info("Tracking data saved after status changes")

# --- MAIN ---
async def main():
    load_tracking_data()
    total_users = len(user_trackings)
    total_awbs = sum(len(awbs) for awbs in user_trackings.values())
    if total_users > 0:
        logger.info(f"Resuming tracking for {total_users} users with {total_awbs} AWB(s)")
    else:
        logger.info("Starting fresh with no existing tracking data")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("list", list_awbs))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("clear", clear_tracking))
    app.add_handler(CommandHandler("completeTracking", complete_tracking))
    app.add_handler(CallbackQueryHandler(button))
    await app.initialize()
    
    # Initialize global scheduler
    global scheduler
    scheduler = AsyncIOScheduler()
    
    async def status_check_wrapper():
        await check_statuses(app)
    
    scheduler.add_job(status_check_wrapper, "interval", minutes=5)
    scheduler.add_job(save_tracking_data, "interval", minutes=30)
    scheduler.start()
    logger.info("Bot started.")
    try:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
    finally:
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
