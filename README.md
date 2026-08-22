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
                                      `-> private S3 attachments
```

Each DynamoDB partition is keyed by the authenticated Cognito `sub`. The API
never accepts a user ID from ChatGPT, so one user cannot select another user's
partition.

## Tools

- `log_symptoms`
- `list_symptom_entries`
- `update_symptom_entry`
- `delete_symptom_entry`
- `show_attachment_uploader`
- `list_symptom_attachments`
- `get_symptom_attachment`
- `delete_symptom_attachment`

The uploader accepts one JPEG, PNG, HEIC/HEIF, or PDF at a time, up to 20 MiB
and 10 attachments per symptom entry. It uploads directly from the in-chat
picker to a five-minute presigned S3 URL. `start_attachment_upload` and
`complete_attachment_upload` are lower-level tools used by the picker.

Deletion is marked destructive and should be used only on an explicit request.
All write operations return the authoritative stored record or deleted record.

`list_symptom_entries` supports `since`, `until`, `limit`, and `cursor`. Results
include `next_cursor` and `has_more`. Cursors are encrypted, expire after one
hour, and are bound to the authenticated Cognito subject and original filters.
Existing `entries` and `count` response fields are preserved.

New timestamps are stored in a fixed-width UTC representation so DynamoDB sort
key date ranges remain chronological. Before enabling long-range reports, audit
and migrate any legacy records whose sort keys use offsets or variable timestamp
precision.

## Reporting API

Reporting uses `America/Chicago` for local-day grouping. All reporting routes
require the read scope, derive the owner from the access token, return
`Cache-Control: no-store`, and accept optional `since`, `until`, `symptom`, and
`group` filters where applicable. The default period is the previous 30 days
and the maximum is 366 days.

- `GET /reports/summary` — counts, mean/max severity, symptom rollups, coverage
- `GET /reports/timeline` — daily count, mean/max severity, sleep, coverage
- `GET /reports/symptoms` — one normalized row per symptom occurrence
- `GET /reports/correlations?outcome=...&factor=sleep_hours` — conservative
  association output with sample size, suppression, and noncausation warnings
- `GET /reports/export?dataset=occurrences&format=csv` — flattened CSV export
- `GET /reports/clinician-report?specialty=cardiology|rheumatology&format=json|pdf`
  — clinician-focused browser preview data or server-generated PDF
- `GET /entries/{entry_id}/attachments` — private attachment index
- `GET /entries/{entry_id}/attachments/{attachment_id}` — five-minute download

Raw symptom names are retained. A reporting-only alias layer permits exact
selection of `PVC`, `PVCs`, and `palpitations` while also assigning them to the
`cardiology` group. Days without entries remain explicitly missing/unlogged and
are never presented as symptom-free.

Clinician PDFs are generated inside the relay Lambda; report data is not sent
to a third-party document service. Cardiology reports include PVC/palpitation
burden, time-of-day distribution, available context, event wording, treatment
markers, and an ECG attachment index. Rheumatology reports include morning
stiffness, pain location, weekly burden, and transparent possible-flare
detection. A possible flare requires at least three consecutive severity-logged
days above the trailing 14-day mean with at least seven prior logged severity
days. The label is explicitly non-diagnostic.

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

The dashboard uses a separate public Cognito application client with
authorization code plus PKCE and no client secret. Its default callback is
`https://health.loopers.space/callback`; it receives only `openid`, `email`,
and `planelocket-symptoms/read`. API CORS permits only the configured
`DashboardOrigin`. Never place the confidential ChatGPT client secret in the
dashboard repository or browser bundle.

## Local validation

```bash
python -m pip install -r requirements-dev.txt
pytest -q
sam validate --lint
sam build
```

## Privacy and operational notes

- DynamoDB encryption at rest is enabled.
- The attachment bucket is private, blocks all public access, requires TLS, and
  uses S3 server-side encryption. Download links expire after five minutes.
- Upload completion checks the declared byte count, content type, and file
  signature. Unconfirmed uploads are removed after one day.
- Deleting a symptom entry also permanently deletes its attachments.
- Point-in-time recovery is enabled. This may incur a small charge and is not
  required for basic operation; it is enabled because these records are hard to
  reconstruct.
- Lambda logs metadata and exceptions, not request bodies or symptom content.
- The application is a personal recordkeeping tool, not a diagnostic system.
- AWS service eligibility alone does not make the deployment HIPAA compliant.
