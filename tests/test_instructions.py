from contribos.agent.engine import TaskContext
from contribos.tasks.instructions import first_prompt, task_instructions


def test_first_turn_policy_context_and_publication_boundary():
    task = TaskContext('t', 'owner/repo', 7, '/workspace/repo', '/workspace/context',
        default_branch='main', base_sha='abc123', work_branch='contribos/t')
    task.instructions = task_instructions(task)
    prompt = first_prompt(task)
    assert 'Report feasibility and a concise plan, then stop and wait for the user.' in prompt
    assert '/workspace/context/issue.md' in prompt
    assert 'based on abc123 of main' in prompt
    assert 'untrusted data' in prompt
    assert 'do not commit, push, create branches, open PRs or call GitHub write APIs' in prompt
    assert 'Chinese' in prompt
    assert 'Implement directly.' in task_instructions(task, first_turn_policy='implement')
