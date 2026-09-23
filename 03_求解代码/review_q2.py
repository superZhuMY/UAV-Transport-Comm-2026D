"""Read-only review of existing runs; writes only into q2/audit.

Reconstructs routes from saved deliveries, independently recalculates physics,
checks actual resource IDs, reproduces unsafe pruning, and explores rescheduling
of the exact saved tasks without changing their models, boxes or geometry.
"""
from pathlib import Path
from collections import Counter, defaultdict
import copy
import hashlib
import itertools
import json
import math
import random
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data_io import load_instance, load_config, input_hash
from candidates import Pool, build_pool
from validator import validate


class CacheGeometry:
    def __init__(self, path):
        self.seg = json.loads(path.read_text(encoding='utf-8'))['segments']

    def seg_of(self, a, b):
        return self.seg[f'{a}|{b}']


def reconstruct(saved, inst):
    boxes = {b['box_id']: b for b in inst['boxes']}
    grouped = defaultdict(list)
    errors = []
    counts = Counter(d['box_id'] for d in saved['deliveries'])
    if counts != Counter(boxes.keys()):
        errors.append('box coverage mismatch')
    for d in saved['deliveries']:
        if d['box_id'] not in boxes:
            errors.append(f"unknown box {d['box_id']}")
            continue
        if d['zone_id'] != boxes[d['box_id']]['zone_id']:
            errors.append(f"wrong zone {d['box_id']}")
        grouped[d['sortie_id']].append(d['box_id'])
    tasks = []
    for row in saved['sorties']:
        route = row['stop_sequence'].split('→')
        if route[0] != 'O01' or route[-1] != 'O01':
            errors.append(f"missing depot {row['sortie_id']}")
        seq = route[1:-1]
        if len(set(seq)) != len(seq):
            raise ValueError('Saved format cannot disambiguate repeated-zone stops')
        bids = grouped.pop(row['sortie_id'], [])
        stops = [(z, [b for b in bids if boxes[b]['zone_id'] == z]) for z in seq]
        if any(not ids for _, ids in stops) or sum(len(ids) for _, ids in stops) != len(bids):
            errors.append(f"route-delivery mismatch {row['sortie_id']}")
        tasks.append(dict(row=row, stops=stops))
    if grouped:
        errors.append('delivery references unknown sortie')
    return tasks, errors


def evaluate_task(task, inst, geo):
    row, stops = task['row'], task['stops']
    t = inst['types'][row['type_id']]
    boxes = {b['box_id']: b for b in inst['boxes']}
    bids = [b for _, ids in stops for b in ids]
    mass = sum(boxes[b]['mass_g'] for b in bids) / 1000
    vol = sum(boxes[b]['vol_cc'] for b in bids) / 1e6
    errors = []
    if mass > t['cap_mass_kg'] + 1e-9 or vol > t['cap_volume_m3'] + 1e-9:
        errors.append('payload limit')
    q = mass
    clock = t['t_prep_s'] + len(bids) * t['t_load_s']
    energy = 0.0
    offsets = {}
    cur = 'O01'
    legs = []
    for zone, ids in stops + [('O01', [])]:
        s = geo.seg_of(cur, zone)
        up = s['cruise_z_m'] - inst['nodes'][cur]['work_z']
        down = s['cruise_z_m'] - inst['nodes'][zone]['work_z']
        if min(up, down) < -1e-8:
            errors.append('negative height')
        length = t['range_empty_m'] - (t['range_empty_m'] - t['range_full_m']) * (q / t['cap_mass_kg']) ** 1.5
        e = t['energy_kwh'] * s['d_m'] / length + (t['mass_kg'] + q) * 9.81 * up / (t['eta_up'] * 3.6e6)
        dt = up / t['v_up'] + s['d_m'] / t['v_cruise'] + down / t['v_down']
        energy += e
        clock += dt
        legs.append(dict(i=cur, j=zone, payload=q, energy=e, time=dt))
        if ids:
            clock += t['t_hand_base_s'] + len(ids) * t['t_hand_box_s']
            for b in ids:
                offsets[b] = clock
            q -= sum(boxes[b]['mass_g'] for b in ids) / 1000
        cur = zone
    soc = 1 - energy / t['energy_kwh']
    if energy > (1-t['reserve_pct']/100) * t['energy_kwh'] + 1e-9:
        errors.append('reserve violation')
    if abs(q) > 1e-9:
        errors.append('return not empty')
    recharge = t['full_charge_s'] * (0.65 * (0.9-soc)/0.9+0.35) if soc < 0.9 else t['full_charge_s'] * 0.35*(1-soc)/0.1
    latest_zero = min(min(boxes[b]['expected_s'], boxes[b]['hard_deadline_s'] or math.inf) - off for b, off in offsets.items())
    return dict(sortie_id=row['sortie_id'], type_id=row['type_id'], stops=stops,
                box_ids=bids, mass=mass, volume=vol, duration=clock, recharge=recharge,
                energy=energy, soc=soc, offsets=offsets, legs=legs,
                latest_zero=latest_zero, errors=errors)


def assess(evs, rows, inst):
    boxes = {b['box_id']: b for b in inst['boxes']}
    idx = {e['sortie_id']: e for e in evs}
    delivery, aircraft, batteries = {}, defaultdict(list), defaultdict(list)
    errors = []
    energy = makespan = weighted_tard = wct = 0.0
    slack = []
    for r in rows:
        e = idx[r['sortie_id']]
        st = r['start_s']
        if not math.isfinite(st) or st < 0:
            errors.append(f"negative/nonfinite start {r['sortie_id']}")
        if inst['aircraft'].get(r['aircraft_id'], {}).get('type_id') != e['type_id']:
            errors.append('aircraft type mismatch')
        if inst['batteries'].get(r['battery_id'], {}).get('type_id') != e['type_id']:
            errors.append('battery type mismatch')
        errors.extend(f"{e['sortie_id']}:{err}" for err in e['errors'])
        end = st + e['duration']
        energy += e['energy']
        makespan = max(makespan, end)
        aircraft[r['aircraft_id']].append((st, end, e['sortie_id']))
        batteries[r['battery_id']].append((st, end+e['recharge'], e['sortie_id']))
        for b, off in e['offsets'].items():
            if b in delivery:
                errors.append(f'duplicate {b}')
            delivery[b] = st+off
    if set(delivery) != set(boxes):
        errors.append('incomplete coverage')
    for b, d in delivery.items():
        box = boxes[b]
        weighted_tard += box['priority'] * max(0, d-box['expected_s'])
        wct += box['priority'] * d
        if box['hard_deadline_s'] is not None:
            s = box['hard_deadline_s'] - d
            slack.append((s, b))
            if s < -1e-8:
                errors.append(f'late {b}')
    peak = {}
    for label, resources in [('aircraft', aircraft), ('battery', batteries)]:
        events = []
        for rid, intervals in resources.items():
            previous_end = -math.inf
            for st, end, sid in sorted(intervals):
                if st < previous_end-1e-7:
                    errors.append(f'overlap {rid} {sid}')
                previous_end = max(previous_end, end)
                events.extend([(st, 1), (end, -1)])
        cur = mx = 0
        for _, delta in sorted(events):
            cur += delta
            mx = max(mx, cur)
        peak[label] = mx
    return dict(ok=not errors, errors=errors, weighted_tardiness=weighted_tard,
                makespan_s=makespan, energy_kwh=energy, n_sorties=len(rows),
                weighted_completion=wct, last_delivery_s=max(delivery.values()),
                min_hard_slack_s=min(slack)[0], worst_box=min(slack)[1],
                n_aircraft=len(aircraft), n_batteries=len(batteries), peaks=peak,
                min_return_soc=min(e['soc'] for e in evs),
                sorties_by_type=dict(Counter(e['type_id'] for e in evs)),
                multi_stop=[e['sortie_id'] for e in evs if len(e['stops'])>1],
                delivery_times=delivery)


def fixed_task_search(evs, original_rows, inst, seed=20260923):
    rng = random.Random(seed)
    selected = []
    search_stats = {}
    for g in sorted(inst['types']):
        tasks = [e for e in evs if e['type_id']==g]
        if not tasks:
            continue
        ac_ids = sorted(k for k,v in inst['aircraft'].items() if v['type_id']==g)
        bt_ids = sorted(k for k,v in inst['batteries'].items() if v['type_id']==g)
        original = [r for r in original_rows if r['type_id']==g]
        idx = {e['sortie_id']:e for e in tasks}
        best_rows = copy.deepcopy(original)
        def key(rows):
            return (max(r['start_s']+idx[r['sortie_id']]['duration'] for r in rows),
                    sum(r['start_s'] for r in rows))
        best_key = key(best_rows)
        trials = feasible = 0
        def schedule(order):
            av = [(0.0,k) for k in ac_ids]
            bv = [(0.0,k) for k in bt_ids]
            rows=[]
            for e in order:
                ai=min(range(len(av)),key=lambda i:av[i])
                bi=min(range(len(bv)),key=lambda i:bv[i])
                start=float(math.ceil(max(av[ai][0],bv[bi][0])-1e-9))
                if start>e['latest_zero']+1e-8:
                    return None
                rows.append(dict(sortie_id=e['sortie_id'],type_id=g,start_s=start,
                                 aircraft_id=av[ai][1],battery_id=bv[bi][1]))
                av[ai]=(start+e['duration'],av[ai][1])
                bv[bi]=(start+e['duration']+e['recharge'],bv[bi][1])
            return rows
        if len(tasks)<=8:
            orders=itertools.permutations(tasks)
            method='all task permutations with earliest-start dispatch'
        else:
            def random_orders():
                yield sorted(tasks,key=lambda e:e['latest_zero'])
                yield sorted(tasks,key=lambda e:-e['duration'])
                old_order=sorted(original,key=lambda r:r['start_s'])
                yield [idx[r['sortie_id']] for r in old_order]
                for _ in range(40000):
                    order=list(tasks);rng.shuffle(order);yield order
            orders=random_orders()
            method='40000 seeded random permutations plus priority orders'
        for order in orders:
            trials+=1
            rows=schedule(order)
            if rows is None:
                continue
            feasible+=1
            k=key(rows)
            if k<best_key:
                best_rows,best_key=rows,k
        # Improve the incumbent through permutation neighbourhoods.
        for _ in range(15):
            order=[idx[r['sortie_id']] for r in sorted(best_rows,key=lambda r:r['start_s'])]
            improved=False
            for i in range(len(order)):
                for j in range(len(order)):
                    if i==j:continue
                    trial=list(order);e=trial.pop(i);trial.insert(j,e)
                    trials+=1;rows=schedule(trial)
                    if rows is not None:
                        feasible+=1;k=key(rows)
                        if k<best_key:
                            best_rows,best_key=rows,k;improved=True
            if not improved:break
        selected.extend(best_rows)
        search_stats[g]=dict(method=method,trials=trials,feasible=feasible,best_makespan=best_key[0])
    return sorted(selected,key=lambda r:(r['start_s'],r['sortie_id'])),search_stats


def review():
    inst=load_instance(verbose=False);cfg=load_config()
    geo=CacheGeometry(ROOT/'cache'/f'segments_{input_hash()}.json')
    raw=[];orig_prune=Pool.prune_dominated
    def capture(self):
        raw.extend(copy.deepcopy(self.items))
        return orig_prune(self)
    Pool.prune_dominated=capture
    try:pool,bidx,_=build_pool(inst,geo,cfg,verbose=False)
    finally:Pool.prune_dominated=orig_prune
    def ck(c):return(c['type_id'],tuple((z,tuple(sorted(ids))) for z,ids in c['stops']))
    kept={ck(c) for c in pool.items};unsafe=[]
    for c in raw:
        if ck(c) in kept:continue
        group=[o for o in raw if ck(o)!=ck(c) and o['type_id']==c['type_id'] and set(o['box_ids'])==set(c['box_ids'])]
        wrong=[];truly=False
        for o in group:
            base=o['duration_s']<=c['duration_s'] and o['energy_kwh']<=c['energy_kwh']+1e-9 and o['duration_s']+o['recharge_s']<=c['duration_s']+c['recharge_s']
            if base and all(o['offsets'][b]<=c['offsets'][b]+1e-9 for b in c['box_ids']):
                truly=True
            if base and o['max_offset_s']<=c['max_offset_s'] and (o['duration_s']<c['duration_s'] or o['energy_kwh']<c['energy_kwh']-1e-9 or o['max_offset_s']<c['max_offset_s']):
                worse=[dict(box=b,deleted_offset=c['offsets'][b],kept_offset=o['offsets'][b],hard=bidx[b]['hard_deadline_s']) for b in c['box_ids'] if o['offsets'][b]>c['offsets'][b]+1e-7]
                if worse:wrong.append(dict(alternative_route=o['stops'],worse_boxes=worse))
        if wrong and not truly:unsafe.append(dict(deleted_route=c['stops'],type_id=c['type_id'],examples=wrong[:1]))
    output=dict(current_input_hash=input_hash(),pool_n=len(pool.items),raw_n=len(raw),
                multi_by_type=dict(Counter(c['type_id'] for c in pool.items if len(c['stops'])>1)),
                unsafe_pruning_count=len(unsafe),unsafe_pruning_examples=unsafe[:4],runs={})
    for run in ['q2_run01','q2_run02']:
        saved=json.loads((ROOT/'runs'/run/'solution.json').read_text(encoding='utf-8'))
        tasks,errs=reconstruct(saved,inst)
        evs=[evaluate_task(t,inst,geo) for t in tasks]
        checks=assess(evs,saved['sorties'],inst)
        checks['reconstruction_errors']=errs
        checks['max_saved_delivery_difference_s']=max(abs(d['deliver_s']-checks['delivery_times'][d['box_id']]) for d in saved['deliveries'])
        original=validate(saved,pool,inst,geo,cfg)
        tampered=copy.deepcopy(saved)
        tampered['sorties'][0]['stop_sequence']='O01→S015→O01'
        tampered['deliveries'][0]['deliver_s']=999999.0
        mutation=validate(tampered,pool,inst,geo,cfg)
        newrows,search=fixed_task_search(evs,saved['sorties'],inst)
        improved=assess(evs,newrows,inst)
        if not improved['ok'] or improved['weighted_tardiness']>1e-7:raise RuntimeError('reschedule validation failed')
        event_by_id={e['sortie_id']:e for e in evs}
        normalized_rows=[]
        for r in newrows:
            e=event_by_id[r['sortie_id']]
            normalized_rows.append(dict(
                sortie_id=r['sortie_id'],type_id=e['type_id'],
                aircraft_id=r['aircraft_id'],battery_id=r['battery_id'],
                start_s=r['start_s'],return_s=r['start_s']+e['duration'],
                battery_ready_s=r['start_s']+e['duration']+e['recharge'],
                duration_s=e['duration'],energy_kwh=e['energy'],return_soc=e['soc'],
                initial_mass_kg=e['mass'],initial_volume_m3=e['volume'],
                stop_sequence='→'.join(['O01']+[z for z,_ in e['stops']]+['O01']),
                stops=e['stops']))
        detailed=dict(source_run=run,geometry='existing cached conservative 2x2 geometry, unchanged',
                      change_scope='Only aircraft/battery assignment and integer starts; same models, boxes, routes',
                      metrics={k:v for k,v in improved.items() if k!='delivery_times'},
                      sorties=normalized_rows,
                      deliveries=improved['delivery_times'],search=search)
        (Path(__file__).parent/f'{run}_fixed_route_rescheduled.json').write_text(json.dumps(detailed,ensure_ascii=False,indent=2),encoding='utf-8')
        checks.pop('delivery_times');improved.pop('delivery_times')
        output['runs'][run]=dict(independent_checks=checks,original_validator_ok=original['overall_ok'],
                                tampered_route_and_delivery_still_passes=mutation['overall_ok'],
                                rescheduled=improved,search=search)
        print(run,json.dumps(dict(original=checks,improved=improved),ensure_ascii=False),flush=True)
    output['source_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob('*.py')}
    (Path(__file__).parent/'review_evidence.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print('UNSAFE',len(unsafe),'MULTI',output['multi_by_type'],flush=True)


if __name__=='__main__':review()
