# PlaneLocket Symptom Relay

Private, OAuth-protected symptom logging for two people through ChatGPT. The
deployment follows the same GitHub-to-AWS pattern as
`PlaneLocket/planelocket-cookbook-relay`.

## Architecture

```text
ChatGPT Work custom app
        |
        | Cognito OAuth authorization-code flow
        v
Amazon Cognito User Pool
        |
        | per-user access token (`sub` identifies the owner)
        v
API Gateway -> Lambda REST/MCP adapter -> DynamoDB
```

Each DynamoDB partition is keyed by the authenticated Cognito `sub`. The API
never accepts a user ID from ChatGPT, so one user cannot select another user's
partition.

## Tools

- `log_symptoms`
- `list_symptom_entries`
- `update_symptom_entry`
- `delete_symptom_entry`

Deletion is marked destructive and should be used only on an explicit request.
All write operations return the authoritative stored record or deleted record.

## Deployment

Pushes to `main` run tests, SAM validation/build, and deploy to AWS account
`379549361690` in `us-east-2` through GitHub OIDC. No AWS access keys are stored
in GitHub.

The deploy workflow assumes:

```text
arn:aws:iam::379549361690:role/planelocket-symptom-github-deploy
```

That role must trust GitHub's OIDC provider and restrict its subject to this
repository. Mirror the cookbook deployment role, replacing its repository
condition with:

```text
repo:PlaneLocket/planelocket-symptom-relay:ref:refs/heads/main
```

The first deployment uses `https://chatgpt.com` as a safe OAuth callback
placeholder.

## First deployment sequence

1. Create the GitHub OIDC deployment role above by adapting the cookbook role.
2. Push or merge this project to `main`.
3. Open the completed **Deploy Symptom Relay** workflow and record its stack outputs.
4. In Cognito (`us-east-2`), create the two allowed users in
   `planelocket-symptom-mcp`. Public self-registration is disabled.
5. In ChatGPT Work, enable Developer Mode and create a custom app using the
   `McpUrl` stack output.
6. Retrieve the Cognito OAuth client secret privately from AWS. Do not commit it
   or paste it into chat.
7. Copy the exact callback URL displayed by ChatGPT into the repository Actions
   variable `MCP_OAUTH_CALLBACK_URL`.
8. Rerun the deployment workflow, then complete OAuth once from each person's
   ChatGPT account.

OAuth scopes:

```text
openid
email
planelocket-symptoms/read
planelocket-symptoms/write
```

## Local validation

```bash
python -m pip install -r requirements-dev.txt
pytest -q
sam validate --lint
sam build
```

## Privacy and operational notes

- DynamoDB encryption at rest is enabled.
- Point-in-time recovery is enabled. This may incur a small charge and is not
  required for basic operation; it is enabled because these records are hard to
  reconstruct.
- Lambda logs metadata and exceptions, not request bodies or symptom content.
- The application is a personal recordkeeping tool, not a diagnostic system.
- AWS service eligibility alone does not make the deployment HIPAA compliant.

