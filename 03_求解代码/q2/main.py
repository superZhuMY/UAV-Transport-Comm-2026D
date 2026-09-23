# -*- coding: utf-8 -*-
"""第二问主流程：prepare -> candidates -> baseline -> solve -> validate -> export。

用法：
  python main.py            # 跑完整链路
  python main.py prepare    # 只做 P0/P1（输入对账、几何、物理核对）
"""
import os
import sys
import json
import time
import shutil

from data_io import load_instance, load_config, input_hash, RUN_DIR, BASE_DIR
from geometry import Geometry
from physics import max_safe_payload
from candidates import build_pool
from scheduling import baseline_greedy, evaluate_selection, solve_cpsat
from assign_ids import assign_ids
from validator import validate
import export as exporter


def _cosine_safe(obj):
    return json.loads(json.dumps(obj, default=str))


def main(argv=None):
    argv = argv or sys.argv
    stage = argv[1] if len(argv) > 1 else 'all'
    t0 = time.time()
    cfg = load_config()
    if len(argv) > 2:
        cfg['run_id'] = argv[2]
    if len(argv) > 3:
        # 受控比较：把目标优先级换成“超期量 → 总能耗 → 完工时刻 → 架次数”
        mode = argv[3]
        if mode == 'energy_first':
            cfg['objective_order'] = ['weighted_tardiness', 'energy', 'makespan',
                                      'sortie_count']
        elif mode == 'sorties_first':
            cfg['objective_order'] = ['weighted_tardiness', 'sortie_count',
                                      'makespan', 'energy']
        print('[config] 目标优先序改为: %s' % cfg['objective_order'])
    run_dir = os.path.join(RUN_DIR, cfg['run_id'])
    os.makedirs(run_dir, exist_ok=True)
    fig_dir = os.path.join(run_dir, 'figures')

    print('=' * 96)
    print('第二问：异构无人机多点多架次运输调度    run_id = %s' % cfg['run_id'])
    print('=' * 96)
    inst = load_instance()
    geo = Geometry(inst['nodes'])
    geo.build_segments(cache_key=input_hash())
    ver = geo.verification()
    print('[P1a] 几何验收：正反向距离/最高地形差 %.3g m，爬升下降互换差 %.3g m，越界航段 %d 条'
          % (ver['max_symmetry_gap_m'], ver['max_climb_descent_swap_gap_m'],
             len(ver['out_of_coverage'])))
    print()

    if stage == 'prepare':
        print('载荷能力矩阵（与第一问交叉核对）：')
        for z in sorted(n for n in inst['nodes'] if n != 'O01'):
            cells = []
            for g in ('A', 'B', 'C'):
                q = max_safe_payload(inst['types'][g], geo, z)
                cells.append('不可行' if q is None else '%.1f' % q)
            print('   %-6s %s' % (z, ' / '.join(cells)))
        return 0

    if stage == 'validate':
        # 只读已保存的解，重新独立复算（用于复现与复核）
        from candidates import build_pool as _bp
        pool, box_index, _ = _bp(inst, geo, cfg, verbose=False)
        with open(os.path.join(run_dir, 'solution.json'), encoding='utf-8') as f:
            saved = json.load(f)
        solution = dict(sorties=saved['sorties'], metrics=saved['metrics'])
        report = validate(solution, pool, inst, geo, cfg)
        print('[P5] 复算 %s：总体 %s' % (cfg['run_id'], '通过' if report['overall_ok'] else '失败'))
        print('     硬时限违规 %d 箱，最小余量 %.1f s；逐航段 %d 段；无人机峰值 %d，电池峰值 %d'
              % (report['deadlines']['n_violation'],
                 report['deadlines']['min_hard_slack_s'] or 0,
                 report['payload_energy']['n_legs'],
                 report['resources']['aircraft']['peak_concurrent'],
                 report['resources']['battery']['peak_concurrent']))
        return 0

    # ------------------------------ 候选池 ------------------------------ #
    pool, box_index, by_zone = build_pool(inst, geo, cfg, verbose=True)
    from collections import Counter
    pool_stats = dict(n=len(pool.items),
                      by_source=dict(Counter(c['source'] for c in pool.items)),
                      by_type=dict(Counter(c['type_id'] for c in pool.items)),
                      multi=len([c for c in pool.items if len(c['stops']) > 1]),
                      max_stops=max(len(c['stops']) for c in pool.items))
    print()

    # ------------------------------ 基线 -------------------------------- #
    base = baseline_greedy(pool, inst, cfg)
    base_metrics = evaluate_selection(base['chosen'], pool, inst, box_index)
    print('[P2] 基线（候选池列表调度）：覆盖 %d/80 箱，%d 架次，硬时限全部满足=%s，'
          '最小硬时限余量 %.0f s' % (base['n_covered'], len(base['chosen']),
                                  base_metrics['hard_ok'], base_metrics['min_hard_slack_s']))
    print('     加权超期量 %.0f，完成时间 %.0f s，总能耗 %.2f kWh'
          % (base_metrics['weighted_tardiness'], base_metrics['makespan_s'],
             base_metrics['energy_kwh']))
    print()

    # ------------------------------ 联合调度 ---------------------------- #
    cfg['horizon_slack_s'] = 20000
    res = solve_cpsat(pool, inst, cfg,
                      hint=dict(chosen=base['chosen'], makespan_s=base_metrics['makespan_s']),
                      verbose=True)
    sel = res['selection']
    final = evaluate_selection(sel, pool, inst, box_index)
    print('[P3] 最终方案：%d 架次，加权超期量 %.0f，完成时间 %.0f s，总能耗 %.2f kWh，'
          '硬时限全部满足=%s，最小余量 %.0f s'
          % (len(sel), final['weighted_tardiness'], final['makespan_s'],
             final['energy_kwh'], final['hard_ok'], final['min_hard_slack_s']))
    print()

    # ------------------------------ 编号分配 ---------------------------- #
    rows, problems = assign_ids(sel, pool, inst)
    if problems:
        raise RuntimeError('设备/电池编号分配失败：%s' % problems[:3])
    print('[P3] 设备编号分配完成：%d 架次，无人机 %d 台、电池 %d 组'
          % (len(rows), len({r['aircraft_id'] for r in rows}),
             len({r['battery_id'] for r in rows})))
    print()

    solution = dict(sorties=rows,
                    metrics={k: final[k] for k in
                             ('weighted_tardiness', 'makespan_s', 'energy_kwh',
                              'n_sorties', 'sum_priority_times_delivery')},
                    status=res['status'], solver=res['reports'])

    # ------------------------------ 独立校验 ---------------------------- #
    report = validate(solution, pool, inst, geo, cfg)
    print('[P5] 独立校验：总体 %s' % ('通过' if report['overall_ok'] else '失败'))
    print('     货箱覆盖 %s；硬时限违规 %d 箱（最小余量 %.1f s，%s）'
          % ('OK' if report['box_coverage']['ok'] else 'FAIL',
             report['deadlines']['n_violation'],
             report['deadlines']['min_hard_slack_s'] or 0,
             report['deadlines']['worst_box']))
    print('     载荷与能量 %s（逐航段 %d 段，总能耗 %.3f kWh）'
          % ('OK' if report['payload_energy']['ok'] else 'FAIL',
             report['payload_energy']['n_legs'],
             report['payload_energy']['total_energy_kwh']))
    print('     资源占用 %s（无人机峰值 %d，电池峰值 %d）'
          % ('OK' if report['resources']['ok'] else 'FAIL',
             report['resources']['aircraft']['peak_concurrent'],
             report['resources']['battery']['peak_concurrent']))
    print('     指标一致性 %s' % ('OK' if report['metrics']['consistent'] else 'FAIL'))
    print()

    # ------------------------------ 导出 -------------------------------- #
    sorties, deliveries, legs = exporter.build_records(solution, pool, inst, geo, cfg)
    outputs = []
    xlsx = exporter.write_template(os.path.join(run_dir, 'Q2结果.xlsx'), sorties, deliveries)
    outputs.append(xlsx)
    _dump = exporter._dump_json
    _dump(os.path.join(run_dir, 'solution.json'), _cosine_safe(
        dict(sorties=sorties, deliveries=deliveries, metrics=solution['metrics'],
             status=solution['status'], solver=solution['solver'],
             aircraft=inst['aircraft'], batteries=inst['batteries'])))
    _dump(os.path.join(run_dir, 'validation.json'), _cosine_safe(
        {k: v for k, v in report.items() if k != 'detail'}))
    _dump(os.path.join(run_dir, 'validation_detail.json'), _cosine_safe(report['detail']))
    _dump(os.path.join(run_dir, 'run_manifest.json'), _cosine_safe(dict(
        run_id=cfg['run_id'], input_hash=input_hash(), config=cfg,
        pool_stats=pool_stats, solver_reports=res['reports'], status=res['status'],
        baseline={k: base_metrics[k] for k in
                  ('weighted_tardiness', 'makespan_s', 'energy_kwh', 'n_sorties')},
        final_metrics=solution['metrics'],
        elapsed_s=time.time() - t0)))
    exporter._dump_csv(os.path.join(run_dir, 'transport_sorties.csv'), sorties,
                       ['sortie_id', 'aircraft_id', 'type_id', 'battery_id', 'start_s',
                        'stop_sequence', 'stops', 'n_boxes', 'mass_kg', 'volume_m3',
                        'duration_s', 'return_s', 'return_soc_pct', 'recharge_s',
                        'energy_kwh'])
    exporter._dump_csv(os.path.join(run_dir, 'box_deliveries.csv'), deliveries,
                       ['box_id', 'sortie_id', 'zone_id', 'deliver_s', 'deliver_offset_s',
                        'expected_s', 'hard_deadline_s', 'hard_slack_s', 'category',
                        'mass_kg', 'priority', 'soft_tardiness_s'])
    exporter._dump_csv(os.path.join(run_dir, 'flight_legs.csv'), legs,
                       ['sortie_id', 'leg_no', 'from_node', 'to_node', 'd_km',
                        'cruise_z_m', 'h_up_m', 'h_dn_m', 'payload_kg', 'time_s',
                        'energy_kwh'])
    ac_rows, bat_rows = [], []
    models = {c['candidate_id']: c for c in pool.items}
    for r in sorted(rows, key=lambda x: x['start_s']):
        c = models[r['candidate_id']]
        ac_rows.append(dict(sortie_id=r['sortie_id'], aircraft_id=r['aircraft_id'],
                            type_id=r['type_id'], event='准备-装载-飞行-交接-返场',
                            start_s=round(r['start_s'], 3),
                            end_s=round(r['start_s'] + c['duration_s'], 3),
                            duration_s=round(c['duration_s'], 3)))
        bat_rows.append(dict(sortie_id=r['sortie_id'], battery_id=r['battery_id'],
                             type_id=r['type_id'],
                             assign_s=round(r['start_s'], 3),
                             return_s=round(r['start_s'] + c['duration_s'], 3),
                             return_soc_pct=round(100 * c['return_soc'], 3),
                             charge_end_s=round(r['start_s'] + c['duration_s']
                                                + c['recharge_s'], 3),
                             recharge_s=round(c['recharge_s'], 3)))
    exporter._dump_csv(os.path.join(run_dir, 'aircraft_timeline.csv'), ac_rows,
                       ['sortie_id', 'aircraft_id', 'type_id', 'event', 'start_s',
                        'end_s', 'duration_s'])
    exporter._dump_csv(os.path.join(run_dir, 'battery_timeline.csv'), bat_rows,
                       ['sortie_id', 'battery_id', 'type_id', 'assign_s', 'return_s',
                        'return_soc_pct', 'recharge_s', 'charge_end_s'])
    figs = exporter.make_figures(fig_dir, solution, pool, inst, geo, sorties)
    md = exporter.write_summary(os.path.join(run_dir, 'Q2结果说明.md'), dict(
        cfg=cfg, input_checks=inst['input_checks'], pool_stats=pool_stats,
        baseline={k: base_metrics[k] for k in
                  ('weighted_tardiness', 'makespan_s', 'energy_kwh', 'n_sorties')},
        final=solution['metrics'], solve_reports=res['reports'], validation=report,
        outputs=[xlsx] + [os.path.join(run_dir, f) for f in
                          ('solution.json', 'validation.json', 'transport_sorties.csv',
                           'box_deliveries.csv', 'flight_legs.csv',
                           'aircraft_timeline.csv', 'battery_timeline.csv')] + figs))
    for f in [md] + figs + [os.path.join(run_dir, n) for n in
                            ('solution.json', 'validation.json', 'validation_detail.json',
                             'run_manifest.json', 'transport_sorties.csv',
                             'box_deliveries.csv', 'flight_legs.csv',
                             'aircraft_timeline.csv', 'battery_timeline.csv')]:
        outputs.append(f)
    # 项目目录留一份便于查看的结果副本
    top = os.path.join(BASE_DIR, 'Q2结果.xlsx')
    shutil.copyfile(xlsx, top)
    print('[P5] 导出完成，运行目录：%s' % run_dir)
    print('     总用时 %.1f s' % (time.time() - t0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
