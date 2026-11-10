"""Adversarial N08 contract tests; evidence exports names/counts/pins only."""
import argparse
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('n08_usage', ROOT / 'skills/agent-workflow-v1-1/tools/extract-usage.py')
u = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(u)


class ScopedUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.tokens = dict(input_tokens=100, cached_input_tokens=60, cache_write_input_tokens=0,
                           output_tokens=20, reasoning_output_tokens=5, total_tokens=120)
        self.rows = [self.row('session_meta', dict(id='thread1', session_id='thread1'), 0),
                     self.row('turn_context', dict(turn_id='turn1', model='gpt-6.1-sol', effort='high'), 1),
                     self.row('response_item', dict(type='function_call', call_id='call1', name='read', arguments='private-body'), 2),
                     self.token('resp1', 3),
                     self.row('response_item', dict(type='function_call_output', call_id='call1', output='ç'), 4)]

    def row(self, kind, payload, seconds):
        return dict(type=kind, payload=payload, timestamp=f'2026-10-09T00:00:{seconds:02}.000Z')

    def token(self, rid, seconds, **extra):
        p = dict(thread_id='thread1', session_id='thread1', turn_id='turn1', response_id=rid,
                 usage=copy.deepcopy(self.tokens), thread_token_usage=copy.deepcopy(self.tokens))
        p.update(extra)
        return self.row('token_usage_record', p, seconds)

    def source(self, rows=None, ranges=None, **kwargs):
        rows = self.rows if rows is None else rows
        p = self.base / ('source' + str(len(list(self.base.glob('*.jsonl')))) + '.jsonl')
        p.write_text(''.join(u.canonical(r) for r in rows), encoding='utf-8')
        out = dict(source_id='main', path=str(p), thread_id='thread1', session_id='thread1',
                   task_id='task1', agent_path='/root', ranges=ranges or [[1, len(rows)]],
                   requested_model=None, requested_effort=None, main_client_selected=True)
        out.update(kwargs)
        return out

    def run_data(self, sources=None, phases=None):
        return dict(run_id='run1', sources=sources or [self.source()], phases=phases or [], coverage_gaps=['full_run_unmeasured'])

    def document(self, run=None):
        return u.extract(dict(runs=[run or self.run_data()], comparison_pairs=[], checks=[], evidence_pins=[]))

    def phase(self, phase='implementation', source='main', start=0, end=10):
        return dict(phase=phase, task_id='task1', source_id=source,
                    start=f'2026-10-09T00:00:{start:02}.000Z' if start is not None else None,
                    end=f'2026-10-09T00:00:{end:02}.000Z' if end is not None else None,
                    authority='captured_ledger')

    def test_raw_cache_excluded_without_subset_double_count(self):
        r = u.extract_run(self.run_data())
        self.assertEqual(r['totals']['total_tokens'], 120)
        self.assertEqual(r['totals']['cache_excluded_input_tokens'], 40)
        self.assertEqual(r['totals']['output_tokens'], 20)
        self.assertEqual(r['tool_output_utf8_representation_bytes'], 2)

    def test_duplicate_response_same_usage_not_additive(self):
        self.rows.insert(4, copy.deepcopy(self.rows[3]))
        r = u.extract_run(self.run_data())
        self.assertEqual(r['response_count'], 1)
        self.assertEqual(r['totals']['input_tokens'], 100)

    def test_duplicate_response_different_usage_conflict(self):
        r = self.token('resp1', 4)
        r['payload']['usage']['cached_input_tokens'] = 1
        self.rows.insert(4, r)
        got = u.extract_run(self.run_data())
        self.assertIsNone(got['totals']['input_tokens'])
        self.assertIn('duplicate_response_scope_or_usage_conflict', got['reasons'])

    def test_duplicate_response_different_turn_scope_conflict(self):
        self.rows.insert(4, self.token('resp1', 4, turn_id='foreign'))
        self.assertIsNone(u.extract_run(self.run_data())['response_count'])

    def test_duplicate_cross_range_not_new_completion(self):
        self.rows.append(copy.deepcopy(self.rows[3]))
        got = u.scan(self.source(ranges=[[5, 6]]))
        self.assertIn('duplicate_response_cross_range', got['reasons'])
        self.assertIsNone(got['response_count'])

    def test_two_cumulative_streams_never_merged(self):
        huge = {k:v*1000 for k,v in self.tokens.items()}
        self.rows.insert(2, self.row('event_msg',dict(type='token_count',info={'total_token_usage':huge}),1))
        got = u.extract_run(self.run_data())
        self.assertEqual(got['totals']['total_tokens'],120)
        self.assertTrue(all(c['delta'] is None for c in got['sources'][0]['cumulative_streams']))

    def test_cumulative_reset_no_false_delta(self):
        self.rows.append(self.token('resp2',5,thread_token_usage={k:0 for k in self.tokens}))
        got=u.scan(self.source())
        diag=next(d for d in got['cumulative_streams'] if d['stream'].startswith('token_usage_record'))
        self.assertIn('cumulative_reset_or_restart',diag['reasons'])
        self.assertIsNone(diag['delta'])
        self.assertEqual(got['totals']['total_tokens'],240)

    def test_missing_baseline_and_interval_unknown(self):
        got=u.scan(self.source(ranges=[[3,5]]))
        diag=next(d for d in got['cumulative_streams'] if d['stream'].startswith('token_usage_record'))
        self.assertIn('cumulative_baseline_missing',diag['reasons'])
        self.assertIsNone(diag['delta'])
        self.rows.append(self.token('resp2',5))
        got=u.scan(self.source(ranges=[[1,3],[5,6]]))
        self.assertIsNone(got['totals']['total_tokens'])
        self.assertIn('range_gaps_unmeasured',got['reasons'])

    def test_restart_not_zero(self):
        self.rows.append(copy.deepcopy(self.rows[0]))
        got=u.scan(self.source())
        self.assertIsNone(got['totals']['total_tokens'])
        self.assertIn('session_restart_or_multiple_headers',got['reasons'])

    def test_cache_subset_violation(self):
        self.rows[3]['payload']['usage']['cached_input_tokens']=101
        self.assertIsNone(u.scan(self.source())['totals']['total_tokens'])

    def test_reasoning_subset_violation(self):
        self.rows[3]['payload']['usage']['reasoning_output_tokens']=21
        self.assertIn('usage_subset_violation',u.scan(self.source())['reasons'])

    def test_numeric_bool_float_negative_missing_reject(self):
        for bad in (True,1.5,-1,None):
            with self.subTest(case=type(bad).__name__):
                x=copy.deepcopy(self.tokens);x['input_tokens']=bad
                with self.assertRaises(u.Invalid):u.usage(x)

    def test_strict_json_duplicates_and_nonfinite(self):
        for value in ('{"a":1,"a":2}','{"a":NaN}','{"a":Infinity}'):
            with self.assertRaises(u.Invalid):u.strict_json(value)

    def test_missing_response_id_not_zero(self):
        del self.rows[3]['payload']['response_id']
        got=u.scan(self.source())
        self.assertIsNone(got['response_count'])
        self.assertIsNone(got['totals']['output_tokens'])

    def test_ambiguous_thread_and_session_scope(self):
        for key in ('thread_id','session_id'):
            rows=copy.deepcopy(self.rows);rows[3]['payload'][key]='foreign'
            self.assertIn('response_source_scope_ambiguous',u.scan(self.source(rows))['reasons'])

    def test_cross_source_duplicate_scope_conflict(self):
        a=self.source();b=self.source(source_id='other')
        got=u.extract_run(self.run_data([a,b]))
        self.assertIsNone(got['totals']['total_tokens'])
        self.assertIn('cross_source_response_scope_or_usage_conflict',got['reasons'])

    def test_selected_response_chronology(self):
        self.rows.append(self.token('resp2',2))
        self.assertIn('response_chronology_nonmonotonic',u.scan(self.source())['reasons'])

    def test_missing_tool_output(self):
        self.rows.pop()
        got=u.scan(self.source())
        self.assertIsNone(got['tool_output_utf8_representation_bytes'])
        self.assertIn('tool_output_missing_or_outside_range',got['tool_reasons'])

    def test_tool_response_mapping_exact_or_explicit_unknown(self):
        got=u.scan(self.source())
        self.assertEqual(got['tool_outputs'][0]['response_id'],'resp1')
        self.rows.insert(4,self.token('resp2',3))
        got=u.scan(self.source())
        self.assertIsNone(got['tool_outputs'][0]['response_id'])
        self.assertEqual(got['tool_outputs'][0]['response_mapping_reason'],'response_association_missing_or_ambiguous')
        self.assertEqual(got['tool_output_utf8_representation_bytes'],2)

    def test_tool_output_kind_mismatch(self):
        self.rows[4]['payload']['type']='custom_tool_call_output'
        got=u.scan(self.source())
        self.assertIn('tool_call_output_kind_mismatch',got['tool_reasons'])
        self.assertIsNone(got['tool_output_utf8_representation_bytes'])

    def test_missing_ranges_and_malformed_source_contract(self):
        s=self.source();del s['ranges']
        with self.assertRaises(u.Invalid):u.scan(s)
        with self.assertRaises(u.Invalid):u.scan(None)
        with self.assertRaises(u.Invalid):u.extract(None)

    def test_malformed_cumulative_scope_no_fallback(self):
        self.rows.insert(2,self.row('event_msg',dict(type='token_count',info='invalid'),1))
        got=u.scan(self.source())
        self.assertEqual(got['totals']['total_tokens'],120)
        diag=next(d for d in got['cumulative_streams'] if d['stream'].startswith('event_msg'))
        self.assertIn('cumulative_fields_missing_or_invalid',diag['reasons'])

    def test_structured_utf8_uses_persisted_order(self):
        self.rows[4]['payload']['output']={'z':'ç','a':'x'}
        got=u.scan(self.source())
        self.assertEqual(got['tool_output_utf8_representation_bytes'],len('{"z":"ç","a":"x"}'.encode('utf8')))

    def test_overlapping_phase_union_vs_agent_interval_sum(self):
        p=[self.phase(source='a',start=0,end=10),self.phase(source='b',start=5,end=15)]
        got=u.phase_summary(p)
        self.assertEqual(got['observed_wall_union_seconds'],15)
        self.assertEqual(got['observed_agent_interval_seconds_sum'],20)
        self.assertIsNone(got['agent_active_seconds_sum'])
        self.assertIsNone(got['complete_wall_union_seconds'])

    def test_phase_missing_capture_and_invalid_chronology(self):
        got=u.phase_summary([self.phase(start=None)])
        self.assertIsNone(got['intervals'][0]['duration_seconds'])
        with self.assertRaises(u.Invalid):u.phase_summary([self.phase(start=10,end=1)])

    def test_external_wait_not_agent_interval_sum(self):
        got=u.phase_summary([self.phase(),self.phase('external_wait',start=10,end=20)])
        self.assertEqual(got['observed_wall_union_seconds'],20)
        self.assertEqual(got['observed_agent_interval_seconds_sum'],10)

    def test_faster_more_tokens_and_more_costly_visible_synthetic(self):
        a=u.extract_run(self.run_data(phases=[self.phase(end=20)]));b=copy.deepcopy(a)
        b['run_id']='run2';b['phases']['observed_wall_union_seconds']=10;b['totals']['total_tokens']=200
        a['cost'].update(amount=10,currency='USD');b['cost'].update(amount=20,currency='USD')
        got=u.compare(a,b)
        self.assertTrue(got['faster_but_more_raw_tokens'])
        self.assertTrue(got['faster_but_more_costly'])
        self.assertFalse(got['economic_acceptance'])

    def test_cost_missing_or_currency_mismatch_unknown(self):
        a=u.extract_run(self.run_data());b=copy.deepcopy(a);b['run_id']='run2'
        self.assertIsNone(u.compare(a,b)['cost_amount_change'])
        a['cost'].update(amount=10,currency='USD');b['cost'].update(amount=20,currency='EUR')
        self.assertIsNone(u.compare(a,b)['faster_but_more_costly'])

    def test_missing_model_and_cost_metadata_remain_unknown(self):
        self.rows.pop(1)
        r=self.document()['runs'][0]
        obs=r['sources'][0]['responses'][0]['observed']
        self.assertIsNone(obs['model']);self.assertIsNone(obs['effective_model'])
        self.assertEqual(obs['reason'],'turn_context_model_or_effort_missing')
        self.assertIsNone(r['cost']['amount']);self.assertIsNone(r['cache_miss_cause'])

    def test_body_canary_excluded_without_reasoning_classification(self):
        marker='private-'+'sentinel-42'
        self.rows[4]['payload']['output']=marker
        self.rows[3]['payload']['private_body']=marker
        self.rows.insert(3,self.row('response_item',dict(type='reasoning',content=marker),2))
        d=self.document();target=self.base/'safe.json'
        u.publish(d,target,[marker])
        self.assertNotIn(marker,target.read_text(encoding='utf8'))
        self.assertEqual(d['runs'][0]['totals']['output_tokens'],20)

    def test_metadata_canary_rejected_before_publication(self):
        marker='private-'+'sentinel-42'
        self.rows[1]['payload']['model']=marker
        d=self.document();target=self.base/'rejected.json'
        with self.assertRaisesRegex(u.Invalid,'private_canary_export_rejected'):u.publish(d,target,[marker])
        self.assertFalse(target.exists())

    def test_canary_unicode_checked_as_values_not_json_escapes(self):
        d=self.document();d['runs'][0]['sources'][0]['requested']['model']='ç-model'
        target=self.base/'rejected.json'
        with self.assertRaises(u.Invalid):u.publish(d,target,['ç-model'])
        self.assertFalse(target.exists())

    def test_exact_schema_closed_nested_object(self):
        d=self.document();u.schema_validate(d)
        d['runs'][0]['sources'][0]['responses'][0]['private_body']='omitted'
        with self.assertRaisesRegex(u.Invalid,'schema_additional_property'):u.schema_validate(d)

    def test_schema_required_type_and_unknown_keywords(self):
        d=self.document();del d['runs'][0]['cost']
        with self.assertRaises(u.Invalid):u.schema_validate(d)
        with self.assertRaises(u.Invalid):u.schema_validate({}, {'not':{}})
        with self.assertRaises(u.Invalid):u.schema_validate(True,{'type':'integer'})

    def test_canonical_reproducible_output_no_overwrite(self):
        d=self.document();a=self.base/'a.json';b=self.base/'b.json'
        self.assertEqual(u.publish(d,a),u.publish(d,b));self.assertEqual(a.read_bytes(),b.read_bytes())
        with self.assertRaises(FileExistsError):u.publish(d,a)

    def test_pinned_prefix_append_ignored_pin_drift_rejected(self):
        source=self.source();raw,p=u.prefix(source['path']);source.update(prefix_bytes=p['scanned_prefix_bytes'],sha256=p['sha256'])
        with Path(source['path']).open('a',encoding='utf8') as f:f.write(u.canonical(self.token('resp2',5)))
        self.assertEqual(u.scan(source)['response_count'],1)
        source['sha256']='0'*64
        with self.assertRaises(u.Invalid):u.scan(source)

    def test_cli_reject_does_not_print_private_source_value(self):
        marker='private-'+'sentinel-42'
        bad=self.base/'manifest.json';bad.write_text(u.canonical({'private':marker}),encoding='utf8')
        target=self.base/'output.json'
        result=subprocess.run([sys.executable,'-B',str(ROOT/'skills/agent-workflow-v1-1/tools/extract-usage.py'),'--manifest',str(bad),'--output',str(target)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertNotIn(marker,result.stdout+result.stderr);self.assertFalse(target.exists())

    def test_exact_child_header_mapping(self):
        rows=copy.deepcopy(self.rows)
        rows[0]['payload'].update(id='child1',source={'subagent':{'thread_spawn':{'parent_thread_id':'thread1','agent_path':'/root/child'}}})
        rows[3]['payload']['thread_id']='child1'
        good=self.source(rows,thread_id='child1',agent_path='/root/child',main_client_selected=False,requested_model='gpt-6.1-sol',requested_effort='high')
        self.assertEqual(u.scan(good)['response_count'],1)
        good['agent_path']='/root/wrong'
        self.assertIn('child_identity_mapping_mismatch',u.scan(good)['reasons'])

    def rejected_mutation(self, mutate):
        document = self.document()
        mutate(document)
        # All these attacks pass structural schema validation: publication must
        # independently enforce semantic relationships before file creation.
        u.schema_validate(document)
        target = self.base / 'semantic-rejection.json'
        with self.assertRaises(u.Invalid):
            u.publish(document, target)
        self.assertFalse(target.exists())

    def test_publication_rejects_impossible_response_subset(self):
        for field, value in (('cached_input_tokens',101),('cache_write_input_tokens',101),('reasoning_output_tokens',21),('total_tokens',121)):
            with self.subTest(field=field):
                self.rejected_mutation(lambda d: d['runs'][0]['sources'][0]['responses'][0]['usage'].update({field:value}))

    def test_publication_rejects_aggregate_arithmetic_at_both_levels(self):
        for level in ('run','source'):
            for field in (*u.FIELDS,'cache_excluded_input_tokens'):
                with self.subTest(level=level,field=field):
                    def mutate(d):
                        r=d['runs'][0] if level=='run' else d['runs'][0]['sources'][0]
                        r['totals'][field]+=1
                    self.rejected_mutation(mutate)

    def test_missing_tool_prevents_coverage_complete_with_all_phases(self):
        self.rows.pop()
        m=self.run_data(phases=[self.phase(p) for p in u.PHASES]);m['coverage_gaps']=[]
        d=self.document(m);r=d['runs'][0]
        self.assertFalse(r['coverage_complete'])
        self.assertIsNone(r['phases']['complete_wall_union_seconds'])
        self.assertIn('run_coverage_incomplete',r['phases']['reasons'])
        self.assertIsNone(r['tool_output_utf8_representation_bytes'])
        u.publish(d,self.base/'honest-incomplete.json')

    def test_declared_gaps_force_complete_wall_null(self):
        d=self.document(self.run_data(phases=[self.phase(p) for p in u.PHASES]))
        r=d['runs'][0]
        self.assertEqual(r['phases']['observed_wall_union_seconds'],10)
        self.assertIsNone(r['phases']['complete_wall_union_seconds'])
        self.assertFalse(r['coverage_complete'])
        u.publish(d,self.base/'bounded.json')

    def test_malformed_prior_same_id_outside_range_invalidates_selected(self):
        for damage in ('usage','timestamp','turn'):
            with self.subTest(damage=damage):
                rows=copy.deepcopy(self.rows);early=self.token('resp1',0)
                if damage=='usage':del early['payload']['usage']['input_tokens']
                if damage=='timestamp':early['timestamp']='bad'
                if damage=='turn':del early['payload']['turn_id']
                rows.insert(2,early)
                r=u.scan(self.source(rows,ranges=[[4,6]]))
                self.assertIn('duplicate_response_prior_invalid',r['reasons'])
                self.assertIsNone(r['response_count'])
                self.assertTrue(all(v is None for v in r['totals'].values()))

    def test_duplicate_timestamp_advances_chronology_watermark(self):
        rows=copy.deepcopy(self.rows);rows.insert(4,self.token('resp1',8));rows.append(self.token('resp2',5))
        r=u.scan(self.source(rows))
        self.assertIn('response_chronology_nonmonotonic',r['reasons'])
        self.assertIsNone(r['response_count'])

    def test_duplicate_earlier_timestamp_and_equal_timestamp_boundaries(self):
        rows=copy.deepcopy(self.rows);rows.append(self.token('resp1',2))
        self.assertIn('response_chronology_nonmonotonic',u.scan(self.source(rows))['reasons'])
        rows=copy.deepcopy(self.rows);rows.append(self.token('resp1',3));rows.append(self.token('resp2',3))
        self.assertEqual(u.scan(self.source(rows))['response_count'],2)

    def test_malformed_later_same_id_outside_range_invalidates_selected(self):
        rows=copy.deepcopy(self.rows);later=self.token('resp1',5);del later['payload']['usage']['input_tokens'];rows.append(later)
        r=u.scan(self.source(rows,ranges=[[1,5]]))
        self.assertIn('usage_fields_missing_or_invalid',r['reasons'])
        self.assertIsNone(r['totals']['total_tokens'])

    def test_unrelated_malformed_unselected_id_does_not_poison_totals(self):
        rows=copy.deepcopy(self.rows);earlier=self.token('other',0);del earlier['payload']['usage']['input_tokens'];rows.insert(2,earlier)
        r=u.scan(self.source(rows,ranges=[[4,6]]))
        self.assertEqual(r['response_count'],1)
        self.assertEqual(r['totals']['total_tokens'],120)

    def test_full_captured_contract_can_be_complete(self):
        m=self.run_data(phases=[self.phase(p) for p in u.PHASES]);m['coverage_gaps']=[]
        d=self.document(m);r=d['runs'][0]
        self.assertTrue(r['coverage_complete']);self.assertEqual(r['phases']['complete_wall_union_seconds'],10)
        self.assertIsNone(r['phases']['agent_active_seconds_sum'])
        u.publish(d,self.base/'complete-synthetic.json')

    def test_token_errors_prevent_complete_wall(self):
        self.rows[3]['payload']['usage']['cached_input_tokens']=101
        m=self.run_data(phases=[self.phase(p) for p in u.PHASES]);m['coverage_gaps']=[]
        r=self.document(m)['runs'][0]
        self.assertFalse(r['coverage_complete']);self.assertIsNone(r['phases']['complete_wall_union_seconds'])
        self.assertIsNone(r['totals']['input_tokens'])

    def test_publication_rejects_fabricated_completeness_and_wall(self):
        self.rejected_mutation(lambda d:d['runs'][0].update(coverage_complete=True))
        self.rejected_mutation(lambda d:d['runs'][0]['phases'].update(complete_wall_union_seconds=10))

    def test_publication_rejects_null_count_total_and_reason_mismatches(self):
        for level in ('run','source'):
            for field in ('response_count','totals','reasons'):
                with self.subTest(level=level,field=field):
                    def mutate(d):
                        r=d['runs'][0] if level=='run' else d['runs'][0]['sources'][0]
                        r[field]={'response_count':None,'totals':u.null_totals(),'reasons':['invented_error']}[field]
                    self.rejected_mutation(mutate)

    def test_publication_rejects_phase_arithmetic_and_reason_mutations(self):
        for field,value in (('observed_wall_union_seconds',2),('observed_agent_interval_seconds_sum',2),('missing_phase_types',[]),('reasons',[])):
            with self.subTest(field=field):
                self.rejected_mutation(lambda d:d['runs'][0]['phases'].update({field:value}))
        d=self.document(self.run_data(phases=[self.phase()]))
        for field,value in (('duration_seconds',11),('reason','phase_capture_missing'),('start','bad'),('end','2026-10-08T23:59:59Z')):
            with self.subTest(field=field):
                bad=copy.deepcopy(d);bad['runs'][0]['phases']['intervals'][0][field]=value
                target=self.base/'phase-bad.json'
                with self.assertRaises(u.Invalid):u.publish(bad,target)
                self.assertFalse(target.exists())

    def test_phase_missing_endpoint_still_validates_known_endpoint(self):
        p=self.phase(start=None);p['end']='bad'
        with self.assertRaises(u.Invalid):u.phase_summary([p])
        r=u.phase_summary([self.phase(start=None)])
        self.assertIsNone(r['intervals'][0]['duration_seconds'])

    def test_phase_task_source_join_rejects_foreign_task(self):
        p=self.phase();p['task_id']='foreign'
        with self.assertRaisesRegex(u.Invalid,'phase_task_unmapped'):
            self.document(self.run_data(phases=[p]))
        d=self.document(self.run_data(phases=[self.phase()]))
        d['runs'][0]['phases']['intervals'][0]['task_id']='foreign'
        target=self.base/'foreign-phase.json'
        with self.assertRaises(u.Invalid):u.publish(d,target)
        self.assertFalse(target.exists())

    def test_publication_rejects_response_identity_range_timestamp_mutations(self):
        for field,value in (('source_id','foreign'),('task_id','foreign'),('thread_id','foreign'),('session_id','foreign'),('line',99),('timestamp','bad')):
            with self.subTest(field=field):
                self.rejected_mutation(lambda d:d['runs'][0]['sources'][0]['responses'][0].update({field:value}))
        self.rejected_mutation(lambda d:d['runs'][0]['sources'][0]['responses'].append(copy.deepcopy(d['runs'][0]['sources'][0]['responses'][0])))

    def test_publication_rejects_tool_aggregate_and_mapping_mutations(self):
        for level in ('run','source'):
            with self.subTest(level=level):
                self.rejected_mutation(lambda d:(d['runs'][0] if level=='run' else d['runs'][0]['sources'][0]).update(tool_output_utf8_representation_bytes=3))
        for field,value in (('response_id','foreign'),('response_mapping_reason','unknown'),('source_id','foreign'),('line',1),('timestamp','bad')):
            with self.subTest(field=field):
                self.rejected_mutation(lambda d:d['runs'][0]['sources'][0]['tool_outputs'][0].update({field:value}))

    def test_publication_rejects_diagnostic_stream_and_null_reason_mutations(self):
        self.rejected_mutation(lambda d:d['runs'][0]['sources'][0]['cumulative_streams'][1].update(stream='event_msg.total_token_usage'))
        self.rejected_mutation(lambda d:d['runs'][0]['sources'][0]['cumulative_streams'][0].update(reasons=[]))
        self.rejected_mutation(lambda d:d['runs'][0]['sources'][0]['responses'][0]['observed'].update(reason='invented_attestation'))
        self.rejected_mutation(lambda d:d['runs'][0]['sources'][0]['responses'][0]['observed'].update(context_line=99))

    def test_publication_rejects_comparison_arithmetic(self):
        a=self.run_data();b=copy.deepcopy(a);b['run_id']='run2'
        d=u.extract(dict(runs=[a,b],comparison_pairs=[['run1','run2']],checks=[],evidence_pins=[]))
        for field,value in (('raw_total_tokens_change',1),('observed_wall_union_seconds_change',0),('faster_but_more_raw_tokens',True),('cost_amount_change',1),('left_run_id','foreign')):
            with self.subTest(field=field):
                bad=copy.deepcopy(d);bad['comparisons'][0][field]=value;target=self.base/'comparison-bad.json'
                u.schema_validate(bad)
                with self.assertRaises(u.Invalid):u.publish(bad,target)
                self.assertFalse(target.exists())

    def test_publication_honest_nulls_preserved_with_missing_scope(self):
        del self.rows[3]['payload']['response_id']
        d=self.document();u.publish(d,self.base/'honest-null.json')
        self.assertIsNone(d['runs'][0]['response_count'])
        self.assertTrue(all(v is None for v in d['runs'][0]['totals'].values()))

    def test_publication_rejects_range_pin_and_source_duplicates(self):
        self.rejected_mutation(lambda d:d['runs'][0]['sources'][0].update(ranges=[[1,5],[4,5]]))
        self.rejected_mutation(lambda d:d['runs'][0]['sources'][0]['pin'].update(source_id='foreign'))
        self.rejected_mutation(lambda d:d['runs'][0]['sources'].append(copy.deepcopy(d['runs'][0]['sources'][0])))

    def test_publication_rejects_fabricated_reason_even_with_matching_nulls(self):
        def mutate(d):
            for r in (d['runs'][0],d['runs'][0]['sources'][0]):
                r.update(response_count=None,totals=u.null_totals(),reasons=['invented_error'])
        self.rejected_mutation(mutate)
        self.rejected_mutation(lambda d:d['runs'][0]['sources'][0]['cumulative_streams'][0].update(reasons=['diagnostic_only_never_cross_subtract','invented_error']))


def main():
    p=argparse.ArgumentParser();p.add_argument('--evidence');a=p.parse_args()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(ScopedUsage)
    stream=io.StringIO();result=unittest.TextTestRunner(stream=stream,verbosity=0).run(suite)
    files=['skills/agent-workflow-v1-1/tools/extract-usage.py','skills/agent-workflow-v1-1/tools/telemetry.schema.json','tooling/tests/test_extract_usage.py','skills/agent-workflow-v1-1/references/measurement.md']
    pins={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files if (ROOT/name).exists()}
    evidence={'schema':'n08_producer_tests_v1','mode':'normal' if __debug__ else 'optimized','tests_run':result.testsRun,
              'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
              'passed':result.wasSuccessful(),'independent_acceptance':False,'source_pins':pins,
              'test_names':sorted(n for n in dir(ScopedUsage) if n.startswith('test_')),
              'scope':'synthetic_adversarial_source_contract_not_native_cache_experiment'}
    if a.evidence:
        with Path(a.evidence).open('x',encoding='utf8',newline='\n') as f:f.write(u.canonical(evidence))
    print(u.canonical(evidence).strip())
    if not result.wasSuccessful():print(stream.getvalue(),file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
