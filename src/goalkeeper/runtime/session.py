"""Common session lifecycle. Storage/UI callbacks cannot override execution policy."""
import asyncio
import json
import copy
import re
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4
from ..cost import totals
from ..models import RunConfig, State, Task, utcnow
from ..storage import Store
from .performance import task_profile, reasoning_effort
from .model_selection import selection
from .provider_catalog import is_account_provider
from . import completion, report_flow
from .files import workspace_snapshot
from .factory import create_engine
from .bridge import services_from_host

class EventStore(Store):
    def __init__(self, path, repository, conversation):
        super().__init__(path, State)
        self.repository, self.conversation = repository, conversation
        self.request_record = None
        self.request_id = None
        self.attempt = 1
        self.active_requests = {}

    def save(self, state, kind, data=None):
        super().save(state, kind, data)
        payload = {**(data or {}), 'run_id': state.id}
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        context = self.active_requests.get(task, {'record': self.request_record,
                                                 'id': self.request_id, 'attempt': self.attempt})
        if kind == 'operation_started':
            item = next((p for p in state.pending_tools if p['call_id'] == payload['call_id']), {})
            try:
                payload['arguments'] = json.loads(item.get('arguments', '{}'))
            except (ValueError, TypeError):
                payload['arguments'] = None
            payload.update(request_id=context['id'], attempt=context['attempt'])
            self.repository.event(self.conversation, 'tool_started', payload)
        elif kind == 'operation_result_saved':
            payload.update(request_id=context['id'], attempt=context['attempt'])
            self.repository.event(self.conversation, 'tool_completed', payload)
        elif kind == 'request_started':
            record = state.calls[-1]
            context = {'record': record, 'id': f'{state.id}:{len(state.calls)}', 'attempt': 1}
            self.active_requests[task] = context
            if record.get('role') == 'main':
                self.request_record, self.request_id, self.attempt = record, context['id'], 1
            payload.update(request_id=context['id'], attempt=1, role=record.get('role'),
                           started_at=record.get('started_at'))
            self.repository.event(self.conversation, kind, payload)
        elif kind == 'transient_retry':
            failed = state.calls[-1]
            payload.update(request_id=context['id'], attempt=context['attempt'],
                           started_at=failed.get('started_at'), finished_at=failed.get('finished_at'),
                           usage=failed.get('usage'), usage_complete='usage' in failed)
            payload['error_detail'] = failed.get('error_detail')
            self.repository.event(self.conversation, kind, payload)
            context['attempt'] += 1
        elif kind == 'request_finished':
            record = context['record'] or {}
            payload.update(request_id=context['id'], attempt=context['attempt'],
                           started_at=record.get('started_at'), finished_at=record.get('finished_at'),
                           usage=record.get('usage'))
            payload['error_detail'] = record.get('error_detail')
            self.repository.event(self.conversation, kind, payload)
            if record.get('role') == 'main':
                self.attempt = context['attempt']
            self.active_requests.pop(task, None)
        elif kind in {'task_progress', 'waiting_for_user', 'verification', 'compaction_completed', 'stopped', 'research_stage'}:
            self.repository.event(self.conversation, kind, payload)


class Runtime:
    def __init__(self, repository, tokens):
        self.repository = repository
        self.tokens = tokens
        self.root = repository.root
        self.index = repository.index
        self.jobs, self.engines, self.active_states = repository.jobs, repository.engines, repository.active_states
        self.deleting_workspaces = repository.deleting_workspaces

    def conversation(self, cid):
        return self.repository.conversation(cid)

    def files_root(self, wid):
        return self.repository.files_root(wid)

    def latest_store(self, cid):
        c = self.conversation(cid)
        return EventStore(self.root / 'conversations' / cid / 'runs' / c['runs'][-1], self.repository, cid)

    def event(self, *args, **kwargs):
        return self.repository.event(*args, **kwargs)

    def persist(self):
        self.repository.persist()

    def start(self, *args, **kwargs):
        return self.new_run(*args, **kwargs)

    def submit_message(self, *args, **kwargs):
        return self.send(*args, **kwargs)

    def answer(self, *args, **kwargs):
        return self.send(*args, **kwargs)

    async def cancel(self, cid):
        return await self.stop(cid)

    def continue_task(self, cid):
        return self.continue_run(cid)

    def retry(self, cid, mid):
        return self.send(cid, '重试任务', mid)

    def review_legacy_completion(self, c):
        if c.get('status') == 'running':
            return
        reviews = c.setdefault('completion_reviews', {})
        for rid in c.get('runs', []):
            store = EventStore(self.root / 'conversations' / c['id'] / 'runs' / rid, self.repository, c['id'])
            try:
                state = store.load()
                if not state.delivery or state.delivery.get('completion', {}).get('version', 0) >= completion.COMPLETION_VERSION:
                    continue
                assessment = completion.review_saved(state, store)
                if reviews.get(rid) == assessment:
                    continue
                reviews[rid] = assessment
                reviewed = {**state.delivery, 'completion': assessment, 'unresolved': assessment['unmet'],
                            'limitations': assessment['limitations'], 'suggestions': assessment['suggestions']}
                self.event(c['id'], 'completion_review', {'run_id': rid, 'status': assessment['status'],
                                                        'delivery': reviewed})
                if rid == c['runs'][-1] and state.phase == 'delivered':
                    c.update(status=assessment['status'], delivery=reviewed)
            except (ValueError, OSError, KeyError, TypeError) as error:
                # Unreadable or ambiguous historical evidence never becomes an automatic success.
                failure = {'version': completion.COMPLETION_VERSION, 'status': 'incomplete', 'unmet': ['原交付证据无法复核：' + str(error)],
                           'limitations': [], 'review_failed': True}
                if reviews.get(rid) != failure:
                    reviews[rid] = failure
                    self.event(c['id'], 'completion_review', {'run_id': rid, 'status': 'incomplete',
                        'reason': failure['unmet'][0]})
                if rid == c.get('runs', [])[-1] and c.get('status') == 'delivered':
                    c['status'] = 'incomplete'
                continue
        if c.get('status') == 'waiting' and len(c.get('runs', [])) > 1:
            previous = reviews.get(c['runs'][-2])
            if not previous or previous['status'] != 'delivered':
                return
            latest = self.latest_store(c['id']).load()
            prior_store = EventStore(self.root / 'conversations' / c['id'] / 'runs' / c['runs'][-2], self.repository, c['id'])
            prior = prior_store.load()
            generated = '继续完成此前未完成事项：' + '；'.join(prior.delivery.get('unresolved', []))
            # Require the exact generated message, with no subsequent human additions.
            last_message = c.get('messages', [])[-1] if c.get('messages') else {}
            if (last_message.get('text') == generated and latest.task.objective.split('\n用户补充要求：')[0] == generated
                    and not latest.queued_messages and is_legacy_limit_wait(latest, previous)):
                reviewed = {**prior.delivery, 'completion': previous, 'unresolved': [], 'limitations': previous['limitations']}
                c.update(status='delivered', question=None, delivery=reviewed)
                self.event(c['id'], 'completion_review', {'run_id': latest.id, 'status': 'scope_reviewed',
                    'derived_message_id': last_message.get('id'), 'scope_source_run': prior.id,
                    'reason': '原要求已交付；此前自动继续把资料限制扩展为补数任务，已撤销等待。'})

    @staticmethod
    def scope_messages(c):
        """Project trusted user messages without changing the original conversation record."""
        corrected = {e['data'].get('derived_message_id') for e in c.get('events', [])
                     if e['kind'] == 'completion_review' and e['data'].get('status') == 'scope_reviewed'}
        corrected.discard(None)
        return [m for m in c['messages'] if not m.get('derived') and m.get('id') not in corrected]

    def send(self, cid, text, mid, display_text=None):
        c = self.conversation(cid)
        if any(m['id'] == mid for m in c['messages']):
            return {'duplicate': True, 'conversation': c}
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise ValueError('请输入有效任务，单条消息不能超过 100000 字符。')
        if c['workspace_id'] in self.deleting_workspaces:
            raise ValueError('工作区正在删除，请稍后重试。')
        # Check before recording: a rejected message can be safely retried with the same id.
        if any(other['id'] != cid and other['workspace_id'] == c['workspace_id'] and other['id'] in self.jobs
               for other in self.index['conversations']):
            raise ValueError('同一工作区另一个会话正在执行，请先停止它。')
        if display_text is not None and (not isinstance(display_text, str) or len(display_text) > 100_000):
            raise ValueError('显示消息无效。')
        # Account connection is desktop-owned. Validate or rebind it before
        # accepting the message so a routing problem cannot create a failed run.
        model_settings = getattr(self.repository, 'model_settings', None)
        if model_settings:
            selected = model_settings.reconcile_account_provider()
            model_settings.ensure_account_connected(selected)
        message = {'id': mid, 'text': text, 'status': 'received', 'at': utcnow()}
        if display_text is not None:
            message['display_text'] = display_text
        c['messages'].append(message)
        if len(c['messages']) == 1 and not c.get('manual_title'):
            title = (display_text if display_text is not None else text).strip()
            # A leading file reference is metadata, not the conversation's task name.
            title = re.sub(r'^@(?:"[^"\n]+"|[^\s]+)\s*', '', title) or title
            end = re.search(r'[。！？!?]|\.(?=\s|$)|[\r\n]', title)
            if end:
                title = title[:end.start() if end.group() in {'\r', '\n'} else end.end()]
            c['title'] = title
        if cid in self.jobs:
            state = self.active_states[cid]
            state.queued_messages.append(message)
            self.latest_store(cid).save(state, 'message_received', {'id': mid})
        elif c['status'] == 'waiting':
            store = self.latest_store(cid)
            state = store.load()
            if model_settings:
                state.config = model_settings.rebind_config(state.config)
            state.queued_messages.append(message)
            store.save(state, 'answer_received', {'id': mid})
            self.launch(cid, state, store)
        else:
            self.new_run(cid, text, mid)
        self.persist()
        return {'conversation': c}

    def new_run(self, cid, text, mid, remaining_seconds=None, previous_config=None, derived=False):
        c = self.conversation(cid)
        root = self.files_root(c['workspace_id'])
        now = time.time()
        rid = uuid4().hex
        task = Task(delivery_mode="interactive", id=cid, objective=text, write_paths=['./'], constraints=[
            f'Python 解释器：{sys.executable}；Python 已提供 docx、openpyxl、pypdf。',
            f'当前工作区：{root}。直接修改此目录内的文件；其他工作区、目录、账号凭据和系统目录没有授权。'])
        if previous_config is not None and hasattr(self.repository, 'model_settings'):
            previous_config = self.repository.model_settings.rebind_config(previous_config)
        selected = (previous_config.provider_id if previous_config is not None
                    else self.index['settings'].get('model_provider', 'account'))
        if previous_config is None and hasattr(self.repository, 'model_settings'):
            selected = self.repository.model_settings.reconcile_account_provider()
        if hasattr(self.repository, 'model_settings'):
            self.repository.model_settings.ensure_account_connected(selected)
        profile = self.index['settings'].get('model_profiles', {}).get(selected)
        account_provider = is_account_provider(selected)
        if not account_provider and (not profile or not self.repository.model_settings.secret(selected)):
            raise ValueError('所选模型尚未配置，请在设置 → 模型中保存 API 密钥。')
        config = RunConfig(effort='auto', seconds_budget=self.index['settings']['minutes'] * 60, token_budget=None,
                           summary_budget_fraction=None, summary_time_fraction=None)
        if account_provider:
            picked_model = self.index['settings'].get('model_selections', {}).get(selected, {}).get('model', config.model)
            picked = selection(self.index['settings'], selected, picked_model)
            config = RunConfig(**{**config.model_dump(), 'provider_id': selected,
                                  'model': picked_model, 'effort': picked['reasoningEffort']})
        else:
            picked = selection(self.index['settings'], selected, profile['model'])
            config = RunConfig(**{**config.model_dump(), 'provider_id': selected,
                                        'model': profile['model'], 'effort': picked['reasoningEffort']})
        if previous_config is not None:
            config = previous_config.model_copy(deep=True)
        profile_name = task_profile(text)
        old = self.latest_store(cid).load() if c['runs'] else None
        scope_old = old
        if old and len(c['runs']) > 1 and any(
                e['kind'] == 'completion_review' and e['data'].get('run_id') == old.id
                and e['data'].get('status') == 'scope_reviewed' for e in c.get('events', [])):
            scope_old = EventStore(self.root / 'conversations' / cid / 'runs' / c['runs'][-2], self.repository, cid).load()
        report_intent = report_flow.intent(text, scope_old, self.scope_messages(c))
        if c['runs']:
            if old.delivery is None or old.delivery.get('files') or task_profile(old.task.objective) != 'short_qa':
                profile_name = 'general'
        if report_intent:
            profile_name = 'research_report'
            task.objective = report_intent['objective']
        config.task_profile = profile_name
        if is_account_provider(config.provider_id):
            if config.adaptive_effort or config.effort == 'auto':
                config.adaptive_effort = True
                config.effort = reasoning_effort(text, profile_name)
        seconds = config.seconds_budget if remaining_seconds is None else remaining_seconds
        state = State(parent_run_id=old.id if old else None, id=rid, workspace=str(root), task=task, config=config,
                             started_at=now, deadline=now + seconds, remaining_seconds=remaining_seconds,
                             baseline_files=workspace_snapshot(root))
        scope_text = next((m.get('display_text', m['text']) for m in c['messages'] if m['id'] == mid), text)
        if old and (derived or re.fullmatch(r'(重试|重新执行|重新做|继续)(?:任务|此前任务)?[。！!]*', text)):
            completion.inherit(state, scope_old, continuation=True)
        elif old and re.search(r'压缩|精简|缩短|篇幅|排版|修改|调整|保留|增加|新增|追加|改为|改成|取消', scope_text):
            completion.inherit(state, scope_old, scope_text, mid)
        else:
            completion.add_requirements(state, scope_text, mid)
        store = EventStore(self.root / 'conversations' / cid / 'runs' / rid, self.repository, cid)
        if c['runs']:
            previous = self.latest_store(cid)
            old = previous.load()
            state.history = copy.deepcopy(old.history)
            # Preserve prior task context and all references within THIS conversation only.
            state.history.insert(0, {'role': 'user', 'content': old.task.objective})
            state.summary = copy.deepcopy(old.summary)
            state.summary_sources = list(old.summary_sources)
            state.material_refs = list(old.material_refs)
            for f in previous.archives.iterdir():
                if f.is_file() and not f.is_symlink():
                    shutil.copyfile(f, store.archives / f.name)
            if old.phase not in {'delivered', 'waiting'}:
                state.history.append({'role': 'user', 'content':
                    '此前执行中断。先读取文件核对实际状态，不直接重放结果不明的命令。'})
            state.pending_tools = copy.deepcopy(old.pending_tools)
            state.operations = copy.deepcopy(old.operations)
            if Path(old.workspace) != root:
                state.pending_tools = []
                state.operations = []
                state.history.append({'role': 'user', 'content': '工作区位置已改变。读取当前目录核对文件，不重放原目录中未确认的操作。'})
        # This message already defines the new run's scope above. Queue it only
        # to add conversation history and acknowledge receipt, not as a new requirement.
        state.queued_messages.append({'id': mid, 'text': text, 'display_text': scope_text,
                                      'derived': True, 'scope_bound': True})
        if report_intent:
            report_flow.prepare(state, store, report_intent['mode'], old)
            store.save(state, 'report_context_prepared', {'mode': report_intent['mode'], 'prior_run': old.id if old else None})
        store.save(state, 'prepared')
        c['runs'].append(rid)
        self.launch(cid, state, store)

    def launch(self, cid, state, store, resume=False):
        if cid in self.jobs:
            raise ValueError('会话正在执行。')
        self.conversation(cid)['status'] = 'running'
        self.active_states[cid] = state
        self.jobs[cid] = asyncio.create_task(self.execute(cid, state, store, resume))

    async def execute(self, cid, state, store, resume):
        provider = None
        self.event(cid, 'run_started', {'run_id': state.id, 'model': state.config.model,
                                      'provider_id': state.config.provider_id, 'effort': state.config.effort})
        try:
            provider = self.repository.resolve_provider(state.config)
            services = services_from_host(self.repository, cid)
            engine = create_engine(state, store, provider, self.tokens, services=services)
            self.engines[cid] = engine
            await engine.run(resume=resume)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.event(cid, 'error', {'message': str(error)})
            state.phase = 'program_error'
            state.first_stop_reason = state.first_stop_reason or type(error).__name__ + ':' + str(error)
        finally:
            if cid in self.engines:
                self.repository.search_capability = self.engines[cid].services.search_capability
                await self.engines[cid].services.stop_previews()
            if hasattr(provider, 'close'):
                await provider.close()
            self.engines.pop(cid, None)
            self.jobs.pop(cid, None)
            self.active_states.pop(cid, None)
            c = self.conversation(cid)
            c['status'] = (state.delivery['completion']['status'] if state.delivery and state.delivery.get('completion') else
                           'incomplete' if state.delivery and state.delivery.get('unresolved') else
                           {'delivered': 'delivered', 'waiting': 'waiting', 'cancelled': 'stopped',
                            'resource_interrupted': 'interrupted', 'stopped': 'incomplete'}.get(state.phase, 'error'))
            c['cost'] = {**totals(state.calls), 'requests': len(state.calls)}
            c['seconds'] = state.active_seconds
            c['delivery'] = state.delivery
            c['question'] = state.question
            c['stop_reason'] = state.first_stop_reason
            if state.phase != 'waiting':
                state.remaining_seconds = max(0, state.deadline - time.time())
                store.save(state, 'execution_paused', {'remaining_seconds': state.remaining_seconds})
            self.event(cid, 'run_finished', {'run_id': state.id, 'status': c['status'], 'cost': c['cost'],
                                          'seconds': c['seconds'], 'delivery': c['delivery'],
                                          'question': c['question'], 'stop_reason': c['stop_reason']})
            # A user message can arrive while the accepted turn is cleaning up.
            if state.queued_messages and state.phase in {'waiting', 'delivered'}:
                if state.phase == 'waiting':
                    self.launch(cid, state, store)
                else:
                    queued = list(state.queued_messages)
                    self.new_run(cid, queued[0]['text'], queued[0]['id'])
                    self.active_states[cid].queued_messages.extend(queued[1:])
                self.persist()

    async def stop(self, cid):
        if job := self.jobs.get(cid):
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            if cid in self.jobs:  # Cancelled before the worker entered its try/finally.
                state = self.active_states.pop(cid)
                state.phase = 'cancelled'
                state.remaining_seconds = max(0, state.deadline - time.time())
                state.first_stop_reason = 'user_or_outer_cancel'
                self.latest_store(cid).save(state, 'stopped_before_start')
                self.jobs.pop(cid, None)
                self.conversation(cid)['status'] = 'stopped'
                self.event(cid, 'run_finished', {'run_id': state.id, 'status': 'stopped',
                                               'cost': {**totals(state.calls), 'requests': len(state.calls)}, 'seconds': 0})
        return {'stopped': True}

    def continue_run(self, cid):
        c = self.conversation(cid)
        if c['workspace_id'] in self.deleting_workspaces:
            raise ValueError('工作区正在删除，请稍后重试。')
        if c['status'] not in {'interrupted', 'incomplete', 'stopped', 'error'}:
            raise ValueError('当前没有可继续的中断任务。')
        if any(other['id'] != cid and other['workspace_id'] == c['workspace_id'] and other['id'] in self.jobs
               for other in self.index['conversations']):
            raise ValueError('同一工作区另一个会话正在执行，请先停止它。')
        store = self.latest_store(cid)
        state = store.load()
        if Path(state.workspace) != self.files_root(c['workspace_id']):
            raise ValueError('工作区位置已更换，请发送新的要求开始执行；原执行记录保留。')
        import json
        legacy = json.loads(store.path.read_text()).get('schema_version', 1) < 2
        assessment = completion.review_saved(state, store) if state.delivery else None
        if assessment and not assessment['unmet']:
            raise ValueError('原要求已经完成；资料限制不需要继续。可以发送新的修改要求。')
        if (state.remaining_seconds or 0) <= state.config.delivery_save_seconds:
            raise ValueError('本轮执行时长已用尽。请调整设置并发送新的要求，开启新一轮执行；已有文件已保留。')
        if state.delivery:
            # A partial delivery is immutable. Continue in a new record with the SAME remaining time.
            mid = uuid4().hex
            text = '继续完成原用户要求中尚未完成的事项：' + '；'.join(assessment['unmet'])
            c['messages'].append({'id': mid, 'text': text, 'derived': True, 'status': 'received', 'at': utcnow()})
            self.new_run(cid, text, mid, state.remaining_seconds, state.config, derived=True)
            self.persist()
            return {'continued': True}
        if legacy:
            mid = uuid4().hex
            text = '继续任务'
            c['messages'].append({'id': mid, 'text': text, 'derived': True, 'status': 'received', 'at': utcnow()})
            self.new_run(cid, text, mid, state.remaining_seconds, state.config, derived=True)
            self.persist()
            return {'continued': True, 'new_execution': True, 'parent_run_id': state.id}
        state.deadline = time.time() + state.remaining_seconds
        if state.phase == 'running':
            state.active_seconds = max(state.active_seconds, state.checkpoint_seconds)
        for record in state.calls:
            if record['status'] == 'in_flight':
                record['status'] = 'interrupted_usage_unknown'
        state.phase = 'prepared'
        state.first_stop_reason = None
        state.history.append({'role': 'user', 'content': '用户要求继续此前任务。先核对实际文件，不能重放结果不明的操作。'})
        report_intent = report_flow.intent('继续任务', state, self.scope_messages(c))
        if report_intent:
            old = state.model_copy(deep=True)
            state.task.objective = report_intent['objective']
            state.config.task_profile = 'research_report'
            if is_account_provider(state.config.provider_id) and state.config.adaptive_effort:
                state.config.effort = 'medium'
            report_flow.prepare(state, store, 'continue', old)
        store.save(state, 'manual_continue', {'remaining_seconds': state.remaining_seconds})
        self.launch(cid, state, store)
        self.persist()
        return {'continued': True}


def is_legacy_limit_wait(state, assessment):
    return bool(assessment['limitations'] and not assessment['unmet'] and state.phase == 'waiting')
