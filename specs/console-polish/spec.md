# Spec: console-polish

## Why
The first owner review of the console (feature/owner-console) found it works but reads like a prototype:
unavailable models mixed with usable ones, starters that send at once, text labels where icons belong, plain
tables, raw camelCase labels and bare forms.

## What changes
1. **Model picker.** Usable models first; unavailable ones in a separate "Unavailable" group with the reason.
2. **Quick starts.** A starter fills the composer (editable) instead of sending. The owner can save their own
   starters per agent (stored in MongoDB, `GET/POST/DELETE /admin/v1/starters`) and delete them.
3. **Chat look.**
   - An icon send button.
   - A "jump to newest" button in long conversations.
   - Channel icons in history.
   - A coloured edit (blue) and delete (red).
   - A stronger active-conversation highlight.
   - Answer metadata as chips with icons, including output tokens when known.
4. **Sources.** Grouped by kind (GitHub repository, work project, document, profile), each with an icon.
   Entries with the same title show their category and a short id so they can be told apart (titles redacted to
   `[PERSON]` stay redacted). Traces store each source's category and tier from now on.
5. **History filters.** Quick filters: all, owner, visitor, and each agent.
6. **Tables.** Sortable headers, page-size choice, first / previous / page / next / last, a refresh icon that
   spins while loading, readable labels (Review status, not reviewStatus), default filters (Review: pending),
   a highlighted selected row, the first row selected by default in list-and-detail views, 10 rows per page.
7. **Forms.**
   - Required fields marked with `*`; no "optional" text.
   - Placeholders on free-text fields.
   - Ingest:
     - a drag-and-drop file zone with a clear button;
     - URLs as chips, each with open-in-new-tab and remove;
     - a warning when the tier is `public`, and on GitHub "sync again".
8. **Records.** "Add to batch" appends the form to the batch JSON.
9. **Profile.** Availability is a dropdown (common values plus "Other…").
10. **Leads.** A "Contact" button (mailto) when the lead has an email.
11. **Security.** Kind and severity filters, a bell icon on the test alert, 10 rows per page.

## Not in scope
- Asking a recruiter for contact details in the public chat. This changes the public agent's lead flow; it is a
  separate spec.
- Links from security events to an observability tool.

## Acceptance
1. The picker groups unavailable models last, each with its reason.
2. Clicking a starter fills the composer. A saved starter survives a reload, and a deleted one is gone.
3. Two sources with the same title render distinguishably, and a new trace stores category and tier.
4. Every list can be sorted, paged (size, first and last) and refreshed, with a spinner.
5. The `test_console.py` safety checks still pass, with no `innerHTML`; icons are built with
   `createElementNS`.
