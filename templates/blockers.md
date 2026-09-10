# Human blockers — the ONLY reasons an item waits on the owner (PROTOCOL §12, template)

Copy this file to `<repo>/.folder-lock/blockers.md` (or `<repo>/blockers.md`) and edit it for your
installation. `scripts/autorun.py` pastes it into every fired agent's prompt; `lib/items.py` only enforces
that a `waiting_owner` item carries a decision-ready question (concrete question, 2–3 options, recommendation).

An item is `waiting_owner` **only** if one of these applies:

- it needs a secret, credential or access the owner has not provided
- it spends money or commits to a contract
- it has an irreversible external side-effect on data (a production push to an accounting/ERP system,
  deleting or overwriting data with no recovery path)
- it is a business decision with no spec and no precedent in the folder's `memory.md`
- there are two materially different interpretations of the spec, and picking wrong would cost more than asking

Explicitly **NOT** blockers:

- sending external e-mail (allowed — apply your own "draft, show, send" rule if you have one)
- assigning work to another workfolder (allowed — write a handoff; it becomes a board item with `created_by` + `owner`)
- ambiguity a reasonable default resolves: **decide, log the decision in the folder's `memory.md`
  (one dated line), continue**

The owner's phrase for all of this: *the owner is the last resort, not the scheduler.*
