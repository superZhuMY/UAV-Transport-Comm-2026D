# -*- coding: utf-8 -*-
"""P2/P3：基线构造与 CP-SAT 联合调度。

baseline_greedy: 在候选池上做列表调度，得到 80 箱全覆盖的可行解（同时作为 CP-SAT 提示）。
solve_cpsat: 对候选池做“选架次 + 排开始时刻 + 设备/电池区间容量”联合优化，
             按 加权超期量 → 完工时刻 → 总能耗 → 架次数 的词典序推进。
"""
import math
import heapq

TARD_UNIT = 1          # 超期量以秒计（整数）
ENERGY_UNIT = 1000     # 能耗缩放为 0.001 kWh 的整数


# --------------------------------------------------------------------------- #
# 基线：候选池上的列表调度
# --------------------------------------------------------------------------- #
def baseline_greedy(pool, inst, cfg, order_key=None):
    types = inst['types']
    aircraft = {}
    batteries = {}
    for aid, a in inst['aircraft'].items():
        aircraft.setdefault(a['type_id'], []).append(0.0)
    for bid, b in inst['batteries'].items():
        batteries.setdefault(b['type_id'], []).append(0.0)

    cands = list(pool.items)
    if order_key is None:
        def order_key(c):
            hards = [c['latest_start_s'] if c['latest_start_s'] is not None else 1e9]
            return (hards[0], -c['n_boxes'], c['max_offset_s'], c['duration_s'])
    cands.sort(key=order_key)

    covered = set()
    chosen = []
    for c in cands:
        if any(b in covered for b in c['box_ids']):
            continue
        g = c['type_id']
        ac = min(aircraft[g])
        bt = min(batteries[g])
        start = max(ac, bt, 0.0)
        if c['latest_start_s'] is not None and start > c['latest_start_s'] + 1e-9:
            continue                                    # 该候选已无法满足硬时限，跳过
        aircraft[g].remove(ac)
        aircraft[g].append(start + c['duration_s'])
        batteries[g].remove(bt)
        batteries[g].append(start + c['duration_s'] + c['recharge_s'])
        covered.update(c['box_ids'])
        chosen.append(dict(candidate_id=c['candidate_id'], start_s=start))
    return dict(chosen=chosen, covered=sorted(covered),
                n_covered=len(covered), feasible=len(covered) == 80)


def evaluate_selection(sel, pool, inst, box_index):
    """按给定选择与开始时刻重算全部指标（供基线与最终报告使用）。"""
    types = inst['types']
    models = {c['candidate_id']: c for c in pool.items}
    per_box = {}
    energy = 0.0
    makespan = 0.0
    hard_ok, hard_min = True, None
    tard_sum, wct = 0.0, 0.0
    per_box_tard = {}
    for s in sel:
        c = models[s['candidate_id']]
        st = s['start_s']
        energy += c['energy_kwh']
        makespan = max(makespan, st + c['duration_s'])
        for b in c['box_ids']:
            d = st + c['offsets'][b]
            per_box[b] = d
    for b in inst['boxes']:
        d = per_box.get(b['box_id'])
        if d is None:
            hard_ok = False
            continue
        if b['hard_deadline_s'] is not None:
            slack = b['hard_deadline_s'] - d
            hard_min = slack if hard_min is None else min(hard_min, slack)
            if slack < -1e-9:
                hard_ok = False
        exp = b['expected_s']
        if exp is not None:
            t = max(0.0, d - exp)
            per_box_tard[b['box_id']] = t * b['priority']
            tard_sum += t * b['priority']
            wct += b['priority'] * d
    return dict(weighted_tardiness=tard_sum, makespan_s=makespan, energy_kwh=energy,
                n_sorties=len(sel), hard_ok=hard_ok,
                min_hard_slack_s=hard_min, sum_priority_times_delivery=wct,
                per_box_delivery=per_box, per_box_tardiness=per_box_tard)


# --------------------------------------------------------------------------- #
# CP-SAT 联合调度
# --------------------------------------------------------------------------- #
def solve_cpsat(pool, inst, cfg, hint=None, time_limit=None, verbose=True,
                fix_upper=None):
    """词典序推进：每个阶段重建模型并带上已固定的上界，用当前最好解作搜索提示。

    逐阶段重建是为了避免同一变量被重复提示（CP-SAT 会判为 MODEL_INVALID），
    也避免上一阶段的搜索状态影响下一阶段。
    """
    from ortools.sat.python import cp_model
    box_index = {b['box_id']: b for b in inst['boxes']}
    types = inst['types']
    cands = pool.items
    H0 = int(math.ceil(cfg['horizon_s']))
    if hint is not None:
        H0 = min(H0, int(math.ceil(hint['makespan_s'])) + int(cfg.get('horizon_slack_s', 20000)))

    cover = {}
    for c in cands:
        for b in c['box_ids']:
            cover.setdefault(b, []).append(c['candidate_id'])
    missing = [b['box_id'] for b in inst['boxes'] if b['box_id'] not in cover]
    if missing:
        raise RuntimeError('候选池未覆盖货箱: %s' % missing)
    n_ac = {g: sum(1 for a in inst['aircraft'].values() if a['type_id'] == g)
            for g in types}
    DUR = {c['candidate_id']: int(math.ceil(c['duration_s'])) for c in cands}

    def build(prev_vals, hint_sel, hi_start):
        model = cp_model.CpModel()
        x, start, dur, rec, iveh, ibat, ene = {}, {}, {}, {}, {}, {}, {}
        for c in cands:
            p = c['candidate_id']
            d = int(math.ceil(c['duration_s']))
            r = int(math.ceil(c['duration_s'] + c['recharge_s']))
            e = int(round(c['energy_kwh'] * ENERGY_UNIT))
            dur[p], rec[p], ene[p] = d, r, e
            st = model.NewIntVar(0, max(0, hi_start), 'S_%d' % p)
            bb = model.NewBoolVar('x_%d' % p)
            start[p], x[p] = st, bb
            iveh[p] = model.NewOptionalFixedSizeIntervalVar(st, d, bb, 'A_%d' % p)
            ibat[p] = model.NewOptionalFixedSizeIntervalVar(st, r, bb, 'B_%d' % p)
            if c['latest_start_s'] is not None:
                model.Add(st <= int(math.floor(c['latest_start_s'])))
        for bid, ps in cover.items():
            model.AddExactlyOne(x[p] for p in ps)
        for g in types:
            ps = [c['candidate_id'] for c in cands if c['type_id'] == g]
            if not ps:
                continue
            model.AddCumulative([iveh[p] for p in ps], [1] * len(ps), n_ac[g])
            model.AddCumulative([ibat[p] for p in ps], [1] * len(ps),
                                types[g]['n_batteries'])
        # 软超期量（医疗与首批仍为硬约束）
        tard_terms = {}
        for bid, ps in cover.items():
            b = box_index[bid]
            exp = b['expected_s']
            if exp is None:
                continue
            terms = []
            for p in ps:
                a = int(math.ceil(pool.items[p]['offsets'][bid]))
                ub = max(0, int(H0 + pool.items[p]['max_offset_s'] - exp))
                tv = model.NewIntVar(0, ub, 'T_%s_%d' % (bid, p))
                model.Add(tv >= start[p] + a - int(exp)).OnlyEnforceIf(x[p])
                terms.append(tv)
            tb = model.NewIntVar(0, 2 * H0 + 200000, 'TB_%s' % bid)
            model.Add(tb == cp_model.LinearExpr.Sum(terms))
            tard_terms[bid] = int(round(b['priority'])) * tb
        F = {}
        cmax = model.NewIntVar(0, 2 * H0 + 200000, 'Cmax')
        for c in cands:
            p = c['candidate_id']
            f = model.NewIntVar(0, 2 * H0 + 200000, 'F_%d' % p)
            model.Add(f == start[p] + dur[p]).OnlyEnforceIf(x[p])
            model.Add(f == 0).OnlyEnforceIf(x[p].Not())
            F[p] = f
        model.AddMaxEquality(cmax, list(F.values()))
        limits = {'tardiness': cp_model.LinearExpr.Sum(list(tard_terms.values())),
                  'makespan': cmax,
                  'energy': sum(ene[p] * x[p] for p in x),
                  'sorties': sum(x.values())}
        for k, v in prev_vals.items():
            model.Add(limits[k] <= v)
        for s in (hint_sel or []):
            p = s['candidate_id']
            model.AddHint(x[p], 1)
            model.AddHint(start[p], int(round(s['start_s'])))
        return model, limits, x, start

    order = cfg.get('objective_order',
                    ['tardiness', 'makespan', 'energy', 'sorties'])
    name_map = {'weighted_tardiness': 'tardiness', 'makespan': 'makespan',
                'energy': 'energy', 'sortie_count': 'sorties'}
    phases = [name_map.get(o, o) for o in order]
    tl = cfg['solver_time_limit_s']
    reports = []
    prev_vals = {}
    snapshot = hint['chosen'] if hint else None
    inc_makespan = int(math.ceil(hint['makespan_s'])) if hint else H0
    solver = cp_model.CpSolver()
    for name in phases:
        # 已加入的完工时间上界（或当前解的完工时间）可安全收紧开始时刻时间域
        hi_start = H0 if not prev_vals and name == phases[0] else min(H0, inc_makespan)
        model, limits, x, start = build(prev_vals, snapshot, hi_start)
        model.Minimize(limits[name])
        solver.parameters.max_time_in_seconds = float(tl.get(name, 60.0))
        solver.parameters.num_search_workers = int(cfg.get('num_workers', 4))
        solver.parameters.random_seed = int(cfg['random_seed'])
        if verbose:
            print('[P3] 求解阶段 %-10s 时间上限 %.0f s（开始时刻上界 %d s）...'
                  % (name, tl.get(name, 60.0), hi_start))
        status = solver.Solve(model)
        info = dict(phase=name, status=solver.StatusName(status), objective=None,
                    best_bound=None, elapsed_s=float(solver.WallTime()))
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            val = solver.ObjectiveValue()
            prev_vals[name] = int(math.ceil(val - 1e-6))
            info['objective'] = float(val)
            info['best_bound'] = float(solver.BestObjectiveBound())
            snapshot = [dict(candidate_id=c['candidate_id'],
                             start_s=float(solver.Value(start[c['candidate_id']])))
                        for c in cands if solver.Value(x[c['candidate_id']]) == 1]
            inc_makespan = int(math.ceil(max(
                s['start_s'] + DUR[s['candidate_id']] for s in snapshot)))
            if verbose:
                print('     -> %s 目标=%.4g 界=%.4g 用时=%.1fs'
                      % (solver.StatusName(status), val, solver.BestObjectiveBound(),
                         solver.WallTime()))
        else:
            if verbose:
                print('     -> %s：该阶段未取得可行解，回退到上一阶段已验证解'
                      % solver.StatusName(status))
        reports.append(info)
        if info['objective'] is None:
            break
    if snapshot is None:
        raise RuntimeError('四个阶段均未取得可行解，请检查候选池与时限设置。')
    return dict(selection=snapshot, reports=reports,
                status=reports[-1]['status'] if reports else 'UNKNOWN',
                model=dict(n_candidates=len(cands), horizon=H0))


if __name__ == '__main__':
    import json
    from data_io import load_instance, input_hash, load_config
    from geometry import Geometry
    from candidates import build_pool
    inst = load_instance(verbose=False)
    geo = Geometry(inst['nodes'])
    geo.build_segments(cache_key=input_hash(), verbose=False)
    cfg = load_config()
    box_index = {b['box_id']: b for b in inst['boxes']}
    pool, box_index, by_zone = build_pool(inst, geo, cfg, verbose=False)
    print('候选池 %d 条' % len(pool.items))
    base = baseline_greedy(pool, inst, cfg)
    print('基线：覆盖 %d/80 箱，%d 架次' % (base['n_covered'], len(base['chosen'])))
    met = evaluate_selection(base['chosen'], pool, inst, box_index)
    print(json.dumps({k: round(v, 2) if isinstance(v, float) else v
                      for k, v in met.items()
                      if k not in ('per_box_delivery', 'per_box_tardiness')},
                     ensure_ascii=False))
    res = solve_cpsat(pool, inst, cfg,
                      hint=dict(chosen=base['chosen'], makespan_s=met['makespan_s']))
    met2 = evaluate_selection(res['selection'], pool, inst, box_index)
    print('CP-SAT 解：', json.dumps({k: round(v, 2) if isinstance(v, float) else v
                                   for k, v in met2.items()
                                   if k not in ('per_box_delivery', 'per_box_tardiness')},
                                  ensure_ascii=False))
