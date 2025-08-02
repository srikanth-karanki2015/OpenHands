import hashlib
import hmac
import json
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from openhands.core.logger import openhands_logger as logger
from openhands.integrations.github.pr_reviewer import PRReviewer
from openhands.server.dependencies import get_dependencies
from openhands.server.shared import server_config

app = APIRouter(prefix='/api/webhooks', dependencies=get_dependencies())


class WebhookConfig(BaseModel):
    """Configuration for webhook processing."""

    secret: Optional[str] = Field(
        None, description='Secret for validating webhook signatures'
    )
    allowed_repositories: list[str] = Field(
        default_factory=list,
        description='List of repositories allowed to trigger reviews (format: owner/repo)',
    )
    auto_fix: bool = Field(
        False, description='Whether to automatically create PRs with fixes'
    )


# Load configuration from server_config
webhook_config = WebhookConfig(
    secret=server_config.webhook_secret,
    allowed_repositories=[
        repo for repo in server_config.webhook_allowed_repositories if repo
    ],
    auto_fix=server_config.webhook_auto_fix,
)


def verify_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify the webhook signature using the secret.

    Args:
        payload: The raw request payload
        signature_header: The signature header from GitHub (X-Hub-Signature-256)
        secret: The webhook secret

    Returns:
        True if signature is valid, False otherwise
    """
    if not signature_header.startswith('sha256='):
        return False

    signature = signature_header[7:]  # Remove 'sha256=' prefix

    # Create HMAC with SHA256
    mac = hmac.new(secret.encode(), msg=payload, digestmod=hashlib.sha256)
    expected_signature = mac.hexdigest()

    # Compare signatures using constant-time comparison
    return hmac.compare_digest(signature, expected_signature)


@app.post('/github')
async def github_webhook(
    request: Request,
    x_github_event: str = Header(..., alias='X-GitHub-Event'),
    x_hub_signature_256: Optional[str] = Header(None, alias='X-Hub-Signature-256'),
):
    """
    Handle GitHub webhook events.

    This endpoint receives webhook events from GitHub and processes them based on the event type.
    Currently supports:
    - pull_request events (opened, synchronize)

    Args:
        request: The FastAPI request object
        x_github_event: The GitHub event type
        x_hub_signature_256: The GitHub signature for verification

    Returns:
        A JSON response indicating the result of processing the webhook
    """
    # Read raw payload
    payload_bytes = await request.body()

    # Verify signature if secret is configured
    if webhook_config.secret and x_hub_signature_256:
        if not verify_signature(
            payload_bytes, x_hub_signature_256, webhook_config.secret
        ):
            logger.warning('Invalid webhook signature')
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail='Invalid signature',
            )

    # Parse payload
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        logger.error('Invalid JSON payload in webhook')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid JSON payload',
        )

    # Process based on event type
    if x_github_event == 'pull_request':
        return await handle_pull_request_event(payload)
    elif x_github_event == 'ping':
        return {'message': 'Webhook received successfully'}
    else:
        logger.info(f'Unhandled GitHub event: {x_github_event}')
        return {'message': f'Event type {x_github_event} not handled'}


async def handle_pull_request_event(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Handle GitHub pull request events.

    Args:
        payload: The webhook payload

    Returns:
        A JSON response indicating the result of processing the event
    """
    action = payload.get('action')
    pr_data = payload.get('pull_request', {})
    repository = payload.get('repository', {})

    # Only process opened or synchronized PRs
    if action not in ['opened', 'synchronize']:
        return {'message': f'PR action {action} not handled'}

    repo_name = repository.get('full_name')

    # Check if repository is in allowed list
    if (
        webhook_config.allowed_repositories
        and repo_name not in webhook_config.allowed_repositories
    ):
        logger.info(f'Repository {repo_name} not in allowed list')
        return {'message': f'Repository {repo_name} not configured for PR reviews'}

    # Extract PR information
    pr_number = pr_data.get('number')
    pr_title = pr_data.get('title')
    pr_data.get('body') or ''
    pr_data.get('head', {}).get('ref')
    pr_data.get('base', {}).get('ref')

    logger.info(f'Processing PR #{pr_number} in {repo_name}: {pr_title}')

    try:
        # Initialize PR reviewer
        reviewer = PRReviewer(
            repo_name=repo_name,
            pr_number=pr_number,
            auto_fix=webhook_config.auto_fix,
        )

        # Start review process
        review_result = await reviewer.review_pr()

        return {
            'message': 'PR review initiated',
            'pr_number': pr_number,
            'repo': repo_name,
            'conversation_id': review_result.get('conversation_id'),
        }
    except Exception as e:
        logger.error(f'Error processing PR review: {e}', exc_info=True)
        return {
            'message': f'Error processing PR review: {str(e)}',
            'pr_number': pr_number,
            'repo': repo_name,
        }
