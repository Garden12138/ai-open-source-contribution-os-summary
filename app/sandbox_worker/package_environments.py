"""Deterministic per-package recipes derived only from a signed frozen bundle."""
from __future__ import annotations


def python_package_environments(plan: dict, metadatas: dict, texts: dict) -> list[dict]:
    """Select each package's dependency closure and its own requirement files."""
    import re
    from pathlib import PurePosixPath

    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    roots = plan['project_directories']
    by_name = {}
    for directory, metadata in metadatas.items():
        name = metadata.get('project', {}).get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
            raise ValueError('Invalid local distribution name')
        key = normalize(name)
        if key in by_name:
            raise ValueError('Duplicate local distribution name')
        by_name[key] = directory

    def belongs(path, directory):
        if PurePosixPath(path).name == 'dev-requirements.txt' and PurePosixPath(path).parent in PurePosixPath(directory).parents:
            return True
        parents = [root for root in metadatas if root == '.' or path.startswith(root + '/')]
        if parents:
            return max(parents, key=lambda value: len(PurePosixPath(value).parts)) == directory
        return False

    result = []
    for root in roots:
        visited = set()
        pending = [root]
        while pending:
            directory = pending.pop()
            if directory in visited:
                continue
            visited.add(directory)
            metadata = metadatas[directory]
            project = metadata.get('project', {})
            declared = list(project.get('dependencies', []))
            for name in ('test', 'tests', 'instruments', 'type-check'):
                declared += project.get('optional-dependencies', {}).get(name, [])
            for name in ('dev', 'test', 'tests'):
                declared += metadata.get('dependency-groups', {}).get(name, [])
            for path, text in texts.items():
                if path.endswith(('.txt', '.lock')) and belongs(path, directory):
                    declared += [line.split(' #', 1)[0].strip() for line in text.splitlines()
                                 if line.strip() and not line.lstrip().startswith('#')]
            for requirement in declared:
                if not isinstance(requirement, str):
                    raise ValueError('Invalid workspace requirement')
                match = re.match(r'^([A-Za-z0-9_.-]+)', requirement)
                target = by_name.get(normalize(match.group(1))) if match else None
                if target is not None and target not in visited:
                    pending.append(target)
        manifests = [manifest for manifest in plan['manifests']
                     if (manifest['path'].endswith('pyproject.toml') and
                         (manifest['path'].rsplit('/', 1)[0] if '/' in manifest['path'] else '.') in visited)
                     or (manifest['path'].endswith(('.txt', '.lock')) and
                         any(belongs(manifest['path'], directory) for directory in visited))]
        result.append({**plan, 'project_directory': root, 'project_directories': [root],
                       'local_projects': sorted(visited - {root}), 'manifests': manifests})
    return result
