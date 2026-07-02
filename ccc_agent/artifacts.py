"""Durable review artifacts for completed/frozen sessions.

Everything a human needs to decide commit-vs-abort lands under
``<state>/<session-id>/reviews/`` so sessions can be inspected long after the
agent (and even the node it ran on) is gone.
"""

import json
import os


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def write_review(store, session, changes_by_root, decision, warnings_by_root=None):
    """Write session.json, per-root status/warning JSON, policy decision, summary.md."""
    warnings_by_root = warnings_by_root or {}
    review = store.review_dir(session.session_id)
    os.makedirs(review, exist_ok=True)

    _write_json(os.path.join(review, "session.json"), session.to_dict())
    for root_name, changes in changes_by_root.items():
        _write_json(os.path.join(review, "status.%s.json" % root_name),
                    [c.to_dict() for c in changes])
    for root_name, warnings in warnings_by_root.items():
        _write_json(os.path.join(review, "warnings.%s.json" % root_name),
                    [w.to_dict() for w in warnings])
    _write_json(os.path.join(review, "policy-decision.json"),
                decision.to_dict())

    with open(os.path.join(review, "summary.md"), "w") as fh:
        fh.write(render_summary(session, changes_by_root, decision,
                                warnings_by_root=warnings_by_root))
    return review


def render_summary(session, changes_by_root, decision, warnings_by_root=None):
    warnings_by_root = warnings_by_root or {}
    lines = []
    out = lines.append
    out("# Agent session %s" % session.session_id)
    out("")
    out("| field | value |")
    out("|---|---|")
    out("| agent | %s |" % session.agent_kind)
    out("| command | `%s` |" % " ".join(session.agent_command))
    out("| workspace | %s |" % session.workspace)
    out("| owner | %s |" % session.owner)
    out("| created | %s |" % session.created_at)
    out("| finished | %s |" % (session.finished_at or "-"))
    out("| exit status | %s |" % (session.exit_status
                                  if session.exit_status is not None else "-"))
    out("| completion | %s |" % session.completion)
    out("| policy mode | %s |" % session.policy.get("mode", "-"))
    out("| decision | **%s** |" % decision.decision)
    out("")
    out("## Protected roots")
    out("")
    for name, root in sorted(session.protected_roots.items()):
        changes = changes_by_root.get(name, [])
        out("- `%s`: branch `%s` over `%s` (%d change(s))"
            % (name, root.branch, root.base, len(changes)))
    out("")
    if decision.reasons:
        out("## Decision reasons")
        out("")
        for reason in decision.reasons:
            out("- %s" % reason)
        out("")
    if decision.out_of_scope:
        out("## Out-of-scope paths")
        out("")
        for path in decision.out_of_scope:
            out("- `%s`" % path)
        out("")
    if decision.deny_matches:
        out("## Deny/hide rule matches")
        out("")
        for match in decision.deny_matches:
            out("- `%s` (rule `%s`)" % (match.path, match.pattern))
        out("")
    warning_total = sum(len(warnings)
                        for warnings in warnings_by_root.values())
    if warning_total:
        out("## BranchFS status warnings")
        out("")
        out("These warnings mean status may be incomplete or commit may fail; review before committing.")
        out("")
        for root_name, warnings in sorted(warnings_by_root.items()):
            for warning in warnings:
                out("- `%s` `%s`: %s" % (root_name, warning.path,
                                           warning.message))
        out("")
    out("## Changed paths")
    out("")
    total = 0
    for name, changes in sorted(changes_by_root.items()):
        for change in changes:
            out("- `%s` `%s` (%s, %d bytes)"
                % (change.op, change.path, change.kind, change.bytes))
            total += 1
    if not total:
        out("(none)")
    out("")
    out("## Next steps")
    out("")
    out("```bash")
    out("ccc-agent show %s" % session.session_id)
    out("ccc-agent diff %s          # list changed paths" % session.session_id)
    out("ccc-agent diff %s <path>   # unified diff for one text file" %
        session.session_id)
    out("ccc-agent commit %s   # apply branch to real storage; repeat IDs to batch" %
        session.session_id)
    out("ccc-agent abort %s    # discard branch; repeat IDs to batch" % session.session_id)
    out("```")
    out("")
    return "\n".join(lines)
