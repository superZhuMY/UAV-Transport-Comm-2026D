# -*- coding: utf-8 -*-
"""P1c：单架次全过程仿真器。

simulate_route(type_id, stops, instance, geo, box_index)
  stops: [(zone_id, [box_id, ...]), ...] 按访问顺序给出，每个停靠事件至少交付 1 箱；
  返回 RouteEvaluation（相对时间偏移，不含实际开始时刻）。

统一口径：同一停靠点的所有货箱在“基础交接 + 逐箱增加交接”全部结束后记为交付完成；
下一航段在此之后开始；每站交接完成后从作业高度重新爬升至该航段巡航海拔。
"""
from physics import leg_energy, leg_time, charge_time


class RouteEvaluation(dict):
    """轻量结果对象，按字典访问（便于序列化）。"""


def simulate_route(type_id, stops, inst, geo, box_index):
    t = inst['types'][type_id]
    boxes = []
    for zone, ids in stops:
        if not ids:
            return RouteEvaluation(feasible=False, rejection_reasons=['empty_stop'])
        for bid in ids:
            b = box_index[bid]
            if b['zone_id'] != zone:
                return RouteEvaluation(feasible=False, rejection_reasons=['zone_mismatch:%s' % bid])
            boxes.append(b)
    if len({b['box_id'] for b in boxes}) != len(boxes):
        return RouteEvaluation(feasible=False, rejection_reasons=['duplicate_box'])

    mass_g = sum(b['mass_g'] for b in boxes)
    vol_cc = sum(b['vol_cc'] for b in boxes)
    reasons = []
    if mass_g > t['cap_mass_kg'] * 1000 + 1e-6:
        reasons.append('over_mass')
    if vol_cc > t['cap_volume_m3'] * 1e6 + 1e-6:
        reasons.append('over_volume')
    if reasons:
        return RouteEvaluation(feasible=False, rejection_reasons=reasons,
                               initial_mass_kg=mass_g / 1000.0,
                               initial_volume_m3=vol_cc / 1e6)

    q = mass_g / 1000.0
    initial_mass_kg = q
    n_total = len(boxes)
    clock = t['t_prep_s'] + t['t_load_s'] * n_total
    legs = []
    stop_records = []
    offsets = {}
    energy = 0.0
    cur = 'O01'
    for zone, ids in stops:
        seg = geo.seg_of(cur, zone)
        e = leg_energy(t, seg['d_m'], seg['h_up_m'], q)
        tt = leg_time(t, seg['d_m'], seg['h_up_m'], seg['h_dn_m'])
        energy += e
        clock += tt
        arrive = clock
        hand = t['t_hand_base_s'] + t['t_hand_box_s'] * len(ids)
        clock += hand
        for bid in ids:
            offsets[bid] = clock
        seg_mass = q
        q -= sum(box_index[b]['mass_g'] for b in ids) / 1000.0
        legs.append(dict(from_node=cur, to_node=zone, d_m=seg['d_m'],
                         cruise_z_m=seg['cruise_z_m'], h_up_m=seg['h_up_m'],
                         h_dn_m=seg['h_dn_m'], payload_kg=seg_mass,
                         time_s=tt, energy_kwh=e))
        stop_records.append(dict(zone_id=zone, box_ids=list(ids), n_boxes=len(ids),
                                 arrive_work_alt_s=arrive, handover_s=hand,
                                 delivery_done_s=clock))
        cur = zone
    seg = geo.seg_of(cur, 'O01')
    e = leg_energy(t, seg['d_m'], seg['h_up_m'], q)
    tt = leg_time(t, seg['d_m'], seg['h_up_m'], seg['h_dn_m'])
    energy += e
    clock += tt
    legs.append(dict(from_node=cur, to_node='O01', d_m=seg['d_m'],
                     cruise_z_m=seg['cruise_z_m'], h_up_m=seg['h_up_m'],
                     h_dn_m=seg['h_dn_m'], payload_kg=q, time_s=tt, energy_kwh=e))
    if q > 1e-9:
        reasons.append('payload_not_empty_on_return')
    if energy > t['energy_usable_kwh'] + 1e-9:
        reasons.append('over_energy')
    soc = 1.0 - energy / t['energy_kwh']
    if soc < t['reserve_pct'] / 100.0 - 1e-9:
        reasons.append('below_reserve_soc')
    recharge = charge_time(t, soc)
    return RouteEvaluation(
        feasible=not reasons, rejection_reasons=reasons, type_id=type_id,
        initial_mass_kg=initial_mass_kg, initial_volume_m3=vol_cc / 1e6,
        energy_kwh=energy, return_soc=soc, duration_s=clock, recharge_s=recharge,
        delivery_offsets=offsets, legs=legs, stops=stop_records,
        mass_margin_kg=t['cap_mass_kg'] - initial_mass_kg,
        volume_margin_m3=t['cap_volume_m3'] - vol_cc / 1e6,
        energy_margin_kwh=t['energy_usable_kwh'] - energy)


if __name__ == '__main__':
    import json
    from data_io import load_instance, input_hash
    from geometry import Geometry
    inst = load_instance(verbose=False)
    geo = Geometry(inst['nodes'])
    geo.build_segments(cache_key=input_hash(), verbose=False)
    bidx = {b['box_id']: b for b in inst['boxes']}
    # 手工用例：O01→S006→S007→O01，各站两箱（首小时医疗 + 饮用水）
    stops = [('S006', ['S006-MED-01', 'S006-WAT-01']),
             ('S007', ['S007-MED-01', 'S007-WAT-01'])]
    for g in ('A', 'B', 'C'):
        ev = simulate_route(g, stops, inst, geo, bidx)
        if not ev['feasible']:
            print('%s 型 O01→S006→S007→O01: 不可行 %s' % (g, ev['rejection_reasons']))
            continue
        print('%s 型 O01→S006→S007→O01: 可行 载重=%.0fkg 体积=%.3fm3 能耗=%.3fkWh '
              '返航SOC=%.1f%% 时长=%.0fs 充电=%.0fs 逐箱交付=%s'
              % (g, ev['initial_mass_kg'], ev['initial_volume_m3'],
                 ev['energy_kwh'], 100 * ev['return_soc'], ev['duration_s'],
                 ev['recharge_s'], {k: round(v) for k, v in ev['delivery_offsets'].items()}))
    # 逐段载荷核验
    ev = simulate_route('C', stops, inst, geo, bidx)
    print('逐航段:', [(l['from_node'] + '->' + l['to_node'], round(l['payload_kg'], 1),
                    round(l['energy_kwh'], 3), round(l['time_s'], 1)) for l in ev['legs']])
