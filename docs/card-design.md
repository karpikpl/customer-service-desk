# Service console in a conversation

The Customer Service Desk experience favors clarity over a dashboard squeezed
into chat: a recognizable masthead, a strong incident title, quiet metadata,
and a small set of relevant next actions. The employee should always know
whether they are **reading**, **proposing**, or **confirming** a change.

## Native card visual language

Use Adaptive Cards 1.5, Teams host typography, wrapped text, Medium/Large title
sizes, Small subtle labels, generous section spacing, and emphasis containers
for the latest update. Status and priority are written out; color is never the
only signal. Honor light, dark, and high-contrast themes.

Every card uses a neutral **Customer** wordmark placeholder alongside SERVICE
DESK / Employee support. It is a native ExtraLarge, bold `TextBlock`, not an
external image or a claim about an actual customer's brand. It follows Teams
theme colors, remains accessible as text, and requires no image host.
Service identity is also included in the card's text fallback.

Use generous white space, strong editorial headings, and restrained dividers.
Replace the shared masthead in `src/service_desk/cards.py` with an approved
customer logo when supplied, preserving accessible identity and theme contrast.
Native Teams cards cannot promise arbitrary fonts, exact brand colors, or
pixel-identical button rendering. Do not add unsupported CSS.

No card has more than three top-level actions. Use five-item incident pages,
compact incident-number actions, short summaries, and separate follow-up cards when
additional choices are genuinely useful. All input fields have visible labels
and validation messages. Cancel actions bypass required-field validation.

## Incident list

Use a native Adaptive Cards 1.5 `Table`, with a header row (the table's default),
four proportional columns, quiet grid lines, and a shaded header:

| Incident | Status | Priority | Updated (UTC) |
| --- | --- | --- | --- |
| **[INC0012345]**<br>Printer unavailable on Floor 2 | In progress | 3 - Moderate | 2026-01-01 10:00:00 |

The incident-number button opens the same ownership-checked detail action as
before. Summaries wrap and are capped at 160 characters with an ellipsis; full
details remain available by opening the incident. Metadata is bounded and
escaped, never interpreted as HTML or Markdown links. Page number and
Previous/Next navigation remain explicit; an empty page shows a neutral message,
not an empty table.

The list requests Teams full-width rendering through the `msteams.width`
extension. Tests validate this extension separately from the unmodified official
Adaptive Cards schema. Clients that support the card but not the native table
get an aligned `ColumnSet` fallback with the same cells and read-only actions.
Five-row adversarial-text payloads remain below the tested 24 KB budget.

## Incident detail

```text
[Customer wordmark]                         SERVICE DESK
                                         Employee support
INC0012345
Printer unavailable on Floor 2

Status     In progress
Priority   3 - Moderate
Updated    <ServiceNow timestamp> UTC

+------------------------------------------------------+
| LATEST CUSTOMER UPDATE                               |
| <customer-visible comment from ServiceNow>            |
+------------------------------------------------------+

DESCRIPTION
<employee's incident description>

[Troubleshoot this issue]    [My incidents]
[View incident in ServiceNow]

[Add comment]    [Resolve]    [Refresh]
                                      [Thumbs up] [Thumbs down]
```

Only actions supported by the current instance policy/state are offered.
Resolve and reopen never change a record directly: they prepare a review.
Read-only cards show current authorized API data, not a model's recollection.
Troubleshooting, incident navigation, and the direct portal link remain available
when writes are disabled; an explicit note explains why comments are unavailable.
Detail and successful receipt links use the authoritative incident sys_id, not
model-supplied identifiers. Portal links use the configured HTTPS instance and
validated relative portal path (default `/sp`), never integration credentials.

Every delivered card has supplementary thumbs-feedback controls. Only incident
details/receipts and actual KB sources include a contextual ServiceNow link;
there is no generic portal/home link on questions, help, or generic responses.
The shared feedback footer preserves the masthead/title/table indices and the limit of
three top-level actions. Completed model replies use the same branded card shell,
with model Markdown limited to a text block, never model-authored card structure.
Model-driven reads and incident-number clicks return the same detail navigation
instead of depending on an extra model-only follow-up card.

## Response feedback

Completed Activity messages, including cards and card updates, carry the AI
entity and Teams default `feedbackLoop` metadata for compatible clients.
Do not also set the deprecated `feedbackLoopEnabled` property: Teams rejects
messages containing both properties.
Cards also contain explicit **👍 Helpful** / **👎 Not helpful** buttons so
ratings remain available when Teams does not render native attachment footers.
Neither set consumes primary action slots. Native clients' default form can
collect an optional comment; card users can type `Feedback: ...`.
`message/submitAction`
feedback invokes return their own acknowledgment and never replace the rated
card, execute an incident write, or advance troubleshooting.

Explicit `Feedback: ...` chat messages can be handled without the model or
ServiceNow. Other feedback and mixed support requests use conservative detection
plus a rating-only model tool; the host supplies the real current user text.
Capture deduplicates within the turn. Normal printer symptoms/step outcomes
remain case data, not assistant feedback.

The isolated feedback sink sends `service_desk.feedback` custom events to the
existing App Insights resource only. Custom dimensions include `is_feedback`,
source, rating, redacted/bounded text, response ID, stable feedback ID, and a
hashed conversation scope. No feedback-specific database, ticket comment,
console propagation, or offline exporter cache is added. See the README for the
filter query, original-chat-history distinction, and buffered-delivery limits.

## Confirmation is a separate visual state

```text
[Customer wordmark]                         SERVICE DESK
                                         Employee support
Review your change

Comment INC0012345
Nothing has been changed. Review exactly what will be sent.

COMMENTS
A test page failed. The printer still appears offline.

This review expires after a short period. Confirm only once.

[Confirm change]    [Edit]    [Cancel]
```

The payload is held server-side. Hidden card input supplies only the action ID
and registered verb. Edit cancels the previous review before presenting its
replacement form. Success appears only after an authoritative ServiceNow
response; a timeout is not painted as either success or a safe-to-repeat error.

## Troubleshooting

Informational KB questions use formatted Teams Markdown rather than a content
card: concise headings, readable bullets, and actual source links, without a generic
ServiceNow portal link. They retain native AI feedback metadata and `/feedback`.
An empty or unavailable search is explicitly distinguished from approved
knowledge. Do not append an unrelated choices card to a completed explanation.
Cards remain appropriate when the user needs to make a choice, enter a fact,
report a check's outcome, view an incident, or review a proposed write.

Load the printer skill for troubleshooting requests, including general requests
for basic steps. When the path is unknown, ask using a branded options card
before retrieving knowledge or giving instructions; do not choose a default.
Skip this question if the user or a saved selection already established the path.
Offer two paths:
**Remote checks** (computer/own queue) and **Local checks** (safe visible
observations). Each option has a label and description; its button carries only
the registered `case_option` verb, saved case token, and option ID.
The reusable `show_case_options` tool supports two or three choices for any
support decision, even before a skill is loaded. Ticket follow-up versus triage,
email-client selection, and printer paths use the same branded card. Never ask
the user to type a numbered menu choice. Wait for selection; do not show a step
while a choice remains unanswered.
Deliver only the interactive question card, not a separate instruction card
telling the user to choose an option or use the question card. The question
and its buttons/input must appear together, including during invoke updates.
If a non-KB reply ends with an unrendered question, allow one model repair turn
within the original timeout to render the matching choices or text-entry card.
Repeated failure surfaces an error, not an optionless question plus unrelated menu.

The server validates the selected option against the current scoped case,
consumes it with an exact ETag, rotates the token, and retains the selected path
for continuation. Unknown, replayed, stale, and cross-conversation choices do
not advance the case. Execute and Submit fallback use the same dispatcher.
An exact typed label or ID also uses that dispatcher; ambiguous or inferred
choices are not accepted, and typed replies cannot confirm a ServiceNow write.
The selected path determines which distinct approved printer KB to search;
draft/unavailable sources are never portrayed as approved guidance.

```text
[Customer wordmark]                         SERVICE DESK
                                         Employee support
Printing / Check the selected printer

Confirm that the intended printer is selected.
Did that solve the problem?

Source: KB0001: Printer checks

[That worked]    [Still not working]    [Create incident]
```

Begin by loading the relevant Toolbox skill and searching approved knowledge.
Follow its questions/procedure one
step at a time. Preserve symptom, impact, attempts, outcome, and the loaded skill
content digest. Step buttons are bound to the latest saved case token so an old
card cannot accidentally advance a different step.

Actual retrieved article citations identify the knowledge source; separate
knowledge-result cards carry authoritative source links. No-match and
unavailable searches are explicitly labeled as general guidance.
The renderer rejects a procedural check until its case has attempted a
knowledge search. A skill is procedural guidance, not a substitute KB citation.

Information questions use `show_case_step(interaction="information")`, not the
outcome buttons above:

```text
Email troubleshooting
What error does Outlook show?

Your answer: [________________________________]

[Continue]    [I don't know]
```

Continue collects only the bounded answer; I don't know bypasses required-input
validation. Both carry the current case token. Answers, choices, and outcomes
are consumed with an ETag and token rotation. Old cards, mismatched interaction
types, cross-user/conversation attempts, and pre-reset contexts cannot advance
the current case. All these cards keep feedback controls, without a generic portal link.

Create incident, shown only when enabled, opens an editable intake populated with the case summary,
followed by a review. Reporting a successful step does not silently resolve an
existing incident.

## Deterministic command cards

`/feedback` opens a labeled feedback text field plus an explicit rating selector;
Submit feedback goes only to the telemetry sink. `/incident` uses the existing
reviewed intake flow when permitted. `/kb` uses a labeled search field and
bounded, source-linked article results, not model-generated summaries.
`/agent`, `/whoami`, and `/help` use deterministic metadata/identity/help cards.
Every response retains thumbs-feedback controls. KB results link to their
specific articles; other command cards do not add a generic portal link.

`/new` starts a logical fresh conversation, not a new Teams thread. Its welcome
explains that model context and case/review applicability were reset, while Teams
history and ServiceNow data remain. Old confirmation and case actions are scoped
to their original generation and cannot be reused after reset.

## State and delivery behavior

| Situation | Experience |
| --- | --- |
| Initial chat | Welcome with My incidents, Report an issue, Troubleshoot. |
| No incidents | Neutral empty state and a Report an issue action. |
| Unmapped identity | Access guidance, no account enumeration or ticket query. |
| Group/channel | Neutral instruction to use a personal chat, no incident details. |
| Card click | Prompt invoke acknowledgment; update the original card after the operation. |
| Expired/stale review | Explain the reason and ask for a fresh review; do not write. |
| Confirmed change | Receipt with authoritative number/status and View/My incidents actions. |
| Ambiguous write outcome | Explain that it must be reconciled before retrying. |
| Continuing case | Contextual step actions; Start a new case is available on follow-up. |
| Unsupported rich client | Plain-language fallback and text-based conversation; never bypass confirmation. |

`Action.Execute` uses `adaptiveCard/action`; `Action.Submit` fallbacks enter the
same validated dispatcher. Automatic refresh is deliberately absent to avoid
render/refresh loops. The JSON fixtures document the visual states but do not
replace a real desktop/mobile Teams rendering review.
