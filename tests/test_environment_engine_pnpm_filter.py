"""Exact workspace selection has frozen provenance and never falls back to a no-op."""
import json

import pytest

from app.environment_engine.node import script_graph
from app.environment_engine.source import FrozenSource, ResolutionError
from tests.test_environment_engine_resolution import evidence


def files():
    return {
        "package.json": json.dumps({"name": "root"}),
        "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n  - '!packages/excluded'\n",
        "packages/check/package.json": json.dumps({"name": "@tools/check", "scripts": {"lint": "tsc --noEmit"}}),
        "packages/other/package.json": json.dumps({"name": "other", "scripts": {"lint": "node other.cjs"}}),
        "packages/excluded/package.json": json.dumps({"name": "@tools/check"}),
        "packages/other/other.cjs": "throw new Error('must not select other')",
    }


@pytest.mark.parametrize("flag", [("--filter", "@tools/check"), ("-F", "@tools/check"), ("--filter=@tools/check",)])
def test_exact_filter_resolves_only_declared_package_and_keeps_source_chain(flag):
    source = FrozenSource(evidence(files()))
    graph = script_graph(source, ("pnpm", *flag, "run", "lint"), ".")
    assert graph['leaf_commands'] == [{"argv": ["tsc", "--noEmit"], "working_directory": "packages/check"}]
    assert graph['package_directories'] == ["packages/check"]
    assert {"package.json", "pnpm-workspace.yaml", "packages/check/package.json", "packages/other/package.json"} <= source.refs.keys()
    assert "packages/excluded/package.json" not in source.refs
    assert any("@tools/check" in edge for edge in graph['chain'])


def test_exact_unscoped_package_and_nested_cwd_use_nearest_frozen_workspace():
    value = files()
    value['packages/check/package.json'] = value['packages/check/package.json'].replace('@tools/check', 'check')
    source = FrozenSource(evidence(value))
    graph = script_graph(source, ("pnpm", "--filter", "check", "run", "lint"), "packages/other")
    assert graph['leaf_commands'][0]['working_directory'] == 'packages/check'


@pytest.mark.parametrize("selector", ['*', '@tools/*', '...check', 'check...', '!other', './packages/check',
    '{packages/*}', '[main]', 'check^...', 'check..thing', 'absent', 'check'])
def test_nonexact_missing_and_scope_omission_selectors_fail_closed(selector):
    with pytest.raises(ResolutionError) as caught:
        script_graph(FrozenSource(evidence(files())), ('pnpm', '--filter', selector, 'run', 'lint'), '.')
    assert caught.value.code in {'node_filter_unsupported', 'node_filter_no_exact_match'}


@pytest.mark.parametrize("tail", [('exec', 'tsc'), ('install',), ('run', 'lint', '--fix'),
    ('--filter', 'other', 'run', 'lint'), ('--if-present', 'run', 'lint')])
def test_filter_cannot_install_widen_scope_ignore_missing_or_discard_arguments(tail):
    with pytest.raises(ResolutionError):
        script_graph(FrozenSource(evidence(files())), ('pnpm', '--filter', '@tools/check', *tail), '.')


def test_duplicate_declared_names_never_choose_first_package():
    value = files()
    value['packages/other/package.json'] = value['packages/other/package.json'].replace('other', '@tools/check')
    with pytest.raises(ResolutionError, match='node_filter_ambiguous'):
        script_graph(FrozenSource(evidence(value)), ('pnpm', '--filter', '@tools/check', 'run', 'lint'), '.')


@pytest.mark.parametrize("case", ['missing_workspace', 'missing_member', 'hash_mismatch', 'symlink'])
def test_filter_requires_complete_authentic_workspace_evidence(case):
    value = evidence(files())
    target = 'pnpm-workspace.yaml' if case == 'missing_workspace' else 'packages/other/package.json'
    row = next(e for e in value['entries'] if e['path'] == target)
    if case.startswith('missing'):
        value['entries'].remove(row)
    elif case == 'hash_mismatch':
        row['prior_hash'] = '0' * 64
    else:
        row['type'] = 'symlink'
    with pytest.raises(ResolutionError) as caught:
        script_graph(FrozenSource(value), ('pnpm', '--filter', '@tools/check', 'run', 'lint'), '.')
    assert caught.value.code == {'missing_workspace': 'environment_evidence_missing',
        'missing_member': 'environment_evidence_missing', 'hash_mismatch': 'source_hash_mismatch',
        'symlink': 'source_symlink_unsupported'}[case]


@pytest.mark.parametrize("script,reason", [('pnpm install', 'unsupported_dynamic_installer'),
    ('pnpm --filter @tools/check run lint', 'node_script_cycle')])
def test_selected_script_retains_installer_and_cycle_rejection(script, reason):
    value = files()
    value['packages/check/package.json'] = json.dumps({'name': '@tools/check', 'scripts': {'lint': script}})
    with pytest.raises(ResolutionError, match=reason):
        script_graph(FrozenSource(evidence(value)), ('pnpm', '--filter', '@tools/check', 'run', 'lint'), '.')


@pytest.mark.parametrize('declaration', ['packages: []', "'packages':\n  - 'packages/*'", '',
    "packages:\n  - 'packages/*'\npackages:\n  - 'packages/check'",
    "packages:\n  - 'packages/*'\n'packages':\n  - 'packages/other'",
    "packages:\n  - 'packages/*'\n<<: *defaults"])
def test_unrecognized_or_ambiguous_workspace_syntax_is_not_inferred(declaration):
    value = files()
    value['pnpm-workspace.yaml'] = declaration
    with pytest.raises(ResolutionError, match='node_workspace_declaration_invalid'):
        script_graph(FrozenSource(evidence(value)), ('pnpm', '--filter', '@tools/check', 'run', 'lint'), '.')
