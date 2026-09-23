# -*- coding: utf-8 -*-
"""P3：把无冲突的调度区间落实到具体无人机与电池编号。

同型设备初始状态完全相同，因此按开始时刻排序后用最小堆做区间着色即可：
每一步取出“最早可用”的那台设备/那组电池，若能满足 available <= start 就分配，
否则说明区间构造或容量约束有误，必须回查而不能临时增建资源。
"""
import heapq


def assign_ids(selection, pool, inst):
    models = {c['candidate_id']: c for c in pool.items}
    ac_pool, bat_pool = {}, {}
    for aid, a in sorted(inst['aircraft'].items()):
        ac_pool.setdefault(a['type_id'], []).append(aid)
    for bid, b in sorted(inst['batteries'].items()):
        bat_pool.setdefault(b['type_id'], []).append(bid)

    rows = []
    problems = []
    for g in sorted(ac_pool):
        sel = sorted([s for s in selection if models[s['candidate_id']]['type_id'] == g],
                     key=lambda s: s['start_s'])
        h_ac = [(0.0, aid) for aid in ac_pool[g]]
        h_bat = [(0.0, bid) for bid in bat_pool[g]]
        heapq.heapify(h_ac)
        heapq.heapify(h_bat)
        for s in sel:
            c = models[s['candidate_id']]
            start = float(s['start_s'])
            t_ac, aid = heapq.heappop(h_ac)
            t_bat, bid = heapq.heappop(h_bat)
            if t_ac > start + 1e-6 or t_bat > start + 1e-6:
                problems.append(dict(candidate_id=c['candidate_id'], start_s=start,
                                     aircraft_ready=t_ac, battery_ready=t_bat))
            heapq.heappush(h_ac, (start + c['duration_s'], aid))
            heapq.heappush(h_bat, (start + c['duration_s'] + c['recharge_s'], bid))
            rows.append(dict(candidate_id=c['candidate_id'], type_id=g,
                             aircraft_id=aid, battery_id=bid, start_s=start))
    rows.sort(key=lambda r: (r['start_s'], r['type_id']))
    for k, r in enumerate(rows):
        r['sortie_id'] = 'F%02d' % (k + 1)
    return rows, problems
