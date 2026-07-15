# Privacy, Cost, and Recovery

## Data boundary

Imprint stores captured words, nearby case context, alternatives, provenance,
and derived records on the local machine. That material can reveal strategy,
preferences, clients, confidential work, or personal information. Treat the data
root, SQLite database, spool, projections, JSON-LD exports, and settings backups
as sensitive.

The core release sends no telemetry and makes no network request. It has no model
dependency. If a future or optional adapter is enabled, its provider receives the
content explicitly sent to it and may charge usage fees under that provider's
terms. No optional adapter is enabled by installation.

Explicit local judgment capture is the only consent-exempt source. Transcripts,
conversation imports, Screenpipe, financial records, behavioral telemetry,
business systems, customer results, and external connectors are denied by
default. A typed `ConsentGrant` must authorize the source, purpose, operation,
sensitivity, retention rule, and effective interval before an Observation or
Outcome can enter the canonical store. Revocation and expiry are evaluated at
write time; they are not configuration comments.

## Permissions and disk protection

Use a private OS account and full-disk encryption. Do not put the data root in a
public repository, support ticket, cloud-sync folder, or shared network drive.
The installer does not change broad directory permissions or grant another user
access. Your OS defaults still apply; inspect them if the machine is shared.

## Backup

Create and verify a transactionally consistent backup with:

```bash
imprint backup create
imprint backup verify /path/from/create.sqlite3
```

The command uses SQLite's backup interface and emits a tamper-evident receipt.
For a whole-directory disaster-recovery copy, stop active sessions and the
compiler, then copy the complete operator directory to encrypted storage.

For a portable logical snapshot:

```bash
imprint export --format jsonld --output imprint-export.jsonld
```

Protect exports like the live bank. Before upgrades, make a physical backup and
record its SHA-256. Additive migrations preserve historical versions, but a
backup is the recovery boundary for hardware loss or operator error.

## Recovery

1. Stop all writers.
2. Preserve the damaged directory before attempting repair.
3. Restore the entire last known-good operator directory to a local path.
4. Point `data_root` at its parent and run `imprint health`.
5. Compare expected counts and hashes before resuming capture.

Never merge two SQLite files. Reconcile immutable spool inputs through one
compiler instead. Quarantined or corrupt inputs should remain preserved for
inspection; do not silently discard them.

Compiler acknowledgement does not delete source input. To prune this producer's
old committed inputs after the configured retention period, run `imprint spool
prune`. An input remains untouched if its acknowledgement, event identity, hash,
producer path, or retention age cannot be verified.

## Uninstall and deletion

Uninstall deliberately preserves captured data. Canonical deletion is a separate
two-step operation:

```bash
imprint delete purge --scope EXACT_NODE_OPERATOR_SESSION_OR_SOURCE_ID --preview
imprint delete purge --scope EXACT_NODE_OPERATOR_SESSION_OR_SOURCE_ID \
  --confirm EXACT_NODE_OPERATOR_SESSION_OR_SOURCE_ID
```

The second command is irreversible. It records only non-content counts, rebuilds
projections, scans the active root, and reports `purged_with_residue` with a
nonzero exit if deletion committed but content remains. Backups and exports
outside the active root are not discoverable and must be inventoried and deleted
separately. Tombstone is the normal reversible removal from current retrieval.

Revoking consent does not silently rewrite historical evidence. If a grant uses
`delete_on_revoke`, the associated source IDs must be purged through the same
explicit preview-and-confirm deletion workflow so backups, exports, and residue
can be reported honestly.
