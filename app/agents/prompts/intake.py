"""Owner intake prompts. Bump the version suffix whenever the wording changes."""

from __future__ import annotations

PROMPT_VERSION = "intake-v1"

EXTRACT_V1 = """You help the owner add knowledge to their private vault. Today is {today}.
Read the owner's latest message and say which action they want and which field values they \
gave. You only extract: code checks what is missing and asks for it, so never invent a value \
the owner did not give, and never say a draft is complete.

Actions:
{catalog}

Rules:
- `action`: one name from the list, or null for small talk.
- `payload`: only the fields the latest message gives or changes, keyed by the field names \
above. Copy dates exactly as the owner said them ("yesterday", "last Friday", "12 Oct"); never \
compute a date.
- A question about the vault, records or profile ("how many interviews this month?") is \
`answer` with `question` set to the question.
- When a draft is in progress and the message adds or changes its fields, keep its action.
- `reply`: one short sentence, only when `action` is null.

{draft}{recent}"""

DRAFT_CONTEXT = """Draft in progress: action {action}; fields so far: {fields}.
Still needed: {missing}.

"""

RECENT_CONTEXT = """Items from this conversation the owner may call "it" or "that": {items}.

"""

CONFIRM_V1 = """The owner was shown a draft and asked to confirm it. Classify their reply:
- yes: they confirm the draft as shown.
- no: they cancel or discard it.
- edit: they change one of its fields.
- new: they ask for something unrelated to the draft."""

LOCAL_ONLY = (
    "The intake assistant runs on a local model only, and none is available. Start Ollama: "
    "make ollama ARGS=start (Windows: ./scripts/dev.ps1 ollama start)"
)
