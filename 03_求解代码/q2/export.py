# -*- coding: utf-8 -*-
"""P5：结果导出（模板填写、明细 CSV、校验报告、说明与图）。"""
import os
import csv
import json
import math
import openpyxl

from data_io import TEMPLATE


def _dump_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def _dump_csv(path, rows, header):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(h, '') for h in header])


def build_records(solution, pool, inst, geo, cfg):
    """把最终解展开成逐架次 / 逐箱 / 逐航段 / 逐设备记录。"""
    from simulator import simulate_route
    box_index = {b['box_id']: b for b in inst['boxes']}
    models = {c['candidate_id']: c for c in pool.items}
    sorties, legs, deliveries = [], [], []
    for s in solution['sorties']:
        c = models[s['candidate_id']]
        stops = [(z, list(ids)) for z, ids in c['stops']]
        ev = simulate_route(c['type_id'], stops, inst, geo, box_index)
        seq = ['O01'] + [z for z, _ in c['stops']] + ['O01']
        sorties.append(dict(
            sortie_id=s['sortie_id'], candidate_id=s['candidate_id'],
            aircraft_id=s['aircraft_id'], type_id=c['type_id'],
            battery_id=s['battery_id'], start_s=round(s['start_s'], 3),
            stop_sequence='→'.join(seq), n_boxes=c['n_boxes'],
            mass_kg=round(ev['initial_mass_kg'], 3),
            volume_m3=round(ev['initial_volume_m3'], 4),
            duration_s=round(ev['duration_s'], 3),
            return_soc_pct=round(100 * ev['return_soc'], 3),
            recharge_s=round(ev['recharge_s'], 3),
            energy_kwh=round(ev['energy_kwh'], 5),
            return_s=round(s['start_s'] + ev['duration_s'], 3),
            stops='; '.join('%s(%d箱)' % (z, len(ids)) for z, ids in c['stops'])))
        for b, off in ev['delivery_offsets'].items():
            deliveries.append(dict(box_id=b, sortie_id=s['sortie_id'],
                                   zone_id=box_index[b]['zone_id'],
                                   deliver_s=round(s['start_s'] + off, 3),
                                   deliver_offset_s=round(off, 3),
                                   expected_s=box_index[b]['expected_s'],
                                   hard_deadline_s=box_index[b]['hard_deadline_s'],
                                   hard_slack_s=(None if box_index[b]['hard_deadline_s'] is None
                                                 else round(box_index[b]['hard_deadline_s']
                                                            - (s['start_s'] + off), 3)),
                                   category=box_index[b]['category'],
                                   mass_kg=box_index[b]['mass_kg'],
                                   priority=box_index[b]['priority'],
                                   soft_tardiness_s=round(max(
                                       0.0, (s['start_s'] + off) - box_index[b]['expected_s'])
                                       * box_index[b]['priority'], 3)
                                   if box_index[b]['expected_s'] is not None else None))
        for k, leg in enumerate(ev['legs']):
            legs.append(dict(sortie_id=s['sortie_id'], leg_no=k + 1,
                             from_node=leg['from_node'], to_node=leg['to_node'],
                             d_km=round(leg['d_m'] / 1000, 4),
                             cruise_z_m=round(leg['cruise_z_m'], 2),
                             h_up_m=round(leg['h_up_m'], 2), h_dn_m=round(leg['h_dn_m'], 2),
                             payload_kg=round(leg['payload_kg'], 4),
                             time_s=round(leg['time_s'], 3),
                             energy_kwh=round(leg['energy_kwh'], 6)))
    return sorties, deliveries, legs


def write_template(out_path, sorties, deliveries):
    wb = openpyxl.load_workbook(TEMPLATE)
    ws = wb['Q2_运输架次']
    for k, s in enumerate(sorted(sorties, key=lambda r: r['start_s'])):
        r = 2 + k
        ws.cell(row=r, column=1, value=s['sortie_id'])
        ws.cell(row=r, column=2, value=s['aircraft_id'])
        ws.cell(row=r, column=3, value=s['type_id'])
        ws.cell(row=r, column=4, value=s['battery_id'])
        ws.cell(row=r, column=5, value=s['start_s'])
        ws.cell(row=r, column=6, value=s['stop_sequence'])
        ws.cell(row=r, column=7, value=s['return_s'])
        ws.cell(row=r, column=8, value=s['energy_kwh'])
    if ws.max_row > 1 + len(sorties):
        ws.delete_rows(2 + len(sorties), ws.max_row - 1 - len(sorties))
    ws2 = wb['Q2_逐箱交付']
    order = {s['sortie_id']: k for k, s in enumerate(sorted(sorties, key=lambda r: r['start_s']))}
    dl = sorted(deliveries, key=lambda d: (order[d['sortie_id']], d['deliver_s']))
    for k, d in enumerate(dl):
        r = 2 + k
        ws2.cell(row=r, column=1, value=d['box_id'])
        ws2.cell(row=r, column=2, value=d['sortie_id'])
        ws2.cell(row=r, column=3, value=d['zone_id'])
        ws2.cell(row=r, column=4, value=d['deliver_s'])
    if ws2.max_row > 1 + len(dl):
        ws2.delete_rows(2 + len(dl), ws2.max_row - 1 - len(dl))
    wb.save(out_path)
    return out_path


def make_figures(fig_dir, solution, pool, inst, geo, sorties):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    os.makedirs(fig_dir, exist_ok=True)
    models = {c['candidate_id']: c for c in pool.items}
    fig_paths = []

    # 1) 路线图（UTM 米制坐标）
    fig, ax = plt.subplots(figsize=(9, 7))
    for s in solution['sorties']:
        c = models[s['candidate_id']]
        seq = ['O01'] + [z for z, _ in c['stops']] + ['O01']
        for a, b in zip(seq[:-1], seq[1:]):
            (xa, ya), (xb, yb) = geo.xy[a], geo.xy[b]
            ax.plot([xa, xb], [ya, yb], '-', lw=1.0, alpha=0.65,
                    color={'A': '#2f7ed8', 'B': '#f2a03d', 'C': '#c0392b'}[c['type_id']])
        ax.plot([geo.xy[seq[-2]][0]], [geo.xy[seq[-2]][1]], 'o', ms=3.2,
                color={'A': '#2f7ed8', 'B': '#f2a03d', 'C': '#c0392b'}[c['type_id']])
    for nid, (x, y) in geo.xy.items():
        if nid == 'O01':
            ax.plot([x], [y], 's', ms=10, color='k')
            ax.annotate('O01 调度中心', (x, y), textcoords='offset points', xytext=(6, 6), fontsize=9)
        else:
            ax.plot([x], [y], 'o', ms=5, color='#555', zorder=5)
            ax.annotate(nid, (x, y), textcoords='offset points', xytext=(4, 4), fontsize=7.5)
    ax.set_title('第二问运输路线（按机型着色：A 蓝 / B 橙 / C 红）')
    ax.set_xlabel('UTM 49N 东向坐标 (m)')
    ax.set_ylabel('UTM 49N 北向坐标 (m)')
    ax.grid(alpha=0.25, ls=':')
    fig.tight_layout()
    p = os.path.join(fig_dir, 'route_map.png')
    fig.savefig(p, dpi=150)
    plt.close(fig)
    fig_paths.append(p)

    # 2) 无人机 / 电池甘特图
    for kind in ('aircraft', 'battery'):
        field = 'aircraft_id' if kind == 'aircraft' else 'battery_id'
        res_type = {}
        for r in sorties:
            res_type.setdefault(r[field], models[r['candidate_id']]['type_id'])
        ids = sorted(res_type)
        idx = {k: i for i, k in enumerate(ids)}
        fig, ax = plt.subplots(figsize=(11, 0.42 * len(ids) + 2.2))
        colors = {'A': '#2f7ed8', 'B': '#f2a03d', 'C': '#c0392b'}
        for r in sorties:
            c = models[r['candidate_id']]
            y = idx[r[field]]
            start, dur = r['start_s'], c['duration_s']
            ax.barh(y, dur, left=start, height=0.62, color=colors[c['type_id']],
                    edgecolor='k', linewidth=0.4)
            if kind == 'battery':
                ax.barh(y, c['recharge_s'], left=start + dur, height=0.62,
                        color='#bdc3c7', edgecolor='k', linewidth=0.4)
            ax.text(start + dur / 2, y, r['sortie_id'], ha='center', va='center',
                    fontsize=6.5, color='w')
        ax.set_yticks(range(len(ids)))
        ax.set_yticklabels(['%s(%s)' % (k, res_type[k]) for k in ids], fontsize=8)
        ax.set_xlabel('时间 (s)')
        ax.set_title('%s 使用时间线（灰 = 电池充电占用）' % ('运输无人机' if kind == 'aircraft' else '共享电池'))
        ax.grid(alpha=0.25, ls=':', axis='x')
        ax.invert_yaxis()
        fig.tight_layout()
        p = os.path.join(fig_dir, '%s_gantt.png' % kind)
        fig.savefig(p, dpi=150)
        plt.close(fig)
        fig_paths.append(p)
    return fig_paths


def write_summary(path, ctx):
    lines = []
    A = lines.append
    A('# 第二问结果说明（异构无人机多点多架次运输调度）\n')
    A('本文件由 `q2/main.py` 自动生成，记录统一口径、求解过程、指标与检验结论。\n')
    A('## 1. 建模与计算口径\n')
    for a in ctx['cfg']['assumptions']:
        A('- %s' % a)
    A('')
    A('目标按词典序推进：加权超期量 → 全部任务完成时间 → 总运输能耗 → 架次数；'
      '医疗物资与首批保障货箱的硬时限为硬约束。\n')
    A('## 2. 输入对账\n')
    for name, ok, info in ctx['input_checks']:
        A('- %s：%s（%s）' % (name, '通过' if ok else '失败', info))
    A('')
    A('## 3. 候选架次池\n')
    A('- 候选数 %d 条；来源分布 %s' % (ctx['pool_stats']['n'], ctx['pool_stats']['by_source']))
    A('- 机型分布 %s；多点候选 %d 条（最多 %d 站）'
      % (ctx['pool_stats']['by_type'], ctx['pool_stats']['multi'], ctx['pool_stats']['max_stops']))
    A('')
    A('## 4. 指标对比（基线 → 最终方案）\n')
    A('| 指标 | 基线（候选池列表调度） | 最终方案 | 变化 |')
    A('| --- | ---: | ---: | ---: |')
    for k, label, fmt in [('weighted_tardiness', '加权超期量（Σω·迟延 s）', '%.0f'),
                          ('makespan_s', '全部任务完成时间（s）', '%.0f'),
                          ('energy_kwh', '总运输能耗（kWh）', '%.2f'),
                          ('n_sorties', '架次数', '%d')]:
        b, f = ctx['baseline'][k], ctx['final'][k]
        d = f - b
        A('| %s | %s | %s | %s |' % (label, fmt % b, fmt % f,
                                     ('%+s' % (fmt % d)) if k != 'weighted_tardiness'
                                     else ('%+.0f' % d)))
    A('')
    A('## 5. 求解状态与证明范围\n')
    A('| 阶段 | 状态 | 目标值 | 下界 | 用时(s) |')
    A('| --- | --- | ---: | ---: | ---: |')
    for r in ctx['solve_reports']:
        A('| %s | %s | %.4g | %.4g | %.1f |' % (r['phase'], r['status'], r['objective'],
                                                r['best_bound'], r['elapsed_s']))
    A('')
    A('上述最优性结论仅对当前候选池、1 s 起始时刻网格与所选建模口径成立；'
      '未穷尽全部可行路线，因此表述为“当前候选集合内的最优解”。\n')
    A('## 6. 独立校验结论\n')
    v = ctx['validation']
    A('- 货箱覆盖：%s（80 箱恰好一次）' % ('通过' if v['box_coverage']['ok'] else '失败'))
    A('- 硬时限：%s，最小余量 %.1f s（%s）'
      % ('通过' if v['deadlines']['ok'] else '失败',
         v['deadlines']['min_hard_slack_s'] or 0, v['deadlines']['worst_box']))
    A('- 载荷与能量：%s，逐航段明细 %d 段'
      % ('通过' if v['payload_energy']['ok'] else '失败', v['payload_energy']['n_legs']))
    A('- 资源占用：无人机峰值同时占用 %d，电池峰值 %d，编号分配无冲突'
      % (v['resources']['aircraft']['peak_concurrent'], v['resources']['battery']['peak_concurrent']))
    A('- 汇总指标一致性：%s' % ('通过' if v['metrics']['consistent'] else '失败'))
    A('')
    A('## 7. 交付文件\n')
    for f in ctx['outputs']:
        A('- `%s`' % os.path.basename(f))
    A('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return path
