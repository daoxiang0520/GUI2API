"""Conservative live control identities. Labels describe objects, not identity.

Rectangles and HWNDs are disambiguators in live responses. Persistent locators
use hashed ancestor paths and structural ordinals, and never authorize input.
"""
from collections import Counter, defaultdict
import hashlib
import json


def hashed(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:24]


def annotate(rows, window=None):
    by_ref = {r['ref']:r for r in rows}
    ordinals, counters = {}, Counter()
    def token(r):
        # Name deliberately excluded: placeholder/value/focus can change it.
        return [r.get('role','Custom'), r.get('class_name',''), r.get('automation_id','')]
    for r in rows:
        key = (r.get('backend'), r.get('parent'), hashed(token(r)))
        ordinals[r['ref']] = counters[key]; counters[key] += 1
    paths, visiting = {}, set()
    def path(r):
        ref = r['ref']
        if ref in paths: return paths[ref]
        if ref in visiting: return []
        visiting.add(ref)
        parent = by_ref.get(r.get('parent'))
        # Fold the full ancestry. Truncating a path to its last N ancestors
        # would collapse deep identical branches whose distinguishing split is
        # above that cutoff (common in deeply nested accessibility trees).
        result = hashed([path(parent) if parent else None,token(r),ordinals[ref]])
        visiting.remove(ref); paths[ref] = result
        return result
    counts = Counter((r.get('role'), r.get('name','')) for r in rows)
    for r in rows:
        chain = path(r)
        parent = by_ref.get(r.get('parent'))
        parent_chain = path(parent) if parent else hashed([])
        r.update(window=window, sibling_index=ordinals[r['ref']],
                 parent_fingerprint=parent_chain, path_fingerprint=chain,
                 fingerprint=hashed([r.get('backend'),chain]),
                 identity_confidence='high' if r.get('automation_id') and counters[(r.get('backend'),r.get('parent'),hashed(token(r)))]==1 else 'medium' if r.get('parent') else 'low',
                 identity_basis='automation_id_and_structure' if r.get('automation_id') else 'ancestor_path_and_structural_ordinal',
                 same_name_role_count=counts[(r.get('role'),r.get('name',''))])
        r.setdefault('rect',{})
    return rows


def group_controls(rows):
    """Merge only strong geometric duplicates; keep every exact method binding.

    Coincident anonymous containers are NOT enough evidence for a merge. Named
    duplicates in distinct provider roots may be merged, but same-parent sibling
    objects stay separate. Geometry is never an executable locator by itself.
    """
    by_ref = {r['ref']:r for r in rows}
    buckets, groups = defaultdict(list), []
    def root(r):
        seen=set()
        while r.get('parent') in by_ref and r['ref'] not in seen:
            seen.add(r['ref']); r=by_ref[r['parent']]
        return r['ref']
    for row in rows:
        rect=row.get('rect',{})
        valid=rect.get('width',0)>0 and rect.get('height',0)>0
        key=(row.get('window'),row.get('role'),*(round(rect.get(k,0),1) for k in ('x','y','width','height')),
             row.get('enabled',True),row.get('offscreen',False),row.get('password',False))
        selected=None
        if valid:
            for group in buckets[key]:
                first=group['rows'][0]
                named=bool(row.get('name','').strip()) and row.get('name')==first.get('name')
                same_handle=bool(row.get('hwnd')) and row.get('hwnd')==first.get('hwnd')
                separate=first.get('backend')!=row.get('backend') or root(first)!=root(row)
                if separate and (named or (same_handle and row.get('role') in ('Edit','ComboBox','Document'))):
                    selected=group; break
        if selected is None:
            selected={'id':'c'+row.get('fingerprint',hashed(row['ref'])),'rows':[]}
            groups.append(selected)
            if valid: buckets[key].append(selected)
        selected['rows'].append(row)
    logical_counts=Counter()
    for group in groups:
        for role,name in {(r.get('role'),r.get('name','')) for r in group['rows']}:logical_counts[(role,name)]+=1
    for group in groups:
        for row in group['rows']: row['control_id']=group['id']
        for row in group['rows']: row['unique_control_name_role_count']=logical_counts[(row.get('role'),row.get('name',''))]
    return groups


def public_locator(row):
    fields=('ref','name','role','backend','window','hwnd','rect','parent','automation_id','class_name',
            'fingerprint','parent_fingerprint','path_fingerprint','sibling_index','identity_confidence',
            'identity_basis','same_name_role_count','unique_control_name_role_count','control_id','enabled','offscreen','methods','invoke_reliability','recommended_method',
            'resource_count','resource_kinds')
    return {k:row[k] for k in fields if k in row}
