from dotenv import load_dotenv
import os

# Load environment variables FIRST
load_dotenv()

from platform_sdk.auth import (
    OAuthConfig, OAuthMode, JWTAlgorithm, AuthDatabase, AuthorizationEngine
)
from platform_sdk.auth.rbac_helpers import create_dynamic_permission_dependency


def get_auth_config() -> OAuthConfig:
    """OAuth configuration for STANDALONE mode - JWT validation only."""
    return OAuthConfig(
        mode=OAuthMode.STANDALONE,
        jwt_algorithm=JWTAlgorithm.HS256,
        jwt_signing_key=os.getenv("JWT_SIGNING_KEY"),
        jwt_issuer=os.getenv("JWT_ISSUER", "CAMS"),
        jwt_audience=os.getenv("JWT_AUDIENCE", "Cams Users"),
    )


# Singleton instances — all share the same auth_db
auth_config = get_auth_config()
auth_db = AuthDatabase(os.getenv("CAMS_DATABASE_URL"), config=auth_config)
auth_engine = AuthorizationEngine(auth_db)

rbac_dependency = create_dynamic_permission_dependency(
    auth_config,
    auth_engine,
    database=auth_db,
)
