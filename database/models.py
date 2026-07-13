import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, DateTime, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base


Base = declarative_base()


class UserCredential(Base):
    __tablename__ = 'user_credentials'

    id = Column(UUID(as_uuid = True), primary_key = True, default = uuid.uuid4)
    slack_workspace_id = Column(String(50), nullable = False)
    slack_user_id = Column(String(50), nullable = False)
    
    # Using Text instead of String/VARCHAR to accommodate massive tokens
    encrypted_access_token = Column(Text, nullable = False)
    encrypted_refresh_token = Column(Text, nullable = True)
    
    # Always use timezone-aware dates for token expiration checks
    token_expires_at = Column(DateTime(timezone = True), nullable = False)
    
    created_at = Column(DateTime(timezone = True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone = True), default=lambda: datetime.now(timezone.utc), onupdate = lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('slack_workspace_id', 'slack_user_id', name = 'uix_workspace_user'),
        Index('idx_slack_lookup', 'slack_workspace_id', 'slack_user_id'),
    )
