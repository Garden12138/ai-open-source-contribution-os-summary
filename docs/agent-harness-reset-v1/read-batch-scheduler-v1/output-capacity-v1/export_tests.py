"""Publish JUnit projections without local home/email/hostname metadata.
Raw reports stay in /tmp. Counts/outcomes/test ordering are verified unchanged.
"""
from pathlib import Path
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from app.provenance import content_hash
from app.security import ensure_no_sensitive_bytes, redact_text
ROOT=Path(__file__).resolve().parent
FILES=[
 ('capacity-focused.xml','development_deterministic',None),
 ('capacity-new.xml','development_deterministic',None),
 ('capacity-journey.xml','deterministic_coding_verify_review',None),
 ('capacity-final-delta.xml','deterministic_tool_budget_and_crash',None),
 ('capacity-real-oci.xml','development_OCI_and_two_unit_cases',None),
 ('capacity-real-oci-final.xml','development_OCI_and_two_unit_cases',None),
 ('capacity-verify-oci.xml','real_OCI_sandbox_verify',None),
 ('capacity-full-0.xml','development_source_changed_during_run',None),
 ('capacity-full-1.xml','development_source_changed_during_run',None),
 ('capacity-projection-final.xml','development_projection',None),
 ('capacity-final-focused.xml','focused_before_final_freeze',None),
 ('capacity-final-full-0.xml','default_deterministic_no_model_no_network','final'),
 ('capacity-final-full-1.xml','default_deterministic_no_model_no_network','final'),
 ('capacity-final-real-oci.xml','real_OCI_with_two_deterministic_unit_cases','final'),
 ('capacity-final-budget-projection.xml','deterministic_budget_and_complete_projection','final'),
 ('capacity-final-atomic.xml','deterministic_transaction_failure_injection','final'),
]

def redacted(text):
 text=re.sub(r'/Users/[^/\s<>"\']+', '/home/local-user', text)
 text=re.sub(r'/private/var/folders/[^/]+/[^/]+/T', '/tmp', text)
 text=re.sub(r'pytest-of-[^/\s<>"\']+', 'pytest-of-local-user', text)
 text=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[private-email]', text)
 return redact_text(re.sub(r'hostname="[^"]*"', 'hostname="local-test-host"', text))


def stats(raw):
 cases=ET.fromstring(raw).findall('.//testcase')
 return {'tests':len(cases),'failed':sum(c.find('failure') is not None for c in cases),
  'errors':sum(c.find('error') is not None for c in cases),'skipped':sum(c.find('skipped') is not None for c in cases),
  'passed':sum(all(c.find(x) is None for x in ('failure','error','skipped')) for c in cases)}


def main():
 final=json.loads((ROOT/'source-manifest.json').read_text())['source_hash']
 out=ROOT/'test-results';out.mkdir(exist_ok=True)
 reports=[]
 for name,kind,source in FILES:
  path=Path('/tmp')/name
  if not path.exists():continue
  raw=path.read_bytes()
  tree=ET.fromstring(raw)
  for node in tree.iter():
   node.attrib.update({k:('local-test-host' if k=='hostname' else redacted(v)) for k,v in node.attrib.items()})
   if node.text:node.text=redacted(node.text)
   if node.tail:node.tail=redacted(node.tail)
  public=ET.tostring(tree,encoding='utf-8',xml_declaration=True)
  ensure_no_sensitive_bytes(public,context='public test report')
  assert stats(raw)==stats(public)
  a=ET.fromstring(raw).findall('.//testcase');b=ET.fromstring(public).findall('.//testcase')
  assert [(redacted(c.get('classname','')),redacted(c.get('name',''))) for c in a]==[(c.get('classname',''),c.get('name','')) for c in b]
  (out/name).write_bytes(public)
  reports.append({'file':'test-results/'+name,'classification':kind,'source_hash':final if source else None,
   'source_integrity': 'frozen' if source else 'development_not_complete_frozen_acceptance',
   'raw_sha256':hashlib.sha256(raw).hexdigest(),'public_sha256':hashlib.sha256(public).hexdigest(),
   'raw_preserved_locally':True,'outcomes_and_order_unchanged':True,**stats(public)})
 index={'version':'capacity-test-evidence-export-v1','redacted_metadata':['home_identity','temporary_path','email','hostname','credential_like_test_fixture_strings'],
  'original_historical_evidence_changed':False,'new_paid_model_calls':0,'reports':reports}
 index['report_hash']=content_hash(index);(ROOT/'test-report-index.json').write_text(json.dumps(index,indent=2)+'\n')
 print(json.dumps([{k:r[k] for k in ('file','tests','passed','failed','skipped')} for r in reports]))

if __name__=='__main__':main()
