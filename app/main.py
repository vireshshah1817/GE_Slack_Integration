import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
# Import the UserCredential model from the previous step
from app.database.models import UserCredential, Base


load_dotenv()
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
    # Acknowledge the command request immediately to prevent Slack timeout
    ack()

    # Extract identity from the Slack payload
    slack_user_id = body.get("user_id")
    slack_workspace_id = body.get("team_id")
    
    # Open a database session
    db = SessionLocal()
    try:
        credential = db.query(UserCredential).filter(
            UserCredential.slack_workspace_id == slack_workspace_id,
            UserCredential.slack_user_id == slack_user_id
        ).first()

        if credential:
            # We have a token! In Sprint 3, we will decrypt and use this.
            respond(f"✅ Token found in database for <@{slack_user_id}>. Ready to query Gemini.")
        else:
            # No token. In Sprint 2, this will be an interactive button.
            respond(f"❌ Token missing for <@{slack_user_id}>. You need to log in.")
    finally:
        db.close()


# 4. FastAPI Route to receive Slack webhooks
@app.post("/slack/events")
async def slack_events(request: Request):
    return await handler.handle(request)
