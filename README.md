# Customer Service Desk

A Python **Microsoft Foundry hosted agent implementing Activity Protocol** for
Teams personal chat, following the
[Foundry Copilot SDK Activity sample](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/bring-your-own/activity/github-copilot).

The application implements owned incident reads, reviewed creation/comments/
resolution/reopening, native Adaptive Cards, and case-handling skills delivered
through **Foundry Toolbox**. The first skill is printer troubleshooting.
It uses the supplied Foundry model through the Copilot SDK, not a GitHub model
subscription. Shell/file tools, automatic config discovery, MCP tools, and
permission approval are disabled; only explicitly registered service desk tools
are exposed to the model.

This repository uses neutral **Customer** branding and the deployment name
`customer-service-desk`. Configure your own Foundry project endpoint, model,
Toolbox, tenant, ServiceNow instance, and managed secrets before deploying.
Credentials, actual identity mappings, local deployment state, and generated
Teams packages are not committed. The rebrand does not rename existing hosted
resources or migrate their session directories and state stores.

Development acceptance covered authenticated Teams greetings, incident
list/detail actions, Toolbox-backed troubleshooting, and reviewed creation and
customer-visible comments on explicitly synthetic incidents. The historical
version numbers below refer to those development runs, not a deployment of
this Customer-branded source. Writes and knowledge access remain disabled by
default until an administrator verifies instance policy and article audiences.
Cross-user state isolation, mobile/theme coverage, and resolution/reopening
rules still require deployment-specific acceptance. Skills and the state store
are preview features; this is not a production-readiness claim.

The agent greets users by the display name supplied in their authenticated
Teams Activity, asks how to help, and suggests incidents, issue reporting, and
printer troubleshooting. Installation/member-added events and explicit
greetings/help requests use the same deterministic welcome card, without
waiting for Key Vault, Foundry state, the model, or ServiceNow. Missing display
names receive a generic greeting rather than a guessed identity. Other tenants
and group chats never receive a personalized welcome.

Card activities use Adaptive Card `fallbackText` and an Activity `summary`,
not accompanying Activity `text`: Teams splits text plus a card into separate
messages and rejects attempts to update them together. Card invokes acknowledge
promptly, then use a fresh SDK continuation turn with the original authenticated
claims and copied Activity. The original turn's connector is closed when the
invoke handler returns and must not be reused for deferred delivery.
Model replies use completed Teams message activities. Choice and information
questions use a single interactive card containing both the question and its
controls, without a separate optionless "choose an option" instruction card.
If the model ends with a plain support question without rendering a card, the
runtime gives it one bounded repair turn to call the question/choices tool.
If it still fails, it reports the failure rather than displaying a dead menu.
Informational KB answers are exempt from this repair.
Informational KB answers instead use
one formatted Markdown message with actual source links;
they do not get a KB-content card or an unrelated follow-up card.
Live tests encountered HTTP 400 from the SDK's channel-streaming final
envelope, including without attachments, so that optional channel-streaming
path is not used. Copilot's internal event streaming and automatic context
compaction are unchanged.

All cards share a neutral **Customer** wordmark placeholder and a restrained
masthead. The wordmark is native Adaptive Card text, not an invented customer
brand or an external image dependency. Replace the shared masthead in
`src/service_desk/cards.py` with an approved customer logo when one is available.
Incident lists use a full-width, four-column table: incident number
and summary, status, priority, and last update in UTC. Open an incident using its
number button; five-item pagination and caller-scoped access are unchanged.
The native table headers/rows, Next/Previous navigation, and row-to-detail
actions were verified in the mapped tester's real Teams web chat on version 10.
Teams controls typography and button colors; the wordmark follows the host
theme rather than depending on customer-specific fonts or colors.
See [the card design specification](docs/card-design.md) for sources and fallback
behavior.

Incident details include **Troubleshoot this issue**, **My incidents**, and a
direct **View incident in ServiceNow** ticket link even when chat writes are disabled.
When the instance policy enables comments, **Add comment** opens an editable
form followed by a review card; only **Confirm change** submits it. Read-only
policies display an explicit explanation instead of silently hiding the reason.
Only incident details/receipts and actual KB sources link to ServiceNow.
Questions, welcome/help cards, and generic responses have no portal/home link.
These contextual links use the employee's own ServiceNow browser sign-in; the temporary
API caller mapping does not provide browser SSO.

Real Teams acceptance on version 26 returned one printer routing card containing
the question, **Local checks** / **Remote checks** buttons, and feedback controls.
There was no separate instruction card or generic ServiceNow link. This latest
card was left unclicked for the user. Live welcome and KB results also omit the
generic link; actual article links remain, and incident details retain
**View incident in ServiceNow**.

To enable **only** in-chat comments, an administrator must set `allow_comments`
to `true` in the existing `service-desk-snow-policy` Key Vault secret, preserving
all other fields and permissions, then restart the affected session compute.
This requires access to the vault's existing private endpoint; do not open its
public network or grant the runtime secret-write access as a workaround.

### Response feedback

Every card includes explicit **👍 Helpful** / **👎 Not helpful** buttons.
Card-based model replies use branded cards so their feedback controls do not depend on
Teams rendering native footers for attachments. Plain Markdown KB answers use
native feedback metadata and retain the `/feedback` path.
Native Like / Dislike controls were verified on a plain-text KB reply in Teams
on version 19; no companion content or feedback card was needed.
Native default feedback metadata
is retained for clients that support it, including its optional feedback form.
Rating submissions acknowledge without replacing the incident card, advancing
a case, or loading ServiceNow.
Feedback can also be written in chat: explicit `Feedback: ...` messages use a
backend-independent path; conservative phrase detection and a classification-only
model tool handle natural-language and mixed feedback/support requests.
Printer symptoms such as “still not printing” are not assistant feedback.

Feedback telemetry goes **only to the existing Application Insights resource**,
using an independent OpenTelemetry log provider and Azure Monitor exporter.
It does not propagate to console/global OTLP/A365 log providers, write a
ServiceNow comment, or create a feedback database. Exporter offline disk storage
is disabled. Original Teams messages and existing Copilot conversation history
are otherwise unchanged; this is not deletion of the source chat.
Common secret formats and email addresses are redacted and feedback text is
bounded to 4,000 UTF-8 bytes; this is not a comprehensive DLP service.
Do not include passwords or unnecessary personal details.

Configure `DESK_FEEDBACK_CONNECTION_STRING` securely in the azd environment
using the existing Application Insights connection string. The dedicated
variable deliberately does not enable global request/body collection.
No new Application Insights or storage resource is created.
Capture is buffered in memory and exported in batches, so acknowledgments mean
received for telemetry, not guaranteed durable storage; process termination or
ingestion failures can lose queued records. Missing configuration is an explicit
failure, not a false “saved” response.

Both card-rating directions, explicit chat feedback, and model-classified mixed
feedback/support messages were verified in the mapped tester's Teams chat and
queried back from the existing Application Insights resource. Copilot tool
callbacks reuse the already-authorized turn capability rather than reading
Foundry request context, which is not propagated into the SDK callback task.

In **Application Insights → Logs**, filter and deduplicate transport retries:

```kusto
customEvents
| where timestamp > ago(7d)
| where name == "service_desk.feedback"
| extend feedback_id = tostring(customDimensions.feedback_id)
| summarize arg_max(timestamp, *) by feedback_id
| project timestamp,
    source = tostring(customDimensions.feedback_source),
    rating = tostring(customDimensions.feedback_rating),
    feedback = tostring(customDimensions.feedback_text),
    response_id = tostring(customDimensions.response_id),
    conversation_scope = tostring(customDimensions.conversation_scope),
    redacted = tostring(customDimensions.feedback_redacted)
| order by timestamp desc
```

`feedback_source` is `card`, `native`, `conversation`, or `command`; `feedback_rating` is `positive`,
`negative`, or `neutral`. `event_type=service_desk.feedback` and
`is_feedback=true` are stable custom dimensions. Identity/conversation scope is
hashed, not raw Entra IDs or display names. Native ratings target Teams'
`replyToId`; conversational feedback uses a reply target or the latest response
remembered in that process, and can be unlinked after restart.
From the associated Log Analytics workspace, use `AppEvents`, `TimeGenerated`,
`Name`, and `Properties` instead of the Application Insights table/column names.

### Deterministic chat commands

Type these in the **Teams message composer**, not the Copilot CLI command line.
They route before greetings, case-choice matching, and model orchestration.
Neither valid commands nor unknown/invalid slash commands call the model.
Commands require the authenticated Foundry Teams endpoint, the configured tenant,
and a personal chat. Data commands also enforce the caller mapping and instance
policies. Rich-card forms use the same deterministic routing.

| Command | Behavior |
| --- | --- |
| `/feedback` | Open a text/rating form. `/feedback YOUR TEXT` records inline feedback with a neutral rating. No ServiceNow write. |
| `/incident` | Open incident intake when creation is enabled; optional issue title after the command. Review and explicit confirmation are still required. |
| `/kb` | Open a search form, or use `/kb printer` to search published, approved employee knowledge. Does not publish drafts or widen knowledge access. |
| `/agent` | Show host-reported agent/version, application/model/harness details, and advertised skill names. Does not reveal credentials, private config, or system instructions. |
| `/new` | Start a fresh logical agent conversation within the same Teams chat; see reset semantics below. |
| `/incidents` | List **your** incidents across all statuses, including closed ones, five per page. `/incidents 2` opens page 2. Never lists other callers' incidents. |
| `/whoami` | Show your authenticated Teams identity and your own temporary ServiceNow caller ID, or explicitly “Not mapped.” Does not accept identity overrides. |
| `/help` | List the supported commands. |

Commands are case-insensitive. Invalid arguments and unknown commands return
explicit usage guidance rather than falling through to the model.
`/feedback` form and inline submissions use `feedback_source=command` in the
same `service_desk.feedback` custom events and App Insights-only sink.

`/new` discards the current caller-scoped Copilot SDK session, then conditionally
rotates a persisted conversation generation in Foundry state. Subsequent model
sessions, case state, and reviews use the new generation; old case/review cards
cannot operate in it. Confirmation also checks that its generation is current.
The reset pointer is refreshed on stateful turns under the existing state-retention
policy. The old SDK session is discarded before rotation so expiration cannot
revive pre-reset model history. A failed reset is reported, not claimed as success.
Teams chat history, ServiceNow records, and existing telemetry are not deleted;
already-confirmed or in-flight operations are not undone. `/new` is therefore
not a request to create a second physical Teams chat.

Incident creation and approved-knowledge search remain unavailable where the
current administrator policy disables them. These commands do not bypass that
policy or require new integration permissions.

## Architecture

```text
Teams personal chat -> Foundry Activity endpoint -> authenticated Activity identity
  -> tenant/object-ID mapping -> guarded tools -> ServiceNow Table API
  -> Copilot SDK -> Toolbox MCP skill resources (progressive loading)
  -> deterministic Adaptive Cards and contextual continuation actions

Foundry session $HOME: persisted Copilot sessions
Foundry durable state store: reviewed drafts, write receipts, case progress
Existing Key Vault: integration credentials, temporary user mapping, instance policy
```

No additional Azure Storage account, custom MCP server, web UI, or second agent
is needed. ServiceNow APIs stay in the application; Toolbox supplies procedures,
not a route around authorization.

## Local development

Use Python 3.13 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --python 3.13.7
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The default tests use synthetic users, mocked HTTP/MCP clients, and test-only
state doubles. They do not call Foundry or ServiceNow. Card fixtures are
validated against the vendored official Adaptive Cards schema.

### Opt-in ServiceNow integration tests

Set `DESK_SNOW_URL` in `.env` and place a credentials-only JSON object with
`username` and `password` in `.secrets`, with file mode `0600`. This file is
excluded from source control and build/deployment uploads; never paste its
contents into a command or chat.

```bash
uv run pytest tests/integration/test_snow_api.py --run-snow-integration -q
# Optional alternate credentials-only file:
uv run pytest tests/integration/test_snow_api.py --run-snow-integration \
  --snow-credentials-file /secure/path/snow.json -q
```

These four read-only tests verify authentication, incident access, instance
state/resolution metadata, caller-scoped application reads, and retrieval of only
published allowlisted printer KBs, excluding any remaining drafts. They never
create, update, or resolve incidents. They require API ACLs for `incident` and
`sys_choice` and `kb_knowledge`, plus an existing incident with a caller.
The printer-KB check skips when its optional demo articles have not been seeded.
Integration tests are
skipped unless explicitly enabled. A credentials-only `.secrets` file is
**not** a valid `DESK_LOCAL_CONFIG_FILE`, which requires the full configuration
object described below.

```bash
docker build -t customer-service-desk:local .
```

The image pre-stages the Copilot SDK runtime during build. Nothing is installed
by the model at runtime. `uv.lock` fixes Python dependencies.

Copy `.env.example` to `.env` and replace the non-secret placeholders. Running
`uv run service-desk` starts the SDK's health and Activity endpoints. **Real
ticket access intentionally fails closed outside a Foundry-hosted request with
caller context.** Do not expose a local development endpoint publicly or fake
platform headers to bypass this guard. Use synthetic unit fixtures for local
development and a deployed test agent for identity/state integration.

## Required configuration

| Variable | Meaning |
| --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | Existing project endpoint; injected by Foundry when hosted. |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | Existing Responses-compatible model deployment, or `<connection>/<model>` for Foundry BYOM routing. |
| `TOOLBOX_ENDPOINT` | Same-project Toolbox MCP consumer endpoint, including `?api-version=v1`. |
| `DESK_TENANT_ID` | Allowed Teams Entra tenant UUID. |
| `DESK_SNOW_URL` | HTTPS ServiceNow instance origin, no credentials/path/query. |
| `DESK_SNOW_PORTAL_PATH` | Trusted relative portal path; default `/sp`, e.g. `/esc` if configured on your instance. No query, fragment, or alternate origin. |
| `DESK_FEEDBACK_CONNECTION_STRING` | Existing Application Insights connection string for isolated feedback telemetry. Required to capture feedback. |
| `DESK_KEY_VAULT_URL` | Existing secret vault accessible to the agent identity. |
| `DESK_CREDENTIALS_SECRET` | Optional secret name; default `service-desk-snow-credentials`. |
| `DESK_IDENTITY_SECRET` | Optional secret name; default `service-desk-identity-map`. |
| `DESK_POLICY_SECRET` | Optional secret name; default `service-desk-snow-policy`. |
| `DESK_CONFIRMATION_TTL` | Optional review lifetime in seconds; default 600. |

Create the following secrets through your approved secret-management workflow,
not through Teams or the model:

- Credentials secret: JSON object with `username` and `password`.
- Identity-map secret: JSON array matching
  [`config/identity-map.example.json`](config/identity-map.example.json).
  All included identifiers are synthetic.
- Policy secret: JSON matching
  [`config/service-now.example.json`](config/service-now.example.json).

The agent identity needs **Key Vault Secrets User**, scoped to its three
configuration secrets (or to a dedicated vault), and permission to invoke the
model, consume the Toolbox, and use the Foundry state store.
Use minimal ServiceNow integration permissions. Credentials are Basic-auth API
credentials; no user OAuth/OBO integration is assumed.

Grant this read-only role to the **hosted agent's runtime identity**, obtained
from the deployed agent, not automatically to the project infrastructure
identity or to the deploying user's account. The deploying account needs
separate secret-write permission for uploads. RBAC does not provide network
connectivity: a private vault also requires working private endpoint routing
and DNS from the hosted environment. Keep TLS verification and public-network
restrictions enabled. This environment's VPN initially resolved the vault to
its public address; credential synchronization used the existing private
endpoint without changing vault network settings.

Use the actual deployed agent's instance principal for role assignments.
Scope secret access to the configured credentials, identity-map, and policy
secrets, not unrelated secrets in a shared vault. The runtime also needs
**Foundry User** on the existing project for project-service access. This
runtime assignment does **not** authorize Teams users to invoke the agent.

With `BotServiceRbac`, each end user separately needs **Foundry Agent Consumer**
at this agent's ARM scope. Grant it to each authorized tester; they do not need
the runtime identity's Key Vault or project roles. Verify the assignments and
allow time for RBAC propagation.
See Microsoft's [publishing authorization troubleshooting guide](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/troubleshoot-publish-copilot-authorization).
`azd deploy` did not create these runtime or end-user assignments automatically.

For an APIM-backed Foundry project without native model deployments, a bare
model name is insufficient. Use the existing connection/model identifier;
the configured static OpenAI connection with `gpt-5.2` has been verified through
the project Responses API. This preserves the chosen model and does not
provision a replacement deployment. Copilot sessions explicitly set
`reasoning_summary="none"` on both creation and resume: this connection's model
rejects the harness's default `reasoning.summary` request parameter. Actual
Copilot SDK inference, manual harness compaction, and synthetic-label recall
after compaction have also been verified against this route. This does not
disable reasoning or automatic context compaction.

For testing configuration loading only, `DESK_LOCAL_CONFIG_FILE` can point to a
mode-0600 JSON file containing `credentials`, `identities`, and `policy`.
Local files are rejected when hosted. They, `.env`, and actual mapping/policy
files are excluded from images and deployment uploads.

Configuration is loaded once per hosted process. Rotate credentials/mappings/
policy by restarting or deploying a new agent version; pending reviews are
revalidated against the current process policy before execution.

### Instance policy and knowledge visibility

Local administrative setup and opt-in ServiceNow API checks use the owner-only
`.secrets` credentials file; those calls do not require Key Vault connectivity.
The hosted agent instead loads credentials, identity mappings, and its access
policy from Key Vault. `.secrets` is excluded from the container, and hosted
local-file overrides are rejected. A valid ServiceNow password does not enable
incident creation or approve a knowledge audience; those are separate policy
choices in the vault.

Writes are **off by default**. Before enabling them, verify field names,
integration ACLs, `sys_mod_count`, instance state values, resolution codes, and
permitted transitions. Fill `states`, `resolve_from`, `resolved_state`,
`reopen_from`, `reopened_state`, and `resolution_codes` from your actual instance.
Do not copy assumed standard state numbers into the policy.

`allow_create` permits only title/description plus the server-selected caller.
`allow_comments` permits only customer-visible `comments`, never `work_notes`.
No arbitrary priority, assignment, caller, URL, encoded query, or table is
accepted from the model.

Knowledge search is enabled only when `knowledge_audience_verified` is true and
`employee_knowledge_bases` is populated. An administrator must verify that
**every published, active article in those bases is appropriate for all mapped
employees**. To approve only specific articles in a shared base, also populate
`employee_knowledge_articles` with their `sys_id` values. Queries apply both
allowlists and reject returned articles outside the article set; audience
verification then concerns only that selected subset. An empty article
allowlist retains the original whole-base behavior. Published does not mean
universally visible. If bases use
article-level restricted audiences, do not enable this broad integration-account
search until delegated audience filtering is implemented. When knowledge is
unavailable, the agent labels any advice as general guidance.

## Toolbox skills

The Toolbox endpoint supplies skills as **MCP resources**, not executable tool
definitions. The adapter discovers metadata, then reads full instructions only
when a relevant case needs them. An active case stores the exact loaded content
and SHA-256 digest, so publishing a new default does not change an in-progress
procedure. New cases can discover new compatible skills without redeploying.

The Toolbox catalog contains these focused procedures:

| Skill | Scope |
| --- | --- |
| [`printer-troubleshooting`](skills/printer-troubleshooting/SKILL.md) | Ask Local/Remote on a card when the path is unknown, then use only the matching printer KB. |
| [`email-troubleshooting`](skills/email-troubleshooting/SKILL.md) | Outlook, webmail/OWA, mobile mail, and current versus historical email issues. |
| [`connectivity-troubleshooting`](skills/connectivity-troubleshooting/SKILL.md) | Network, Wi-Fi, and existing workplace VPN observations. |
| [`account-access-troubleshooting`](skills/account-access-troubleshooting/SKILL.md) | Sign-in, MFA, account lockout, and access-denied questions; no account administration. |
| [`general-support`](skills/general-support/SKILL.md) | KB-first employee questions and other supported software/device issues. |

Toolbox version 2 is published with these five references. The four new skills
are version 1; the printer skill is version 3. Their returned MCP instruction
bodies were compared with the local sources. `/agent` shows the expanded catalog.
Teams verification on agent version 18 covered email-client buttons, selecting
Outlook, submitting a symptom through the question form, and receiving a
matching outcome-check card. Knowledge was unavailable during that historical
check, so it was explicitly labeled general guidance. The later administrator
setup enables only the two reviewed printer articles, not general email knowledge.

Support answers search approved knowledge first and cite actual returned
articles. A troubleshooting check cannot be rendered until its case has attempted
a knowledge search. Actual source links are shown on knowledge-result cards;
only citation metadata, not article HTML, is saved with the case. Informational
KB explanations are formatted Markdown with source links, not Adaptive Cards.
Guided-check cards retain their actual KB links alongside the outcome buttons.
Empty or unavailable knowledge is labeled as general guidance, not a fabricated
official procedure. These changes do not publish articles or expand audience
permissions. A different explicitly requested topic can start a fresh case via
`load_case_skill(start_new_case=true)`; continuing the same case retains its
pinned instructions. This does not change an incident or confirm a pending review.

For printer troubleshooting questions, including general requests for basic
steps, load the printer skill first. If the path is unknown, ask **Local checks**
or **Remote checks** using `show_case_options`, then wait for the click before
retrieving the matching article or providing steps. Do not assume a default or
combine both procedures. Preserve an explicit path or saved selection without
asking again. After selection, answer general questions in concise Markdown;
for an active issue, collect only missing facts and guide one safe step at a time.
Local means observations at the physical printer, not repairs; remote means
user-guided computer checks, never remote-control access.
Initial printer skill version 2 was read back through the existing
Toolbox. Initial Teams testing covered the options card, a remote button selection,
and an exact typed at-printer selection, with the correct path-specific
unavailable-KB messages and clearly labeled general guidance while the articles
were drafts.
Skills cannot supply credentials, run scripts, grant tools, change identity,
or authorize writes.

`show_case_options` is a reusable model tool for two or three choices, each
with an ID, label, and short description. It saves the question/options in the
identity-and-conversation-scoped case before rendering the branded card. It
also works before a skill is loaded, for choices such as ticket follow-up versus
email triage. The agent must use it instead of a "reply 1, 2, or 3" menu.
Buttons submit only the option ID and current case token, never a prompt,
source URL, or write payload. Selection is validated against the saved options,
consumed with an ETag-conditional write, and passed back as data to the pinned
skill. Replays, stale cards, unknown choices, and cross-conversation clicks fail.
`show_case_step` cannot bypass an unanswered choices card. Information gathering
uses `interaction="information"`: a text field with **Continue** / **I don't know**.
Actual checks use `interaction="outcome"`: **That worked** / **Still not working**,
plus incident intake only when creation is enabled. Answers and outcomes are
consumed once with an ETag and token rotation, just like choices; none authorizes
a ticket write.
An exact typed option ID or label (case-insensitive) follows the same dispatcher.
Ambiguous replies require a button; arbitrary text and typed approval are never
inferred as a choice or write confirmation.

### Printer knowledge articles

| Article | Purpose | Current status |
| --- | --- | --- |
| KB0010001 — Printer troubleshooting - remote checks | Computer selection, approved connectivity, own queue, one test print | Published; exact-ID allowlist |
| KB0010002 — Printer troubleshooting - local checks | Visible label/display and own output; stop for hardware or consumable warnings | Published; exact-ID allowlist |

Their source drafts are
[`knowledge/printer-remote.html`](knowledge/printer-remote.html) and
[`knowledge/printer-local.html`](knowledge/printer-local.html).
The articles were initially added as drafts to the existing Knowledge base,
then published using the native **Publish** action and existing approval flow.
The later local naming/content revision used native **Checkout** and **Publish**;
the exact-ID allowlist now refers only to the two new published versions.
Their reviewed content and audience controls were preserved. The base's read
criteria permit users and explicitly deny the guest account; the native guest
access check also returned false for each article before publication.
The agent's approved scope contains only these two exact article `sys_id` values
and their base, not all knowledge in that shared base. Article numbers are not
unique in this mocked instance: never publish, modify, or allowlist by number alone.
Guarded API retrieval returned both published articles and excluded unrelated
knowledge. Other topics without approved sources still use labeled general guidance.
Knowledge search matches literal title fragments, not combined full-text keywords.
The tool schema advertises this explicitly; the persona starts with a short topic
and retries one shorter relevant fragment before treating an empty result as no match.
On version 22, the same natural-language request for both printer paths returned
a plain Markdown summary with both actual approved article links in Teams, with
no extra content/choice card or invented source.
That deliberately constrained prompt was a capability smoke check, not realistic
acceptance. User-experience checks must use ordinary messages such as
"My printer isn't printing" or "I'm standing next to it and the red light is
flashing", without telling the agent which tool, output mode, sources, or safety
instructions to use. For an unspecified path, check that the skill produces a
Local/Remote card before any steps. For a known or selected path, check that the
response uses only that article, asks only relevant missing questions, and does
not combine procedures or change tickets.
Historical version-23 checks used those ordinary symptom messages and
"What are the basic troubleshooting steps for a printer?" The symptom flow
asked about the local device display; the overview returned plain Markdown with
only KB0010002 and native feedback controls. Its implicit local default was
subsequently corrected: an unspecified path now requires the clickable question.
Live version-24 testing used the exact same basic-steps question without hints:
the agent displayed **Local checks** / **Remote checks** buttons and waited.
Selecting **Local checks** replaced the question card with a Markdown overview
citing only the local KB; no combined procedure or extra intake was shown.

For repeatable administrative setup, preview first:

```bash
uv run python scripts/seed_printer_knowledge.py --knowledge-base EXISTING_BASE_SYS_ID
# Explicitly create only missing drafts; existing articles are never modified:
uv run python scripts/seed_printer_knowledge.py --knowledge-base EXISTING_BASE_SYS_ID --apply
```

This maintenance script is not a model tool. It reads `.env` and owner-only
credentials, and has no publication option. A knowledge owner must review and
publish the articles through ServiceNow's approval workflow, then approve their
audience before enabling knowledge access. Prefer an article allowlist containing
only the two returned `sys_id` values rather than exposing a shared base wholesale.
Existing-article lookup filters `latest=true`, so historical versions do not
make a repeat preview ambiguous or cause a duplicate draft.

Using installed `azd` Foundry extensions, with your endpoint set in the shell:

```bash
azd auth login
azd ai skill create printer-troubleshooting \
  --file ./skills/printer-troubleshooting/SKILL.md \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT"

# Create each other catalog skill using its corresponding SKILL.md, for example:
azd ai skill create email-troubleshooting \
  --file ./skills/email-troubleshooting/SKILL.md \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT"

azd ai toolbox create customer-service-desk \
  --from-file ./toolbox/service-desk.yaml \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT"
```

For an existing skill, preserve earlier immutable versions:

```bash
azd ai skill update printer-troubleshooting \
  --file ./skills/printer-troubleshooting/SKILL.md
```

The skills-only Toolbox follows the skill's default version; active cases
continue using their pinned contents until reset.

For an existing compatible Toolbox:

```bash
azd ai toolbox skill add YOUR_TOOLBOX email-troubleshooting \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT"
# Inspect the returned new version, test it, then explicitly promote it:
azd ai toolbox publish YOUR_TOOLBOX RETURNED_VERSION \
  --project-endpoint "$FOUNDRY_PROJECT_ENDPOINT"
```

For a batch addition, `toolbox skill add --from-file` accepts a document
containing only `skills`, not the full create definition's `description` or
`tools` fields. Branch additions from the intended version so unrelated
references are preserved.

Skills-only creation with `tools: []` and the application's resource
discovery/loading adapter have been verified against this project, despite
older CLI help advertising a nonempty-tools requirement. Do **not** add a dummy
Web Search/Code Interpreter tool. The adapter does not expose any Toolbox tools
even when a reused Toolbox contains them.

Validate with MCP `initialize`, paginated `resources/list`, and `resources/read`
against a version-specific endpoint before publishing. An empty `tools/list`
is expected for a skills-only Toolbox. Raw resource shape, required preview
headers/capabilities, and region availability must be checked against the live
project. An unavailable Toolbox produces a clear limitation; independent ticket
reads remain available.

## Deploy through azd

`azure.yaml` binds a project service to the **existing project endpoint**, with
no model deployments to provision. Do not run the original sample's provisioning
flow unchanged.

After supplying the existing project ARM ID, model deployment, unique agent
name, and approved existing ACR project connection, initialize the environment
using the installed CLI's existing-project/container flow:

```bash
azd env new snow-agent --subscription "$AZURE_SUBSCRIPTION_ID" \
  --location "$AZURE_LOCATION"
azd ai agent init --src . \
  --project-id "$PROJECT_RESOURCE_ID" \
  --model-deployment "$AZURE_AI_MODEL_DEPLOYMENT_NAME" \
  --agent-name "$DESK_AGENT_NAME" \
  --deploy-mode container \
  --acr-connection "$ACR_CONNECTION"
```

Review the adopted manifest and generated environment. Preserve the Activity
protocol, `BotServiceRbac` endpoint authorization, and the existing project/model.
The native CLI may add a duplicate agent service and a default model deployment
during initialization. Remove those additions: keep one service with all
required runtime settings and an endpoint-only existing project service.
Do not provision an unsolicited model.
Keep the agent's `name` a literal valid name in `azure.yaml`; the installed
extension validates it before expanding custom environment references.

Set the non-secret values in the azd environment:

```bash
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME "$AZURE_AI_MODEL_DEPLOYMENT_NAME"
azd env set FOUNDRY_PROJECT_ENDPOINT "$FOUNDRY_PROJECT_ENDPOINT"
azd env set TOOLBOX_ENDPOINT "$TOOLBOX_ENDPOINT"
azd env set DESK_TENANT_ID "$DESK_TENANT_ID"
azd env set DESK_SNOW_URL "$DESK_SNOW_URL"
azd env set DESK_KEY_VAULT_URL "$DESK_KEY_VAULT_URL"
azd env set DESK_CREDENTIALS_SECRET "$DESK_CREDENTIALS_SECRET"
azd deploy service-desk
```

If supporting resources or permissions are missing, stop and approve them
explicitly before provisioning through `azd`. No standalone infrastructure
deployment scripts are included. Follow the generated Teams setup guide for the
personal-scope app, Bot/Teams channel registration, and tenant sideload policy.
No group/channel installation or ticket sharing is supported.

### Personal Teams app package

The generated package is `dist/teams/appPackage.zip`; sideload it as a custom
app in Teams. It is bound to the deployed bot identity and restricted to
personal chat. Installing and testing it still requires an authorized Teams
user; configure explicit test-account mappings. Other accounts remain
unmapped and are denied ticket access.

To regenerate the base package:

```bash
azd ai agent pack service-desk --scope personal \
  --display-name "Customer Service Desk" --output-dir ./dist/teams
```

**Inspect the manifest before installing a regenerated package.** The installed
extension's `--scope personal` controls distribution, not bot installation
scopes: its generated manifest still enables team/group/Copilot scopes and file
support. Extract the ZIP, restrict each bot's `scopes` to `["personal"]`, set
`supportsFiles` to `false`, remove `copilotAgents`, and repackage the manifest and
icons at the ZIP root. Generated packages are not included in the repository.
Do not install the unmodified generated package for this application.

### Investigating silent Teams delivery

A hosted version being active does not prove that the Bot channel can deliver
messages. Inspect the Azure Bot messaging endpoint and Teams channel, then
check sessions for the actual test user:

```bash
azd ai agent endpoint show service-desk
azd ai agent sessions list --agent-name customer-service-desk \
  --user-identity USER_OBJECT_ID
azd ai agent monitor service-desk --session-id SESSION_ID \
  --user-identity USER_OBJECT_ID
```

Keep `BotServiceRbac` enabled. A direct Azure CLI bearer token is not a Bot
Framework token and is expected to fail against that endpoint; do not remove
authentication or forge sender/call headers to make a probe pass. A Direct
Line probe without a real Teams Entra sender receives an upstream HTTP 403;
it cannot verify Teams-user authorization under `BotServiceRbac`.
Check the **end user's** agent-scoped Foundry Agent Consumer assignment,
separately from runtime permissions, then test from the authenticated user's
personal Teams chat. The real Teams path now works for greetings,
incident-list/detail invokes, model-driven incident reads, and printer-step
cards. Version 9 was verified with an authenticated mapped tester, including
saved-case continuation and clearing only the synthetic test case.
The bot's `BotRequest` diagnostics now flow to the existing `log-analytics`
workspace through the `service-desk-delivery` diagnostic setting.
Do not switch to an APIM/protocol bridge without an explicit design decision.

### Deployed acceptance gate

Before broader use, test with at least two mapped users and an unmapped user:

1. Verify sender tenant/object ID on both message and invoke Activities; ensure
   platform endpoint authorization prevents forged direct requests.
2. Verify state-store user isolation, unchanged opaque call-ID forwarding, and
   message/invoke consistency; local tests cannot establish this boundary.
3. List/view owned incidents and deny another caller's incident without
   revealing whether it exists. Group/channel requests must reveal no details.
4. Create/comment/resolve/reopen approved mock incidents only after reviewing the
   exact change. Exercise cancel, edit, expiry, stale records, and double-clicks.
5. Resume a case/review after idle or process restart. Ensure the original card
   updates after prompt invoke acknowledgment, including on mobile Teams.
6. Complete printer troubleshooting using the Toolbox-loaded skill; publish a
   second compatible skill and verify discovery for a new case without a
   code redeployment. Review light/dark/high-contrast and narrow layouts.

## Experience design

See [the card design specification](docs/card-design.md) and the fourteen
[synthetic JSON card snapshots](tests/fixtures/cards) for concrete designs.
Cards use native Teams typography, spacing, labels, and theme-aware colors.
Arbitrary CSS/custom fonts/red buttons are not available. The default Customer
wordmark is an explicit placeholder; no external company logo is loaded.

## Reliability and operational limits

- Only `caller_id == mapped ServiceNow user` is authorized. A Teams display name
  or identity mentioned in chat has no authority. Re-check ownership on each
  card interaction and commit.
- A random action ID references a server-side draft. Card data never grants
  authority or supplies the committed payload. A typed "yes" cannot commit.
- Confirmations use ETag claims in the built-in Foundry state store. Expired,
  stale, cancelled, and consumed reviews cannot trigger another write.
- Review freshness uses a pre-write read of `sys_mod_count`; it is **not an
  atomic ServiceNow compare-and-swap**. If strict cross-system optimistic locking
  is required, add an approved instance-side transactional API before rollout.
- A store claim and ServiceNow write are not a distributed transaction. Timeout,
  crash, or a lost receipt can leave `executing` / `outcome_unknown`. Never
  automatically retry that write. Check ServiceNow history/records using approved
  support procedures before issuing a new review. Creates and journal comments
  have no assumed universal idempotency key.
- ServiceNow reads retain a 12-second timeout. Confirmed writes wait up to
  60 seconds for a receipt because instance processing can delay a response
  after committing the change; the connection timeout remains five seconds.
  Mutation responses request only raw incident ID, number, and caller, then
  fetch the owned incident separately for display fields and journal text.
  The caller and returned incident ID must match before a success receipt.
  Writes are still sent only once, and an eventual timeout remains
  `outcome_unknown`, not a success claim or automatic retry.
  Two initial synthetic creates committed but their expanded response transactions
  hit the instance's maximum execution time. Their records were verified by
  direct read without replaying either action.
- Invoke requests are acknowledged promptly; deferred work updates the original
  card. Active work is tracked for graceful shutdown, but is not a guaranteed
  durable job queue. A crash after claim requires the same reconciliation.
- Drafts expire after 10 minutes by default; state-store items expire 30 days
  after their last write. Active case reset clears procedural context, not
  ServiceNow records or the Copilot transcript. Treat model/session retention
  and any longer-term audit requirements separately.
- Stored case progress pins skill contents/digest, not an unverified version
  label. New capabilities still require code, even when new procedures do not.
- Unexpected errors are logged by class/correlation context, not with raw
  response bodies, credentials, internal errors, or personal records.

The official schema in `tests/fixtures/adaptive-card.schema.json` was obtained
from `https://adaptivecards.io/schemas/adaptive-card.json`; its MIT license is
retained alongside it. Test users and dates in card snapshots are illustrative.
