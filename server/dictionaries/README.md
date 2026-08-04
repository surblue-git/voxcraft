# VoxCraft dictionary profiles

This directory is the canonical store for VoxCraft dictionaries. On first use,
`server/userdict.json` is copied non-destructively to `profiles/common.json` and
`sets.json` is created. The legacy file is not deleted.

## Profile schema (version 1)

```json
{
  "schemaVersion": 1,
  "id": "it",
  "name": "IT",
  "description": "IT product and development terms",
  "language": "ja",
  "entries": [
    {
      "observed": "オブシディアン",
      "output": "Obsidian",
      "enabled": true,
      "hotword": true,
      "priority": 100,
      "note": "Product name"
    }
  ],
  "symbols": {},
  "hotwords": ["VoxCraft"],
  "hallucinations": []
}
```

- `id` must match the JSON filename and use lowercase ASCII letters, numbers,
  `.`, `_`, or `-`.
- Each observed spelling is one entry. Several observed spellings may share the
  same output.
- `enabled`, `hotword`, `priority`, and `note` are optional extension fields.
  `priority` orders entry-derived hotwords; larger values are considered first.
- `symbols`, `hotwords`, and `hallucinations` are optional and default to empty.
- Unknown fields are preserved when the legacy `/dict` editor updates `common`.

## Dictionary sets

`sets.json` lists named combinations of profiles. In schema version 1, later
profiles are more specific and override earlier profiles when the same observed
spelling or symbol occurs. Overrides are returned as diagnostics rather than
being hidden.

```json
{
  "schemaVersion": 1,
  "sets": [
    {
      "id": "default",
      "name": "Common",
      "profiles": ["common"],
      "writableProfile": "common"
    }
  ]
}
```

`writableProfile` is the profile that receives quick additions from Obsidian. It
must be one of the set's profiles; when omitted, the last (most specific) profile
is used.

The plugin stores a set ID per server endpoint. The WebSocket start message accepts
`dictionarySetId`; the server resolves it before recording starts and returns the
set ID, semantic revision, profile IDs, profile revisions, and warnings in the
`started` response. Every queued recognition job retains that immutable snapshot,
so editing a JSON file cannot mix dictionary versions within a session.

REST `/reconvert` and `/recognize` accept the same optional `dictionarySetId` and
return the resolved dictionary metadata with their result.

`POST /dictionaries/{profile_id}/entries` safely appends one entry. Clients may
send `expectedRevision`; stale revisions and an existing observed spelling with a
different output return HTTP 409. Repeating the exact same entry is idempotent.
Before a successful write, the prior file is copied to `<profile>.json.bak`.

## Planned CSV/TSV columns

The stable interchange columns are `observed`, `output`, `enabled`, `hotword`,
`priority`, and `note`. Import/export UI belongs to Phase 2; defining the columns
now allows genre dictionaries to be prepared without changing their meaning.
