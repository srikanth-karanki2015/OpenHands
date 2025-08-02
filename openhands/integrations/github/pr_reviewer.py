import dataclasses
import os
from typing import Any, Optional

from pydantic import BaseModel, Field, SecretStr

from openhands.core.logger import openhands_logger as logger
from openhands.events.types import EventType
from openhands.integrations.github.github_service import GitHubService
from openhands.integrations.service_types import RequestMethod
from openhands.server.session.conversation_init_data import ConversationInitData
from openhands.server.shared import conversation_manager
from openhands.storage.data_models.conversation_metadata import ConversationTrigger


class PRReviewResult(BaseModel):
    """Result of a PR review."""

    conversation_id: str = Field(
        ..., description='ID of the conversation created for the review'
    )
    review_comments: list[str] = Field(
        default_factory=list, description='Review comments'
    )
    fix_pr_url: Optional[str] = Field(
        None, description='URL of the PR with fixes, if created'
    )


class PRReviewer:
    """
    PR Reviewer for GitHub pull requests.

    This class handles the process of reviewing GitHub pull requests using OpenHands AI.
    It creates a new conversation, analyzes the PR changes, and generates review comments.
    Optionally, it can create a new PR with suggested fixes.
    """

    def __init__(
        self,
        repo_name: str,
        pr_number: int,
        auto_fix: bool = False,
        github_token: Optional[SecretStr] = None,
    ):
        """
        Initialize the PR reviewer.

        Args:
            repo_name: The repository name in format 'owner/repo'
            pr_number: The PR number
            auto_fix: Whether to automatically create PRs with fixes
            github_token: GitHub token for API access (optional, will use system token if not provided)
        """
        self.repo_name = repo_name
        self.pr_number = pr_number
        self.auto_fix = auto_fix
        self.github_token = github_token

    async def _get_github_service(self) -> GitHubService:
        """
        Get a GitHub service instance with appropriate authentication.

        Returns:
            An authenticated GitHub service instance
        """
        # Use provided token or system token
        token = self.github_token or SecretStr(os.environ.get('GITHUB_TOKEN', ''))

        # Create GitHub service
        return GitHubService(token=token)

    async def _fetch_pr_details(self, github_service: GitHubService) -> dict[str, Any]:
        """
        Fetch PR details from GitHub.

        Args:
            github_service: The GitHub service to use for API calls

        Returns:
            PR details including title, body, and branches
        """
        url = f'{github_service.BASE_URL}/repos/{self.repo_name}/pulls/{self.pr_number}'
        pr_data, _ = await github_service._make_request(url, method=RequestMethod.GET)

        return {
            'title': pr_data.get('title', ''),
            'body': pr_data.get('body', ''),
            'head_branch': pr_data.get('head', {}).get('ref', ''),
            'base_branch': pr_data.get('base', {}).get('ref', ''),
            'user': pr_data.get('user', {}).get('login', ''),
            'html_url': pr_data.get('html_url', ''),
        }

    async def _fetch_pr_files(
        self, github_service: GitHubService
    ) -> list[dict[str, Any]]:
        """
        Fetch files changed in the PR.

        Args:
            github_service: The GitHub service to use for API calls

        Returns:
            List of files changed in the PR
        """
        url = f'{github_service.BASE_URL}/repos/{self.repo_name}/pulls/{self.pr_number}/files'
        files_data, _ = await github_service._make_request(
            url, method=RequestMethod.GET
        )

        return files_data

    async def _create_conversation(
        self, pr_details: dict[str, Any], pr_files: list[dict[str, Any]]
    ) -> str:
        """
        Create a new conversation for PR review.

        Args:
            pr_details: PR details including title, body, and branches
            pr_files: List of files changed in the PR

        Returns:
            The ID of the created conversation
        """
        # Format PR information for the initial message
        pr_title = pr_details['title']
        pr_body = pr_details['body']
        pr_url = pr_details['html_url']
        head_branch = pr_details['head_branch']
        base_branch = pr_details['base_branch']

        # Format files information
        files_info = []
        for file in pr_files:
            filename = file.get('filename', '')
            status = file.get('status', '')
            additions = file.get('additions', 0)
            deletions = file.get('deletions', 0)
            changes = file.get('changes', 0)

            files_info.append(
                f'- {filename} ({status}): +{additions} -{deletions} ({changes} total changes)'
            )

        files_summary = '\n'.join(files_info)

        # Create initial message
        initial_message = f"""
# PR Review: {pr_title}

## PR Information
- Repository: {self.repo_name}
- PR Number: {self.pr_number}
- PR URL: {pr_url}
- Head Branch: {head_branch}
- Base Branch: {base_branch}

## PR Description
{pr_body}

## Files Changed
{files_summary}

Please review this PR and provide feedback on:
1. Code quality and best practices
2. Potential bugs or issues
3. Performance considerations
4. Security concerns
5. Suggested improvements

If you find issues that need fixing, please create a new PR with the necessary changes.
"""

        # Create conversation
        import uuid

        from openhands.server.services.conversation_service import (
            create_new_conversation,
        )

        # Generate a new conversation ID
        conversation_id = uuid.uuid4().hex

        # Create the conversation with the initial message
        ConversationInitData()

        # Use the existing create_new_conversation function
        await create_new_conversation(
            user_id=None,  # Webhook-triggered conversations don't have a user ID
            git_provider_tokens=None,
            custom_secrets=None,
            selected_repository=self.repo_name,
            selected_branch=None,
            initial_user_msg=initial_message,
            image_urls=None,
            replay_json=None,
            conversation_instructions=None,
            conversation_trigger=ConversationTrigger.GUI,  # Use GUI as fallback since API is not defined
            attach_convo_id=False,
            git_provider=None,
            conversation_id=conversation_id,
        )

        return conversation_id

    async def _add_pr_diff_to_conversation(
        self,
        conversation_id: str,
        github_service: GitHubService,
        pr_files: list[dict[str, Any]],
    ) -> None:
        """
        Add PR diff information to the conversation.

        Args:
            conversation_id: The ID of the conversation
            github_service: The GitHub service to use for API calls
            pr_files: List of files changed in the PR
        """
        # For each file, fetch the diff and add it to the conversation
        for file in pr_files:
            filename = file.get('filename', '')
            status = file.get('status', '')
            patch = file.get('patch', '')

            if not patch and status != 'removed':
                # If patch is not included in the API response, fetch the file content
                try:
                    file_url = f'{github_service.BASE_URL}/repos/{self.repo_name}/contents/{filename}'
                    params = {
                        'ref': file.get('blob_url', '').split('/')[-2]
                    }  # Extract commit SHA
                    file_data, _ = await github_service._make_request(
                        file_url, params=params, method=RequestMethod.GET
                    )

                    if file_data.get('type') == 'file':
                        content = f'File: {filename} (New file)\n\n```\n{file_data.get("content", "")}\n```'

                        # Add file content to conversation
                        from openhands.events.action.message import MessageAction

                        # Create a message action
                        message_action = MessageAction(content=content)

                        # Send the message to the conversation
                        await conversation_manager.send_event_to_conversation(
                            conversation_id,
                            {
                                'type': EventType.MESSAGE.value,
                                'data': dataclasses.asdict(message_action),
                            },
                        )
                except Exception as e:
                    logger.error(f'Error fetching file content for {filename}: {e}')
            elif patch:
                # Add patch to conversation
                content = f'File: {filename} ({status})\n\n```diff\n{patch}\n```'

                # Create a message action
                message_action = MessageAction(content=content)

                # Send the message to the conversation
                await conversation_manager.send_event_to_conversation(
                    conversation_id,
                    {
                        'type': EventType.MESSAGE.value,
                        'data': dataclasses.asdict(message_action),
                    },
                )

    async def _add_review_request_to_conversation(self, conversation_id: str) -> None:
        """
        Add a review request message to the conversation.

        Args:
            conversation_id: The ID of the conversation
        """
        review_request = """
Now that you've seen the PR changes, please provide a comprehensive review of the code. Include:

1. A summary of the changes
2. Code quality assessment
3. Potential bugs or issues
4. Performance considerations
5. Security concerns
6. Suggested improvements

If there are issues that need fixing, please describe them in detail.
"""

        # Create a message action
        from openhands.events.action.message import MessageAction

        message_action = MessageAction(content=review_request)

        # Send the message to the conversation
        await conversation_manager.send_event_to_conversation(
            conversation_id,
            {
                'type': EventType.MESSAGE.value,
                'data': dataclasses.asdict(message_action),
            },
        )

    async def _post_review_comment(
        self, github_service: GitHubService, conversation_id: str, review_content: str
    ) -> None:
        """
        Post a review comment on the PR.

        Args:
            github_service: The GitHub service to use for API calls
            conversation_id: The ID of the conversation
            review_content: The content of the review
        """
        # Add link to the conversation
        # Use the server URL from environment or default to localhost
        server_url = os.environ.get('SERVER_URL', 'http://localhost:3000')
        conversation_url = f'{server_url}/conversation/{conversation_id}'
        review_with_link = f'{review_content}\n\n---\n*This review was generated by OpenHands AI. [View the full conversation]({conversation_url}).*'

        # Create review
        url = f'{github_service.BASE_URL}/repos/{self.repo_name}/pulls/{self.pr_number}/reviews'
        payload = {
            'body': review_with_link,
            'event': 'COMMENT',  # Could be APPROVE, REQUEST_CHANGES, or COMMENT
        }

        await github_service._make_request(
            url=url, params=payload, method=RequestMethod.POST
        )

    async def _create_fix_pr(
        self,
        github_service: GitHubService,
        conversation_id: str,
        pr_details: dict[str, Any],
    ) -> Optional[str]:
        """
        Create a new PR with suggested fixes.

        This is a placeholder for future implementation. The actual implementation would:
        1. Create a new branch based on the PR head branch
        2. Apply suggested fixes
        3. Create a new PR targeting the original PR branch

        Args:
            github_service: The GitHub service to use for API calls
            conversation_id: The ID of the conversation
            pr_details: PR details including branches

        Returns:
            URL of the created PR, if successful
        """
        # This is a placeholder - actual implementation would require more complex logic
        # to create a branch, apply fixes, and create a PR
        logger.info(
            f'Auto-fix not implemented yet for PR #{self.pr_number} in {self.repo_name}'
        )
        return None

    async def review_pr(self) -> dict[str, Any]:
        """
        Review a GitHub pull request.

        This method:
        1. Fetches PR details and files
        2. Creates a new conversation
        3. Adds PR diff information to the conversation
        4. Requests a review from the AI
        5. Optionally creates a new PR with fixes

        Returns:
            A dictionary with the review results
        """
        # Get GitHub service
        github_service = await self._get_github_service()

        # Fetch PR details and files
        pr_details = await self._fetch_pr_details(github_service)
        pr_files = await self._fetch_pr_files(github_service)

        # Create conversation
        conversation_id = await self._create_conversation(pr_details, pr_files)

        # Add PR diff to conversation
        await self._add_pr_diff_to_conversation(
            conversation_id, github_service, pr_files
        )

        # Add review request
        await self._add_review_request_to_conversation(conversation_id)

        # The AI will now process the conversation and generate a review
        # We don't wait for the review to complete, as it may take some time

        # Return result with conversation ID
        result = {
            'conversation_id': conversation_id,
            'pr_number': self.pr_number,
            'repo_name': self.repo_name,
        }

        return result
