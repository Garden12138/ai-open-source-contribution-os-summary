"""PR copy requested from the implementation session."""

from dataclasses import dataclass

from contribos.agent.output import final_json

DRAFT_PROMPT = '''Prepare English text for a draft pull request for this task. Read the repository's
PR template, contributing instructions and current changes. Summarize only the changes
in this workspace and report only tests actually run. Use the relevant issue number.
Do not edit files or the index, commit, push, or perform any GitHub writes.
End your reply with a fenced `json` block containing only three string fields:
"title", "body", and "commit_message". Keep the title on one line.'''


@dataclass(frozen=True)
class Draft:
    title: str
    body: str
    commit_message: str
    fallback: bool = False


def draft_from_response(text: str, issue_number: int) -> Draft:
    value = final_json(text)
    if value is not None:
        title, body, message = (value.get(key) for key in ('title', 'body', 'commit_message'))
        if (all(isinstance(item, str) and item.strip() and '\0' not in item for item in (title, body, message))
                and len(title) <= 256 and len(title.splitlines()) == 1
                and len(body) <= 65536 and len(message) <= 8192 and '\r' not in title):
            return Draft(title.strip(), body.strip(), message.strip())
    title = f'Fix issue #{issue_number}'
    return Draft(title, f'## Summary\n\nFixes #{issue_number}.\n\n## Testing\n\nAdd test results before publishing.',
                 title, fallback=True)
