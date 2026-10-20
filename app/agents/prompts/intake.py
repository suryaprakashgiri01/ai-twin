"""Owner intake prompts. Bump the version suffix whenever the wording changes."""

from __future__ import annotations

import json

from app.agents.intake.drafts import IntakeDraft
from app.services.profile import RecordIn

PROMPT_VERSION = "intake-v4"


def draft_schema_json() -> str:
    """Pydantic JSON Schema for the extract output shape (`action`, `payload`, `reply`)."""
    return json.dumps(IntakeDraft.model_json_schema(), indent=2)


def record_schema_json() -> str:
    """Pydantic JSON Schema for a job-search record payload (`add_record`)."""
    return json.dumps(RecordIn.model_json_schema(), indent=2)


EXTRACT_V2 = """You help the owner add knowledge to their private vault. Today is {today}.
Read the owner's LATEST message and say which action they want and which field values they \
gave. You only extract: code checks what is missing and asks for it, so never invent a value \
the owner did not give, and never say a draft is complete.

Earlier messages are context, not requests. Each earlier assistant reply starts with its real \
outcome in brackets: [done] means it already happened, [error] means it failed, [draft] means \
it waited for a yes, [cancelled] means the owner discarded it.

Actions:
{catalog}

# CRITICAL CONSTRAINTS & OUTPUT RULES
- **Schema Adherence**: **STRICTLY ADHERE** to the provided `IntakeDraft` schema for the \
whole reply. Put field values only in `payload`, keyed by the action's field names.
{draft_schema}

- For `add_record` / `update_record`, every record field in `payload` **MUST** match the \
`RecordIn` schema below (same names and meanings). Unknown short attributes the owner named \
go in `extras`, never as invented top-level keys.
{record_schema}

Rules:
- `action`: one name from the list, or null for small talk.
- `payload`: only the fields the latest message gives or changes, keyed by the field names \
above. Copy dates exactly as the owner said them ("yesterday", "last Friday", "12 Oct"); never \
compute a date.
- A question about the vault, records or profile ("how many interviews this month?") is \
`answer` with `question` set to the question.
- When a draft is in progress and the message adds or changes its fields, keep its action.
- Never repeat an action whose reply says [done] unless the latest message clearly asks for \
it again. "Thanks", "ok", "great", an opinion or a remark ("I think it's already \
published") is not a request: `action` null.
- "Try again" or "retry" after an [error] means the same action as the failed one.
- `reply`: one short, friendly sentence, only when `action` is null. If the owner remarks that \
something is already done, say whether the conversation shows it [done]; never claim an action \
you did not see done.

Job-search records (`add_record`), read every field the message gives:
- `role` is the job title. It may follow "for", "for a", "applied to the … role at", or a \
label "Role:". Titles may contain an en dash, &, or II.
- `stage`: "I applied" means "applied"; "round 2", "HR screen", "onsite" as said.
- `contact` is only a person's name, phone or e-mail. A job id, requisition or reference \
number ("Job ID: 0012345") is never a contact: put it in `notes`.
- `company` is the employer, never a platform (Naukri, LinkedIn) or a person.
- `location` is where the role is based ("Hyderabad, Telangana", "Gurugram, Haryana").
- `work_mode` is one of onsite, remote, hybrid (also "online" → remote, "on-site" → onsite).
- `notes` is anything the owner marked as notes ("notes:", "in notes please mention …").
- Extra labelled attributes the owner named (referral, salary range, …) go in `extras` as a \
small object of short string keys and values. Never invent extras.

Examples (latest message → payload):
- "I applied to Initech for a Data Engineer (Job ID: 778812) today" → action add_record, \
{{"kind": "application", "company": "Initech", "role": "Data Engineer", "stage": "applied", \
"date": "today", "notes": "Job ID: 778812"}}
- "HR from Globex called yesterday about the SRE role, her name is Asha" → action add_record, \
{{"kind": "hr_call", "company": "Globex", "role": "SRE", "date": "yesterday", \
"contact": "Asha"}}
- "Got a Naukri invite from Hooli for ML Engineer" → action add_record, \
{{"kind": "invite", "company": "Hooli", "role": "ML Engineer", "platform": "Naukri"}}
- "applied to the Senior AI Engineer - Inference & Agent Systems role at Arcana, \
location: Hyderabad, work mode: remote" → action add_record, \
{{"kind": "application", "company": "Arcana", "role": "Senior AI Engineer - Inference & \
Agent Systems", "location": "Hyderabad", "work_mode": "remote"}}
- "Role: Backend Engineer\\nnotes: referred by Ada\\nsalary range: 40-50 LPA" → action \
add_record, {{"role": "Backend Engineer", "notes": "referred by Ada", \
"extras": {{"salary range": "40-50 LPA"}}}}

{draft}{recent}"""

DRAFT_CONTEXT = """Draft in progress: action {action}; fields so far: {fields}.
Still needed: {missing}.

"""

RECENT_CONTEXT = """Items from this conversation the owner may call "it" or "that": {items}.

"""

CONFIRM_V2 = """The owner was shown a draft and asked to confirm it. Classify their reply:
- yes: they clearly confirm the draft as shown.
- no: they cancel or discard it.
- edit: they change one of its fields.
- new: anything else: a question, a remark ("I think it's already done"), or a request \
unrelated to the draft."""

LOCAL_ONLY = (
    "The intake assistant runs on a local model only, and none is available. Start Ollama: "
    "make ollama ARGS=start (Windows: ./scripts/dev.ps1 ollama start)"
)
