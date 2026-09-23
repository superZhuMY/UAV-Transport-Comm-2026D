# -*- coding: utf-8 -*-
"""P1b：飞行时间、能耗与充电周转的统一计算口径（第二问与第一问一致）。

能耗换算为统一建模假设：
  水平: E_hor = E_use * d / L(q)
  爬升: E_up  = (m + q) * 9.81 * h_up / (eta_up * 3.6e6)      [kWh]
  下降: 不另计附加能耗（附件下降能耗效率为 0）
整趟上限：E_total <= (1 - rho) * E_use，安全余量仅扣一次。
"""
import math

G = 9.81


def equivalent_range(t, q_kg):
    """载荷 q 下的等效航程（m）。q 会被截断到 [0, Q]。"""
    q = min(max(q_kg, 0.0), t['cap_mass_kg'])
    ratio = q / t['cap_mass_kg']
    return t['range_empty_m'] - (t['range_empty_m'] - t['range_full_m']) * ratio ** 1.5


def leg_energy(t, d_m, h_up_m, q_kg):
    """单航段能耗（kWh）：水平 + 爬升。"""
    e = t['energy_kwh'] * d_m / equivalent_range(t, q_kg)
    if h_up_m > 0:
        e += (t['mass_kg'] + q_kg) * G * h_up_m / (t['eta_up'] * 3.6e6)
    return e


def leg_time(t, d_m, h_up_m, h_dn_m):
    """单航段飞行时间（s）：爬升 + 巡航 + 下降。"""
    return (max(h_up_m, 0.0) / t['v_up'] + d_m / t['v_cruise']
            + max(h_dn_m, 0.0) / t['v_down'])


def return_soc(t, energy_kwh):
    return 1.0 - energy_kwh / t['energy_kwh']


def charge_time(t, soc):
    """两阶段等效充电模型：SOC<90% 的快充段占等效完全充电时间 65%，90%~100% 占 35%。"""
    tf = t['full_charge_s']
    if soc >= 0.9:
        return tf * 0.35 * (1.0 - soc) / 0.1
    return tf * (0.65 * (0.9 - soc) / 0.9 + 0.35)


def max_safe_payload(t, geo, zone, tol=1e-6):
    """单点往返最大安全载荷（kg）；不满足空载往返时返回 None。

    仅用于与第一问结果交叉核对，第二问多点架次按整条路线逐段核算。
    """
    out = geo.seg_of('O01', zone)
    back = geo.seg_of(zone, 'O01')
    limit = t['energy_usable_kwh']

    def e_rt(q):
        return (leg_energy(t, out['d_m'], out['h_up_m'], q)
                + leg_energy(t, back['d_m'], back['h_up_m'], 0.0))

    if e_rt(0.0) > limit:
        return None
    if e_rt(t['cap_mass_kg']) <= limit:
        return t['cap_mass_kg']
    lo, hi = 0.0, t['cap_mass_kg']
    while hi - lo > 1e-7:
        mid = (lo + hi) / 2
        if e_rt(mid) <= limit:
            lo = mid
        else:
            hi = mid
    return lo


def direct_sortie_cost(t, geo, zone, mass_kg, n_boxes):
    """单点往返的能耗与作业时间，供第一问交叉核对。"""
    out = geo.seg_of('O01', zone)
    back = geo.seg_of(zone, 'O01')
    e = (leg_energy(t, out['d_m'], out['h_up_m'], mass_kg)
         + leg_energy(t, back['d_m'], back['h_up_m'], 0.0))
    tt = (t['t_prep_s'] + t['t_load_s'] * n_boxes
          + leg_time(t, out['d_m'], out['h_up_m'], out['h_dn_m'])
          + t['t_hand_base_s'] + t['t_hand_box_s'] * n_boxes
          + leg_time(t, back['d_m'], back['h_up_m'], back['h_dn_m']))
    return e, tt


if __name__ == '__main__':
    from data_io import load_instance, input_hash
    from geometry import Geometry
    inst = load_instance(verbose=False)
    geo = Geometry(inst['nodes'])
    geo.build_segments(cache_key=input_hash(), verbose=True)
    types = inst['types']
    print()
    print('%-6s %-8s %-9s %-9s | %s' % ('服务区', '距离km', '巡航m', '最高地形m',
                                        '最大安全载荷 A / B / C (kg)'))
    print('-' * 92)
    for z in sorted(n for n in inst['nodes'] if n != 'O01'):
        s = geo.seg_of('O01', z)
        cells = []
        for g in ('A', 'B', 'C'):
            q = max_safe_payload(types[g], geo, z)
            cells.append('不可行' if q is None else '%.1f' % q)
        print('%-8s %-9.2f %-10.1f %-11.1f | %s' % (z, s['d_m'] / 1000, s['cruise_z_m'],
                                                    s['max_z_m'], ' / '.join(cells)))
    print()
    print('充电模型核对：')
    for g in ('A', 'B', 'C'):
        t = types[g]
        print('  %s T_full=%.0f s -> c(0)=%.1f c(0.9)=%.1f c(1.0)=%.1f'
              % (g, t['full_charge_s'], charge_time(t, 0.0), charge_time(t, 0.9),
                 charge_time(t, 1.0)))
