#!/usr/bin/env python3
"""
ADMIN EMAIL NOTIFIER BOT
Automatyczne powiadomienia o nowych emailach Admin na ClickUp.
Używa Gmail API z service account (domain-wide delegation).
"""

import os
import re
import time
import base64
from datetime import datetime
from zoneinfo import ZoneInfo
import logging
from threading import Thread
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify

from google.oauth2 import service_account
from googleapiclient.discovery import build

# Load environment variables
load_dotenv()

# Configure logging
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

# Gmail API Settings
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE', '/app/oauth2service.json')
IMPERSONATED_USER = os.getenv('IMPERSONATED_USER')
GROUP_ADDRESS = os.getenv('GROUP_ADDRESS')
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.modify']  # Need modify to mark as read

# ClickUp
CLICKUP_API_KEY = os.getenv('CLICKUP_API_KEY')
CLICKUP_WORKSPACE_ID = os.getenv('CLICKUP_WORKSPACE_ID')
CLICKUP_CHANNEL_ID = os.getenv('CLICKUP_CHANNEL_ID')

# Bot settings
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '60'))  # seconds
FLASK_PORT = int(os.getenv('FLASK_PORT', '8080'))
MAX_BODY_LENGTH = 500  # Max characters for email body in notification

# Heartbeat
LAST_HEARTBEAT_FILE = '/tmp/admin_last_heartbeat.txt'
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv('HEARTBEAT_TIMEOUT_SECONDS', '300'))

# Tracking processed messages
PROCESSED_IDS_FILE = '/tmp/admin_processed_ids.txt'
MAX_PROCESSED_IDS = 1000  # Keep last N message IDs

# Timezone
POLAND_TZ = ZoneInfo("Europe/Warsaw")


# ============================================================================
# FLASK API (HEALTH ENDPOINT)
# ============================================================================

app = Flask(__name__)

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint - returns bot status and last heartbeat."""
    try:
        if os.path.exists(LAST_HEARTBEAT_FILE):
            with open(LAST_HEARTBEAT_FILE, 'r') as f:
                last_heartbeat = float(f.read().strip())
            
            time_since_heartbeat = time.time() - last_heartbeat
            
            if time_since_heartbeat <= HEARTBEAT_TIMEOUT_SECONDS:
                return jsonify({
                    "status": "healthy",
                    "last_heartbeat": last_heartbeat,
                    "seconds_since_heartbeat": int(time_since_heartbeat),
                    "message": "Bot is alive and checking emails"
                }), 200
            else:
                return jsonify({
                    "status": "unhealthy",
                    "last_heartbeat": last_heartbeat,
                    "seconds_since_heartbeat": int(time_since_heartbeat),
                    "message": f"Bot not responding (>{HEARTBEAT_TIMEOUT_SECONDS}s since last heartbeat)"
                }), 503
        else:
            return jsonify({
                "status": "unknown",
                "message": "No heartbeat file found - bot may not have started yet"
            }), 503
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def now_poland() -> datetime:
    """Get current time in Poland timezone."""
    return datetime.now(POLAND_TZ)


def format_poland_time(dt: datetime = None) -> str:
    """Format datetime for Poland timezone in Polish format."""
    if dt is None:
        dt = now_poland()
    
    months_pl = {
        1: 'stycznia', 2: 'lutego', 3: 'marca', 4: 'kwietnia',
        5: 'maja', 6: 'czerwca', 7: 'lipca', 8: 'sierpnia',
        9: 'września', 10: 'października', 11: 'listopada', 12: 'grudnia'
    }
    
    day = dt.day
    month = months_pl[dt.month]
    year = dt.year
    time_str = dt.strftime('%H:%M')
    
    return f"{day} {month} {year}, {time_str}"


def record_heartbeat():
    """Record current timestamp as heartbeat (bot is alive)."""
    try:
        with open(LAST_HEARTBEAT_FILE, 'w') as f:
            f.write(str(time.time()))
    except Exception as e:
        logger.warning(f"Failed to record heartbeat: {e}")


def load_processed_ids() -> set:
    """Load set of already processed message IDs."""
    try:
        if os.path.exists(PROCESSED_IDS_FILE):
            with open(PROCESSED_IDS_FILE, 'r') as f:
                return set(line.strip() for line in f if line.strip())
    except Exception as e:
        logger.warning(f"Failed to load processed IDs: {e}")
    return set()


def save_processed_id(msg_id: str):
    """Save a processed message ID."""
    try:
        processed = load_processed_ids()
        processed.add(msg_id)
        
        # Keep only last N IDs
        if len(processed) > MAX_PROCESSED_IDS:
            processed = set(list(processed)[-MAX_PROCESSED_IDS:])
        
        with open(PROCESSED_IDS_FILE, 'w') as f:
            f.write('\n'.join(processed))
    except Exception as e:
        logger.warning(f"Failed to save processed ID: {e}")


def strip_html(html_content: str) -> str:
    """Remove HTML tags and clean up whitespace."""
    html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', html_content)
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = text.replace('&#39;', "'")
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ============================================================================
# GMAIL API FUNCTIONS
# ============================================================================

def get_gmail_service():
    """Build a Gmail API service using service account with domain-wide delegation."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=GMAIL_SCOPES,
            subject=IMPERSONATED_USER,
        )
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        logger.debug(f"Gmail service created, impersonating {IMPERSONATED_USER}")
        return service
    except Exception as e:
        logger.error(f"Failed to create Gmail service: {e}")
        raise


def list_group_messages(service, max_results=20):
    """List unread message IDs for emails sent to GROUP_ADDRESS."""
    query = f"to:{GROUP_ADDRESS} is:unread"
    
    try:
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=max_results,
            includeSpamTrash=True,
        ).execute()
        
        return resp.get("messages", [])
    except Exception as e:
        logger.error(f"Failed to list messages: {e}")
        return []


def get_message_details(service, msg_id: str) -> dict:
    """Return dict with From, Subject, Date, and text body."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="full",
        ).execute()
        
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        
        def get_header(name):
            for h in headers:
                if h.get("name", "").lower() == name.lower():
                    return h.get("value", "")
            return ""
        
        from_addr = get_header("From")
        subject = get_header("Subject") or "Brak tematu"
        date_str = get_header("Date")
        
        # Extract text body
        body = extract_text_body(payload)
        
        # Parse date
        try:
            from email.utils import parsedate_to_datetime
            date_received = parsedate_to_datetime(date_str)
            if date_received.tzinfo is None:
                date_received = date_received.replace(tzinfo=POLAND_TZ)
            else:
                date_received = date_received.astimezone(POLAND_TZ)
        except:
            date_received = now_poland()
        
        return {
            "id": msg_id,
            "from": from_addr,
            "subject": subject,
            "date": date_received,
            "body": body,
        }
    except Exception as e:
        logger.error(f"Failed to get message {msg_id}: {e}")
        return None


def extract_text_body(payload: dict) -> str:
    """Extract best-effort text body from a Gmail message payload."""
    def decode_part(part):
        data = part.get("body", {}).get("data")
        if not data:
            return ""
        try:
            decoded_bytes = base64.urlsafe_b64decode(data.encode("utf-8"))
            return decoded_bytes.decode("utf-8", errors="replace")
        except:
            return ""
    
    mime_type = payload.get("mimeType", "")
    
    # Simple text email
    if mime_type == "text/plain":
        return decode_part(payload)
    
    # HTML only
    if mime_type == "text/html":
        return strip_html(decode_part(payload))
    
    # Multipart - walk parts and prefer text/plain
    if mime_type.startswith("multipart/"):
        parts = payload.get("parts", []) or []
        text_parts = []
        html_parts = []
        
        def walk(parts_list):
            for p in parts_list:
                p_type = p.get("mimeType", "")
                if p_type == "text/plain":
                    text_parts.append(decode_part(p))
                elif p_type == "text/html":
                    html_parts.append(decode_part(p))
                elif p_type.startswith("multipart/"):
                    walk(p.get("parts", []) or [])
        
        walk(parts)
        
        if text_parts:
            return "\n".join(text_parts).strip()
        if html_parts:
            return strip_html("\n".join(html_parts)).strip()
    
    # Fallback
    return decode_part(payload)


def mark_as_read(service, msg_id: str):
    """Mark a message as read by removing UNREAD label."""
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        logger.debug(f"Marked message {msg_id} as read")
    except Exception as e:
        logger.warning(f"Failed to mark message {msg_id} as read: {e}")


# ============================================================================
# CLICKUP FUNCTIONS
# ============================================================================

def send_message_to_clickup(message_text: str) -> bool:
    """Send message to ClickUp Chat channel (using Chat API v3)."""
    if not CLICKUP_API_KEY:
        logger.error("CLICKUP_API_KEY not set")
        return False
    
    url = f"https://api.clickup.com/api/v3/workspaces/{CLICKUP_WORKSPACE_ID}/chat/channels/{CLICKUP_CHANNEL_ID}/messages"
    
    headers = {
        'Authorization': CLICKUP_API_KEY,
        'Content-Type': 'application/json'
    }
    
    payload = {
        'content': message_text,
        'content_format': 'text/md'
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        if response.status_code in [200, 201]:
            logger.info("✅ Message sent to ClickUp chat")
            return True
        else:
            logger.error(f"ClickUp API error: {response.status_code} - {response.text}")
            return False
            
    except requests.exceptions.Timeout:
        logger.error("ClickUp API timeout (>30s)")
        return False
        
    except Exception as e:
        logger.error(f"Failed to send message to ClickUp: {e}")
        return False


def format_notification_message(from_addr: str, subject: str, body: str, date_received: datetime, msg_id: str) -> str:
    """Format email notification message for ClickUp."""
    
    # Truncate body if too long
    if len(body) > MAX_BODY_LENGTH:
        body = body[:MAX_BODY_LENGTH] + "..."
    
    # Format date
    date_str = format_poland_time(date_received)
    
    # Google Groups link to view the group inbox
    # Extract domain from group address (e.g., kooperatywa.online from admin@kooperatywa.online)
    group_name = GROUP_ADDRESS.split('@')[0]  # admin
    domain = GROUP_ADDRESS.split('@')[1]      # kooperatywa.online
    groups_link = f"https://groups.google.com/a/{domain}/g/{group_name}"
    
    message = f"""--------------------------------------------------
## 📩 Admin - Nowa wiadomość

**Od:** {from_addr}
**Temat:** {subject}

**Treść:**
{body}

---
📅 Otrzymano: {date_str}
📧 [Otwórz skrzynkę Admin]({groups_link})"""
    
    return message


# ============================================================================
# MAIN LOGIC
# ============================================================================

def check_for_new_emails():
    """Check for new emails to the group and send notifications."""
    try:
        service = get_gmail_service()
        
        # Get unread messages to the group
        messages = list_group_messages(service, max_results=20)
        
        if not messages:
            logger.debug("No new emails found")
            record_heartbeat()
            return
        
        logger.info(f"Found {len(messages)} unread email(s) to {GROUP_ADDRESS}")
        
        # Load already processed IDs (for extra safety)
        processed_ids = load_processed_ids()
        
        for msg_ref in messages:
            msg_id = msg_ref["id"]
            
            # Skip if already processed
            if msg_id in processed_ids:
                logger.debug(f"Skipping already processed message {msg_id}")
                continue
            
            # Get message details
            msg_details = get_message_details(service, msg_id)
            if not msg_details:
                continue
            
            logger.info(f"Processing email from: {msg_details['from']}, subject: {msg_details['subject']}")
            
            # Format and send notification
            notification = format_notification_message(
                msg_details['from'],
                msg_details['subject'],
                msg_details['body'],
                msg_details['date'],
                msg_id
            )
            
            success = send_message_to_clickup(notification)
            
            if success:
                # Mark as read and save to processed
                mark_as_read(service, msg_id)
                save_processed_id(msg_id)
                logger.info(f"✅ Email processed successfully")
            else:
                logger.warning(f"⚠️ Failed to send notification, will retry next cycle")
            
            # Small delay between messages
            time.sleep(1)
        
        # Record heartbeat after successful check
        record_heartbeat()
        
    except Exception as e:
        logger.error(f"Error checking emails: {e}", exc_info=True)


# ============================================================================
# MAIN LOOP
# ============================================================================

def main():
    """Main function - starts Flask API and polling loop."""
    logger.info("=" * 60)
    logger.info("ADMIN EMAIL NOTIFIER BOT (Gmail API)")
    logger.info("=" * 60)
    
    # Validate configuration
    if not CLICKUP_API_KEY:
        logger.error("CLICKUP_API_KEY not set - exiting")
        return
    
    # Validate required environment variables
    required_vars = {
        'IMPERSONATED_USER': IMPERSONATED_USER,
        'GROUP_ADDRESS': GROUP_ADDRESS,
        'CLICKUP_WORKSPACE_ID': CLICKUP_WORKSPACE_ID,
        'CLICKUP_CHANNEL_ID': CLICKUP_CHANNEL_ID,
    }
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)} - exiting")
        return
    
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logger.error(f"Service account file not found: {SERVICE_ACCOUNT_FILE}")
        return
    
    # Test Gmail API connection
    logger.info(f"Testing Gmail API connection (impersonating {IMPERSONATED_USER})...")
    try:
        service = get_gmail_service()
        # Simple test - get user profile
        profile = service.users().getProfile(userId="me").execute()
        logger.info(f"✅ Gmail API connection OK - {profile.get('emailAddress')}")
    except Exception as e:
        logger.error(f"❌ Gmail API connection failed: {e}")
        return
    
    # Start Flask API in separate thread
    logger.info(f"Starting Flask health API on port {FLASK_PORT}...")
    flask_thread = Thread(target=lambda: app.run(host='0.0.0.0', port=FLASK_PORT), daemon=True)
    flask_thread.start()
    
    # Wait for Flask to start
    time.sleep(2)
    
    # Main polling loop
    logger.info(f"Starting main polling loop (interval: {CHECK_INTERVAL}s)...")
    logger.info(f"Monitoring group: {GROUP_ADDRESS}")
    logger.info(f"Via mailbox: {IMPERSONATED_USER}")
    logger.info("Bot is ready! 🚀")
    
    while True:
        try:
            check_for_new_emails()
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
        
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
