"""Models-free mechanical batches. Native collaboration remains the caller's job.

This is a single-main-writer journal, not an atomic native tool interceptor or a
receipt authenticator. Unknown mutation outcomes require explicit inspection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import workflow as w


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class Coordination:
    def __init__(self, engine, fault=None):
        self.engine = engine
        self.fault = fault

    def journal(self, batch_id):
        w.check_field(batch_id, 'task_id', '$.batch_id')
        return self.engine.internal('coordination', batch_id + '.json')

    def state_pin(self):
        return w.digest(self.engine.path)

    def source_pin(self):
        return fingerprint({'workflow.py': w.digest(Path(w.__file__)), 'coordination.py': w.digest(Path(__file__))})

    def compact(self, path, journal, seen=None):
        seen = seen or []
        token = fingerprint(journal)
        state = w.read_json(self.engine.path) if self.engine.path.exists() else {'tasks': {}}
        results = {}
        sessions = {}
        for step in journal['steps']:
            payload = step['request'].get('payload', {})
            task = state.get('tasks', {}).get(payload.get('id'), {})
            if task.get('session_id'):
                sessions[payload['id']] = task['session_id']
            if token in seen:
                continue
            result = dict(outcome=step['outcome'])
            # Actual dispatch arguments must remain available to the main caller.
            if step.get('result', {}).get('tool') == 'collaboration.spawn_agent':
                result['dispatch'] = step['result']
            results[step['request']['key']] = result
        blocker = next((step.get('error', 'inspect unresolved durable outcome') for step in journal['steps']
                        if step['outcome'] != 'applied'), None)
        return dict(batch_id=journal['batch_id'], blocker=blocker, source_pin=journal['source_pin'],
                    evidence_path=str(path), session_identity=sessions, token=token, results=results)

    def validate_step(self, step):
        w.require(type(step) is dict and set(step) == {'key', 'command', 'payload'}, 'exact batch step fields required')
        w.check_field(step['key'], 'task_id', '$.steps[].key')
        w.check_field(step['command'], 'text', '$.steps[].command')
        w.require(type(step['payload']) is dict, 'step payload object required')
        w.json_value(step['payload'])
        if step['command'] == 'read':
            w.object_fields(step['payload'], dict(paths='strings'), {}, '$.payload')
            for name in step['payload']['paths']:
                self.engine.file(name)
        else:
            w.validate_command(step['command'], step['payload'])

    def batch(self, batch_id, steps, seen=None):
        """Apply ordered independent helpers; each mutation has its own commit.

        Replaying a completed key never executes it. A journal pending after a
        crash blocks further work even if state bytes appear unchanged: external
        snapshot/restore side effects could already exist. No automatic retry.
        """
        if self.engine.provenance_guard:
            self.engine.provenance_guard()
        w.require(type(steps) is list and steps, 'nonempty bounded steps required')
        w.require(len(steps) <= 64, 'batch exceeds 64 steps')
        for step in steps:
            self.validate_step(step)
        w.require(len({s['key'] for s in steps}) == len(steps), 'duplicate batch key')
        path = self.journal(batch_id)
        lock = self.engine.internal('coordination.lock')
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise w.Error('another batch or stale coordination lock; inspect owner before removing')
        try:
            os.write(fd, str(os.getpid()).encode())
            if path.exists():
                journal = self.load(path)
                w.require(journal['request_hash'] == fingerprint(steps), 'batch identity reused with different request')
                w.require(journal['source_pin'] == self.source_pin(), 'source changed; inspect old journal before continuing')
                if any(s['outcome'] != 'applied' for s in journal['steps']):
                    return self.compact(path, journal, seen)
            else:
                journal = dict(schema_version=1, batch_id=batch_id, request_hash=fingerprint(steps),
                               source_pin=self.source_pin(), steps=[])
                w.write_json(path, journal)
            for step in steps[len(journal['steps']):]:
                if self.engine.provenance_guard:
                    self.engine.provenance_guard()
                entry = dict(request=step, before_state=self.state_pin(), outcome='pending', at=self.engine.clock())
                journal['steps'].append(entry)
                w.write_json(path, journal)
                w.cut(self.fault, 'batch.before', path)
                try:
                    if step['command'] == 'read':
                        result = {'files': []}
                        for name in step['payload']['paths']:
                            target = self.engine.file(name)
                            raw = target.read_bytes() if target.exists() else None
                            result['files'].append(dict(path=name, sha256=None if raw is None else hashlib.sha256(raw).hexdigest(),
                                                        bytes=0 if raw is None else len(raw), text=None if raw is None else raw.decode('utf-8-sig')))
                    else:
                        result = self.engine.execute(step['command'], step['payload'])
                except (w.Error, OSError, ValueError) as exc:
                    entry.update(outcome='failed', after_state=self.state_pin(), error=str(exc),
                                 error_code=getattr(exc, 'code', type(exc).__name__))
                    w.write_json(path, journal)
                    break
                # Fault here models lost local result after a successfully applied mutation.
                w.cut(self.fault, 'batch.after_apply', path)
                entry.update(outcome='applied', after_state=self.state_pin(), result=result)
                w.write_json(path, journal)
            return self.compact(path, journal, seen)
        finally:
            os.close(fd)
            lock.unlink()

    def load(self, path):
        journal = w.read_json(path)
        w.require(type(journal) is dict and set(journal) == {'schema_version', 'batch_id', 'request_hash', 'source_pin', 'steps'}, 'invalid batch journal')
        w.require(journal['schema_version'] == 1 and type(journal['steps']) is list, 'invalid batch journal schema')
        for entry in journal['steps']:
            w.require(type(entry) is dict and entry.get('outcome') in {'pending', 'applied', 'failed'}, 'invalid journal outcome')
            self.validate_step(entry['request'])
        return journal

    def receipt(self, batch_id, task_id, submitted_arguments, session_id, receipt, evidence,
                effective_model=None, effective_effort=None):
        """Persist raw caller-provided receipt BEFORE core reconciliation.

        Fresh native inventory must already have been recorded via inventory.
        Raw source provenance is procedural; structural identity is checked by core.
        """
        payload = dict(id=task_id, submitted_arguments=submitted_arguments, session_id=session_id,
                       receipt=receipt, evidence=evidence, no_owned_work_running=False)
        if effective_model is not None:
            payload['effective_model'] = effective_model
        if effective_effort is not None:
            payload['effective_effort'] = effective_effort
        return self.batch(batch_id, [dict(key='actual_receipt', command='reconcile', payload=payload)])


def main():
    if (w.PACKAGE / 'candidate-profile.json').exists():
        print('candidate requires scripts/candidate.py with external manifest pin', file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--codex-home', required=True)
    parser.add_argument('--input', required=True, help='JSON {batch_id,steps,seen?}; raw logs stay in journal')
    args = parser.parse_args()
    try:
        request = w.read_json(Path(args.input))
        w.require(type(request) is dict and set(request) <= {'batch_id', 'steps', 'seen'} and 'steps' in request and 'batch_id' in request, 'batch_id and steps required')
        if 'seen' in request:
            w.check_field(request['seen'], 'strings', '$.seen')
        result = Coordination(w.Workflow(args.project, args.codex_home)).batch(request['batch_id'], request['steps'], request.get('seen'))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result['blocker'] else 0
    except (w.Error, OSError, ValueError) as exc:
        print(json.dumps(dict(blocker=str(exc), code=getattr(exc, 'code', type(exc).__name__)), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
