"""Incremental interaction registration from use, without exploratory actions.

Persistent records contain hashed locators and structural states, never UI
labels, input values or session references. Historical edges are observations,
not invocation authority or proof of causality/business completion.
"""
from collections import Counter, OrderedDict
import copy
import json
import os
from pathlib import Path
import time
import uuid

from discovery import digest, file_stamp
from object_bridge import Fault

FORMAT = 'appctl.learning/1'
ROLES = set('Window Pane Group Custom Document Canvas Button Edit ComboBox CheckBox RadioButton List ListItem Tab TabItem Tree TreeItem Menu MenuItem ToolBar Slider ScrollBar Hyperlink Image Text Header HeaderItem DataGrid DataItem ProgressBar Separator StatusBar ToolTip Spinner SplitButton Thumb TitleBar'.split())
INTERACTIVE = set('Window Button Edit ComboBox CheckBox RadioButton List ListItem Tab TabItem Tree TreeItem Menu MenuItem ToolBar Slider ScrollBar Hyperlink Canvas Document Custom SplitButton'.split())
BACKENDS = {'uia','uia3','msaa','win32','cdp','DOM','native','cdp-dom','electron-dom','other'}
LIMITS = {'apps':32, 'nodes':2048, 'states':128, 'edges':512, 'state_items':512, 'sequence':8, 'bytes':2*1024*1024}


def label_hash(value):
    return digest(' '.join(str(value or '').casefold().split()))


def hexadecimal(value):
    return isinstance(value, str) and len(value)==24 and all(c in '0123456789abcdef' for c in value)


def clean_app(raw):
    """Allowlist disk data; a modified cache cannot inject labels or commands."""
    from gui_api import METHODS
    result = {'nodes':{}, 'states':{}, 'edges':{}, 'outcomes':{}, 'updated_at':0, 'truncated':False}
    if not isinstance(raw, dict):
        return result
    result['updated_at'] = raw.get('updated_at', 0) if type(raw.get('updated_at')) in (int,float) else 0
    result['truncated'] = raw.get('truncated') is True
    if hexadecimal(raw.get('family')): result['family']=raw['family']
    for key, row in list(raw.get('nodes', {}).items())[-LIMITS['nodes']:] if isinstance(raw.get('nodes'), dict) else []:
        if not hexadecimal(key) or not isinstance(row,dict): continue
        if row.get('role') not in ROLES or row.get('backend') not in BACKENDS: continue
        if not all(hexadecimal(row.get(field)) for field in ('name_hash','automation_id_hash','class_hash','path_hash')): continue
        result['nodes'][key] = {field:row[field] for field in ('role','backend','name_hash','automation_id_hash','class_hash','path_hash')}
        result['nodes'][key]['methods'] = sorted({m for m in row.get('methods', []) if isinstance(m,str) and m in METHODS}) if isinstance(row.get('methods'),list) else []
        node=result['nodes'][key]
        for field in ('parent_hash','structure_hash'):
            if hexadecimal(row.get(field)): node[field]=row[field]
        if row.get('identity_confidence') in ('high','medium','low'): node['identity_confidence']=row['identity_confidence']
        rect=row.get('last_rect')
        if isinstance(rect,dict) and set(rect)<= {'x','y','width','height'} and all(type(v) in (int,float) and abs(v)<10000000 for v in rect.values()): node['last_rect']=rect
        health=row.get('health',{}) if isinstance(row.get('health',{}),dict) else {}
        node['health']={k:min(max(health.get(k,0),0),1000000) if type(health.get(k)) is int else 0 for k in ('attempts','resolved','ambiguous','missing')}
        for field in ('last_resolution_at','last_success_at'):
            node['health'][field]=health.get(field,0) if type(health.get(field)) in (int,float) and 0<=health[field]<=1e12 else 0
    for key, row in list(raw.get('states', {}).items())[-LIMITS['states']:] if isinstance(raw.get('states'), dict) else []:
        if not hexadecimal(key) or not isinstance(row,dict): continue
        items = row.get('items', [])
        if not isinstance(items,list): continue
        items = [item for item in items[:LIMITS['state_items']] if isinstance(item,list) and len(item)==5 and hexadecimal(item[0]) and all(type(v) is int and 0<=v<=1000000 for v in item[1:])]
        result['states'][key] = {'items':items, 'partial':row.get('partial') is True,
                                'observations':min(max(row.get('observations',0),0),1000000) if type(row.get('observations')) is int else 0}
    for key, row in list(raw.get('edges', {}).items())[-LIMITS['edges']:] if isinstance(raw.get('edges'), dict) else []:
        if not hexadecimal(key) or not isinstance(row,dict) or not hexadecimal(row.get('from')) or not hexadecimal(row.get('to')): continue
        kind = row.get('kind')
        if kind=='contains':
            if row['from'] not in result['nodes'] or row['to'] not in result['nodes']: continue
            entry = {'kind':kind, 'from':row['from'], 'to':row['to'], 'evidence':'provider_tree'}
        elif kind=='transition':
            if row['from'] not in result['states'] or row['to'] not in result['states']: continue
            steps = row.get('steps')
            if not isinstance(steps,list) or not 1<=len(steps)<=LIMITS['sequence']: continue
            if any(not isinstance(s,dict) or s.get('target') not in result['nodes'] or s.get('method') not in METHODS for s in steps): continue
            entry = {'kind':kind, 'from':row['from'], 'to':row['to'],
                     'steps':[{k:s[k] for k in ('target','method')} for s in steps],
                     'evidence':'observed_after_action' if len(steps)==1 else 'observed_after_sequence',
                     'before_state_fresh':row.get('before_state_fresh') is True,
                     'structure_changed':row['from']!=row['to'], 'causality_verified':False,
                     'business_completion_proven':False}
        else: continue
        entry['observations'] = min(max(row.get('observations',0),0),1000000) if type(row.get('observations')) is int else 0
        result['edges'][key] = entry
    for key in ('returned','failed','unknown','unobserved'):
        value = raw.get('outcomes',{}).get(key,0) if isinstance(raw.get('outcomes'),dict) else 0
        result['outcomes'][key] = min(max(value,0),1000000) if type(value) is int else 0
    return result


class LearningStore:
    def __init__(self, path):
        self.path, self.apps = Path(path), {}
        self.persistence = 'not_written'
        self.apps = self._read()

    def _read(self):
        try:
            if self.path.stat().st_size > LIMITS['bytes']: return {}
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if raw.get('format') != FORMAT or not isinstance(raw.get('apps'),dict): return {}
            return {key:clean_app(row) for key,row in list(raw['apps'].items())[-LIMITS['apps']:] if hexadecimal(key)}
        except (OSError,ValueError,TypeError,AttributeError,RecursionError):
            return {}

    def save(self):
        temporary = None
        try:
            # Bound memory even when the cache directory is unavailable.
            self.apps = dict(sorted(((key,clean_app(row)) for key,row in self.apps.items()),
                                    key=lambda item:item[1]['updated_at'])[-LIMITS['apps']:])
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with open(str(self.path)+'.lock','a+b') as lock:
                lock.seek(0,2)
                if not lock.tell(): lock.write(b'0'); lock.flush()
                lock.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
                merged = self._read()
                for app, local in self.apps.items():
                    combined = merged.setdefault(app,clean_app({}))
                    if local.get('family'): combined['family']=local['family']
                    for field in ('nodes','states','edges'):
                        for key, row in local[field].items():
                            old = combined[field].get(key,{})
                            combined[field][key] = copy.deepcopy(row)
                            if 'observations' in row:
                                # Concurrent sessions may undercount identical
                                # observations, but never fabricate extra ones.
                                combined[field][key]['observations'] = max(old.get('observations',0),row['observations'])
                    for name,value in local['outcomes'].items():
                        combined['outcomes'][name] = max(combined['outcomes'].get(name,0),value)
                    combined['updated_at'] = max(combined['updated_at'],local['updated_at'])
                    combined['truncated'] |= local['truncated'] or any(len(combined[f])>LIMITS[f] for f in ('nodes','states','edges'))
                    merged[app] = clean_app(combined)
                merged = dict(sorted(merged.items(),key=lambda item:item[1]['updated_at'])[-LIMITS['apps']:])
                wire = lambda:json.dumps({'format':FORMAT,'apps':merged},separators=(',',':'),allow_nan=False)
                encoded = wire()
                while len(encoded.encode())>LIMITS['bytes']:
                    if len(merged)>1: merged.pop(next(iter(merged)))
                    else:
                        row = next(iter(merged.values()))
                        row['truncated'] = True
                        if row['states']: row['states'].pop(next(iter(row['states'])))
                        elif row['nodes']: row['nodes'].pop(next(iter(row['nodes'])))
                        else: break
                        key = next(iter(merged)); merged[key] = clean_app(row)
                    encoded = wire()
                temporary = self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
                temporary.write_text(encoded,encoding='utf-8'); temporary.replace(self.path)
                self.apps, self.persistence = merged, 'saved'
        except (OSError,ValueError,TypeError,AttributeError,RecursionError):
            self.persistence = 'unavailable'
        finally:
            if temporary:
                try: temporary.unlink(missing_ok=True)
                except OSError: pass


class InteractionLearning:
    def __init__(self, gui, path):
        self.gui, self.store = gui, LearningStore(path)
        self.latest, self.pending = {}, {}

    def app_id(self, source):
        if 'learning_host_stamp' not in source:
            stamps = []
            target = source['target']
            if target.get('pid'):
                try:
                    import psutil
                    process = psutil.Process(target['pid'])
                    if process.create_time()==target.get('created_at'):
                        for arg in process.cmdline()[1:16]:
                            if Path(arg).suffix.lower() in ('.py','.pyw','.js','.mjs','.cjs') and Path(arg).is_file():
                                stamps.append(file_stamp(arg))
                except (OSError,ValueError,psutil.Error): pass
            source['learning_host_stamp'] = stamps
        return digest([self.gui.discovery.gui_fingerprint(source),source['learning_host_stamp'],source.get('learning_document_origin')])

    def app(self, source):
        key = self.app_id(source)
        app=self.store.apps.setdefault(key,clean_app({}))
        # Stable family excludes image versions and live HWND/endpoint IDs.
        app['family']=digest([source['runtime'],str(source['target'].get('exe','')).casefold(),
            source.get('discovery_info',{}).get('class_name'),
            [stamp[0] for stamp in source.get('learning_host_stamp',[]) if isinstance(stamp,list) and stamp],source.get('learning_document_origin')])
        return key,app

    def normalize(self, rows):
        from gui_api import METHODS
        from gui_identity import annotate, group_controls
        if any('fingerprint' not in r for r in rows): annotate(rows)
        groups=group_controls(rows)
        nodes, refs = {}, {}
        for group in groups:
            row=group['rows'][0]
            if row.get('password'): continue
            identity = row['fingerprint']
            for candidate in group['rows']: refs[candidate['ref']]=identity
            nodes[identity] = {'role':row.get('role') if row.get('role') in ROLES else 'Custom',
                'backend':row.get('backend') if row.get('backend') in BACKENDS else 'other',
                'name_hash':label_hash(row.get('name')), 'automation_id_hash':label_hash(row.get('automation_id')),
                'class_hash':label_hash(row.get('class_name')), 'path_hash':row['path_fingerprint'],
                'parent_hash':row['parent_fingerprint'],'structure_hash':row['path_fingerprint'],
                'identity_confidence':row['identity_confidence'],'last_rect':row.get('rect',{}),
                'methods':sorted({m for r in group['rows'] for m in r.get('methods',[]) if m in METHODS})}
        return nodes,refs

    def observe(self, source, collection):
        if collection.get('cursor') or collection['options'].get('root_ref'):
            return {'registered':False,'reason':'complete_window_observation_required'}
        old = self.latest.get(source['id'])
        app_id, app = self.app(source)
        if old and old.get('observation_id')==collection.get('observation_id',collection['created']) and old['app']==app_id: return old['summary']
        rows = list(collection['rows'].values())
        all_nodes, refs = self.normalize(rows)
        chosen = [r for r in rows if r['ref'] in refs and (r.get('role') in INTERACTIVE or set(r.get('actions',[])) & {'set','select','toggle-on','expand','collapse','scroll'})]
        limited = len(chosen)>LIMITS['state_items']
        chosen = chosen[:LIMITS['state_items']]
        items = Counter((refs[r['ref']],int(r.get('enabled',True)),int(r.get('offscreen',False)),int(r.get('msaa_state',0)) & 0x672) for r in chosen)
        encoded = sorted([[key[0],count,*key[1:]] for key,count in items.items()])
        state_id = digest(encoded)
        partial = bool(limited or collection['metadata'].get('truncated'))
        state = app['states'].pop(state_id,{'items':encoded,'partial':partial,'observations':0})
        state['observations'] += 1; state['partial'] |= partial
        app['states'][state_id] = state
        for node in list(all_nodes)[:LIMITS['nodes']]:
            old_node=app['nodes'].pop(node,{})
            app['nodes'][node] = {**all_nodes[node],'health':old_node.get('health',{})}
        for row in rows:
            child, parent = refs.get(row['ref']), refs.get(row.get('parent'))
            if not child or not parent or child==parent or child not in app['nodes'] or parent not in app['nodes']: continue
            edge_id = digest(['contains',parent,child])
            edge = app['edges'].setdefault(edge_id,{'kind':'contains','from':parent,'to':child,'evidence':'provider_tree','observations':0})
            edge['observations'] += 1
        pending = self.pending.get(source['id'])
        if pending and 'first_unchanged_at' in pending and time.monotonic()-pending['first_unchanged_at']>=5:
            self.store.apps.get(pending['app'],app)['outcomes']['unobserved'] += 1
            self.pending.pop(source['id'],None)
            pending = None
        learned = None
        if pending and pending['app']==app_id and pending['before'] in app['states'] and pending['steps'] and all(s['returned'] for s in pending['steps']):
            steps = [{k:s[k] for k in ('target','method')} for s in pending['steps']]
            if all(step['target'] in app['nodes'] for step in steps):
                learned = digest(['transition',pending['before'],state_id,steps,pending['before_fresh']])
                edge = app['edges'].setdefault(learned,{'kind':'transition','from':pending['before'],'to':state_id,'steps':steps,
                    'evidence':'observed_after_action' if len(steps)==1 else 'observed_after_sequence',
                    'before_state_fresh':pending['before_fresh'],
                    'structure_changed':pending['before']!=state_id,'causality_verified':False,'business_completion_proven':False,'observations':0})
                edge['observations'] += 1
                # Providers can publish a new subtree after Invoke returns.
                # Retain a short-lived candidate when this read still looks
                # unchanged; later *requested reads* can add the changed state.
                # No extra read, waiting loop or repeated action is scheduled.
                if pending['before']==state_id:
                    pending.setdefault('first_unchanged_at',time.monotonic())
                if pending['before']!=state_id or time.monotonic()-pending.get('first_unchanged_at',time.monotonic())>=5:
                    self.pending.pop(source['id'],None)
        elif pending:
            self.store.apps.get(pending['app'],app)['outcomes']['unobserved'] += 1
            self.pending.pop(source['id'],None)
        app['updated_at'] = time.time()
        app['truncated'] |= limited or len(all_nodes)>LIMITS['nodes']
        summary = {'format':FORMAT,'registered':True,'app':app_id,'state':state_id,'observed_transition':learned,
                   'business_completion_proven':False,'extra_gui_actions':0}
        self.latest[source['id']] = {'app':app_id,'state':state_id,'refs':refs,'created':collection['created'],
                                     'observation_id':collection.get('observation_id',collection['created']),
                                     'rows':rows,'summary':summary}
        self.store.save()
        return summary

    def begin(self, source, row, method):
        if method=='get': return None
        old = self.latest.get(source['id'])
        app_id, app = self.app(source)
        pending = self.pending.get(source['id'])
        if pending and 'first_unchanged_at' in pending and time.monotonic()-pending['first_unchanged_at']>=5:
            self.pending.pop(source['id'],None)
            pending = None
        if pending is None:
            before, before_fresh = None, False
            collection = source.get('collection')
            if old and old['app']==app_id and collection and not source.get('dirty'):
                current = self.gui._event_state(source,after=collection.get('event_revision'))
                maximum = 30 if current['watching'] else 5
                if not current.get('changed'):
                    before = old['state']
                    before_fresh = time.monotonic()-collection['created']<=maximum and not (collection.get('event_watching') and not current['watching'])
            pending = self.pending[source['id']] = {'app':app_id,'before':before,'before_fresh':before_fresh,'steps':[],
                                                   'started':time.monotonic()}
        target = old['refs'].get(row['ref']) if old else None
        if target is None:
            nodes, refs = self.normalize([row]); app['nodes'].update(nodes); target = refs.get(row['ref'])
            pending['before'] = None
        if len(pending['steps'])>=LIMITS['sequence']:
            pending['before'] = None; pending['steps'].clear()
        step = {'target':target,'method':method,'returned':False}
        pending.pop('first_unchanged_at',None)
        pending['steps'].append(step)
        return {'source':source['id'],'app':app_id,'step':step}

    def finish(self, ticket, ok, code=None):
        if ticket is None: return
        app = self.store.apps.get(ticket['app'])
        if app is None: return
        outcome = 'returned' if ok else 'unknown' if code in ('outcome_unknown','unexpected_exception') else 'failed'
        app['outcomes'][outcome] += 1
        ticket['step']['returned'] = bool(ok)
        if not ok: self.pending.pop(ticket['source'],None)
        elif ticket['source'] in self.pending:
            self.pending[ticket['source']]['returned_at'] = time.monotonic()
        app['updated_at'] = time.time()
        self.store.save()

    def release(self, source):
        pending = self.pending.pop(source,None)
        if pending and pending['app'] in self.store.apps:
            self.store.apps[pending['app']]['outcomes']['unobserved'] += 1
            self.store.save()
        self.latest.pop(source,None)

    def query(self, source, query='', relation='all', limit=100, offset=0):
        if not isinstance(query,str) or len(query)>1024 or relation not in ('all','contains','transitions'):
            raise Fault('invalid_arguments','query must be at most 1024 characters; relation is all/contains/transitions')
        if type(limit) is not int or not 1<=limit<=1000 or type(offset) is not int or not 0<=offset<=20000:
            raise Fault('invalid_arguments','limit must be 1..1000 and offset 0..20000')
        bound = self.gui._source(source)
        self.gui._find({'source':source,'limit':1})
        app_id, app = self.app(bound)
        live = self.latest.get(source,{})
        bindings = {}
        for row in live.get('rows',[]):
            identity = live['refs'].get(row['ref'])
            if identity: bindings.setdefault(identity,[]).append(row)
        matched = {key for key,row in app['nodes'].items() if not query or row['name_hash']==label_hash(query) or any(query.casefold() in r.get('name','').casefold() for r in bindings.get(key,[]))}
        edges = []
        for key,row in app['edges'].items():
            if relation=='contains' and row['kind']!='contains' or relation=='transitions' and row['kind']!='transition': continue
            involved = {row['from'],row['to']} if row['kind']=='contains' else {s['target'] for s in row['steps']}
            if not query or involved & matched: edges.append({'id':key,**copy.deepcopy(row)})
        page = edges[offset:offset+limit]
        wanted, states = set(), {live.get('state')}
        for edge in page:
            if edge['kind']=='contains': wanted.update((edge['from'],edge['to']))
            else:
                states.update((edge['from'],edge['to'])); wanted.update(s['target'] for s in edge['steps'])
        if query: wanted.update(sorted(matched)[:100])
        nodes = []
        for key in sorted(wanted):
            row = app['nodes'].get(key)
            if row is None: continue
            current = bindings.get(key,[])
            from gui_identity import public_locator
            unique={r.get('control_id',r['ref']) for r in current}
            health=copy.deepcopy(row.get('health',{})); health['resolution_success_rate']=health.get('resolved',0)/health['attempts'] if health.get('attempts') else None
            nodes.append({'id':key,'identity':'locator_pattern',**copy.deepcopy(row),'health':health,'live_matches':[public_locator(r) for r in current[:16]],
                          'live_match_count':len(unique),'live_binding_count':len(current),'unique_live_match':len(unique)==1,
                          'historical_record_authorizes_execution':False})
        return {'format':FORMAT,'app':app_id,'source':source,'current_state':live.get('state'),
                'nodes':nodes,'edges':page,'matched_edges':len(edges),'has_more':offset+len(page)<len(edges),
                'states':[{'id':key,'observations':app['states'][key]['observations'],'partial':app['states'][key]['partial'],
                           'node_patterns':len(app['states'][key]['items'])} for key in sorted(k for k in states if k in app['states'])],
                'registered':{field:len(app[field]) for field in ('nodes','states','edges')},
                'outcomes':copy.deepcopy(app['outcomes']),'persistence':self.store.persistence,'truncated':app['truncated'],
                'awaiting_later_observation':source in self.pending and time.monotonic()-self.pending[source].get('first_unchanged_at',time.monotonic())<5,
                'limits':LIMITS,'inferred_edges_registered':0,'automatic_gui_actions':0,'automatic_retries':0,
                'coverage':'observed objects and action sequences only; app completeness unmeasured',
                'query_policy':'live labels support substring; historical labels support exact normalized hash match',
                'disk_content':'hashed locators, roles, methods, structure and counters; no UI labels, values, arguments or refs'}

    def rebind(self, source, node_id, method=None, app=None, allow_migration=False):
        from gui_identity import public_locator
        from gui_api import SHORTCUTS, METHODS
        if not hexadecimal(node_id) or app is not None and not hexadecimal(app) or type(allow_migration) is not bool:
            raise Fault('invalid_arguments','node_id/app must be registered hashes; allow_migration is boolean')
        if method is not None and SHORTCUTS.get(method,method) not in METHODS: raise Fault('invalid_arguments','Unknown rebind method')
        method=SHORTCUTS.get(method,method)
        bound=self.gui._source(source)
        self.gui._find({'source':source,'refresh':True,'limit':1})
        current_id,current=self.app(bound)
        historical_id=app or current_id
        historical=self.store.apps.get(historical_id)
        node=historical.get('nodes',{}).get(node_id) if historical else None
        if node is None: raise Fault('element_not_found','Registered node not found; provide its app hash or import its candidates')
        migrated=historical_id!=current_id
        if migrated and (not allow_migration or historical.get('family')!=current.get('family')):
            raise Fault('stale_capability','Version/source namespace changed; explicit same-family candidate migration is required')
        live=self.latest.get(source,{})
        matches=[]
        for row in live.get('rows',[]):
            identity=live.get('refs',{}).get(row['ref'])
            locator=current['nodes'].get(identity,{})
            same=identity==node_id
            if migrated:
                same=(locator.get('role')==node.get('role') and locator.get('structure_hash')==node.get('structure_hash') and
                      locator.get('automation_id_hash')==node.get('automation_id_hash') and locator.get('class_hash')==node.get('class_hash'))
            if same and (method is None or method in row.get('methods',[])): matches.append(row)
        logical={}
        for row in matches: logical.setdefault(row.get('control_id',row['ref']),[]).append(row)
        count=len(logical)
        health=node.setdefault('health',{})
        for key in ('attempts','resolved','ambiguous','missing'): health.setdefault(key,0)
        health['attempts']+=1; health['last_resolution_at']=time.time()
        health['resolved' if count==1 else 'ambiguous' if count else 'missing']+=1
        if count==1: health['last_success_at']=time.time()
        self.store.save()
        candidates=[]
        for controls in logical.values():
            candidates.append({**public_locator(controls[0]),'bindings':[public_locator(r) for r in controls]})
        # A migration match is a candidate, never automatic authority to execute.
        resolved=count==1 and not migrated
        return {'source':source,'node_id':node_id,'app':historical_id,'current_app':current_id,
            'status':'resolved' if resolved else 'migration_candidate' if count==1 else 'ambiguous' if count else 'not_observed',
            'ref':candidates[0]['ref'] if resolved else None,'candidates':candidates,'candidate_count':count,
            'historical_record_authorizes_execution':False,'live_identity_resolved':resolved,
            'version_migration':migrated,'migration_verified':False,
            'health':{**health,'resolution_success_rate':health['resolved']/health['attempts']},
            'next_action':'invoke_with_live_guards' if resolved else 'review_migration_candidate' if migrated and count else 'choose_candidate' if count else 'restore_or_observe_or_navigate'}

    def export(self, source):
        bound=self.gui._source(source)
        self.gui._find({'source':source,'limit':1})
        app_id,app=self.app(bound)
        return {'format':'appctl.learning-export/1','app':app_id,'candidates':clean_app(app),
                'historical_record_authorizes_execution':False,'contains_labels_values_or_live_refs':False}

    def import_candidates(self, data):
        if not isinstance(data,dict) or data.get('format')!='appctl.learning-export/1' or not hexadecimal(data.get('app')):
            raise Fault('invalid_arguments','Import requires appctl.learning-export/1 with app hash')
        try: encoded=json.dumps(data,allow_nan=False)
        except (TypeError,ValueError) as exc: raise Fault('invalid_arguments','Import must be finite JSON') from exc
        if len(encoded.encode())>LIMITS['bytes']: raise Fault('invalid_arguments','Import exceeds bounded cache size')
        candidate=clean_app(data.get('candidates'))
        local=self.store.apps.setdefault(data['app'],clean_app({}))
        for key,row in candidate['nodes'].items():
            if key not in local['nodes']:
                row.pop('health',None); local['nodes'][key]=row
        if not local.get('family') and candidate.get('family'): local['family']=candidate['family']
        local['updated_at']=time.time()
        self.store.save()
        return {'app':data['app'],'candidate_nodes':len(candidate['nodes']),'persistence':self.store.persistence,
                'imported_as':'candidates_only','historical_record_authorizes_execution':False,'next_action':'rebind'}
