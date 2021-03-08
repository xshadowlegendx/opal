from typing import List

from fastapi import APIRouter, status, Request, Depends, status
from opal.common.logger import get_logger
from opal.server.config import POLICY_REPO_URL
from opal.server.policy.github_webhook.deps import validate_github_signature_or_throw, affected_repo_urls

logger = get_logger('opal.webhook.api')


def init_git_webhook_router(pubsub_endpoint):
    router = APIRouter()

    @router.post(
        "/webhook",
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(validate_github_signature_or_throw)]
    )
    async def trigger_git_webhook(
        request: Request,
        urls: List[str] = Depends(affected_repo_urls)
    ):
        event = request.headers.get('X-GitHub-Event', 'ping')

        if POLICY_REPO_URL is not None and POLICY_REPO_URL in urls:
            logger.info("triggered webhook", repo=urls[0], hook_event=event)
            if event == 'push':
                # notifies the webhook listener via the pubsub broadcaster
                await pubsub_endpoint.publish("webhook")
            return { "status": "ok", "event": event, "repo_url": urls[0] }

        return { "status": "ignored", "event": event }
    return router

