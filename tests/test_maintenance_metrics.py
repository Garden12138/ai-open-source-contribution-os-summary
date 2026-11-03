"""Incomplete historical maintenance can never imply autonomous success."""
import pytest

from app.agent_runtime import observe
from app.agent_v2.metrics import journey_metrics
from app.workbench import Workbench
from tests.test_native_protocol_repairs import fixture
from tests.test_agent_native_tools import response


@pytest.mark.parametrize('source_id',[None,{},''])
def test_unverified_maintenance_is_counted_without_changing_history(tmp_path,source_id):
    db,s,task,*_=fixture(tmp_path,response('list_paths',{}))
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        payload={'reason_code':'operator_storage_recovery','counts_as_autonomous':False}
        if source_id is not None:payload['source_id']=source_id
        event=wb.append(task,'agent_maintenance_recorded',payload,key='incomplete-operator-record')
        before=[(e.id,e.record_hash) for e in wb.history(task)]
        observation=observe(wb,task)
        metrics=journey_metrics(wb,task)
        assert observation['maintenance_count']>=1 and observation['autonomous_eligible'] is False
        assert metrics['operator_interventions']>=1 and metrics['native_journey_completed']==0
        assert 'legacy_unverified' in metrics['measurement_note']
        assert [(e.id,e.record_hash) for e in wb.history(task)]==before
        assert next(e for e in wb.history(task) if e.id==event.id).payload==payload
