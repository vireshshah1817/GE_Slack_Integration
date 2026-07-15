import os
import httpx
import secrets
import base64
import threading
from typing import Optional
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
# Import the UserCredential model from the previous step
from app.database.models import UserCredential, Base
from app.security.token_service import TokenEncryptionService


load_dotenv()

# Mock Google OAuth Config (Replace with your Google Cloud Console Web Application credentials)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = "https://approval-mankind-flask.ngrok-free.dev/auth/callback"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

MASTER_KEY = os.environ.get("ENCRYPTION_MASTER_KEY")

if not MASTER_KEY:
    raise ValueError("ENCRYPTION_MASTER_KEY environment variable is missing!")
    
crypto_service = TokenEncryptionService(MASTER_KEY)

# 1. Initialize Slack App & FastAPI
slack_app = App(
    token = os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
)
app = FastAPI()
handler = SlackRequestHandler(slack_app)

# 2. Setup Database Connection
DB_URL = os.environ.get("DATABASE_URL")

if DB_URL is None:
    raise ValueError("DATABASE_URL environment variable is not set.")

engine = create_engine(DB_URL)
Base.metadata.create_all(bind = engine)
SessionLocal = sessionmaker(autocommit = False, autoflush = False, bind = engine)


# 3. The Command Handler
@slack_app.command("/gemini-enterprise")
def handle_gemini_command(ack, body, respond):
    # 1. Instantly acknowledge the command (Must happen within 3 seconds)
    ack()

    slack_user_id = body.get("user_id")
    slack_workspace_id = body.get("team_id")
    user_query = body.get("text", "").strip() 
    
    if not user_query:
        respond("Please provide a prompt! Example: `/gemini-enterprise Tell me a joke.`")
        return

    # 2. Move the heavy API work to a background thread
    # This prevents the main thread from blocking, stopping the Slack timeout
    thread = threading.Thread(
        target = execute_gemini_query_in_background,
        args = (slack_user_id, slack_workspace_id, user_query, respond)
    )
    thread.start()


def execute_gemini_query_in_background(slack_user_id, slack_workspace_id, user_query, respond):
    db = SessionLocal()
    try:
        credential = db.query(UserCredential).filter(
            UserCredential.slack_workspace_id == slack_workspace_id,
            UserCredential.slack_user_id == slack_user_id
        ).first()

        if not credential:
            # Send the login button if they somehow aren't authenticated
            login_url = f"https://approval-mankind-flask.ngrok-free.dev/auth/login?slack_user_id={slack_user_id}&slack_workspace_id={slack_workspace_id}"
            
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"❌ *Token missing for <@{slack_user_id}>.* You need to authorize your Gemini Enterprise account to use this command."
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🔗 Connect Gemini Enterprise"
                            },
                            "url": login_url,
                            "style": "primary",
                            "action_id": "auth_button_click"
                        }
                    ]
                }
            ]
            respond(blocks=blocks)
            return

        # Tell the user we are working on it (visible only to them)
        respond("⏳ _Gemini Enterprise is thinking..._")

        # Decrypt the token
        access_token = crypto_service.decrypt(credential.encrypted_access_token) #type: ignore
        
        # Build your Gemini Enterprise (Discovery Engine) call
        gcp_project_id = os.environ.get("GCP_PROJECT_ID", "project-4-workndemos")
        print("=========== GCP Project ID: =======", gcp_project_id)
        
        location = "global"
        engine_id = "viresh-engineering-app_1779273678265"
        
        # Gemini Enterprise apps use the Discovery Engine API
        url = f"https://discoveryengine.googleapis.com/v1/projects/{gcp_project_id}/locations/{location}/collections/default_collection/engines/{engine_id}/conversations/-:converse"
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        # The payload structure is specific for the Discovery Engine converse API
        payload = {
            "query": {
                "input": user_query
            }
        }
        
        import requests
        response = requests.post(url, headers = headers, json = payload)
        
        if response.status_code == 200:
            data = response.json()
            
            # Extract the AI's reply. We use a try/except to safely handle 
            # variations in the response structure based on your specific Agent configuration.
            try:
                ai_text = data["reply"]["reply"]
            except KeyError:
                # If the structure differs, we print the raw JSON so you can see it in Slack
                ai_text = f"```\n{data}\n```" 
            
            # Send the final response to the channel
            respond(blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Prompt:* {user_query}\n\n*Response:*\n{ai_text}"
                    }
                }
            ], response_type="in_channel") 
            
        else:
            respond(f"⚠️ Gemini Enterprise API Error {response.status_code}: {response.text}")

    except Exception as e:
        respond(f"⚠️ An error occurred while processing your request: {str(e)}")
    finally:
        db.close()


# 4. FastAPI Route to receive Slack webhooks
@app.post("/slack/events")
async def slack_events(request: Request):
    return await handler.handle(request)


# 1. Initiates the OAuth flow when the Slack button is clicked
@app.get("/auth/login")
async def auth_login(slack_user_id: str, slack_workspace_id: str):
    # 1. Base64 encode the state to make it URL and parser-safe
    raw_state = f"{slack_workspace_id}:{slack_user_id}"
    safe_state = base64.urlsafe_b64encode(raw_state.encode('utf-8')).decode('utf-8')
    
    scopes = "https://www.googleapis.com/auth/cloud-platform"
    
    auth_uri = (
        f"{GOOGLE_AUTH_URL}?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={scopes}"
        f"&state={safe_state}"
        f"&access_type=offline"
        f"&prompt=consent"
    )

    return RedirectResponse(url = auth_uri)


# 2. Handles the return trip from Google after the user logs in
@app.get("/auth/callback")
async def auth_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        raise HTTPException(status_code = 400, detail = f"Authorization failed: {error}")
    if not code or not state:
        raise HTTPException(status_code = 400, detail = "Missing code or state parameters.")

    # 2. Safely decode the Base64 state back into our IDs
    try:
        decoded_state = base64.urlsafe_b64decode(state.encode('utf-8')).decode('utf-8')
        slack_workspace_id, slack_user_id = decoded_state.split(":")
    except Exception:
        raise HTTPException(status_code = 400, detail = "Failed to parse state parameter.")

    # Exchange the authorization code for access and refresh tokens
    payload = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(GOOGLE_TOKEN_URL, data = payload)
        token_data = response.json()

    if "error" in token_data:
        return HTMLResponse(content = f"<h2>Token Exchange Failed</h2><p>{token_data.get('error_description')}</p>", status_code = 400)

    # Extract tokens from the response
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token") # Provided on initial login due to access_type=offline
    expires_in = token_data.get("expires_in", 3600)
    
    # Calculate target expiration timestamp
    expiration_time = datetime.now(timezone.utc) + timedelta(seconds = expires_in)
    encrypted_access = crypto_service.encrypt(access_token)
    encrypted_refresh = crypto_service.encrypt(refresh_token) if refresh_token else None

    db = SessionLocal()
    try:
        # Check if credential record already exists to perform an upsert
        credential = db.query(UserCredential).filter(
            UserCredential.slack_workspace_id == slack_workspace_id,
            UserCredential.slack_user_id == slack_user_id
        ).first()

        if credential:
            credential.encrypted_access_token = encrypted_access # type: ignore

            if encrypted_refresh:
                credential.encrypted_refresh_token = encrypted_refresh # type: ignore

            credential.token_expires_at = expiration_time # type: ignore
        else:
            credential = UserCredential(
                slack_workspace_id=slack_workspace_id,
                slack_user_id=slack_user_id,
                encrypted_access_token=encrypted_access,
                encrypted_refresh_token=encrypted_refresh,
                token_expires_at=expiration_time
            )
            db.add(credential)
        
        db.commit()
    except Exception as e:
        db.rollback()
        return HTMLResponse(content=f"<h2>Database Error</h2><p>{str(e)}</p>", status_code=500)
    finally:
        db.close()

    # Show a clean validation screen to the user in their browser
    return HTMLResponse(
        content="""
        <html>
            <body style="font-family: Arial, sans-serif; text-align: center; padding-top: 100px;">
                <h1 style="color: #2eb67d;">🎉 Account Linked Successfully!</h1>
                <p>Your Gemini Enterprise account is securely connected to Slack.</p>
                <p>You can close this tab and return to Slack to use the command.</p>
            </body>
        </html>
        """
    )
