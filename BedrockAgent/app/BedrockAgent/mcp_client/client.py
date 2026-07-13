import logging
import os
from typing import Optional

from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

logger = logging.getLogger(__name__)

# AgentCore Gateway exposes its tools as MCP over streamable HTTP and authenticates
# with IAM, so every request must be SigV4-signed against this service name.
AWS_SERVICE = "bedrock-agentcore"

# Gateway endpoint, e.g.
#   https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
GATEWAY_URL_ENV = "AGENTCORE_GATEWAY_URL"


def _resolve_region() -> Optional[str]:
    """Region used to sign requests. The Runtime injects AWS_REGION."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def get_streamable_http_mcp_client() -> Optional[MCPClient]:
    """Return an MCP client bound to the AgentCore Gateway, or None if it is not configured.

    Credentials come from the default AWS chain:
      - In the Runtime, from the Runtime execution role (AWS_PROFILE is unset there).
      - Locally, from AWS_PROFILE / the default chain.

    Returning None rather than raising keeps the agent usable without the Gateway;
    main.py skips falsy clients when assembling its tool list.
    """
    endpoint = os.environ.get(GATEWAY_URL_ENV)
    if not endpoint:
        logger.warning(
            "%s is not set. Starting without Gateway tools, so the Knowledge Base is unreachable.",
            GATEWAY_URL_ENV,
        )
        return None

    region = _resolve_region()
    # Unset inside the Runtime, where the execution role supplies credentials.
    profile = os.environ.get("AWS_PROFILE")

    logger.info("AgentCore Gateway endpoint=%s region=%s", endpoint, region)

    return MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=endpoint,
            aws_service=AWS_SERVICE,
            aws_region=region,
            aws_profile=profile,
        )
    )
