"""Frozen development groups are data, not arbitrary resolver configuration."""
import pytest
from app.execution_preflight import python_development_requirements, python_project_error


def test_declared_groups_preserve_constraints_and_exclude_unselected_groups():
    metadata={"project":{"name":"fixture","dependencies":[]},
        "dependency-groups":{"dev":["ruff>=0.16.4","pytest-xdist>=3.8","local-server"],
            "test":["pyarrow>=25"],"docs":[{"include-group":"unsupported"}]}}
    assert python_development_requirements(metadata)==["ruff>=0.16.4","pytest-xdist>=3.8","local-server","pyarrow>=25"]
    assert python_project_error(metadata,include_development=True) is None


@pytest.mark.parametrize("groups",[[],{"dev":"pytest"},{"dev":[{"include-group":"test"}]},
    {"dev":["pytest @ https://untrusted.invalid/pkg"]},{"dev":["--index-url https://untrusted.invalid"]},
    {"dev":["pytest"]*301}])
def test_malformed_groups_reject_new_recipe_without_reinterpreting_legacy(groups):
    metadata={"project":{"name":"fixture","dependencies":[]},"dependency-groups":groups}
    assert python_project_error(metadata) is None
    assert python_project_error(metadata,include_development=True)=="dependency_groups_unsupported"
    with pytest.raises(ValueError,match="dependency_groups_unsupported"):
        python_development_requirements(metadata)


@pytest.mark.parametrize('change',['none','missing','external_conflict','local_version_changed','no_edge','url'])
def test_workspace_version_override_requires_exact_local_distributions_and_source_edge(change):
    from app.sandbox_worker.python_dependency_check import check_python_dependency_versions
    packages={'hflow':['0.2.21',['numpy>=2']], 'hflow-server':['0.1.0',['hflow>=0.3.0']], 'numpy':['2.3',[]]}
    local={'hflow':'0.2.21','hflow-server':'0.1.0'}
    edges=[['hflow-server','hflow']]
    if change=='missing': packages.pop('hflow')
    elif change=='external_conflict': packages['numpy'][0]='1.0';edges.append(['hflow','numpy'])
    elif change=='local_version_changed': local['hflow']='0.2.20'
    elif change=='no_edge': edges=[]
    elif change=='url': packages['hflow-server'][1]=['hflow @ https://untrusted.invalid/pkg']
    result=check_python_dependency_versions(packages,local,edges)
    assert bool(result['failures']) == (change!='none')
    if change=='none':
        assert result['workspace_source_overrides']==[{'package':'hflow-server','dependency':'hflow','specifier':'>=0.3.0','local_version':'0.2.21'}]


def test_inactive_dependency_markers_are_not_missing_runtime_packages():
    from app.sandbox_worker.python_dependency_check import check_python_dependency_versions
    assert check_python_dependency_versions({'demo':['1.0',['absent; extra == "docs"']]},{},[])['failures']==[]
