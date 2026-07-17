import os
import httpx
import secrets
import base64
import threading
import logging
from typing import Optional
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import internal app dependencies
from app.database.models import UserCredential, Base
from app.security.token_service import TokenEncryptionService

# Import Google Cloud SDK dependencies
from google.cloud import discoveryengine_v1alpha as discoveryengine
from google.api_core.exceptions import GoogleAPICallError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

load_dotenv()

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Google OAuth Config
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = "https://approval-mankind-flask.ngrok-free.dev/auth/callback"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

MASTER_KEY = os.environ.get("ENCRYPTION_MASTER_KEY")
if not MASTER_KEY:
    raise ValueError("ENCRYPTION_MASTER_KEY environment variable is missing!")
    
crypto_service = TokenEncryptionService(MASTER_KEY)

# 1. Initialize Slack App & FastAPI using User Token
slack_app = App(
    token = os.environ.get("SLACK_USER_TOKEN"),
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


# 3. The Slack Slash Command Handler
@slack_app.command("/gemini-enterprise")
def handle_gemini_command(ack, body, respond):
    # Instantly acknowledge the command within 3 seconds
    ack()

    slack_user_id = body.get("user_id")
    slack_workspace_id = body.get("team_id")
    user_query = body.get("text", "").strip() 
    
    if not user_query:
        respond("Please provide a prompt! Example: `/gemini-enterprise Tell me a joke.`")
        return

    # Defer execution to a background thread to prevent Slack timeout errors
    thread = threading.Thread(
        target = execute_gemini_query_in_background,
        args = (slack_user_id, slack_workspace_id, user_query, respond)
    )
    thread.start()


def execute_gemini_query_in_background(slack_user_id, slack_workspace_id, user_query, respond):
    db = SessionLocal()
    try:
        credential = db.query(UserCredential).filter(
            UserCredential.slack_workspace_id == slack_workspace_id
        ).first()

        if not credential:
            login_url = f"https://approval-mankind-flask.ngrok-free.dev/auth/login?slack_user_id={slack_user_id}&slack_workspace_id={slack_workspace_id}"
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "🔒 *Gemini Enterprise is not activated yet.* A user with a valid Gemini Enterprise license must click below to authorize this workspace."
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🔗 Authorize Workspace"
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

        respond("⏳ _Gemini Enterprise is thinking..._")

        # Decrypt the shared tokens
        access_token = crypto_service.decrypt(credential.encrypted_access_token) # type: ignore
        
        refresh_token = None
        if credential.encrypted_refresh_token:
            refresh_token = crypto_service.decrypt(credential.encrypted_refresh_token)

        # Build credentials
        gcp_credentials = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET
        )

        # --- NEW: Aggressively force a token refresh ---
        try:
            gcp_credentials.refresh(GoogleAuthRequest())
        except Exception as e:
            logger.error(f"Token Refresh Failed: {e}")
            # If this fails, the refresh token is missing or dead. The user MUST re-authenticate.
            respond("⚠️ *Authentication Expired:* The background credentials have expired or are missing a refresh token. Please clear your database records and click 'Authorize Workspace' again.")
            return

        # Initialize the Discovery Engine client using the fresh credentials
        search_client = discoveryengine.SearchServiceClient(credentials=gcp_credentials)

        # --- ENTERPRISE ENGINE CONFIGURATION ---
        PROJECT_NUMBER = "238017122334"
        LOCATION = "global"
        ENGINE_ID = "viresh-engineering-app_1779273678265" 
        SERVING_CONFIG = f"projects/{PROJECT_NUMBER}/locations/{LOCATION}/collections/default_collection/engines/{ENGINE_ID}/servingConfigs/default_search"

        # Build the formal SDK Search Request
        request = discoveryengine.SearchRequest(
            serving_config=SERVING_CONFIG,
            query=user_query,
            page_size=5, 
            spell_correction_spec=discoveryengine.SearchRequest.SpellCorrectionSpec(
                mode=discoveryengine.SearchRequest.SpellCorrectionSpec.Mode.AUTO
            ),
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                    return_snippet=True
                ),
                summary_spec=discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec(
                    summary_result_count=3,
                    include_citations=True,
                    ignore_non_summary_seeking_query=False,  
                    
                    # --- NEW: INJECT CUSTOM MODEL & PROMPT CONFIGURATION ---
                    model_spec=discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec.ModelSpec(
                        version="preview" # Force the latest generative model
                    ),
                    model_prompt_spec=discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec.ModelPromptSpec(
                        preamble="You are a helpful and conversational AI assistant. Answer the user's question using the provided search results. If the exact answer is not in the documents, try your best to infer a helpful response based on the context, but state that you are making an inference. Be friendly."
                    )
                )
            )
        )

        # Execute the search request
        response = search_client.search(request)

        # --- ADD THIS DEBUG BLOCK ---
        print("\n--- DEBUG: FULL SUMMARY OBJECT ---")
        if response.summary:
            print(f"Summary Text: {response.summary.summary_text}")
            # The summary object often contains details on why it failed
            print(f"Summary Status: {response.summary}") 
        else:
            print("No summary object returned by the engine.")
        print("-----------------------------------\n")
        
        bot_response = ""
        
        # Extract the AI-generated answer safely
        if response.summary and response.summary.summary_text:
            summary_text = response.summary.summary_text
            if "A summary could not be generated" not in summary_text:
                bot_response += f"{summary_text}\n\n"
            else:
                bot_response += "I couldn't write a confident summary based on the documents, but I found these files:\n\n"
            
        # Extract individual document links safely
        if response.results:
            bot_response += "Here are the top documents I found:\n"
            for result in response.results[:3]: 
                title = "Untitled Document"
                link = ""
                
                if hasattr(result.document, "derived_struct_data"):
                    struct_data = result.document.derived_struct_data
                    if "title" in struct_data:
                        title = str(struct_data["title"])
                    if "link" in struct_data:
                        link = str(struct_data["link"])
                
                if link:
                    bot_response += f"• <{link}|{title}>\n"
                else:
                    bot_response += f"• {title}\n"
                    
        if not bot_response.strip():
            bot_response = "I couldn't find any relevant information or documents for that query in the enterprise engine."

        # Post the response so the entire channel context can see it
        respond(blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Prompt from <@{slack_user_id}>:* {user_query}\n\n*Response:*\n{bot_response}"
                }
            }
        ], response_type="in_channel")

    except GoogleAPICallError as api_err:
        logger.error(f"Google API Error: {api_err.message}")
        respond(f"⚠️ Google API Error: {api_err.message}")
    except Exception as e:
        logger.error(f"Unexpected processing error: {e}")
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
    raw_state = f"{slack_workspace_id}:{slack_user_id}"
    safe_state = base64.urlsafe_b64encode(raw_state.encode('utf-8')).decode('utf-8')
    
    # Request identity information along with full cloud access to persist credentials cleanly
    scopes = "https://www.googleapis.com/auth/cloud-platform openid email"
    
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

    try:
        decoded_state = base64.urlsafe_b64decode(state.encode('utf-8')).decode('utf-8')
        slack_workspace_id, slack_user_id = decoded_state.split(":")
    except Exception:
        raise HTTPException(status_code = 400, detail = "Failed to parse state parameter.")

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

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    
    expiration_time = datetime.now(timezone.utc) + timedelta(seconds = expires_in)
    encrypted_access = crypto_service.encrypt(access_token)
    encrypted_refresh = crypto_service.encrypt(refresh_token) if refresh_token else None

    db = SessionLocal()
    try:
        # Cross-reference existing records per workspace to carry out a clean upsert
        credential = db.query(UserCredential).filter(
            UserCredential.slack_workspace_id == slack_workspace_id
        ).first()

        if credential:
            credential.encrypted_access_token = encrypted_access #type: ignore
            if encrypted_refresh:
                credential.encrypted_refresh_token = encrypted_refresh #type: ignore
            credential.token_expires_at = expiration_time #type: ignore
            credential.slack_user_id = slack_user_id #type: ignore
        else:
            credential = UserCredential(
                slack_workspace_id = slack_workspace_id,
                slack_user_id = slack_user_id,
                encrypted_access_token = encrypted_access,
                encrypted_refresh_token = encrypted_refresh,
                token_expires_at = expiration_time
            )
            db.add(credential)
        
        db.commit()
    except Exception as e:
        db.rollback()
        return HTMLResponse(content=f"<h2>Database Error</h2><p>{str(e)}</p>", status_code=500)
    finally:
        db.close()

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