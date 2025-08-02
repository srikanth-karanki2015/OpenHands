# GitHub Integration

This directory contains the GitHub integration for OpenHands, including:

- GitHub API client for repository operations
- PR creation functionality
- PR review automation

## PR Reviewer

The PR Reviewer is a feature that automatically reviews pull requests using OpenHands AI. It can:

1. Detect new PRs via GitHub webhooks
2. Create a new conversation in OpenHands
3. Analyze the code changes
4. Generate review comments
5. Optionally create a new PR with suggested fixes

### Setup

To set up the PR Reviewer, you need to:

1. Configure a GitHub webhook to point to your OpenHands instance
2. Set the following environment variables:

```
WEBHOOK_SECRET=your_webhook_secret
WEBHOOK_ALLOWED_REPOS=owner/repo1,owner/repo2
WEBHOOK_AUTO_FIX=false
```

### Webhook Configuration

In your GitHub repository settings:

1. Go to Settings > Webhooks > Add webhook
2. Set the Payload URL to `https://your-openhands-instance.com/api/webhooks/github`
3. Set the Content type to `application/json`
4. Set the Secret to the same value as your `WEBHOOK_SECRET` environment variable
5. Select "Let me select individual events" and choose "Pull requests"
6. Click "Add webhook"

### How It Works

1. When a PR is opened or updated, GitHub sends a webhook event to OpenHands
2. OpenHands validates the webhook signature and processes the event
3. A new conversation is created with the PR details and diff
4. The OpenHands AI analyzes the code changes and generates a review
5. The review is posted as a comment on the PR
6. If auto-fix is enabled, OpenHands may create a new PR with suggested fixes

### Security Considerations

- The webhook endpoint validates the signature using the secret to ensure the request is from GitHub
- Only repositories in the allowed list can trigger reviews
- The GitHub token used for API calls should have minimal permissions (read access to PRs and write access for commenting)

