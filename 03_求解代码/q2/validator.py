# -*- coding: utf-8 -*-
"""P5：独立校验器。

从原始规范化输入与最终解（架次—货箱—机型—设备编号—开始时刻）出发重新计算，
不直接相信候选缓存中的能耗、结束时刻或优化器汇总值：重新调用物理函数重建逐航段
载荷、事件顺序与资源占用，并逐项给出证据。
"""
import math
from collections import defaultdict
from simulator import simulate_route


def _overlap_check(records, cap, label):
    """按编号检查区间不重叠，并给出同时占用峰值。"""
    by_id = defaultdict(list)
    for r in records:
        by_id[r['id']].append((r['start'], r['end'], r['key']))
    errors = []
    peak = 0
    for rid, ints in by_id.items():
        ints.sort()
        for k in range(1, len(ints)):
            if ints[k][0] < ints[k - 1][1] - 1e-6:
                errors.append(dict(resource=rid, label=label,
                                   conflict=[ints[k - 1], ints[k]]))
    events = []
    for rid, ints in by_id.items():
        for st, en, _ in ints:
            events.append((st, 1))
            events.append((en, -1))
    cur = 0
    for _, dv in sorted(events):
        cur += dv
        peak = max(peak, cur)
    return errors, peak, len(by_id)


def validate(solution, pool, inst, geo, cfg, tolerance=1e-6):
    box_index = {b['box_id']: b for b in inst['boxes']}
    types = inst['types']
    models = {c['candidate_id']: c for c in pool.items}
    report = {}

    # ---- 1) 重新仿真每一趟架次 ----
    sorties = []
    for s in solution['sorties']:
        c = models[s['candidate_id']]
        stops = [(z, list(ids)) for z, ids in c['stops']]
        ev = simulate_route(c['type_id'], stops, inst, geo, box_index)
        assert ev['feasible'], '最终解出现不可行架次 %s' % s['sortie_id']
        sorties.append(dict(sortie_id=s['sortie_id'], candidate=c, ev=ev,
                            start=s['start_s'], aircraft_id=s['aircraft_id'],
                            battery_id=s['battery_id'], type_id=c['type_id']))

    # ---- 2) 货箱覆盖 ----
    seen = defaultdict(list)
    for so in sorties:
        for b in so['candidate']['box_ids']:
            seen[b].append(so['sortie_id'])
    dup = {b: v for b, v in seen.items() if len(v) > 1}
    missing = [b['box_id'] for b in inst['boxes'] if b['box_id'] not in seen]
    wrong_zone = []
    for so in sorties:
        for z, ids in so['candidate']['stops']:
            for b in ids:
                if box_index[b]['zone_id'] != z:
                    wrong_zone.append((b, z, box_index[b]['zone_id']))
    report['box_coverage'] = dict(n_boxes=len(inst['boxes']), n_delivered=len(seen),
                                  duplicates=dup, missing=missing,
                                  wrong_zone=wrong_zone,
                                  ok=not dup and not missing and not wrong_zone)

    # ---- 3) 交付时刻与硬时限 ----
    delivery = {}
    for so in sorties:
        for b, off in so['ev']['delivery_offsets'].items():
            delivery[b] = so['start'] + off
    hard_rows, soft_tard, wct = [], 0.0, 0.0
    for b in inst['boxes']:
        d = delivery.get(b['box_id'])
        row = dict(box_id=b['box_id'],
                   sortie_id=(seen[b['box_id']][0] if b['box_id'] in seen else None),
                   zone_id=b['zone_id'], deliver_s=round(d, 3) if d is not None else None,
                   expected_s=b['expected_s'], hard_deadline_s=b['hard_deadline_s'],
                   priority=b['priority'])
        if d is not None and b['expected_s'] is not None:
            t = max(0.0, d - b['expected_s'])
            soft_tard += t * b['priority']
            wct += b['priority'] * d
            row['soft_tardiness_s'] = round(t * b['priority'], 3)
        else:
            row['soft_tardiness_s'] = None
        if b['hard_deadline_s'] is not None and d is not None:
            row['hard_slack_s'] = round(b['hard_deadline_s'] - d, 3)
            row['violation'] = row['hard_slack_s'] < -tolerance
            hard_rows.append(row)
        elif b['hard_deadline_s'] is not None:
            row['hard_slack_s'] = None
            row['violation'] = True
            hard_rows.append(row)
    min_slack = min([r['hard_slack_s'] for r in hard_rows], default=None)
    worst = min(hard_rows, key=lambda r: r['hard_slack_s']) if hard_rows else None
    report['deadlines'] = dict(n_hard=len(hard_rows),
                              n_violation=len([r for r in hard_rows
                                               if r.get('hard_slack_s', 0) < -tolerance]),
                              min_hard_slack_s=min_slack,
                              worst_box=worst['box_id'] if worst else None,
                              ok=all(r['hard_slack_s'] >= -tolerance for r in hard_rows))

    # ---- 4) 载荷与能量逐航段 ----
    load_rows = []
    payload_ok = True
    for so in sorties:
        ev = so['ev']
        t = types[so['type_id']]
        if ev['initial_mass_kg'] > t['cap_mass_kg'] + 1e-9 or \
                ev['initial_volume_m3'] > t['cap_volume_m3'] + 1e-9:
            payload_ok = False
        if ev['energy_kwh'] > t['energy_usable_kwh'] + 1e-9:
            payload_ok = False
        if abs(ev['legs'][-1]['payload_kg']) > 1e-9:
            payload_ok = False
        for k, leg in enumerate(ev['legs']):
            load_rows.append(dict(sortie_id=so['sortie_id'], leg=k + 1,
                                  from_node=leg['from_node'], to_node=leg['to_node'],
                                  d_km=round(leg['d_m'] / 1000, 4),
                                  cruise_z_m=round(leg['cruise_z_m'], 2),
                                  payload_kg=round(leg['payload_kg'], 4),
                                  energy_kwh=round(leg['energy_kwh'], 6),
                                  time_s=round(leg['time_s'], 2)))
    report['payload_energy'] = dict(ok=payload_ok, n_legs=len(load_rows),
                                    total_energy_kwh=round(sum(s['ev']['energy_kwh']
                                                               for s in sorties), 4))

    # ---- 5) 设备与电池时间线 ----
    ac_records, bat_records = [], []
    for so in sorties:
        ac_records.append(dict(id=so['aircraft_id'], start=so['start'],
                               end=so['start'] + so['ev']['duration_s'],
                               key=so['sortie_id']))
        bat_records.append(dict(id=so['battery_id'], start=so['start'],
                                end=so['start'] + so['ev']['duration_s'] + so['ev']['recharge_s'],
                                key=so['sortie_id']))
    ac_err, ac_peak, ac_used = _overlap_check(ac_records, 0, 'aircraft')
    bat_err, bat_peak, bat_used = _overlap_check(bat_records, 0, 'battery')
    type_ok = all(models[s['candidate_id']]['type_id'] ==
                  inst['aircraft'][s['aircraft_id']]['type_id'] and
                  models[s['candidate_id']]['type_id'] ==
                  inst['batteries'][s['battery_id']]['type_id'] for s in solution['sorties'])
    used_by_type = defaultdict(set)
    for so in sorties:
        used_by_type[so['type_id']].add(so['battery_id'])
    count_ok = all(len(v) <= inst['types'][g]['n_batteries']
                   for g, v in used_by_type.items())
    report['resources'] = dict(
        aircraft=dict(n_used=ac_used, peak_concurrent=ac_peak, conflicts=ac_err),
        battery=dict(n_used=bat_used, peak_concurrent=bat_peak, conflicts=bat_err,
                     per_type={g: len(v) for g, v in used_by_type.items()}),
        aircraft_type_match=type_ok, battery_count_ok=count_ok,
        ok=not ac_err and not bat_err and type_ok and count_ok)

    # ---- 6) 时间口径与汇总指标 ----
    makespan = max(s['start'] + s['ev']['duration_s'] for s in sorties)
    energy = sum(s['ev']['energy_kwh'] for s in sorties)
    metrics = dict(weighted_tardiness=round(soft_tard, 6),
                   makespan_s=round(makespan, 6), energy_kwh=round(energy, 6),
                   n_sorties=len(sorties),
                   sum_priority_times_delivery=round(wct, 6))
    reported = solution.get('metrics', {})
    cmp = {}
    for k, v in metrics.items():
        rv = reported.get(k)
        cmp[k] = dict(recomputed=v, reported=rv,
                      ok=(rv is None or abs(v - rv) <= max(1e-3, abs(v) * 1e-6)))
    report['metrics'] = dict(recomputed=metrics, compare=cmp,
                             consistent=all(x['ok'] for x in cmp.values()))

    report['overall_ok'] = (report['box_coverage']['ok'] and report['deadlines']['ok']
                            and report['payload_energy']['ok'] and report['resources']['ok']
                            and report['metrics']['consistent'])
    report['detail'] = dict(legs=load_rows, hard_boxes=sorted(hard_rows,
                                                              key=lambda r: r['hard_slack_s']),
                            deliveries=sorted(delivery.items()))
    return report
